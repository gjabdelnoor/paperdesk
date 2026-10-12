import hashlib
import json
import subprocess
import shutil
from pathlib import Path

import pytest

from paperdesk.installer import DeviceInstaller, InstallerError, UnknownInstallationOutcome
from paperdesk.simulator import APP_ROOT, LOADER_CONF, RECOVERY_UNIT, TabletSimulator


@pytest.fixture
def setup(tmp_path):
    sim=TabletSimulator(tmp_path/"tablet")
    return sim,DeviceInstaller(sim,tmp_path/"state")


def test_paperpro_and_rm2_architecture_compatibility(tmp_path):
    sim=TabletSimulator(tmp_path/"tablet",model="rm2");install=DeviceInstaller(sim,tmp_path/"state")
    plan=install.plan(["keyboard"]);assert plan["compatible"] and plan["device"]["architecture"]=="armv7l"
    assert not install.plan(["direct-paste"])["compatible"]
    before=len(sim.operations)
    with pytest.raises(InstallerError,match="before changes"):install.install(["touch"])
    assert not any(item["action"]=="write" for item in sim.operations[before:])
    install.install(["keyboard"]);data=sim.read_bytes(APP_ROOT+"/current/bin/injector")
    assert data[:5]==b"\x7fELF\x01" and int.from_bytes(data[18:20],"little")==40
    assert sim.status()["capabilities"]["keyboard"]


@pytest.mark.parametrize("fault",["wrong-firmware","wrong-hash","duplicate-loader"])
def test_advanced_pin_refused_before_writes(setup,fault):
    sim,installer=setup;sim.set_fault(fault)
    with pytest.raises(InstallerError,match="before changes"):installer.install(["keyboard","direct-paste"])
    assert not any(event["action"]=="write" for event in sim.operations)
    assert not sim.exists(APP_ROOT+"/current")


def test_install_repeat_reboot_uninstall_preserves_external_configuration(setup):
    sim,installer=setup;external="/run/systemd/system/xochitl.service.d/site.conf"
    sim.write_bytes(external,b"[Service]\nEnvironment=OTHER=value\n")
    result=installer.install(["keyboard","touch","direct-paste"])
    manifest=json.loads(sim.read_bytes(result["manifest"]));assert manifest["owner"]=="paperdesk"
    for name,digest in manifest["files"].items():assert hashlib.sha256(sim.read_bytes(APP_ROOT+"/current/"+name)).hexdigest()==digest
    repeated=installer.install(["keyboard","touch","direct-paste"])
    assert repeated["backup"] and sim.exists(repeated["backup"]+"/manifest.json")
    assert sim.read_bytes(external)==b"[Service]\nEnvironment=OTHER=value\n"
    assert installer.uninstall()["ok"] and not sim.exists(LOADER_CONF)
    assert not sim.exists(RECOVERY_UNIT) and len(sim.documents())==2
    assert sim.read_bytes(external)==b"[Service]\nEnvironment=OTHER=value\n"
    assert installer.uninstall()["ok"]


@pytest.mark.parametrize("fault",["write-failure","manifest-corrupt","activation-failure"])
def test_partial_failure_rolls_back_fresh_install(setup,fault):
    sim,installer=setup;sim.set_fault(fault)
    with pytest.raises(InstallerError,match="restored"):installer.install(["keyboard","direct-paste"])
    assert not sim.exists(APP_ROOT+"/current")
    assert not sim.exists(LOADER_CONF) and not sim.exists(RECOVERY_UNIT)
    assert len(sim.documents())==2


def test_failed_upgrade_restores_previous_manifest_and_runtime(setup):
    sim,installer=setup;installer.install(["keyboard","direct-paste"])
    previous=sim.read_bytes(APP_ROOT+"/current/manifest.json");runtime=sim.read_bytes(LOADER_CONF)
    sim.set_fault("activation-failure")
    with pytest.raises(InstallerError):installer.install(["keyboard","touch","direct-paste"])
    assert sim.read_bytes(APP_ROOT+"/current/manifest.json")==previous
    assert sim.read_bytes(LOADER_CONF)==runtime
    assert not sim.status()["capabilities"]["touch"]


def test_modified_owned_configuration_is_preserved(setup):
    sim,installer=setup;installer.install(["keyboard","direct-paste"])
    sim.write_bytes(LOADER_CONF,b"[Service]\nEnvironment=CUSTOM=yes\n")
    with pytest.raises(InstallerError,match="modified"):installer.install(["keyboard"])
    with pytest.raises(InstallerError,match="modified"):installer.uninstall()
    assert sim.read_bytes(LOADER_CONF)==b"[Service]\nEnvironment=CUSTOM=yes\n"


def test_symlink_owned_root_fails_closed(setup,tmp_path):
    sim,installer=setup;outside=tmp_path/"outside";outside.mkdir()
    sim._path("/home/root/.local/lib").mkdir(exist_ok=True)
    sim._path(APP_ROOT).symlink_to(outside,target_is_directory=True)
    with pytest.raises(RuntimeError,match="Symlink"):installer.install(["keyboard"])
    assert not list(outside.iterdir())


