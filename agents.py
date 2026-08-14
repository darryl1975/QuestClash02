"""Question-generation agents.

Each supported question type (see config.QUESTION_TYPES) is served by its
own QuestionGenerationAgent instance. An agent is a self-contained unit that
advertises what it can do via an AgentCard (id, description, and the list
of skills/question-types it supports) and knows how to turn a generation
request into validated questions: it prompts the model, parses/validates
the response, retries with corrective feedback on failure, and reports
progress incrementally.

Agents don't decide *when* they run or *which* request goes to which agent
— that's the Orchestrator's job (see orchestrator.py). This module only
defines what an agent is and how it does its work in isolation.
"""
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import config
import observability
import ollama_client
import prompts
import quality_eval
from validators import VALIDATORS, normalize_text

ProgressCallback = Callable[[dict], Awaitable[None]]

# Strips a leading/trailing ```json fenced-code-block wrapper, since models
# often ignore "respond with only JSON" and wrap it in markdown anyway.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_model_json(raw: str) -> tuple[dict | None, str | None]:
    """Best-effort JSON parse of a model response: strip markdown fences,
    try a straight json.loads, then fall back to slicing out the outermost
    {...} span in case the model added stray prose before/after the JSON.
    Returns (parsed, None) on success or (None, error_message) on failure.
    """
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text), None
    except (json.JSONDecodeError, TypeError):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1]), None
            except json.JSONDecodeError:
                pass
        return None, "Response was not valid JSON."


@dataclass(frozen=True)
class AgentSkill:
    """One discrete capability an agent offers, keyed by question_type so
    the Orchestrator can match a requested type to the agent that handles
    it (see Orchestrator.find_agent)."""
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class AgentCard:
    """Self-description an agent publishes for discovery, loosely modeled
    on the A2A protocol's AgentCard: stable id, human-readable metadata,
    and the list of skills it supports. Orchestrator.list_agent_cards()
    exposes these (serialized) over /api/agents so callers can discover
    what generation capabilities exist without hardcoding a type list."""
    id: str
    name: str
    description: str
    version: str
    skills: list[AgentSkill]
    input_modes: list[str]
    output_modes: list[str]

    def supports(self, question_type: str) -> bool:
        return any(skill.id == question_type for skill in self.skills)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentResult:
    """What an agent hands back to the orchestrator for one generation
    request: the validated items it produced (possibly fewer than
    requested, if every retry attempt was exhausted) plus any warnings."""
    items: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def _run_quality_eval(parent_span, qtype: str, items: list[dict], source_text: str, category: str) -> None:
    """Fire-and-forget LLM-judge scoring for a just-validated batch. Only
    runs when both Langfuse and OpenAI are configured (see observability.py);
    otherwise this is a no-op so quality scoring never blocks generation."""
    if not items or observability.get_langfuse() is None or observability.get_openai() is None:
        return

    with observability.observation(
        name=f"quality-eval:{qtype}", as_type="evaluator", input={"item_count": len(items)}
    ) as eval_span:
        result = await quality_eval.evaluate_batch(qtype, items, source_text, category)
        if not result or result.get("error"):
            if eval_span:
                eval_span.update(level="ERROR", status_message=(result or {}).get("error", "no result"))
            return

        if eval_span:
            eval_span.update(output=result)
        target = eval_span or parent_span
        if target is None:
            return
        for metric in ("answer_correctness", "relevance", "toxicity_bias", "distractor_quality"):
            value = result.get(metric)
            if isinstance(value, (int, float)):
                target.score(name=metric, value=float(value), data_type="NUMERIC")


