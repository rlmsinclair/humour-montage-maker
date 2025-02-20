import sys
import asyncio
import json
import os
import subprocess
import platform
import tempfile
from pathlib import Path
from typing import List, Dict, Optional
import aiofiles
import aiohttp
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                            QPushButton, QProgressBar, QLabel, QFileDialog, QTextEdit,
                            QMessageBox, QSlider, QComboBox, QInputDialog, QLineEdit)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QUrl, QTime
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

# API settings
API_URL = "https://udder-app-api-te987.ondigitalocean.app"

class ProcessingThread(QThread):
    progress_update = pyqtSignal(str, int)
    status_update = pyqtSignal(str)
    processing_finished = pyqtSignal(bool, str)

    def __init__(self, video_path: Path, custom_prompt: str = None, montage_type: str = "Humor"):
        super().__init__()
        self.video_path = video_path
        self.custom_prompt = custom_prompt
        self.montage_type = montage_type.lower()
        self.running = True

    def run(self):
        asyncio.run(self.process_video())

    def stop(self):
        self.running = False

    async def process_video(self):
        try:
            # Use home directory for output file
            output_path = Path.home() / f'{self.montage_type}_montage.mp4'
            
            # Extract audio
            self.status_update.emit("Extracting audio...")
            audio_path = await self.extract_audio(self.video_path)
            if not audio_path:
                self.processing_finished.emit(False, "Error extracting audio!")
                return

            # Get audio duration
            self.status_update.emit("Getting audio duration...")
            duration = await self.get_audio_duration(audio_path)
            if duration is None:
                self.processing_finished.emit(False, "Could not determine audio duration!")
                return

            # Process audio in chunks
            self.status_update.emit("Processing audio in chunks...")
            chunk_duration = 60  # 60 seconds per chunk
            funny_clips = []
            
            async with aiohttp.ClientSession() as session:
                for start_time in range(0, int(duration), chunk_duration):
                    if not self.running:
                        break
                        
                    # Create temporary directory for chunks
                    temp_dir = Path(tempfile.mkdtemp(prefix='udder_chunks_'))
                    
                    # Create chunk in temp directory
                    chunk_path = temp_dir / f'chunk_{start_time}.mp3'
                    success = await self.create_audio_chunk(audio_path, start_time, chunk_duration, chunk_path)
                    if not success:
                        continue

                    try:
                        # Send chunk to API
                        self.status_update.emit(f"Processing chunk starting at {start_time}s...")
                        data = aiohttp.FormData()
                        data.add_field('file',
                            open(chunk_path, 'rb'),
                            filename=chunk_path.name,
                            content_type='audio/mpeg'
                        )
                        data.add_field('chunk_start', str(start_time))
                        data.add_field('chunk_duration', str(chunk_duration))
                        if self.custom_prompt:
                            data.add_field('custom_prompt', self.custom_prompt)
                        
                        async with session.post(f"{API_URL}/process-audio-chunk", data=data) as response:
                            if response.status != 200:
                                error_msg = await response.text()
                                self.status_update.emit(f"Error processing chunk: {error_msg}")
                                continue
                                
                            result = await response.json()
                            chunk_clips = result.get('clips', [])
                            funny_clips.extend(chunk_clips)
                            
                            progress = min(100, int((start_time + chunk_duration) / duration * 100))
                            self.progress_update.emit("Processing audio", progress)
                    finally:
                        # Clean up chunk file
                        if chunk_path.exists():
                            os.remove(chunk_path)

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
                    self.processing_finished.emit(True, f"Successfully created {self.montage_type}_montage.mp4!")
                else:
                    self.processing_finished.emit(False, "Error creating montage!")
            else:
                self.processing_finished.emit(False, "No funny clips found!")

        except Exception as e:
            self.processing_finished.emit(False, f"An error occurred: {e}")
        finally:
            # Cleanup temporary files
            await self.cleanup_files([audio_path, temp_dir])

    @staticmethod
    def get_ffmpeg_path() -> str:
        # Try system PATH first since installer ensures FFmpeg is installed
        try:
            result = subprocess.run(['which', 'ffmpeg'], 
                                 capture_output=True, 
                                 text=True, 
                                 check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass

        # Common installation locations as fallback
        possible_paths = [
            '/opt/homebrew/bin/ffmpeg',  # Homebrew on Apple Silicon
            '/usr/local/bin/ffmpeg',     # Homebrew on Intel Mac
            '/usr/bin/ffmpeg',           # System install
        ]
        
        for path in possible_paths:
            if os.path.isfile(path):
                return path

        # Default to just 'ffmpeg' and let the system handle it
        return 'ffmpeg'

    async def get_audio_duration(self, audio_path: Path) -> Optional[float]:
        try:
            ffmpeg_path = self.get_ffmpeg_path()
            # Use ffprobe to get duration
            cmd = [
                ffmpeg_path.replace('ffmpeg', 'ffprobe'),
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(audio_path)
            ]
            
            self.status_update.emit(f"Getting duration using command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and stdout:
                duration = float(stdout.decode().strip())
                self.status_update.emit(f"Audio duration: {duration} seconds")
                return duration
            else:
                self.status_update.emit("Failed to get audio duration")
                return None
                
        except Exception as e:
            self.status_update.emit(f"Error getting audio duration: {e}")
            return None

    async def create_audio_chunk(self, audio_path: Path, start_time: int, chunk_duration: int, output_path: Path) -> bool:
        try:
            ffmpeg_path = self.get_ffmpeg_path()
            cmd = [
                ffmpeg_path, '-i', str(audio_path),
                '-ss', str(start_time),
                '-t', str(chunk_duration),
                '-acodec', 'libmp3lame',
                '-ac', '1', '-ar', '16000',
                '-y', str(output_path)
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and output_path.exists():
                self.status_update.emit(f"Successfully created chunk at {start_time}s")
                return True
            else:
                self.status_update.emit(f"Failed to create chunk at {start_time}s")
                return False
                
        except Exception as e:
            self.status_update.emit(f"Error creating audio chunk: {e}")
            return False

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
        temp_dir = Path(tempfile.mkdtemp(prefix='udder_clips_'))
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
        self.setMinimumSize(900, 800)
        
        # Create central widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # Create and style UI elements
        self.file_button = QPushButton("Select Video File")
        self.file_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.file_button.clicked.connect(self.select_file)
        
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: white; margin: 8px 0;")
        
        # Add montage type selector
        self.montage_type_label = QLabel("Montage Type:")
        self.montage_type_label.setStyleSheet("color: white; margin: 8px 0;")
        
        self.montage_type_selector = QComboBox()
        self.montage_type_selector.setStyleSheet("""
            QComboBox {
                border: 2px solid #e0e0e0;
                border-radius: 6px;
                padding: 8px 12px;
                background: white;
                min-width: 200px;
                font-size: 14px;
                color: #333333;
            }
            QComboBox:hover {
                border-color: #2196F3;
            }
            QComboBox:focus {
                border-color: #2196F3;
                outline: none;
            }
            QComboBox::drop-down {
                border: none;
                width: 30px;
            }
            QComboBox::down-arrow {
                width: 12px;
                height: 12px;
                margin-right: 8px;
                border: 2px solid #666666;
                border-width: 0 2px 2px 0;
                transform: rotate(45deg);
            }
            QComboBox:hover::down-arrow {
                border-color: #2196F3;
            }
            QComboBox QAbstractItemView {
                border: 2px solid #e0e0e0;
                border-radius: 6px;
                padding: 4px;
                background: white;
                selection-background-color: #2196F3;
                selection-color: white;
            }
            QComboBox QAbstractItemView::item {
                padding: 8px;
                margin: 2px;
                border-radius: 4px;
            }
            QComboBox QAbstractItemView::item:hover {
                background: #e3f2fd;
            }
        """)
        self.montage_type_selector.addItems(["Humor", "Drama", "Educational", "Inspiration", "Action"])
        self.montage_type_selector.currentTextChanged.connect(self.update_prompt)
        
        self.start_button = QPushButton("Start Processing")
        self.start_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.start_button.clicked.connect(self.start_processing)
        self.start_button.setEnabled(False)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMinimumHeight(40)
        self.status_text.setMaximumHeight(40)
        self.status_text.setStyleSheet("""
            QTextEdit {
                color: black;
            }
        """)
        
        # Create video player
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(300)
        self.video_widget.hide()
        
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        
        # Create video controls
        self.video_controls = QWidget()
        self.video_controls.hide()
        controls_layout = QHBoxLayout(self.video_controls)
        
        self.play_button = QPushButton("Play")
        self.play_button.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
        """)
        self.play_button.clicked.connect(self.toggle_playback)
        
        # Position slider and time labels
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.set_position)
        
        self.time_label = QLabel("0:00 / 0:00")
        
        # Volume control
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setMaximumWidth(100)
        self.volume_slider.valueChanged.connect(self.set_volume)
        
        self.media_player.positionChanged.connect(self.position_changed)
        self.media_player.durationChanged.connect(self.duration_changed)
        
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.position_slider)
        controls_layout.addWidget(self.time_label)
        controls_layout.addWidget(QLabel("Volume:"))
        controls_layout.addWidget(self.volume_slider)
        
        # Add prompt editor
        self.prompt_label = QLabel("Custom Prompt (advanced):")
        self.prompt_label.setStyleSheet("color: white; margin: 8px 0;")
        
        # Default prompt text from server
        self.default_prompt = """You are a humor analysis system. Analyze these segments of a transcribed video which may have multiple speakers for humorous content and rate them:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how funny the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the funny part>",
                    "reason": "<brief explanation of why this moment is humorous>"
                }
            ]
        }
    ]
}

