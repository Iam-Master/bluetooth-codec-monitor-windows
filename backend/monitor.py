"""
Codec Monitor backend v5.

Reads REAL codec, sample rate, bitrate from Alt A2DP driver registry.
No hardcoded codec values — everything comes from the system.

Ports:
  8765 - serves frontend files + cached device photos
  8766 - WebSocket for live data
"""
import asyncio
import collections
import ctypes
from ctypes import wintypes
import http.server
import json
import os
import re
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import winreg
from datetime import datetime
from pathlib import Path

import websockets
from win11toast import toast as _win_toast
import comtypes
from pycaw.utils import AudioUtilities

# Bundled read-only resources (frontend HTML/CSS/JS, codec_info.json) live next
# to this script when run from source, or inside PyInstaller's temp extraction
# dir (sys._MEIPASS) when packaged as an .exe.
if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    _BUNDLE_DIR = Path(__file__).resolve().parent

ROOT = Path(__file__).resolve().parent.parent if not getattr(sys, "frozen", False) else _BUNDLE_DIR
FRONTEND_DIR = ROOT / "frontend"
INFO_PATH = _BUNDLE_DIR / "codec_info.json"

# Writable runtime data (settings, history, cached photos) must NOT live in the
# PyInstaller temp dir — that gets wiped and re-extracted on every launch.
# Use %APPDATA%\CodecMonitor when packaged, the backend folder when run from source.
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "CodecMonitor"
else:
    DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

PHOTOS_DIR = DATA_DIR / "device_photos"
PHOTOS_DIR.mkdir(exist_ok=True)

PORT_HTTP = 8765
PORT_WS = 8766
APP_VERSION = "1.0"

CODEC_INFO = json.loads(INFO_PATH.read_text(encoding="utf-8"))

_cached_snapshot = None
_snapshot_lock = threading.Lock()
_current_device_name = None
_device_connect_time = time.time()

_photo_fetch_lock = threading.Lock()

# Raw bt_devices data from the slow PowerShell-based loop (battery lookups are
# the expensive part here, ~1.2s/device). The fast loop reads this (possibly
# several seconds stale) for non-Alt-A2DP battery/device info — never blocks on it.
_cached_raw = {"bluetooth": []}
_cached_raw_lock = threading.Lock()

# Audio endpoints (built-in speakers, wired, HDMI, etc.) on their own faster,
# independent loop — these don't need battery lookups, so gating them behind
# the slow bt+battery cycle was making the whole "All audio outputs" list (and
# the active wired/built-in device) sit empty for as long as that cycle took.
_cached_endpoints = []
_cached_endpoints_lock = threading.Lock()

# Battery is the slowest thing PowerShell gives us (~1.2s per device via
# Get-PnpDeviceProperty) and barely changes minute to minute, so it's cached
# opportunistically by the slow loop instead of blocking the fast one.
_battery_cache = {}
_battery_cache_lock = threading.Lock()


def get_cached_battery(mac_raw: str):
    with _battery_cache_lock:
        entry = _battery_cache.get(mac_raw)
    return entry if entry is not None else None


def set_cached_battery(mac_raw: str, value):
    with _battery_cache_lock:
        _battery_cache[mac_raw] = value


# A device's Windows InstanceId (e.g. "BTHENUM\DEV_xxx\...") is stable for as
# long as it stays paired, so it only needs discovering once via the slow
# PowerShell loop — after that, checking whether it's connected RIGHT NOW can
# be done instantly via direct Win32 device-property calls (see
# cm_is_device_connected), with no PowerShell involved at all.
_instance_id_cache = {}
_instance_id_cache_lock = threading.Lock()


def set_cached_instance_id(name: str, instance_id: str):
    """One physical device can expose multiple PnP nodes under the SAME
    friendly name (e.g. a classic BTHENUM profile node and a separate BTHLE
    node) — keeping only the most-recently-seen one is a real bug: whichever
    node happens to win is enumeration-order-dependent, and if the "wrong"
    one wins, its IsConnected never reflects the device's actual connection.
    So this tracks ALL instance_ids seen per name; get_live_connected_status()
    below then treats the device as connected if ANY of them report True."""
    with _instance_id_cache_lock:
        _instance_id_cache.setdefault(name, set()).add(instance_id)


def get_cached_instance_ids() -> dict:
    with _instance_id_cache_lock:
        return {name: set(ids) for name, ids in _instance_id_cache.items()}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.wintypes.DWORD), ("Data2", ctypes.wintypes.WORD), ("Data3", ctypes.wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


_DEVPKEY_Device_IsConnected = _DEVPROPKEY(
    _GUID(0x83DA6326, 0x97A6, 0x4088, (ctypes.c_ubyte * 8)(0x94, 0x53, 0xA1, 0x92, 0x3F, 0x57, 0x3B, 0x29)),
    15,
)


def cm_is_device_connected(instance_id: str) -> bool | None:
    """Direct Win32 device-property query (CfgMgr32) for whether a Bluetooth
    device is connected RIGHT NOW — the same property Windows' own Bluetooth
    settings page uses (verified by diffing real property dumps against
    Windows' UI). No PowerShell, no subprocess — a few hundred microseconds.
    Returns None if the device can't be located (e.g. unpaired)."""
    try:
        cfgmgr32 = ctypes.windll.cfgmgr32
        devinst = ctypes.wintypes.ULONG()
        if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id, 0) != 0:
            return None
        prop_type = ctypes.c_ulong()
        buf = ctypes.create_string_buffer(8)
        buf_size = ctypes.wintypes.ULONG(8)
        ret = cfgmgr32.CM_Get_DevNode_PropertyW(
            devinst, ctypes.byref(_DEVPKEY_Device_IsConnected),
            ctypes.byref(prop_type), buf, ctypes.byref(buf_size), 0,
        )
        if ret != 0 or buf_size.value < 1:
            return None
        return buf.raw[0] != 0
    except Exception:
        return None


def get_live_connected_status() -> dict:
    """{name: bool} for every device whose InstanceId(s) we've discovered, via
    the fast ctypes check above — instant, independent of the slow loop's
    cadence. This is what makes the Devices page detect connect/disconnect
    of any known device (not just the active audio one) in well under a
    second instead of waiting on the next PowerShell battery cycle.

    A device counts as connected if ANY of its known PnP nodes report it —
    one physical device can have multiple nodes (classic + BLE) sharing the
    same friendly name, and only the classic profile node's IsConnected
    reliably tracks the actual audio connection."""
    return {
        name: any(cm_is_device_connected(iid) for iid in instance_ids)
        for name, instance_ids in get_cached_instance_ids().items()
    }


MAX_HISTORY = 2200
_history = collections.deque(maxlen=MAX_HISTORY)
_history_lock = threading.Lock()

HISTORY_DB_PATH = DATA_DIR / "history.db"
HISTORY_LOAD_HOURS = 48       # how much history to preload into memory on startup
_pending_history_rows = []
_pending_history_lock = threading.Lock()

# ---------- Settings ----------

