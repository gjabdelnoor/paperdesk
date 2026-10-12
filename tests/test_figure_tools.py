import json
import zipfile
from pathlib import Path
import pytest
from PIL import Image
from paperdesk.figure_tools import create_sticker, prepare_ink, stream_ink, probe_ink
from paperdesk.vendor.ink.geometry import protocol


def test_sticker_preserves_source_and_existing_destination(tmp_path):
    source = tmp_path / "original.png"
    Image.new("RGBA", (80, 120), (20, 30, 40, 80)).save(source)
    original = source.read_bytes()
    target = create_sticker(str(source), str(tmp_path / "sticker.rmdoc"))
    with zipfile.ZipFile(target) as archive:
        assert any(name.endswith(".rm") for name in archive.namelist())
        metadata = next(n for n in archive.namelist() if n.endswith(".metadata"))
        info = json.loads(archive.read(metadata))
        assert info["type"] == "DocumentType" and info["visibleName"] == "sticker"
    saved = target.read_bytes()
    with pytest.raises(FileExistsError):
        create_sticker(str(source), str(target))
    assert source.read_bytes() == original and target.read_bytes() == saved


def test_ink_preview_refuses_external_svg_and_keeps_original(tmp_path):
    source = tmp_path / "geometry.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><line x1="10" y1="20" x2="70" y2="80" /></svg>')
    result = prepare_ink(str(source), str(tmp_path / "preview.png"))
    assert result["stats"]["strokes"] == 1
    assert result["stats"]["records"] <= 1200
    with pytest.raises(FileExistsError):
        prepare_ink(str(source), str(source))
    source.write_text('<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///etc/passwd">]><svg/>')
    with pytest.raises(ValueError):
        prepare_ink(str(source), str(tmp_path / "bad.png"))
    assert not (tmp_path / "bad.png").exists()


def test_unknown_build_blocks_all_remote_writes():
    class Remote:
        def run(self, script): return "bad-hash /usr/bin/xochitl"
        def write_bytes(self, *args, **kwargs): pytest.fail("unverified build was written")
    with pytest.raises(RuntimeError, match="unverified"):
        probe_ink(Remote(), {"model": "paper-pro", "firmware": "3.28.0.172"})


def test_ack_failure_closes_stream_and_never_replays():
    class Stream:
        closed = False
        writes = []
        replies = iter([b"R", b"X"])
        def write(self, data): self.writes.append(data)
        def flush(self): pass
        def read(self, size): return next(self.replies)
        def close(self): self.closed = True
    class Remote:
        calls = 0
        channel = Stream()
        def stream(self, *args, **kwargs):
            self.calls += 1
            return self.channel
    remote = Remote()
    with pytest.raises(RuntimeError, match="1 records sent"):
        stream_ink(remote, [[(1000, 1000), (2000, 2000)]])
    assert remote.calls == 1 and remote.channel.closed
    assert len(remote.channel.writes) == 2
