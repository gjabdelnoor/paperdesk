"""Suite API checks against disposable simulation and mocked native transport."""
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from paperdesk.backend import SuiteService, DeviceConfig, DeviceError, UnknownMutation, FIRMWARE, XOCHITL_SHA256

DOCUMENT = "11111111-1111-4111-8111-111111111111"
PAGE = "22222222-2222-4222-8222-222222222222"
INSTANCE = "33333333-3333-4333-8333-333333333333"


def physical_mock(tmp_path):
    service = SuiteService(DeviceConfig(host="test-tablet.invalid", state_dir=tmp_path / "state", password="memory-only-fixture"))
    service._connected = True
    service.transport = Mock(connected=True)
    service._installer = Mock()
    service._installer.probe.return_value = {"model":"paper-pro", "firmware":FIRMWARE, "xochitl_sha256":XOCHITL_SHA256}
    return service


def document_inventory(name="existing", ident=DOCUMENT):
    return {ident: {"id":ident, "name":name, "type":"pdf", "pages":1, "parent":"", "modified":0}}


def test_startup_is_disconnected_and_does_not_contact_tablet(tmp_path, monkeypatch):
    connect = Mock(side_effect=AssertionError("Unexpected physical connection"))
    monkeypatch.setattr("paperdesk.transport.SSHTransport.connect", connect)
    service = SuiteService(DeviceConfig(state_dir=tmp_path))
    assert service.status()["connected"] is False
    assert connect.call_count == 0
    with pytest.raises(DeviceError, match="Connect"):
        service.documents()


def test_simulation_full_basic_suite_and_explicit_reboot_restore(tmp_path):
    service = SuiteService.demo(tmp_path / "tablet")
    assert service.status()["simulated"] is True
    assert service.status()["capabilities"]["keyboard"] is False
    features = ["keyboard","touch","direct-paste","art-display"]
    assert service.installer_plan(features)["compatible"]
    assert service.install(features)["ok"]
    assert service.type_text("PaperDesk\nworks")["ok"]
    assert service.send_key("erase-five")["ok"]
    image = tmp_path / "image.png"
    Image.new("RGB", (80,100), "white").save(image)
    assert service.paste_image(str(image))["ok"]
    assert service.add_page("cornell")["ok"]
    assert service.touch("down", .5, .6)["ok"]
    assert service.touch("up", .5, .6)["ok"]
    screenshot = tmp_path / "screen.png"
    assert service.screenshot(str(screenshot))["ok"]
    assert screenshot.read_bytes().startswith(b"\x89PNG")
    exported = tmp_path / "current.pdf"
    assert service.export_current(str(exported))["ok"]
    assert exported.read_bytes().startswith(b"%PDF-")
    assert service.start_art("krita", {"display":False})["simulated"] is True
    assert service.stop_art()["ok"]
    assert service.simulate_reboot()["status"]["capabilities"]["direct-paste"] is False
    with pytest.raises(RuntimeError, match="Install|restore|setup"):
        service.paste_image(str(image))
    assert service.install(features)["ok"]
    assert service.status()["capabilities"]["direct-paste"]
    assert service.uninstall()["ok"]
    assert not service.status()["capabilities"]["keyboard"]


def test_simulator_models_and_faults_are_disposable(tmp_path):
    service = SuiteService.demo(tmp_path / "rm2", model="rm2", firmware="3.28.0.172")
    assert service.status()["model"] == "rm2"
    assert service.installer_plan(["keyboard"])["compatible"]
    assert not service.installer_plan(["direct-paste"])["compatible"]
    assert service.simulation_fault("offline")["ok"]
    with pytest.raises(RuntimeError, match="offline"):
        service.documents()
    assert service.simulation_fault("offline",False)["ok"]
    assert service.documents()
    live = SuiteService(DeviceConfig(state_dir=tmp_path / "live"))
    with pytest.raises(DeviceError, match="only in Simulation"):
        live.simulate_reboot()
    with pytest.raises(DeviceError, match="only in Simulation"):
        live.simulation_fault("offline")


def test_unknown_upload_never_reposts_even_after_service_restart(tmp_path):
    source = tmp_path / "reading.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic native-import fixture")
    service = physical_mock(tmp_path)
    service._inventory = Mock(return_value=document_inventory())
    service._web_select = Mock()
    service._web_post_file = Mock(side_effect=ConnectionError("link lost after POST"))
    with pytest.raises(UnknownMutation) as failed:
        service.upload(str(source))
    assert failed.value.no_retry
    assert service._web_post_file.call_count == 1
    journal = json.loads((service.state_dir / "uploads.json").read_text())
    assert journal["operations"][0]["state"] == "unknown"
    assert "memory-only-fixture" not in (service.state_dir / "uploads.json").read_text()
    restarted = physical_mock(tmp_path)
    restarted._inventory = Mock(return_value=document_inventory())
    restarted._web_post_file = Mock(side_effect=AssertionError("Mutation replayed"))
    with pytest.raises(UnknownMutation, match="No upload was retried"):
        restarted.upload(str(source))
    assert restarted._web_post_file.call_count == 0


