"""Exercise bundled assets and suite features entirely inside a disposable tablet."""
from pathlib import Path
import hashlib
import json
import tempfile


def run() -> dict:
    from PIL import Image
    from .backend import SuiteService
    from .paths import resource_root

    inventory = json.loads((resource_root() / "vendor/tablet/artifacts.json").read_text())["sha256"]
    for relative, expected in inventory.items():
        actual = hashlib.sha256((resource_root() / "vendor" / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Bundled tablet asset integrity failed: {relative}")
    with tempfile.TemporaryDirectory(prefix="paperdesk-selftest-") as directory:
        root = Path(directory)
        service = SuiteService.demo(root / "tablet")
        try:
            assert service.connect()["simulated"]
            features = ["keyboard", "touch", "direct-paste", "art-display"]
            assert service.install(features)["ok"]
            assert service.type_text("Bundled application test\n")["ok"]
            assert service.send_key("backspace")["ok"]
            assert service.add_page("cornell")["ok"]
            source = root / "image.png"
            Image.new("RGBA", (80, 120), (50, 80, 110, 190)).save(source)
            assert service.paste_image(str(source))["ok"]
            assert service.create_sticker(str(source))["ok"]
            assert service.screenshot(str(root / "screen.png"))["ok"]
            assert service.export_current(str(root / "copy.pdf"))["ok"]
            assert service.upload(str(root / "copy.pdf"))["ok"]
            assert service.start_art("krita")["ok"]
            assert service.stop_art()["ok"]
            assert service.simulate_reboot()["ok"]
            assert not service.status()["capabilities"]["direct-paste"]
            assert service.install(features)["ok"]
            assert service.uninstall()["ok"]
        finally:
            service.disconnect()
    return {"ok": True, "simulated": True, "asset_hashes_verified": len(inventory),
            "features": ["setup", "text", "keys", "pages", "image-paste", "sticker",
                         "screenshot", "export", "upload", "art", "reboot", "restore", "uninstall"],
            "hardware_validated": False}
