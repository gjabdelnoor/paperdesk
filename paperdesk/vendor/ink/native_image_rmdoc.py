#!/usr/bin/env python3
"""Create a one-page native-image notebook for the reMarkable USB importer.

The image is a real lines-v6 scene image (not a rasterized PDF), so xochitl
can select, copy, paste, and move it.  The block layout follows the public
image support in rmscene/remarkable-shapes.  This module deliberately has no
third-party dependency beyond Pillow for dimensions/conversion in the caller.

The scene serialization is adapted from the MIT-licensed
``remarkable-shapes`` image writer by knox-dawson; the local notice file keeps
that attribution with this standalone helper.
"""

from __future__ import annotations

import json
import os
import struct
import time
import uuid
import zipfile
from pathlib import Path


PAGE_WIDTH = 1404
PAGE_HEIGHT = 1872
HEADER_V6 = b"reMarkable .lines file, version=6          "


def _varuint(value: int) -> bytes:
    out = bytearray()
    value = int(value)
    if value < 0:
        raise ValueError("varuint cannot be negative")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(index: int, tag_type: int) -> bytes:
    return _varuint((int(index) << 4) | int(tag_type))


def _sub(index: int, data: bytes) -> bytes:
    return _tag(index, 0x0C) + struct.pack("<I", len(data)) + data


def _block(block_type: int, minimum: int, current: int, data: bytes) -> bytes:
    return (struct.pack("<I", len(data)) + bytes((0, minimum, current, block_type))
            + data)


def _tag_bool(index: int, value: bool) -> bytes:
    return _tag(index, 0x01) + bytes((1 if value else 0,))


def _tag_id(index: int, author: int, value: int) -> bytes:
    return _tag(index, 0x0F) + bytes((author,)) + _varuint(value)


def _tag_int(index: int, value: int) -> bytes:
    return _tag(index, 0x04) + struct.pack("<I", int(value))


