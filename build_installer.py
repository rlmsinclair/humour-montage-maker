#!/usr/bin/env python3
import PyInstaller.__main__
import os
import shutil
import sys

def build_main_app():
    print("Building main Udder AI app...")
    main_opts = [
        'main.py',  # Script to bundle
        '--name=Udder AI',  # Output name
        '--icon=udder.icns',  # Use app icon
        '--windowed',  # GUI application
        # Add hidden imports for PyQt6
        '--hidden-import=PyQt6.QtCore',
        '--hidden-import=PyQt6.QtWidgets',
        '--hidden-import=PyQt6.QtGui',
        '--hidden-import=PyQt6.sip',
        # Add PyQt6 plugins and platforms
        '--add-data=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins:PyQt6/Qt6/plugins',
        '--add-binary=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/platforms:PyQt6/Qt6/plugins/platforms',
        '--add-binary=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/styles:PyQt6/Qt6/plugins/styles',
        # Bundle mode
        '--onedir',  # Create a directory containing the executable
    ]
    
    PyInstaller.__main__.run(main_opts)
    
    # Verify main app was built
    if not os.path.exists('dist/Udder AI.app'):
        print("Error: Main app build failed")
        sys.exit(1)
    print("Successfully built main app")

def build_installer():
    print("Building installer...")
    # PyInstaller options
    opts = [
        'installer.py',  # Script to bundle
        '--name=UdderAI-Installer',  # Output name
        '--add-data=requirements.txt:.',  # Include requirements.txt
        '--icon=udder.icns',  # Use app icon
        '--windowed',  # GUI application
        # Add hidden imports for PyQt6
        '--hidden-import=PyQt6.QtCore',
        '--hidden-import=PyQt6.QtWidgets',
        '--hidden-import=PyQt6.QtGui',
        '--hidden-import=PyQt6.sip',
        # Add PyQt6 plugins and platforms
        '--add-data=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins:PyQt6/Qt6/plugins',
        '--add-binary=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/platforms:PyQt6/Qt6/plugins/platforms',
        '--add-binary=/Users/robbiesinclair/PycharmProjects/udder/.venv/lib/python3.13/site-packages/PyQt6/Qt6/plugins/styles:PyQt6/Qt6/plugins/styles',
        # Debug mode
        '--debug=all',
        # Bundle mode
        '--onedir',  # Create a directory containing the executable
        # Runtime hooks
        '--runtime-hook=fix_paths.py'
    ]

    # Create runtime hook to fix paths
    with open('fix_paths.py', 'w') as f:
        f.write("""
import os
import sys

# Add common binary paths to PATH
paths_to_add = [
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
    '/opt/homebrew/bin',
    '/usr/sbin'
]

if 'PATH' not in os.environ:
    os.environ['PATH'] = ''

os.environ['PATH'] = ':'.join(paths_to_add) + ':' + os.environ['PATH']
""")
    
    # Run PyInstaller
    PyInstaller.__main__.run(opts)
    
    # Copy the built installer to dist directory
    if os.path.exists('dist/UdderAI-Installer'):
        # Make it executable
        os.chmod('dist/UdderAI-Installer', 0o755)
        print("Successfully built installer")
    else:
        print("Error: Installer build failed")
        sys.exit(1)

if __name__ == '__main__':
    # First build the main app
    build_main_app()
    # Then build the installer
    build_installer()
