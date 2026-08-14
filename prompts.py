"""Loads and renders the prompt templates under prompts/*.txt.

Each question type has its own template file containing the user-turn
instructions plus a `<!-- SCHEMA START --> ... <!-- SCHEMA END -->` block
documenting the exact JSON shape the model must return. That schema block
gets re-injected into corrective retry messages (see build_corrective_message)
so the model is reminded of the required shape after producing invalid JSON.

Templates use string.Template ($placeholder) substitution, not f-strings/
Jinja, so a missing kwarg raises KeyError immediately rather than silently
leaving a literal "$name" in the rendered prompt.
"""
import re
from pathlib import Path
from string import Template

import config
from langfuse import Langfuse

# Initialize client
langfuse = Langfuse()


PROMPTS_DIR = Path(__file__).parent / "prompts"

_SCHEMA_RE = re.compile(r"<!-- SCHEMA START -->\s*(.*?)\s*<!-- SCHEMA END -->", re.DOTALL)


def load_template(name: str) -> str:
    # """Read prompts/<name>.txt verbatim (no substitution)."""
    # path = PROMPTS_DIR / f"{name}.txt"
    # return path.read_text(encoding="utf-8")
    prompt_obj = langfuse.get_prompt(f"QuestClash/{name}")
    return prompt_obj.get_langchain_prompt()

def render(name: str, **kwargs) -> str:
    """Load prompts/<name>.txt and substitute $-placeholders from kwargs."""
    template = Template(load_template(name))
    return template.substitute(**kwargs)


def extract_schema_block(question_type: str) -> str:
    """Pull the SCHEMA START/END block out of a question type's template,
    for reuse in corrective retry prompts. Raises if the marker is missing —
    every prompts/<type>.txt is expected to define one."""
    raw = load_template(question_type)
    match = _SCHEMA_RE.search(raw)
    if not match:
        raise ValueError(f"No SCHEMA block found in prompts/{question_type}.txt")
    return match.group(1)


def build_messages(question_type: str, *, source_text: str, category: str, count: int) -> list[dict]:
    """Build the initial system+user chat messages sent to the model for one
    question-type batch (see agents.QuestionGenerationAgent.run)."""
    system_content = render("system_instructions", category=category)
    user_content = render(
        question_type,
        source_text=source_text,
        category=category,
        count=count,
        mc_option_count=config.MC_OPTION_COUNT,
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def build_corrective_message(question_type: str, *, expected_count: int, errors: list[str]) -> str:
    """Follow-up user message appended after a batch fails validation,
    listing what went wrong and re-stating the expected JSON schema so the
    model can self-correct on the next attempt."""
    error_list = "\n".join(f"- {e}" for e in errors)
    return render(
        "retry_corrective",
        error_list=error_list,
        expected_count=expected_count,
        question_type_label=config.QUESTION_TYPE_LABELS.get(question_type, question_type),
        schema_reminder=extract_schema_block(question_type),
    )
