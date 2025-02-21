FROM python:3.13-slim

# Install ffmpeg and other dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application code
COPY . .

# Command to run the application
CMD uvicorn app.server:app --host 0.0.0.0 --port $PORT
