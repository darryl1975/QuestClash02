# evals/

Langfuse-based testing/experiment tooling for QuestClash's question-generation
pipeline (`QuestionGenerationAgent.run()` in `agents.py`). This folder holds one
concrete test dataset plus the scripts to create it in Langfuse and to replay it
through the real pipeline as an experiment:

| File | Purpose |
|---|---|
| `langfuse_dataset_mgf_agentic_ai.json` | 30 hand-curated test cases sourced from IMDA's *Model AI Governance Framework for Agentic AI* (v1.5). Each item is one `(source_text, category, qtype, count)` combination plus a hand-checked `expected_facts` list. |
| `create_langfuse_dataset.py` | One-off uploader: pushes the JSON above into Langfuse as a Dataset named `mgf-agentic-ai-quiz-questions`. |
| `run_langfuse_experiment.py` | Experiment runner: pulls that dataset from Langfuse and replays every item through `QuestionGenerationAgent.run()` for a model you choose, tracing and scoring each item. |

The two scripts are separate on purpose: you create the dataset **once** (or
whenever you want to revise the test cases), then run the experiment **many
times** — once per model, prompt version, or pipeline change you want to compare.

---

## Constraints and limitations — dataset creation

- **Not idempotent.** `create_dataset_item` always appends; re-running
  `create_langfuse_dataset.py` against a dataset that already exists will
  duplicate all 30 items rather than replacing them. If you want a clean
  reload, delete the dataset in the Langfuse UI first (**Datasets →
  mgf-agentic-ai-quiz-questions → Settings → Delete**).
- **No schema validation against the live app.** Each item's `input` shape
  (`source_text`, `category`, `qtype`, `count`) is a manual mirror of
  `QuestionGenerationAgent.run()`'s arguments. If that method's signature
  changes, the JSON has to be updated by hand — nothing enforces the two stay
  in sync.
- **`expected_facts` are a human-curated rubric, not a ground-truth oracle.**
  They were hand-extracted from the source PDF to give a human (or a future
  LLM-judge evaluator) something to check generated questions against. No
  evaluator in this folder currently consumes them automatically — see the
  experiment limitations below.
- **Single-domain, fixed-size set.** All 30 items come from one document under
  one category (`"Agentic AI Governance"`). This is a scenario-specific
  regression set for that content, not a general-purpose benchmark for
  QuestClash's full range of quiz categories.
- **Requires live Langfuse credentials.** The script calls
  `observability.get_langfuse()` and exits immediately if
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` aren't set — there's no offline
  or dry-run mode.
- **Items are uploaded one at a time**, not batched, so creating a much larger
  dataset this way would be slow; fine at 30 items.
- **The source document is explicitly a "living document"** (per its own
  Annex B) — IMDA expects it to be revised. If a newer version changes the
  content this dataset was built from, the items should be reviewed and
  possibly regenerated rather than assumed still accurate.

## Constraints and limitations — running an experiment

- **Every run is a real, billable/rate-limited call.** There's no mock mode:
  `run_langfuse_experiment.py` calls the actual Ollama endpoint
  (`config.OLLAMA_HOST`) for every dataset item, and each item can retry up to
  `config.GENERATION_MAX_ATTEMPTS` (default 3) times internally, so a 30-item
  run can issue up to ~90 chat calls.
- **The built-in LLM-judge scores are conditional.** `answer_correctness`,
  `relevance`, `toxicity_bias`, and `distractor_quality` only get recorded if
  *both* Langfuse and `OPENAI_API_KEY` are configured (see
  `quality_eval.py`/`observability.py`). Without an OpenAI key, only the
  deterministic evaluators in this script (`completion_rate`,
  `needed_retries_or_partial`, `duplicate_rate`) will show up.
- **No automated fact-checking against `expected_facts`.** The evaluators here
  are structural/deterministic only (did it produce enough valid items, did it
  need retries, are there duplicates). Actually grading generated questions
  against the dataset's `expected_facts` still requires either a manual review
  pass in the Langfuse UI or a future LLM-judge evaluator — not implemented
  yet.
- **Non-deterministic output.** LLM generations vary run to run even for the
  same model and inputs. A single experiment run is one noisy sample — treat
  individual numbers cautiously and prefer comparing trends across a few runs,
  especially at this dataset's size (30 items).
- **Duplicate detection is per-item, not per-run.** Each dataset item calls
  `agent.run()` with a fresh `seen_texts = set()`, so `duplicate_rate` only
  catches duplicates *within* one item's own generated batch, not across the
  whole 30-item experiment.
- **Hard failure on missing credentials.** Unlike the main app (which
  degrades gracefully when Langfuse/OpenAI aren't configured), this script
  raises immediately if `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are unset.
