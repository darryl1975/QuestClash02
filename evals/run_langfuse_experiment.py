"""Run a Langfuse dataset experiment against QuestClash's real generation pipeline.

Pulls the mgf-agentic-ai-quiz-questions dataset (see create_langfuse_dataset.py)
and replays each item through QuestionGenerationAgent.run() for a chosen Ollama
model, using Langfuse's dataset.run_experiment() machinery so every item is
traced and linked back to its dataset item as a dataset run. The app's own
LLM-judge (quality_eval.py, invoked inside agent.run()) still fires as normal
and scores answer_correctness/relevance/toxicity_bias/distractor_quality on
the nested trace -- the evaluators added here are deliberately the deterministic
checks that judge doesn't cover: how many of the requested questions actually
came back valid, whether retries/partial batches were needed, and whether the
batch contains duplicate questions.

Run one experiment per model/prompt version you want to compare; Langfuse's
dataset run comparison view (linked at the end of this script) is what you use
to see them side by side.

Usage:
    python evals/run_langfuse_experiment.py --model qwen3:30b
    python evals/run_langfuse_experiment.py --model qwen3:30b --run-name qwen3-baseline
    python evals/run_langfuse_experiment.py --model qwen3:30b --limit 5   # smoke test
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import observability
from agents import QuestionGenerationAgent
from langfuse import Evaluation
from validators import normalize_text

DATASET_NAME = "mgf-agentic-ai-quiz-questions"


async def _noop_progress(event: dict) -> None:
    return None


def build_task(model: str, host: str, api_key: str):
    """Bind the model/host/credentials under test into a Langfuse TaskFunction closure."""

    async def generate_task(*, item, **kwargs):
        agent = QuestionGenerationAgent(item.input["qtype"])
        result = await agent.run(
            count=item.input["count"],
            source_text=item.input["source_text"],
            category=item.input["category"],
            model=model,
            host=host,
            api_key=api_key,
            seen_texts=set(),
            progress_cb=_noop_progress,
        )
        return {
            "items": result.items,
            "warnings": result.warnings,
            "requested": item.input["count"],
            "produced": len(result.items),
        }

    return generate_task


# ── Item-level evaluators ────────────────────────────────────────────────────
# Deterministic, structural checks. Content-quality scores (answer_correctness,
# relevance, toxicity_bias, distractor_quality) are already recorded on the
# nested trace by quality_eval.py inside agent.run() -- no need to duplicate.

def completion_rate_evaluator(*, input, output, expected_output=None, metadata=None, **kwargs):
    requested = output.get("requested") or 1
    produced = output.get("produced", 0)
    return Evaluation(
        name="completion_rate",
        value=produced / requested,
        comment=f"{produced}/{requested} valid questions produced",
    )


def needed_retries_evaluator(*, input, output, expected_output=None, metadata=None, **kwargs):
    warnings = output.get("warnings") or []
    return Evaluation(
        name="needed_retries_or_partial",
        value=bool(warnings),
        comment="; ".join(warnings) if warnings else "clean run, no retries needed",
    )


def duplicate_rate_evaluator(*, input, output, expected_output=None, metadata=None, **kwargs):
    items = output.get("items") or []
    if not items:
        return Evaluation(name="duplicate_rate", value=None, comment="no items produced")
    seen, duplicates = set(), 0
    for it in items:
        key = normalize_text(it.get("question_text", ""))
        if key in seen:
            duplicates += 1
        seen.add(key)
    return Evaluation(
        name="duplicate_rate",
        value=duplicates / len(items),
        comment=f"{duplicates}/{len(items)} duplicate question(s) within this batch",
    )


# ── Run-level evaluators ─────────────────────────────────────────────────────

def average_completion_rate(*, item_results, **kwargs):
    rates = [
        e.value for r in item_results for e in r.evaluations
        if e.name == "completion_rate" and isinstance(e.value, (int, float))
    ]
    if not rates:
        return Evaluation(name="avg_completion_rate", value=0.0, comment="no results")
    return Evaluation(
        name="avg_completion_rate",
        value=sum(rates) / len(rates),
        comment=f"averaged over {len(rates)} item(s)",
    )


def partial_run_rate(*, item_results, **kwargs):
    flags = [
        e.value for r in item_results for e in r.evaluations
        if e.name == "needed_retries_or_partial"
    ]
    if not flags:
        return Evaluation(name="partial_run_rate", value=0.0, comment="no results")
    n_partial = sum(1 for f in flags if f)
    return Evaluation(
        name="partial_run_rate",
        value=n_partial / len(flags),
        comment=f"{n_partial}/{len(flags)} item(s) needed retries or returned a partial batch",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Ollama model to test, e.g. qwen3:30b")
    parser.add_argument("--run-name", default=None, help="Dataset run name (defaults to '<experiment name> - <timestamp>')")
    parser.add_argument("--dataset-name", default=DATASET_NAME)
    parser.add_argument("--max-concurrency", type=int, default=3, help="Concurrent generation calls in flight")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N dataset items (smoke test)")
    args = parser.parse_args()

    client = observability.get_langfuse()
    if client is None:
        raise SystemExit(
            "Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY."
        )

    dataset = client.get_dataset(args.dataset_name)
    items = dataset.items[: args.limit] if args.limit else dataset.items
    print(f"Loaded {len(items)} item(s) from dataset '{args.dataset_name}'")

    task = build_task(args.model, config.OLLAMA_HOST, config.OLLAMA_API_KEY)

    result = client.run_experiment(
        name=f"MGF Agentic AI quiz generation - {args.model}",
        run_name=args.run_name,
        description=f"QuestionGenerationAgent.run() replayed against model={args.model}",
        data=items,
        task=task,
        evaluators=[completion_rate_evaluator, needed_retries_evaluator, duplicate_rate_evaluator],
        run_evaluators=[average_completion_rate, partial_run_rate],
        max_concurrency=args.max_concurrency,
        metadata={"model": args.model, "host": config.OLLAMA_HOST},
    )

    print(result.format(include_item_results=False))
    if result.dataset_run_url:
        print(f"\nView in Langfuse: {result.dataset_run_url}")


if __name__ == "__main__":
    main()
