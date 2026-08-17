"""One-off script: create the mgf-agentic-ai-quiz-questions Langfuse dataset.

Reads evals/langfuse_dataset_mgf_agentic_ai.json and creates a Langfuse
Dataset + DatasetItems from it. Each item's input mirrors the arguments to
QuestionGenerationAgent.run() (source_text, category, qtype, count), so the
dataset can be replayed through the real generation pipeline in an
experiment/dataset run. expected_output holds a hand-checked fact list for
grading answer_correctness independently of the LLM judge.

Requires LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to be set (see
.env.example). Safe to re-run: existing items with the same id are left
alone by Langfuse's create_dataset_item (it appends, so re-running will
duplicate items — delete the dataset in the UI first if you want a clean
reload).

Usage: python evals/create_langfuse_dataset.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import observability

DATASET_FILE = Path(__file__).resolve().parent / "langfuse_dataset_mgf_agentic_ai.json"


def main() -> None:
    client = observability.get_langfuse()
    if client is None:
        raise SystemExit(
            "Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY (see .env.example) before running this script."
        )

    data = json.loads(DATASET_FILE.read_text())
    dataset_name = data["dataset_name"]

    client.create_dataset(name=dataset_name, description=data["dataset_description"])
    print(f"Created dataset '{dataset_name}'")

    for item in data["items"]:
        client.create_dataset_item(
            dataset_name=dataset_name,
            input={
                "source_text": item["source_text"],
                "category": item["category"],
                "qtype": item["question_type"],
                "count": item["count"],
            },
            expected_output={"facts": item["expected_facts"]},
            metadata={"topic": item["topic"], **item["metadata"]},
            id=item["id"],
        )
        print(f"  added {item['id']} ({item['question_type']}, {item['topic']})")

    client.flush()
    print(f"Done: {len(data['items'])} items uploaded to '{dataset_name}'.")


if __name__ == "__main__":
    main()