def test_unknown_upload_reconciles_once_without_posting_duplicate(tmp_path):
    source = tmp_path / "reading.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic fixture")
    service = physical_mock(tmp_path)
    before = document_inventory()
    service._inventory = Mock(return_value=before)
    service._web_select = Mock()
    service._web_post_file = Mock(side_effect=ConnectionError())
    with pytest.raises(UnknownMutation):
        service.upload(str(source))
    fresh_id = "44444444-4444-4444-8444-444444444444"
    after = before | document_inventory("reading",fresh_id)
    service._inventory.return_value = after
    result = service.upload(str(source))
    assert result["id"] == fresh_id and result["ok"]
    assert service._web_post_file.call_count == 1
    repeated = service.upload(str(source))
    assert repeated["reused"] is True
    assert service._web_post_file.call_count == 1


def test_sticker_unknown_upload_reuses_generated_asset_without_replay(tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGBA", (64,64), (20,40,60,255)).save(source)
    service = physical_mock(tmp_path)
    service._inventory = Mock(return_value=document_inventory())
    service._web_select = Mock()
    service._web_post_file = Mock(side_effect=ConnectionError())
    with pytest.raises(UnknownMutation):
        service.create_sticker(str(source))
    with pytest.raises(UnknownMutation,match="No upload was retried"):
        service.create_sticker(str(source))
    assert service._web_post_file.call_count == 1
    assert len(list((service.state_dir / "stickers").glob("*/*.rmdoc"))) == 1


def test_export_current_falls_back_to_unique_recent_open_without_install(tmp_path):
    service = physical_mock(tmp_path)
    service._inventory = Mock(return_value={DOCUMENT: document_inventory()[DOCUMENT] | {"last_opened":12345}})
    service._extension_status = Mock(side_effect=DeviceError("No extension"))
    service._export = Mock(return_value={"ok":True,"message":"exported"})
    result = service.export_current(str(tmp_path / "current.pdf"))
    assert result["target_source"] == "most-recently-opened"
    assert "Most recently" in result["message"]
    service._installer.install.assert_not_called()


def test_export_current_refuses_ambiguous_recent_open(tmp_path):
    service = physical_mock(tmp_path)
    service._inventory = Mock(return_value={ident:document_inventory(ident=ident)[ident] | {"last_opened":12345} for ident in (DOCUMENT,PAGE)})
    service._extension_status = Mock(side_effect=DeviceError("No extension"))
    service._export = Mock()
    with pytest.raises(DeviceError,match="ambiguous"):
        service.export_current(str(tmp_path / "current.pdf"))
    service._export.assert_not_called()


def test_successful_upload_uses_exactly_one_post_and_verifies_inventory(tmp_path):
    source = tmp_path / "reading.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic fixture")
    service = physical_mock(tmp_path)
    before = document_inventory()
    ident = "44444444-4444-4444-8444-444444444444"
    service._inventory = Mock(side_effect=[before, before | document_inventory("reading",ident)])
    service._web_select = Mock()
    service._web_post_file = Mock()
    assert service.upload(str(source))["id"] == ident
    assert service._web_post_file.call_count == 1


def test_upload_folder_ambiguity_is_refused_before_network_mutation(tmp_path):
    source = tmp_path / "reading.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic fixture")
    service = physical_mock(tmp_path)
    before = {ident:{"id":ident,"name":"Inbox","type":"folder","parent":""} for ident in (DOCUMENT,PAGE)}
    service._inventory = Mock(return_value=before)
    service._web_select = Mock()
    service._web_post_file = Mock()
    with pytest.raises(DeviceError,match="ambiguous"):
        service.upload(str(source),"Inbox")
    assert not service._web_select.called and not service._web_post_file.called


def test_download_rejects_traversal_and_preserves_original(tmp_path):
    service = physical_mock(tmp_path)
    with pytest.raises(DeviceError,match="identifier"):
        service.download("../../secret","whatever")
    service._inventory = Mock(return_value=document_inventory())
    original = tmp_path / "reading.pdf"
    original.write_bytes(b"original")
    with pytest.raises(DeviceError,match="preserve"):
        service.download(DOCUMENT,str(original))
    assert original.read_bytes() == b"original"
    assert not service.transport.download.called


def test_unicode_typing_preflight_sends_nothing_and_no_silent_data_loss(tmp_path):
    service = physical_mock(tmp_path)
    service._send_input = Mock()
    with pytest.raises(DeviceError,match="unsupported"):
        service.type_text("entire batch contains Ω")
    assert not service._send_input.called