Consider elements like:
- Unexpected twists or surprises
- Clever wordplay or puns
- Amusing situations or scenarios
- Funny reactions or responses
- Irony or sarcasm
- Comedic timing in dialogue

Only include genuinely funny moments. If nothing is funny, return an empty funny_moments array.
Ensure the "text" field matches words exactly as they appear in the original text."""

        class PromptEditor(QTextEdit):
            def __init__(self, default_text, parent=None):
                super().__init__(parent)
                self._default_text = default_text
                self.setMinimumHeight(100)
                self.setStyleSheet("""
                    QTextEdit {
                        border: 1px solid #cccccc;
                        border-radius: 4px;
                        padding: 8px;
                        background-color: white;
                        color: black;
                    }
                """)
                # Use QTimer to set text after initialization
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self.setPlainText(self._default_text))

        self.prompt_editor = PromptEditor(self.default_prompt, self)
        
        # Define montage prompts
        self.montage_prompts = {
            "Humor": """You are a humor analysis system. Analyze these segments of a transcribed video which may have multiple speakers for humorous content and rate them:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how funny the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the funny part>",
                    "reason": "<brief explanation of why this moment is humorous>"
                }
            ]
        }
    ]
}

Consider elements like:
- Unexpected twists or surprises
- Clever wordplay or puns
- Amusing situations or scenarios
- Funny reactions or responses
- Irony or sarcasm
- Comedic timing in dialogue

