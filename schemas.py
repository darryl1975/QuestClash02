"""Pydantic request bodies for the FastAPI endpoints in main.py.

`data` and `correct_answer` on QuestionDraft are intentionally untyped
dicts — their shape varies per question_type (see validators.py, which
produces them, and grading.py/app.js, which consume them), so we validate
FastAPI-side structure here and leave per-type shape checking to those
modules rather than duplicating seven different schemas.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field

import config


class CategoryCreate(BaseModel):
    name: str


class GenerateRequest(BaseModel):
    source_text: str
    category: str
    model: str
    selected_types: list[str] = Field(min_length=1)
    total_count: int = Field(ge=1, le=100)

    def validate_types(self) -> None:
        """Called explicitly by the /api/generate handler (not a pydantic
        validator) so an unknown type produces the same 400-with-message
        behavior as other request errors in main.py."""
        unknown = [t for t in self.selected_types if t not in config.QUESTION_TYPES]
        if unknown:
            raise ValueError(f"Unknown question type(s): {', '.join(unknown)}")


class QuestionDraft(BaseModel):
    """One question as sent from the review/edit step to POST /api/save."""
    question_type: str
    question_text: str
    data: dict[str, Any] = Field(default_factory=dict)
    correct_answer: dict[str, Any]
    explanation: Optional[str] = None


class SaveQuizRequest(BaseModel):
    title: str
    category_id: int
    source_filename: str
    source_file_ext: str
    source_text_excerpt: str = ""
    source_char_count: int = 0
    model_used: str
    requested_total_count: int
    type_distribution: dict[str, int]
    generation_warnings: list[str] = Field(default_factory=list)
    questions: list[QuestionDraft]


class SubmitAnswer(BaseModel):
    question_id: int
    user_answer: dict[str, Any]


class SubmitAttemptRequest(BaseModel):
    answers: list[SubmitAnswer] = Field(min_length=1)


class SelfGradeItem(BaseModel):
    attempt_answer_id: int
    is_correct: bool


class SelfGradeRequest(BaseModel):
    results: list[SelfGradeItem] = Field(min_length=1)
