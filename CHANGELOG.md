# Changelog

All notable changes to Codec Monitor will be documented in this file.

## [1.0.0] - 2026-06-24

### Features
- Real-time Bluetooth codec detection (SBC, AAC, aptX, aptX HD, LDAC) via Alt A2DP Driver registry
- Live dashboard with bitrate, sample rate, bit depth, battery, and connection uptime
- Session timeline chart (Chart.js) with configurable time ranges
- Connection stability tracking (disconnects and codec downgrades)
- Device management page with live connection status via Win32 CfgMgr32 API
- Codec comparison page with educational breakdowns
- Debounced alert system with toast notifications
- Export to CSV, Markdown, and PDF
- System tray integration (minimize to tray, background monitoring)
- Light and dark theme support
- Automatic device photo fetching via DuckDuckGo image search
- Settings page (poll interval, retention, notifications, tracked devices, close behavior)
- Single-instance enforcement with bring-to-front on re-launch
- PyInstaller packaging for standalone exe
- Inno Setup installer script