SETTINGS_PATH = DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "poll_interval_ms": 800,
    "history_retention_days": 14,
    "notifications_enabled": True,
    "start_minimized": False,
    "tracked_devices": [],   # empty = track all paired devices
    "close_action": "minimize",  # "minimize" (hide to tray) or "quit" (fully exit)
}
_settings = dict(DEFAULT_SETTINGS)
_settings_lock = threading.Lock()


def load_settings():
    global _settings
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    with _settings_lock:
        _settings = data
    if not SETTINGS_PATH.exists():
        save_settings(data)
    return data


def get_settings() -> dict:
    with _settings_lock:
        return dict(_settings)


def save_settings(new_settings: dict):
    global _settings
    merged = dict(DEFAULT_SETTINGS)
    with _settings_lock:
        merged.update(_settings)
    for key in DEFAULT_SETTINGS:
        if key in new_settings:
            merged[key] = new_settings[key]
    with _settings_lock:
        _settings = merged
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged

_alerts = collections.deque(maxlen=100)
_alerts_lock = threading.Lock()

_stability_events = collections.deque(maxlen=200)  # timestamps of disconnects/downgrades
_stability_lock = threading.Lock()

_UNSET = object()  # sentinel distinct from None, since None is a legitimate "no device" value
_prev_codec = _UNSET
_prev_device = _UNSET
_pending_device = None
_pending_device_count = 0
_pending_codec = None
_pending_codec_count = 0
DEBOUNCE_POLLS = 2  # require N consecutive identical polls before confirming a change/alert

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------- Orphan-proofing for the PowerShell child process ----------
# subprocess.Popen children are NOT killed automatically when the parent dies
# on Windows — not on a clean exit, and definitely not if the parent is force-
# killed (Task Manager, a crash, etc). A Windows Job Object with
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes Windows itself kill any assigned
# process the moment this process's handle table is torn down, for ANY reason.

_job_handle = None
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        _JobObjectExtendedLimitInformation = 9
        _PROCESS_ALL_ACCESS = 0x1F0FFF

        _job_handle = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if _job_handle:
            _info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            _info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ctypes.windll.kernel32.SetInformationJobObject(
                _job_handle, _JobObjectExtendedLimitInformation, ctypes.byref(_info), ctypes.sizeof(_info)
            )
    except Exception:
        _job_handle = None


def _assign_process_to_job(proc):
    """Best-effort — if this fails, the process just behaves as it did before
    (still cleaned up on a normal Quit, just not orphan-proof against a crash)."""
    if not _job_handle or not proc or not proc.pid:
        return
    try:
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, proc.pid)
        if handle:
            ctypes.windll.kernel32.AssignProcessToJobObject(_job_handle, handle)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


# ---------- Alt A2DP codec detection via registry ----------

ALT_A2DP_REG_BASE = r"SYSTEM\CurrentControlSet\Services\AltA2dp\Parameters\Devices"

CODEC_NAMES = {1: "SBC", 2: "AAC", 4: "LDAC", 8: "aptX", 16: "aptX HD"}

CODEC_SAMPLE_RATES = {
    "SBC":     {1: 48000, 2: 44100, 4: 32000, 8: 16000},
    "AAC":     {8: 44100, 16: 48000},
    "LDAC":    {4: 96000, 16: 48000, 32: 44100},
    "aptX":    {1: 48000, 2: 44100},
    "aptX HD": {1: 48000, 2: 44100},
}

CODEC_SF_KEY = {
    "SBC": "SbcSamplingFrequency", "AAC": "AacSamplingFrequency",
    "LDAC": "LdacSamplingFrequency", "aptX": "AptxSamplingFrequency",
    "aptX HD": "AptxHdSamplingFrequency",
}

CODEC_BIT_DEPTH_KEY = {"LDAC": "LdacSampleFormat", "aptX HD": "AptxHdSampleFormat"}
CODEC_BIT_DEPTH_MAP = {1: 16, 2: 24, 4: 32}

LDAC_EQMID_BITRATE = {0: 990, 1: 660, 2: 330}
CODEC_NOMINAL_BITRATE = {"SBC": 328, "AAC": 256, "aptX": 352, "aptX HD": 576}


def _reg_read_dword(key, name):
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return int(val)
    except (FileNotFoundError, ValueError, OSError):
        return 0


def read_alt_a2dp_current(mac_12: str) -> dict | None:
    """Read real-time codec info from Alt A2DP registry for a device."""
    reg_key_name = mac_12.lower().zfill(16)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"{ALT_A2DP_REG_BASE}\\Current\\{reg_key_name}",
        )
    except (FileNotFoundError, OSError):
        return None

    codec_val = _reg_read_dword(key, "Codec")
    opened = _reg_read_dword(key, "Opened")
    bitrate = _reg_read_dword(key, "Bitrate")

    codec_name = CODEC_NAMES.get(codec_val)
    if not codec_name or not opened:
        winreg.CloseKey(key)
        return None

    sf_key = CODEC_SF_KEY.get(codec_name, "")
    sf_val = _reg_read_dword(key, sf_key) if sf_key else 0
    sample_rate = CODEC_SAMPLE_RATES.get(codec_name, {}).get(sf_val, 44100)

    bd_key = CODEC_BIT_DEPTH_KEY.get(codec_name)
    if bd_key:
        bd_val = _reg_read_dword(key, bd_key)
        bit_depth = CODEC_BIT_DEPTH_MAP.get(bd_val, 16)
    else:
        bit_depth = 16

    if bitrate > 0:
        bitrate_kbps = round(bitrate / 1000)
    elif codec_name == "LDAC":
        eqmid = _reg_read_dword(key, "LdacEqmid")
        bitrate_kbps = LDAC_EQMID_BITRATE.get(eqmid, 660)
    else:
        bitrate_kbps = CODEC_NOMINAL_BITRATE.get(codec_name, 328)

    winreg.CloseKey(key)
    return {
        "name": codec_name,
        "bitrate_kbps": bitrate_kbps,
        "sample_rate_khz": sample_rate / 1000,
        "bit_depth": bit_depth,
        "driver": "Alt A2DP",
    }


def alt_a2dp_device_opened(mac_12: str) -> bool | None:
    """True/False if the Alt A2DP registry has live connection state for this device,
    None if it has no entry at all (e.g. driver not installed, or never connected via it).

    This is checked because Windows' own PnP/endpoint status can lag tens of seconds
    behind a real disconnect — the driver's registry flips almost instantly.
    """
    reg_key_name = mac_12.lower().zfill(16)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"{ALT_A2DP_REG_BASE}\\Current\\{reg_key_name}",
        )
    except (FileNotFoundError, OSError):
        return None
    opened = bool(_reg_read_dword(key, "Opened"))
    winreg.CloseKey(key)
    return opened


def is_alt_a2dp_installed() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Services\AltA2dp")
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False


def _normalize_mac_raw(mac_raw: str) -> str:
    """Registry key names are sometimes zero-padded to 16 hex chars (matching
    the Current\\{mac} key format), while extract_mac_raw() (used by the slow
    PowerShell loop, via InstanceId regex) always produces the real 12-char
    MAC. Without normalizing both to the same form, the fast path's mac_raw
    never matches the battery cache key set by the slow loop — battery always
    showed "Not reported" for fast-path-detected devices because of this."""
    mac_raw = mac_raw.lower()
    return mac_raw[-12:] if len(mac_raw) > 12 else mac_raw


