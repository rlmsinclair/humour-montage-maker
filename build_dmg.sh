#!/bin/bash

# Check if dmgbuild is installed
if ! command -v dmgbuild &> /dev/null; then
    echo "Installing dmgbuild..."
    pip3 install dmgbuild
fi

# Build the DMG
echo "Building DMG..."
dmgbuild -s dmg_settings.py "Udder AI" UdderAI.dmg

echo "DMG created successfully: UdderAI.dmg"
