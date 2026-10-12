"""Build a native one-directory app on the active operating system."""
import argparse
import os
import platform
from pathlib import Path
import subprocess
import sys
import shutil
import zipfile
import importlib.util


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default=f"{platform.system().lower()}-{platform.machine().lower()}")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--name", "PaperDesk", "--onedir", "--paths", str(root),
               "--collect-submodules", "paperdesk",
               "--exclude-module", "PySide6.QtWebEngineCore",
               "--exclude-module", "PySide6.QtWebEngineWidgets",
               "--exclude-module", "PySide6.QtQml",
               "--add-data", f"{root / 'paperdesk' / 'vendor'}{os.pathsep}paperdesk/vendor"]
    if sys.platform in ("win32", "darwin"):
        command.append("--windowed")
    if sys.platform.startswith("linux") and importlib.util.find_spec("evdev") is not None:
        command.extend(["--collect-all", "evdev"])
    command.append(str(root / "packaging" / "entry.py"))
    subprocess.run(command, cwd=root, check=True)
    bundle = root / "dist" / ("PaperDesk.app" if sys.platform == "darwin" else "PaperDesk")
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md"):
        target = bundle / ("Contents/Resources" if sys.platform == "darwin" else ".")
        shutil.copy2(root / filename, target / filename)
    if sys.platform.startswith("linux"):
        shutil.copy2(root / "packaging/install-linux.sh", bundle / "install.sh")
        (bundle / "install.sh").chmod(0o755)
    archive = root / "dist" / f"PaperDesk-{args.label}.zip"
    # Preserve POSIX executable bits and symlinks (especially macOS Frameworks).
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        paths = [bundle] + sorted(bundle.rglob("*"))
        for path in paths:
            arcname = path.relative_to(bundle.parent).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
                out.writestr(info, os.readlink(path))
            else:
                out.write(path, arcname)
    print(archive)


if __name__ == "__main__":
    main()
