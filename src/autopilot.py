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
floor, current -- plus, since 2026-09-22, until, headroom_wh, request and
tape. Add keys freely; never rename or drop one.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import bridge
import config
import days
import policy
import sun

# How many marks of the day's tape are kept. A day has two or three plan
# changes and a handful of disagreements; the cap is only there so a
# flapping reading cannot grow the published state without bound.
TAPE_MAX = 96

# And how far back they are kept. The watch screen's rule runs sunrise to
# sunrise, so the night it draws straddles midnight and a tape wiped at
# midnight would lose half of it. 36 hours covers any such window with
# room for a late sunrise.
TAPE_KEEP_H = 36

# A reading has to hold this long before the tape believes it. The inverter
# flips line/battery every few seconds around dawn (see FINDINGS) and a
# cloud crosses faster than a lane is wide -- the rule is 2.4 minutes to the
# pixel, so anything shorter than this is noise, not a record.
SETTLE_S = 300

# The pack covering less than this share of the house, with the sun up, is
# the pack topping off a burst rather than carrying anything: a full pack
# nudging an ample solar day still reads as solar (owner, 2026-09-22).
BURST_SHARE = 0.25


TRIM_PATH = bridge.LOG_DIR / "trim.json"


def read_trim() -> dict:
    """The carried landing correction: {"points": float, "last": "YYYY-MM-DD",
    "log": [...]}. It outlives restarts because it is a night-to-night
    memory, not a reading."""
    try:
        payload = json.loads(TRIM_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"points": 0.0, "last": None, "log": []}
    if not isinstance(payload, dict):
        return {"points": 0.0, "last": None, "log": []}
    payload.setdefault("points", 0.0)
    payload.setdefault("last", None)
    payload.setdefault("log", [])
    return payload


