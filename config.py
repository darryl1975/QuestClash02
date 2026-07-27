"""Central configuration, loaded from environment variables (via .env locally).

Every other module reads settings from here rather than touching os.environ
directly, so this file is the single place to look when changing hosts,
keys, or tuning knobs. See .env.example for the full list of variables and
their defaults.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ── Ollama (question generation backend) ────────────────────────────────────
# Defaults to Ollama Cloud. OLLAMA_API_KEY is required for cloud inference;
# leave it blank and point OLLAMA_BASE_URL at a local daemon (e.g.
# http://localhost:11434) to use locally-hosted models instead.
OLLAMA_HOST = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")

# ── Langfuse observability ───────────────────────────────────────────────────
# Tracing is entirely optional: observability.get_langfuse() returns None
# (and every trace call becomes a no-op) unless both keys below are set.
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

# ── OpenAI (used as the LLM judge for quiz quality metrics) ─────────────────
# Also optional: quality_eval.evaluate_batch() short-circuits to None when
# no key is configured, so grading works fine without it.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_JUDGE_MODEL = os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")

# ── MySQL (quiz/category/attempt persistence, see db.py) ─────────────────────
MYSQL_HOST = os.environ.get("MYSQL_URL3", "")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", ""))
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "")
MYSQL_USER = os.environ.get("MYSQL_USERNAME", "")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")

# ── Generation tuning ─────────────────────────────────────────────────────────
GENERATION_MAX_ATTEMPTS = int(os.environ.get("GENERATION_MAX_ATTEMPTS", "3"))  # retries per question type before giving up
MC_OPTION_COUNT = int(os.environ.get("MC_OPTION_COUNT", "4"))  # required option count for multiple_choice
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "16000"))  # source doc is truncated to this before prompting
SOURCE_EXCERPT_STORE_CHARS = int(os.environ.get("SOURCE_EXCERPT_STORE_CHARS", "20000"))  # cap on source text saved to DB

# ── Supported question types ─────────────────────────────────────────────────
# Adding a new type requires: an entry here + QUESTION_TYPE_LABELS, a
# prompts/<type>.txt template, a validators.VALIDATORS entry, and a
# grading.GRADERS entry.
QUESTION_TYPES = [
    "multiple_choice",
    "multiple_select",
    "true_false",
    "yes_no",
    "fill_blank",
    "short_answer",
    "ordering",
]

QUESTION_TYPE_LABELS = {
    "multiple_choice": "Multiple Choice",
    "multiple_select": "Multiple Select",
    "true_false": "True / False",
    "yes_no": "Yes / No",
    "fill_blank": "Fill in the Blank",
    "short_answer": "Short Answer",
    "ordering": "Ordering / Sequence",
}
