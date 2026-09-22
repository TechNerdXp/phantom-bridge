"""The collector's output-priority autopilot.

Glue between the pure policy and the running link: builds the profile from
the last days (in a thread, once a day -- yesterday's log is tens of
megabytes), asks the Governor every cycle, confirms what the inverter is
actually set to, and -- only with AUTO_PRIORITY on -- writes POP01 / POP02
and reads QPIRI back to prove it moved. With the switch off it does all of
that except the write, so the decisions can be watched for a few nights in
the log, the tray and the watch screen before the inverter is handed the
wheel.

Everything it learns each cycle is returned as a small dict that
bridge.write_state publishes as `policy` in logs/state.json.

That payload is a CONTRACT. Switch-X (D:/PowerTools/switch-x) reads it
instead of recomputing the night, and a renamed key breaks it quietly.
Stable keys: want, phase, release_at, turnaround, usable_wh, need_wh,
floor, current -- plus, since 2026-09-22, until, headroom_wh and request.
Add keys freely; never rename or drop one.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import bridge
import config
import days
import policy


def profile_kwargs() -> dict:
    """config -> policy.build_profile keyword arguments. One place, so the
    collector and `ctl.py auto` cannot read the config differently."""
    return {
        "default_turnaround": config.AUTO_DEFAULT_TURNAROUND,
        "default_dusk": config.AUTO_DEFAULT_DUSK,
        "default_load_w": config.AUTO_DEFAULT_NIGHT_LOAD_W,
        "dusk_fraction": config.AUTO_DUSK_PV_FRACTION,
        "pack_wh": config.BATTERY_PACK_WH,
        "capacity_ah": config.BATTERY_CAPACITY_AH,
        "fallback_pack_wh": config.AUTO_FALLBACK_PACK_WH,
        "pack_cap_wh": config.BATTERY_PACK_WH_CAP,
        "fallback_efficiency": config.AUTO_EFFICIENCY_FALLBACK,
    }


def recent_days(store: days.DayStore, today: dt.date, count: int) -> list:
    """The last `count` finished days' analyses, yesterday first, skipping
    days with no samples."""
    out = []
    for back in range(1, count + 1):
        rec = store.day(today - dt.timedelta(days=back))
        if rec.get("samples"):
            out.append(rec)
    return out


def build_for(store: days.DayStore, today: dt.date) -> policy.Profile:
    prof = policy.build_profile(recent_days(store, today, config.AUTO_HISTORY_DAYS),
                                **profile_kwargs())
    prof.built_for = today
    return prof


class Autopilot:
    def __init__(self, log, initial_priority: int | None):
        self.log = log
        self.store = days.DayStore()
        self.governor = policy.Governor(config.AUTO_SOC_FLOOR, config.AUTO_SOC_RESUME,
                                        margin_wh=config.AUTO_RELEASE_MARGIN_WH,
                                        request_max_min=config.AUTO_REQUEST_MAX_MIN)
        self.last_request_key = None
        self.profile: policy.Profile | None = None
        self._building: dt.date | None = None
        self.current = initial_priority          # QPIRI output_source_prio
        self.current_at = time.time()
        self.last_write_at = 0.0
        self.backoff_until = 0.0
        self.last_write: dict | None = None
        self.last_key = None
        self.stood_down = False

    # -- the profile ---------------------------------------------------------

    def _build(self, today: dt.date) -> None:
        started = time.time()
        try:
            prof = build_for(self.store, today)
        except Exception as exc:                  # noqa: BLE001 -- never the loop
            self.log({"event": "priority-plan-failed", "error": str(exc)})
            if self.profile is None:
                self.profile = policy.build_profile([], **profile_kwargs())
        else:
            self.profile = prof
            self.log({"event": "priority-plan", "seconds": round(time.time() - started, 1),
                      **prof.as_dict()})
        finally:
            self._building = None

    def ensure_profile(self, now: dt.datetime) -> None:
        today = now.date()
        if self.profile is not None and self.profile.built_for == today:
            return
        if self._building == today:
            return
        self._building = today
        if self.profile is None:
            # Something to decide from while yesterday is being read.
            self.profile = policy.build_profile([], **profile_kwargs())
        threading.Thread(target=self._build, args=(today,), daemon=True,
                         name="priority-plan").start()

    # -- one cycle -----------------------------------------------------------

    def step(self, conn, now: dt.datetime, analysis: dict) -> dict:
        self.ensure_profile(now)
        prof = self.profile
        soc = analysis.get("batt_soc")
        naive = now.replace(tzinfo=None)
        request = policy.parse_request(bridge.read_request(), naive)
        decision = self.governor.decide(prof, naive, soc, request=request,
                                        grid_present=bool(analysis.get("grid_present", True)))

        req_key = (decision.request or {}).get("state"), request.key if request else None
        if req_key != self.last_request_key:
            self.last_request_key = req_key
            if decision.request:
                self.log({"event": "priority-request", **decision.request})
        key = (decision.want, decision.phase)
        if key != self.last_key:
            self.last_key = key
            self.log({"event": "priority-decision", "want": decision.name,
                      "phase": decision.phase, "reason": decision.reason,
                      "soc": soc, "current": policy.NAMES.get(self.current),
                      "enabled": bool(config.AUTO_PRIORITY),
                      "release_at": decision.release_at.strftime("%H:%M")
                      if decision.release_at else None,
                      "until": decision.until.strftime("%H:%M") if decision.until else None,
                      "need_wh": round(decision.need_wh),
                      "usable_wh": round(decision.usable_wh),
                      "headroom_wh": round(decision.headroom_wh)})

        # What is the inverter actually set to? Its own timer, or a hand on
        # the panel, can change it under us.
        if conn is not None and time.time() - self.current_at >= config.AUTO_VERIFY_INTERVAL_S:
            seen = bridge.read_output_priority(conn, self.log)
            self.current_at = time.time()
            if seen is not None and seen != self.current:
                self.log({"event": "priority-external-change",
                          "from": policy.NAMES.get(self.current, self.current),
                          "to": policy.NAMES.get(seen, seen),
                          "hint": "the inverter's own timer (menu 99) or the panel"})
                self.current = seen

        if config.AUTO_PRIORITY and conn is not None:
            self._maybe_write(conn, decision)

        # The contract with Switch-X -- see the module docstring. Add, never
        # rename.
        payload = {
            "enabled": bool(config.AUTO_PRIORITY),
            "want": decision.name, "want_code": decision.want,
            "current": policy.NAMES.get(self.current), "current_code": self.current,
            "phase": decision.phase, "reason": decision.reason,
            "release_at": decision.release_at.strftime("%H:%M") if decision.release_at else None,
            "until": decision.until.strftime("%H:%M") if decision.until else None,
            "request": decision.request,
            "turnaround": policy.fmt_hm(prof.turnaround_min),
            "dusk": policy.fmt_hm(prof.dusk_min),
            "pack_wh": round(prof.pack_wh),
            "pack_wh_basis": prof.notes.get("pack_wh"),
            "efficiency": round(prof.efficiency, 2),
            "usable_wh": round(decision.usable_wh),
            "need_wh": round(decision.need_wh),
            "headroom_wh": round(decision.headroom_wh),
            "margin_wh": config.AUTO_RELEASE_MARGIN_WH,
            "floor": config.AUTO_SOC_FLOOR, "resume": config.AUTO_SOC_RESUME,
            "floor_hold": decision.floor_hold,
            "profile_days": prof.days,
            "profile_for": prof.built_for.isoformat() if prof.built_for else None,
            "last_write": self.last_write,
        }
        return payload

    def _maybe_write(self, conn, decision: policy.Decision) -> None:
        if self.current not in (policy.SUB, policy.SBU):
            # UTI, or unknown: the owner has set something this policy is
            # not about, or we have not read it yet. Stand down, once.
            if not self.stood_down:
                self.stood_down = True
                self.log({"event": "priority-stand-down",
                          "current": policy.NAMES.get(self.current, self.current),
                          "why": "the inverter is not on SUB or SBU"})
            return
        self.stood_down = False
        if decision.want == self.current:
            return
        now = time.time()
        if now < self.backoff_until:
            return
        # The floor is protective and a request is an ask for now, not in
        # ten minutes; both skip the dwell. A request is bounded by its own
        # cap, so it cannot turn the dwell into a flap.
        protective = decision.phase in ("floor", "requested")
        if not protective and now - self.last_write_at < config.AUTO_MIN_DWELL_S:
            return
        result = bridge.set_output_priority(conn, decision.want, self.log,
                                            announce=lambda *a: None,
                                            why=decision.reason)
        self.last_write_at = now
        self.last_write = {"at": bridge.now_site().isoformat(timespec="seconds"),
                           "want": decision.name, "ok": bool(result.get("ok")),
                           "action": result.get("action"),
                           "verdict": result.get("verdict")}
        if result.get("ok"):
            self.current = decision.want
            self.current_at = now
        else:
            self.backoff_until = now + config.AUTO_MIN_DWELL_S