def test_invalid_touch_coordinates_never_open_stream(tmp_path):
    service = physical_mock(tmp_path)
    for x in (-1,1.1,float("nan"),float("inf"),True):
        with pytest.raises(DeviceError,match="normalized"):
            service.touch("down",x,.5)
    assert not service.transport.stream.called


def status_fixture():
    return {"protocol":2,"firmware":FIRMWARE,"xochitl_sha256":XOCHITL_SHA256,
            "instance_id":INSTANCE,"document_uuid":DOCUMENT,"page_uuid":PAGE,"pid":42,
            "heartbeat_ms":int(time.time()*1000),"ready":True,"insert_compiled":True,
            "native_methods":[{"signature":"insertImageFileAsSceneItem(QUrl,QPointF)"}],
            "paper_portrait_bounds":{"x":-810,"y":0,"width":1620,"height":2160},
            "paper_width":1620,"paper_height":2160}


def test_extension_exact_pins_and_process_identity_are_read_only(tmp_path):
    service = physical_mock(tmp_path)
    status = status_fixture()
    content = {"fileType":"notebook","orientation":"portrait","cPages":{"pages":[{"id":PAGE}]}}
    service.transport.read_bytes.side_effect = lambda path: json.dumps(status if path.endswith("status.json") else content).encode()
    service.transport.run.return_value = "42\n"
    assert service._extension_status()["paper"] == (1620,2160)
    status["xochitl_sha256"] = "0"*64
    with pytest.raises(DeviceError,match="exact supported"):
        service._extension_status()
    assert not service.transport.write_bytes.called
    status["xochitl_sha256"] = XOCHITL_SHA256
    service.transport.run.return_value = "42 99\n"
    with pytest.raises(DeviceError,match="different"):
        service._extension_status()


def test_direct_paste_unknown_request_is_not_replayed(tmp_path):
    service = physical_mock(tmp_path)
    image = tmp_path / "figure.png"
    Image.new("RGBA", (64,64), (100,20,30,128)).save(image)
    status = status_fixture() | {"paper":(1620,2160)}
    service._extension_status = Mock(return_value=status)
    service.transport.write_bytes.side_effect = DeviceError("Connection interrupted")
    with pytest.raises(UnknownMutation):
        service.paste_image(str(image))
    calls = service.transport.write_bytes.call_count
    with pytest.raises(UnknownMutation,match="earlier operation"):
        service.paste_image(str(image))
    assert service.transport.write_bytes.call_count == calls


def test_late_direct_paste_receipt_resolves_without_second_insert(tmp_path):
    service = physical_mock(tmp_path)
    image = tmp_path / "figure.png"
    Image.new("RGB", (64,64), "white").save(image)
    service._extension_status = Mock(return_value=status_fixture() | {"paper":(1620,2160)})
    service.transport.write_bytes.side_effect = DeviceError("Lost link")
    with pytest.raises(UnknownMutation):
        service.paste_image(str(image))
    request = next((service.state_dir / "requests/images").iterdir()).name
    service.transport.read_bytes.return_value = json.dumps({"request_id":request,"state":"persisted"}).encode()
    writes = service.transport.write_bytes.call_count
    result = service.paste_image(str(image))
    assert result["ok"] and result["reused"]
    assert service.transport.write_bytes.call_count == writes


def test_no_ready_read_causes_loader_install_or_tablet_restart(tmp_path):
    service = physical_mock(tmp_path)
    service.transport.read_bytes.side_effect = DeviceError("Missing extension")
    with pytest.raises(DeviceError):
        service._extension_status()
    assert not service.transport.run.called
    assert not service.transport.write_bytes.called


def test_guarded_local_image_preparation_preserves_alpha_without_upscaling(tmp_path):
    from paperdesk.vendor.direct_paste.host_core import prepare
    source = tmp_path / "source.png"
    image = Image.new("RGBA", (64,64), (40,80,120,128)); image.save(source)
    result = prepare(source, tmp_path / "request", (1620,2160))
    with Image.open(tmp_path / "request/image.png") as output:
        assert output.size == image.size
        assert output.tobytes() == image.tobytes()
    assert result["alpha_preserved"]
    assert result["sha256"] == hashlib.sha256((tmp_path / "request/image.png").read_bytes()).hexdigest()


def test_ink_simulation_has_explicit_check_and_bounded_records(tmp_path):
    service = SuiteService.demo(tmp_path / "tablet")
    strokes = [[(1000,1000),(1000,1500),(1500,1500)]]
    with pytest.raises(DeviceError,match="Check the pen"):
        service.stream_ink(strokes)
    assert service.probe_ink()["simulated"]
    result = service.stream_ink(strokes)
    assert result["simulated"] and result["records"] > 0
    service.simulate_reboot()
    with pytest.raises(DeviceError,match="Check the pen"):
        service.stream_ink(strokes)