- **Concurrency is a blunt instrument.** `--max-concurrency` only limits how
  many `agent.run()` calls are in flight from this script's side; it doesn't
  account for the Ollama host's own rate limits. Lower it if you see timeouts
  or 429s, especially against Ollama Cloud or a modest local daemon.
- **Per-call timeout is fixed at 300s** (`ollama_client.chat()`), so a slow
  model combined with high `count` values can make a single item take a long
  time — this isn't currently configurable via CLI flag.

---

## Running `create_langfuse_dataset.py` step by step

1. **Activate the project's virtualenv** (from the project root):
   ```bash
   source venv/bin/activate
   ```
2. **Set Langfuse credentials.** Add to `.env` (see `.env.example`) or export
   them directly:
   ```bash
   export LANGFUSE_PUBLIC_KEY=pk-lf-...
   export LANGFUSE_SECRET_KEY=sk-lf-...
   export LANGFUSE_HOST=https://cloud.langfuse.com   # or your self-hosted URL
   ```
3. **(Optional) Review or edit the dataset content** in
   `evals/langfuse_dataset_mgf_agentic_ai.json` before uploading, if you want
   to add/remove/edit items.
4. **If a dataset named `mgf-agentic-ai-quiz-questions` already exists** and
   you want a clean reload (not an append), delete it first in the Langfuse
   UI: **Datasets → mgf-agentic-ai-quiz-questions → Settings → Delete
   dataset**.
5. **Run the uploader** from the project root:
   ```bash
   python evals/create_langfuse_dataset.py
   ```
6. **Verify.** The script prints one line per item as it's created, ending
   with `Done: 30 items uploaded to 'mgf-agentic-ai-quiz-questions'.` Then
   open Langfuse → **Datasets → mgf-agentic-ai-quiz-questions** and confirm 30
   items are listed.

## Running `run_langfuse_experiment.py` step by step

1. **Make sure the dataset exists first** (run the steps above at least
   once).
2. **Set the Ollama connection.** For Ollama Cloud, set an API key; for a
   local daemon, point the base URL at it instead:
   ```bash
   export OLLAMA_API_KEY=...              # if using Ollama Cloud (default host)
   # or
   export OLLAMA_BASE_URL=http://localhost:11434   # if using a local daemon
   ```
3. **(Optional) Enable the built-in LLM-judge scores** by also setting an
   OpenAI key:
   ```bash
   export OPENAI_API_KEY=sk-...
   export OPENAI_JUDGE_MODEL=gpt-4o-mini   # default, override if you want
   ```
4. **Smoke test first**, on a handful of items, to confirm everything is wired
   up before spending a full run:
   ```bash
   python evals/run_langfuse_experiment.py --model gpt-oss:20b --limit 3
   ```
5. **Check the output.** The script prints a summary and, if applicable, a
   `dataset_run_url` — open it to confirm traces, evaluator scores, and (if
   configured) the LLM-judge scores landed correctly in Langfuse.
6. **Run the full dataset** once the smoke test looks right:
   ```bash
   python evals/run_langfuse_experiment.py --model gpt-oss:20b --run-name gpt-oss-20b-baseline
   ```
7. **Compare experiments.** Re-run with a different `--model` (or after a
   prompt/pipeline change) using a distinct `--run-name`, then open the
   dataset in Langfuse and use its **dataset run comparison** view to see the
   runs side by side.
8. **Tune concurrency if needed.** If you see timeouts or rate-limit errors,
   lower `--max-concurrency` (default `3`).

---

## Suggested `OLLAMA_MODEL` for testing

**`gpt-oss:20b`** is a good default for exercising this experiment.

- It's a real, currently-available Ollama Cloud model (OpenAI's open-weight
  reasoning/agentic model family, offered in `20b` and `120b` sizes on
  `ollama.com`).
- The `20b` size is fast and inexpensive enough for iterating on smoke tests
  and repeated comparison runs, while still being a genuine reasoning model —
  a meaningfully different comparison point from `gpt-oss:120b` if you later
  want to test whether the larger size changes `completion_rate` or the
  judge's `answer_correctness`/`distractor_quality` scores.
- Since `config.OLLAMA_HOST` defaults to `https://ollama.com`, no `-cloud`
  suffix is needed on the model name — that suffix only matters when routing
  cloud models through a **local** Ollama daemon
  (`OLLAMA_BASE_URL=http://localhost:11434`).

```bash
python evals/run_langfuse_experiment.py --model gpt-oss:20b --limit 3
```

If you want a second point of comparison, `gpt-oss:120b` (larger, slower,
Ollama Cloud) or a smaller general model already pulled to a local daemon are
both reasonable follow-ups.
