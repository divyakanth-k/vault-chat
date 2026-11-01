#!/usr/bin/env python3
"""Quiz yourself on notes you have not thought about in a while.

This is the step that moves knowledge into your head rather than onto your
disk. You answer first, then reveal — recalling before reading is what makes
it work, so resist the urge to peek.

    ./quiz.py              # one note that is due
    ./quiz.py -n 3         # three notes
    ./quiz.py --due        # what is waiting, without starting a session
    ./quiz.py --path notes/retries.md
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time

import vaultdb as v

# A simple expanding ladder: remembered moves you up a rung, missed resets.
LADDER = [1, 3, 7, 16, 35, 75, 160]
MAX_NOTE_CHARS = 4000

PROMPT = """You are writing recall questions to help someone remember their own note.

Write {n} short questions that test understanding of the specific content below —
not generic questions that could apply to any note. Each question must be
answerable from the note alone.

Format each one on exactly two lines, nothing else:
Q: <question>
A: <short answer, one or two sentences>

Note:
{body}"""


def due_notes(conn, limit: int | None = None):
    """Notes whose review interval has elapsed, oldest first. Notes never
    reviewed are due immediately."""
    now = time.time()
    rows = conn.execute("SELECT path FROM files ORDER BY path").fetchall()
    reviews = {
        r["path"]: (r["last_review"], r["interval_days"])
        for r in conn.execute("SELECT * FROM reviews")
    }

    pending = []
    for row in rows:
        path = row["path"]
        last, interval = reviews.get(path, (0.0, 0.0))
        if now - last >= interval * 86400:
            pending.append((last, path))
    pending.sort()
    paths = [p for _, p in pending]
    return paths[:limit] if limit else paths


def note_body(conn, path: str) -> str:
    rows = conn.execute(
        "SELECT heading, text FROM chunks WHERE path = ? ORDER BY ord", (path,)
    ).fetchall()
    parts = []
    for r in rows:
        parts.append((f"## {r['heading']}\n" if r["heading"] else "") + r["text"])
    return "\n\n".join(parts)[:MAX_NOTE_CHARS]


def parse_pairs(raw: str) -> list[tuple[str, str]]:
    """Loose parsing on purpose — small models drift from the format, and a
    missing blank line should not cost you the session."""
    pairs, question = [], None
    for line in raw.splitlines():
        line = line.strip()
        q = re.match(r"^(?:\**\s*)?Q(?:uestion)?\s*\d*\s*[:.)]\s*(.+)", line, re.I)
        a = re.match(r"^(?:\**\s*)?A(?:nswer)?\s*\d*\s*[:.)]\s*(.+)", line, re.I)
        if q:
            question = q.group(1).strip(" *")
        elif a and question:
            pairs.append((question, a.group(1).strip(" *")))
            question = None
    return pairs


def record(conn, path: str, remembered: bool) -> None:
    row = conn.execute("SELECT interval_days FROM reviews WHERE path = ?", (path,)).fetchone()
    current = row["interval_days"] if row else 0.0
    rung = 0
    for i, days in enumerate(LADDER):
        if current >= days:
            rung = i + 1
    nxt = LADDER[min(rung, len(LADDER) - 1)] if remembered else LADDER[0]
    conn.execute(
        "INSERT INTO reviews(path, last_review, interval_days) VALUES(?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET last_review=excluded.last_review, "
        "interval_days=excluded.interval_days",
        (path, time.time(), float(nxt)),
    )
    conn.commit()
    print(f"  → next review in {nxt} day{'s' if nxt != 1 else ''}")


def run_note(conn, path: str, count: int) -> None:
    body = note_body(conn, path)
    if len(body) < 120:
        print(f"{path}: too short to quiz, skipping.")
        return

    print(f"\n=== {path} ===")
    print("generating questions…", end="", flush=True)
    raw = v.chat(
        [{"role": "user", "content": PROMPT.format(n=count, body=body)}],
        temperature=0.6,
    )
    pairs = parse_pairs(raw)
    print("\r" + " " * 24 + "\r", end="")

    if not pairs:
        print("The model did not return usable questions. Try again, or use a "
              "larger chat model via VAULT_CHAT_MODEL.")
        return

    correct = 0
    for i, (question, expected) in enumerate(pairs, start=1):
        print(f"\n{i}. {question}")
        try:
            input("   (answer out loud or in your head, then press Enter) ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        print(f"   → {expected}")
        try:
            verdict = input("   did you get it? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if verdict.startswith("y"):
            correct += 1

    print(f"\n{correct}/{len(pairs)} recalled.")
    record(conn, path, remembered=correct >= len(pairs) / 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=1, help="how many notes to review (default 1)")
    parser.add_argument("-q", type=int, default=3, help="questions per note (default 3)")
    parser.add_argument("--due", action="store_true", help="list what is due and exit")
    parser.add_argument("--path", help="quiz one specific note, ignoring the schedule")
    args = parser.parse_args()

    conn = v.connect()

    if args.path:
        run_note(conn, args.path, args.q)
        return 0

    pending = due_notes(conn)
    if not pending:
        print("Nothing due. Come back tomorrow.")
        return 0

    if args.due:
        print(f"{len(pending)} note{'s' if len(pending) != 1 else ''} due:")
        for path in pending[:20]:
            print(f"  {path}")
        return 0

    # Shuffle the front of the queue so you are not always served the same
    # note first — variety keeps the habit alive.
    head = pending[: max(args.n * 3, 5)]
    random.shuffle(head)
    for path in head[: args.n]:
        run_note(conn, path, args.q)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
