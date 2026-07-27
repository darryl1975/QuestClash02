#!/bin/bash
cd "$(dirname "$0")"
PORT="${PORT:-8010}"
echo "Starting QuestClash on http://localhost:$PORT"
python -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
