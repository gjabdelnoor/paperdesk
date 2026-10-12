"""Native desktop launcher and deterministic packaged application smoke test."""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PaperDesk desktop companion")
    parser.add_argument("--demo", action="store_true", help="Use an isolated simulated tablet")
    parser.add_argument("--smoke-test", action="store_true", help="Check isolated simulator and native GUI, then exit")
    parser.add_argument("--self-test", action="store_true", help="Check bundled assets and tools in a disposable tablet, then exit")
    parser.add_argument("--report", type=Path, help="Save self-test results as JSON")
    parser.add_argument("--screenshot", type=Path, help="Save the GUI during the smoke test")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    if args.self_test:
        from .selftest import run
        try:
            result = run()
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "hardware_validated": False}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + "\n")
        if sys.stdout is not None:
            print(json.dumps(result))
        return 0 if result["ok"] else 1
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .ui import create_window

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("PaperDesk")
    app.setApplicationName("PaperDesk")
    window = create_window(demo=args.demo or args.smoke_test)
    window.show()
    failures = []
    if args.smoke_test:
        deadline = time.monotonic() + 12
        def finish():
            if (not getattr(window, "connected", False)
                    and getattr(window, "runner", None) is not None
                    and window.runner.busy and time.monotonic() < deadline):
                QTimer.singleShot(100, finish)
                return
            try:
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(args.screenshot)):
                        raise RuntimeError("Could not save GUI screenshot")
                if not window.isVisible():
                    raise RuntimeError("The application window is not visible")
                if getattr(window, "connection_mode", None) != "simulation":
                    raise RuntimeError("The bundled simulator did not connect")
                if not getattr(window, "connected", False):
                    raise RuntimeError("The simulated tablet is disconnected")
                if getattr(window, "status", {}).get("simulated") is not True:
                    raise RuntimeError("The simulator did not report an isolated connection")
                print(json.dumps({"ok": True, "platform": sys.platform,
                                  "gui": "Qt Widgets", "simulated": True}))
            except Exception as exc:
                failures.append(str(exc))
                diagnostic = {"ok": False, "error": str(exc),
                              "backend_error": getattr(window, "backend_error", ""),
                              "connect_error": getattr(window, "connect_error", ""),
                              "connection_mode": getattr(window, "connection_mode", "unknown"),
                              "activity": window.status_text() if callable(getattr(window, "status_text", None)) else "",
                              "recent_log": getattr(window, "log", [])[-5:]}
                if args.screenshot:
                    args.screenshot.with_suffix(".json").write_text(json.dumps(diagnostic, indent=2) + "\n")
                if sys.stderr is not None:
                    print(json.dumps(diagnostic), file=sys.stderr)
            finally:
                window.close()
                app.quit()
        QTimer.singleShot(1600, finish)
    result = app.exec()
    return 1 if failures else result


if __name__ == "__main__":
    raise SystemExit(main())
