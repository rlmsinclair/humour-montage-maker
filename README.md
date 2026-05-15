# Udder App — Backend API

FastAPI backend for the Udder AI video montage service. Accepts a video upload, uses OpenAI and Google Gemini to identify the best clips, and returns timestamped clip data for the client to assemble.

## How it works

1. Client uploads a video file
2. API extracts audio and transcribes it
3. OpenAI/Gemini analyses the transcript and video to identify compelling clip segments
4. Returns clip list (start/end times, reason for inclusion) to the client
5. Client assembles the final montage (browser uses ffmpeg.wasm, desktop uses ffmpeg)

## Stack

- **FastAPI** + uvicorn
- **OpenAI API** — clip selection analysis
- **Google Gemini** — secondary analysis model
- **ffmpeg** — server-side audio extraction
- **Docker** + DigitalOcean App Platform

## Setup

```bash
pip install -r requirements.txt
# Requires: OPENAI_API_KEY, GEMINI_API_KEY in .env
uvicorn app.server:app --reload
```

Or with Docker:
```bash
docker build -t udder-app .
docker run -p 8000:8000 udder-app
```

## Related

- [udder](https://github.com/rlmsinclair/udder) — React web frontend
- [udder-demo](https://github.com/rlmsinclair/udder-demo) — PyQt6 desktop client
