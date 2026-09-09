# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['stock_monitor.py'],
    pathex=[], binaries=[], datas=[], hiddenimports=[], hookspath=[],
    hooksconfig={}, runtime_hooks=['qt_runtime_hook.py'], excludes=[],
    noarchive=False, optimize=0,
)

# Do not bundle incompatible third-party ICU DLLs. Windows 11 provides the
# ICU ABI required by this PySide6 build, and excluding these avoids the
# ucnv_open / ucnv_open_78 conflict seen in earlier packages.
a.binaries = [
    entry for entry in a.binaries
    if entry[0].lower() not in {'icuuc.dll', 'icudt78.dll'}
]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='gp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
