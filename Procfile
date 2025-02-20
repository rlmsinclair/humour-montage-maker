web: bash -c "which ffmpeg && which ffprobe || (apt-get update && apt-get install -y $(cat apt.txt)) && uvicorn app:app --host 0.0.0.0 --port $PORT"
