# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[('/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/platforms', 'PyQt6/Qt6/plugins/platforms'), ('/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/styles', 'PyQt6/Qt6/plugins/styles')],
    datas=[('requirements.txt', '.'), ('/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins', 'PyQt6/Qt6/plugins')],
    hiddenimports=['PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui', 'PyQt6.sip'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['fix_paths.py'],
    excludes=[],
    noarchive=True,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [('v', None, 'OPTION')],
    exclude_binaries=True,
    name='UdderAI-Installer',
    debug=True,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['udder.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='UdderAI-Installer',
)
app = BUNDLE(
    coll,
    name='UdderAI-Installer.app',
    icon='udder.icns',
    bundle_identifier=None,
)