class QuestionGenerationAgent:
    """An autonomous agent that generates one question type.

    Each instance is scoped to a single question_type and publishes an
    AgentCard describing that capability. run() drives the full
    prompt -> validate -> retry-with-feedback loop for one batch, up to
    config.GENERATION_MAX_ATTEMPTS attempts, exactly as the app's original
    single-function generator used to — the behavior is unchanged, only
    the packaging (agent + card, dispatched by an orchestrator) is new.
    """

    def __init__(self, question_type: str):
        self.question_type = question_type
        label = config.QUESTION_TYPE_LABELS.get(question_type, question_type)
        self.card = AgentCard(
            id=f"agent.{question_type}",
            name=f"{label} Generator",
            description=(
                f"Generates validated {label} quiz questions from source text via an LLM, "
                f"retrying up to {config.GENERATION_MAX_ATTEMPTS} time(s) with corrective "
                f"feedback when the model's response fails validation."
            ),
            version="1.0",
            skills=[AgentSkill(
                id=question_type,
                name=label,
                description=f"Produce validated '{question_type}' questions from a source document.",
            )],
            input_modes=["text/plain", "application/json"],
            output_modes=["application/json"],
        )

    async def run(
        self,
        *,
        count: int,
        source_text: str,
        category: str,
        model: str,
        host: str,
        api_key: str,
        seen_texts: set[str],
        progress_cb: ProgressCallback,
    ) -> AgentResult:
        """Generate `count` validated questions of this agent's type.

        On each failed attempt, the model's bad response plus a corrective
        message (what was wrong + the expected schema again) are appended to
        the conversation so the next attempt has that context — this is a
        single growing conversation across attempts, not independent retries.
        best_valid_items always holds the largest valid batch seen so far, so
        if every attempt is exhausted we still return partial results (with a
        warning) instead of nothing.
        """
        qtype = self.question_type
        messages = prompts.build_messages(qtype, source_text=source_text, category=category, count=count)
        validator = VALIDATORS[qtype]
        best_valid_items: list[dict] = []
        last_errors: list[str] = []
        failed_attempts = 0

        with observability.observation(
            name=f"agent:{self.card.id}",
            as_type="span",
            input={"count": count, "category": category, "model": model},
        ) as type_span:
            for attempt in range(1, config.GENERATION_MAX_ATTEMPTS + 1):
                await progress_cb({"event": "attempt", "type": qtype, "attempt": attempt, "max_attempts": config.GENERATION_MAX_ATTEMPTS})

                with observability.observation(
                    name=f"ollama-chat:{qtype}:attempt-{attempt}",
                    as_type="generation",
                    model=model,
                    input=messages,
                ) as gen:
                    try:
                        chat_result = await ollama_client.chat(host, model, messages, api_key=api_key, format="json")
                    except Exception as e:
                        failed_attempts += 1
                        if gen:
                            gen.update(level="ERROR", status_message=str(e))
                            gen.score(name="api_error", value=True, data_type="BOOLEAN")
                        raise

                    raw = chat_result.content
                    is_empty = not raw.strip()
                    if is_empty:
                        failed_attempts += 1
                    if gen:
                        gen.update(
                            output=raw,
                            usage_details={
                                "input": chat_result.prompt_tokens or 0,
                                "output": chat_result.completion_tokens or 0,
                            },
                        )
                        gen.score(name="latency_ms", value=chat_result.latency_ms, data_type="NUMERIC")
                        gen.score(name="api_error", value=False, data_type="BOOLEAN")
                        gen.score(name="empty_response", value=is_empty, data_type="BOOLEAN")

                parsed, parse_err = parse_model_json(raw)

                if parse_err:
                    result_valid, result_errors = [], [parse_err]
                else:
                    result = validator(parsed, count, seen_texts)
                    result_valid, result_errors = result.valid_items, result.errors

                if len(result_valid) > len(best_valid_items):
                    best_valid_items = result_valid
                last_errors = result_errors

                if not result_errors and len(result_valid) == count:
                    # Register these questions as "seen" so later question types
                    # in this same run get flagged if they duplicate one.
                    for it in result_valid:
                        seen_texts.add(normalize_text(it["question_text"]))
                    await progress_cb({"event": "type_done", "type": qtype, "count": count, "attempts": attempt})
                    if type_span:
                        type_span.update(output={"status": "success", "attempts": attempt, "produced": len(result_valid)})
                        type_span.score(name="error_rate", value=failed_attempts / attempt, data_type="NUMERIC")
                    await _run_quality_eval(type_span, qtype, result_valid, source_text, category)
                    return AgentResult(items=result_valid[:count], warnings=[])

                if attempt == config.GENERATION_MAX_ATTEMPTS:
                    for it in best_valid_items:
                        seen_texts.add(normalize_text(it["question_text"]))
                    warning = (
                        f"{config.QUESTION_TYPE_LABELS.get(qtype, qtype)}: after {config.GENERATION_MAX_ATTEMPTS} "
                        f"attempt(s) only {len(best_valid_items)}/{count} valid question(s) were generated. "
                        f"Last issues: {'; '.join(last_errors) if last_errors else 'unknown'}"
                    )
                    await progress_cb({"event": "warning", "message": warning})
                    if type_span:
                        type_span.update(output={"status": "partial", "attempts": attempt, "produced": len(best_valid_items)})
                        type_span.score(name="error_rate", value=failed_attempts / attempt, data_type="NUMERIC")
                    await _run_quality_eval(type_span, qtype, best_valid_items, source_text, category)
                    return AgentResult(items=best_valid_items, warnings=[warning])

                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": prompts.build_corrective_message(qtype, expected_count=count, errors=result_errors),
                })

        return AgentResult(items=best_valid_items, warnings=[f"{qtype}: exhausted attempts unexpectedly."])
