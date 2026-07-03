from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
import shutil
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import aiofiles
import os
from pathlib import Path
import asyncio
import json
from typing import List, Dict
import logging
import subprocess
import uuid
import time

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import logging.handlers

# Configure logging with rotating file handler
log_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)

# Application logger
app_handler = logging.handlers.RotatingFileHandler(
    'app.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
app_handler.setFormatter(log_formatter)

# Separate uvicorn access log
access_handler = logging.handlers.RotatingFileHandler(
    'access.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
access_handler.setFormatter(log_formatter)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(console_handler)
root_logger.addHandler(app_handler)

# Configure uvicorn loggers
logging.getLogger("uvicorn.access").handlers = [access_handler, console_handler]
logging.getLogger("uvicorn.error").handlers = [app_handler, console_handler]

# Get application logger
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize API clients
# Transcription runs on xAI (Grok) Speech-to-Text — custom /v1/stt endpoint,
# ~3.6x cheaper than OpenAI Whisper, returns word-level timestamps by default.
XAI_API_KEY = os.getenv('XAI_API_KEY')
XAI_STT_URL = "https://api.x.ai/v1/stt"

anthropic_client = AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# Model used for humor analysis
HUMOR_MODEL = "claude-sonnet-5"

# Global state for analysis
analysis_state = {
    'in_progress': False,
    'last_activity': None,
    'error': None,
    'clips': []
}

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """Verify ffmpeg installation on startup"""
    # Since ffmpeg is installed via DO App Platform packages, it should be in PATH
    ffmpeg_path = 'ffmpeg'
    logger.info("Checking ffmpeg installation at startup")
    
    try:
        # Try to run ffmpeg -version
        result = subprocess.run([ffmpeg_path, "-version"], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        logger.info(f"ffmpeg check successful: {result.stdout.splitlines()[0]}")
        # Set the environment variable to just 'ffmpeg' since it's in PATH
        os.environ['FFMPEG_PATH'] = ffmpeg_path
    except Exception as e:
        logger.critical(f"STARTUP ERROR: ffmpeg check failed: {str(e)}")
        logger.critical("Please ensure ffmpeg is installed and available in PATH")
        # Don't raise here - let the application start but log the error

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Serve the simple web frontend
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def frontend():
    """Serve the single-page web UI."""
    return FileResponse(STATIC_DIR / "index.html")


# A default prompt if none provided
DEFAULT_PROMPT = """You are a humor analysis system. Analyze these segments... {segments_text} ..."""

# Settings
MAX_RETRIES = 3
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


#################################
# HELPER: Transcription Function
#################################

def group_words_into_segments(words: List[Dict], max_segment_duration: float = 30.0) -> List[Dict]:
    """Group words into segments of specified maximum duration."""
    if not words:
        return []
        
    segments = []
    current_segment = {
        'start_time': words[0]['start'],
        'end_time': words[0]['end'],
        'words': [words[0]],
        'text': words[0]['text']
    }
    
    for word in words[1:]:
        # If adding this word would exceed max duration, start new segment
        if word['end'] - current_segment['start_time'] > max_segment_duration:
            segments.append(current_segment)
            current_segment = {
                'start_time': word['start'],
                'end_time': word['end'],
                'words': [word],
                'text': word['text']
            }
        else:
            current_segment['end_time'] = word['end']
            current_segment['words'].append(word)
            current_segment['text'] += ' ' + word['text']
            
    segments.append(current_segment)
    return segments

async def transcribe_chunks_batch(audio_paths: List[tuple[Path, float]], batch_size: int = 50) -> List[Dict]:
    """
    Transcribe multiple audio chunks concurrently using xAI (Grok) Speech-to-Text.
    Each audio_path tuple contains (path, offset)
    Returns a list of word-level info for all chunks.
    """
    async def process_single_chunk(audio_path: Path, offset: float) -> List[Dict]:
        if not audio_path.exists():
            logger.warning(f"Audio file does not exist: {audio_path}")
            return []

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Transcribing audio: {audio_path} (attempt {attempt + 1}/{MAX_RETRIES})")
                
                logger.info("Sending request to xAI (Grok) Speech-to-Text API...")
                async with httpx.AsyncClient(timeout=300.0) as http:
                    with open(audio_path, "rb") as audio:
                        stt_resp = await http.post(
                            XAI_STT_URL,
                            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                            files={"file": (audio_path.name, audio, "audio/mpeg")},
                            data={"language": "en"},
                        )
                    stt_resp.raise_for_status()
                    transcript_response = stt_resp.json()
                logger.info("Successfully received xAI STT response")
            
                # Extract words with timestamps
                logger.debug("Processing transcript response...")
                words = []
                if isinstance(transcript_response, dict):
                    logger.debug("Response is dictionary format")
                    words = (transcript_response.get('words', []) or 
                            transcript_response.get('segments', []) or 
                            transcript_response.get('word_segments', []))
                elif hasattr(transcript_response, 'words'):
                    logger.debug("Response has words attribute")
                    words = transcript_response.words
                
                logger.info(f"Found {len(words)} words in transcript")
                
                # Convert words to our format
                logger.debug("Converting words to standard format...")
                formatted_words = []
                for word in words:
                    try:
                        if isinstance(word, dict):
                            start = float(word.get('start', word.get('start_time', 0)))
                            end = float(word.get('end', word.get('end_time', 0)))
                            text = word.get('word', word.get('text', ''))
                        else:
                            start = float(getattr(word, 'start', getattr(word, 'start_time', 0)))
                            end = float(getattr(word, 'end', getattr(word, 'end_time', 0)))
                            text = getattr(word, 'word', getattr(word, 'text', ''))

                        # Adjust by offset
                        start += offset
                        end += offset

                        formatted_words.append({
                            'text': text,
                            'start': start,
                            'end': end
                        })
                    except Exception as e:
                        logger.error(f"Error processing word: {e}")
                        continue
                        
                logger.debug(f"Formatted {len(formatted_words)} words")
                return formatted_words

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    retry_delay = 5 * (attempt + 1)
                    logger.warning(f"Transcription attempt {attempt + 1} failed: {e}. Retrying in {retry_delay}s")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"All transcription retries failed for {audio_path}: {e}")
                    return []
        return []

    # Process chunks in batches
    all_words = []
    for i in range(0, len(audio_paths), batch_size):
        batch = audio_paths[i:i + batch_size]
        tasks = [process_single_chunk(path, offset) for path, offset in batch]
        batch_results = await asyncio.gather(*tasks)
        for words in batch_results:
            all_words.extend(words)
    
    return all_words


##############################
# HELPER: AI Analysis
##############################

async def analyze_humor_segments(segments: List[Dict], custom_prompt: str = None, batch_size: int = 50) -> List[Dict]:
    """
    Analyze segments for humor using Claude in batches.
    """
    if not segments:
        return []

    logger.info("\n=== Starting humor analysis ===")
    analyzed_segments = []
    
    async def analyze_single_segment(segment: Dict, index: int) -> Dict:
        logger.info(f"\nAnalyzing segment {index}/{len(segments)}")
        try:
            logger.info(f"Segment text: {segment['text'][:100]}...")

            prompt = f"""Analyze this text for humorous content and rate it:

{segment['text']}

Return your analysis as a JSON object with this exact format:
{{
    "score": <number 0-100 indicating how funny the text is overall>,
    "funny_moments": [
        {{
            "text": "<exact word-for-word quote of the funny part>",
            "reason": "<brief explanation of why this moment is humorous>"
        }}
    ]
}}

Consider elements like:
- Unexpected twists or surprises
- Clever wordplay or puns
- Amusing situations or scenarios
- Funny reactions or responses
- Irony or sarcasm
- Comedic timing in dialogue

Only include genuinely funny moments. If nothing is funny, return an empty funny_moments array.
Ensure the "text" field matches words exactly as they appear in the original text."""

            logger.info("Sending request to Claude...")
            try:
                # Thinking disabled: this is a high-volume, per-segment extraction
                # run 50-concurrent, so we optimize for throughput/cost. Sonnet 5
                # runs adaptive thinking by default when the field is omitted.
                response = await anthropic_client.messages.create(
                    model=HUMOR_MODEL,
                    max_tokens=2048,
                    thinking={"type": "disabled"},
                    system=(
                        "You are a humor analysis system. Respond with ONLY the "
                        "JSON object requested — no preamble, no code fences, no "
                        "commentary before or after."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in response.content if b.type == "text")
                logger.info("Received Claude response")
            except Exception as e:
                logger.error(f"Claude API error: {str(e)}")
                logger.info("Skipping this segment due to API error")
                return {
                    'start_time': segment['start_time'],
                    'end_time': segment['end_time'],
                    'text': segment['text'],
                    'humor_score': 0,
                    'funny_clips': []
                }

            try:
                # Extract JSON from response
                start_idx = text.find('{')
                end_idx = text.rfind('}') + 1

                if start_idx >= 0 and end_idx > start_idx:
                    try:
                        analysis = json.loads(text[start_idx:end_idx])
                        logger.info(f"Humor score: {analysis.get('score', 0)}")
                        logger.info(f"Found {len(analysis.get('funny_moments', []))} funny moments")
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON response: {e}")
                        raise
                else:
                    logger.error("No valid JSON found in response")
                    raise ValueError("No valid JSON in Claude response")
            except Exception as e:
                logger.error(f"Error processing Claude response: {str(e)}")
                logger.info("Skipping this segment due to processing error")
                return {
                    'start_time': segment['start_time'],
                    'end_time': segment['end_time'],
                    'text': segment['text'],
                    'humor_score': 0,
                    'funny_clips': []
                }

            funny_clips = []
            try:
                for moment in analysis.get('funny_moments', []):
                    try:
                        if not isinstance(moment, dict) or 'text' not in moment or 'reason' not in moment:
                            logger.warning("Skipping malformed funny moment")
                            continue

                        words = moment['text'].split()
                        for i in range(len(segment['words']) - len(words) + 1):
                            segment_words = segment['words'][i:i+len(words)]
                            if ' '.join(w['text'] for w in segment_words).lower() == ' '.join(words).lower():
                                clip = {
                                    'start_time': segment_words[0]['start'],
                                    'end_time': segment_words[-1]['end'],
                                    'text': moment['text'],
                                    'reason': moment['reason']
                                }
                                logger.info(f"Found funny clip: {clip['start_time']}s - {clip['end_time']}s")
                                funny_clips.append(clip)
                                break
                    except Exception as e:
                        logger.error(f"Error processing funny moment: {str(e)}")
                        continue
            except Exception as e:
                logger.error(f"Error processing funny moments array: {str(e)}")

            return {
                'start_time': segment['start_time'],
                'end_time': segment['end_time'],
                'text': segment['text'],
                'humor_score': analysis.get('score', 0),
                'funny_clips': funny_clips
            }

        except Exception as e:
            logger.error(f"Error analyzing segment: {str(e)}")
            return {
                'start_time': segment['start_time'],
                'end_time': segment['end_time'],
                'text': segment['text'],
                'humor_score': 0,
                'funny_clips': []
            }

    # Process segments in batches
    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        tasks = [analyze_single_segment(segment, idx) for idx, segment in enumerate(batch, i + 1)]
        batch_results = await asyncio.gather(*tasks)
        analyzed_segments.extend(batch_results)
        
        # Update activity timestamp after each batch
        analysis_state['last_activity'] = time.time()

    return analyzed_segments


##############################################
# ENDPOINT: Process entire video in one pass
##############################################

@app.post("/process-video-whole")
async def process_video_whole(
    file: UploadFile = File(...),
    video_duration: float = Form(...),
    custom_prompt: str = Form(None),
):
    """
    Receives the full video file once. We'll:
     1) Save the video to disk.
     2) Optionally chunk it using ffmpeg (by time).
     3) Transcribe each chunk, run AI analysis, gather funny/relevant clips.
     4) Return them to the client.
    """
    try:
        logger.info(f"Starting full-video chunk-based processing for file: {file.filename}")
        logger.info(f"Video duration: {video_duration} seconds")

        # 1. Save the full video to a temporary file
        file_id = str(uuid.uuid4())
        logger.info(f"Generated processing ID: {file_id}")
        temp_video_path = f"temp_{file_id}.mp4"
        logger.debug(f"Saving entire video to {temp_video_path}")
        async with aiofiles.open(temp_video_path, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        logger.debug("Video file saved successfully.")

        # 2. We'll do chunk-based audio extraction in a loop, for example:
        #    chunk ~2 minutes at a time
        chunk_duration = 120  # 2 minutes
        overlap = 0           # optional overlap in seconds
        start = 0.0
        step = chunk_duration - overlap
        all_clips = []

        while start < video_duration:
            # Collect audio chunks for batch processing (up to 50 at a time)
            audio_chunks = []
            current_start = start
            
            while current_start < video_duration and len(audio_chunks) < 50:
                current_end = min(current_start + chunk_duration, video_duration)
                audio_path = f"temp_{file_id}_{int(current_start)}.mp3"
                logger.info(f"Processing chunk {int(current_start/chunk_duration) + 1}: {current_start:.1f}s to {current_end:.1f}s => {audio_path}")
                
                ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
                ffmpeg_cmd = [
                    ffmpeg_path, "-y",
                    "-i", temp_video_path,
                    "-ss", str(current_start),
                    "-to", str(current_end),
                    "-vn",
                    "-acodec", "mp3",
                    audio_path
                ]
                try:
                    logger.debug(f"Running FFmpeg command: {' '.join(ffmpeg_cmd)}")
                    process = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True, env=os.environ)
                    logger.debug("FFmpeg chunk extraction completed successfully")
                    audio_chunks.append((Path(audio_path), current_start))
                except subprocess.CalledProcessError as e:
                    logger.error(f"FFmpeg error chunk [{current_start}-{current_end}]: {e.stderr}")
                    break
                
                current_start += step

            # 2b. Transcribe audio chunks in batches
            logger.info(f"Starting batch transcription for {len(audio_chunks)} chunks")
            words = await transcribe_chunks_batch(audio_chunks)
            logger.info(f"Batch transcription complete: {len(words)} words found")

            # Group words into segments for batch analysis
            segments = []
            current_segment_start = start
            current_segment_words = []
            
            for word in words:
                if word['end'] - current_segment_start > chunk_duration:
                    if current_segment_words:
                        segment_text = " ".join(w["text"] for w in current_segment_words)
                        segments.append({
                            "start_time": current_segment_start,
                            "end_time": current_segment_words[-1]['end'],
                            "words": current_segment_words,
                            "text": segment_text
                        })
                    current_segment_start = word['start']
                    current_segment_words = [word]
                else:
                    current_segment_words.append(word)
            
            if current_segment_words:
                segment_text = " ".join(w["text"] for w in current_segment_words)
                segments.append({
                    "start_time": current_segment_start,
                    "end_time": current_segment_words[-1]['end'],
                    "words": current_segment_words,
                    "text": segment_text
                })

            # Analyze segments in batches
            logger.info(f"Starting batch humor analysis for {len(segments)} segments")
            results = await analyze_humor_segments(segments, custom_prompt)
            logger.info(f"Batch humor analysis complete")
            
            # Process results
            for seg in results:
                if seg.get("humor_score", 0) >= 40:
                    all_clips.extend(seg.get("funny_clips", []))

            # Cleanup chunk audio files
            for audio_path, _ in audio_chunks:
                if os.path.exists(audio_path):
                    logger.debug(f"Cleaning up temporary audio file: {audio_path}")
                    os.remove(audio_path)

            start = current_start

        # Return all discovered clips and the file_id for montage creation
        logger.info(f"Video processing complete. Found {len(all_clips)} total clips.")
        logger.info("-------- End of video processing --------")
        return {"clips": all_clips, "file_id": file_id}

    except Exception as e:
        logger.error(f"Error in /process-video-whole: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

    finally:
        # Keep the temporary video file for montage creation
        # It will be cleaned up after montage is downloaded or after a timeout
        pass


##############################################
# ENDPOINT: Process a single audio chunk
# (Used when we do in-browser extraction)
##############################################

@app.post("/process-audio-chunk")
async def process_audio_chunk(
    file: UploadFile = File(...),
    chunk_start: float = Form(...),
    chunk_duration: float = Form(...),
    custom_prompt: str = Form(None),
    is_short_video: bool = Form(False),
):
    """
    Process an audio chunk and analyze it for humor.
    For short videos, only return timestamps without creating montage.
    """
    global analysis_state
    try:
        # Validate format
        if not file.filename.lower().endswith(('.mp3', '.wav', '.m4a', '.aac')):
            logger.error(f"Unsupported file format: {file.filename}")
            return JSONResponse(
                status_code=400,
                content={"error": "Unsupported file format. Must be MP3, WAV, M4A, or AAC."}
            )

        logger.info(f"Received in-browser audio chunk from {chunk_start}s to {chunk_start+chunk_duration}s")

        # Save to disk
        temp_audio_path = UPLOAD_DIR / f"inbrowser_{uuid.uuid4()}.mp3"
        async with aiofiles.open(temp_audio_path, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        try:
            logger.info("\n=== Starting Whisper transcription ===")
            words = []

            # Update activity timestamp
            analysis_state['last_activity'] = time.time()

            if is_short_video:
                # Process exactly like process-video-whole for short videos
                chunk_duration = 120  # 2 minutes
                overlap = 0
                start = 0.0
                step = chunk_duration - overlap
                all_clips = []
                current_start = start
                
                # Collect audio chunks for batch processing (up to 50 at a time)
                audio_chunks = []
                while current_start < chunk_duration and len(audio_chunks) < 50:
                    current_end = min(current_start + chunk_duration, chunk_duration)
                    audio_path = f"temp_{uuid.uuid4()}_{int(current_start)}.mp3"
                    
                    # Get ffmpeg path
                    ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
                    logger.info(f"Using ffmpeg path: {ffmpeg_path}")
                    
                    # Check if ffmpeg exists
                    try:
                        subprocess.run([ffmpeg_path, "-version"], capture_output=True, check=True)
                        logger.info("ffmpeg is available and working")
                    except Exception as e:
                        logger.error(f"Error checking ffmpeg: {str(e)}")
                        raise HTTPException(status_code=500, detail="ffmpeg is not available")

                    ffmpeg_cmd = [
                        ffmpeg_path, "-y",  # Add -y flag to overwrite output files
                        "-i", str(temp_audio_path),
                        "-ss", str(current_start),
                        "-to", str(current_end),
                        "-vn",
                        "-acodec", "mp3",
                        audio_path
                    ]
                    try:
                        logger.debug(f"Running FFmpeg command: {' '.join(ffmpeg_cmd)}")
                        process = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True, env=os.environ)
                        logger.debug("FFmpeg chunk extraction completed successfully")
                        audio_chunks.append((Path(audio_path), current_start))
                    except subprocess.CalledProcessError as e:
                        logger.error(f"FFmpeg error chunk [{current_start}-{current_end}]: {e.stderr}")
                        break
                    
                    current_start += step

                # Transcribe audio chunks in batches
                logger.info(f"Starting batch transcription for {len(audio_chunks)} chunks")
                words = await transcribe_chunks_batch(audio_chunks)
                logger.info(f"Batch transcription complete: {len(words)} words found")

                # Group words into segments for batch analysis
                segments = []
                current_segment_start = start
                current_segment_words = []
                
                for word in words:
                    if word['end'] - current_segment_start > chunk_duration:
                        if current_segment_words:
                            segment_text = " ".join(w["text"] for w in current_segment_words)
                            segments.append({
                                "start_time": current_segment_start,
                                "end_time": current_segment_words[-1]['end'],
                                "words": current_segment_words,
                                "text": segment_text
                            })
                        current_segment_start = word['start']
                        current_segment_words = [word]
                    else:
                        current_segment_words.append(word)
                
                if current_segment_words:
                    segment_text = " ".join(w["text"] for w in current_segment_words)
                    segments.append({
                        "start_time": current_segment_start,
                        "end_time": current_segment_words[-1]['end'],
                        "words": current_segment_words,
                        "text": segment_text
                    })

                # Analyze segments in batches
                logger.info(f"Starting batch humor analysis for {len(segments)} segments")
                analyzed_segments = await analyze_humor_segments(segments, custom_prompt)
                logger.info(f"Batch humor analysis complete")
                
                # Process results
                funny_clips = []
                for seg in analyzed_segments:
                    if seg.get("humor_score", 0) >= 40:
                        funny_clips.extend(seg.get("funny_clips", []))

                # Cleanup chunk audio files
                for audio_path, _ in audio_chunks:
                    if os.path.exists(audio_path):
                        logger.debug(f"Cleaning up temporary audio file: {audio_path}")
                        os.remove(audio_path)

            else:
                # Original processing for longer videos
                words = await transcribe_chunks_batch([(temp_audio_path, chunk_start)])
                logger.info(f"Transcription complete: {len(words)} words found")

                # Group words into segments
                segments = group_words_into_segments(words)
                logger.info(f"Created {len(segments)} segments")

                # Update activity timestamp
                analysis_state['last_activity'] = time.time()

                # Analyze segments for humor
                analyzed_segments = await analyze_humor_segments(segments, custom_prompt)

                logger.info("\n=== Processing final clips ===")
                # Filter and merge funny clips
                funny_clips = []
                for segment in analyzed_segments:
                    if segment['humor_score'] >= 40:
                        funny_clips.extend(segment['funny_clips'])

            logger.info(f"Found {len(funny_clips)} total funny clips")

            # Merge overlapping clips
            try:
                if funny_clips:
                    logger.info("\nMerging overlapping clips...")
                    max_gap = 2.0  # Maximum gap between clips to merge
                    min_duration = 3.0  # Minimum duration for a clip
                    
                    try:
                        sorted_clips = sorted(funny_clips, key=lambda x: x['start_time'])
                        merged = []
                        
                        if sorted_clips:  # Check if there are clips to process
                            current = sorted_clips[0]
                            
                            for next_clip in sorted_clips[1:]:
                                try:
                                    if next_clip['start_time'] - current['end_time'] <= max_gap:
                                        logger.info(f"Merging clips: {current['start_time']}s-{current['end_time']}s with {next_clip['start_time']}s-{next_clip['end_time']}s")
                                        current = {
                                            'start_time': current['start_time'],
                                            'end_time': next_clip['end_time'],
                                            'text': current['text'] + ' ' + next_clip['text'],
                                            'reason': current['reason'] + ' & ' + next_clip['reason']
                                        }
                                    else:
                                        if current['end_time'] - current['start_time'] >= min_duration:
                                            merged.append(current)
                                        current = next_clip
                                except Exception as e:
                                    logger.error(f"Error merging clip: {str(e)}")
                                    continue
                            
                            if current and current['end_time'] - current['start_time'] >= min_duration:
                                merged.append(current)
                        
                        # Sort by humor score and limit to top 30
                        try:
                            merged.sort(key=lambda x: x.get('humor_score', 0), reverse=True)
                            final_clips = merged[:30]
                        except Exception as e:
                            logger.error(f"Error sorting clips: {str(e)}")
                            final_clips = merged  # Use unsorted clips if sorting fails
                        
                        logger.info(f"Final number of clips after merging: {len(final_clips)}")
                    except Exception as e:
                        logger.error(f"Error in clip merging process: {str(e)}")
                        final_clips = funny_clips[:30]  # Use unmerged clips if merging fails
                else:
                    final_clips = []
                    logger.info("No funny clips found")
            except Exception as e:
                logger.error(f"Error in clip processing: {str(e)}")
                final_clips = []  # Fallback to empty clips list if all processing fails

            logger.info("\n=== Analysis complete ===")
            analysis_state['clips'] = final_clips
            analysis_state['in_progress'] = False
            analysis_state['error'] = None

            return {
                "chunk_start": chunk_start,
                "chunk_duration": chunk_duration,
                "clips": final_clips,
                "client_side_montage": is_short_video
            }

        except Exception as e:
            logger.error(f"Error in background analysis: {str(e)}")
            analysis_state['error'] = str(e)
            analysis_state['in_progress'] = False
            raise

        finally:
            try:
                os.unlink(temp_audio_path)
                logger.info(f"Cleaned up temporary file: {temp_audio_path}")
            except:
                pass

    except Exception as e:
        logger.error(f"Error in /process-audio-chunk: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


##########################
# Heartbeat
##########################

@app.post("/heartbeat")
async def heartbeat():
    """Simple heartbeat to keep the server awake."""
    logger.debug(f"Received heartbeat at {datetime.utcnow().isoformat()}")
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


##########################
# Run with Uvicorn
##########################

##########################
# HELPER: FFmpeg Helper
##########################

class FFmpegHelper:
    @staticmethod
    async def run_command(cmd: List[str]) -> tuple[bool, str]:
        """Run an FFmpeg command and return success status and error message."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            success = process.returncode == 0
            error = stderr.decode() if stderr else ''
            return success, error
        except Exception as e:
            return False, str(e)

##########################
# HELPER: File Manager
##########################

class FileManager:
    @staticmethod
    async def cleanup_files(paths: List[Path]) -> None:
        """Clean up temporary files and directories."""
        for path in paths:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except Exception as e:
                logger.error(f"Error cleaning up {path}: {e}")

##########################
# Montage Creation
##########################

async def create_montage_parallel(video_path: Path, segments: List[Dict], output_path: Path) -> bool:
    """Create a montage from video segments in parallel."""
    temp_dir = None
    try:
        ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')

        # Build a single filter_complex that trims each segment on exact frame
        # boundaries, resets its timestamps, and concatenates them. Doing the
        # whole montage in one decode/encode pass keeps audio and video locked
        # together — no keyframe snapping (which stream-copy causes) and no
        # cumulative A/V drift across clips.
        filter_parts = []
        concat_inputs = []
        n = 0
        for segment in segments:
            # 0.5s padding on each side, clamped to a valid range
            start_time = max(0.0, float(segment['start_time']) - 0.5)
            end_time = float(segment['end_time']) + 0.5
            if end_time <= start_time:
                logger.warning(f"Skipping segment with non-positive duration: {segment}")
                continue

            filter_parts.append(
                f"[0:v]trim=start={start_time:.3f}:end={end_time:.3f},"
                f"setpts=PTS-STARTPTS[v{n}];"
            )
            filter_parts.append(
                f"[0:a]atrim=start={start_time:.3f}:end={end_time:.3f},"
                f"asetpts=PTS-STARTPTS[a{n}];"
            )
            concat_inputs.append(f"[v{n}][a{n}]")
            logger.info(f"Segment {n}: {start_time:.2f}s -> {end_time:.2f}s")
            n += 1

        if n == 0:
            logger.error("No valid segments to build a montage from")
            return False

        filter_complex = (
            "".join(filter_parts)
            + "".join(concat_inputs)
            + f"concat=n={n}:v=1:a=1[outv][outa]"
        )

        logger.info(f"Building montage from {n} segment(s) in a single pass...")
        cmd = [
            ffmpeg_path,
            '-i', str(video_path),
            '-filter_complex', filter_complex,
            '-map', '[outv]', '-map', '[outa]',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
            '-movflags', '+faststart',
            str(output_path),
            '-y'
        ]
        success, error = await FFmpegHelper.run_command(cmd)

        if not success:
            logger.error(f"Failed to build montage: {error}")
            return False

        if not output_path.exists() or output_path.stat().st_size == 0:
            logger.error("Montage file was not created or is empty")
            return False

        logger.info(f"Successfully created montage at {output_path}")
        return True

    except Exception as e:
        logger.error(f"Error in create_montage_parallel: {e}")
        return False

    finally:
        # Clean up temporary files
        if temp_dir:
            await FileManager.cleanup_files([temp_dir])


@app.post("/create-montage")
async def create_montage_endpoint(
    segments: str = Form(...),
    file: UploadFile = File(None),
    file_id: str = Form(None),
    background_tasks: BackgroundTasks = None
):
    """Create a montage from the selected segments using either a new upload or existing video."""
    try:
        # Parse segments JSON
        segments_data = json.loads(segments)
        if not segments_data:
            raise HTTPException(status_code=400, detail="No segments provided")

        # If no file_id provided, handle new upload
        if not file_id:
            if not file:
                raise HTTPException(status_code=400, detail="Either file or file_id must be provided")
            
            file_id = str(uuid.uuid4())
            temp_video_path = f"temp_{file_id}.mp4"
            
            # Save uploaded video
            async with aiofiles.open(temp_video_path, 'wb') as out_file:
                content = await file.read()
                await out_file.write(content)
        else:
            # Use existing video file
            temp_video_path = f"temp_{file_id}.mp4"
            if not os.path.exists(temp_video_path):
                raise HTTPException(status_code=404, detail="Original video file not found")

        output_path = f"temp_{file_id}_montage.mp4"

        # Create montage using the provided function
        success = await create_montage_parallel(Path(temp_video_path), segments_data, Path(output_path))
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to create montage")

        # Clean up original video file
        if background_tasks:
            background_tasks.add_task(os.unlink, temp_video_path)
        else:
            os.unlink(temp_video_path)

        return {"file_id": file_id}

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid segments JSON")
    except Exception as e:
        logger.error(f"Error creating montage: {e}")
        raise HTTPException(status_code=500, detail=str(e))

##########################
# Download Montage
##########################

from fastapi.responses import StreamingResponse
from starlette.responses import Response
import mimetypes
import stat

@app.get("/download-montage/{file_id}")
async def download_montage(file_id: str, background_tasks: BackgroundTasks, download: bool = False, request: Request = None):
    """
    Stream or download the montage file.
    If download=True, schedule cleanup after 24 hours.
    """
    try:
        output_path = Path(f"temp_{file_id}_montage.mp4")
        original_video = Path(f"temp_{file_id}.mp4")
        
        if not output_path.exists():
            raise HTTPException(status_code=404, detail="Montage file not found")

        # Get file size
        stat_result = os.stat(output_path)
        file_size = stat_result.st_size

        # Only schedule cleanup if this is a download request
        if download:
            async def cleanup_files():
                await asyncio.sleep(24 * 60 * 60)  # Wait 24 hours
                for path in [output_path, original_video]:
                    try:
                        if path.exists():
                            path.unlink()
                            logger.info(f"Cleaned up temporary file: {path}")
                    except Exception as e:
                        logger.warning(f"Error cleaning up {path}: {e}")
                        
            background_tasks.add_task(cleanup_files)
            
            return FileResponse(
                path=output_path,
                filename=f"montage_{file_id}.mp4",
                media_type="video/mp4",
                content_disposition_type="attachment"
            )
        else:
            # For streaming, return the file with inline content disposition
            return FileResponse(
                path=output_path,
                media_type="video/mp4",
                content_disposition_type="inline",
                filename=f"montage_{file_id}.mp4"
            )
    except Exception as e:
        logger.error(f"Error serving montage file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
