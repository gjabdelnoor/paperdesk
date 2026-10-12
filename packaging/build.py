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
import json
import re
import hashlib


OPENSSL_DYLIB = re.compile(r"^lib(ssl|crypto)\.(\d+(?:\.\d+)*)\.dylib$")


def openssl_dependencies(binary, get_imports):
    """Resolve actual Mach-O imports, including libssl's indirect libcrypto."""
    found = {}
    pending, scanned = [Path(binary)], set()
    while pending:
        source = pending.pop()
        if source in scanned:
            continue
        scanned.add(source)
        for reference, resolved in sorted(get_imports(str(source))):
            name = Path(reference).name
            if not OPENSSL_DYLIB.fullmatch(name):
                continue
            if not resolved or not Path(resolved).is_file():
                raise RuntimeError(f"Cannot resolve {reference} required by {source}")
            path = Path(resolved)  # Keep referenced basename; do not resolve symlinks.
            if path.name != name:
                raise RuntimeError(f"OpenSSL dependency {reference} resolved to a different filename: {path}")
            if name in found and found[name] != path and not found[name].samefile(path):
                raise RuntimeError(f"Conflicting OpenSSL dependency {name}: {found[name]} and {path}")
            found.setdefault(name, path)
            pending.append(path)
    return found


def select_macos_openssl(crypto_dependencies, python_dependencies, crypto_version, python_version):
    """Select one matched pair before PyInstaller deduplicates by basename.

    macOS cryptography wheels are static. Intel source builds can instead use a
    newer Homebrew OpenSSL than setup-python's _ssl module. All frozen consumers
    of the same dylib basename must use the newer compatible pair, never one
    library from each build. OpenSSL preserves ABI within the same major release.
    """
    if not crypto_dependencies:
        return {}, "cryptography-static"

    def require_pair(dependencies, consumer, version):
        suffixes = {OPENSSL_DYLIB.fullmatch(name).group(2) for name in dependencies}
        if len(suffixes) != 1:
            raise RuntimeError(f"{consumer} references multiple OpenSSL dylib ABI families")
        suffix = suffixes.pop()
        names = {f"libssl.{suffix}.dylib", f"libcrypto.{suffix}.dylib"}
        if set(dependencies) != names:
            raise RuntimeError(f"{consumer} does not resolve a complete matched OpenSSL ssl/crypto pair")
        if int(suffix.split('.')[0]) != version >> 28:
            raise RuntimeError(f"{consumer} OpenSSL runtime version does not match its dylib ABI family")
        return names

    crypto_names = require_pair(crypto_dependencies, "cryptography", crypto_version)
    shared_names = crypto_names & set(python_dependencies)
    if not shared_names:
        return dict(crypto_dependencies), "cryptography-dynamic-distinct-abi"
    python_names = require_pair(python_dependencies, "Python _ssl", python_version)
    if crypto_names != python_names:
        raise RuntimeError("Python and cryptography share an OpenSSL filename without sharing a complete ABI pair")
    if python_version > crypto_version:
        return dict(python_dependencies), "python-newer-compatible-pair"
    return dict(crypto_dependencies), "cryptography-newer-compatible-pair"


def command_diagnostic(argv):
    """Record only known build commands, never credentials or an environment dump."""
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False)
        return {"command": [str(arg) for arg in argv], "returncode": result.returncode,
                "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": [str(arg) for arg in argv], "error": str(error)}


