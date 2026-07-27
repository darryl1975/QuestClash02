# QuestClash

Turns an uploaded document (txt/pdf/docx/xlsx/pptx) into a quiz: pick question types and
a total count, generate via an Ollama Cloud model, review/edit the AI's output, then save
the quiz (questions + answer key) to MySQL. Saved quizzes can also be taken directly in
the app, which auto-grades objective question types and lets you self-grade short-answer
questions, tracking a score/history per quiz.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` (or just export the vars) if you need to override any default
— out of the box it targets `localhost:11434` for Ollama and `localhost:3306` / `test_Db` /
`root` / `darryl1975` for MySQL.

## Run

```bash
./start.sh
```

Serves on `http://localhost:8010` (configurable via `PORT`). On startup it creates its
`quest_categories` / `quest_quizzes` / `quest_questions` / `quest_answers` /
`quest_attempts` / `quest_attempt_answers` tables in the target database if they don't
already exist — it never touches any other tables.

Note: `uvicorn --reload` doesn't always pick up every backend file change reliably —
if a code edit doesn't seem to take effect, restart `start.sh`.

## Editing the generation prompts

Each question type's prompt lives in `prompts/*.txt` as a plain text template
(`string.Template` `$placeholder` syntax) — edit those files directly to tune question
style; no code changes or restart required, they're read fresh on every generation call.
