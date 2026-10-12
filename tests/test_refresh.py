"""Refresh receipts must prove this invocation suspended and restored state."""
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from paperdesk.backend import SuiteService, DeviceConfig, DeviceError, UnknownMutation, FIRMWARE

CURSOR = "s=synthetic;i=1;b=fixture;m=2;t=3;x=4"


def refresh_mock(tmp_path, *, journal=1, restored=True, launch_error=False):
    service = SuiteService(DeviceConfig(host="refresh-fixture.invalid",state_dir=tmp_path / "state"))
    service._connected = True
    service._installer = Mock()
    service._installer.probe.return_value = {"model":"paper-pro","firmware":FIRMWARE}
    service.transport = Mock(connected=True)
    service._cancel_event.wait = Mock(return_value=False)
    launched = []
    def run(script,**_kwargs):
        if "--show-cursor" in script: return "-- cursor: "+CURSOR+"\n"
        if script.startswith("systemd-run"):
            launched.append(script)
            if launch_error: raise DeviceError("Simulated launch receipt lost")
            return ""
        if "--after-cursor" in script:
            assert shlex.quote(CURSOR) in script
            return str(journal)+"\n"
        raise AssertionError("Unexpected remote command: "+script)
    def read(path):
        assert path.startswith("/run/paperdesk-refresh-")
        identifier = re.search(r"paperdesk-refresh-([0-9a-f]{32})", launched[0])[1]
        return json.dumps({"request_id":identifier,"cursor":CURSOR,"suspended":True,"autosleep_restored":restored,"nap_seconds":2}).encode()
    service.transport.run.side_effect = run
    service.transport.read_bytes.side_effect = read
    return service,launched


def test_refresh_requires_new_journal_entry_and_restored_receipt(tmp_path):
    service,launched = refresh_mock(tmp_path)
    result = service.refresh()
    assert result["ok"] and result["verified_by"] == "journal-cursor-and-restoration-receipt"
    assert len(launched) == 1
    command = shlex.split(launched[0])[-1]
    assert "trap restore EXIT" in command
    assert command.index("orig=$(cat") < command.index("echo off") < command.index("syspowernap 2")
    assert command.index("syspowernap 2") < command.index("restore; test") < command.index("--after-cursor")
    assert "xochitl" not in command
    assert shlex.quote(CURSOR) in command


@pytest.mark.parametrize("journal,restored", [(0,True),(1,False)])
def test_exit_marker_alone_or_unrestored_autosleep_never_claims_refresh(tmp_path,journal,restored):
    service,launched = refresh_mock(tmp_path,journal=journal,restored=restored)
    with pytest.raises(UnknownMutation) as outcome: service.refresh()
    assert outcome.value.no_retry and len(launched) == 1
    assert [json.loads(path.read_text())["state"] for path in (service.state_dir / "refreshes").glob("*.json")] == ["unknown"]


def test_lost_launch_acknowledgement_can_only_retry_readonly_verification(tmp_path):
    service,launched = refresh_mock(tmp_path,launch_error=True)
    assert service.refresh()["ok"]
    assert len(launched) == 1


def test_missing_cursor_refuses_before_scheduling_nap(tmp_path):
    service,launched = refresh_mock(tmp_path)
    service.transport.run.side_effect = lambda *_args,**_kwargs: "no kernel cursor available"
    with pytest.raises(DeviceError,match="before scheduling") as refused: service.refresh()
    assert not isinstance(refused.value,UnknownMutation) and not launched


def test_cancel_after_scheduling_leaves_detached_restore_running_and_no_replay(tmp_path):
    service,launched = refresh_mock(tmp_path)
    def wait(_seconds):
        service.cancel_current(); return True
    service._cancel_event.wait.side_effect = wait
    with pytest.raises(UnknownMutation,match="detached unit still restores") as outcome: service.refresh()
    assert outcome.value.no_retry and len(launched) == 1
    assert not service.transport.read_bytes.called
    commands = [call.args[0] for call in service.transport.run.call_args_list]
    assert not any("systemctl stop" in command or "kill" in command for command in commands)


@pytest.mark.parametrize("nap_failure",[False,True])
def test_detached_restore_trap_executes_on_success_and_failure_in_isolated_shell(tmp_path,nap_failure):
    if shutil.which("sh") is None:
        pytest.skip("Detached tablet unit shell validation requires a POSIX shell; hardware still remains untouched.")
    service,launched = refresh_mock(tmp_path)
    service.refresh()
    command = shlex.split(launched[0])[-1]
    sandbox = tmp_path / "tablet-fixture"; sandbox.mkdir()
    autosleep = sandbox / "autosleep"; autosleep.write_text("mem\n")
    regulator = sandbox / "enable_nowait"; regulator.write_text("0\n")
    marker = sandbox / "receipt.json"
    command = command.replace("/sys/power/autosleep",shlex.quote(str(autosleep)))
    command = command.replace("/sys/bus/i2c/devices/1-0048/enable_nowait",shlex.quote(str(regulator)))
    command = re.sub(r"/run/paperdesk-refresh-[0-9a-f]{32}\.json",str(marker),command)
    # Guard against accidentally running any hardware command from the source.
    assert "/sys/" not in command and "/run/" not in command
    tools = sandbox / "bin"; tools.mkdir()
    for name,body in (("syspowernap","exit "+("1" if nap_failure else "0")),("journalctl","printf 'synthetic Enter suspend\\n'")):
        executable = tools / name; executable.write_text("#!/bin/sh\n"+body+"\n"); executable.chmod(0o700)
    environment = dict(os.environ); environment["PATH"] = str(tools)+os.pathsep+environment["PATH"]
    result = subprocess.run(["sh","-c",command],env=environment,capture_output=True,timeout=5)
    assert autosleep.read_text() == "mem\n" and regulator.read_text() == "1\n"
    if nap_failure:
        assert result.returncode != 0 and not marker.exists()
    else:
        assert result.returncode == 0
        assert json.loads(marker.read_text())["autosleep_restored"] is True
