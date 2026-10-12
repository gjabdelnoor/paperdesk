import json

from paperdesk.archive_status import read_archive_status


def test_large_archive_sync_progress_is_read_only_and_stale_records_are_inactive(tmp_path):
    path = tmp_path / "status.json"
    contents = json.dumps({"active": True, "large": True, "updated_at": 1000,
                           "bytes_done": 4, "bytes_total": 10})
    path.write_text(contents)
    status = read_archive_status(tmp_path, now=1001)
    assert status["active"] and status["large"]
    assert status["bytes_done"] == 4 and status["bytes_total"] == 10
    assert "USB-C" in status["message"]
    status = read_archive_status(tmp_path, now=1601)
    assert status["stale"] and not status["active"] and not status["large"]
    assert path.read_text() == contents


def test_missing_invalid_future_and_symlink_status_never_starts_anything(tmp_path):
    assert not read_archive_status(tmp_path, now=1000)["available"]
    path = tmp_path / "status.json"
    path.write_text('{"active": true, "updated_at": 2000}')
    assert not read_archive_status(tmp_path, now=1000)["active"]
    path.write_text('{"active": true, "updated_at": 1000, "bytes_done": -1}')
    assert read_archive_status(tmp_path, now=1000)["invalid"]
    original = tmp_path / "original"
    path.rename(original)
    try:
        path.symlink_to(original)
    except OSError:
        return  # Windows may not grant unprivileged symlink creation.
    assert read_archive_status(tmp_path, now=1000)["invalid"]
    assert original.read_text().endswith('"bytes_done": -1}')
