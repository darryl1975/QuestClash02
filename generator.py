"""Core quiz-generation orchestration: prompts the model per question type,
validates its output, retries with corrective feedback on failure, and
reports progress incrementally so the frontend can show a live log.

Entry point is run_generation(), called once per /api/generate request; it
divides the requested total across the selected question types
(split_count) and calls generate_for_type() for each one in turn.
"""
import json
import re
from typing import Awaitable, Callable

import config
import observability
import ollama_client
import prompts
import quality_eval
from validators import VALIDATORS

ProgressCallback = Callable[[dict], Awaitable[None]]

# Strips a leading/trailing ```json fenced-code-block wrapper, since models
# often ignore "respond with only JSON" and wrap it in markdown anyway.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def split_count(total: int, types: list[str]) -> dict[str, int]:
    """Divide total as evenly as possible across types; any remainder goes
    to the first N types so the totals always sum to exactly `total`."""
    if not types:
        return {}
    base = total // len(types)
    remainder = total % len(types)
    result = {}
    for i, qtype in enumerate(types):
        result[qtype] = base + (1 if i < remainder else 0)
    return result


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


async def generate_for_type(
    qtype: str,
    count: int,
    source_text: str,
    category: str,
    model: str,
    host: str,
    api_key: str,
    seen_texts: set[str],
    progress_cb: ProgressCallback,
) -> tuple[list[dict], list[str]]:
    """Generate `count` validated questions of one type, retrying up to
    config.GENERATION_MAX_ATTEMPTS times.

    On each failed attempt, the model's bad response plus a corrective
    message (what was wrong + the expected schema again) are appended to
    the conversation so the next attempt has that context — this is a
    single growing conversation across attempts, not independent retries.
    best_valid_items always holds the largest valid batch seen so far, so
    if every attempt is exhausted we still return partial results (with a
    warning) instead of nothing.
    """
    messages = prompts.build_messages(qtype, source_text=source_text, category=category, count=count)
    validator = VALIDATORS[qtype]
    best_valid_items: list[dict] = []
    last_errors: list[str] = []
    failed_attempts = 0

    with observability.observation(
        name=f"generate:{qtype}", as_type="span", input={"count": count, "category": category, "model": model}
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
                    seen_texts.add(_normalize(it["question_text"]))
                await progress_cb({"event": "type_done", "type": qtype, "count": count, "attempts": attempt})
                if type_span:
                    type_span.update(output={"status": "success", "attempts": attempt, "produced": len(result_valid)})
                    type_span.score(name="error_rate", value=failed_attempts / attempt, data_type="NUMERIC")
                await _run_quality_eval(type_span, qtype, result_valid, source_text, category)
                return result_valid[:count], []

            if attempt == config.GENERATION_MAX_ATTEMPTS:
                for it in best_valid_items:
                    seen_texts.add(_normalize(it["question_text"]))
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
                return best_valid_items, [warning]

            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": prompts.build_corrective_message(qtype, expected_count=count, errors=result_errors),
            })

    return best_valid_items, [f"{qtype}: exhausted attempts unexpectedly."]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


async def run_generation(
    *,
    source_text: str,
    category: str,
    model: str,
    host: str,
    selected_types: list[str],
    total_count: int,
    progress_cb: ProgressCallback,
    api_key: str = "",
) -> dict:
    """Top-level generation flow for one /api/generate request: truncate an
    oversized source doc, split total_count across selected_types, then
    generate each type in turn (sequentially, not in parallel, so seen_texts
    dedup and progress events stay ordered) and stream progress via
    progress_cb the whole way. Returns the full result dict as well, in
    case a caller wants it directly rather than via the event stream."""
    with observability.observation(
        name="quiz-generation",
        as_type="span",
        input={
            "category": category,
            "model": model,
            "selected_types": selected_types,
            "total_count": total_count,
        },
    ) as root_span:
        if root_span:
            root_span.update_trace(name=f"quiz-generation:{category}")
        warnings: list[str] = []
        truncated_source = source_text
        if len(source_text) > config.MAX_SOURCE_CHARS:
            truncated_source = source_text[: config.MAX_SOURCE_CHARS]
            warnings.append(
                f"Source text was truncated to the first {config.MAX_SOURCE_CHARS} characters "
                f"(original was {len(source_text)} characters)."
            )

        type_distribution = split_count(total_count, selected_types)
        await progress_cb({"event": "plan", "type_distribution": type_distribution})

        seen_texts: set[str] = set()
        all_questions: list[dict] = []

        for qtype in selected_types:
            count = type_distribution[qtype]
            if count <= 0:
                continue
            items, type_warnings = await generate_for_type(
                qtype, count, truncated_source, category, model, host, api_key, seen_texts, progress_cb
            )
            all_questions.extend(items)
            warnings.extend(type_warnings)

        await progress_cb({
            "event": "done",
            "questions": all_questions,
            "warnings": warnings,
            "type_distribution": type_distribution,
        })
        if root_span:
            root_span.update(output={
                "question_count": len(all_questions),
                "warning_count": len(warnings),
            })
        return {
            "questions": all_questions,
            "warnings": warnings,
            "type_distribution": type_distribution,
            "source_char_count": len(source_text),
        }
