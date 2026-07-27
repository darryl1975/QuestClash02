"""Langfuse tracing + OpenAI judge client bootstrap.

Both integrations are optional: if credentials are not configured, the
getters below return None and callers fall back to no-op behavior so the
app keeps working without observability wired up.
"""
import contextlib

import config

try:
    from langfuse import Langfuse
except ImportError:
    Langfuse = None

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

_langfuse_client = None
_langfuse_init_done = False
_openai_client = None
_openai_init_done = False


def get_langfuse():
    global _langfuse_client, _langfuse_init_done
    if _langfuse_init_done:
        return _langfuse_client
    _langfuse_init_done = True
    if Langfuse is None or not config.LANGFUSE_PUBLIC_KEY or not config.LANGFUSE_SECRET_KEY:
        return None
    _langfuse_client = Langfuse(
        public_key=config.LANGFUSE_PUBLIC_KEY,
        secret_key=config.LANGFUSE_SECRET_KEY,
        host=config.LANGFUSE_HOST,
    )
    return _langfuse_client


def get_openai():
    global _openai_client, _openai_init_done
    if _openai_init_done:
        return _openai_client
    _openai_init_done = True
    if AsyncOpenAI is None or not config.OPENAI_API_KEY:
        return None
    _openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


def observation(**kwargs):
    """Start a Langfuse observation context manager, or a no-op if tracing is disabled."""
    client = get_langfuse()
    if client is None:
        return contextlib.nullcontext()
    return client.start_as_current_observation(**kwargs)
