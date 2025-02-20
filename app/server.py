from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
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

from openai import AsyncOpenAI
from google import genai
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
openai_client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# Global state for analysis
analysis_state = {
    'in_progress': False,
    'last_activity': None,
    'error': None,
    'clips': []
}

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

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

async def transcribe_chunk(audio_path: Path, offset: float = 0) -> List[Dict]:
    """
    Transcribe the given audio chunk using OpenAI Whisper API.
    Return a list of word-level info: [{ 'text': str, 'start': float, 'end': float }, ...]
    """
    if not audio_path.exists():
        logger.warning(f"Audio file does not exist: {audio_path}")
        return []

    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"Transcribing audio: {audio_path} (attempt {attempt + 1}/{MAX_RETRIES})")
            
            logger.info("Sending request to OpenAI Whisper API...")
            with open(audio_path, "rb") as audio:
                transcript_response = await openai_client.audio.transcriptions.create(
                    file=audio,
                    model="whisper-1",
                    response_format="verbose_json",
                    timestamp_granularities=["word"]
                )
            logger.info("Successfully received Whisper response")
            
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


##############################
# HELPER: AI Analysis
##############################

async def analyze_humor_segments(segments: List[Dict], custom_prompt: str = None) -> List[Dict]:
    """
    Analyze segments for humor using Gemini.
    """
    if not segments:
        return []

    logger.info("\n=== Starting humor analysis ===")
    analyzed_segments = []
    for i, segment in enumerate(segments, 1):
        logger.info(f"\nAnalyzing segment {i}/{len(segments)}")
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

        logger.info("Sending request to Gemini...")
        try:
            response = await asyncio.to_thread(
                lambda: gemini_client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt
                )
            )
            text = response.text
            logger.info("Received Gemini response")
        except Exception as e:
            logger.error(f"Gemini API error: {str(e)}")
            logger.info("Skipping this segment due to API error")
            analyzed_segments.append({
                'start_time': segment['start_time'],
                'end_time': segment['end_time'],
                'text': segment['text'],
                'humor_score': 0,
                'funny_clips': []
            })
            continue

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
                raise ValueError("No valid JSON in Gemini response")
        except Exception as e:
            logger.error(f"Error processing Gemini response: {str(e)}")
            logger.info("Skipping this segment due to processing error")
            analyzed_segments.append({
                'start_time': segment['start_time'],
                'end_time': segment['end_time'],
                'text': segment['text'],
                'humor_score': 0,
                'funny_clips': []
            })
            continue

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

        analyzed_segments.append({
            'start_time': segment['start_time'],
            'end_time': segment['end_time'],
            'text': segment['text'],
            'humor_score': analysis.get('score', 0),
            'funny_clips': funny_clips
        })

        # Update activity timestamp after each segment
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
            end = min(start + chunk_duration, video_duration)
            # 2a. Extract audio for [start, end] using ffmpeg
            audio_path = f"temp_{file_id}_{int(start)}.mp3"
            logger.info(f"Processing chunk {int(start/chunk_duration) + 1}: {start:.1f}s to {end:.1f}s => {audio_path}")
            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-i", temp_video_path,
                "-ss", str(start),
                "-to", str(end),
                "-vn",
                "-acodec", "mp3",
                audio_path
            ]
            try:
                logger.debug(f"Running FFmpeg command: {' '.join(ffmpeg_cmd)}")
                process = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
                logger.debug("FFmpeg chunk extraction completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(f"FFmpeg error chunk [{start}-{end}]: {e.stderr}")
                break

            # 2b. Transcribe that audio chunk (offset the word times by `start`)
            logger.info(f"Starting transcription for chunk {int(start/chunk_duration) + 1}")
            words = await transcribe_chunk(Path(audio_path), offset=start)
            logger.info(f"Transcription complete for chunk {int(start/chunk_duration) + 1}: {len(words)} words found")

            # 2c. We'll store each chunk as a segment. After the loop, we batch-analyze them.
            segment_text = " ".join(w["text"] for w in words)
            segment = {
                "start_time": start,
                "end_time": end,
                "words": words,
                "text": segment_text
            }

            # For demonstration, let's do immediate analysis per chunk
            # (Alternatively, gather all segments first and analyze once in a big batch.)
            logger.info(f"Starting humor analysis for chunk {int(start/chunk_duration) + 1}")
            results = await analyze_humor_segments([segment], custom_prompt)
            logger.info(f"Humor analysis complete for chunk {int(start/chunk_duration) + 1}")
            if results:
                # Check the first (and only) segment's humor score, if >= 40 => keep funny_clips
                seg = results[0]
                if seg.get("humor_score", 0) >= 40:
                    all_clips.extend(seg.get("funny_clips", []))

            # Cleanup chunk audio
            if os.path.exists(audio_path):
                logger.debug(f"Cleaning up temporary audio file: {audio_path}")
                os.remove(audio_path)

            start += step

        # Return all discovered clips
        logger.info(f"Video processing complete. Found {len(all_clips)} total clips.")
        logger.info("-------- End of video processing --------")
        return {"clips": all_clips}

    except Exception as e:
        logger.error(f"Error in /process-video-whole: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

    finally:
        # Remove the temporary video
        try:
            if os.path.exists(temp_video_path):
                os.remove(temp_video_path)
        except Exception as cleanup_err:
            logger.warning(f"Error removing temp video: {cleanup_err}")


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
):
    """
    Process an audio chunk and analyze it for humor.
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

            # Transcribe audio using OpenAI Whisper
            words = await transcribe_chunk(temp_audio_path, offset=chunk_start)
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
                "clips": final_clips
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

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
