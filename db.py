"""MySQL persistence layer — categories, quizzes, questions, attempts.

Raw SQL via pymysql, no ORM. Every public function opens its own connection
and closes it in a finally block; functions that need to call another
db function without a second round-trip accept an optional `_conn` to reuse
an existing connection/transaction (see save_quiz -> get_quiz).

init_schema() runs CREATE TABLE IF NOT EXISTS for everything in
SCHEMA_STATEMENTS on app startup (see main.py's startup hook), so there is
no separate migration step — schema changes go directly into the statements
below and are additive/idempotent by construction.
"""
import datetime
import json

import pymysql
import pymysql.cursors

import config
import grading

# Applied in order on every startup; CREATE TABLE IF NOT EXISTS makes this
# safe to re-run against an already-initialized database.
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS quest_categories (
      id INT AUTO_INCREMENT PRIMARY KEY,
      name VARCHAR(120) NOT NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uq_quest_categories_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_quizzes (
      id INT AUTO_INCREMENT PRIMARY KEY,
      title VARCHAR(255) NOT NULL,
      category_id INT NOT NULL,
      source_filename VARCHAR(255) NOT NULL,
      source_file_ext VARCHAR(10) NOT NULL,
      source_text_excerpt MEDIUMTEXT NULL,
      source_char_count INT NOT NULL DEFAULT 0,
      model_used VARCHAR(120) NOT NULL,
      requested_total_count INT NOT NULL,
      type_distribution JSON NOT NULL,
      generation_warnings JSON NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      CONSTRAINT fk_quest_quizzes_category FOREIGN KEY (category_id) REFERENCES quest_categories(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_questions (
      id INT AUTO_INCREMENT PRIMARY KEY,
      quiz_id INT NOT NULL,
      question_type VARCHAR(30) NOT NULL
        CHECK (question_type IN ('multiple_choice','multiple_select','true_false',
                                  'yes_no','fill_blank','short_answer','ordering')),
      position INT NOT NULL,
      question_text TEXT NOT NULL,
      data JSON NULL,
      explanation TEXT NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT fk_quest_questions_quiz FOREIGN KEY (quiz_id) REFERENCES quest_quizzes(id) ON DELETE CASCADE,
      KEY idx_quest_questions_quiz (quiz_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_answers (
      id INT AUTO_INCREMENT PRIMARY KEY,
      question_id INT NOT NULL,
      correct_answer JSON NOT NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT fk_quest_answers_question FOREIGN KEY (question_id) REFERENCES quest_questions(id) ON DELETE CASCADE,
      UNIQUE KEY uq_quest_answers_question (question_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_attempts (
      id INT AUTO_INCREMENT PRIMARY KEY,
      quiz_id INT NOT NULL,
      score INT NOT NULL DEFAULT 0,
      total_questions INT NOT NULL,
      percentage DECIMAL(5,2) NOT NULL DEFAULT 0,
      pending_self_grade INT NOT NULL DEFAULT 0,
      started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      completed_at TIMESTAMP NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT fk_quest_attempts_quiz FOREIGN KEY (quiz_id) REFERENCES quest_quizzes(id) ON DELETE CASCADE,
      KEY idx_quest_attempts_quiz (quiz_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_attempt_answers (
      id INT AUTO_INCREMENT PRIMARY KEY,
      attempt_id INT NOT NULL,
      question_id INT NOT NULL,
      user_answer JSON NOT NULL,
      is_correct TINYINT(1) NULL,
      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT fk_quest_attempt_answers_attempt FOREIGN KEY (attempt_id) REFERENCES quest_attempts(id) ON DELETE CASCADE,
      CONSTRAINT fk_quest_attempt_answers_question FOREIGN KEY (question_id) REFERENCES quest_questions(id),
      KEY idx_quest_attempt_answers_attempt (attempt_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


def get_connection():
    """New pymysql connection; DictCursor so rows come back as dicts (JSON-ready)."""
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def init_schema() -> None:
    """Create all tables if they don't already exist. Called once at app startup."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def list_categories() -> list[dict]:
    """All categories, alphabetical — used to populate the category picker."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, created_at FROM quest_categories ORDER BY name")
            return cur.fetchall()
    finally:
        conn.close()


def get_or_create_category(name: str) -> dict:
    """Look up a category by name, creating it if it doesn't exist yet.

    The INSERT ... ON DUPLICATE KEY UPDATE trick lets us do this in one
    round-trip: on a name collision it rewrites id = LAST_INSERT_ID(id),
    which makes LAST_INSERT_ID() return the *existing* row's id instead of
    0, so the SELECT below always finds the right row whether it was just
    created or already existed.
    """
    name = name.strip()
    if not name:
        raise ValueError("Category name cannot be empty")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO quest_categories (name) VALUES (%s) "
                "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
                (name,),
            )
            cur.execute("SELECT LAST_INSERT_ID() AS id")
            new_id = cur.fetchone()["id"]
            cur.execute("SELECT id, name, created_at FROM quest_categories WHERE id = %s", (new_id,))
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def save_quiz(payload: dict) -> dict:
    """payload: {title, category_id, source_filename, source_file_ext, source_text_excerpt,
    source_char_count, model_used, requested_total_count, type_distribution,
    generation_warnings, questions: [{question_type, question_text, data, explanation,
    correct_answer}]}"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quest_quizzes
                  (title, category_id, source_filename, source_file_ext, source_text_excerpt,
                   source_char_count, model_used, requested_total_count, type_distribution,
                   generation_warnings)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload["title"],
                    payload["category_id"],
                    payload["source_filename"],
                    payload["source_file_ext"],
                    payload.get("source_text_excerpt", "")[: config.SOURCE_EXCERPT_STORE_CHARS],
                    payload.get("source_char_count", 0),
                    payload["model_used"],
                    payload["requested_total_count"],
                    json.dumps(payload["type_distribution"]),
                    json.dumps(payload.get("generation_warnings") or []),
                ),
            )
            quiz_id = cur.lastrowid

            for i, q in enumerate(payload["questions"]):
                cur.execute(
                    """
                    INSERT INTO quest_questions
                      (quiz_id, question_type, position, question_text, data, explanation)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        quiz_id,
                        q["question_type"],
                        i,
                        q["question_text"],
                        json.dumps(q.get("data") or {}),
                        q.get("explanation"),
                    ),
                )
                question_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO quest_answers (question_id, correct_answer) VALUES (%s, %s)",
                    (question_id, json.dumps(q["correct_answer"])),
                )
        conn.commit()
        # Reuse this connection (via _conn) rather than opening a second one
        # just to re-read what we already wrote.
        return get_quiz(quiz_id, _conn=conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_quizzes(category_id: int | None = None) -> list[dict]:
    """Quiz summaries (no question bodies) for the browse/list view, newest first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT q.id, q.title, q.category_id, c.name AS category_name, "
                "q.source_filename, q.model_used, q.requested_total_count, "
                "q.type_distribution, q.generation_warnings, q.created_at, "
                "(SELECT COUNT(*) FROM quest_questions qq WHERE qq.quiz_id = q.id) AS question_count "
                "FROM quest_quizzes q JOIN quest_categories c ON c.id = q.category_id"
            )
            params: tuple = ()
            if category_id is not None:
                sql += " WHERE q.category_id = %s"
                params = (category_id,)
            sql += " ORDER BY q.created_at DESC"
            cur.execute(sql, params)
            rows = cur.fetchall()
            for row in rows:
                row["type_distribution"] = json.loads(row["type_distribution"])
                row["generation_warnings"] = json.loads(row["generation_warnings"] or "[]")
            return rows
    finally:
        conn.close()


def get_quiz(quiz_id: int, _conn=None) -> dict:
    """Full quiz including questions and correct answers — used for review/editing,
    never for the "take the quiz" flow (see get_quiz_for_attempt for that).

    _conn: reuse an already-open connection/transaction (see save_quiz) instead
    of opening a new one; when set, this function does not close it.
    """
    conn = _conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT q.id, q.title, q.category_id, c.name AS category_name, "
                "q.source_filename, q.source_file_ext, q.source_char_count, q.model_used, "
                "q.requested_total_count, q.type_distribution, q.generation_warnings, q.created_at "
                "FROM quest_quizzes q JOIN quest_categories c ON c.id = q.category_id "
                "WHERE q.id = %s",
                (quiz_id,),
            )
            quiz = cur.fetchone()
            if quiz is None:
                raise ValueError(f"Quiz {quiz_id} not found")
            quiz["type_distribution"] = json.loads(quiz["type_distribution"])
            quiz["generation_warnings"] = json.loads(quiz["generation_warnings"] or "[]")

            cur.execute(
                "SELECT qq.id, qq.question_type, qq.position, qq.question_text, qq.data, "
                "qq.explanation, qa.correct_answer "
                "FROM quest_questions qq "
                "JOIN quest_answers qa ON qa.question_id = qq.id "
                "WHERE qq.quiz_id = %s ORDER BY qq.position",
                (quiz_id,),
            )
            questions = cur.fetchall()
            for question in questions:
                question["data"] = json.loads(question["data"] or "{}")
                question["correct_answer"] = json.loads(question["correct_answer"])
            quiz["questions"] = questions
            return quiz
    finally:
        if _conn is None:
            conn.close()


# ── Quiz attempts ────────────────────────────────────────────────────────────


def get_quiz_for_attempt(quiz_id: int) -> dict:
    """Question list for taking the quiz — never includes correct_answer/explanation."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT q.id, q.title, q.category_id, c.name AS category_name "
                "FROM quest_quizzes q JOIN quest_categories c ON c.id = q.category_id "
                "WHERE q.id = %s",
                (quiz_id,),
            )
            quiz = cur.fetchone()
            if quiz is None:
                raise ValueError(f"Quiz {quiz_id} not found")

            cur.execute(
                "SELECT id AS question_id, question_type, question_text, data "
                "FROM quest_questions WHERE quiz_id = %s ORDER BY position",
                (quiz_id,),
            )
            questions = cur.fetchall()
            for question in questions:
                question["data"] = json.loads(question["data"] or "{}")
            quiz["questions"] = questions
            return quiz
    finally:
        conn.close()


def create_attempt(quiz_id: int, answers: list[dict]) -> dict:
    """answers: [{question_id, user_answer}]. Grades objective types immediately;
    short_answer items are left pending (is_correct NULL) for self-grading."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM quest_quizzes WHERE id = %s", (quiz_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Quiz {quiz_id} not found")

            question_ids = [a["question_id"] for a in answers]
            if not question_ids:
                raise ValueError("No answers submitted")
            placeholders = ",".join(["%s"] * len(question_ids))
            cur.execute(
                f"SELECT qq.id, qq.quiz_id, qq.question_type, qa.correct_answer "
                f"FROM quest_questions qq JOIN quest_answers qa ON qa.question_id = qq.id "
                f"WHERE qq.id IN ({placeholders})",
                tuple(question_ids),
            )
            by_id = {row["id"]: row for row in cur.fetchall()}

            score = 0
            pending = 0
            graded_rows = []
            for a in answers:
                q = by_id.get(a["question_id"])
                if q is None or q["quiz_id"] != quiz_id:
                    raise ValueError(f"Question {a['question_id']} does not belong to quiz {quiz_id}")
                correct_answer = json.loads(q["correct_answer"])
                grader = grading.GRADERS[q["question_type"]]
                is_correct = grader(a["user_answer"], correct_answer)
                if is_correct is None:  # short_answer: always pending, graded by the user later
                    pending += 1
                elif is_correct:
                    score += 1
                graded_rows.append((a["question_id"], a["user_answer"], is_correct))

            total = len(answers)
            percentage = round((score / total) * 100, 2) if total else 0

            cur.execute(
                """
                INSERT INTO quest_attempts (quiz_id, score, total_questions, percentage,
                    pending_self_grade, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (quiz_id, score, total, percentage, pending, None if pending else _now_expr()),
            )
            attempt_id = cur.lastrowid

            for question_id, user_answer, is_correct in graded_rows:
                cur.execute(
                    "INSERT INTO quest_attempt_answers (attempt_id, question_id, user_answer, is_correct) "
                    "VALUES (%s, %s, %s, %s)",
                    (attempt_id, question_id, json.dumps(user_answer), is_correct),
                )
        conn.commit()
        return get_attempt(attempt_id, _conn=conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_expr():
    """Completion timestamp — only set once an attempt has zero pending answers."""
    return datetime.datetime.now()


def apply_self_grade(attempt_id: int, results: list[dict]) -> dict:
    """Apply the user's self-marked scores for previously-pending (short_answer)
    questions, then recompute the attempt's totals.

    results: [{attempt_answer_id, is_correct}].
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM quest_attempts WHERE id = %s", (attempt_id,))
            if cur.fetchone() is None:
                raise ValueError(f"Attempt {attempt_id} not found")

            for r in results:
                cur.execute(
                    "UPDATE quest_attempt_answers SET is_correct = %s "
                    "WHERE id = %s AND attempt_id = %s",
                    (r["is_correct"], r["attempt_answer_id"], attempt_id),
                )

            cur.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS score, "
                "SUM(CASE WHEN is_correct IS NULL THEN 1 ELSE 0 END) AS pending "
                "FROM quest_attempt_answers WHERE attempt_id = %s",
                (attempt_id,),
            )
            row = cur.fetchone()
            total = row["total"] or 0
            score = row["score"] or 0
            pending = row["pending"] or 0
            percentage = round((score / total) * 100, 2) if total else 0

            cur.execute(
                "UPDATE quest_attempts SET score = %s, percentage = %s, pending_self_grade = %s, "
                "completed_at = %s WHERE id = %s",
                (score, percentage, pending, None if pending else _now_expr(), attempt_id),
            )
        conn.commit()
        return get_attempt(attempt_id, _conn=conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_attempt(attempt_id: int, _conn=None) -> dict:
    """Attempt summary plus every answer, joined with question + correct answer
    data, for the results screen (including any still-pending self-grade items)."""
    conn = _conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id AS attempt_id, quiz_id, score, total_questions, percentage, "
                "pending_self_grade, started_at, completed_at "
                "FROM quest_attempts WHERE id = %s",
                (attempt_id,),
            )
            attempt = cur.fetchone()
            if attempt is None:
                raise ValueError(f"Attempt {attempt_id} not found")

            cur.execute(
                "SELECT aa.id AS attempt_answer_id, aa.question_id, qq.question_type, "
                "qq.question_text, qq.data, aa.user_answer, qa.correct_answer, aa.is_correct, qq.explanation "
                "FROM quest_attempt_answers aa "
                "JOIN quest_questions qq ON qq.id = aa.question_id "
                "JOIN quest_answers qa ON qa.question_id = qq.id "
                "WHERE aa.attempt_id = %s ORDER BY aa.id",
                (attempt_id,),
            )
            items = cur.fetchall()
            for item in items:
                item["data"] = json.loads(item["data"] or "{}")
                item["user_answer"] = json.loads(item["user_answer"])
                item["correct_answer"] = json.loads(item["correct_answer"])
                item["is_correct"] = bool(item["is_correct"]) if item["is_correct"] is not None else None
            attempt["items"] = items
            return attempt
    finally:
        if _conn is None:
            conn.close()


def list_attempts_for_quiz(quiz_id: int) -> list[dict]:
    """Attempt history (summaries only) for a quiz, newest first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id AS attempt_id, quiz_id, score, total_questions, percentage, "
                "pending_self_grade, started_at, completed_at "
                "FROM quest_attempts WHERE quiz_id = %s ORDER BY started_at DESC",
                (quiz_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()
