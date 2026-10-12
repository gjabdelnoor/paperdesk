"""Public suite API workflows against a fresh isolated tablet, with network disabled."""
import socket
from pathlib import Path
import subprocess
import pytest
from PIL import Image

from paperdesk.backend import SuiteService


@pytest.fixture
def offline_demo(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("The simulation attempted external I/O")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    service = SuiteService.demo(tmp_path / "tablet")
    assert service.connect()["simulated"] is True
    try:
        yield service
    finally:
        service.disconnect()


def test_full_demo_tool_sequence_and_original_preservation(offline_demo, tmp_path):
    service = offline_demo
    features = ["keyboard", "touch", "direct-paste", "art-display"]
    assert service.installer_plan(features)["compatible"]
    assert service.install(features)["ok"]
    assert service.type_text("A portable paper workspace.\n")["ok"]
    assert service.send_key("erase-five")["ok"]
    assert service.add_page("cornell")["ok"]
    image = tmp_path / "original.png"
    Image.new("RGBA", (80, 120), (30, 40, 50, 180)).save(image)
    original = image.read_bytes()
    assert service.paste_image(str(image))["ok"]
    assert service.screenshot(str(tmp_path / "screen.png"))["ok"]
    Image.open(tmp_path / "screen.png").verify()
    export = tmp_path / "current.pdf"
    assert service.export_current(str(export))["ok"]
    assert export.read_bytes().startswith(b"%PDF-")
    export_bytes = export.read_bytes()
    assert service.upload(str(export))["ok"]
    assert service.create_sticker(str(image))["ok"]
    for index, document in enumerate(service.documents()):
        if document["type"] == "folder":
            continue
        result = service.download(document["id"], str(tmp_path / f"download-{index}.pdf"))
        assert result["ok"]
    assert service.refresh()["ok"]
    assert service.start_art("krita")["ok"]
    assert service.stop_art()["ok"]
    assert service.uninstall()["ok"]
    assert image.read_bytes() == original and export.read_bytes() == export_bytes


def test_demo_export_refuses_overwrite(offline_demo, tmp_path):
    path = tmp_path / "original.pdf"
    path.write_bytes(b"keep original")
    with pytest.raises(Exception):
        offline_demo.export_current(str(path))
    assert path.read_bytes() == b"keep original"
