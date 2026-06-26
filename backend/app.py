"""
Codec Monitor desktop app shell.

Replaces "run monitor.py in a console + open a browser tab" with a single
window (pywebview) plus a system tray icon (pystray). Closing the window
hides it to the tray instead of quitting; Quit from the tray menu actually
exits.
"""
import asyncio
import os
import socket
import threading
import time
import webbrowser

import webview
import pystray
from PIL import Image, ImageDraw

import monitor

_window = None
_icon = None
_stop_tooltip = threading.Event()

PORT_CONTROL = 8767  # used only to detect/signal an already-running instance

EXPORTERS = {
    "csv": (monitor.export_history_csv, "wb", lambda c: c.encode("utf-8")),
    "md": (monitor.export_history_markdown, "wb", lambda c: c.encode("utf-8")),
    "pdf": (monitor.export_history_pdf, "wb", lambda c: c),
}
DEFAULT_NAMES = {"csv": "codec_monitor_report.csv", "md": "codec_monitor_report.md", "pdf": "codec_monitor_report.pdf"}
FILE_TYPES = {
    "csv": ("CSV files (*.csv)",),
    "md": ("Markdown files (*.md)",),
    "pdf": ("PDF files (*.pdf)",),
}


class JsApi:
    def open_external(self, url):
        if not (url.startswith("https://") or url.startswith("http://")):
            return {"ok": False, "error": "Unsupported URL scheme"}
        webbrowser.open(url)
        return {"ok": True}

    def export_report(self, fmt):
        if fmt not in EXPORTERS:
            return {"ok": False, "error": f"Unsupported format: {fmt}"}
        build_fn, mode, encode = EXPORTERS[fmt]
        result = _window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=DEFAULT_NAMES[fmt],
            file_types=FILE_TYPES[fmt],
        )
        if not result:
            return {"ok": False, "error": "cancelled"}
        path = result if isinstance(result, str) else result[0]
        try:
            content = build_fn()
            with open(path, mode) as f:
                f.write(encode(content))
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _signal_existing_instance() -> bool:
    """If another instance is already listening on PORT_CONTROL, tell it to show
    its window and return True (caller should exit immediately instead of
    starting a second, doomed-to-conflict set of servers/tray icon)."""
    try:
        with socket.create_connection(("127.0.0.1", PORT_CONTROL), timeout=0.3) as s:
            s.sendall(b"show")
            resp = s.recv(32)
            return resp == b"codec-monitor-ack"
    except OSError:
        return False


_control_srv = None


def _start_control_listener():
    def _serve():
        global _control_srv
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", PORT_CONTROL))
        except OSError:
            return
        srv.listen(5)
        _control_srv = srv
        while True:
            try:
                conn, _ = srv.accept()
                data = conn.recv(16)
                if data == b"show":
                    try:
                        conn.sendall(b"codec-monitor-ack")
                    except Exception:
                        pass
                conn.close()
                if data == b"show":
                    _show_window()
            except OSError:
                # srv was closed (shutdown) — exit the loop instead of spinning.
                break
            except Exception:
                pass
    threading.Thread(target=_serve, daemon=True).start()


import sys
from pathlib import Path

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = Path(__file__).parent.absolute()
    return os.path.join(base_path, relative_path)

def _make_icon_image():
    icon_path = get_resource_path("icon.png")
    if os.path.exists(icon_path):
        with Image.open(icon_path) as img:
            img.load()
            return img
    # fallback if not found
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(83, 74, 183, 255))
    return img


def _run_ws_server_thread():
    asyncio.run(monitor.run_ws_server())


def _tooltip_updater():
    while not _stop_tooltip.is_set():
        cached = monitor.get_cached_snapshot()
        if cached and _icon:
            snap, _ = cached
            codec = snap["codec"]
            device = snap["device"]
            if device:
                bitrate = codec.get("bitrate_kbps")
                text = f"Codec Monitor — {codec['name']}"
                if bitrate:
                    text += f" {bitrate}kbps"
            else:
                text = "Codec Monitor — no device"
            try:
                _icon.title = text[:127]
            except Exception:
                pass
        time.sleep(2)


def _show_window():
    if _window:
        _window.show()
        _window.restore()
        monitor.set_window_visible(True)


def _check_window_visible():
    """Ground-truth check via the native WinForms object, instead of trusting
    pywebview's shown/minimized/restored events alone — those didn't fire
    reliably enough in testing (native notifications were firing/not-firing
    incorrectly). Returns True/False, or None to fall back to the event-
    tracked flag if the native object isn't available for some reason."""
    try:
        native = getattr(_window, "native", None)
        if native is None:
            return None

        def get_vis_state():
            state = str(getattr(native, "WindowState", ""))
            if "Minimized" in state:
                return False
            visible = getattr(native, "Visible", None)
            if visible is False:
                return False
            return True

        if getattr(native, "InvokeRequired", False):
            import System
            func_delegate = System.Func[bool](get_vis_state)
            return native.Invoke(func_delegate)
        else:
            return get_vis_state()
    except Exception:
        return None


def _terminate():
    """Shared shutdown logic. Does NOT call _window.destroy() — callers that
    are already inside the window's closing event must not call destroy()
    again, or pywebview re-fires closing -> infinite recursion -> stack
    overflow (this was a real, reproduced bug). os._exit kills the whole
    process immediately regardless of whether the window finished destroying."""
    _stop_tooltip.set()
    monitor.shutdown()
    if _control_srv:
        try:
            _control_srv.close()
        except OSError:
            pass
    if _icon:
        try:
            _icon.stop()
        except Exception:
            pass
    os._exit(0)


def _quit_app():
    """Tray menu 'Quit' — NOT called from within the closing event, so
    destroying the window here is safe."""
    if _window:
        try:
            _window.destroy()
        except Exception:
            pass
    _terminate()


def _on_closing():
    if monitor.get_settings().get("close_action") == "quit":
        _terminate()
        return False  # moot — os._exit already terminated the process
    # Default: hide instead of quitting — monitor keeps running in the tray.
    _window.hide()
    monitor.set_window_visible(False)
    return False


def main():
    if sys.platform == "win32":
        import ctypes
        myappid = 'codecmonitor.app.1.0' # arbitrary string
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    global _window, _icon

    if _signal_existing_instance():
        print("Codec Monitor is already running — bringing its window to front.")
        return

    _start_control_listener()
    monitor.start_backend()
    threading.Thread(target=_run_ws_server_thread, daemon=True).start()

    start_min = bool(monitor.get_settings().get("start_minimized", False))
    _window = webview.create_window(
        "Codec Monitor",
        f"http://127.0.0.1:{monitor.PORT_HTTP}/",
        width=1000, height=850, min_size=(700, 600),
        js_api=JsApi(),
        hidden=start_min,
    )
    _window.events.closing += _on_closing
    _window.events.shown += lambda: monitor.set_window_visible(True)
    _window.events.minimized += lambda: monitor.set_window_visible(False)
    _window.events.restored += lambda: monitor.set_window_visible(True)
    monitor.set_visibility_checker(_check_window_visible)

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", lambda: _show_window(), default=True),
        pystray.MenuItem("Quit", lambda: _quit_app()),
    )
    _icon = pystray.Icon("codec-monitor", _make_icon_image(), "Codec Monitor", menu)
    _icon.run_detached()

    threading.Thread(target=_tooltip_updater, daemon=True).start()

    webview.start(icon=get_resource_path("icon.ico"))


if __name__ == "__main__":
    main()
