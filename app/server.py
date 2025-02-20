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

# Default prompt template
DEFAULT_PROMPT = """You are a humor analysis system. Analyze these segments of a transcribed video which may have multiple speakers for humorous content and rate them:

{segments_text}

Return your analysis as a JSON object with this exact format:
{{
    "segments": [
        {{
            "segment_number": <number of the segment>,
            "score": <number 0-100 indicating how funny the text is overall>,
            "moments": [
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

Only include genuinely funny moments. If nothing is funny, return an empty moments array.
Ensure the "text" field matches words exactly as they appear in the original text."""

# Settings
MAX_RETRIES = 3
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

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

async def analyze_humor_segments(segments: List[Dict], batch_size: int = 5, custom_prompt: str = None) -> List[Dict]:
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

                # Use custom prompt if provided, otherwise use default
                try:
                    if custom_prompt:
                        # Double the curly braces to escape them in the format string
                        escaped_prompt = custom_prompt.replace('{', '{{').replace('}', '}}')
                        # Replace back the format placeholder
                        escaped_prompt = escaped_prompt.replace('{{segments_text}}', '{segments_text}')
                        prompt = escaped_prompt.format(segments_text=segments_text)
                    else:
                        # Default prompt already has properly escaped braces
                        prompt = DEFAULT_PROMPT.format(segments_text=segments_text)
                except Exception as e:
                    logger.error(f"Error formatting prompt: {e}")
                    # Fallback to default prompt
                    prompt = DEFAULT_PROMPT.format(segments_text=segments_text)

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

                        for moment in segment_analysis.get('moments', []):
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
                    return [{**segment, 'humor_score': 0, 'funny_clips': []} for segment in batch]  # Keep funny_clips in output for backward compatibility

            except Exception as e:
                logger.error(f"Error analyzing batch: {e}", exc_info=True)
                return [{**segment, 'humor_score': 0, 'funny_clips': []} for segment in batch]  # Keep funny_clips in output for backward compatibility

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

from fastapi import Form

@app.post("/process-audio-chunk")
async def process_audio_chunk(
    file: UploadFile = File(...),
    chunk_start: float = Form(...),
    chunk_duration: float = Form(...),
    custom_prompt: str = Form(None)  # Optional custom prompt parameter
):
    try:
        # Validate file format
        if not file.filename.lower().endswith(('.mp3', '.wav', '.m4a', '.aac')):
            logger.error(f"Unsupported file format: {file.filename}")
            return JSONResponse(
                status_code=400,
                content={"error": "Unsupported file format. Please upload an MP3, WAV, M4A, or AAC file."}
            )
            
        logger.info(f"Received audio chunk starting at {chunk_start}s with duration {chunk_duration}s")
        
        # Save chunk file
        chunk_path = UPLOAD_DIR / f"chunk_{chunk_start}.mp3"
        async with aiofiles.open(chunk_path, 'wb') as f:
            content = await file.read()
            await f.write(content)
        logger.info(f"Saved chunk to {chunk_path}")

        # Transcribe chunk
        logger.info("Starting transcription process")
        words = await transcribe_chunk(chunk_path, chunk_start)
        logger.info(f"Transcription complete, got {len(words)} words")

        # Group words into a segment
        segment = {
            'start_time': chunk_start,
            'end_time': chunk_start + chunk_duration,
            'words': words,
            'text': ' '.join(w['text'] for w in words)
        }

        # Analyze humor for this segment
        analyzed_segments = await analyze_humor_segments([segment], custom_prompt=custom_prompt)
        
        # Extract funny clips from this segment
        funny_clips = []
        if analyzed_segments:
            segment = analyzed_segments[0]
            humor_score = segment.get('humor_score', 0)
            if humor_score >= 40:
                funny_clips.extend(segment.get('funny_clips', []))  # Keep using funny_clips in API response for backward compatibility

        # Cleanup chunk file
        if chunk_path.exists():
            os.remove(chunk_path)
            logger.debug(f"Removed chunk file: {chunk_path}")

        return {
            "chunk_start": chunk_start,
            "chunk_duration": chunk_duration,
            "clips": funny_clips
        }

    except Exception as e:
        logger.error(f"Error processing audio chunk: {e}", exc_info=True)
        # Clean up chunk file if it exists
        if 'chunk_path' in locals() and chunk_path.exists():
            try:
                os.remove(chunk_path)
                logger.debug(f"Cleaned up chunk file after error: {chunk_path}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up chunk file: {cleanup_error}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