def get_known_devices_with_mac() -> list[tuple[str, str]]:
    """Enumerate the Alt A2DP Capability registry: [(mac_raw, name), ...].

    The Capability subkey name IS the raw mac hex string (same format used by
    the Current\\{mac} key), so this also gives us a fast way to find which
    paired device is actively streaming without touching PowerShell at all.
    """
    result = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              f"{ALT_A2DP_REG_BASE}\\Capability")
        i = 0
        while True:
            try:
                key_name = winreg.EnumKey(base, i)
                sub = winreg.OpenKey(base, key_name)
                try:
                    name, _ = winreg.QueryValueEx(sub, "Name")
                    if name:
                        result.append((_normalize_mac_raw(key_name), name))
                except (FileNotFoundError, OSError):
                    pass
                winreg.CloseKey(sub)
                i += 1
            except OSError:
                break
        winreg.CloseKey(base)
    except (FileNotFoundError, OSError):
        pass
    return result


def get_known_device_names() -> list[str]:
    """Read device names from Alt A2DP Capability registry for photo pre-fetch."""
    seen = []
    for _, name in get_known_devices_with_mac():
        if name not in seen:
            seen.append(name)
    return seen


def get_default_playback_device_name() -> str | None:
    """The TRUE current Windows default playback device, via the Core Audio
    API (pycaw) — not inferred from PnP/registry state. Needed because Alt
    A2DP can leave a device's Current\\{mac} registry key showing Opened=1
    even after Windows switches the active output to something else (the
    underlying A2DP connection just isn't torn down immediately) — trusting
    Opened alone meant switching outputs kept showing the previous device.
    Sub-50ms; safe to call every fast-loop tick."""
    try:
        return AudioUtilities.GetSpeakers().FriendlyName
    except Exception:
        return None


def find_active_alt_a2dp_device() -> dict | None:
    """Pure-registry scan (no PowerShell, sub-millisecond) for which known,
    paired device is actively streaming via Alt A2DP right now — cross-checked
    against the real Windows default playback device so a device that's still
    "Opened" but no longer selected as the output doesn't get reported as active.

    This is the key fix for slow connect/disconnect/switch detection: codec
    state already comes from the registry (instant), but it was only ever
    re-checked once a new line arrived from the PowerShell poller — which can
    take many seconds per cycle (see force_refresh/_slow_loop). Scanning the
    known MACs directly means a device switch is caught on the very next fast
    poll tick, independent of how slow the PowerShell-based battery/endpoint
    refresh happens to be.
    """
    if not is_alt_a2dp_installed():
        return None
    default_name = get_default_playback_device_name()
    seen_macs = set()
    for mac_raw, name in get_known_devices_with_mac():
        if mac_raw in seen_macs:
            continue
        seen_macs.add(mac_raw)
        codec = read_alt_a2dp_current(mac_raw)
        if codec is None:
            continue
        if default_name and name.lower() not in default_name.lower():
            continue  # Opened in the registry, but not the selected output right now
        return {"mac_raw": mac_raw, "name": name, "codec": codec}
    return None


# ---------- Device photo fetching (generic — works for any device) ----------

