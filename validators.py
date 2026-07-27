"""Structural validation of raw model output, one validator per question type.

generator.py calls VALIDATORS[question_type](parsed_json, expected_count,
seen_texts) after parsing the model's JSON response. Each validator returns
a ValidationResult: items that pass every check for that type end up in
valid_items (already reshaped into the {question_type, question_text, data,
correct_answer, explanation} form the DB expects); anything else produces a
human-readable string in errors, which generator.py feeds back to the model
as a corrective retry prompt (see prompts.build_corrective_message).

seen_texts carries question texts (normalized) already accepted earlier in
the same generation run, so duplicate questions across question types are
rejected too, not just within one batch.
"""
import re
from dataclasses import dataclass, field

import config


@dataclass
class ValidationResult:
    valid_items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    """Case/whitespace-insensitive key used for duplicate-question detection."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _parse_top_level(parsed, expected_count: int) -> tuple[list[dict], list[str]]:
    """Shared structural checks. Returns (raw_items, errors). raw_items is [] if
    the structure is unusable."""
    errors = []
    if not isinstance(parsed, dict):
        return [], ["Response was not a JSON object."]
    items = parsed.get("questions")
    if not isinstance(items, list):
        return [], ['Response is missing a "questions" array.']
    if not items:
        return [], ["The \"questions\" array was empty."]
    raw_items = [it for it in items if isinstance(it, dict)]
    if len(raw_items) < len(items):
        errors.append(f"{len(items) - len(raw_items)} item(s) in \"questions\" were not JSON objects and were dropped.")
    return raw_items, errors


def _check_question_text(item: dict, idx: int, seen_texts: set[str], local_seen: set[str]) -> tuple[str | None, str | None]:
    """Returns (question_text, error). question_text is None if invalid."""
    text = item.get("question")
    if not isinstance(text, str) or not text.strip():
        return None, f"Item {idx}: missing or blank \"question\" text."
    text = text.strip()
    norm = normalize_text(text)
    if norm in local_seen:
        return None, f"Item {idx}: duplicate question within this response (\"{text[:60]}\")."
    if norm in seen_texts:
        return None, f"Item {idx}: duplicates a question already generated earlier in this batch (\"{text[:60]}\")."
    return text, None


def _finalize(errors: list[str], valid_items: list[dict], expected_count: int) -> ValidationResult:
    if len(valid_items) != expected_count:
        errors.append(f"Expected {expected_count} valid question(s), got {len(valid_items)}.")
    return ValidationResult(valid_items=valid_items, errors=errors)


def validate_multiple_choice(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()
    n_options = config.MC_OPTION_COUNT

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        options = item.get("options")
        if not isinstance(options, list) or len(options) != n_options:
            errors.append(f"Item {idx}: \"options\" must be a list of exactly {n_options} strings.")
            continue
        if not all(isinstance(o, str) and o.strip() for o in options):
            errors.append(f"Item {idx}: all options must be non-empty strings.")
            continue
        normed_opts = [normalize_text(o) for o in options]
        if len(set(normed_opts)) != len(normed_opts):
            errors.append(f"Item {idx}: options contain duplicates.")
            continue

        correct_index = item.get("correct_index")
        if not isinstance(correct_index, int) or isinstance(correct_index, bool) or not (0 <= correct_index < n_options):
            errors.append(f"Item {idx}: \"correct_index\" must be an integer between 0 and {n_options - 1}.")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "multiple_choice",
            "question_text": text,
            "data": {"options": options},
            "correct_answer": {"correct_index": correct_index},
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_multiple_select(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        options = item.get("options")
        if not isinstance(options, list) or not (4 <= len(options) <= 6):
            errors.append(f"Item {idx}: \"options\" must be a list of 4 to 6 strings.")
            continue
        if not all(isinstance(o, str) and o.strip() for o in options):
            errors.append(f"Item {idx}: all options must be non-empty strings.")
            continue
        normed_opts = [normalize_text(o) for o in options]
        if len(set(normed_opts)) != len(normed_opts):
            errors.append(f"Item {idx}: options contain duplicates.")
            continue

        correct_indices = item.get("correct_indices")
        if not isinstance(correct_indices, list) or not correct_indices:
            errors.append(f"Item {idx}: \"correct_indices\" must be a non-empty list of integers.")
            continue
        valid_int_indices = (
            all(isinstance(i, int) and not isinstance(i, bool) for i in correct_indices)
            and len(set(correct_indices)) == len(correct_indices)
            and all(0 <= i < len(options) for i in correct_indices)
        )
        if not valid_int_indices:
            errors.append(f"Item {idx}: \"correct_indices\" must be distinct integers within range of options.")
            continue
        if len(correct_indices) >= len(options):
            # Marking every option correct makes the question trivial/degenerate.
            errors.append(f"Item {idx}: \"correct_indices\" cannot mark every option correct.")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "multiple_select",
            "question_text": text,
            "data": {"options": options},
            "correct_answer": {"correct_indices": sorted(correct_indices)},
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_true_false(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        value = item.get("correct_answer")
        if not isinstance(value, bool):
            errors.append(f"Item {idx}: \"correct_answer\" must be a literal JSON boolean (true/false).")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "true_false",
            "question_text": text,
            "data": {},
            "correct_answer": {"value": value},
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_yes_no(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        value = item.get("correct_answer")
        if not isinstance(value, str) or value.strip().lower() not in ("yes", "no"):
            errors.append(f"Item {idx}: \"correct_answer\" must be exactly \"yes\" or \"no\".")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "yes_no",
            "question_text": text,
            "data": {},
            "correct_answer": {"value": value.strip().lower()},
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_fill_blank(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        if text.count("____") != 1:
            errors.append(f"Item {idx}: \"question\" must contain the blank marker \"____\" exactly once.")
            continue

        correct_answer = item.get("correct_answer")
        if not isinstance(correct_answer, str) or not correct_answer.strip():
            errors.append(f"Item {idx}: missing or blank \"correct_answer\".")
            continue

        acceptable = item.get("acceptable_answers", [])
        if acceptable is None:
            acceptable = []
        if not isinstance(acceptable, list) or not all(isinstance(a, str) for a in acceptable):
            errors.append(f"Item {idx}: \"acceptable_answers\" must be a list of strings.")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "fill_blank",
            "question_text": text,
            "data": {},
            "correct_answer": {
                "correct_answer": correct_answer.strip(),
                "acceptable_answers": acceptable,
            },
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_short_answer(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        model_answer = item.get("model_answer")
        if not isinstance(model_answer, str) or not model_answer.strip():
            errors.append(f"Item {idx}: missing or blank \"model_answer\".")
            continue
        if normalize_text(model_answer) == normalize_text(text):
            errors.append(f"Item {idx}: \"model_answer\" must not just repeat the question text.")
            continue

        keywords = item.get("keywords", [])
        if keywords is None:
            keywords = []
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            errors.append(f"Item {idx}: \"keywords\" must be a list of strings.")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "short_answer",
            "question_text": text,
            "data": {},
            "correct_answer": {
                "model_answer": model_answer.strip(),
                "keywords": keywords,
            },
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


def validate_ordering(parsed, expected_count: int, seen_texts: set[str]) -> ValidationResult:
    raw_items, errors = _parse_top_level(parsed, expected_count)
    valid_items = []
    local_seen: set[str] = set()

    for idx, item in enumerate(raw_items):
        text, err = _check_question_text(item, idx, seen_texts, local_seen)
        if err:
            errors.append(err)
            continue

        items_list = item.get("items")
        if not isinstance(items_list, list) or not (3 <= len(items_list) <= 6):
            errors.append(f"Item {idx}: \"items\" must be a list of 3 to 6 strings.")
            continue
        if not all(isinstance(s, str) and s.strip() for s in items_list):
            errors.append(f"Item {idx}: all \"items\" entries must be non-empty strings.")
            continue
        normed = [normalize_text(s) for s in items_list]
        if len(set(normed)) != len(normed):
            errors.append(f"Item {idx}: \"items\" contains duplicates.")
            continue

        correct_order = item.get("correct_order")
        expected_indices = set(range(len(items_list)))
        # correct_order must be a permutation of 0..N-1 (one entry per item,
        # each index used exactly once) — anything else can't be graded.
        if (
            not isinstance(correct_order, list)
            or len(correct_order) != len(items_list)
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in correct_order)
            or set(correct_order) != expected_indices
        ):
            errors.append(f"Item {idx}: \"correct_order\" must be a permutation of the indices 0..{len(items_list) - 1}.")
            continue

        local_seen.add(normalize_text(text))
        valid_items.append({
            "question_type": "ordering",
            "question_text": text,
            "data": {"items": items_list},
            "correct_answer": {"correct_order": correct_order},
            "explanation": item.get("explanation") if isinstance(item.get("explanation"), str) else None,
        })

    return _finalize(errors, valid_items, expected_count)


VALIDATORS = {
    "multiple_choice": validate_multiple_choice,
    "multiple_select": validate_multiple_select,
    "true_false": validate_true_false,
    "yes_no": validate_yes_no,
    "fill_blank": validate_fill_blank,
    "short_answer": validate_short_answer,
    "ordering": validate_ordering,
}
