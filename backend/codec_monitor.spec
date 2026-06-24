# PyInstaller spec for Codec Monitor.
# Build with: .venv\Scripts\python -m PyInstaller backend\codec_monitor.spec --noconfirm
# Bundles frontend/ and codec_info.json as read-only resources (see DATA_DIR vs
# _BUNDLE_DIR split in monitor.py for why settings/history/photos are NOT bundled here).
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "backend" / "app.py")],
    pathex=[str(ROOT / "backend")],
    binaries=[],
    datas=[
        (str(ROOT / "frontend"), "frontend"),
        (str(ROOT / "backend" / "codec_info.json"), "."),
        (str(ROOT / "backend" / "icon.png"), "."),
        (str(ROOT / "backend" / "icon.ico"), "."),
    ],
    hiddenimports=["win11toast"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Codec Monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "backend" / "icon.ico"),
)
