#!/usr/bin/env python3
"""A personal agent that runs on demand, grounded in your vault and — when
LM Studio is the backend — able to call MCP tools.

LM Studio 0.4.0+ runs the whole tool-calling loop server-side via
/api/v1/chat, so this file is deliberately thin: it chooses a profile,
grounds the question in your notes, and prints a readable transcript. There
is no hand-rolled agent loop to go wrong.

    ./agent.py "what did I decide about retry budgets?"
    ./agent.py -p code --file src/handler.py "why does this retry twice?"
    ./agent.py -p tools "what is open on the billing board?"
    ./agent.py --list
    ./agent.py                       # interactive, remembers earlier turns

Conversations are saved automatically and can be picked up again:

    ./agent.py --sessions            # list saved conversations
    ./agent.py -c                    # continue the most recent one (interactive)
    ./agent.py -c "and for writes?"  # continue it with a one-shot follow-up
    ./agent.py --resume 7            # continue conversation 7
    ./agent.py --no-history ...      # neither read nor write saved history

Profiles live in DEFAULT_PROFILES below and can be overridden or extended in
~/.config/vault-chat/profiles.json (same shape, merged over the defaults).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import ask
import vaultdb as v

PROFILE_FILE = v.config_dir() / "profiles.json"

CODE_SYSTEM = """You are a careful coding assistant working offline on a small model.

- Think about the specific code shown; never invent APIs, flags or filenames.
- If the context does not contain what you need, say exactly what is missing.
- Prefer a small, surgical change over a rewrite. Show only the lines that change.
- When unsure, say so in one sentence rather than guessing confidently."""

VAULT_SYSTEM = """You answer from the user's own notes, which appear below the question.