def write_trim(trim: dict) -> None:
    bridge.LOG_DIR.mkdir(exist_ok=True)
    tmp = TRIM_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(trim, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.replace(TRIM_PATH)
    except OSError:
        pass


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
        "fallback_points_per_kwh": config.AUTO_POINTS_PER_KWH,
        "turnaround_band_min": config.AUTO_TURNAROUND_BAND_MIN,
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


def sunrise_of(day: dt.date):
    """Sunrise at the site, for the day's readings to be hooked to. None
    when no position is configured, which falls the profile back to
    comparing the clock readings with each other."""
    lat = getattr(config, "SITE_LATITUDE", None)
    lon = getattr(config, "SITE_LONGITUDE", None)
    if lat is None or lon is None:
        return None
    return sun.sunrise_min(day, lat, lon, config.SITE_UTC_OFFSET_HOURS)


def build_for(store: days.DayStore, today: dt.date) -> policy.Profile:
    prof = policy.build_profile(recent_days(store, today, config.AUTO_HISTORY_DAYS),
                                **profile_kwargs(),
                                sunrise_of=sunrise_of, for_date=today)
    prof.built_for = today
    return prof


class Autopilot:
    def __init__(self, log, initial_priority: int | None):
        self.log = log
        self.store = days.DayStore()
        self.governor = policy.Governor(config.AUTO_SOC_FLOOR, config.AUTO_SOC_RESUME,
                                        margin_wh=config.AUTO_RELEASE_MARGIN_WH,
                                        request_max_min=config.AUTO_REQUEST_MAX_MIN,
                                        latest_release=config.AUTO_RELEASE_LATEST,
                                        evening_reserve_h=config.AUTO_EVENING_RESERVE_H)
        self.trim = read_trim()
        self.governor.trim = self.trim.get("points", 0.0)
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
        self.tape_date: str | None = None
        self.tape: list[dict] = []
        self.settling: dict = {}        # key -> (believed, seen, since)

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
            self._apply_trim(today)
        finally:
            self._building = None

    def _apply_trim(self, today: dt.date) -> None:
        """Carry last night's landing into tonight, once per day. Landed
        above the floor and points went unspent, so widen the window at the
        front; landed under it and shorten at the back. Half the miss, and
        the carried total clamped, so one odd night cannot swing the plan."""
        yesterday = today - dt.timedelta(days=1)
        if self.trim.get("last") == yesterday.isoformat():
            return
        rec = self.store.day(yesterday)
        step = policy.trim_step(rec, config.AUTO_SOC_FLOOR, config.AUTO_TRIM_GAIN)
        if step is None:
            return
        cap = abs(float(config.AUTO_TRIM_MAX_POINTS))
        points = round(max(-cap, min(cap, float(self.trim.get("points") or 0.0) + step)), 2)
        entry = {"night": yesterday.isoformat(), "landed": rec.get("soc_min"),
                 "at": rec.get("soc_min_at"), "aimed": config.AUTO_SOC_FLOOR,
                 "step": step, "carried": points}
        self.trim = {"points": points, "last": yesterday.isoformat(),
                     "log": [*(self.trim.get("log") or []), entry][-30:]}
        self.governor.trim = points
        write_trim(self.trim)
        self.log({"event": "priority-trim", **entry})

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

    # -- the day's tape ------------------------------------------------------

    @staticmethod
    def _prune(marks: list, now: dt.datetime) -> list:
        """The marks still inside the keep window, oldest first, capped.
        The stamps are fixed-width ISO minutes, so a string compare orders
        them."""
        cutoff = (now - dt.timedelta(hours=TAPE_KEEP_H)).strftime("%Y-%m-%dT%H:%M")
        return [m for m in marks if str(m.get("at")) >= cutoff][-TAPE_MAX:]

    def _seed_tape(self, now: dt.datetime) -> None:
        """Adopt the marks from the last published state, so a collector
        restarted in the evening still draws the day behind it -- and the
        night, which the rule's window straddles. Anything undated is from
        the first cut of this format and is dropped."""
        tape = ((bridge.read_state(max_age_s=float("inf")) or {})
                .get("policy") or {}).get("tape") or {}
        if not isinstance(tape, dict):
            return
        self.tape = self._prune([m for m in (tape.get("marks") or [])
                                 if isinstance(m, dict) and "T" in str(m.get("at"))], now)

    def settle(self, key: str, value, now: dt.datetime, seconds=SETTLE_S):
        """A reading, held for `seconds` before it counts. The first one is
        taken as it stands; after that a change has to last, so a flap
        leaves the believed value alone."""
        state, seen, since = self.settling.get(key, (None, None, None))
        if value is None:
            return state
        if value != seen:
            seen, since = value, now
        if state is None or (value != state and since is not None
                             and (now - since).total_seconds() >= seconds):
            state = value
        self.settling[key] = (state, seen, since)
        return state

    @staticmethod
    def carried_by(flow: dict) -> str | None:
        """Who actually carried the house, in one word for the tape: solar,
        battery or grid. The setting says who was *meant* to -- SUB with the
        grid down is still the pack -- so the rule colours by this."""
        flow = flow or {}
        source = flow.get("source")
        if source == "solar+battery":
            load = flow.get("load_w") or 0.0
            out = max(0.0, -(flow.get("batt_w") or 0.0))
            return "battery" if load > 0 and out / load >= BURST_SHARE else "solar"
        return source if source in ("solar", "battery", "grid") else None

    def mark_tape(self, now: dt.datetime, decision: policy.Decision,
                  flow: dict | None = None) -> dict:
        """The lane record: one mark per change in the plan or in what the
        inverter is actually set to. It is what lets the watch screen draw
        the estimate against the record -- the stretches the inverter spent
        on its own timer show up as a disagreement. Dated, and kept for a
        day and a half, because the rule runs sunrise to sunrise and its
        night is cut in two by midnight."""
        today = now.date().isoformat()
        if self.tape_date is None:
            self._seed_tape(now)
        current = policy.NAMES.get(self.current)
        present = (flow or {}).get("grid_present")
        grid = self.settle("grid", None if present is None else bool(present), now)
        src = self.settle("source", self.carried_by(flow), now)
        last = self.tape[-1] if self.tape else None
        if (not last or last.get("want") != decision.name
                or last.get("current") != current or last.get("grid") != grid
                or last.get("src") != src):
            self.tape.append({"at": now.strftime("%Y-%m-%dT%H:%M"),
                              "want": decision.name, "current": current,
                              "grid": grid, "src": src})
            self.tape = self._prune(self.tape, now)
        elif self.tape_date != today:
            self.tape = self._prune(self.tape, now)
        self.tape_date = today
        return {"date": self.tape_date, "marks": self.tape}

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
        # The first cycle decides on the placeholder profile while the plan
        # thread reads yesterday; the profile is part of the key so the
        # line is written again when the real one lands, with its numbers.
        key = (decision.want, decision.phase, prof.built_for, prof.days)
        if key != self.last_key:
            self.last_key = key
            self.log({"event": "priority-decision", "want": decision.name,
                      "phase": decision.phase, "reason": decision.reason,
                      "soc": soc, "current": policy.NAMES.get(self.current),
                      "enabled": bool(config.AUTO_PRIORITY),
                      "profile": "placeholder" if prof.built_for is None
                      else "%d days, pack %d Wh" % (prof.days, round(prof.pack_wh)),
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
            "trim_points": round(decision.trim, 2),
            "budget_points": round(decision.budget, 1),
            "cost_points": round(decision.cost, 1),
            "points_per_kwh": round(prof.points_per_kwh, 2),
            "stand_down": decision.stand_down.strftime("%H:%M") if decision.stand_down else None,
            "evening_reserve_h": config.AUTO_EVENING_RESERVE_H,
            "latest_release": config.AUTO_RELEASE_LATEST,
            "floor": config.AUTO_SOC_FLOOR, "resume": config.AUTO_SOC_RESUME,
            "floor_hold": decision.floor_hold,
            "tape": self.mark_tape(now, decision, analysis),
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