def test_unowned_current_directory_is_preserved(setup):
    sim,installer=setup;sim.write_bytes(APP_ROOT+"/current/private",b"keep")
    with pytest.raises(InstallerError,match="no ownership"):installer.install(["keyboard"])
    assert sim.read_bytes(APP_ROOT+"/current/private")==b"keep"


def test_all_generated_real_scripts_have_valid_posix_shell_syntax(setup,tmp_path):
    if not shutil.which("sh"):pytest.skip("POSIX shell syntax is checked on Linux/macOS, not on hosts without sh.")
    sim,installer=setup;original=sim.run;scripts=[]
    def capture(script,timeout=30):scripts.append(script);return original(script,timeout)
    sim.run=capture
    installer.install(["keyboard","touch","direct-paste"]);installer.uninstall()
    for index,script in enumerate(scripts):
        path=tmp_path/f"script-{index}.sh";path.write_text(script)
        check=subprocess.run(["sh","-n",str(path)],capture_output=True,text=True)
        assert check.returncode==0,check.stderr


def test_rollback_failure_is_explicit(setup):
    sim,installer=setup;sim.set_fault("activation-failure");sim.set_fault("restore-failure")
    with pytest.raises(InstallerError,match="Rollback could not be verified.*do not rerun"):installer.install(["keyboard","direct-paste"])


def test_active_transaction_lock_is_preserved(setup):
    sim,installer=setup;installer.install(["keyboard","direct-paste"])
    previous=sim.read_bytes(APP_ROOT+"/current/manifest.json");runtime=sim.read_bytes(LOADER_CONF)
    sim.write_bytes(APP_ROOT+"/install.lock/owner",b"another-process")
    with pytest.raises(InstallerError,match="transaction"):installer.install(["keyboard","touch","direct-paste"])
    with pytest.raises(InstallerError,match="transaction"):installer.uninstall()
    assert sim.read_bytes(APP_ROOT+"/install.lock/owner")==b"another-process"
    assert sim.read_bytes(APP_ROOT+"/current/manifest.json")==previous
    assert sim.read_bytes(LOADER_CONF)==runtime


def test_partial_uninstall_restores_previous_app_and_loader(setup):
    sim,installer=setup;installer.install(["keyboard","direct-paste"])
    previous=sim.read_bytes(APP_ROOT+"/current/manifest.json");runtime=sim.read_bytes(LOADER_CONF)
    sim.set_fault("uninstall-failure")
    with pytest.raises(InstallerError,match="previous app installation was restored"):installer.uninstall()
    assert sim.read_bytes(APP_ROOT+"/current/manifest.json")==previous
    assert sim.read_bytes(LOADER_CONF)==runtime


def test_art_display_installs_without_direct_paste_dependency(setup):
    sim,installer=setup;installer.install(["art-display"])
    assert sim.exists(APP_ROOT+"/current/bin/pen-bridge")
    assert sim.exists(APP_ROOT+"/current/lib/krita_viewport.so")
    assert not sim.exists(APP_ROOT+"/current/xovi.so")
    assert sim.status()["capabilities"]["art-display"]


def test_uncertain_activation_keeps_ui_no_retry_flags(setup):
    sim,installer=setup;sim.set_fault("reboot-during-install")
    with pytest.raises(UnknownInstallationOutcome) as error:installer.install(["keyboard","direct-paste"])
    assert error.value.no_retry is True and error.value.outcome_unknown is True


def test_packaged_artifact_corruption_is_refused_before_writes(setup,tmp_path):
    sim,installer=setup;resources=tmp_path/"bad-resources";binary=resources/"tablet/bin/injector-aarch64";binary.parent.mkdir(parents=True)
    binary.write_bytes((installer.resources/"tablet/bin/injector-aarch64").read_bytes()+b"modified")
    (resources/"tablet/artifacts.json").write_bytes((installer.resources/"tablet/artifacts.json").read_bytes())
    installer.resources=resources
    with pytest.raises(InstallerError,match="checksum"):installer.install(["keyboard"])
    assert not any(event["action"]=="write" for event in sim.operations)


def test_wrong_model_architecture_never_installs_arm64_on_rm2(setup):
    sim,installer=setup;sim._state["model"]="rm2";sim._state["architecture"]="aarch64"
    with pytest.raises(InstallerError,match="before changes"):installer.install(["keyboard"])
    assert not any(event["action"]=="write" for event in sim.operations)


@pytest.mark.parametrize("path", [LOADER_CONF, RECOVERY_UNIT])
def test_first_install_preserves_unowned_runtime_files(setup, path):
    sim, installer = setup
    original = b"Unrelated configuration must be preserved\n"
    sim.write_bytes(path, original)
    sim.operations.clear()
    assert not installer.plan(["keyboard", "direct-paste"])["compatible"]
    with pytest.raises(InstallerError, match="before changes"):
        installer.install(["keyboard", "direct-paste"])
    assert sim.read_bytes(path) == original
    assert not any(event["action"] == "write" for event in sim.operations)