def _search_device_image_url(device_name: str) -> str | None:
    """Search DuckDuckGo for a product image of the given device.

    Verifies that the image comes from an official brand source or a
    reputable tech site to ensure correctness. Returns the first verified
    image URL found, or None.
    """
    query = f"{device_name} bluetooth product photo"
    
    # Extract likely brand from the first word of the device name
    brand = device_name.split()[0].lower()
    
    # Reputable domains to verify against (includes the brand itself)
    reputable_domains = [
        brand, "rtings.com", "soundguys.com", "head-fi.org", "whathifi.com",
        "techradar.com", "theverge.com", "cnet.com", "tomsguide.com",
        "amazon.", "bestbuy.com", "gsmarena.com", "scarbir.com"
    ]

    try:
        # Step 1: get a vqd token from the search page
        token_url = f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(token_url, headers={"User-Agent": "CodecMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r"vqd=(['\"])([^'\"]+)\1", html)
        if not m:
            # Fallback: try the alternate vqd pattern
            m = re.search(r"vqd=([\d\-]+)", html)
        if not m:
            return None
        vqd = m.group(2) if m.lastindex == 2 else m.group(1)

        # Step 2: query the image search API
        img_url = (
            f"https://duckduckgo.com/i.js?q={urllib.parse.quote(query)}"
            f"&vqd={vqd}&o=json&p=1&s=0"
        )
        req2 = urllib.request.Request(img_url, headers={
            "User-Agent": "CodecMonitor/1.0",
            "Referer": "https://duckduckgo.com/",
        })
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            data = json.loads(resp2.read().decode("utf-8", errors="replace"))
        
        # Step 3: Verify the results
        results = data.get("results", [])
        for r in results:
            url = r.get("image", "")
            source_url = r.get("url", "").lower()
            
            if url and url.startswith("http"):
                # Ensure the source webpage is from the brand or a trusted review site
                if any(domain in source_url for domain in reputable_domains):
                    return url
    except Exception as e:
        print(f"  Image search failed for '{device_name}': {e}")
    return None

WINDOWS_PAIRED_BT_SCRIPT = r"""
Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {
    $_.FriendlyName -and
    $_.FriendlyName -notmatch 'Generic|Profile|^Bluetooth LE|Service|Enumerator|Transport|Avrcp|RFCOMM|Microsoft Bluetooth|Personal Area|Identification|Standard Serial|Wireless Bluetooth|Bluetooth Adapter|Bluetooth Radio|Bluetooth Module'
} | Select-Object -ExpandProperty FriendlyName
"""

def get_windows_paired_bt_names() -> list[str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_PAIRED_BT_SCRIPT],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError):
        return []

def get_history_seen_device_names() -> list[str]:
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT DISTINCT device FROM history WHERE device IS NOT NULL AND type = 'bluetooth'"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]

_DEVICE_NAME_WRAPPER_RE = re.compile(r"^(?:headset|headphones|hands-?free.*?)\s*\((.+)\)$", re.IGNORECASE)

def _clean_device_name(name: str) -> str:
    m = _DEVICE_NAME_WRAPPER_RE.match(name.strip())
    return m.group(1).strip() if m else name.strip()

def get_all_known_device_names() -> list[str]:
    raw = get_known_device_names() + get_history_seen_device_names() + get_windows_paired_bt_names()
    names = []
    seen_lower = set()
    for r in raw:
        name = _clean_device_name(r)
        if name.lower() not in seen_lower:
            seen_lower.add(name.lower())
            names.append(name)
    return names

def get_last_known_battery(device_name: str):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT battery FROM history WHERE device = ? AND battery IS NOT NULL "
            "ORDER BY t DESC LIMIT 1",
            (device_name,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None

def get_currently_connected_bt_devices() -> dict:
    live_status = get_live_connected_status()
    result = {}
    instance_id_sets = get_cached_instance_ids()
    for name, connected in live_status.items():
        if not connected:
            continue
        instance_id = next(iter(instance_id_sets.get(name, ())), "")
        mac_raw = extract_mac_raw(instance_id)
        battery = get_cached_battery(mac_raw) if mac_raw else None
        result[name] = {"mac_raw": mac_raw, "battery": battery}
    return result

def list_known_devices() -> list[dict]:
    cached = get_cached_snapshot()
    active_device = None
    if cached:
        snap, _ = cached
        active_device = snap.get("device")

    connected_now = get_currently_connected_bt_devices()

    names = get_all_known_device_names()
    if active_device and active_device.get("type") == "bluetooth" and active_device["name"] not in names:
        names.append(active_device["name"])

    result = []
    for name in names:
        is_active = bool(active_device and active_device.get("name") == name)
        live = connected_now.get(name)
        is_connected = is_active or live is not None
        if is_active:
            result.append({
                "name": name,
                "photo": active_device.get("photo"),
                "is_active": True,
                "is_connected": True,
                "mac": active_device.get("mac"),
                "battery": active_device.get("battery"),
                "codec": cached[0]["codec"] if cached else None,
            })
        elif live is not None:
            mac = format_mac(live["mac_raw"]) if live["mac_raw"] else None
            result.append({
                "name": name,
                "photo": get_photo_path(name),
                "is_active": False,
                "is_connected": True,
                "mac": mac,
                "battery": live["battery"] if live["battery"] is not None else get_last_known_battery(name),
                "codec": None,
            })
        else:
            result.append({
                "name": name,
                "photo": get_photo_path(name),
                "is_active": False,
                "is_connected": False,
                "mac": None,
                "battery": get_last_known_battery(name),
                "codec": None,
            })
    return result


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def get_photo_path(device_name: str) -> str | None:
    if not device_name:
        return None
    s = slug(device_name)
    for ext in ("png", "jpg", "webp", "jpeg"):
        p = PHOTOS_DIR / f"{s}.{ext}"
        if p.exists() and p.stat().st_size > 500:
            return f"/photos/{s}.{ext}"
    return None


def fetch_photo_for_device(device_name: str):
    """Automatically fetch a product photo for any Bluetooth device.

    First checks if a cached photo already exists.  If not, searches
    DuckDuckGo for a product image, downloads it, and caches it locally
    in the device_photos/ folder for future use.

    Users can also manually place images in device_photos/ — any file
    named <slugified-device-name>.<png|jpg|webp> will be picked up
    automatically.
    """
    if not device_name or get_photo_path(device_name):
        return
    with _photo_fetch_lock:
        if get_photo_path(device_name):
            return
        url = _search_device_image_url(device_name)
        if not url:
            return
        ext = "jpg" if (".jpg" in url or ".jpeg" in url) else "webp" if ".webp" in url else "png"
        dest = PHOTOS_DIR / f"{slug(device_name)}.{ext}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CodecMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read(2 * 1024 * 1024)
            if len(data) > 500:
                dest.write_bytes(data)
                print(f"  Photo cached: {dest.name} ({len(data)} bytes)")
        except Exception as e:
            print(f"  Photo fetch failed for {device_name}: {e}")


def prefetch_photos():
    """Download photos for all known devices at startup — not just Alt A2DP-
    paired ones, so the Devices page doesn't show generic icons for devices
    that just haven't happened to be the active one yet."""
    names = get_all_known_device_names()
    for name in names:
        if not get_photo_path(name):
            print(f"  Pre-fetching photo for {name}...")
            fetch_photo_for_device(name)


# ---------- History (in-memory + SQLite) + Alerts ----------

def init_history_db():
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                t REAL NOT NULL,
                device TEXT,
                mac TEXT,
                codec TEXT,
                bitrate INTEGER,
                battery INTEGER,
                type TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_t ON history(t)")
        conn.commit()
    finally:
        conn.close()


def add_history_point(snap: dict):
    point = {
        "t": snap["server_epoch"],
        "codec": snap["codec"]["name"],
        "bitrate": snap["codec"].get("bitrate_kbps"),
        "device": snap["device"]["name"] if snap["device"] else None,
        "mac": snap["device"].get("mac") if snap["device"] else None,
        "battery": snap["device"].get("battery") if snap["device"] else None,
        "type": snap["device"]["type"] if snap["device"] else None,
    }
    with _history_lock:
        _history.append(point)
    with _pending_history_lock:
        _pending_history_rows.append(point)


def get_history():
    with _history_lock:
        return list(_history)


def load_recent_history_into_memory():
    """On startup, pull recent rows from SQLite so the timeline isn't empty."""
    since = time.time() - HISTORY_LOAD_HOURS * 3600
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT t, device, mac, codec, bitrate, battery, type FROM history "
            "WHERE t >= ? ORDER BY t ASC LIMIT ?",
            (since, MAX_HISTORY),
        ).fetchall()
    finally:
        conn.close()
    with _history_lock:
        for t, device, mac, codec, bitrate, battery, dtype in rows:
            _history.append({
                "t": t, "device": device, "mac": mac, "codec": codec,
                "bitrate": bitrate, "battery": battery, "type": dtype,
            })
    if rows:
        print(f"  Loaded {len(rows)} history points from disk")


def flush_history_to_db():
    """Write any pending in-memory history points to SQLite. Call periodically."""
    with _pending_history_lock:
        if not _pending_history_rows:
            return
        rows = list(_pending_history_rows)
        _pending_history_rows.clear()
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.executemany(
            "INSERT INTO history (t, device, mac, codec, bitrate, battery, type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r["t"], r["device"], r["mac"], r["codec"], r["bitrate"], r["battery"], r["type"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def prune_history_db():
    retention_days = get_settings()["history_retention_days"]
    cutoff = time.time() - retention_days * 86400
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.execute("DELETE FROM history WHERE t < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def export_history_csv(mac: str | None = None, since_epoch: float | None = None) -> str:
    import csv
    import io as _io
    rows = query_history_rows(mac=mac, since_epoch=since_epoch)
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "device", "mac", "codec", "bitrate_kbps", "battery", "type"])
    for r in rows:
        writer.writerow([
            datetime.fromtimestamp(r["t"]).isoformat(timespec="seconds"),
            r["device"], r["mac"], r["codec"], r["bitrate"], r["battery"], r["type"],
        ])
    return buf.getvalue()


def export_history_markdown(mac: str | None = None, since_epoch: float | None = None) -> str:
    rows = query_history_rows(mac=mac, since_epoch=since_epoch)
    stats = compute_bitrate_stats(mac=mac, since_epoch=since_epoch)
    lines = [
        "# Codec Monitor report",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Samples: {len(rows)}",
        f"Bitrate — min {stats['min']} kbps, avg {stats['avg']} kbps, max {stats['max']} kbps" if stats["count"] else "Bitrate — no data in range",
        "",
        "| Timestamp | Device | Codec | Bitrate (kbps) | Battery | Type |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        ts = datetime.fromtimestamp(r["t"]).isoformat(timespec="seconds")
        lines.append(f"| {ts} | {r['device'] or ''} | {r['codec'] or ''} | {r['bitrate'] if r['bitrate'] is not None else ''} | {r['battery'] if r['battery'] is not None else ''} | {r['type'] or ''} |")
    return "\n".join(lines) + "\n"


def export_history_pdf(mac: str | None = None, since_epoch: float | None = None) -> bytes:
    from fpdf import FPDF

    rows = query_history_rows(mac=mac, since_epoch=since_epoch)
    stats = compute_bitrate_stats(mac=mac, since_epoch=since_epoch)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Codec Monitor report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Generated: {datetime.now().isoformat(timespec='seconds')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Samples: {len(rows)}", new_x="LMARGIN", new_y="NEXT")
    if stats["count"]:
        pdf.cell(0, 6, f"Bitrate: min {stats['min']} / avg {stats['avg']} / max {stats['max']} kbps", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    col_widths = [38, 40, 22, 28, 20, 24]
    headers = ["Timestamp", "Device", "Codec", "Bitrate", "Battery", "Type"]
    pdf.set_font("Helvetica", "B", 9)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 7, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for r in rows:
        ts = datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d %H:%M:%S")
        values = [
            ts, str(r["device"] or "")[:22], str(r["codec"] or ""),
            str(r["bitrate"]) if r["bitrate"] is not None else "",
            str(r["battery"]) if r["battery"] is not None else "",
            str(r["type"] or ""),
        ]
        for w, v in zip(col_widths, values):
            pdf.cell(w, 6, v, border=1)
        pdf.ln()
    return bytes(pdf.output())


def query_history_rows(mac: str | None = None, since_epoch: float | None = None) -> list[dict]:
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        query = "SELECT t, device, mac, codec, bitrate, battery, type FROM history WHERE 1=1"
        params = []
        if mac:
            query += " AND mac = ?"
            params.append(mac)
        if since_epoch:
            query += " AND t >= ?"
            params.append(since_epoch)
        query += " ORDER BY t ASC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [
        {"t": t, "device": device, "mac": devmac, "codec": codec, "bitrate": bitrate, "battery": battery, "type": dtype}
        for t, device, devmac, codec, bitrate, battery, dtype in rows
    ]


def compute_bitrate_stats(mac: str | None = None, since_epoch: float | None = None) -> dict:
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        query = "SELECT MIN(bitrate), AVG(bitrate), MAX(bitrate), COUNT(*) FROM history WHERE bitrate IS NOT NULL"
        params = []
        if mac:
            query += " AND mac = ?"
            params.append(mac)
        if since_epoch:
            query += " AND t >= ?"
            params.append(since_epoch)
        row = conn.execute(query, params).fetchone()
    finally:
        conn.close()
    mn, avg, mx, count = row
    return {
        "min": mn, "avg": round(avg) if avg is not None else None, "max": mx, "count": count,
    }


def _history_maintenance_loop():
    last_prune = 0
    while True:
        time.sleep(5)
        flush_history_to_db()
        if time.time() - last_prune > 6 * 3600:
            prune_history_db()
            last_prune = time.time()


def check_alerts(snap: dict):
    """Debounced: a transition only fires an alert once it's been seen on
    DEBOUNCE_POLLS consecutive polls. Prevents a single physical event (e.g. a
    disconnect) from firing many duplicate alerts while Windows' own device/
    endpoint enumeration flickers through intermediate states for a few polls.
    The live dashboard is unaffected — it always shows the current snap directly.
    """
    global _prev_codec, _prev_device
    global _pending_device, _pending_device_count, _pending_codec, _pending_codec_count
    now = time.time()
    codec_name = snap["codec"]["name"]
    device_name = snap["device"]["name"] if snap["device"] else None

    alerts = []

    if device_name == _prev_device:
        _pending_device, _pending_device_count = None, 0
    else:
        if device_name == _pending_device:
            _pending_device_count += 1
        else:
            _pending_device, _pending_device_count = device_name, 1
        if _pending_device_count >= DEBOUNCE_POLLS:
            if _prev_device is _UNSET:
                pass  # first-ever observation — record it, but nothing actually "changed"
            elif device_name is None:
                alerts.append({"time": now, "type": "disconnect", "msg": f"{_prev_device} disconnected"})
            elif _prev_device is None:
                alerts.append({"time": now, "type": "connect", "msg": f"{device_name} connected"})
            else:
                alerts.append({"time": now, "type": "switch", "msg": f"Switched from {_prev_device} to {device_name}"})
            _prev_device = device_name
            _pending_device, _pending_device_count = None, 0

    if codec_name == _prev_codec:
        _pending_codec, _pending_codec_count = None, 0
    else:
        if codec_name == _pending_codec:
            _pending_codec_count += 1
        else:
            _pending_codec, _pending_codec_count = codec_name, 1
        if _pending_codec_count >= DEBOUNCE_POLLS:
            # Skip if a device alert already fired this poll — a connect/disconnect/switch
            # always changes the reported codec too, but that's a side-effect, not news.
            if _prev_codec is not _UNSET and not alerts:
                codec_rank = {"PCM": 5, "LDAC": 4, "aptX HD": 3, "aptX": 2, "AAC": 1, "SBC": 0}
                old_rank = codec_rank.get(_prev_codec, -1)
                new_rank = codec_rank.get(codec_name, -1)
                if new_rank < old_rank:
                    alerts.append({"time": now, "type": "downgrade", "msg": f"Codec downgraded: {_prev_codec} → {codec_name}"})
                elif new_rank > old_rank:
                    alerts.append({"time": now, "type": "upgrade", "msg": f"Codec upgraded: {_prev_codec} → {codec_name}"})
                else:
                    alerts.append({"time": now, "type": "codec_change", "msg": f"Codec changed: {_prev_codec} → {codec_name}"})
            _prev_codec = codec_name
            _pending_codec, _pending_codec_count = None, 0

    if alerts:
        with _alerts_lock:
            for a in alerts:
                _alerts.append(a)
        with _stability_lock:
            for a in alerts:
                if a["type"] in ("disconnect", "downgrade"):
                    _stability_events.append(a["time"])
        if get_settings()["notifications_enabled"] and not is_window_visible():
            for a in alerts:
                send_native_notification(a)
    return alerts


def compute_connection_stability(is_bluetooth: bool) -> dict | None:
    """Stability label derived from recent disconnects/downgrades.

    Windows has no API to read RSSI for an already-connected classic
    Bluetooth audio device, so this is an honest proxy built from data we
    actually have, not a fabricated dBm number.
    """
    if not is_bluetooth:
        return None
    now = time.time()
    with _stability_lock:
        recent = [t for t in _stability_events if now - t <= 600]
    n = len(recent)
    label = "Stable" if n == 0 else "Occasional drops" if n <= 2 else "Unstable"
    return {"label": label, "events_10min": n}


_window_visible = True
_window_visible_lock = threading.Lock()
_visibility_checker = None  # optional callable set by app.py — see set_visibility_checker


def set_window_visible(visible: bool):
    """Called by app.py on window show/hide/minimize/restore events. Kept as a
    fallback — see is_window_visible() for why it's not the only source of truth."""
    global _window_visible
    with _window_visible_lock:
        _window_visible = visible


def set_visibility_checker(fn):
    """app.py registers a function here that queries the native window's
    WindowState/Visible directly (ctypes/WinForms), rather than trusting
    pywebview's shown/minimized/restored events alone — those didn't fire
    reliably enough in testing, causing native toasts to fire (or not fire)
    incorrectly. The checker returns True/False, or None if it can't tell
    (falls back to the event-tracked flag)."""
    global _visibility_checker
    _visibility_checker = fn


def is_window_visible() -> bool:
    if _visibility_checker is not None:
        try:
            result = _visibility_checker()
            if result is not None:
                return result
        except Exception:
            pass
    with _window_visible_lock:
        return _window_visible


def send_native_notification(alert: dict):
    def _fire():
        try:
            _win_toast("Codec Monitor", alert["msg"])
        except Exception:
            pass
    threading.Thread(target=_fire, daemon=True).start()


def get_alerts():
    with _alerts_lock:
        return list(_alerts)


def get_cached_snapshot():
    with _snapshot_lock:
        return _cached_snapshot


# ---------- PowerShell poller ----------

BT_BATTERY_LOOP_SCRIPT = r"""
$keyBattery = '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'

while ($true) {
    try {
        $allDevices = @(Get-PnpDevice -ErrorAction SilentlyContinue)
        $bt = @($allDevices | Where-Object {
            $_.Class -eq 'Bluetooth' -and $_.Status -eq 'OK' -and $_.FriendlyName -and
            $_.FriendlyName -notmatch 'Generic|Profile|^Bluetooth LE|Service|Enumerator|Transport|Avrcp|RFCOMM|Microsoft Bluetooth|Personal Area|Identification|Standard Serial|Wireless Bluetooth|Bluetooth Adapter|Bluetooth Radio|Bluetooth Module'
        })

        $rows = @()
        foreach ($d in $bt) {
            $mac = ''
            if ($d.InstanceId -match '([0-9A-Fa-f]{12})') { $mac = $Matches[1] }
            $relatedIds = @($d.InstanceId)
            $batt = $null
            try {
                $b = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName $keyBattery -ErrorAction Stop
                if ($b.Data -ne $null) { $batt = [int]$b.Data }
            } catch {}
            if ($mac) {
                # All PnP nodes sharing this MAC — not just the one matching our
                # FriendlyName filter. Windows' own "Connected" status can live on
                # a node with a decorated name (e.g. "... Hands-Free AG") that our
                # filter deliberately excludes from the display name list, but its
                # IsConnected property still needs checking (see
                # get_live_connected_status in monitor.py).
                $related = @($allDevices | Where-Object { $_.InstanceId -match $mac -and $_.InstanceId -ne $d.InstanceId })
                $relatedIds += $related.InstanceId
                if ($batt -eq $null -or $batt -eq 0) {
                    foreach ($r in $related) {
                        try {
                            $rb = Get-PnpDeviceProperty -InstanceId $r.InstanceId -KeyName $keyBattery -ErrorAction Stop
                            if ($rb.Data -ne $null -and $rb.Data -gt 0) { $batt = [int]$rb.Data; break }
                        } catch {}
                    }
                }
            }
            $rows += [PSCustomObject]@{
                Name               = $d.FriendlyName
                InstanceId         = $d.InstanceId
                RelatedInstanceIds = $relatedIds
                Battery            = $batt
            }
        }

        [Console]::WriteLine((@{ bluetooth = $rows } | ConvertTo-Json -Depth 5 -Compress))
    } catch {
        [Console]::WriteLine('{"bluetooth":[]}')
    }
    Start-Sleep -Milliseconds __POLL_MS__
}
"""

ENDPOINTS_LOOP_SCRIPT = r"""
while ($true) {
    try {
        $endpoints = @(Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
            Select-Object FriendlyName, Status)
        [Console]::WriteLine((@{ endpoints = $endpoints } | ConvertTo-Json -Depth 5 -Compress))
    } catch {
        [Console]::WriteLine('{"endpoints":[]}')
    }
    Start-Sleep -Milliseconds __POLL_MS__
}
"""


SLOW_LOOP_SLEEP_MS = 3000   # bt+battery cycle gap; each cycle itself can still take seconds
ENDPOINTS_LOOP_SLEEP_MS = 1500  # endpoints alone are cheap (~1-1.5s/call) — keep this snappy


def start_ps_poller():
    script = BT_BATTERY_LOOP_SCRIPT.replace("__POLL_MS__", str(SLOW_LOOP_SLEEP_MS))
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )
    _assign_process_to_job(proc)
    return proc


def start_endpoints_poller():
    script = ENDPOINTS_LOOP_SCRIPT.replace("__POLL_MS__", str(ENDPOINTS_LOOP_SLEEP_MS))
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )
    _assign_process_to_job(proc)
    return proc


# ---------- Data processing ----------

def extract_mac_raw(instance_id: str) -> str | None:
    """Extract raw 12-char hex MAC from instance ID, lowercased so it matches
    the registry-derived mac_raw form everywhere else (_normalize_mac_raw,
    read_alt_a2dp_current) — InstanceId strings have it uppercase, and the
    battery cache is a plain dict keyed by this string, so a case mismatch
    here means lookups silently miss forever."""
    m = re.search(r"([0-9A-Fa-f]{12})", instance_id or "")
    return m.group(1).lower() if m else None


def format_mac(mac_12: str) -> str:
    mac = mac_12.upper()
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


HEADPHONE_RE = re.compile(
    r"buds|headphones|headset|airpods|sony|wh-|jabra|bose|cmf|enco|realme|oppo|nothing|galaxy|pixel|beats",
    re.IGNORECASE,
)
MIC_RE = re.compile(r"microphone|mic array|stereo mix|line in", re.IGNORECASE)


def classify_endpoint(name: str, bt_device_names: list) -> str:
    low = name.lower()
    for bt_name in bt_device_names:
        if bt_name.lower() in low or low.split(" (")[0] in bt_name.lower():
            return "bluetooth"
    if HEADPHONE_RE.search(name):
        for bt_name in bt_device_names:
            if bt_name.lower() in low:
                return "bluetooth"
        if "headphone" in low or "headset" in low:
            return "headphones"
    if MIC_RE.search(name):
        return "microphone"
    if any(k in low for k in ("realtek", "speakers")):
        return "built-in"
    if any(k in low for k in ("hdmi", "displayport", "amd high", "nvidia")):
        return "hdmi"
    if any(k in low for k in ("usb",)):
        return "usb"
    return "other"


_alt_a2dp_installed = None

def build_snapshot_from_raw(raw: dict) -> dict:
    global _current_device_name, _device_connect_time, _alt_a2dp_installed

    if _alt_a2dp_installed is None:
        _alt_a2dp_installed = is_alt_a2dp_installed()

    bt_devices = raw.get("bluetooth", [])
    if not isinstance(bt_devices, list):
        bt_devices = [bt_devices] if bt_devices else []
    tracked = get_settings()["tracked_devices"]
    if tracked:
        bt_devices = [d for d in bt_devices if d.get("Name") in tracked]
    raw_endpoints = raw.get("endpoints", [])
    if not isinstance(raw_endpoints, list):
        raw_endpoints = [raw_endpoints] if raw_endpoints else []

    bt_names = [d.get("Name", "") for d in bt_devices if d.get("Name")]

    endpoints = []
    for ep in raw_endpoints:
        if isinstance(ep, dict):
            name = ep.get("FriendlyName", "")
            status = ep.get("Status", "Unknown")
        else:
            name = str(ep)
            status = "OK"
        if not name:
            continue
        ep_type = classify_endpoint(name, bt_names)
        endpoints.append({"name": name, "type": ep_type, "status": status})

    ok_outputs = [e for e in endpoints if e["status"] == "OK" and e["type"] != "microphone"]
    all_outputs = [e for e in endpoints if e["type"] != "microphone"]

    bt_endpoints = [e for e in ok_outputs if e["type"] == "bluetooth"]
    hp_endpoints = [e for e in ok_outputs if e["type"] == "headphones"]
    spk_endpoints = [e for e in ok_outputs if e["type"] in ("built-in", "hdmi", "usb", "other")]

    # Fast path: scan known Alt A2DP devices directly via the registry (sub-ms,
    # no PowerShell). This is what makes connect/disconnect/switch detection
    # fast regardless of how slow the PowerShell-based battery/endpoint
    # refresh (_cached_raw) happens to be — see find_active_alt_a2dp_device().
    fast_hit = find_active_alt_a2dp_device() if _alt_a2dp_installed else None
    if fast_hit and tracked and fast_hit["name"] not in tracked:
        fast_hit = None

    matched_bt = None
    mac_raw = None
    codec = None

    if fast_hit:
        mac_raw = fast_hit["mac_raw"]
        bt_name = fast_hit["name"]
        codec = fast_hit["codec"]
        matched_ep = next(
            (e for e in bt_endpoints if bt_name.lower() in e["name"].lower() or e["name"].split(" (")[0].lower() in bt_name.lower()),
            None,
        )
        active_ep = matched_ep or {"name": bt_name, "type": "bluetooth", "status": "OK"}
        matched_bt = {"Name": bt_name, "InstanceId": mac_raw, "Battery": get_cached_battery(mac_raw)}
    else:
        # Prefer matching the TRUE Windows default playback device (pycaw) over
        # just grabbing the first "OK" endpoint — multiple BT devices can show
        # Status=OK simultaneously (see get_currently_connected_bt_devices),
        # so "first one in the list" isn't reliably "the one actually playing".
        default_name = get_default_playback_device_name()
        candidates = bt_endpoints + hp_endpoints + spk_endpoints
        active_ep = None
        if default_name:
            active_ep = next(
                (e for e in candidates if e["name"].lower() in default_name.lower() or default_name.lower() in e["name"].lower()),
                None,
            )
        if active_ep is None:
            active_ep = (bt_endpoints or hp_endpoints or spk_endpoints or [None])[0]

        if active_ep and active_ep["type"] == "bluetooth":
            for d in bt_devices:
                dev_name = d.get("Name", "")
                if dev_name and dev_name.lower() in active_ep["name"].lower():
                    matched_bt = d
                    break
                ep_base = active_ep["name"].split(" (")[0].lower()
                if dev_name and ep_base in dev_name.lower():
                    matched_bt = d
                    break

        # Trust Alt A2DP's live registry over Windows' PnP/endpoint status, which can take
        # tens of seconds to notice a real disconnect (Bluetooth supervision timeout).
        # (fast_hit already covers this for any currently-Opened device — this is the
        # safety net for when _cached_raw is stale and matched_bt is a known mac too.)
        if matched_bt and _alt_a2dp_installed:
            stale_mac = extract_mac_raw(matched_bt.get("InstanceId", ""))
            if stale_mac and alt_a2dp_device_opened(stale_mac) is False:
                matched_bt = None
                bt_endpoints = [e for e in bt_endpoints if e["name"] != active_ep["name"]]
                active_ep = (bt_endpoints or hp_endpoints or spk_endpoints or [None])[0]

        if matched_bt:
            mac_raw = extract_mac_raw(matched_bt.get("InstanceId", ""))

    device_name = active_ep["name"] if active_ep else None
    if device_name != _current_device_name:
        _current_device_name = device_name
        _device_connect_time = time.time()

    if matched_bt:
        bt_name = matched_bt["Name"]
        fetch_photo_for_device(bt_name)
        photo_url = get_photo_path(bt_name)
        device = {
            "name": bt_name,
            "type": "bluetooth",
            "mac": format_mac(mac_raw) if mac_raw else None,
            "battery": matched_bt.get("Battery"),
            "connected": True,
            "connect_epoch": _device_connect_time,
            "photo": photo_url,
        }
    elif active_ep:
        device = {
            "name": active_ep["name"],
            "type": active_ep["type"],
            "mac": None,
            "battery": None,
            "connected": True,
            "connect_epoch": _device_connect_time,
            "photo": None,
        }
    else:
        device = None

    # --- Real codec detection (already resolved by the fast path above if found) ---
    if device and device["type"] == "bluetooth" and codec is None and mac_raw and _alt_a2dp_installed:
        codec = read_alt_a2dp_current(mac_raw)
    if device and device["type"] == "bluetooth" and codec is None:
        codec = {"name": "SBC", "bitrate_kbps": 328, "sample_rate_khz": 44.1, "bit_depth": 16, "driver": "Windows Standard"}
    if codec is None:
        codec = {"name": "PCM", "bitrate_kbps": None, "sample_rate_khz": 48, "bit_depth": 16, "driver": "System"}

    output_list = []
    for e in all_outputs:
        is_active = active_ep is not None and e["name"] == active_ep["name"]
        output_list.append({"name": e["name"], "type": e["type"], "status": e["status"], "active": is_active})
    for e in endpoints:
        if e["type"] == "microphone" and e["status"] == "OK":
            output_list.append({"name": e["name"], "type": "microphone", "status": e["status"], "active": False})

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "server_epoch": time.time(),
        "device": device,
        "codec": codec,
        "alt_a2dp_installed": _alt_a2dp_installed,
        "connection_stability": compute_connection_stability(bool(device and device["type"] == "bluetooth")),
        "outputs": output_list,
    }


# ---------- Poller thread ----------

_ps_proc = None
_ps_proc_lock = threading.Lock()
_endpoints_proc = None
_endpoints_proc_lock = threading.Lock()


_shutting_down = threading.Event()


def force_refresh():
    """Kill the current PowerShell pollers so they immediately respawn."""
    with _ps_proc_lock:
        proc = _ps_proc
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    with _endpoints_proc_lock:
        proc = _endpoints_proc
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass


def shutdown():
    """Stop the loops from respawning their PowerShell pollers and kill the
    current ones. Called on a clean Quit, before the process exits — the Job
    Object in _assign_process_to_job is the backstop for unclean exits."""
    _shutting_down.set()
    force_refresh()


def slow_loop():
    """Background-only: refreshes _cached_raw (bt_devices/battery) from
    PowerShell. Never touches _cached_snapshot directly — Get-PnpDeviceProperty
    (battery) measured at ~1.2s/call on real hardware, so this can take
    anywhere from ~2s to ~60s per cycle. fast_loop() is what keeps the UI
    responsive; endpoints_loop() handles non-BT outputs on its own fast cadence.
    """
    global _ps_proc
    while not _shutting_down.is_set():
        proc = start_ps_poller()
        with _ps_proc_lock:
            _ps_proc = proc
        for line in proc.stdout:
            if _shutting_down.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            with _cached_raw_lock:
                _cached_raw["bluetooth"] = raw.get("bluetooth", [])
            bt_list = raw.get("bluetooth", [])
            if not isinstance(bt_list, list):
                bt_list = [bt_list] if bt_list else []
            for d in bt_list:
                instance_id = d.get("InstanceId", "")
                name = d.get("Name")
                if name:
                    related_ids = d.get("RelatedInstanceIds") or ([instance_id] if instance_id else [])
                    if not isinstance(related_ids, list):
                        related_ids = [related_ids]
                    for iid in related_ids:
                        if iid:
                            set_cached_instance_id(_clean_device_name(name), iid)
                batt = d.get("Battery")
                if batt:
                    mac_raw = extract_mac_raw(instance_id)
                    if mac_raw:
                        set_cached_battery(mac_raw, batt)
        # proc's stdout closed (terminated via force_refresh, or it crashed) — respawn.


def endpoints_loop():
    """Background-only: refreshes _cached_endpoints from PowerShell, on its own
    fast cadence, independent of the slow BT+battery loop — these don't need
    battery lookups, so there's no reason to gate built-in/wired output
    detection behind that loop's much higher per-cycle cost."""
    global _endpoints_proc
    while not _shutting_down.is_set():
        proc = start_endpoints_poller()
        with _endpoints_proc_lock:
            _endpoints_proc = proc
        for line in proc.stdout:
            if _shutting_down.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            with _cached_endpoints_lock:
                _cached_endpoints[:] = raw.get("endpoints", [])
        # proc's stdout closed — respawn.


def fast_loop():
    """Recomputes the snapshot on a short, fixed cadence using whatever's in
    _cached_raw/_cached_endpoints right now (however stale) plus always-fresh
    registry reads (codec, Alt A2DP Opened flag) — this is what makes connect/
    disconnect/codec changes show up in well under a second instead of waiting
    on slow_loop.
    """
    global _cached_snapshot
    comtypes.CoInitialize()  # this thread calls pycaw (Core Audio API) every tick
    while True:
        with _cached_raw_lock:
            bt = list(_cached_raw["bluetooth"])
        with _cached_endpoints_lock:
            endpoints = list(_cached_endpoints)
        raw = {"bluetooth": bt, "endpoints": endpoints}
        snap = build_snapshot_from_raw(raw)
        new_alerts = check_alerts(snap)
        add_history_point(snap)
        with _snapshot_lock:
            _cached_snapshot = (snap, new_alerts)
        time.sleep(get_settings()["poll_interval_ms"] / 1000)
        # proc's stdout closed (terminated via force_refresh, or it crashed) — respawn.


# ---------- HTTP server (serves frontend + photos) ----------

class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/settings":
            body = json.dumps(get_settings()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/devices":
            body = json.dumps(list_known_devices()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sysinfo":
            info = {
                "version": APP_VERSION,
                "data_dir": str(DATA_DIR),
                "ports": {"http": PORT_HTTP, "ws": PORT_WS},
                "frozen": bool(getattr(sys, "frozen", False)),
                "alt_a2dp_installed": is_alt_a2dp_installed(),
                "settings_path": str(SETTINGS_PATH),
                "history_db_path": str(HISTORY_DB_PATH),
            }
            body = json.dumps(info).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/history") or self.path.startswith("/stats") or self.path.startswith("/export.csv"):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            mac = qs.get("mac", [None])[0]
            since_hours = qs.get("since_hours", [None])[0]
            since_epoch = time.time() - float(since_hours) * 3600 if since_hours else None

            if parsed.path == "/history":
                rows = query_history_rows(mac=mac, since_epoch=since_epoch)
                body = json.dumps(rows).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/export.csv":
                csv_text = export_history_csv(mac=mac, since_epoch=since_epoch)
                body = csv_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="codec_monitor_history.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            stats = compute_bitrate_stats(mac=mac, since_epoch=since_epoch)
            body = json.dumps(stats).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/photos/"):
            fname = self.path[len("/photos/"):]
            fpath = PHOTOS_DIR / fname
            if fpath.exists() and fpath.is_file():
                self.send_response(200)
                ext = fpath.suffix.lower()
                ct = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(fpath.stat().st_size))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(fpath.read_bytes())
                return
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/refresh":
            force_refresh()
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/open-sound-settings":
            try:
                os.startfile("ms-settings:sound")
            except Exception:
                pass
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/settings":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_error(400)
                return
            merged = save_settings(payload)
            body = json.dumps(merged).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *a):
        return


def run_http_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT_HTTP), _Handler) as httpd:
        httpd.serve_forever()


