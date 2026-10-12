"""Native image stickers and explicitly calibrated, bounded experimental ink."""
from __future__ import annotations
import hashlib
import math
from pathlib import Path
import struct
import tempfile
import time

from PIL import Image, ImageOps
from .vendor.ink import geometry
from .vendor.ink.native_image_rmdoc import write_rmdoc

FIRMWARE = "3.28.0.172"
XOCHITL_SHA256 = "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"
REMOTE_HELPER = "/home/root/.local/lib/paperdesk/current/bin/figure-ink"


def create_sticker(image_path: str, output_path: str) -> Path:
    """Create a separate native-image notebook; never overwrite an existing file."""
    source, target = Path(image_path).resolve(), Path(output_path).expanduser()
    if target.suffix.lower() != ".rmdoc":
        raise ValueError("A native sticker file must end in .rmdoc")
    if source.stat().st_size > 40 * 1024 * 1024:
        raise ValueError("Choose an image smaller than 40 MiB")
    with Image.open(source) as im:
        if im.width * im.height > 30_000_000 or getattr(im, "n_frames", 1) != 1:
            raise ValueError("Choose a single image under 30 million pixels")
        im.verify()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paperdesk-sticker-") as temp:
        png = Path(temp) / "image.png"
        with Image.open(source) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
            if min(im.size) < 8:
                raise ValueError("Image dimensions must be at least 8 pixels")
            im.thumbnail((1290, 1660), Image.Resampling.LANCZOS)
            im.save(png)
        generated = Path(temp) / "sticker.rmdoc"
        write_rmdoc(png, generated, visible_name=target.stem)
        with target.open("xb") as out, generated.open("rb") as data:
            for block in iter(lambda: data.read(1024 * 1024), b""):
                out.write(block)
    return target


def prepare_ink(source: str, preview: str,
                placement=(1000, 1000, 9000, 9000), threshold=160,
                resolution=160, mode="diagram", axis_ratio=11180 / 15340) -> dict:
    """Prepare a local preview. This operation sends no tablet input."""
    source_path = Path(source).resolve()
    if source_path.stat().st_size > 40 * 1024 * 1024:
        raise ValueError("Choose a figure smaller than 40 MiB")
    preview_path = Path(preview).expanduser()
    # Exclusive output reservation prevents overwriting a selected original.
    with preview_path.open("xb"):
        pass
    try:
        strokes, stats = geometry.convert(source_path, preview_path, placement,
                                           threshold, resolution, mode, axis_ratio)
    except BaseException:
        preview_path.unlink(missing_ok=True)
        raise
    return {"strokes": strokes, "stats": stats, "preview": str(preview_path),
            "placement": list(placement), "experimental": True}


def _read_exact(stream, amount: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < amount:
        if time.monotonic() >= deadline:
            raise TimeoutError("Ink acknowledgement timed out; inspect the tablet, no replay")
        block = stream.read(amount - len(data))
        if not block:
            raise RuntimeError("Ink link closed; partial marks may remain, no replay")
        data.extend(block)
    return bytes(data)


def probe_ink(transport, info: dict) -> dict:
    """Verify a wizard-managed helper and probe physical pen axes; no provisioning."""
    if (info.get("model") not in ("paper-pro", "Paper Pro", "reMarkable Paper Pro", "ferrari")
            or info.get("firmware") != FIRMWARE):
        raise RuntimeError("Experimental ink requires the verified Paper Pro firmware 3.28.0.172")
    observed = transport.run("sha256sum /usr/bin/xochitl").split()[0]
    if observed != XOCHITL_SHA256:
        raise RuntimeError("The tablet app build is unverified; no ink helper was installed")
    binary = (Path(__file__).parent / "vendor/ink/stylus-injector-aarch64").read_bytes()
    if not transport.exists(REMOTE_HELPER):
        raise RuntimeError("Install Keyboard forwarding in Setup first; the verified ink helper is required.")
    digest = transport.run(f"sha256sum {REMOTE_HELPER}").split()[0]
    if digest != hashlib.sha256(binary).hexdigest():
        raise RuntimeError("Ink helper transfer verification failed")
    stream = transport.stream(REMOTE_HELPER, timeout=10)
    try:
        stream.write(b"I")
        stream.flush()
        raw = _read_exact(stream, 144, time.monotonic() + 10)
    finally:
        stream.close()
    axes = struct.unpack("<36i", raw)
    if not (axes[1] < axes[2] and axes[7] < axes[8]):
        raise RuntimeError("Physical pen reported invalid axes; drawing refused")
    ratio = (axes[2] - axes[1]) / (axes[8] - axes[7])
    if not math.isfinite(ratio) or not 0.1 <= ratio <= 10:
        raise RuntimeError("Physical pen aspect ratio is invalid")
    return {"ok": True, "axes": list(axes), "axis_ratio": ratio,
            "message": "Select the pen and an empty region; inspect a short calibration stroke first."}


def stream_ink(transport, strokes, cancelled=lambda: False,
               progress=lambda n, total: None) -> dict:
    """Send acknowledged records once; caller must complete preview/calibration UX."""
    records = geometry.protocol(strokes)
    stream = transport.stream(REMOTE_HELPER, timeout=2)
    deadline = time.monotonic() + 28
    sent = 0
    try:
        stream.write(b"D")
        stream.flush()
        if _read_exact(stream, 1, deadline) != b"R":
            raise RuntimeError("Ink helper is not ready")
        for index, record in enumerate(records):
            if cancelled():
                raise InterruptedError("Ink cancelled; partial marks may remain, no replay")
            if time.monotonic() >= deadline:
                raise TimeoutError("Ink deadline exceeded; partial marks may remain, no replay")
            stream.write(record)
            stream.flush()
            sent += 1
            expected = b"E" if record[0] == 3 else b"A"
            if _read_exact(stream, 1, deadline) != expected:
                raise RuntimeError("Ink acknowledgement failed; partial marks may remain, no replay")
            progress(index + 1, len(records))
        return {"ok": True, "records": sent,
                "message": "Ink events sent once. Inspect the marks on the tablet; undo is not atomic."}
    except Exception as exc:
        raise RuntimeError(f"{exc}. {sent} records sent; inspect the tablet before any new drawing.") from exc
    finally:
        stream.close()
