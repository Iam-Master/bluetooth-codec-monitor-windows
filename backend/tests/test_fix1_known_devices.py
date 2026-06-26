"""FIX-1: list_known_devices must not raise KeyError when the active device
dict is missing its 'name' key, and must still handle every active /
connected / known-device branch correctly.

All external dependencies (cached snapshot, live BT scan, known-device list,
photo lookup, last-known battery) are monkeypatched so NO PowerShell, DB, or
network access happens during the test.
"""
import monitor


def _patch(monkeypatch, *, snapshot=None, connected=None, known=None,
           photo=None, last_battery=None):
    monkeypatch.setattr(monitor, "get_cached_snapshot", lambda: snapshot)
    monkeypatch.setattr(monitor, "get_currently_connected_bt_devices",
                        lambda: dict(connected or {}))
    monkeypatch.setattr(monitor, "get_all_known_device_names",
                        lambda: list(known or []))
    monkeypatch.setattr(monitor, "get_photo_path", lambda name: photo)
    monkeypatch.setattr(monitor, "get_last_known_battery",
                        lambda name: last_battery)


def test_missing_name_key_does_not_raise(monkeypatch):
    # active device is bluetooth but has NO 'name' key -> old code raised KeyError
    snap = ({"device": {"type": "bluetooth", "photo": "/x.png"},
             "codec": "SBC"}, [])
    _patch(monkeypatch, snapshot=snap, known=[])
    result = monitor.list_known_devices()  # must NOT raise
    assert isinstance(result, list)
    assert result == []  # nothing usable to append


def test_name_present_not_in_list_appended(monkeypatch):
    snap = ({"device": {"type": "bluetooth", "name": "Sony WH-1000XM4",
                        "photo": "/p.png", "mac": "AA:BB", "battery": 90},
             "codec": "LDAC"}, [])
    _patch(monkeypatch, snapshot=snap, known=[])
    result = monitor.list_known_devices()
    entry = next(d for d in result if d["name"] == "Sony WH-1000XM4")
    assert entry["is_active"] is True
    assert entry["is_connected"] is True
    assert entry["codec"] == "LDAC"


def test_name_present_already_in_list_not_duplicated(monkeypatch):
    snap = ({"device": {"type": "bluetooth", "name": "Buds"},
             "codec": "AAC"}, [])
    _patch(monkeypatch, snapshot=snap, known=["Buds"])
    result = monitor.list_known_devices()
    names = [d["name"] for d in result]
    assert names.count("Buds") == 1


def test_active_device_none(monkeypatch):
    # no snapshot -> active_device stays None, must not raise
    _patch(monkeypatch, snapshot=None, known=["Alpha", "Beta"])
    result = monitor.list_known_devices()
    assert [d["name"] for d in result] == ["Alpha", "Beta"]
    assert all(d["is_active"] is False for d in result)


def test_type_not_bluetooth_not_appended(monkeypatch):
    snap = ({"device": {"type": "usb", "name": "USB DAC"},
             "codec": "PCM"}, [])
    _patch(monkeypatch, snapshot=snap, known=[])
    result = monitor.list_known_devices()
    assert "USB DAC" not in [d["name"] for d in result]
    assert result == []


def test_empty_names_list_returns_empty(monkeypatch):
    _patch(monkeypatch, snapshot=None, connected={}, known=[])
    assert monitor.list_known_devices() == []


def test_connected_not_active_uses_live_battery(monkeypatch):
    _patch(monkeypatch, snapshot=None,
           connected={"Headset": {"mac_raw": "", "battery": 55}},
           known=["Headset"], photo="/h.png")
    entry = next(d for d in monitor.list_known_devices()
                 if d["name"] == "Headset")
    assert entry["is_active"] is False
    assert entry["is_connected"] is True
    assert entry["battery"] == 55
    assert entry["mac"] is None     # mac_raw empty -> None
    assert entry["codec"] is None


def test_disconnected_else_branch_uses_last_known_battery(monkeypatch):
    _patch(monkeypatch, snapshot=None, connected={},
           known=["Old Device"], photo=None, last_battery=80)
    entry = next(d for d in monitor.list_known_devices()
                 if d["name"] == "Old Device")
    assert entry["is_active"] is False
    assert entry["is_connected"] is False
    assert entry["battery"] == 80
    assert entry["mac"] is None
    assert entry["codec"] is None


def test_active_name_empty_string_not_appended(monkeypatch):
    # empty-string name is falsy -> must not be appended and must not raise
    snap = ({"device": {"type": "bluetooth", "name": ""},
             "codec": "SBC"}, [])
    _patch(monkeypatch, snapshot=snap, known=[])
    assert monitor.list_known_devices() == []


def test_missing_name_still_processes_other_known(monkeypatch):
    # active device missing 'name' must not corrupt processing of known names
    snap = ({"device": {"type": "bluetooth"}, "codec": "SBC"}, [])
    _patch(monkeypatch, snapshot=snap, known=["Known1", "Known2"])
    result = monitor.list_known_devices()
    assert [d["name"] for d in result] == ["Known1", "Known2"]
    assert all(d["is_active"] is False for d in result)
