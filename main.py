"""The one entry point the frozen exe has.

    PhantomBridge.exe tray [--direct <ip> | --redirect]
    PhantomBridge.exe collector --quiet --dongle <ip> [...]
    PhantomBridge.exe                                  same as `tray`

Only the two resident services are in the exe: it is built without a
console, so anything you read output from (ctl.py, insights.py, probe.py,
the collector's --panel) stays `python <script>` from the checkout. The
task name is the script's own name, which is what bridge.launch_command
writes into the Run key, so `collector --autostart on` from the exe points
autostart at the exe.

From source, `python main.py tray` does the same thing; it exists so the
dispatch can be tested without a build.

A windowed process has nowhere to print a traceback, so an unhandled
exception is written to logs/crash.log before the process ends. That file
is the first place to look when the tray is not in the tray after a login.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

TASKS = ("tray", "collector")


def _crash(exc: BaseException) -> None:
    import datetime as dt
    import traceback
    try:
        import bridge
        bridge.LOG_DIR.mkdir(exist_ok=True)
        with open(bridge.LOG_DIR / "crash.log", "a", encoding="utf-8") as fh:
            fh.write("\n--- %s  %s\n" % (dt.datetime.now().isoformat(" ", "seconds"),
                                       " ".join(sys.argv)))
            fh.write("".join(traceback.format_exception(exc)))
    except Exception:
        pass


def main() -> int:
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-"):
        task, rest = argv[0], argv[1:]
    else:
        task, rest = "tray", argv
    if task not in TASKS:
        msg = ("Unknown task %r.\n\nPhantomBridge.exe tray\n"
               "PhantomBridge.exe collector --quiet --dongle <ip>" % task)
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "phantom-bridge", 0x10)
        return 2

    # The script's own argparse sees only its arguments, as it would from
    # `python tray.py ...`.
    sys.argv = [sys.argv[0]] + rest
    try:
        if task == "tray":
            import tray
            return tray.main()
        import collector
        return collector.main()
    except SystemExit:
        raise
    except BaseException as exc:          # noqa: BLE001 -- last stop, logged
        _crash(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
