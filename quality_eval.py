"""LLM-judge quality scoring for a batch of freshly generated quiz questions.

Uses OpenAI (configured in config.py) to score a just-generated batch of
questions of a single type against the source text they were derived from.
Returns None if no OpenAI key is configured, or a dict with an "error" key
if the judge call itself failed.
"""
import json

import config
import observability

JUDGE_SYSTEM_PROMPT = """You are a strict quiz quality auditor. You will be given the source \
text a quiz was generated from, and a batch of generated quiz questions of a single type. \
Score the batch as a whole on these metrics, each a float from 0.0 (worst) to 1.0 (best):

- answer_correctness: are the marked correct answers actually and verifiably true according to \
the source text?
- distractor_quality: for questions with multiple-choice style options, are the wrong options \
plausible and not trivially guessable? Use null if the question type has no distractor options.
- relevance: do the questions match the source text's topic and the stated category?
- toxicity_bias: 1.0 means no toxic, offensive, or biased language was found in the questions; \
lower values indicate such issues were found.

Respond with ONLY a JSON object of this exact shape, no other text:
{"answer_correctness": <float>, "distractor_quality": <float or null>, "relevance": <float>, \
"toxicity_bias": <float>, "notes": "<one or two sentence summary of any issues found>"}
"""


def _summarize_item(qtype: str, item: dict) -> dict:
    summary = {"question": item["question_text"]}
    data = item.get("data", {})
    correct = item.get("correct_answer", {})
    if qtype in ("multiple_choice", "multiple_select"):
        summary["options"] = data.get("options", [])
        summary["correct"] = correct
    elif qtype == "ordering":
        summary["items"] = data.get("items", [])
        summary["correct_order"] = correct.get("correct_order")
    else:
        summary["correct_answer"] = correct
    if item.get("explanation"):
        summary["explanation"] = item["explanation"]
    return summary


async def evaluate_batch(qtype: str, items: list[dict], source_text: str, category: str) -> dict | None:
    client = observability.get_openai()
    if client is None or not items:
        return None

    summaries = [_summarize_item(qtype, it) for it in items]
    user_content = (
        f"Category: {category}\n"
        f"Question type: {qtype}\n\n"
        "=== SOURCE TEXT ===\n"
        f"{source_text}\n"
        "=== END SOURCE TEXT ===\n\n"
        "=== GENERATED QUESTIONS ===\n"
        f"{json.dumps(summaries, indent=2)}\n"
        "=== END GENERATED QUESTIONS ==="
    )

    try:
        response = await client.chat.completions.create(
            model=config.OPENAI_JUDGE_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        return {"error": str(e)}
