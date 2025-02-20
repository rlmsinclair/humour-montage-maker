#!/usr/bin/env python3
import os
import sys
import subprocess
from PyQt6.QtWidgets import (QApplication, QWizard, QWizardPage, QLabel, 
                           QVBoxLayout, QProgressBar, QLineEdit)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

class InstallThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    details = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, password):
        super().__init__()
        self.password = password

    def run(self):
        try:
            # Set up environment
            if 'PATH' not in os.environ:
                os.environ['PATH'] = ''
            
            # Add common binary paths
            paths_to_add = [
                '/usr/local/bin',
                '/usr/bin',
                '/bin',
                '/opt/homebrew/bin',
                '/usr/sbin'
            ]
            os.environ['PATH'] = ':'.join(paths_to_add) + ':' + os.environ['PATH']
            
            # Initialize
            self.status.emit("Checking system requirements...")
            self.progress.emit(5)
            self.details.emit("• Verifying Python installation")
            
            # Check Python
            try:
                # First verify sudo access
                verify_cmd = f"echo '{self.password}' | sudo -S echo 'Password verified'"
                result = subprocess.run(
                    verify_cmd,
                    shell=True,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    raise Exception("Invalid administrator password")

                # Check various Python installations
                python_paths = [
                    "/usr/local/bin/python3",
                    "/opt/homebrew/bin/python3",
                    "/usr/bin/python3",
                    "python3"
                ]
                
                python_found = False
                for python_path in python_paths:
                    try:
                        python_version = subprocess.run(
                            [python_path, "--version"],
                            check=True,
                            capture_output=True,
                            text=True
                        ).stdout.strip()
                        
                        # Check if Python version is 3.13 or higher
                        version = tuple(map(int, python_version.split()[1].split('.')))
                        if version >= (3, 13):
                            self.details.emit(f"✓ {python_version} is already installed at {python_path}")
                            python_found = True
                            break
                        else:
                            self.details.emit(f"• Found {python_version} at {python_path}, but version 3.13 or higher is required")
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        continue
                
                if not python_found:
                    self.details.emit("• No suitable Python installation found")
                    self.status.emit("Installing Python 3.13...")
                    self.details.emit("• Downloading Python installer")
                    
                    # First verify the Python installer URL is accessible
                    self.details.emit("• Verifying Python installer availability")
                    check_url = subprocess.run(
                        ["/usr/bin/curl", "-I", "https://www.python.org/ftp/python/3.13.0/python-3.13.0-macos11.pkg"],
                        capture_output=True,
                        text=True
                    )
                    if check_url.returncode != 0:
                        raise Exception("Unable to access Python installer. Please check your internet connection.")

                    # Download Python installer with error checking
                    self.details.emit("• Downloading Python installer")
                    download_result = subprocess.run(
                        ["/usr/bin/curl", "-L", "-o", "python-installer.pkg", "https://www.python.org/ftp/python/3.13.0/python-3.13.0-macos11.pkg"],
                        capture_output=True,
                        text=True
                    )
                    if download_result.returncode != 0:
                        raise Exception(f"Failed to download Python installer: {download_result.stderr}")
                    
                    if not os.path.exists("python-installer.pkg"):
                        raise Exception("Python installer was not downloaded successfully")
                    
                    # Verify the installer file size
                    file_size = os.path.getsize("python-installer.pkg")
                    if file_size < 1000000:  # Less than 1MB would indicate a failed download
                        raise Exception("Python installer download appears incomplete")
                    
                    self.details.emit("• Installing Python (this may take a few minutes)")
                    # Install Python using provided password
                    install_cmd = f"echo '{self.password}' | sudo -S installer -pkg python-installer.pkg -target /"
                    install_result = subprocess.run(
                        install_cmd,
                        shell=True,
                        capture_output=True,
                        text=True
                    )
                    
                    # Cleanup
                    if os.path.exists("python-installer.pkg"):
                        os.remove("python-installer.pkg")
                    
                    if install_result.returncode != 0:
                        error_msg = install_result.stderr or install_result.stdout or "No error output available"
                        raise Exception(f"Failed to install Python. Error: {error_msg}")
                    
                    # Verify the installation
                    verify_cmd = subprocess.run(
                        ["/usr/local/bin/python3", "--version"],
                        capture_output=True,
                        text=True
                    )
                    if verify_cmd.returncode != 0:
                        raise Exception("Python installation could not be verified")
                        
                    self.details.emit("✓ Python 3.13 installed successfully")
                    
            except subprocess.CalledProcessError as e:
                raise Exception(f"Error checking/installing Python: {str(e)}\nOutput: {e.stderr if hasattr(e, 'stderr') else 'No error output'}")
            except Exception as e:
                raise Exception(f"Error during Python installation: {str(e)}")
            
            self.progress.emit(50)
            self.status.emit("Checking package manager...")
            self.details.emit("\n• Verifying Homebrew installation")

            # Check if Homebrew is installed
            try:
                brew_paths = [
                    "/usr/local/bin/brew",
                    "/opt/homebrew/bin/brew",
                    "brew"
                ]
                
                brew_found = False
                for brew_path in brew_paths:
                    try:
                        brew_version = subprocess.run(
                            [brew_path, "--version"],
                            check=True,
                            capture_output=True,
                            text=True
                        ).stdout.split('\n')[0]
                        self.details.emit(f"✓ Homebrew is already installed: {brew_version}")
                        brew_found = True
                        break
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        continue
                
                if not brew_found:
                    self.status.emit("Installing Homebrew...")
                    try:
                        self.details.emit("• Installing Homebrew package manager")
                        # Download Homebrew install script
                        download_result = subprocess.run(
                            ["/usr/bin/curl", "-fsSL", "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", "-o", "homebrew_install.sh"],
                            capture_output=True,
                            text=True
                        )
                        if download_result.returncode != 0:
                            raise Exception(f"Failed to download Homebrew installer: {download_result.stderr}")
                        
                        # Make script executable
                        os.chmod("homebrew_install.sh", 0o755)
                        
                        # Run Homebrew installer
                        brew_install = f"echo '{self.password}' | sudo -S /bin/bash homebrew_install.sh"
                        install_result = subprocess.run(
                            brew_install,
                            shell=True,
                            capture_output=True,
                            text=True
                        )
                        
                        # Cleanup install script
                        if os.path.exists("homebrew_install.sh"):
                            os.remove("homebrew_install.sh")
                            
                        if install_result.returncode != 0:
                            raise Exception(f"Failed to install Homebrew: {install_result.stderr}")
                        
                        self.details.emit("✓ Homebrew installed successfully")
                        
                        # Update PATH to include Homebrew
                        os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")
                        
                    except Exception as e:
                        raise Exception(f"Error installing Homebrew: {str(e)}")

            except Exception as e:
                raise Exception(f"Error during Homebrew installation: {str(e)}")

            self.progress.emit(75)
            self.status.emit("Checking media components...")
            self.details.emit("\n• Verifying FFmpeg installation")

            # Check FFmpeg
            try:
                # Check if FFmpeg is in PATH
                ffmpeg_paths = [
                    "/usr/local/bin/ffmpeg",
                    "/opt/homebrew/bin/ffmpeg",
                    "ffmpeg"
                ]
                
                ffmpeg_found = False
                for ffmpeg_path in ffmpeg_paths:
                    try:
                        ffmpeg_version = subprocess.run(
                            [ffmpeg_path, "-version"],
                            check=True,
                            capture_output=True,
                            text=True
                        ).stdout.split('\n')[0]
                        self.details.emit(f"✓ FFmpeg is already installed: {ffmpeg_version}")
                        ffmpeg_found = True
                        break
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        continue
                
                if not ffmpeg_found:
                    self.status.emit("Installing FFmpeg...")
                    self.details.emit("• Installing FFmpeg via Homebrew")
                    ffmpeg_install = subprocess.run(
                        ["/opt/homebrew/bin/brew", "install", "ffmpeg"],
                        capture_output=True,
                        text=True
                    )
                    
                    if ffmpeg_install.returncode != 0:
                        raise Exception(f"Failed to install FFmpeg: {ffmpeg_install.stderr}")
                    
                    self.details.emit("✓ FFmpeg installed successfully")
            except Exception as e:
                raise Exception(f"Error during FFmpeg installation: {str(e)}")

            self.progress.emit(100)
            self.status.emit("Dependencies installed successfully!")
            self.details.emit("\n✓ All required components are now installed")
            self.finished.emit()

        except Exception as e:
            self.error.emit(str(e))

class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to Udder AI")
        layout = QVBoxLayout()
        label = QLabel(
            "Welcome to the Udder AI installation wizard!\n\n"
            "This wizard will install the required components for Udder AI:\n"
            "• Python 3.13 (if not already installed)\n"
            "• Homebrew package manager (if not already installed)\n"
            "• FFmpeg for video processing\n\n"
            "After installation completes, you'll be able to drag Udder AI "
            "to your Applications folder.\n\n"
            "Click Next to continue."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.setLayout(layout)

class RequirementsPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Installation Components")
        layout = QVBoxLayout()
        
        label = QLabel(
            "The following components will be installed:\n\n"
            "1. Python 3.13\n"
            "   • Required for running the application\n"
            "   • Will be installed if not already present\n\n"
            "2. Homebrew\n"
            "   • Required for installing FFmpeg\n"
            "   • Will be installed if not already present\n\n"
            "3. FFmpeg\n"
            "   • Required for video processing\n"
            "   • Will be installed via Homebrew\n\n"
            "After these components are installed, you can drag Udder AI "
            "to your Applications folder to complete the installation.\n\n"
            "Total estimated disk space required: 400MB"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.setLayout(layout)

class PasswordPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Administrator Access Required")
        layout = QVBoxLayout()
        
        label = QLabel(
            "Administrator access is required to install dependencies.\n\n"
            "Your password will be used securely to:\n"
            "• Install Python 3.13 (if needed)\n"
            "• Install Homebrew package manager (if needed)\n"
            "• Install FFmpeg via Homebrew (if needed)\n\n"
            "Please enter your administrator password:"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Enter administrator password")
        # Style the password field to look like a native macOS password field
        self.password.setStyleSheet("""
            QLineEdit {
                padding: 8px;
                border: 1px solid #c0c0c0;
                border-radius: 4px;
                background-color: white;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #0066cc;
            }
        """)
        # Allow pressing Enter/Return to proceed
        self.password.returnPressed.connect(lambda: self.wizard().next())
        layout.addWidget(self.password)
        
        security_note = QLabel(
            "\nNote: Your password is only used locally for installation "
            "and is never stored or transmitted."
        )
        security_note.setWordWrap(True)
        layout.addWidget(security_note)
        
        self.registerField("password*", self.password)
        self.setLayout(layout)

class InstallationPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Installing Dependencies")
        layout = QVBoxLayout()
        
        self.status = QLabel("Preparing to install...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        
        self.details = QLabel("")
        self.details.setStyleSheet("font-family: monospace;")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        
        note = QLabel(
            "\nInstallation may take several minutes. Please do not close "
            "the installer or put your computer to sleep."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        
        self.setLayout(layout)

    def initializePage(self):
        password = self.field("password")
        
        self.thread = InstallThread(password)
        self.thread.progress.connect(self.progress.setValue)
        self.thread.status.connect(self.status.setText)
        self.thread.details.connect(self.details.setText)
        self.thread.finished.connect(self.completeChanged)
        self.thread.error.connect(self.handleError)
        self.thread.start()

    def handleError(self, error):
        self.status.setText("Installation Error")
        self.details.setText(f"An error occurred:\n{error}\n\nPlease check your password and try again.")
        self.progress.setValue(0)
        # Don't go back to password screen, just stay on current page
        self.completeChanged.emit()

    def isComplete(self):
        # Only complete if progress is 100% or there was no error
        return self.progress.value() == 100 and not self.status.text().startswith("Installation Error")

class CompletionPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Dependencies Installed")
        layout = QVBoxLayout()
        label = QLabel(
            "Dependencies installed successfully!\n\n"
            "To complete the installation:\n"
            "1. Close this installer\n"
            "2. Drag Udder AI to your Applications folder\n"
            "3. Launch Udder AI from Applications and start creating amazing videos!\n\n"
            "For help and tutorials, visit: https://udderai.com/help\n"
            "For support, contact: support@udderai.com\n\n"
            "Note: You can safely close this installer now and drag Udder AI to Applications."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.setLayout(layout)

class InstallerWizard(QWizard):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("Udder AI Installer")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        
        # Full installation flow
        self.addPage(WelcomePage())
        self.addPage(RequirementsPage())
        self.addPage(PasswordPage())
        self.addPage(InstallationPage())
        self.addPage(CompletionPage())
        
        # Set a professional size
        self.setMinimumWidth(700)
        self.setMinimumHeight(500)

def main():
    app = QApplication(sys.argv)
    wizard = InstallerWizard()
    wizard.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
