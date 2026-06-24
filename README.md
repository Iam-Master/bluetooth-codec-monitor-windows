# Codec Monitor

Real-time Bluetooth audio codec monitor for Windows. See exactly which codec (SBC, AAC, aptX, aptX HD, LDAC), bitrate, sample rate, and bit depth your Bluetooth headphones are actually using — read directly from the system, not guessed.

![Windows 10/11](https://img.shields.io/badge/Windows-10%2F11-0078D6?logo=windows&logoColor=white)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

## Features

- **Real codec detection** — reads the actual negotiated codec from the Alternative A2DP Driver registry (no hardcoded values)
- **Live dashboard** — bitrate, sample rate, bit depth, battery, connection uptime, all updating in real time
- **Session timeline** — interactive Chart.js graph of bitrate over time with configurable ranges
- **Connection stability** — tracks disconnects and codec downgrades (Windows has no RSSI API for connected classic BT devices, so this is an honest proxy)
- **Device management** — shows all known Bluetooth devices with live connection status via direct Win32 API calls (sub-millisecond, no PowerShell)
- **Codec comparison** — educational breakdown of SBC, AAC, aptX, aptX HD, and LDAC with interactive bars
- **Smart alerts** — debounced notifications for codec upgrades/downgrades, connects/disconnects
- **Export reports** — CSV, Markdown, and PDF export of session history
- **System tray** — minimizes to tray, keeps monitoring in the background
- **Light & dark themes** — toggle between light and dark mode
- **Auto device photos** — automatically fetches product images for connected devices

## Requirements

- **Windows 10 or 11**
- **Python 3.10+** (for running from source)
- **[Alternative A2DP Driver](https://bluetoothgoodies.com/a2dp/)** (optional but recommended — unlocks LDAC/aptX HD codecs; without it, Windows only supports SBC)

## Quick Start

### Option 1: Run from Source

1. Clone this repository:
   ```bash
   git clone https://github.com/user/codec-monitor.git
   cd codec-monitor
   ```

2. Double-click `start.bat` — it will:
   - Create a Python virtual environment (first run only)
   - Install dependencies
   - Launch the app

### Option 2: Build Standalone Exe

1. Run `start.bat` first (sets up the venv)
2. Double-click `build.bat` — it will:
   - Build a single-file exe via PyInstaller
   - Optionally build a Windows installer via Inno Setup (if installed)
   - Output to `dist/`

## Project Structure

```
codec-monitor/
├── backend/
│   ├── app.py              # Desktop app shell (pywebview + pystray)
│   ├── monitor.py          # Core backend: codec detection, polling, HTTP/WS servers
│   ├── codec_info.json     # Educational content for the Codecs page
│   ├── codec_monitor.spec  # PyInstaller build spec
│   ├── requirements.txt    # Python dependencies
│   └── requirements-dev.txt
├── frontend/
│   ├── index.html          # Single-page app UI
│   ├── app.js              # Frontend logic, WebSocket client, Chart.js
│   └── style.css           # Full design system with light/dark themes
├── start.bat               # One-click launcher
├── build.bat               # Build standalone exe + installer
├── installer.iss           # Inno Setup installer script
├── LICENSE
├── CONTRIBUTING.md
└── CHANGELOG.md
```

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  pywebview Window (app.py)                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Frontend (HTML/JS/CSS)                           │  │
│  │  ← WebSocket (port 8766) ← live snapshots         │  │
│  │  ← HTTP (port 8765) ← static files + REST API    │  │
│  └───────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────┤
│  Backend (monitor.py)                                   │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────────┐  │
│  │ Fast Loop│ │ Slow Loop│ │ Endpoints Loop         │  │
│  │ (800ms)  │ │ (3s+)   │ │ (1.5s)                 │  │
│  │ Registry │ │ PS+Batt │ │ AudioEndpoint          │  │
│  │ + pycaw  │ │ via PS  │ │ via PS                 │  │
│  └──────────┘ └──────────┘ └────────────────────────┘  │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Win32 CfgMgr32 API — instant device connection   │   │
│  │ status (no PowerShell)                            │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

- **Fast loop** (~800ms): Reads codec/bitrate/sample-rate directly from the Alt A2DP Driver registry + checks the Windows default playback device via pycaw. This is what makes codec changes and device switches show up in under a second.
- **Slow loop** (~3s+): PowerShell-based battery lookups via `Get-PnpDeviceProperty` (~1.2s per device). Results are cached and read by the fast loop.
- **Endpoints loop** (~1.5s): Enumerates all audio endpoints (speakers, HDMI, USB) independently of the slow BT loop.
- **Win32 direct calls**: Device connection status uses `CfgMgr32` via ctypes — instant, no subprocess overhead.

## Configuration

Settings are stored in `%APPDATA%\CodecMonitor\settings.json` (packaged exe) or `backend/settings.json` (source). Configurable options:

| Setting | Default | Description |
|---|---|---|
| `poll_interval_ms` | 800 | How often the fast loop polls (milliseconds) |
| `history_retention_days` | 14 | How long to keep history in the database |
| `notifications_enabled` | true | Show Windows toast notifications for events |
| `start_minimized` | false | Start minimized to the system tray |
| `close_action` | "minimize" | What happens when you close the window: `"minimize"` (to tray) or `"quit"` |
| `tracked_devices` | [] | Only monitor these device names (empty = all) |

## Device Photos

Codec Monitor automatically fetches product images for connected Bluetooth devices using DuckDuckGo image search. Photos are cached in `device_photos/` and reused on subsequent launches.

You can also manually place images in `device_photos/` — name them `<slugified-device-name>.<png|jpg|webp>` (e.g., `sony_wh_1000xm4.png`).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.