def prepare_macos_openssl(diagnostic_path=None):
    """Gather source diagnostics and explicitly seed native binary collection."""
    import _ssl
    import ssl
    from cryptography.hazmat.bindings import _rust
    from cryptography.hazmat.backends.openssl.backend import backend
    from PyInstaller.depend import bindepend

    crypto_version = backend.openssl_version_number()
    python_version = ssl.OPENSSL_VERSION_NUMBER
    sources = {"cryptography": Path(_rust.__file__), "python_ssl": Path(_ssl.__file__)}
    report = {
        "schema": 1, "platform": sys.platform, "architecture": platform.machine(),
        "python": sys.version, "python_executable": sys.executable,
        "packages": {name: importlib.metadata.version(name) for name in
                     ("cryptography", "paramiko", "pyinstaller", "pyinstaller-hooks-contrib")},
        "python_openssl": {"version": ssl.OPENSSL_VERSION, "number": python_version},
        "cryptography_openssl": {"version": backend.openssl_version_text(), "number": crypto_version},
        "sources": {name: {"path": str(path), "otool": command_diagnostic(["otool", "-L", str(path)])}
                    for name, path in sources.items()},
        "brew_openssl_prefix": command_diagnostic(["brew", "--prefix", "openssl@3"]),
        "signing": {"identity": "ad-hoc", "developer_id": False, "notarized": False},
        "frozen_runtime_validated": False,
    }
    try:
        dependencies = {name: openssl_dependencies(path, bindepend.get_imports) for name, path in sources.items()}
        for name in sources:
            report["sources"][name]["resolved_openssl"] = {key: str(value) for key, value in dependencies[name].items()}
        selected, mode = select_macos_openssl(dependencies["cryptography"], dependencies["python_ssl"],
                                             crypto_version, python_version)
        report.update(selection=mode, selected_binaries={name: str(path) for name, path in selected.items()},
                      selected_library_dependencies={name: command_diagnostic(["otool", "-L", str(path)])
                                                     for name, path in selected.items()})
        provider_dirs = {path.parent / "ossl-modules" for name, path in selected.items()
                         if name.startswith("libssl.") and (path.parent / "ossl-modules").is_dir()}
        report["selected_provider_directories"] = [str(path) for path in sorted(provider_dirs)]
    except Exception as error:
        report["selection_error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if diagnostic_path is not None:
            diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostic_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    # User binaries initialize Analysis before its automatic dependency scan.
    # Keeping the upstream cryptography hook retains metadata and provider modules.
    arguments = [item for _, path in sorted(selected.items())
                 for item in ("--add-binary", f"{path}{os.pathsep}.")]
    # If Python requires the newer pair, its matching modules must also take
    # precedence over the older directory gathered by cryptography's hook.
    arguments += [item for path in report["selected_provider_directories"]
                  for item in ("--add-binary", f"{path}{os.pathsep}ossl-modules")]
    return arguments, report


def inspect_macos_bundle(bundle, report):
    frameworks = bundle / "Contents/Frameworks"
    files = sorted({path for pattern in ("libssl.*.dylib", "libcrypto.*.dylib", "_rust*.so", "_ssl*.so")
                    for path in frameworks.rglob(pattern) if path.is_file()})
    report["bundle_dependencies"] = {str(path.relative_to(bundle)): command_diagnostic(["otool", "-L", str(path)])
                                     for path in files}
    analysis = bundle.parents[1] / "build/PaperDesk/Analysis-00.toc"
    if not analysis.is_file():
        raise RuntimeError("The PyInstaller Analysis TOC is missing; selected OpenSSL origins cannot be verified")
    if analysis.is_file():
        # The analysis TOC identifies the original source chosen for each dylib;
        # otool alone shows only paths rewritten for the frozen application.
        import ast
        from PyInstaller.building.build_main import Analysis
        value = ast.literal_eval(analysis.read_text(encoding="utf-8"))
        # The cached TOC contains input and output lists. Only inspect the final
        # binaries field, or the requested source could masquerade as the result.
        binary_index = next(index for index, (name, _) in enumerate(Analysis._GUTS) if name == "binaries")
        report["analysis_openssl_entries"] = [
            {"destination": name, "source": source, "type": kind}
            for name, source, kind in value[binary_index] if OPENSSL_DYLIB.fullmatch(Path(name).name)]
        for name, source in report["selected_binaries"].items():
            matches = [entry for entry in report["analysis_openssl_entries"] if entry["destination"] == name]
            if len(matches) != 1 or Path(matches[0]["source"]) != Path(source):
                raise RuntimeError(f"PyInstaller did not retain the selected OpenSSL binary: {name} from {source}")


def sign_macos_bundle(bundle, report):
    # Resource copies occur after PyInstaller signs. Re-seal only the top-level
    # app, preserving PyInstaller's already-signed inner Mach-O files. --deep is
    # a verification flag, never a blanket recursive re-sign of vendor assets.
    for key, argv in (
        ("final_sign", ["codesign", "--force", "--sign", "-", str(bundle)]),
        ("verify", ["codesign", "--verify", "--deep", "--strict", str(bundle)]),
    ):
        result = command_diagnostic(argv)
        report["signing"][key] = result
        if result.get("returncode") != 0:
            raise RuntimeError(f"macOS app {key} failed: {result.get('stderr') or result.get('error')}")
    # Gatekeeper trust is distinct from signature integrity. Record the runner's
    # actual policy and assessment, without changing policy or treating an
    # ad-hoc app as Developer ID signed/notarized.
    report["signing"]["gatekeeper"] = {
        "status": command_diagnostic(["spctl", "--status"]),
        "assessment": command_diagnostic(["spctl", "--assess", "--type", "execute", "--verbose=4", str(bundle)]),
    }


def copy_macos_openssl_licenses(destination, report):
    """Copy original licensing from the selected library's installation prefix."""
    selected = report["selected_binaries"]
    if not selected:
        report["openssl_licensing"] = {"mode": "static-cryptography-wheel", "copied_by": "Python distribution license helper"}
        return
    ssl_path = next(Path(path) for name, path in selected.items() if name.startswith("libssl."))
    prefix = ssl_path.resolve().parent.parent
    version_info = report["python_openssl"] if report["selection"].startswith("python-") else report["cryptography_openssl"]
    match = re.search(r"\bOpenSSL (\d+\.\d+\.\d+)\b", version_info["version"])
    if not match:
        raise RuntimeError("The selected OpenSSL version cannot be identified for license provenance")
    version = match.group(1)
    candidates = list(prefix.iterdir())
    documentation = prefix / "share/doc"
    if documentation.is_dir():
        candidates += list(documentation.rglob("*"))
    for provider_path in report.get("selected_provider_directories", []):
        provider = Path(provider_path).resolve()
        if provider.is_dir() and provider.is_relative_to(prefix):
            candidates += list(provider.rglob("*"))
    material = sorted({path for path in candidates if path.is_file()
                       and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
                       and path.resolve().is_relative_to(prefix)})
    licenses = [path for path in material if path.name.upper().startswith(("LICENSE", "COPYING"))
                and all(marker in path.read_bytes() for marker in
                        (b"Apache License", b"Version 2.0", b"END OF TERMS AND CONDITIONS"))]
    if not licenses:
        raise RuntimeError(f"The complete OpenSSL Apache-2.0 license is missing from selected package prefix {prefix}")
    # Preserve all package license/NOTICE files verbatim, including notices for
    # providers installed alongside this exact pair. No network fallback can
    # accidentally substitute the license or source URL of a newer Homebrew keg.
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in material:
        relative = path.relative_to(prefix)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append({"source": str(path), "file": relative.as_posix(),
                       "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    origin = {"project": "OpenSSL", "version": version, "license": "Apache-2.0",
              "homepage": "https://openssl-library.org/", "installed_prefix": str(prefix),
              "source_url": f"https://github.com/openssl/openssl/releases/download/openssl-{version}/openssl-{version}.tar.gz",
              "license_url": f"https://github.com/openssl/openssl/blob/openssl-{version}/LICENSE.txt",
              "files": copied}
    (destination / "ORIGIN.json").write_text(json.dumps(origin, indent=2) + "\n", encoding="utf-8")
    report["openssl_licensing"] = origin


def copy_public_docs(root, destination):
    """Include tracked docs and their images, excluding unrelated local files."""
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "docs"],
                            text=True, capture_output=True, check=True, timeout=30)
    docs = (root / "docs").resolve()
    for name in sorted(filter(None, result.stdout.split("\0"))):
        source = root / name
        if not source.is_file() or not source.resolve().is_relative_to(docs):
            raise RuntimeError(f"Tracked public documentation is missing or escapes docs/: {name}")
        target = destination / source.relative_to(root / "docs")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


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
    macos_report = None
    diagnostic_path = root / "evidence" / f"build-openssl-{args.label}.json"
    if sys.platform == "darwin":
        arguments, macos_report = prepare_macos_openssl(diagnostic_path)
        command.extend(arguments)
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(json.dumps(macos_report, indent=2) + "\n", encoding="utf-8")
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
    copy_public_docs(root, bundle / ("Contents/Resources/docs" if sys.platform == "darwin" else "docs"))
    if sys.platform == "darwin":
        try:
            inspect_macos_bundle(bundle, macos_report)
            copy_macos_openssl_licenses(bundle / "Contents/Resources/licenses/OpenSSL", macos_report)
            sign_macos_bundle(bundle, macos_report)
        finally:
            diagnostic_path.write_text(json.dumps(macos_report, indent=2) + "\n", encoding="utf-8")
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
