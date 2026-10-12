"""Image normalization adapted from reMarkable Type's guarded host client.

Only local preparation is included here. This module does not install, connect
or mutate devices. Advanced protocol and live guards are in backend.py.
"""
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps

PROTOCOL = 2
FIRMWARE = "3.28.0.172"
XOCHITL_SHA256 = "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_PIXELS = 30_000_000
MIN_IMAGE_DIMENSION = 8
KNOWN_PAPERS = ((1404, 1872), (1620, 2160))
FIT_WIDTH = 0.92
FIT_HEIGHT = 0.90


def prepare(source: Path, output_dir: Path, paper: tuple[int, int], *, preserve_alpha: bool = True) -> dict:
    """Normalize one image and write image.png plus the initial manifest.

    By default meaningful PNG alpha is preserved. Explicitly disabling
    preserve_alpha composites transparency onto opaque white RGB instead.
    The image is never upscaled and is scaled down at most to the fit caps of
    the given paper. The manifest's protocol, sizes, and centre placement are
    what the extension re-validates at insert time.
    """
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("image missing or larger than 40 MiB")
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    with Image.open(source) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("animated images are not supported")
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            raise ValueError("image dimensions outside supported range")
        image.verify()
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            raise ValueError("image dimensions outside supported range")
        if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
            raise ValueError(f"image smaller than {MIN_IMAGE_DIMENSION} px in each dimension")
        rgba = image.convert("RGBA")
        alpha_preserved = preserve_alpha and rgba.getchannel("A").getextrema()[0] < 255
        if alpha_preserved:
            prepared = rgba
        else:
            prepared = Image.new("RGB", rgba.size, "white")
            prepared.paste(rgba, mask=rgba.getchannel("A"))
        # Match the extension's fit fractions: the staged image may cover at
        # most 92% of the paper's width and 90% of its height. Images are
        # never upscaled; only larger sources are scaled down.
        max_width = int(paper[0] * FIT_WIDTH + 0.5)
        max_height = int(paper[1] * FIT_HEIGHT + 0.5)
        if prepared.width > max_width or prepared.height > max_height:
            scale = min(max_width / prepared.width, max_height / prepared.height)
            new_size = (max(1, round(prepared.width * scale)), max(1, round(prepared.height * scale)))
            prepared = prepared.resize(new_size, Image.Resampling.LANCZOS)
        png = output_dir / "image.png"
        prepared.save(png, format="PNG")
        png.chmod(0o600)
    if png.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("normalized PNG exceeds 40 MiB")
    data = png.read_bytes()
    manifest = {
        "protocol": PROTOCOL,
        "request_id": output_dir.name,
        "image_name": "image.png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_width": width,
        "source_height": height,
        "image_width": prepared.width,
        "image_height": prepared.height,
        "alpha_preserved": alpha_preserved,
        "paper_width": paper[0],
        "paper_height": paper[1],
        "center_x": 0.0,
        "center_y": paper[1] / 2,
    }
    (output_dir / "request.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    (output_dir / "request.json").chmod(0o600)
    return manifest