# ---------- WebSocket ----------

async def ws_handler(websocket):
    await websocket.send(json.dumps({"type": "education", "data": CODEC_INFO}))
    await websocket.send(json.dumps({"type": "history", "data": get_history()}))
    past_alerts = get_alerts()
    if past_alerts:
        await websocket.send(json.dumps({"type": "alerts_history", "data": past_alerts}))

    with _snapshot_lock:
        cached = _cached_snapshot
    if cached:
        snap, _ = cached
        await websocket.send(json.dumps({"type": "snapshot", "data": snap}))

    last_json = ""
    try:
        while True:
            with _snapshot_lock:
                cached = _cached_snapshot
            if cached:
                snap, new_alerts = cached
                j = json.dumps({"type": "snapshot", "data": snap})
                if j != last_json:
                    await websocket.send(j)
                    if new_alerts:
                        await websocket.send(json.dumps({"type": "alerts", "data": new_alerts}))
                    last_json = j
            await asyncio.sleep(0.3)
    except websockets.exceptions.ConnectionClosed:
        return


async def run_ws_server():
    async with websockets.serve(ws_handler, "127.0.0.1", PORT_WS):
        await asyncio.Future()


def start_backend():
    """Start photo prefetch, poll loop, and HTTP server in background threads.

    Does not start the WebSocket server — callers run that themselves
    (asyncio.run(run_ws_server()) blocks, so app.py runs it on its own thread).
    """
    print("Codec Monitor backend v5 starting...")
    load_settings()
    init_history_db()
    load_recent_history_into_memory()
    # HTTP server starts first and on its own thread so the window has something
    # to load immediately — photo prefetch is a nice-to-have, not a blocker.
    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=fast_loop, daemon=True).start()
    threading.Thread(target=slow_loop, daemon=True).start()
    threading.Thread(target=endpoints_loop, daemon=True).start()
    threading.Thread(target=_history_maintenance_loop, daemon=True).start()
    threading.Thread(target=prefetch_photos, daemon=True).start()
    print(f"  Dashboard: http://localhost:{PORT_HTTP}/")
    print(f"  Live data: ws://localhost:{PORT_WS}/")


def main():
    start_backend()
    print("  Press Ctrl+C to stop")
    try:
        asyncio.run(run_ws_server())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
