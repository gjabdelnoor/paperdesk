from pathlib import Path

import pytest
from PIL import Image

from paperdesk.installer import DeviceInstaller
from paperdesk.simulator import SimulationError, TabletSimulator, UnknownMutationError, _pdf


def test_fresh_models_and_persistent_documents(tmp_path):
    pro=TabletSimulator(tmp_path/"pro");rm2=TabletSimulator(tmp_path/"rm2",model="rm2")
    assert pro._state["architecture"]=="aarch64"
    assert rm2._state["architecture"]=="armv7l"
    assert not pro.status()["capabilities"]["keyboard"]
    assert len(pro.documents())==2
    assert TabletSimulator(tmp_path/"pro").documents()==pro.documents()


def test_existing_real_folder_is_refused(tmp_path):
    (tmp_path/"private.txt").write_text("keep")
    with pytest.raises(SimulationError,match="existing files"):TabletSimulator(tmp_path)
    assert (tmp_path/"private.txt").read_text()=="keep"


def test_paths_cannot_escape_or_follow_symlinks(tmp_path):
    sim=TabletSimulator(tmp_path/"sim"); outside=tmp_path/"outside";outside.mkdir();secret=outside/"secret";secret.write_bytes(b"original")
    sim.root.joinpath("escape").symlink_to(outside,target_is_directory=True)
    for path in ("/escape/secret","/escape/new","/../outside/secret","../secret"):
        with pytest.raises(SimulationError):sim.read_bytes(path)
        with pytest.raises(SimulationError):sim.write_bytes(path,b"bad")
    assert secret.read_bytes()==b"original" and not (outside/"new").exists()
    with pytest.raises(SimulationError):sim.run("cat /etc/passwd")


def test_upload_unknown_is_not_retried(tmp_path):
    sim=TabletSimulator(tmp_path/"sim");pdf=tmp_path/"reading.pdf";pdf.write_bytes(_pdf("synthetic upload"));sim.set_fault("upload-unknown")
    with pytest.raises(UnknownMutationError,match="do not retry"):sim.upload(pdf)
    assert len(sim.documents())==3
    assert sum(event["action"]=="native-import" for event in sim.operations)==1


def test_native_import_export_preserves_existing_file(tmp_path):
    sim=TabletSimulator(tmp_path/"sim");data=_pdf("fixture");source=tmp_path/"source.pdf";source.write_bytes(data)
    result=sim.upload(source,"Archive");output=tmp_path/"copy.pdf"
    sim.download(result["id"],output);assert output.read_bytes()==data
    with pytest.raises(SimulationError,match="already exists"):sim.download(result["id"],output)
    assert output.read_bytes()==data
    with pytest.raises(SimulationError):sim.download("../../outside",output)


def test_keyboard_page_image_screenshot_and_reboot(tmp_path):
    sim=TabletSimulator(tmp_path/"sim");installer=DeviceInstaller(sim,tmp_path/"state")
    with pytest.raises(SimulationError):sim.type_text("before install")
    installer.install(["keyboard","touch","direct-paste"])
    sim.type_text("abcdef");sim.send_key("erase-five");assert sim._active()["text"].endswith("a")
    sim.touch("down",.5,.25);sim.touch("up",.5,.25)
    with pytest.raises(SimulationError):sim.touch("down",2,0)
    image=tmp_path/"image.png";Image.new("RGB",(30,40),"blue").save(image)
    assert sim.paste_image(image)["state"]=="persisted"
    assert sim.add_page("cornell")["pages"]==2
    screenshot=tmp_path/"screen.png";sim.screenshot(screenshot)
    with Image.open(screenshot) as img:assert img.size==(162,216)
    target=tmp_path/"notebook.pdf";sim.export_current(target);assert target.read_bytes().startswith(b"%PDF-")
    sim.reboot();assert sim.status()["capabilities"]["keyboard"]
    assert not sim.status()["capabilities"]["direct-paste"]
    assert sim._active()["pages"]==2 and len(sim._active()["images"])==1
    installer.install(["keyboard","touch","direct-paste"])
    assert sim.status()["capabilities"]["direct-paste"]


def test_faults_and_root_reboot_symlink_guard(tmp_path):
    sim=TabletSimulator(tmp_path/"sim");sim.set_fault("offline")
    with pytest.raises(SimulationError,match="offline"):sim.documents()
    sim.set_fault("offline",False);outside=tmp_path/"protected";outside.mkdir();(outside/"file").write_text("keep")
    sim.root.joinpath("run/link").symlink_to(outside,target_is_directory=True)
    with pytest.raises(SimulationError,match="Symlink"):sim.reboot()
    assert (outside/"file").read_text()=="keep"
