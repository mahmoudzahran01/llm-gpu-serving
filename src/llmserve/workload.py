"""Prompt workloads for load tests: one long *shared* system prompt plus a pool of short questions.

The shape matters more than the content. A text-to-SQL service sends the same multi-thousand-token schema with
every request, which is what makes prefix caching so effective (the shared prefix is prefilled once). The
published measurements used a ~1.6k-token schema prompt; this module can build a synthetic stand-in of any size
so the benchmark runs without anyone's real schema, or load your own prompt and questions from files.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

CHARS_PER_TOKEN = 4  # rough English/SQL average; use the server's reported prompt_tokens for exact numbers

ENTITIES = (
    "stations sensors readings alerts technicians sites circuits meters batches samples assays reagents vessels "
    "voyages ports cargos"
).split()
ATTRIBUTES = (
    ("state", "TEXT", "active | retired | pending"),
    ("magnitude", "REAL", "in SI units, 2 decimals"),
    ("zone", "TEXT", "site zone code"),
    ("created_at", "TIMESTAMP", "UTC"),
    ("updated_at", "TIMESTAMP", "UTC"),
    ("operator_id", "INTEGER", "foreign key to technicians"),
    ("kind", "TEXT", "free-text taxonomy, lower-case"),
    ("count", "INTEGER", "units"),
    ("severity", "INTEGER", "1 (highest) to 5"),
    ("is_retired", "BOOLEAN", "soft delete flag; exclude when true"),
)
QUESTION_TEMPLATES = (
    "What is the total magnitude of {entity} by zone in the last quarter? Return columns: zone, total.",
    "Which 5 {entity} have the highest count? Break ties by id. Return columns: id, count.",
    "How many {entity} were created each month this year? Return columns: month, n.",
    "ما عدد {entity} حسب الحالة؟ أعد العمودين: state, n.",
    "What share of {entity} is in the retired state, rounded to 2 decimals? Return columns: retired_share.",
    "List the distinct kinds of {entity} created after 2024-01-01. Return columns: kind.",
)


@dataclass(frozen=True)
class Workload:
    system: str
    questions: tuple[str, ...]


def approx_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _table(name: str, rng: random.Random, n_columns: int) -> str:
    singular = name.rstrip("s")
    lines = [f"TABLE {name} (one row per {singular})", f"  {singular}_id INTEGER  -- primary key"]
    for column, sql_type, note in rng.sample(ATTRIBUTES, k=min(n_columns, len(ATTRIBUTES))):
        lines.append(f"  {column} {sql_type}  -- {note}")
    return "\n".join(lines)


def _table_names(limit: int = 400) -> list[str]:
    """stations, sensors, ... then stations_2, sensors_2, ...: enough distinct names for any target size."""
    names = [
        e if round_ == 0 else f"{e}_{round_ + 1}" for round_ in range(limit // len(ENTITIES) + 1) for e in ENTITIES
    ]
    return names[:limit]


def synthetic_workload(target_tokens: int = 1600, seed: int = 7) -> Workload:
    """A deterministic fake database schema of roughly ``target_tokens`` tokens, plus matching questions."""
    rng = random.Random(seed)
    header = "You are a text-to-SQL engine for the analytics database described below.\n\nSCHEMA:\n"
    rules = "\n\nRULES:\n- Return only one SQL query, no prose.\n- Round money to 2 decimals.\n- Break ties deterministically."
    tables: list[str] = []
    used: list[str] = []
    for name in _table_names():
        tables.append(_table(name, rng, n_columns=6))
        used.append(name)
        if approx_tokens(header + "\n\n".join(tables) + rules) >= target_tokens:
            break
    questions = tuple(tpl.format(entity=name) for name in used[: len(ENTITIES)] for tpl in QUESTION_TEMPLATES)
    return Workload(header + "\n\n".join(tables) + rules, questions)


def load_questions(path: Path) -> tuple[str, ...]:
    """One question per line, or a jsonl file whose objects have a ``question`` field."""
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if lines and lines[0].startswith("{"):
        return tuple(json.loads(ln)["question"] for ln in lines)
    return tuple(lines)


def load_workload(system_file: Path | None, questions_file: Path | None, target_tokens: int = 1600) -> Workload:
    """Your own prompt/questions where given, the synthetic stand-in for whichever part is missing."""
    synthetic = synthetic_workload(target_tokens)
    system = system_file.read_text(encoding="utf-8").strip() if system_file else synthetic.system
    questions = load_questions(questions_file) if questions_file else synthetic.questions
    if not questions:
        raise ValueError("the questions file is empty")
    return Workload(system, questions)
