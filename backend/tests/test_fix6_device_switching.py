"""FIX (bugfix): the active device must follow the Windows default output, even
when switching between two Bluetooth headphones.

Root causes fixed:
  A) _name_match_score split on " (" so "Headphones (CMF Buds 2 Plus)" and
     "Headphones (OPPO Enco Air3 Pro)" both reduced to base "headphones" and
     tied — _best_name_match then kept whichever was listed first (the
     first-connected device). Exact full-string match now scores highest.
  B) The default-output match only considered Status=="OK" endpoints, but
     Windows reports BT endpoints as "Unknown" even while selected, so the
     match fell back to the first OK endpoint. It now matches against ALL
     endpoints.
"""
import monitor


# ---------- A) _name_match_score / _best_name_match ----------

def test_exact_match_beats_shared_prefix_tie():
    exact = monitor._name_match_score("Headphones (OPPO Enco Air3 Pro)",
                                      "Headphones (OPPO Enco Air3 Pro)")
    prefix = monitor._name_match_score("Headphones (OPPO Enco Air3 Pro)",
                                       "Headphones (CMF Buds 2 Plus)")
    assert exact is not None and prefix is not None
    assert exact > prefix


def test_empty_string_returns_none():
    assert monitor._name_match_score("", "Headphones (X)") is None
    assert monitor._name_match_score("Headphones (X)", "") is None
    assert monitor._name_match_score("   ", "x") is None


def test_base_suffix_match_still_works():
    # "X" vs "X (Stereo)" should still match via the base check.
    assert monitor._name_match_score("CMF Buds 2 Plus", "CMF Buds 2 Plus (Stereo)") == 1_000_000


def test_buds_vs_buds_pro_prefers_exact():
    cands = ["Buds", "Buds Pro"]
    assert monitor._best_name_match("Buds", cands) == "Buds"
    assert monitor._best_name_match("Buds Pro", cands) == "Buds Pro"


def test_best_match_picks_exact_endpoint_over_sibling_listed_first():
    # CMF endpoint is listed FIRST; matching the Air3 default must still pick Air3.
    eps = [
        {"name": "Headphones (CMF Buds 2 Plus)"},
        {"name": "Headphones (OPPO Enco Air3 Pro)"},
    ]
    match = monitor._best_name_match("Headphones (OPPO Enco Air3 Pro)", eps,
                                     name_key=lambda e: e["name"])
    assert match["name"] == "Headphones (OPPO Enco Air3 Pro)"


# ---------- B) build_snapshot_from_raw device switching ----------

def _raw_two_headphones():
    return {
        "bluetooth": [
            {"Name": "CMF Buds 2 Plus", "InstanceId": "BTHENUM\\DEV_2CBEEE5B8F4A\\x"},
            {"Name": "OPPO Enco Air3 Pro", "InstanceId": "BTHENUM\\DEV_AABBCCDDEEFF\\x"},
        ],
        # CMF first AND Status OK; Air3 deliberately "Unknown" (the hard case).
        "endpoints": [
            {"FriendlyName": "Headphones (CMF Buds 2 Plus)", "Status": "OK"},
            {"FriendlyName": "Headphones (OPPO Enco Air3 Pro)", "Status": "Unknown"},
            {"FriendlyName": "Speakers (Realtek(R) Audio)", "Status": "OK"},
        ],
    }


def _patch_common(monkeypatch, default_name):
    # Skip the Alt A2DP registry fast-path entirely so we exercise the
    # endpoint/default-output resolution that the bug was in.
    monkeypatch.setattr(monitor, "_alt_a2dp_installed", False)
    monkeypatch.setattr(monitor, "get_default_playback_device_name", lambda: default_name)
    monkeypatch.setattr(monitor, "get_settings", lambda: {"tracked_devices": []})


def test_switch_to_second_headphone_follows_default(monkeypatch):
    _patch_common(monkeypatch, "Headphones (OPPO Enco Air3 Pro)")
    snap = monitor.build_snapshot_from_raw(_raw_two_headphones())
    assert snap["device"] is not None
    assert snap["device"]["name"] == "OPPO Enco Air3 Pro"  # NOT the first-listed CMF


def test_default_is_first_headphone(monkeypatch):
    _patch_common(monkeypatch, "Headphones (CMF Buds 2 Plus)")
    snap = monitor.build_snapshot_from_raw(_raw_two_headphones())
    assert snap["device"]["name"] == "CMF Buds 2 Plus"


def test_default_is_builtin_speakers(monkeypatch):
    _patch_common(monkeypatch, "Speakers (Realtek(R) Audio)")
    snap = monitor.build_snapshot_from_raw(_raw_two_headphones())
    assert snap["device"]["name"] == "Speakers (Realtek(R) Audio)"
    assert snap["device"]["type"] != "bluetooth"


def test_active_flag_marks_the_default_output(monkeypatch):
    _patch_common(monkeypatch, "Headphones (OPPO Enco Air3 Pro)")
    snap = monitor.build_snapshot_from_raw(_raw_two_headphones())
    active = [o for o in snap["outputs"] if o["active"]]
    assert len(active) >= 1
    assert all("OPPO Enco Air3 Pro" in o["name"] for o in active)


def test_no_matching_default_does_not_crash(monkeypatch):
    # default output not present among endpoints -> must not raise, must fall back.
    _patch_common(monkeypatch, "Headphones (Some Unknown Device)")
    snap = monitor.build_snapshot_from_raw(_raw_two_headphones())
    assert "device" in snap and "codec" in snap
