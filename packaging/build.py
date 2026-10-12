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
import importlib.metadata


def copy_dependency_licenses(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for package in ("PySide6-Essentials", "shiboken6", "paramiko", "Pillow", "PyMuPDF",
                    "platformdirs", "portalocker", "cryptography", "bcrypt", "PyNaCl", "cffi", "evdev"):
        try:
            distribution = importlib.metadata.distribution(package)
        except importlib.metadata.PackageNotFoundError:
            continue
        for relative in distribution.files or []:
            name = str(relative)
            if (".dist-info/" in name and any(word in name.lower() for word in ("license", "copying", "notice"))):
                source = Path(distribution.locate_file(relative))
                if source.is_file():
                    tail = Path(name.split(".dist-info/", 1)[1])
                    target = destination / package / tail
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)


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
    copy_dependency_licenses(bundle / ("Contents/Resources/licenses" if sys.platform == "darwin" else "licenses"))
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
