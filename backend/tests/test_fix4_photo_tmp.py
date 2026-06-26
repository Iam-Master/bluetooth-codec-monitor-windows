"""FIX-4: _write_photo_atomic must write atomically and never leave an orphan
.tmp file, whether the write/replace succeeds or fails."""
import os

import pytest

import monitor


def test_success_creates_dest_with_data(tmp_path):
    dest = tmp_path / "a.png"
    monitor._write_photo_atomic(dest, b"imgdata")
    assert dest.exists()
    assert dest.read_bytes() == b"imgdata"


def test_success_leaves_no_tmp(tmp_path):
    dest = tmp_path / "a.png"
    monitor._write_photo_atomic(dest, b"imgdata")
    assert not (tmp_path / "a.png.tmp").exists()


def test_replace_raises_cleans_tmp_and_no_dest(tmp_path, monkeypatch):
    dest = tmp_path / "b.png"
    tmp = tmp_path / "b.png.tmp"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(monitor.os, "replace", boom)
    with pytest.raises(OSError):
        monitor._write_photo_atomic(dest, b"data")
    assert not tmp.exists()      # orphan cleaned by finally
    assert not dest.exists()     # dest never created


def test_write_bytes_failure_no_tmp_no_dest(tmp_path):
    # parent dir does not exist -> tmp.write_bytes raises (subclass of OSError)
    dest = tmp_path / "missing_dir" / "c.png"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with pytest.raises(OSError):
        monitor._write_photo_atomic(dest, b"data")
    assert not tmp.exists()
    assert not dest.exists()


def test_idempotent_overwrite(tmp_path):
    dest = tmp_path / "d.png"
    monitor._write_photo_atomic(dest, b"first")
    monitor._write_photo_atomic(dest, b"second")
    assert dest.read_bytes() == b"second"
    assert not (tmp_path / "d.png.tmp").exists()


def test_multiple_files_no_tmp_remnants(tmp_path):
    for i in range(5):
        dest = tmp_path / f"dev_{i}.png"
        monitor._write_photo_atomic(dest, f"data{i}".encode())
        assert dest.read_bytes() == f"data{i}".encode()
    assert list(tmp_path.glob("*.tmp")) == []


def test_empty_data_written(tmp_path):
    dest = tmp_path / "empty.png"
    monitor._write_photo_atomic(dest, b"")
    assert dest.exists()
    assert dest.read_bytes() == b""
    assert not (tmp_path / "empty.png.tmp").exists()


def test_tmp_naming_and_cleanup_on_noop_replace(tmp_path, monkeypatch):
    dest = tmp_path / "cool.png"
    recorded = {}

    def fake_replace(src, dst):
        # record the arguments but do NOT actually move the file
        recorded["src"] = os.fspath(src)
        recorded["dst"] = os.fspath(dst)

    monkeypatch.setattr(monitor.os, "replace", fake_replace)
    monitor._write_photo_atomic(dest, b"data")
    assert recorded["src"].endswith("cool.png.tmp")
    assert recorded["dst"] == os.fspath(dest)
    # replace was a no-op, so tmp still existed afterwards -> finally removed it
    assert not (tmp_path / "cool.png.tmp").exists()
    assert not dest.exists()


def test_replace_failure_preserves_existing_dest(tmp_path, monkeypatch):
    dest = tmp_path / "e.png"
    dest.write_bytes(b"original")

    def boom(src, dst):
        raise OSError("nope")

    monkeypatch.setattr(monitor.os, "replace", boom)
    with pytest.raises(OSError):
        monitor._write_photo_atomic(dest, b"new")
    assert dest.read_bytes() == b"original"       # untouched
    assert not (tmp_path / "e.png.tmp").exists()   # orphan cleaned


def test_no_tmp_glob_after_success(tmp_path):
    dest = tmp_path / "f.png"
    monitor._write_photo_atomic(dest, b"xyz")
    assert list(tmp_path.glob("*.tmp")) == []
