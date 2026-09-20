# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller PhantomBridge.spec --clean
# Output: dist\PhantomBridge\PhantomBridge.exe   (that folder is the installation)
#
# One folder, not one file, on purpose: two of these start at every login
# (tray and collector), and a --onefile exe unpacks its ~15 MB into %TEMP%
# on each launch. A folder starts from where it is. The runtime cost is the
# same interpreter either way; the exe buys an identity (icon, name in Task
# Manager, no dependence on the installed Python), not speed.
#
# No console: this exe carries only the two resident services. Everything
# with output stays `python <script>`. icon.ico comes from `python icon.py`.

a = Analysis(
    ['main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'test'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PhantomBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PhantomBridge',
)
