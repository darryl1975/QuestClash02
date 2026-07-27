"""Auto-grading a submitted answer against the stored correct answer.

Each grader takes (user_answer, correct_answer) — both already-parsed dicts,
see the `data`/`correct_answer` shapes produced by validators.py — and
returns True/False, or None to mean "can't be graded automatically, needs a
human". Only short_answer returns None today; callers (db.create_attempt)
treat a None result as "pending_self_grade" rather than wrong.
"""
import re
from typing import Callable, Optional


def normalize(text: str) -> str:
    """Case/whitespace-insensitive comparison key for free-text answers."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def grade_multiple_choice(user: dict, correct: dict) -> Optional[bool]:
    return user.get("selected_index") == correct.get("correct_index")


def grade_multiple_select(user: dict, correct: dict) -> Optional[bool]:
    return set(user.get("selected_indices", []) or []) == set(correct.get("correct_indices", []) or [])


def grade_true_false(user: dict, correct: dict) -> Optional[bool]:
    # `is` is safe here: both sides are JSON-decoded booleans, and Python
    # interns True/False as singletons, so identity == equality for bool.
    return user.get("value") is correct.get("value")


def grade_yes_no(user: dict, correct: dict) -> Optional[bool]:
    return str(user.get("value", "")).strip().lower() == correct.get("value")


def grade_fill_blank(user: dict, correct: dict) -> Optional[bool]:
    accepted = {normalize(correct.get("correct_answer", ""))}
    accepted |= {normalize(a) for a in correct.get("acceptable_answers", []) or []}
    return normalize(user.get("text", "")) in accepted


def grade_short_answer(user: dict, correct: dict) -> Optional[bool]:
    return None  # always pending — user self-grades on the results screen


def grade_ordering(user: dict, correct: dict) -> Optional[bool]:
    return list(user.get("order", []) or []) == list(correct.get("correct_order", []) or [])


# Question type -> grader. Keys must match config.QUESTION_TYPES exactly;
# db.create_attempt looks functions up here by the stored question_type.
GRADERS: dict[str, Callable[[dict, dict], Optional[bool]]] = {
    "multiple_choice": grade_multiple_choice,
    "multiple_select": grade_multiple_select,
    "true_false": grade_true_false,
    "yes_no": grade_yes_no,
    "fill_blank": grade_fill_blank,
    "short_answer": grade_short_answer,
    "ordering": grade_ordering,
}
