"""Thin async HTTP client for the Ollama chat API (local daemon or Ollama Cloud).

Only two operations are needed by this app: list_models (populate the model
picker) and chat (generate one batch of quiz questions, see generator.py).
Both raise HTTPException directly on auth/model errors so callers can let
FastAPI turn them straight into an API response.
"""
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float


def _size_label(model: dict) -> str:
    """Human-readable model size for the UI, e.g. "7B" or "1.3T".

    Prefers Ollama's own parameter_size string when present; otherwise
    derives a rough label from the raw byte size of the model file.
    """
    param_size = model.get("details", {}).get("parameter_size")
    if param_size:
        return param_size
    size_bytes = model.get("size", 0)
    if size_bytes >= 1_000_000_000_000:
        return f"{size_bytes / 1e12:.1f}T"
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1e9:.0f}B"
    return ""


def _auth_headers(api_key: str) -> dict:
    """Ollama Cloud needs a bearer token; a local daemon needs none, so an
    empty/blank key just means "no Authorization header"."""
    key = (api_key or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def list_models(host: str, api_key: str = "") -> list[dict]:
    """Fetch models known to the Ollama host (local daemon or Ollama Cloud).

    Each result is tagged is_cloud so the frontend can default to showing
    only cloud-hosted models (see /api/models + app.js renderModelOptions),
    since locally-pulled models may not actually be runnable on this host.
    """
    host_is_cloud = "ollama.com" in host
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(f"{host}/api/tags", headers=_auth_headers(api_key))
        if r.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="Ollama Cloud returned 401 Unauthorized. Set a valid "
                       "OLLAMA_API_KEY environment variable.",
            )
        r.raise_for_status()
        data = r.json()
        result = []
        for m in data.get("models", []):
            name = m.get("name", "")
            # Hitting Ollama Cloud directly means every listed model is cloud-hosted;
            # a local daemon only proxies specific models tagged `:cloud`.
            is_cloud = host_is_cloud or bool(m.get("remote_host")) or name.endswith(":cloud")
            result.append({
                "name": name,
                "is_cloud": is_cloud,
                "size_label": _size_label(m),
            })
        result.sort(key=lambda m: (not m["is_cloud"], m["name"]))
        return result


async def chat(
    host: str,
    model: str,
    messages: list[dict],
    api_key: str = "",
    format: str | None = "json",
) -> ChatResult:
    """One non-streaming chat completion. format="json" tells Ollama to
    constrain output to valid JSON — still just a hint, so generator.py
    parses/validates the response defensively rather than trusting it."""
    payload = {"model": model, "messages": messages, "stream": False}
    if format:
        payload["format"] = format
    headers = {"Content-Type": "application/json", **_auth_headers(api_key)}
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        r = await client.post(f"{host}/api/chat", json=payload, headers=headers)
        if r.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="Ollama Cloud returned 401 Unauthorized. Set a valid "
                       "OLLAMA_API_KEY environment variable.",
            )
        if r.status_code == 404:
            raise HTTPException(
                status_code=502,
                detail=f"Model '{model}' not found on Ollama host {host}.",
            )
        r.raise_for_status()
        data = r.json()
    latency_ms = (time.perf_counter() - start) * 1000
    return ChatResult(
        content=data["message"]["content"],
        prompt_tokens=data.get("prompt_eval_count"),
        completion_tokens=data.get("eval_count"),
        latency_ms=latency_ms,
    )
