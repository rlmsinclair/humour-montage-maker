from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
import aiofiles
import os
from pathlib import Path
import asyncio
import json
from typing import List, Dict
import hashlib
import time
import logging
from openai import AsyncOpenAI
from google import genai
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('api.log')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
openai_client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
gemini_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

app = FastAPI()

# Settings
CHUNK_DURATION = 60  # Duration in seconds per chunk
MAX_CONCURRENT_CHUNKS = 4
MAX_RETRIES = 3
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

class FFmpegHelper:
    @staticmethod
    async def run_command(cmd: List[str]) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            return process.returncode == 0, stderr.decode()
        except Exception as e:
            return False, str(e)

    @staticmethod
    async def get_duration(file_path: Path) -> float | None:
        try:
            # First, verify the file exists and get format info
            format_cmd = [
                'ffprobe', '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                str(file_path)
            ]
            process = await asyncio.create_subprocess_exec(
                *format_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if stderr:
                logger.error(f"FFprobe format error: {stderr.decode()}")
                return None
                
            if stdout:
                try:
                    format_info = json.loads(stdout.decode())
                    if 'format' in format_info:
                        logger.info(f"Detected format: {format_info['format'].get('format_name', 'unknown')}")
                except json.JSONDecodeError:
                    logger.error("Failed to parse FFprobe format info")
            
            # Then get duration with more verbose error logging
            cmd = [
                'ffprobe', '-v', 'warning',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                '-i', str(file_path)
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if stderr:
                logger.error(f"FFprobe duration error: {stderr.decode()}")
            
            if process.returncode == 0 and stdout:
                duration = float(stdout.decode().strip())
                logger.info(f"Successfully determined duration: {duration} seconds")
                return duration
            else:
                logger.error(f"FFprobe failed with return code: {process.returncode}")
                
        except Exception as e:
            logger.error(f"Error getting duration: {str(e)}", exc_info=True)
        return None

async def split_audio_into_chunks(audio_path: Path) -> List[Path]:
    logger.info(f"Starting to split audio file: {audio_path}")
    chunks_dir = Path('temp_audio_chunks')
    chunks_dir.mkdir(exist_ok=True)

    try:
        duration = await FFmpegHelper.get_duration(audio_path)
        if not duration:
            logger.error("Could not determine audio duration")
            return []
        logger.info(f"Audio duration: {duration} seconds")

        chunk_paths = []
        semaphore = asyncio.Semaphore(4)

        async def create_chunk(start_time: int) -> Path | None:
            async with semaphore:
                chunk_path = chunks_dir / f'chunk_{start_time}.mp3'
                logger.debug(f"Creating chunk at {start_time}s -> {chunk_path}")

                cmd = [
                    'ffmpeg', '-i', str(audio_path),
                    '-ss', str(start_time),
                    '-t', str(CHUNK_DURATION),
                    '-acodec', 'libmp3lame',
                    '-ac', '1', '-ar', '16000',
                    '-y', str(chunk_path)
                ]

                success, error = await FFmpegHelper.run_command(cmd)
                if success and chunk_path.exists():
                    logger.debug(f"Successfully created chunk at {start_time}s")
                    return chunk_path
                else:
                    logger.warning(f"Failed to create chunk at {start_time}s: {error}")
                    return None

        tasks = []
        for start_time in range(0, int(duration), CHUNK_DURATION):
            tasks.append(create_chunk(start_time))
        logger.info(f"Created {len(tasks)} chunk tasks")

        results = await asyncio.gather(*tasks)
        chunk_paths = [path for path in results if path is not None]
        logger.info(f"Successfully created {len(chunk_paths)} chunks")

        return sorted(chunk_paths)
    except Exception as e:
        logger.error(f"Error splitting audio into chunks: {e}", exc_info=True)
        return []

async def transcribe_chunk(chunk_path: Path, offset: float = 0) -> List[Dict]:
    if not chunk_path.exists():
        logger.warning(f"Chunk file does not exist: {chunk_path}")
        return []

    for attempt in range(MAX_RETRIES):
        try:
            logger.debug(f"Transcribing chunk: {chunk_path} (attempt {attempt + 1}/{MAX_RETRIES})")
            with open(chunk_path, "rb") as audio_file:
                response = await openai_client.audio.transcriptions.create(
                    file=audio_file,
                    model="whisper-1",
                    response_format="verbose_json",
                    timestamp_granularities=["word"]
                )
            logger.debug(f"Successfully got transcription response for {chunk_path}")

            words = []
            if isinstance(response, dict):
                words = (response.get('words', []) or
                        response.get('segments', []) or
                        response.get('word_segments', []))
            elif hasattr(response, 'words'):
                words = response.words
            elif hasattr(response, 'segments'):
                words = response.segments
            logger.debug(f"Extracted {len(words)} words from response")

            result = []
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

                    result.append({
                        'text': text,
                        'start': offset + start,
                        'end': offset + end
                    })
                except Exception as e:
                    logger.warning(f"Failed to process word: {e}")
                    continue

            logger.debug(f"Successfully processed {len(result)} words for chunk {chunk_path}")
            return result

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                retry_delay = 5 * (attempt + 1)
                logger.warning(f"Attempt {attempt + 1} failed for chunk {chunk_path}: {e}. Retrying in {retry_delay}s")
                await asyncio.sleep(retry_delay)
            else:
                logger.error(f"All retries failed for chunk {chunk_path}: {e}")
                return []
    return []

async def analyze_humor_segments(segments: List[Dict], batch_size: int = 5) -> List[Dict]:
    logger.info(f"Starting humor analysis for {len(segments)} segments with batch size {batch_size}")
    results = []
    semaphore = asyncio.Semaphore(2)

    # Group segments into batches
    batches = [segments[i:i + batch_size] for i in range(0, len(segments), batch_size)]
    logger.info(f"Created {len(batches)} batches")

    async def analyze_batch(batch: List[Dict]) -> List[Dict]:
        async with semaphore:
            try:
                logger.debug(f"Analyzing batch of {len(batch)} segments")
                segments_text = "\n---\n".join(
                    [f"Segment {i + 1}: {s['text']}" for i, s in enumerate(batch)])

                prompt = f"""You are a humor analysis system. Analyze these segments of a transcribed video which may have multiple speakers for humorous content and rate them:

{segments_text}

Return your analysis as a JSON object with this exact format:
{{
    "segments": [
        {{
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how funny the text is overall>,
            "funny_moments": [
                {{
                    "text": "<Lengthy, exact word-for-word quote of the funny part>",
                    "reason": "<brief explanation of why this moment is humorous>"
                }}
            ]
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

                logger.debug("Sending request to Gemini API")
                response = await asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model="gemini-2.0-flash",
                    contents=prompt
                )
                logger.debug("Received response from Gemini API")

                text = response.text.strip()
                start_idx = text.find('{')
                end_idx = text.rfind('}') + 1
                json_str = text[start_idx:end_idx] if start_idx >= 0 and end_idx > start_idx else '{"segments": []}'

                try:
                    analysis = json.loads(json_str)
                    batch_results = []
                    logger.debug(f"Successfully parsed JSON response with {len(analysis.get('segments', []))} segments")

                    for segment_analysis in analysis.get('segments', []):
                        logger.debug(f"Processing segment {segment_analysis.get('segment_number', '?')} with score {segment_analysis.get('score', 0)}")
                        segment_num = segment_analysis.get('segment_number', 1) - 1
                        if segment_num < 0 or segment_num >= len(batch):
                            continue

                        segment = batch[segment_num]
                        funny_clips = []

                        for moment in segment_analysis.get('funny_moments', []):
                            try:
                                words = moment.get('text', '').split()
                                for i in range(len(segment.get('words', [])) - len(words) + 1):
                                    segment_words = segment['words'][i:i + len(words)]
                                    if ' '.join(w.get('text', '') for w in segment_words).lower() == ' '.join(words).lower():
                                        funny_clips.append({
                                            'start_time': segment_words[0].get('start', 0),
                                            'end_time': segment_words[-1].get('end', 0),
                                            'text': moment.get('text', ''),
                                            'reason': moment.get('reason', 'Unknown reason')
                                        })
                                        break
                            except Exception:
                                continue

                        batch_results.append({
                            **segment,
                            'humor_score': segment_analysis.get('score', 0),
                            'funny_clips': funny_clips
                        })

                    logger.debug(f"Successfully processed batch with {len(batch_results)} results")
                    return batch_results

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON response: {e}")
                    return [{**segment, 'humor_score': 0, 'funny_clips': []} for segment in batch]

            except Exception as e:
                logger.error(f"Error analyzing batch: {e}", exc_info=True)
                return [{**segment, 'humor_score': 0, 'funny_clips': []} for segment in batch]

    # Process batches
    logger.info("Processing all batches")
    batch_results = await asyncio.gather(*[analyze_batch(batch) for batch in batches])
    
    # Flatten results
    for batch in batch_results:
        results.extend(batch)

    logger.info(f"Completed humor analysis with {len(results)} total results")
    return results

def group_words_into_segments(words: List[Dict], segment_duration: int = 120, overlap: int = 20) -> List[Dict]:
    segments = []
    if not words:
        return segments
        
    total_duration = words[-1]['end']

    for segment_start in range(0, int(total_duration), segment_duration - overlap):
        segment_words = []
        segment_end = segment_start + segment_duration
        padding = 5

        for word in words:
            if (word['start'] >= segment_start - padding and
                    word['end'] <= segment_end + padding):
                segment_words.append(word)

        if segment_words:
            segments.append({
                'start_time': segment_start,
                'end_time': segment_end,
                'words': segment_words,
                'text': ' '.join(w['text'] for w in segment_words)
            })

    return segments

@app.post("/process-audio")
async def process_audio(file: UploadFile = File(...)):
    try:
        logger.info(f"Received audio file: {file.filename}")
        
        # Save uploaded file
        file_path = UPLOAD_DIR / file.filename
        async with aiofiles.open(file_path, 'wb') as f:
            content = await file.read()
            await f.write(content)
        logger.info(f"Saved file to {file_path}")

        # Split audio into chunks
        logger.info("Starting audio chunking process")
        chunks = await split_audio_into_chunks(file_path)
        if not chunks:
            logger.error("Failed to split audio into chunks")
            return JSONResponse(
                status_code=400,
                content={"error": "Failed to split audio into chunks"}
            )
        logger.info(f"Successfully split audio into {len(chunks)} chunks")

        # Transcribe chunks
        logger.info("Starting transcription process")
        all_words = []
        for i, chunk in enumerate(chunks):
            logger.info(f"Transcribing chunk {i+1}/{len(chunks)}")
            start_time = float(chunk.stem.split('_')[1])
            words = await transcribe_chunk(chunk, start_time)
            all_words.extend(words)
        logger.info(f"Transcription complete, got {len(all_words)} words")

        # Group words into segments
        logger.info("Grouping words into segments")
        segments = group_words_into_segments(sorted(all_words, key=lambda x: x['start']))
        logger.info(f"Created {len(segments)} segments")
        
        # Analyze humor
        logger.info("Starting humor analysis")
        analyzed_segments = await analyze_humor_segments(segments)
        logger.info(f"Completed humor analysis for {len(analyzed_segments)} segments")

        # Extract funny clips
        logger.info("Extracting funny clips")
        funny_clips = []
        for segment in analyzed_segments:
            try:
                humor_score = segment.get('humor_score', 0)
                clips = segment.get('funny_clips', [])
                if humor_score >= 40:
                    funny_clips.extend(clips)
            except Exception as e:
                logger.error(f"Error processing segment: {e}")
                continue
        logger.info(f"Found {len(funny_clips)} funny clips")

        # Prepare response
        response = {"clips": funny_clips}
        logger.info("Processing complete, preparing to send response")

        # Send response first
        await asyncio.create_task(cleanup_files(file_path, chunks))
        return response

    except Exception as e:
        logger.error(f"Error processing audio: {e}", exc_info=True)
        # Ensure cleanup happens even on error
        if 'file_path' in locals() and 'chunks' in locals():
            await asyncio.create_task(cleanup_files(file_path, chunks))
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

async def cleanup_files(file_path: Path, chunks: List[Path]):
    """Cleanup all temporary files after response is sent."""
    try:
        logger.info("Starting post-response cleanup")
        
        # Clean up uploaded file
        if file_path.exists():
            os.remove(file_path)
            logger.debug(f"Removed uploaded file: {file_path}")

        # Clean up chunk files
        for chunk in chunks:
            try:
                if chunk.exists():
                    os.remove(chunk)
                    logger.debug(f"Removed chunk file: {chunk}")
            except Exception as e:
                logger.warning(f"Failed to remove chunk file {chunk}: {e}")

        # Clean up cache files
        cache_dir = Path("cache")
        if cache_dir.exists():
            for cache_file in cache_dir.glob("*.json"):
                try:
                    os.remove(cache_file)
                    logger.debug(f"Removed cache file: {cache_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove cache file {cache_file}: {e}")

        # Clean up empty directories
        for dir_path in [UPLOAD_DIR, Path("temp_audio_chunks"), cache_dir]:
            try:
                if dir_path.exists() and not any(dir_path.iterdir()):
                    dir_path.rmdir()
                    logger.debug(f"Removed empty directory: {dir_path}")
            except Exception as e:
                logger.warning(f"Failed to remove directory {dir_path}: {e}")

        logger.info("Post-response cleanup completed")
    except Exception as e:
        logger.error(f"Error during post-response cleanup: {e}", exc_info=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
