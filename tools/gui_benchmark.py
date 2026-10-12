"""Capture the native demo pages and measure the idle app, with no tablet/network."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import psutil
from paperdesk.ui import create_window


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("evidence/local"))
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    window = create_window(demo=True)
    window.show()
    proc = psutil.Process()
    sample = {}
    errors = []

    def capture():
        try:
            if not window.connected or not window.status.get("simulated"):
                raise RuntimeError("Simulator failed to connect")
            for page in ("home", "library", "type", "art", "setup"):
                window.navigate(page)
                app.processEvents()
                assert window.grab().save(str(args.output / f"{page}.png"))
            window.navigate("home")
            cpu = proc.cpu_times()
            sample.update(start=time.monotonic(), cpu=cpu.user + cpu.system,
                          rss=proc.memory_info().rss, threads=proc.num_threads())
            QTimer.singleShot(int(args.seconds * 1000), finish)
        except Exception as exc:
            errors.append(str(exc))
            window.close()

    def finish():
        try:
            cpu = proc.cpu_times()
            elapsed = time.monotonic() - sample["start"]
            result = {"platform": sys.platform, "gui": "Qt Widgets", "simulated": True,
                      "idle_seconds": round(elapsed, 3),
                      "idle_cpu_percent_one_core": round((cpu.user + cpu.system - sample["cpu"]) / elapsed * 100, 3),
                      "rss_mib": round(proc.memory_info().rss / 1024**2, 2),
                      "threads": proc.num_threads(), "no_web_engine": True}
            (args.output / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
        finally:
            window.close()

    QTimer.singleShot(2500, capture)
    app.exec()
    if errors:
        raise RuntimeError(errors[0])


if __name__ == "__main__":
    main()
