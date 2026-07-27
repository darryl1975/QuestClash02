"""FastAPI application entry point — HTTP layer only.

Every route here is a thin wrapper: request bodies are validated by the
pydantic models in schemas.py, real work is delegated to the other modules
(extractors for uploads, generator for LLM-driven quiz generation, db for
persistence), and errors are translated into HTTPExceptions. Run via
start.sh (uvicorn), not this file directly.
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
import db
import extractors
import generator
import observability
import ollama_client
from schemas import (
    CategoryCreate,
    GenerateRequest,
    SaveQuizRequest,
    SelfGradeRequest,
    SubmitAttemptRequest,
)

app = FastAPI(title="QuestClash")

BASE_DIR = Path(__file__).parent


class NoCacheStaticFiles(StaticFiles):
    """Serve /static with Cache-Control: no-cache so browsers always
    re-validate app.js/CSS — avoids serving a stale frontend after a
    deploy, at the cost of an extra round-trip per asset."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=BASE_DIR / "static"), name="static")


@app.on_event("startup")
async def on_startup():
    # Idempotent (CREATE TABLE IF NOT EXISTS) — see db.SCHEMA_STATEMENTS.
    db.init_schema()


# ── Frontend ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")


# ── Config / models ──────────────────────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    return {
        "ollama_host": config.OLLAMA_HOST,
        "mysql_database": config.MYSQL_DATABASE,
        "max_attempts": config.GENERATION_MAX_ATTEMPTS,
        "question_types": config.QUESTION_TYPES,
        "question_type_labels": config.QUESTION_TYPE_LABELS,
    }


@app.get("/api/models")
async def get_models():
    try:
        models = await ollama_client.list_models(config.OLLAMA_HOST, config.OLLAMA_API_KEY)
        return {"models": models, "ollama_host": config.OLLAMA_HOST}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ollama at {config.OLLAMA_HOST}: {e}")


# ── Document upload ──────────────────────────────────────────────────────────


@app.post("/api/upload")
async def upload_document(file: UploadFile = File(...)):
    data = await file.read()
    try:
        text = extractors.extract_text(file.filename, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    if not text.strip():
        raise HTTPException(status_code=400, detail="Document appears to be empty.")

    ext = Path(file.filename).suffix.lower()
    return {
        "filename": file.filename,
        "ext": ext,
        "char_count": len(text),
        "text": text,
    }


# ── Categories ───────────────────────────────────────────────────────────────


@app.get("/api/categories")
async def get_categories():
    try:
        return {"categories": db.list_categories()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


@app.post("/api/categories")
async def create_category(payload: CategoryCreate):
    try:
        return db.get_or_create_category(payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


# ── Generation (streaming) ──────────────────────────────────────────────────


@app.post("/api/generate")
async def generate_quiz(req: GenerateRequest):
    """Kick off quiz generation and stream progress back as newline-delimited
    JSON (see generator.run_generation for the event types: plan, attempt,
    type_done, warning, error, done).

    Generation runs in a background task that pushes events onto a queue;
    the response generator just drains the queue and yields each event as
    one JSON line, so the client sees progress as it happens rather than
    waiting for the whole batch to finish.
    """
    try:
        req.validate_types()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()  # marks end-of-stream so the consumer loop can stop

    async def progress_cb(event: dict):
        await queue.put(event)

    async def worker():
        try:
            await generator.run_generation(
                source_text=req.source_text,
                category=req.category,
                model=req.model,
                host=config.OLLAMA_HOST,
                api_key=config.OLLAMA_API_KEY,
                selected_types=req.selected_types,
                total_count=req.total_count,
                progress_cb=progress_cb,
            )
        except Exception as e:
            await queue.put({"event": "error", "message": str(e)})
        finally:
            # Flush any buffered Langfuse traces before the request ends —
            # there's no later opportunity once the response finishes.
            langfuse = observability.get_langfuse()
            if langfuse:
                langfuse.flush()
            await queue.put(SENTINEL)

    async def stream():
        task = asyncio.create_task(worker())
        try:
            while True:
                event = await queue.get()
                if event is SENTINEL:
                    break
                yield json.dumps(event) + "\n"
        finally:
            await task  # propagate worker exceptions / ensure it's fully done

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ── Save / browse saved quizzes ──────────────────────────────────────────────


@app.post("/api/save")
async def save_quiz(payload: SaveQuizRequest):
    if not payload.questions:
        raise HTTPException(status_code=400, detail="Cannot save a quiz with zero questions.")
    try:
        quiz = db.save_quiz({
            "title": payload.title,
            "category_id": payload.category_id,
            "source_filename": payload.source_filename,
            "source_file_ext": payload.source_file_ext,
            "source_text_excerpt": payload.source_text_excerpt,
            "source_char_count": payload.source_char_count,
            "model_used": payload.model_used,
            "requested_total_count": payload.requested_total_count,
            "type_distribution": payload.type_distribution,
            "generation_warnings": payload.generation_warnings,
            "questions": [q.model_dump() for q in payload.questions],
        })
        return quiz
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not save quiz: {e}")


@app.get("/api/quizzes")
async def get_quizzes(category_id: int | None = None):
    try:
        return {"quizzes": db.list_quizzes(category_id)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


@app.get("/api/quizzes/{quiz_id}")
async def get_quiz(quiz_id: int):
    try:
        return db.get_quiz(quiz_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


# ── Quiz attempts ────────────────────────────────────────────────────────────


@app.get("/api/quizzes/{quiz_id}/take")
async def get_quiz_for_attempt(quiz_id: int):
    try:
        return db.get_quiz_for_attempt(quiz_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


@app.post("/api/quizzes/{quiz_id}/attempts")
async def submit_attempt(quiz_id: int, payload: SubmitAttemptRequest):
    try:
        return db.create_attempt(quiz_id, [a.model_dump() for a in payload.answers])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not submit attempt: {e}")


@app.get("/api/quizzes/{quiz_id}/attempts")
async def get_attempts_for_quiz(quiz_id: int):
    try:
        return {"attempts": db.list_attempts_for_quiz(quiz_id)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


@app.get("/api/attempts/{attempt_id}")
async def get_attempt(attempt_id: int):
    try:
        return db.get_attempt(attempt_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")


@app.patch("/api/attempts/{attempt_id}/self-grade")
async def self_grade_attempt(attempt_id: int, payload: SelfGradeRequest):
    try:
        return db.apply_self_grade(attempt_id, [r.model_dump() for r in payload.results])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not apply self-grade: {e}")
