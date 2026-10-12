# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packaging regression checks use generated files and fake native commands."""
import importlib.util
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location("paperdesk_packaging_build", Path(__file__).parents[1] / "packaging/build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def pair(root, suffix="3"):
    root.mkdir(parents=True, exist_ok=True)
    result = {}
    for stem in ("ssl", "crypto"):
        path = root / f"lib{stem}.{suffix}.dylib"
        path.write_bytes(b"Generated dependency fixture; not a native library.")
        result[path.name] = path
    return result


def test_dynamic_crypto_newer_than_python_selects_complete_new_pair(tmp_path):
    fresh, old = pair(tmp_path / "brew"), pair(tmp_path / "python")
    selected, mode = build.select_macos_openssl(fresh, old, 0x30600000, 0x30000120)
    assert selected == fresh and mode == "cryptography-newer-compatible-pair"
    # Exercise the real pinned PyInstaller normalizer: explicit inputs must win
    # over the automatically appended older dependencies at identical names.
    from PyInstaller.building.datastruct import normalize_toc
    entries = [(name, str(path), "BINARY") for source in (selected, old) for name, path in source.items()]
    assert {(name, source) for name, source, _ in normalize_toc(entries)} == {(name, str(path)) for name, path in fresh.items()}


def test_python_newer_same_abi_selects_its_complete_pair(tmp_path):
    crypto, python = pair(tmp_path / "crypto"), pair(tmp_path / "python")
    selected, mode = build.select_macos_openssl(crypto, python, 0x30400000, 0x30600000)
    assert selected == python and mode == "python-newer-compatible-pair"


def test_static_wheel_keeps_existing_automatic_python_collection(tmp_path):
    selected, mode = build.select_macos_openssl({}, pair(tmp_path / "python"), 0x40000000, 0x30000120)
    assert selected == {} and mode == "cryptography-static"


def test_different_dylib_abi_families_do_not_replace_python_libraries(tmp_path):
    crypto, python = pair(tmp_path / "crypto", "4"), pair(tmp_path / "python", "3")
    selected, mode = build.select_macos_openssl(crypto, python, 0x40000000, 0x30600000)
    assert selected == crypto and mode == "cryptography-dynamic-distinct-abi"


@pytest.mark.parametrize("damage", ["incomplete", "version_mismatch", "multiple_families"])
def test_pair_selection_refuses_unknown_or_mixed_dependencies(tmp_path, damage):
    crypto, python = pair(tmp_path / "crypto"), pair(tmp_path / "python")
    version = 0x30600000
    if damage == "incomplete":
        crypto.pop("libcrypto.3.dylib")
    elif damage == "version_mismatch":
        version = 0x40000000
    else:
        crypto.update(pair(tmp_path / "other", "4"))
    with pytest.raises(RuntimeError):
        build.select_macos_openssl(crypto, python, version, 0x30000120)


def test_dependency_scan_follows_indirect_crypto_and_ignores_system_frameworks(tmp_path):
    libraries = pair(tmp_path / "openssl")
    rust = tmp_path / "_rust.abi3.so"
    rust.write_bytes(b"Fixture")
    ssl, crypto = libraries["libssl.3.dylib"], libraries["libcrypto.3.dylib"]
    links = {str(rust): {(str(ssl), str(ssl)), ("/usr/lib/libSystem.B.dylib", None)},
             str(ssl): {("@rpath/libcrypto.3.dylib", str(crypto))}, str(crypto): set()}
    assert build.openssl_dependencies(rust, links.__getitem__) == libraries


def test_dependency_scan_refuses_unresolved_openssl(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot resolve"):
        build.openssl_dependencies(tmp_path / "_rust.abi3.so", lambda _: {("@rpath/libssl.3.dylib", None)})


def test_scan_preserves_symlink_basename_but_accepts_aliases_of_same_library(tmp_path):
    libraries = pair(tmp_path / "openssl")
    aliases = tmp_path / "alias"
    aliases.mkdir()
    for name, source in libraries.items():
        (aliases / name).symlink_to(source)
    rust = tmp_path / "_rust.abi3.so"
    rust.write_bytes(b"Fixture")
    links = {str(rust): {(str(libraries["libssl.3.dylib"]), str(libraries["libssl.3.dylib"])),
                         ("@rpath/libcrypto.3.dylib", str(aliases / "libcrypto.3.dylib"))},
             str(libraries["libssl.3.dylib"]): {("@rpath/libcrypto.3.dylib", str(libraries["libcrypto.3.dylib"]))},
             str(libraries["libcrypto.3.dylib"]): set(), str(aliases / "libcrypto.3.dylib"): set()}
    found = build.openssl_dependencies(rust, links.__getitem__)
    assert found["libcrypto.3.dylib"] == aliases / "libcrypto.3.dylib"
    assert found["libcrypto.3.dylib"].is_symlink()


def test_conflicting_dependency_paths_fail_instead_of_arbitrary_selection(tmp_path):
    first, second = pair(tmp_path / "first"), pair(tmp_path / "second")
    rust = tmp_path / "_rust.abi3.so"
    links = {str(rust): {(str(first["libssl.3.dylib"]), str(first["libssl.3.dylib"])),
                         (str(first["libcrypto.3.dylib"]), str(first["libcrypto.3.dylib"]))},
             str(first["libssl.3.dylib"]): {("@rpath/libcrypto.3.dylib", str(second["libcrypto.3.dylib"]))},
             str(first["libcrypto.3.dylib"]): set()}
    with pytest.raises(RuntimeError, match="Conflicting"):
        build.openssl_dependencies(rust, links.__getitem__)


def write_analysis(root, inputs, actual):
    from PyInstaller.building.build_main import Analysis
    entries = [[] for _ in Analysis._GUTS]
    for index, (name, _) in enumerate(Analysis._GUTS):
        if name == "_input_binaries":
            entries[index] = [(key, str(value), "BINARY") for key, value in inputs.items()]
        elif name == "binaries":
            entries[index] = [(key, str(value), "BINARY") for key, value in actual.items()]
    path = root / "build/PaperDesk/Analysis-00.toc"
    path.parent.mkdir(parents=True)
    path.write_text(repr(tuple(entries)))


def test_bundle_origin_check_reads_final_analysis_not_requested_inputs(tmp_path, monkeypatch):
    fresh, old = pair(tmp_path / "new"), pair(tmp_path / "old")
    bundle = tmp_path / "dist/PaperDesk.app"
    bundle.mkdir(parents=True)
    report = {"selected_binaries": {key: str(path) for key, path in fresh.items()}}
    write_analysis(tmp_path, fresh, old)
    monkeypatch.setattr(build, "command_diagnostic", lambda argv: {"returncode": 0})
    with pytest.raises(RuntimeError, match="did not retain"):
        build.inspect_macos_bundle(bundle, report)
    assert {entry["source"] for entry in report["analysis_openssl_entries"]} == {str(path) for path in old.values()}


def test_bundle_origin_check_retains_selected_sources(tmp_path, monkeypatch):
    fresh = pair(tmp_path / "new")
    bundle = tmp_path / "dist/PaperDesk.app"
    bundle.mkdir(parents=True)
    write_analysis(tmp_path, fresh, fresh)
    report = {"selected_binaries": {key: str(path) for key, path in fresh.items()}}
    monkeypatch.setattr(build, "command_diagnostic", lambda argv: {"returncode": 0})
    build.inspect_macos_bundle(bundle, report)
    assert len(report["analysis_openssl_entries"]) == 2


def test_top_level_resigning_preserves_inner_signatures_and_verifies_strictly(tmp_path, monkeypatch):
    commands = []
    def run(argv):
        commands.append(argv)
        return {"command": argv, "returncode": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(build, "command_diagnostic", run)
    bundle = tmp_path / "PaperDesk.app"
    report = {"signing": {"identity": "ad-hoc", "notarized": False}}
    build.sign_macos_bundle(bundle, report)
    assert commands == [["codesign", "--force", "--sign", "-", str(bundle)],
                        ["codesign", "--verify", "--deep", "--strict", str(bundle)]]
    assert report["signing"]["verify"]["returncode"] == 0 and not report["signing"]["notarized"]


@pytest.mark.parametrize("failed_command", [0, 1])
def test_sign_or_verification_failure_stops_packaging(tmp_path, monkeypatch, failed_command):
    calls = []
    def run(argv):
        index = len(calls)
        calls.append(argv)
        return {"returncode": 1 if index == failed_command else 0, "stderr": "generated failure"}
    monkeypatch.setattr(build, "command_diagnostic", run)
    with pytest.raises(RuntimeError, match="generated failure"):
        build.sign_macos_bundle(tmp_path / "PaperDesk.app", {"signing": {}})
    assert len(calls) == failed_command + 1
