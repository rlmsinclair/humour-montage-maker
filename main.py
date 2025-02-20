import sys
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import List, Dict, Optional
import aiofiles
import aiohttp
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                            QPushButton, QProgressBar, QLabel, QFileDialog, QTextEdit,
                            QMessageBox, QSlider, QFrame, QGraphicsScene, QGraphicsView,
                            QGraphicsProxyWidget)
from PyQt6.QtGui import QPainter, QBrush, QLinearGradient, QColor
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QUrl, QTime, QTimer
from PyQt6.QtGui import QColor, QPalette, QIcon
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

# API settings
API_URL = "http://127.0.0.1:8000"

class ProcessingThread(QThread):
    progress_update = pyqtSignal(str, int)
    status_update = pyqtSignal(str)
    processing_finished = pyqtSignal(bool, str)

    def __init__(self, video_path: Path):
        super().__init__()
        self.video_path = video_path
        self.running = True

    def run(self):
        asyncio.run(self.process_video())

    def stop(self):
        self.running = False

    async def process_video(self):
        try:
            output_path = Path('funny_montage.mp4')
            
            # Extract audio
            self.status_update.emit("Extracting audio...")
            audio_path = await self.extract_audio(self.video_path)
            if not audio_path:
                self.processing_finished.emit(False, "Error extracting audio!")
                return

            # Send audio to API for processing
            self.status_update.emit("Processing audio...")
            async with aiohttp.ClientSession() as session:
                data = aiohttp.FormData()
                data.add_field('file',
                    open(audio_path, 'rb'),
                    filename=audio_path.name,
                    content_type='audio/mpeg'
                )
                
                async with session.post(f"{API_URL}/process-audio", data=data) as response:
                    if response.status != 200:
                        error_msg = await response.text()
                        self.processing_finished.emit(False, f"API Error: {error_msg}")
                        return
                        
                    result = await response.json()
                    funny_clips = result.get('clips', [])

            if funny_clips:
                merged_clips = self.merge_overlapping_clips(funny_clips)
                merged_clips.sort(key=lambda x: x.get('humor_score', 0), reverse=True)
                final_clips = merged_clips[:60]

                self.status_update.emit("Creating montage...")
                success = await self.create_montage(
                    self.video_path,
                    final_clips,
                    output_path
                )

                if success:
                    self.processing_finished.emit(True, "Successfully created funny_montage.mp4!")
                else:
                    self.processing_finished.emit(False, "Error creating montage!")
            else:
                self.processing_finished.emit(False, "No funny clips found!")

        except Exception as e:
            self.processing_finished.emit(False, f"An error occurred: {e}")
        finally:
            # Cleanup temporary files
            await self.cleanup_files([audio_path])

    @staticmethod
    def get_ffmpeg_path() -> str:
        # First try system PATH
        try:
            result = subprocess.run(['which', 'ffmpeg'], 
                                 capture_output=True, 
                                 text=True, 
                                 check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass

        # Common installation locations
        possible_paths = [
            '/opt/homebrew/bin/ffmpeg',  # Homebrew on Apple Silicon
            '/usr/local/bin/ffmpeg',     # Homebrew on Intel Mac
            '/usr/bin/ffmpeg',           # System install
        ]
        
        if getattr(sys, 'frozen', False):
            # Add bundled paths when running as app
            bundle_dir = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(sys.executable).parent
            possible_paths.extend([
                str(bundle_dir / 'bin' / 'ffmpeg'),
                str(bundle_dir / 'ffmpeg'),
                str(bundle_dir.parent / 'MacOS' / 'ffmpeg'),
            ])

        # Try each path
        for path in possible_paths:
            if os.path.isfile(path):
                return path

        # Default to just 'ffmpeg' and let the system handle it
        return 'ffmpeg'

    async def extract_audio(self, video_path: Path) -> Optional[Path]:
        try:
            self.status_update.emit(f"Input video path: {video_path}")
            audio_path = video_path.with_suffix('.mp3')
            self.status_update.emit(f"Output audio path: {audio_path}")
            
            ffmpeg_path = self.get_ffmpeg_path()
            self.status_update.emit(f"Using ffmpeg from: {ffmpeg_path}")
            
            # Verify ffmpeg exists and is executable
            if not os.path.isfile(ffmpeg_path):
                self.status_update.emit(f"FFmpeg not found at: {ffmpeg_path}")
                if ffmpeg_path == 'ffmpeg':
                    self.status_update.emit("Checking PATH locations:")
                    for path in os.environ.get('PATH', '').split(':'):
                        ffmpeg_check = os.path.join(path, 'ffmpeg')
                        exists = os.path.isfile(ffmpeg_check)
                        self.status_update.emit(f"  {ffmpeg_check}: {'Found' if exists else 'Not found'}")
                return None
            
            try:
                os.access(ffmpeg_path, os.X_OK)
            except Exception as e:
                self.status_update.emit(f"FFmpeg exists but may not be executable: {e}")
                return None
                
            cmd = [
                ffmpeg_path, '-i', str(video_path),
                '-vn', '-acodec', 'libmp3lame',
                '-ac', '1', '-ar', '16000',
                '-q:a', '5',
                '-y', str(audio_path)
            ]
            self.status_update.emit(f"Executing command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_output = stderr.decode() if stderr else "No error output"
                self.status_update.emit(f"FFmpeg failed with return code {process.returncode}")
                self.status_update.emit(f"Error output:\n{error_output}")
                return None
                
            if not audio_path.exists():
                self.status_update.emit("Audio file was not created")
                return None
                
            self.status_update.emit(f"Successfully created audio file: {audio_path}")
            return audio_path
            
        except Exception as e:
            self.status_update.emit(f"Exception during audio extraction: {str(e)}")
            import traceback
            self.status_update.emit(f"Traceback:\n{traceback.format_exc()}")
            return None

    @staticmethod
    def merge_overlapping_clips(clips: List[Dict], max_gap: float = 2.0, min_duration: float = 3.0) -> List[Dict]:
        if not clips:
            return []

        sorted_clips = sorted(clips, key=lambda x: x.get('start_time', 0))
        merged = []
        current = sorted_clips[0]

        for next_clip in sorted_clips[1:]:
            if next_clip.get('start_time', 0) - current.get('end_time', 0) <= max_gap:
                current = {
                    'start_time': current.get('start_time', 0),
                    'end_time': next_clip.get('end_time', 0),
                    'text': str(current.get('text', '')) + ' ' + str(next_clip.get('text', '')),
                    'reason': str(current.get('reason', '')) + ' & ' + str(next_clip.get('reason', '')),
                    'humor_score': max(current.get('humor_score', 0), next_clip.get('humor_score', 0))
                }
            else:
                if current.get('end_time', 0) - current.get('start_time', 0) >= min_duration:
                    merged.append(current)
                current = next_clip

        if current.get('end_time', 0) - current.get('start_time', 0) >= min_duration:
            merged.append(current)

        return merged

    async def create_montage(self, video_path: Path, clips: List[Dict], output_path: Path) -> bool:
        temp_dir = Path('temp_clips')
        temp_dir.mkdir(exist_ok=True)
        self.status_update.emit(f"Created temporary directory: {temp_dir}")

        try:
            # Create individual clips
            clip_paths = []
            for idx, clip in enumerate(clips):
                clip_path = temp_dir / f"clip_{idx:03d}.mp4"
                start_time = max(0, clip['start_time'] - 0.5)
                duration = (clip['end_time'] + 0.5) - start_time

                self.status_update.emit(f"Processing clip {idx+1}/{len(clips)}")
                self.status_update.emit(f"Start time: {start_time:.2f}s, Duration: {duration:.2f}s")

                cmd = [
                    self.get_ffmpeg_path(),
                    '-ss', str(start_time),
                    '-t', str(duration),
                    '-i', str(video_path),
                    '-c', 'copy',
                    str(clip_path),
                    '-y'
                ]
                self.status_update.emit(f"Executing command: {' '.join(cmd)}")

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_output = stderr.decode() if stderr else "No error output"
                    self.status_update.emit(f"FFmpeg failed with return code {process.returncode}")
                    self.status_update.emit(f"Error output:\n{error_output}")
                    continue

                if clip_path.exists() and clip_path.stat().st_size > 0:
                    clip_paths.append(clip_path)
                    self.status_update.emit(f"Successfully created clip: {clip_path}")
                else:
                    self.status_update.emit(f"Failed to create clip: {clip_path}")

            if not clip_paths:
                self.status_update.emit("No valid clips were created")
                return False

            self.status_update.emit(f"Successfully created {len(clip_paths)} clips")

            # Create concat file
            concat_file = temp_dir / 'concat.txt'
            self.status_update.emit("Creating concat file for merging clips")
            async with aiofiles.open(concat_file, 'w') as f:
                for clip_path in clip_paths:
                    await f.write(f"file '{clip_path.absolute()}'\n")

            # Merge clips
            self.status_update.emit("Merging clips into final montage")
            cmd = [
                self.get_ffmpeg_path(), '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c', 'copy',
                str(output_path),
                '-y'
            ]
            self.status_update.emit(f"Executing command: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_output = stderr.decode() if stderr else "No error output"
                self.status_update.emit(f"Final merge failed with return code {process.returncode}")
                self.status_update.emit(f"Error output:\n{error_output}")
                return False

            if output_path.exists():
                self.status_update.emit(f"Successfully created montage: {output_path}")
                return True
            else:
                self.status_update.emit("Failed to create final montage file")
                return False

        finally:
            # Cleanup
            await self.cleanup_files([temp_dir])

    @staticmethod
    async def cleanup_files(paths: List[Path]):
        for path in paths:
            try:
                if path and path.exists():
                    if path.is_file():
                        os.remove(path)
                    elif path.is_dir():
                        for file in path.glob('*'):
                            try:
                                os.remove(file)
                            except Exception:
                                pass
                        try:
                            path.rmdir()
                        except Exception:
                            pass
            except Exception:
                pass

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Udder AI")
        self.setMinimumSize(600, 400)
        
        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # Create UI elements
        self.file_button = QPushButton("Select Video File")
        self.file_button.clicked.connect(self.select_file)
        
        self.file_label = QLabel("No file selected")
        
        self.start_button = QPushButton("Start Processing")
        self.start_button.clicked.connect(self.start_processing)
        self.start_button.setEnabled(False)
        
        self.save_button = QPushButton("Save Video")
        self.save_button.clicked.connect(self.save_video)
        self.save_button.hide()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMinimumHeight(100)
        
        # Create video container using QGraphicsScene
        self.video_container = QGraphicsView()
        self.video_container.setMinimumHeight(400)
        self.video_container.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_container.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_container.hide()
        
        # Create scene
        self.scene = QGraphicsScene()
        self.video_container.setScene(self.scene)
        
        # Create video player
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(400)
        
        # Add video widget to scene
        self.video_proxy = QGraphicsProxyWidget()
        self.video_proxy.setWidget(self.video_widget)
        self.scene.addItem(self.video_proxy)
        
        # Create overlay frame
        self.overlay_frame = QWidget()
        self.overlay_frame.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # Add overlay to scene
        self.overlay_proxy = QGraphicsProxyWidget()
        self.overlay_proxy.setWidget(self.overlay_frame)
        self.overlay_proxy.setZValue(1)  # Ensure overlay is on top
        self.scene.addItem(self.overlay_proxy)
        self.overlay_frame.installEventFilter(self)
        self.overlay_frame.setGeometry(0, 0, self.video_widget.width(), self.video_widget.height())
        self.overlay_frame.raise_()  # Ensure overlay is on top
        self.overlay_frame.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Make overlay frame resize with video widget
        def handle_resize(event):
            self.overlay_frame.setGeometry(0, 0, event.size().width(), event.size().height())
            self.overlay_frame.raise_()
            event.accept()
        self.video_widget.resizeEvent = handle_resize
        self.overlay_frame.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 transparent,
                    stop:0.7 transparent,
                    stop:1 rgba(0, 0, 0, 0.7));
                border: none;
            }
            QPushButton {
                background-color: transparent;
                border: none;
                color: white;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.1);
            }
            QSlider::groove:horizontal {
                border: none;
                height: 4px;
                background: rgba(255, 255, 255, 0.3);
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #2196F3;
                width: 12px;
                height: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }
            QSlider::handle:horizontal:hover {
                background: #64B5F6;
                width: 16px;
                height: 16px;
                margin: -6px 0;
            }
            QSlider::sub-page:horizontal {
                background: #2196F3;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal:hover {
                background: #64B5F6;
            }
            QLabel {
                color: white;
                font-size: 13px;
            }
        """)
        
        # Create media player
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        
        # Create video controls
        controls_layout = QVBoxLayout(self.overlay_frame)
        controls_layout.setContentsMargins(20, 20, 20, 20)
        controls_layout.setSpacing(10)
        controls_layout.addStretch()
        
        # Bottom controls container
        bottom_controls = QWidget()
        bottom_controls_layout = QVBoxLayout(bottom_controls)
        bottom_controls_layout.setContentsMargins(0, 0, 0, 0)
        bottom_controls_layout.setSpacing(5)
        
        # Progress bar with hover preview
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.set_position)
        self.position_slider.setToolTip("Seek (Left/Right: ±5s)")
        self.position_slider.setMouseTracking(True)
        self.position_slider.mouseMoveEvent = self.slider_mouse_move
        
        # Controls row
        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        
        self.play_button = QPushButton()
        self.play_button.setFixedSize(32, 32)
        self.play_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_playback)
        self.play_button.setToolTip("Play/Pause (Space)\nDouble-click video for fullscreen")
        
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setFixedWidth(100)
        
        # Volume control
        volume_container = QWidget()
        volume_layout = QHBoxLayout(volume_container)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(5)
        
        self.volume_button = QPushButton()
        self.volume_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaVolume))
        self.volume_button.setFixedSize(24, 24)
        self.volume_button.clicked.connect(self.toggle_mute)
        self.volume_button.setToolTip("Mute (M)")
        
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.volume_slider.setToolTip("Volume (Up/Down)")
        
        volume_layout.addWidget(self.volume_button)
        volume_layout.addWidget(self.volume_slider)
        
        # Fullscreen button
        self.fullscreen_button = QPushButton()
        self.fullscreen_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_TitleBarMaxButton))
        self.fullscreen_button.setFixedSize(24, 24)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.fullscreen_button.setToolTip("Fullscreen (F)")
        
        # Add widgets to controls row
        controls_row.addWidget(self.play_button)
        controls_row.addWidget(self.time_label)
        controls_row.addStretch()
        controls_row.addWidget(volume_container)
        controls_row.addWidget(self.fullscreen_button)
        
        # Add progress bar and controls row to bottom controls
        bottom_controls_layout.addWidget(self.position_slider)
        bottom_controls_layout.addLayout(controls_row)
        
        # Add bottom controls to overlay
        controls_layout.addWidget(bottom_controls)
        
        # Handle resize events
        def handle_resize(event):
            self.scene.setSceneRect(0, 0, event.size().width(), event.size().height())
            self.video_proxy.setGeometry(0, 0, event.size().width(), event.size().height())
            self.overlay_proxy.setGeometry(0, 0, event.size().width(), event.size().height())
            event.accept()
            
        self.video_container.resizeEvent = handle_resize
        
        self.media_player.positionChanged.connect(self.position_changed)
        self.media_player.durationChanged.connect(self.duration_changed)
        
        # Setup overlay visibility timer
        self.overlay_timer = QTimer(self)
        self.overlay_timer.setSingleShot(True)
        self.overlay_timer.timeout.connect(lambda: self.overlay_frame.hide())
        
        # Event filter for overlay visibility
        def eventFilter(self, obj, event):
            if obj == self.overlay_frame:
                if event.type() == event.Type.Enter:
                    self.overlay_frame.show()
                    self.overlay_frame.raise_()
                    return True
                elif event.type() == event.Type.Leave:
                    self.overlay_timer.start(3000)
                    return True
                elif event.type() == event.Type.Paint:
                    painter = QPainter(self.overlay_frame)
                    gradient = QLinearGradient(0, 0, 0, self.overlay_frame.height())
                    gradient.setColorAt(0.7, QColor(0, 0, 0, 0))
                    gradient.setColorAt(1.0, QColor(0, 0, 0, 178))
                    painter.fillRect(self.overlay_frame.rect(), QBrush(gradient))
                    return True
            return super().eventFilter(obj, event)
        
        # Add elements to layout
        layout.addWidget(self.file_button)
        layout.addWidget(self.file_label)
        layout.addWidget(self.start_button)
        layout.addWidget(self.save_button)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.video_container)
        layout.addWidget(self.status_text)
        
        self.processing_thread = None
        self.video_path = None

    def select_file(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            "",
            "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*)"
        )
        
        if file_name:
            self.video_path = Path(file_name)
            self.file_label.setText(f"Selected: {self.video_path.name}")
            self.start_button.setEnabled(True)
            self.log_message(f"Selected video file: {self.video_path}")

    def start_processing(self):
        if not self.video_path:
            return
            
        self.start_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.progress_bar.setValue(0)
        
        self.processing_thread = ProcessingThread(self.video_path)
        self.processing_thread.progress_update.connect(self.update_progress)
        self.processing_thread.status_update.connect(self.log_message)
        self.processing_thread.processing_finished.connect(self.processing_complete)
        self.processing_thread.start()

    def update_progress(self, task: str, value: int):
        self.progress_bar.setValue(value)
        self.log_message(f"{task}: {value}%")

    def log_message(self, message: str):
        self.status_text.append(message)
        # Scroll to bottom
        scrollbar = self.status_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def processing_complete(self, success: bool, message: str):
        self.log_message(message)
        self.start_button.setEnabled(True)
        self.file_button.setEnabled(True)
        
        if success:
            self.progress_bar.setValue(100)
            # Show and play the video
            montage_path = Path('funny_montage.mp4')
            if montage_path.exists():
                self.video_container.show()
                self.overlay_frame.show()
                self.save_button.show()
                self.media_player.setSource(QUrl.fromLocalFile(str(montage_path.absolute())))
                self.media_player.play()
                self.play_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaPause))
                self.overlay_timer.start(3000)  # Hide overlay after 3 seconds
        else:
            self.progress_bar.setValue(0)
            self.video_container.hide()
            self.save_button.hide()

    def video_mouse_move(self, event):
        self.overlay_frame.show()
        self.overlay_timer.start(3000)  # Hide overlay after 3 seconds of inactivity
        
    def video_double_click(self, event):
        self.toggle_fullscreen()  # Double click to toggle fullscreen

    def toggle_playback(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            self.play_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaPlay))
        else:
            self.media_player.play()
            self.play_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaPause))
    
    def set_position(self, position):
        self.media_player.setPosition(position)
        
    def slider_mouse_move(self, event):
        # Show time preview when hovering over slider
        value = self.position_slider.minimum() + (self.position_slider.maximum() - self.position_slider.minimum()) * event.position().x() / self.position_slider.width()
        self.position_slider.setToolTip(self.format_time(int(value)))
    
    def format_time(self, ms):
        time = QTime(0, 0)
        time = time.addMSecs(ms)
        if time.hour() > 0:
            return time.toString('h:mm:ss')
        return time.toString('m:ss')
    
    def position_changed(self, position):
        self.position_slider.setValue(position)
        current = self.format_time(position)
        duration = self.format_time(self.media_player.duration())
        self.time_label.setText(f"{current} / {duration}")
    
    def duration_changed(self, duration):
        self.position_slider.setRange(0, duration)
        self.time_label.setText(f"0:00 / {self.format_time(duration)}")
    
    def set_volume(self, volume):
        self.audio_output.setVolume(volume / 100.0)
        # Update volume button icon based on volume level
        if volume == 0:
            self.volume_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaVolumeMuted))
        else:
            self.volume_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_MediaVolume))
            
    def toggle_mute(self):
        if self.audio_output.volume() > 0:
            self.previous_volume = self.volume_slider.value()
            self.volume_slider.setValue(0)
        else:
            self.volume_slider.setValue(self.previous_volume if hasattr(self, 'previous_volume') else 100)
            
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_TitleBarMaxButton))
        else:
            self.showFullScreen()
            self.fullscreen_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_TitleBarNormalButton))
            
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
        elif event.key() == Qt.Key.Key_F:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key.Key_M:
            self.toggle_mute()
        elif event.key() == Qt.Key.Key_Left:
            new_pos = max(0, self.media_player.position() - 5000)  # Back 5 seconds
            self.media_player.setPosition(new_pos)
            self.position_slider.setToolTip(self.format_time(new_pos))
        elif event.key() == Qt.Key.Key_Right:
            new_pos = min(self.media_player.duration(), self.media_player.position() + 5000)  # Forward 5 seconds
            self.media_player.setPosition(new_pos)
            self.position_slider.setToolTip(self.format_time(new_pos))
        elif event.key() == Qt.Key.Key_Up:
            self.volume_slider.setValue(min(100, self.volume_slider.value() + 5))
        elif event.key() == Qt.Key.Key_Down:
            self.volume_slider.setValue(max(0, self.volume_slider.value() - 5))
        elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_TitleBarMaxButton))
        else:
            super().keyPressEvent(event)
    
    def save_video(self):
        downloads_dir = str(Path.home() / "Downloads")
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Video",
            downloads_dir,
            "Video Files (*.mp4)"
        )
        
        if file_name:
            try:
                source_path = Path('funny_montage.mp4')
                target_path = Path(file_name)
                
                # Ensure the file has .mp4 extension
                if target_path.suffix.lower() != '.mp4':
                    target_path = target_path.with_suffix('.mp4')
                
                # Copy the file
                import shutil
                shutil.copy2(str(source_path), str(target_path))
                self.log_message(f"Video saved successfully to: {target_path}")
            except Exception as e:
                self.log_message(f"Error saving video: {str(e)}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to save video: {str(e)}"
                )
    
    def closeEvent(self, event):
        if self.processing_thread and self.processing_thread.isRunning():
            self.processing_thread.stop()
            self.processing_thread.wait()
        event.accept()


def check_ffmpeg_installed() -> bool:
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def install_ffmpeg():
    # Script content
    script_content = '''#!/bin/bash

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
'''
    
    # Create temporary script file
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        script_path = Path(f.name)
        f.write(script_content)
    
    try:
        # Make script executable
        os.chmod(script_path, 0o755)
        print(f"Created temporary script at: {script_path}")
        # Execute script
        cmd = ['bash', str(script_path)]
        print(f"Executing command: {cmd}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            return True, "FFmpeg installed successfully"
        else:
            error_msg = stderr.strip() if stderr else stdout.strip() if stdout else "Unknown error occurred"
            print(f"Process failed with error: {error_msg}")
            return False, f"Installation failed: {error_msg}"
        
    except subprocess.CalledProcessError as e:
        return False, f"Installation failed: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"
    finally:
        # Clean up temporary script
        try:
            os.unlink(script_path)
        except Exception:
            pass

def main():
    app = QApplication(sys.argv)
    
    # Check if ffmpeg is installed
    if not check_ffmpeg_installed():
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("FFmpeg Required")
        msg.setText("FFmpeg is required but not installed.")
        msg.setInformativeText("Would you like to install it now?")
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | 
            QMessageBox.StandardButton.No
        )
        
        if msg.exec() == QMessageBox.StandardButton.Yes:
            success, message = install_ffmpeg()
            if success:
                QMessageBox.information(
                    None,
                    "Success",
                    message
                )
            else:
                QMessageBox.critical(
                    None,
                    "Error",
                    message
                )
                sys.exit(1)
        else:
            sys.exit(1)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