def _tag_string(index: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _sub(index, _varuint(len(encoded)) + b"\x01" + encoded)


def _uuid_bytes(value: str) -> bytes:
    return uuid.UUID(value).bytes


def _uuid_sub(index: int, value: str) -> bytes:
    return _sub(index, _uuid_bytes(value))


def _lww_string(index: int, author: int, value_id: int, value: str) -> bytes:
    return _sub(index, _tag_id(1, author, value_id) + _tag_string(2, value))


def _lww_uuid(index: int, author: int, value_id: int, value: str) -> bytes:
    return _sub(index, _tag_id(1, author, value_id) + _uuid_sub(2, value))


def _lww_bytes(index: int, author: int, value_id: int, values: list[int]) -> bytes:
    raw = bytes(values)
    return _sub(index, _tag_id(1, author, value_id) + _sub(2, raw))


def _float_sequence(index: int, values: list[float]) -> bytes:
    raw = _varuint(len(values)) + b"".join(struct.pack("<f", float(v)) for v in values)
    return _sub(index, raw)


def _int_sequence(index: int, values: list[int]) -> bytes:
    raw = _varuint(len(values)) + b"".join(struct.pack("<I", int(v)) for v in values)
    return _sub(index, raw)


def _author_ids_block() -> bytes:
    # The fixed author id is the same neutral id used by the tablet's own
    # importer and by remarkable-shapes.  It is little-endian UUID bytes.
    author = bytes((0x9F, 0xA5, 0x5B, 0x49, 0x43, 0xC9, 0x5C, 0x2B,
                    0xB4, 0x55, 0x36, 0x82, 0xF6, 0x94, 0x89, 0x06))
    entry = _varuint(len(author)) + author + struct.pack("<H", 1)
    return _block(0x09, 1, 1, _varuint(1) + _sub(0, entry))


def _page_bytes(image_uuid: str, file_name: str, x: float, y: float,
                width: float, height: float) -> bytes:
    out = bytearray(HEADER_V6)
    out += _author_ids_block()
    out += _block(0x00, 1, 1, _tag_id(1, 1, 1) + _tag_bool(2, True))
    page_info = _tag_int(1, 1) + _tag_int(2, 0) + _tag_int(3, 0) + _tag_int(4, 0)
    out += _block(0x0A, 0, 1, page_info)

    timestamp_id = 17  # image item starts at id 14; image-info timestamp is +3
    image_entry = (_uuid_bytes(image_uuid)
                   + _lww_string(1, 1, timestamp_id, file_name)
                   + _lww_bytes(2, 0, 0, [17, 0]))
    image_info = _sub(1, _varuint(1) + _sub(0, image_entry))
    out += _block(0x0E, 3, 3, image_info)

    scene_tree = (_tag_id(1, 0, 11) + _tag_id(2, 0, 0) + _tag_bool(3, True)
                  + _sub(4, _tag_id(1, 0, 1)))
    out += _block(0x01, 1, 1, scene_tree)

    # Tree-node fields are last-write-wins wrappers, expanded explicitly to
    # keep the bytes identical to the reference writer.
    node = (_tag_id(1, 0, 1) + _sub(2, _tag_id(1, 0, 12) + _tag_string(2, ""))
            + _sub(3, _tag_id(1, 0, 12) + _tag_bool(2, True)))
    out += _block(0x02, 1, 1, node)
    layer = (_tag_id(1, 0, 11) + _sub(2, _tag_id(1, 0, 14)
             + _tag_string(2, "Layer 1"))
             + _sub(3, _tag_id(1, 0, 14) + _tag_bool(2, True)))
    out += _block(0x02, 1, 1, layer)

    group = (_tag_id(1, 0, 1) + _tag_id(2, 0, 13) + _tag_id(3, 0, 0)
             + _tag_id(4, 0, 0) + _tag_int(5, 0)
             + _sub(6, b"\x02" + _tag_id(2, 0, 11)))
    out += _block(0x04, 1, 1, group)

    left = float(x) - PAGE_WIDTH / 2.0
    right = float(x + width) - PAGE_WIDTH / 2.0
    top = float(y)
    bottom = float(y + height)
    vertices = [left, top, 0.0, 0.0, right, top, 1.0, 0.0,
                right, bottom, 1.0, 1.0, left, bottom, 0.0, 1.0]
    image_value = (b"\x07" + _lww_uuid(1, 1, 16, image_uuid)
                   + _tag_id(2, 1, 15) + _float_sequence(3, vertices)
                   + _int_sequence(4, [0, 1, 2, 2, 3, 0]))
    item = (_tag_id(1, 0, 11) + _tag_id(2, 1, 14) + _tag_id(3, 0, 0)
            + _tag_id(4, 0, 0) + _tag_int(5, 0) + _sub(6, image_value))
    out += _block(0x0F, 2, 2, item)
    return bytes(out)


def _content(document_uuid: str, page_uuid: str) -> str:
    data = {
        "cPages": {
            "lastOpened": {"timestamp": "1:1", "value": page_uuid},
            "original": {"timestamp": "1:1", "value": -1},
            "pages": [{"id": page_uuid,
                       "idx": {"timestamp": "1:2", "value": "ba"},
                       "template": {"timestamp": "1:2", "value": "Blank"}}],
            "uuids": [{"first": document_uuid, "second": 1}],
        },
        "coverPageNumber": -1,
        "customZoomCenterX": 0,
        "customZoomCenterY": PAGE_HEIGHT // 2,
        "customZoomOrientation": "portrait",
        "customZoomPageHeight": PAGE_HEIGHT,
        "customZoomPageWidth": PAGE_WIDTH,
        "customZoomScale": 1,
        "documentMetadata": {},
        "extraMetadata": {"LastBallpointv2Color": "Black",
                           "LastBallpointv2Size": "2",
                           "LastEraserColor": "Black", "LastEraserSize": "2",
                           "LastEraserTool": "Eraser", "LastPen": "Ballpointv2",
                           "LastTool": "Ballpointv2"},
        "fileType": "notebook", "fontName": "", "formatVersion": 2,
        "lineHeight": -1, "margins": 125, "orientation": "portrait",
        "pageCount": 1, "pageTags": [], "sizeInBytes": "0", "tags": [],
        "textAlignment": "justify", "textScale": 1, "zoomMode": "bestFit",
    }
    return json.dumps(data, indent=4) + "\n"


def _metadata(visible_name: str) -> str:
    now = str(int(time.time() * 1000))
    return json.dumps({"createdTime": now, "lastModified": now,
                       "lastOpened": now, "lastOpenedPage": 0,
                       "parent": "", "pinned": False, "type": "DocumentType",
                       "visibleName": visible_name}, indent=4) + "\n"


def write_rmdoc(image_path: str | os.PathLike[str], output_path: str | os.PathLike[str],
                visible_name: str | None = None) -> Path:
    """Write *image_path* into a native-image one-page ``.rmdoc`` archive."""
    source = Path(image_path)
    output = Path(output_path)
    document_uuid = str(uuid.uuid4())
    page_uuid = str(uuid.uuid4())
    image_uuid = str(uuid.uuid4())
    name = visible_name or source.stem
    suffix = source.suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in (".png", ".jpg"):
        raise ValueError("native image source must be PNG or JPEG")
    file_name = image_uuid + suffix

    # The image is centered and scaled to fit a generous page margin.  Pillow
    # is imported lazily so callers that only inspect this module need not
    # import it.
    from PIL import Image
    with Image.open(source) as image:
        image_width, image_height = image.size
    x, y = 130.0, 130.0
    max_width, max_height = PAGE_WIDTH - 2 * x, PAGE_HEIGHT - 2 * y
    scale = min(max_width / image_width, max_height / image_height)
    width, height = image_width * scale, image_height * scale

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(document_uuid + ".content", _content(document_uuid, page_uuid))
        archive.writestr(document_uuid + ".metadata", _metadata(name))
        archive.writestr(document_uuid + "/" + page_uuid + ".rm",
                         _page_bytes(image_uuid, file_name, x, y, width, height))
        archive.writestr(document_uuid + "/" + page_uuid + "/" + file_name,
                         source.read_bytes())
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("output")
    args = parser.parse_args()
    write_rmdoc(args.image, args.output)
