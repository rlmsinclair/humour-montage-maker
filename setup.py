from setuptools import setup

APP = ['main.py']
DATA_FILES = [
    ('bin', ['/opt/homebrew/bin/ffmpeg'])  # Include ffmpeg binary
]
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'udder.icns',
    'packages': [
        'PyQt6', 'aiohttp', 'aiofiles'
    ],
    'excludes': [
        'typing_extensions', 'PyQt6.QtWebChannel', 'PyQt6.QtWebEngine',
        'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebSockets',
        'PyQt6.QtXml', 'PyQt6.QtDesigner', 'PyQt6.QtHelp', 'PyQt6.QtLocation',
        'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtNetwork',
        'PyQt6.QtNfc', 'PyQt6.QtOpenGL', 'PyQt6.QtPositioning', 'PyQt6.QtQml',
        'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtRemoteObjects',
        'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.QtSql', 'PyQt6.QtTest',
        'pkg_resources', 'packaging'  # Exclude packaging to avoid conflicts
    ],
    'qt_plugins': ['platforms'],
    'plist': {
        'CFBundleName': 'Udder AI',
        'CFBundleDisplayName': 'Udder AI',
        'CFBundleIdentifier': 'com.udder.ai',
        'CFBundleVersion': "1.0.0",
        'CFBundleShortVersionString': "1.0.0",
        'LSMinimumSystemVersion': '10.15',
        'NSHighResolutionCapable': True,
        'LSEnvironment': {
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/opt/ffmpeg/bin'  # Added ffmpeg path
        }
    },
    'includes': [
        'PyQt6', 'aiohttp', 'aiofiles', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui',
        'logging', 'asyncio', 'json', 'pathlib', 'subprocess'
    ]
}

setup(
    name='Udder AI',
    app=APP,
    data_files=DATA_FILES,
    options={
        'py2app': OPTIONS,
        'bdist_dmg': {
            'volume_label': 'Udder AI',
            'applications_shortcut': True,
        }
    },
    setup_requires=['py2app', 'dmgbuild'],
)
