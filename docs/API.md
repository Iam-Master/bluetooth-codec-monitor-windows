# Local API Reference

Codec Monitor runs two local servers inside the backend (`backend/monitor.py`):

| Server | Bind address | Default port | Purpose |
|---|---|---|---|
| HTTP | `127.0.0.1` | `8765` | Serves the frontend (static files), the REST endpoints, and cached device photos |
| WebSocket | `127.0.0.1` | `8766` | Pushes live snapshots, alerts, history, and codec reference data to the UI |

> **Localhost only.** Both servers bind to `127.0.0.1` (loopback) — they are not exposed on the network and are intended solely for the bundled desktop UI (pywebview). There is no authentication because the surface is not reachable from other machines.
>
> **CSRF / same-origin check.** All `POST` endpoints reject cross-origin requests. If an `Origin` or `Referer` header is present, it must parse to `http` on host `127.0.0.1` or `localhost` with port `8765`; otherwise the request is rejected with `403`. Requests with no `Origin`/`Referer` (such as pywebview's own same-origin calls) are allowed. The WebSocket server applies a similar origin check on connect (see below).

---

## HTTP API (`http://127.0.0.1:8765`)

### Static files

**`GET /` and any other unmatched path**

- **Purpose:** Serves the frontend from the `frontend/` directory (`index.html`, `app.js`, `style.css`, etc.) via Python's `SimpleHTTPRequestHandler`. `GET /` returns `index.html`.
- **Query params:** None.
- **Response:** The requested static file. `Content-Type` is inferred by the static handler from the file extension.
- **Status codes:** `200` if the file exists; `404` if it does not.

---

### `GET /settings`

- **Purpose:** Return the current effective settings.
- **Query params:** None.
- **Response content-type:** `application/json`
- **Response shape:**
  ```json
  {
    "poll_interval_ms": 800,
    "history_retention_days": 14,
    "notifications_enabled": true,
    "start_minimized": false,
    "tracked_devices": [],
    "close_action": "minimize"
  }
  ```
- **Status codes:** `200`

---

### `GET /devices`

- **Purpose:** List all known Bluetooth devices (paired devices, devices seen in history, plus the currently active device) with their live connection status, battery, and photo.
- **Query params:** None.
- **Response content-type:** `application/json`
- **Response shape:** An array of device objects:
  ```json
  [
    {
      "name": "Sony WH-1000XM4",
      "photo": "/photos/sony_wh_1000xm4.jpg",
      "is_active": true,
      "is_connected": true,
      "mac": "AA:BB:CC:DD:EE:FF",
      "battery": 80,
      "codec": {
        "name": "LDAC",
        "bitrate_kbps": 990,
        "sample_rate_khz": 96.0,
        "bit_depth": 24,
        "driver": "Alternative A2DP Driver"
      }
    }
  ]
  ```
  - `photo`, `mac`, and `battery` may be `null`.
  - `codec` is the full codec object **only** for the currently active device; for other devices it is `null`.
- **Status codes:** `200`

---

### `GET /sysinfo`

- **Purpose:** Return runtime/environment information about the running instance.
- **Query params:** None.
- **Response content-type:** `application/json`
- **Response shape:**
  ```json
  {
    "version": "1.1.0",
    "data_dir": "%APPDATA%/CodecMonitor",
    "ports": { "http": 8765, "ws": 8766 },
    "frozen": false,
    "alt_a2dp_installed": true,
    "settings_path": "%APPDATA%/CodecMonitor/settings.json",
    "history_db_path": "%APPDATA%/CodecMonitor/history.db"
  }
  ```
  - `frozen` is `true` when running as a PyInstaller-packaged exe.
  - `alt_a2dp_installed` reflects whether the Alternative A2DP Driver was detected.
  - File-system paths are sanitized to `%APPDATA%`/`%USERPROFILE%`-relative form where applicable.
- **Status codes:** `200`

---

### `GET /history`

- **Purpose:** Return recorded history points (codec/bitrate/battery over time) for the session timeline.
- **Query params (both optional):**
  - `mac` — filter to a single device by MAC address.
  - `since_hours` — only return points newer than this many hours ago (float).
- **Response content-type:** `application/json`
- **Response shape:** An array of history points, ordered oldest-first:
  ```json
  [
    {
      "t": 1750000000.0,
      "device": "Sony WH-1000XM4",
      "mac": "AA:BB:CC:DD:EE:FF",
      "codec": "LDAC",
      "bitrate": 990,
      "battery": 80,
      "type": "bluetooth"
    }
  ]
  ```
  - `device`, `mac`, `bitrate`, `battery`, and `type` may be `null`.
- **Status codes:** `200`; `400` if `since_hours` is not a valid number.

---

### `GET /stats`

- **Purpose:** Return aggregate bitrate statistics over the recorded history.
- **Query params (both optional):**
  - `mac` — filter to a single device by MAC address.
  - `since_hours` — only consider points newer than this many hours ago (float).
- **Response content-type:** `application/json`
- **Response shape:**
  ```json
  { "min": 328, "avg": 712, "max": 990, "count": 1234 }
  ```
  - All values may be `null` when no matching rows with a bitrate exist. `avg` is rounded to an integer.
- **Status codes:** `200`; `400` if `since_hours` is not a valid number.

---

### `GET /export.csv`

- **Purpose:** Download the recorded history as a CSV file.
- **Query params (both optional):**
  - `mac` — filter to a single device by MAC address.
  - `since_hours` — only export points newer than this many hours ago (float).
- **Response content-type:** `text/csv`
- **Response headers:** `Content-Disposition: attachment; filename="codec_monitor_history.csv"`
- **Response shape:** CSV text of the history rows.
- **Status codes:** `200`; `400` if `since_hours` is not a valid number.

---

### `GET /photos/<filename>`

- **Purpose:** Serve a cached device product image from the `device_photos/` directory.
- **Path parameter:** `<filename>` — the (URL-encoded) image file name, e.g. `sony_wh_1000xm4.jpg`.
- **Query params:** None (any `?`/`#` suffix is stripped).
- **Response content-type:** `image/png`, `image/jpeg`, or `image/webp` based on the extension (otherwise `application/octet-stream`).
- **Response headers:** `Cache-Control: max-age=86400`.
- **Status codes:** `200` if the file exists; `404` if it does not exist or if the resolved path escapes the photos directory (path-traversal guard).

---

### `POST /refresh`

- **Purpose:** Force an immediate re-poll (restarts the background PowerShell pollers so fresh data is gathered without waiting for the next cycle).
- **Same-origin check:** Yes (see CSRF note above).
- **Request body:** None.
- **Response:** Empty.
- **Status codes:** `204` No Content; `403` if the origin check fails.

---

### `POST /open-sound-settings`

- **Purpose:** Open the Windows Sound settings page (`ms-settings:sound`) on the host machine.
- **Same-origin check:** Yes.
- **Request body:** None.
- **Response:** Empty.
- **Status codes:** `204` No Content; `403` if the origin check fails.

---

### `POST /settings`

- **Purpose:** Update one or more settings. Only keys that already exist in the default settings are accepted; unknown keys are ignored. The updated settings are merged and persisted to `settings.json`.
- **Same-origin check:** Yes.
- **Request body:** `application/json` — a partial or full settings object, e.g.:
  ```json
  { "notifications_enabled": false, "poll_interval_ms": 1000 }
  ```
  Maximum body size: **64 KB**.
- **Response content-type:** `application/json` — the full merged settings object (same shape as `GET /settings`).
- **Status codes:** `200` on success; `400` if the body is not valid JSON; `413` if the body exceeds 64 KB; `403` if the origin check fails.

---

### Unmatched `POST`

Any `POST` to a path other than the three above returns `404` (after passing the origin check).

---

## WebSocket API (`ws://127.0.0.1:8766`)

The WebSocket connection is **one-directional for application data**: the server pushes messages to the client, and the client is not expected to send application messages back (the server does not process inbound messages).

**Origin check on connect:** If the connection request includes an `Origin` header, it must be `http://127.0.0.1:8765` / `http://localhost:8765`, or otherwise resolve to hostname `127.0.0.1`/`localhost`. Connections from other origins are closed.

Every message is a JSON object with a `type` field and a `data` field:

```json
{ "type": "<message-type>", "data": <payload> }
```

### Message types (server → client)

| `type` | When sent | `data` payload |
|---|---|---|
| `education` | Once, immediately on connect | Codec reference content (parsed `codec_info.json`) |
| `history` | Once, immediately on connect | Array of history points |
| `alerts_history` | Once on connect, **only if** past alerts exist | Array of alert objects |
| `snapshot` | Once on connect (if a snapshot is cached), then whenever the live snapshot changes | Snapshot object |
| `alerts` | Whenever one or more new alerts fire | Array of newly-fired alert objects |

---

#### `type: "education"`

- **Direction:** server → client
- **When:** Sent once, right after the connection opens.
- **Shape:** `data` is the parsed contents of `backend/codec_info.json` — an object with top-level keys `codecs`, `metrics`, and `outputs` describing each codec (title, summary, bitrate, color, paragraphs) and related educational content.
  ```json
  {
    "type": "education",
    "data": {
      "codecs": { "SBC": { "title": "...", "summary": "...", "bitrate_kbps": 328, "color": "#888780", "paragraphs": ["..."] }, "...": {} },
      "metrics": { "...": {} },
      "outputs": { "...": {} }
    }
  }
  ```

---

#### `type: "history"`

- **Direction:** server → client
- **When:** Sent once, right after the connection opens.
- **Shape:** `data` is an array of history points (same shape as `GET /history`):
  ```json
  {
    "type": "history",
    "data": [
      { "t": 1750000000.0, "device": "Sony WH-1000XM4", "mac": "AA:BB:CC:DD:EE:FF", "codec": "LDAC", "bitrate": 990, "battery": 80, "type": "bluetooth" }
    ]
  }
  ```

---

#### `type: "alerts_history"`

- **Direction:** server → client
- **When:** Sent once on connect, **only if** there are already-recorded alerts from the current session.
- **Shape:** `data` is an array of alert objects:
  ```json
  {
    "type": "alerts_history",
    "data": [
      { "time": 1750000000.0, "type": "connect", "msg": "Sony WH-1000XM4 connected" }
    ]
  }
  ```
  Each alert's inner `type` is one of: `connect`, `disconnect`, `switch`, `upgrade`, `downgrade`, `codec_change`.

---

#### `type: "snapshot"`

- **Direction:** server → client
- **When:** Sent once on connect if a snapshot is already cached, and then broadcast by the fast loop **every time the live snapshot payload changes** (device, codec, Alt A2DP status, connection stability, or outputs).
- **Shape:** `data` is the live snapshot object:
  ```json
  {
    "type": "snapshot",
    "data": {
      "timestamp": "2026-06-26T18:43:03",
      "server_epoch": 1750000000.0,
      "device": {
        "name": "Sony WH-1000XM4",
        "type": "bluetooth",
        "mac": "AA:BB:CC:DD:EE:FF",
        "battery": 80,
        "connected": true,
        "connect_epoch": 1750000000.0,
        "photo": "/photos/sony_wh_1000xm4.jpg"
      },
      "codec": {
        "name": "LDAC",
        "bitrate_kbps": 990,
        "sample_rate_khz": 96.0,
        "bit_depth": 24,
        "driver": "Alternative A2DP Driver"
      },
      "alt_a2dp_installed": true,
      "connection_stability": { "label": "Stable", "events_10min": 0 },
      "outputs": [
        { "name": "Speakers (Realtek)", "type": "built-in", "status": "OK", "active": false }
      ]
    }
  }
  ```
  Notes:
  - `device` is `null` when no active output device is detected. When non-null, `mac`, `battery`, and `photo` may individually be `null`.
  - `device.type` is one of `bluetooth`, `headphones`, `built-in`, `hdmi`, `usb`, `other`, or `microphone`.
  - `codec.name` is one of `SBC`, `AAC`, `aptX`, `aptX HD`, `LDAC`, or `PCM`. `bitrate_kbps` may be `null` (e.g. for `PCM`).
  - `connection_stability` is `null` for non-Bluetooth devices; otherwise its `label` is `Stable`, `Occasional drops`, or `Unstable`.

---

#### `type: "alerts"`

- **Direction:** server → client
- **When:** Broadcast whenever the fast loop detects one or more new (debounced) events — a connect, disconnect, device switch, or codec upgrade/downgrade/change.
- **Shape:** `data` is an array of the newly-fired alert objects:
  ```json
  {
    "type": "alerts",
    "data": [
      { "time": 1750000000.0, "type": "downgrade", "msg": "Codec downgraded: LDAC → SBC" }
    ]
  }
  ```
  Each alert's inner `type` is one of: `connect`, `disconnect`, `switch`, `upgrade`, `downgrade`, `codec_change`.