- Cite the note number inline, like [2], for each claim.
- If the notes do not answer it, say so plainly. Do not fill the gap from general knowledge.
- Be concise and concrete."""

DEFAULT_PROFILES: dict = {
    "ask": {
        "description": "Daily driver. Grounded in your vault, no tools, fastest.",
        "system": VAULT_SYSTEM,
        "vault": True,
        "integrations": [],
    },
    "code": {
        "description": "Coding questions. Attach files with --file; no vault, no tools.",
        "system": CODE_SYSTEM,
        "vault": False,
        "integrations": [],
        "temperature": 0.1,
    },
    "tools": {
        "description": "Read-only MCP tools. Edit the allowlist before using.",
        "system": "Use the available tools to answer. State plainly if a tool fails.",
        "vault": False,
        # Every integration MUST carry an allowed_tools list. Tools execute
        # server-side with no approval prompt, so this allowlist is the only
        # thing standing between a question and an action.
        "integrations": [
            {"type": "plugin", "id": "mcp/github", "allowed_tools": ["search_code", "get_file_contents"]},
        ],
    },
}


# ------------------------------------------------------------- profiles


def load_profiles() -> dict:
    profiles = json.loads(json.dumps(DEFAULT_PROFILES))  # deep copy
    if PROFILE_FILE.exists():
        try:
            for name, body in json.loads(PROFILE_FILE.read_text()).items():
                profiles.setdefault(name, {}).update(body)
        except (json.JSONDecodeError, AttributeError) as exc:
            sys.exit(f"{PROFILE_FILE} is not valid profile JSON: {exc}")
    return profiles


def check_allowlists(profile: dict, allow_any: bool) -> None:
    for integration in profile.get("integrations", []):
        if isinstance(integration, str):
            sys.exit(
                f"Integration '{integration}' is shorthand with no tool allowlist.\n"
                "Write it as {\"type\": \"plugin\", \"id\": \"...\", \"allowed_tools\": [...]}"
                " or pass --allow-any."
            )
        if not integration.get("allowed_tools") and not allow_any:
            sys.exit(
                f"Integration '{integration.get('id') or integration.get('server_label')}' "
                "has no allowed_tools.\nTools run server-side without an approval "
                "prompt, so an unrestricted server can take any action it exposes.\n"
                "Add allowed_tools, or pass --allow-any if you really mean it."
            )


# --------------------------------------------------------------- output


def render(data: dict, show_reasoning: bool) -> str:
    """LM Studio returns a transcript of reasoning / tool_call / message
    items. Parse defensively — print something useful even if the shape
    shifts in a future release."""
    items = data.get("output")
    if not isinstance(items, list):
        choices = data.get("choices")
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return json.dumps(data, indent=2)[:4000]

    answer_parts: list[str] = []
    for item in items:
        kind = item.get("type")
        if kind == "tool_call":
            args = item.get("arguments")
            pretty = json.dumps(args, separators=(", ", "=")) if isinstance(args, dict) else str(args)
            print(f"  · called {item.get('tool')}({pretty[:160]})", file=sys.stderr)
            output = str(item.get("output", ""))
            if output:
                print(f"    → {output[:200].strip()}", file=sys.stderr)
        elif kind == "reasoning" and show_reasoning:
            print(f"  · thinking: {str(item.get('content', ''))[:400]}", file=sys.stderr)
        elif kind == "message":
            answer_parts.append(str(item.get("content", "")))

    return "\n".join(p for p in answer_parts if p).strip()


def show_stats(data: dict) -> None:
    stats = data.get("stats") or {}
    if not stats:
        return
    rate = stats.get("tokens_per_second")
    ttft = stats.get("time_to_first_token_seconds")
    bits = []
    if stats.get("input_tokens") is not None:
        bits.append(f"{stats['input_tokens']} in")
    if stats.get("total_output_tokens") is not None:
        bits.append(f"{stats['total_output_tokens']} out")
    if rate:
        bits.append(f"{rate:.1f} tok/s")
    if ttft:
        bits.append(f"{ttft:.1f}s to first token")
    if bits:
        print(f"  [{' · '.join(bits)}]", file=sys.stderr)


def save_to_daily(question: str, answer: str) -> None:
    today = dt.date.today().isoformat()
    note = v.VAULT / "daily" / f"{today}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H:%M")
    with note.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {stamp} — {question[:70]}\n\n{answer}\n")
    print(f"  saved to {v.relative(note)}", file=sys.stderr)


# -------------------------------------------------------------- history

HISTORY_CHARS = 4000  # roughly 1,000 tokens of earlier turns


def trim_history(messages: list[dict], budget: int) -> list[dict]:
    """Keep the most recent turns that fit the budget. A small model has a
    small window and pays for every token on every turn, so old turns are
    dropped whole rather than truncated mid-sentence."""
    kept: list[dict] = []
    spent = 0
    for m in reversed(messages):
        spent += len(m["content"])
        if spent > budget and kept:
            break
        kept.append(m)
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # never open on an orphaned reply
        kept.pop(0)
    return kept


class Conversation:
    """The turns so far, optionally mirrored into the database.

    Held in memory even when saving is off, so an interactive session still
    has context; --no-history only stops it touching the database."""

    def __init__(self, conn, profile_name: str, save: bool, resume: int | None = None):
        self.conn = conn
        self.profile_name = profile_name
        self.save = save
        self.id = resume
        self.messages: list[dict] = v.session_messages(conn, resume) if resume else []

    def history(self, budget: int) -> list[dict]:
        return trim_history(self.messages, budget)

    def remember(self, question: str, answer: str) -> None:
        self.messages += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        if not self.save:
            return
        if self.id is None:
            self.id = v.create_session(self.conn, self.profile_name, question)
        v.add_messages(self.conn, self.id, [("user", question), ("assistant", answer)])


def print_sessions(conn) -> None:
    rows = v.list_sessions(conn, 30)
    if not rows:
        print("No saved conversations yet.")
        return
    print("Saved conversations (resume with --resume ID, or -c for the latest):\n")
    for r in rows:
        print(f"  {r['id']:>4}  {v.ago(r['updated_at']):<13} {r['profile']:<7} "
              f"{r['n'] // 2:>3} turns  {r['title']}")


# ------------------------------------------------------------------ run


def build_prompt(question: str, profile: dict, args, conn) -> str:
    parts: list[str] = []

    for path in args.file or []:
        p = Path(path).expanduser()
        if not p.is_file():
            sys.exit(f"No such file: {p}")
        body = p.read_text(encoding="utf-8", errors="replace")[: args.max_file_chars]
        parts.append(f"File: {p}\n```\n{body}\n```")

    if profile.get("vault") and not args.no_vault:
        hits = ask.retrieve(conn, question, args.k, args.min_score)
        if hits:
            context, included = ask.build_context(hits, args.max_context_chars)
            parts.append(f"Notes:\n\n{context}")
            for n, (score, row) in enumerate(included, start=1):
                where = row["path"] + (f" › {row['heading']}" if row["heading"] else "")
                print(f"  · [{n}] {where} ({score:.2f})", file=sys.stderr)
        else:
            print("  · no relevant notes found", file=sys.stderr)

    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


def run(question: str, profile: dict, args, conn, convo: Conversation | None = None) -> str:
    prompt = build_prompt(question, profile, args, conn)
    integrations = profile.get("integrations") or []
    temperature = profile.get("temperature", 0.2)
    model = args.model or profile.get("model")

    # No tools means no reason to use the agent endpoint — plain streaming
    # chat shows words as they arrive, which matters a lot at 8 tokens/sec.
    if not integrations:
        earlier = convo.history(args.history_chars) if convo else []
        messages = [
            {"role": "system", "content": profile.get("system", "")},
            *earlier,
            {"role": "user", "content": prompt},
        ]
        answer_parts = []
        for piece in v.chat_stream(messages, model=model, temperature=temperature):
            print(piece, end="", flush=True)
            answer_parts.append(piece)
        print()
        answer = "".join(answer_parts)
    else:
        print("  working…", file=sys.stderr)
        data = v.agent_chat(
            prompt,
            integrations=integrations,
            model=model,
            system=profile.get("system"),
            context_length=args.context_length,
            temperature=temperature,
        )
        if args.json:
            print(json.dumps(data, indent=2))
            return ""
        answer = render(data, args.show_reasoning)
        print(answer)
        show_stats(data)

    if args.save and answer.strip():
        save_to_daily(question, answer)
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", nargs="*")
    parser.add_argument("-p", "--profile", default=None,
                        help="which profile to use (default: ask, or the resumed conversation's)")
    parser.add_argument("--list", action="store_true", help="show available profiles and exit")
    parser.add_argument("--file", action="append", help="attach a file (repeatable)")
    parser.add_argument("--model", default=None, help="override the model for this run")
    parser.add_argument("--save", action="store_true", help="append the exchange to today's daily note")
    parser.add_argument("--no-vault", action="store_true", help="skip note retrieval for this run")
    parser.add_argument("--allow-any", action="store_true", help="permit integrations with no tool allowlist")
    parser.add_argument("--show-reasoning", action="store_true", help="print the model's reasoning steps")
    parser.add_argument("--json", action="store_true", help="dump the raw response (tool profiles only)")
    parser.add_argument("-k", type=int, default=6, help="notes to retrieve (default 6)")
    parser.add_argument("--min-score", type=float, default=0.25)
    parser.add_argument("--max-context-chars", type=int, default=6000, help="prompt budget for notes")
    parser.add_argument("--max-file-chars", type=int, default=12000, help="per-file truncation")
    parser.add_argument("--context-length", type=int, default=v.NUM_CTX)
    parser.add_argument("--sessions", action="store_true", help="list saved conversations and exit")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true",
                        help="continue the most recent saved conversation")
    parser.add_argument("--resume", type=int, default=None, metavar="ID",
                        help="continue a specific saved conversation")
    parser.add_argument("--no-history", action="store_true",
                        help="do not save this conversation or load saved ones")
    parser.add_argument("--history-chars", type=int, default=HISTORY_CHARS,
                        help="how much earlier conversation to send (default 4000)")
    args = parser.parse_args()

    profiles = load_profiles()

    if args.list:
        print(f"Profiles (override in {PROFILE_FILE}):\n")
        for name, body in profiles.items():
            tools = body.get("integrations") or []
            marks = []
            if body.get("vault"):
                marks.append("vault")
            if tools:
                marks.append(f"{len(tools)} MCP server{'s' if len(tools) != 1 else ''}")
            suffix = f"  [{', '.join(marks)}]" if marks else ""
            print(f"  {name:<8} {body.get('description', '')}{suffix}")
        return 0

    conn = v.connect()

    if args.sessions:
        print_sessions(conn)
        return 0

    resume = None
    if not args.no_history:
        resume = args.resume
        if resume is None and args.cont:
            resume = v.latest_session_id(conn)
        if resume is not None and v.get_session(conn, resume) is None:
            sys.exit(f"No saved conversation {resume}. Try --sessions.")

    stored = v.get_session(conn, resume) if resume is not None else None
    name = args.profile or (stored["profile"] if stored else "ask")
    if name not in profiles:
        sys.exit(f"Unknown profile '{name}'. Try --list.")
    profile = profiles[name]
    check_allowlists(profile, args.allow_any)

    convo = Conversation(conn, name, save=not args.no_history, resume=resume)
    if stored:
        print(f"  resuming #{stored['id']}: {stored['title']} "
              f"({len(convo.messages) // 2} earlier turns)", file=sys.stderr)
        if profile.get("integrations"):
            print("  note: tool profiles answer each question on its own; "
                  "earlier turns are not sent.", file=sys.stderr)

    if args.question:
        question = " ".join(args.question)
        answer = run(question, profile, args, conn, convo)
        if answer.strip():
            convo.remember(question, answer)
        return 0

    print(f"profile '{name}' · {v.backend()} · Ctrl-D to quit\n")
    while True:
        try:
            question = input("» ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question:
            answer = run(question, profile, args, conn, convo)
            if answer.strip():
                convo.remember(question, answer)
            print()


if __name__ == "__main__":
    raise SystemExit(main())