Only include genuinely funny moments. If nothing is funny, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text.""",

            "Drama": """You are a drama analysis system. Analyze these segments for dramatic, tense, or emotionally powerful moments and rate them:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how dramatic the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the dramatic part>",
                    "reason": "<brief explanation of why this moment is dramatic>"
                }
            ]
        }
    ]
}

Consider elements like:
- Intense confrontations or revelations
- Emotional breakthroughs
- Suspenseful moments
- Powerful personal stories
- Major plot developments
- Dramatic pauses or silence

Only include genuinely dramatic moments. If nothing is dramatic, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text.""",

            "Educational": """You are an educational content analyzer. Review these segments for valuable learning moments and key insights:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how educational the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the educational part>",
                    "reason": "<brief explanation of why this moment is educational>"
                }
            ]
        }
    ]
}

Consider elements like:
- Clear explanations of complex topics
- Practical demonstrations
- Important facts or statistics
- Memorable analogies or examples
- Expert insights
- Teachable moments

Only include genuinely educational moments. If nothing is educational, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text.""",

            "Inspiration": """You are an inspiration detector. Analyze these segments for motivational and uplifting content:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how inspirational the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the inspirational part>",
                    "reason": "<brief explanation of why this moment is inspirational>"
                }
            ]
        }
    ]
}

Consider elements like:
- Personal triumph stories
- Overcoming challenges
- Words of wisdom
- Positive messages
- Acts of kindness
- Moments of achievement

Only include genuinely inspirational moments. If nothing is inspirational, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text.""",

            "Action": """You are an action sequence analyzer. Review these segments for exciting and dynamic moments:

{segments_text}

Return your analysis as a JSON object with this exact format:
{
    "segments": [
        {
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how action-packed the text is overall>,
            "moments": [
                {
                    "text": "<Lengthy, exact word-for-word quote of the action part>",
                    "reason": "<brief explanation of why this moment is action-packed>"
                }
            ]
        }
    ]
}

Consider elements like:
- Fast-paced sequences
- Physical activities
- Surprising moves or tricks
- Skilled performances
- High-energy moments
- Impressive feats

