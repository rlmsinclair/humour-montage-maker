#!/bin/bash

# Function to run command as non-root user
run_as_user() {
    if [ $(id -u) = 0 ]; then
        # If running as root, switch to the sudo user
        local real_user=$(who am i | awk '{print $1}')
        su - $real_user -c "$1"
    else
        # If already non-root, just run the command
        eval "$1"
    fi
}

echo "Checking dependencies..."

# Check if Homebrew is installed
if ! command -v brew &> /dev/null; then
    echo "Installing Homebrew..."
    run_as_user '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
fi

# Check if ffmpeg is installed
if ! command -v ffmpeg &> /dev/null; then
    echo "Installing ffmpeg..."
    run_as_user 'brew install ffmpeg'
fi

echo "Dependencies installation complete!"