Only include genuinely action-packed moments. If nothing is action-packed, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text."""
        }

        # Set default prompt
        self.default_prompt = self.montage_prompts["Humor"]

        # Add elements to layout
        layout.addWidget(self.file_button)
        layout.addWidget(self.file_label)
        layout.addWidget(self.montage_type_label)
        layout.addWidget(self.montage_type_selector)
        layout.addWidget(self.prompt_label)
        layout.addWidget(self.prompt_editor)
        layout.addWidget(self.start_button)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.video_controls)
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
        
        montage_type = self.montage_type_selector.currentText()
        custom_prompt = self.prompt_editor.toPlainText().strip() or None
        self.processing_thread = ProcessingThread(self.video_path, custom_prompt, montage_type)
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

    def save_video(self):
        montage_type = self.montage_type_selector.currentText().lower()
        montage_path = Path.home() / f'{montage_type}_montage.mp4'
        if not montage_path.exists():
            return
            
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Video",
            str(self.video_path.stem + f"_{montage_type}_montage.mp4" if self.video_path else f"{montage_type}_montage.mp4"),
            "Video Files (*.mp4)"
        )
        
        if file_name:
            try:
                import shutil
                shutil.copy2(str(montage_path), file_name)
                self.log_message(f"Successfully saved video to: {file_name}")
            except Exception as e:
                self.log_message(f"Error saving video: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to save video: {str(e)}"
                )

    def processing_complete(self, success: bool, message: str):
        self.log_message(message)
        self.start_button.setEnabled(True)
        self.file_button.setEnabled(True)
        
        if success:
            self.progress_bar.setValue(100)
            self.progress_bar.hide()
            # Show and play the video
            montage_path = Path.home() / f'{self.montage_type_selector.currentText().lower()}_montage.mp4'
            if montage_path.exists():
                # Add download button
                download_button = QPushButton("Save Video")
                download_button.setStyleSheet("""
                    QPushButton {
                        background-color: #FF4081;
                        color: white;
                        border: none;
                        padding: 12px 24px;
                        border-radius: 6px;
                        font-size: 16px;
                        font-weight: bold;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.2);
                        transition: all 0.3s ease;
                    }
                    QPushButton:hover {
                        background-color: #FF80AB;
                        transform: scale(1.05);
                        box-shadow: 0 4px 8px rgba(0,0,0,0.3);
                    }
                """)
                download_button.clicked.connect(self.save_video)
                self.centralWidget().layout().insertWidget(
                    self.centralWidget().layout().indexOf(self.start_button) + 1,
                    download_button
                )
                
                # Trigger save dialog immediately
                self.save_video()
                
                # Show video player
                self.video_widget.show()
                self.video_controls.show()
                self.media_player.setSource(QUrl.fromLocalFile(str(montage_path.absolute())))
                self.media_player.play()
                self.play_button.setText("Pause")
        else:
            self.progress_bar.setValue(0)
            self.video_widget.hide()
            self.video_controls.hide()

    def toggle_playback(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            self.play_button.setText("Play")
        else:
            self.media_player.play()
            self.play_button.setText("Pause")
    
    def set_position(self, position):
        self.media_player.setPosition(position)
    
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
    
    def update_prompt(self, montage_type: str):
        self.prompt_editor.setPlainText(self.montage_prompts[montage_type])
        
    def closeEvent(self, event):
        if self.processing_thread and self.processing_thread.isRunning():
            self.processing_thread.stop()
            self.processing_thread.wait()
        event.accept()


def check_ffmpeg_installed() -> bool:
    # First check if ffmpeg is in PATH
    try:
        result = subprocess.run(['which', 'ffmpeg'], capture_output=True, text=True, check=True)
        ffmpeg_path = result.stdout.strip()
        if ffmpeg_path:
            try:
                subprocess.run([ffmpeg_path, '-version'], capture_output=True, check=True)
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
    except subprocess.CalledProcessError:
        pass

    # Check common installation paths
    ffmpeg_paths = [
        '/opt/homebrew/bin/ffmpeg',  # Homebrew on Apple Silicon
        '/usr/local/bin/ffmpeg',     # Homebrew on Intel Mac
        '/usr/bin/ffmpeg',           # System install
    ]
    
    for path in ffmpeg_paths:
        try:
            if os.path.isfile(path):
                subprocess.run([path, '-version'], capture_output=True, check=True)
                return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    
    return False

def get_sudo_password(parent=None) -> str:
    """Get sudo password from user using Qt dialog."""
    password, ok = QInputDialog.getText(
        parent,
        "Authentication Required",
        "Enter your password to install FFmpeg:",
        QLineEdit.EchoMode.Password
    )
    return password if ok else ""

def show_dependency_instructions(parent=None):
    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setWindowTitle("Dependencies Required")
    msg.setText("FFmpeg is required but not installed.")
    msg.setInformativeText(
        "To install the required dependencies:\n\n"
        "1. Install Homebrew by visiting:\n"
        "   https://brew.sh\n\n"
        "2. Open Terminal and run:\n"
        "   brew install ffmpeg\n\n"
        "After installing, restart the application."
    )
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    return msg.exec()

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.prompt_editor.setPlainText(window.default_prompt)
    
    # Check if ffmpeg is installed
    if not check_ffmpeg_installed():
        show_dependency_instructions(window)
        sys.exit(1)
    
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
