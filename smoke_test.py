"""Offline smoke test. Stubs out the model so it needs no runtime and no
model downloads — run it first on a new machine to prove the plumbing works.

    python3 smoke_test.py

Exercises indexing, incremental updates, deletion, retrieval ranking, the
per-file cap, review scheduling, agent profiles, and the tool allowlist.
"""

import argparse
import json
import os
import shutil
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VAULT = ROOT / ".smoketest-vault"

shutil.rmtree(VAULT, ignore_errors=True)
(VAULT / "notes").mkdir(parents=True)
os.environ["VAULT_DIR"] = str(VAULT)
os.environ["VAULT_DB"] = str(VAULT / ".index" / "vault.db")
os.environ["VAULT_BACKEND"] = "ollama"  # pinned so nothing probes the network
os.environ["VAULT_EMBED_MODEL"] = "stub-embed"
sys.path.insert(0, str(ROOT))

import numpy as np
import vaultdb as v

# --- stub the model ------------------------------------------------------
DIM = 96


def fake_embed(texts):
    """Hashed bag-of-words. Similar text really does land close together, so
    retrieval ordering is meaningfully exercised.

    crc32 rather than hash(): the built-in is salted per process, which would
    make scores — and therefore any threshold assertion — differ run to run.
    """
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for word in t.lower().split():
            word = "".join(c for c in word if c.isalnum())
            if word:
                out[i, zlib.crc32(word.encode()) % DIM] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.clip(norms, 1e-9, None)


v.embed = fake_embed
v.chat_stream = lambda *a, **k: iter(["(stubbed answer) "])
v.chat = lambda *a, **k: "Q: What is the retry budget?\nA: Three attempts."

import agent
import ask
import index
import quiz

for module in (ask, index, agent):
    module.v.embed = fake_embed

failures = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {label}{(' — ' + detail) if detail and not condition else ''}")
    if not condition:
        failures.append(label)


def raises(fn) -> str | None:
    try:
        fn()
    except SystemExit as exc:
        return str(exc)
    return None


# --- fixture -------------------------------------------------------------
(VAULT / "notes" / "retries.md").write_text(
    "# Retry policy\n\n"
    "We retry idempotent requests three times with exponential backoff.\n"
    "Non-idempotent writes are never retried automatically.\n\n"
    "## Backoff\n\n"
    "Base delay is 200ms, doubling each attempt, with jitter.\n"
)
(VAULT / "notes" / "kafka.md").write_text(
    "# Kafka consumers\n\n"
    "Consumer lag alerts fire above 10000 messages.\n"
    + ("Partitions are rebalanced on deploy which causes a brief stall. " * 40)
)
(VAULT / "inbox.md").write_text("Random thought about gardening and tomatoes.\n")
(VAULT / ".index").mkdir(exist_ok=True)
(VAULT / ".index" / "should-be-ignored.md").write_text("# hidden\n\nmust not be indexed\n")

# --- 0. backend selection ------------------------------------------------
print("\n[0] backend configuration")
check("VAULT_BACKEND is respected without probing", v.backend() == "ollama", v.backend())
check("embed model honours the env override", v.embed_model() == "stub-embed")
check("ollama chat default", v.default_model("chat") == "qwen2.5-coder:7b")
check("lmstudio chat default differs",
      v.DEFAULT_MODELS["lmstudio"]["chat"] == "qwen2.5-coder-7b-instruct")
check("agent endpoint refuses non-lmstudio backends",
      "lmstudio" in (raises(lambda: v.agent_chat("hi", integrations=[{"id": "x"}])) or ""))

# --- 1. initial index ----------------------------------------------------
print("\n[1] initial index")
sys.argv = ["index.py", "--quiet"]
index.main()
conn = v.connect()
n_files = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
n_chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
check("indexed 3 files", n_files == 3, f"got {n_files}")
check("produced chunks", n_chunks >= 5, f"got {n_chunks}")
check("dot-directory excluded",
      conn.execute("SELECT COUNT(*) n FROM chunks WHERE path LIKE '.index%'").fetchone()["n"] == 0)
check("long note was split",
      conn.execute("SELECT COUNT(*) n FROM chunks WHERE path='notes/kafka.md'").fetchone()["n"] > 1)
check("headings captured",
      conn.execute("SELECT COUNT(*) n FROM chunks WHERE heading='Backoff'").fetchone()["n"] == 1)

# --- 2. incremental ------------------------------------------------------
print("\n[2] incremental re-index (no changes)")
before = conn.execute("SELECT MAX(id) m FROM chunks").fetchone()["m"]
conn.close()
index.main()
conn = v.connect()
after = conn.execute("SELECT MAX(id) m FROM chunks").fetchone()["m"]
check("unchanged files not re-embedded", before == after, f"{before} -> {after}")

print("\n[3] edit one file")
(VAULT / "inbox.md").write_text("Updated thought about retries and timeouts.\n")
conn.close()
index.main()
conn = v.connect()
txt = conn.execute("SELECT text FROM chunks WHERE path='inbox.md'").fetchone()["text"]
check("edited file re-embedded", "timeouts" in txt, txt[:40])

print("\n[4] delete one file")
(VAULT / "notes" / "kafka.md").unlink()
conn.close()
index.main()
conn = v.connect()
check("removed file purged from index",
      conn.execute("SELECT COUNT(*) n FROM chunks WHERE path='notes/kafka.md'").fetchone()["n"] == 0)
check("file row purged too",
      conn.execute("SELECT COUNT(*) n FROM files WHERE path='notes/kafka.md'").fetchone()["n"] == 0)

# --- 5. retrieval --------------------------------------------------------
print("\n[5] retrieval")
hits = ask.retrieve(conn, "how many times do we retry requests", 6, 0.0)
check("retrieval returned hits", len(hits) > 0, f"got {len(hits)}")
if hits:
    check("most relevant note ranked first", hits[0][1]["path"] == "notes/retries.md",
          hits[0][1]["path"])
    check("scores descend", all(hits[i][0] >= hits[i + 1][0] for i in range(len(hits) - 1)))

context, included = ask.build_context(hits, 200)
check("context respects the char budget", len(context) <= 400, f"{len(context)} chars")
check("at least one passage always included", len(included) >= 1)
check("citation markers present", context.startswith("[1] "))

# --- 6. per-file cap -----------------------------------------------------
print("\n[6] per-file cap")
(VAULT / "notes" / "long.md").write_text(
    "# Retries everywhere\n\n"
    + "\n\n".join(f"Retry attempt paragraph {i} about retrying requests repeatedly." * 12
                  for i in range(8))
)
conn.close()
index.main()
conn = v.connect()
counts = {}
for _, row in ask.retrieve(conn, "retry retrying requests", 6, 0.0):
    counts[row["path"]] = counts.get(row["path"], 0) + 1
check("no file exceeds 2 passages", all(c <= 2 for c in counts.values()), str(counts))

# --- 7. quiz scheduling --------------------------------------------------
print("\n[7] quiz scheduling")
check("all notes due initially", len(quiz.due_notes(conn)) == 3, str(len(quiz.due_notes(conn))))
quiz.record(conn, "notes/retries.md", remembered=True)
check("first success schedules 1 day",
      conn.execute("SELECT interval_days d FROM reviews WHERE path='notes/retries.md'").fetchone()["d"] == 1.0)
quiz.record(conn, "notes/retries.md", remembered=True)
check("second success climbs the ladder",
      conn.execute("SELECT interval_days d FROM reviews WHERE path='notes/retries.md'").fetchone()["d"] == 3.0)
quiz.record(conn, "notes/retries.md", remembered=False)
check("a miss resets to 1 day",
      conn.execute("SELECT interval_days d FROM reviews WHERE path='notes/retries.md'").fetchone()["d"] == 1.0)
check("reviewed note no longer due", "notes/retries.md" not in quiz.due_notes(conn))
check("question parsing handles sloppy formatting",
      len(quiz.parse_pairs("Q: Budget?\nA: Three.\n**Q2:** Backoff?\n**A:** 200ms.")) == 2)

# --- 8. agent profiles and the tool allowlist ----------------------------
print("\n[8] agent profiles")
profiles = agent.load_profiles()
check("default profiles load", {"ask", "code", "tools"} <= set(profiles))
check("ask profile is vault-grounded", profiles["ask"]["vault"] is True)
check("code profile takes no tools", profiles["code"]["integrations"] == [])
check("shipped tools profile is allowlisted",
      all(i.get("allowed_tools") for i in profiles["tools"]["integrations"]))

check("bare-string integration is refused",
      "allowlist" in (raises(lambda: agent.check_allowlists({"integrations": ["mcp/github"]}, False)) or ""))
check("missing allowed_tools is refused",
      "allowed_tools" in (raises(lambda: agent.check_allowlists(
          {"integrations": [{"type": "plugin", "id": "mcp/github"}]}, False)) or ""))
check("--allow-any overrides the refusal",
      raises(lambda: agent.check_allowlists(
          {"integrations": [{"type": "plugin", "id": "mcp/github"}]}, True)) is None)
check("a proper allowlist passes",
      raises(lambda: agent.check_allowlists(
          {"integrations": [{"type": "plugin", "id": "mcp/github",
                             "allowed_tools": ["search_code"]}]}, False)) is None)

# --- 9. agent response parsing -------------------------------------------
print("\n[9] agent response parsing")
sample = {
    "output": [
        {"type": "reasoning", "content": "deciding which tool"},
        {"type": "tool_call", "tool": "search_code", "arguments": {"q": "retry"}, "output": "3 hits"},
        {"type": "message", "content": "You retry three times."},
    ],
    "stats": {"input_tokens": 419, "total_output_tokens": 362,
              "tokens_per_second": 27.6, "time_to_first_token_seconds": 1.4},
}
check("transcript yields only the message text",
      agent.render(sample, show_reasoning=False) == "You retry three times.")
check("openai-shaped fallback still parses",
      agent.render({"choices": [{"message": {"content": "fallback"}}]}, False) == "fallback")
check("unknown shape does not crash",
      isinstance(agent.render({"weird": True}, False), str))

# --- 10. prompt assembly and saving --------------------------------------
print("\n[10] prompt assembly")
args = argparse.Namespace(file=None, no_vault=False, k=6, min_score=0.0,
                          max_context_chars=6000, max_file_chars=12000)
prompt = agent.build_prompt("how many retries", profiles["ask"], args, conn)
check("vault notes are injected", "Notes:" in prompt and "retry" in prompt.lower())
check("question comes last", prompt.strip().endswith("Question: how many retries"))

args.no_vault = True
check("--no-vault skips retrieval",
      "Notes:" not in agent.build_prompt("how many retries", profiles["ask"], args, conn))

probe = VAULT / "probe.py"
probe.write_text("def handler():\n    return retry(3)\n")
args.file = [str(probe)]
check("attached files are embedded in the prompt",
      "def handler" in agent.build_prompt("explain", profiles["code"], args, conn))

agent.save_to_daily("a question", "an answer")
daily = list((VAULT / "daily").glob("*.md"))
check("--save writes a dated daily note", len(daily) == 1, str(daily))
check("daily note contains the exchange",
      "an answer" in daily[0].read_text() if daily else False)

# --- 11. embed-model guard -----------------------------------------------
print("\n[11] embed model change guard")
conn.close()
os.environ["VAULT_EMBED_MODEL"] = "some-other-model"
check("model change blocks indexing",
      "rebuild" in (raises(index.main) or "").lower())
os.environ["VAULT_EMBED_MODEL"] = "stub-embed"

# --- 12. chat server -----------------------------------------------------
print("\n[12] chat web server")
import threading
import urllib.request

import chat

chat.v.chat_stream = lambda *a, **k: iter(["Three ", "attempts."])

httpd = chat.Server(("127.0.0.1", 0), chat.Handler)
httpd.profile = "ask"
httpd.verbose = False
httpd.min_score = 0.0  # the stub embedder scores lower than real ones
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.read().decode()


def sse(body):
    req = urllib.request.Request(
        base + "/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    events = []
    with urllib.request.urlopen(req, timeout=15) as r:
        for line in r:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


status, page = get("/")
check("serves the chat page", status == 200 and "<title>Vault Chat</title>" in page)
check("page has no external resources",
      "http://" not in page.split("<style>")[1].split("</script>")[0].replace(
          "http://www.w3.org", "") or "cdn" not in page.lower())

status, raw = get("/api/meta")
meta = json.loads(raw)
check("meta reports the backend", meta["backend"] == "ollama", meta["backend"])
check("meta lists profiles", "ask" in meta["profiles"] and "code" in meta["profiles"])
check("meta counts indexed passages", meta["chunks"] > 0, str(meta["chunks"]))

events = sse({"profile": "ask", "messages": [{"role": "user", "content": "how many retries"}]})
kinds = [e["type"] for e in events]
check("stream emits citations before tokens",
      "citations" in kinds and kinds.index("citations") < kinds.index("token"), str(kinds))
check("stream emits the answer in pieces", kinds.count("token") == 2, str(kinds))
check("answer reassembles correctly",
      "".join(e["text"] for e in events if e["type"] == "token") == "Three attempts.")
check("stream terminates with done", kinds[-1] == "done", str(kinds[-1]))
cites = [e for e in events if e["type"] == "citations"][0]["items"]
check("citations carry a source and score",
      bool(cites) and "where" in cites[0] and "score" in cites[0], str(cites[:1]))

events = sse({"profile": "code", "messages": [{"role": "user", "content": "hi"}]})
check("profiles without vault send no citations",
      "citations" not in [e["type"] for e in events])

events = sse({"profile": "nope", "messages": [{"role": "user", "content": "hi"}]})
check("unknown profile returns an error event",
      events and events[0]["type"] == "error", str(events[:1]))

events = sse({"profile": "ask", "messages": []})
check("empty message is rejected", events and events[0]["type"] == "error")

try:
    get("/nope")
    check("unknown path 404s", False, "no error raised")
except urllib.error.HTTPError as exc:
    check("unknown path 404s", exc.code == 404, str(exc.code))

httpd.shutdown()
httpd.server_close()

# --- 13. installer helpers -----------------------------------------------
print("\n[13] installer")
import install

check("every launcher maps to a real script",
      all((install.HERE / s).exists() for s in install.COMMANDS.values()),
      str(install.COMMANDS))
check("venv python path is platform-correct",
      str(install.venv_python()).endswith("python.exe" if sys.platform == "win32" else "python"))
check("config dir matches the one vaultdb reads",
      install.config_dir() == v.config_dir(), f"{install.config_dir()} vs {v.config_dir()}")
check("runtime help covers all three platforms",
      {"Linux", "Darwin", "Windows"} == set(install.RUNTIME_HELP))
check("scheduler names cover all three platforms",
      all(install._scheduler_name(p) != "the system scheduler"
          for p in ("Linux", "Darwin", "Windows")))

# --- 14. hardware detection and model choice -----------------------------
print("\n[14] hardware detection")
import hardware as hw

GB = hw.GB


def machine(ram_gb, bandwidth, cores=4, gpu_vram_gb=None, unified=False):
    gpus = []
    if gpu_vram_gb:
        gpus = [{"vendor": "nvidia", "name": "test", "vram": int(gpu_vram_gb * GB),
                 "unified": unified}]
    return {"ram_total": int(ram_gb * GB), "bandwidth_gbps": bandwidth,
            "physical_cores": cores, "logical_cores": cores * 2, "gpus": gpus}


check("catalog entries name a model for both runtimes",
      all(m.get("ollama") and m.get("lmstudio") and m.get("quants") for m in hw.CATALOG))
check("every quant has a size and an ordering",
      all(q in hw.QUANT_ORDER for m in hw.CATALOG for q in m["quants"]))

# Mixture-of-experts is the whole reason a 32 GB CPU box is viable: it should
# predict far faster than a dense model of the same file size.
moe = next(m for m in hw.CATALOG if m["key"] == "moe-large")
dense = next(m for m in hw.CATALOG if m["key"] == "medium")
moe_speed = hw.predict_tokens_per_second(moe, "Q4_K_M", 22.0)
dense_speed = hw.predict_tokens_per_second(dense, "Q4_K_M", 22.0)
check("MoE predicts faster than a larger-on-disk dense model",
      moe_speed > dense_speed * 2, f"moe {moe_speed} vs dense {dense_speed}")
check("bandwidth drives the prediction",
      hw.predict_tokens_per_second(dense, "Q4_K_M", 44.0) > dense_speed * 1.9)
check("no bandwidth means no fabricated number",
      hw.predict_tokens_per_second(dense, "Q4_K_M", None) is None)

big = hw.recommend(machine(32, 22.0, cores=8))
check("32 GB CPU box is given the MoE model", big["chat"]["key"] == "moe-large",
      big["chat"]["key"])
check("its recommendation clears the responsiveness bar",
      big["chat"]["tokens_per_second"] >= hw.RESPONSIVE,
      str(big["chat"]["tokens_per_second"]))
check("model fits inside the memory budget",
      big["chat"]["size_gb"] <= big["budget_gb"],
      f"{big['chat']['size_gb']} vs {big['budget_gb']}")
check("threads leave one core for the system", big["threads"] == 7, str(big["threads"]))

small = hw.recommend(machine(8, 12.0, cores=2))
check("8 GB box still gets a usable model", small["chat"] is not None)
check("8 GB box is not handed a 30B model", small["chat"]["size_gb"] < 5,
      str(small["chat"]["size_gb"]))
check("small core count keeps every core", small["threads"] == 2, str(small["threads"]))

# The flaw worth testing for: when nothing is fast, pick the quickest option
# rather than the biggest one that happens to fit.
slow = hw.recommend(machine(16, 4.0, cores=4))
alternatives = slow["alternatives"]
check("slow machine optimises for speed, not size",
      all((slow["chat"]["tokens_per_second"] or 0) >= (a["tokens_per_second"] or 0)
          for a in alternatives),
      f"{slow['chat']['label']} {slow['chat']['tokens_per_second']}")

gpu_box = hw.recommend(machine(16, 20.0, cores=8, gpu_vram_gb=24))
check("a discrete GPU switches placement", gpu_box["placement"] == "gpu")
check("GPU budget comes from VRAM, not system RAM", gpu_box["budget_gb"] > 16,
      str(gpu_box["budget_gb"]))
check("GPU box enables autocomplete", gpu_box["autocomplete"] is True)
check("CPU box with slow memory disables autocomplete",
      hw.recommend(machine(16, 6.0))["autocomplete"] is False)

check("context shrinks when memory is tight",
      hw.recommend(machine(8, 12.0))["context_length"] <= 8192)
check("context grows when memory is plentiful",
      hw.recommend(machine(128, 60.0, cores=16))["context_length"] >= 16384)
check("unified memory is not treated as separate VRAM",
      hw.recommend(machine(16, 50.0, gpu_vram_gb=16, unified=True))["placement"] == "ram")

probe_info = hw.collect(quick=True, disk_path=str(ROOT))
check("collect returns a full report without the benchmark",
      {"platform", "cpu", "ram_total", "gpus", "recommendation"} <= set(probe_info))
check("--quick really skips the benchmark", probe_info["bandwidth_gbps"] is None)
check("disk free space is reported", (probe_info["disk_free"] or 0) > 0)

# These probe the real machine. They exist because a thin PATH once made every
# macOS sysctl call fail silently, so RAM fell back to an assumed 8 GB and a
# 32 GB laptop was recommended a 3B model.
check("total RAM is actually detected", (probe_info["ram_total"] or 0) > 1 * GB,
      str(probe_info["ram_total"]))
check("RAM total is at least as large as RAM available",
      (probe_info["ram_total"] or 0) >= (probe_info["ram_available"] or 0),
      f"{probe_info['ram_total']} vs {probe_info['ram_available']}")
check("physical cores are detected", (probe_info["physical_cores"] or 0) > 0,
      str(probe_info["physical_cores"]))
check("physical cores do not exceed logical",
      (probe_info["physical_cores"] or 1) <= probe_info["logical_cores"])
check("cpu name is not a placeholder",
      probe_info["cpu"] not in ("i386", "unknown", ""), probe_info["cpu"])
check("a missing binary probe returns None rather than raising",
      hw._run(["definitely-not-a-real-binary-xyz"]) is None)
check("probe resolution finds binaries that exist",
      (hw._run(["ls", "/"]) is not None) if sys.platform != "win32" else True)
check("report renders without raising", hw.print_report(probe_info) is None)
check("hardware has a launcher", "vault-hardware" in install.COMMANDS)

shutil.rmtree(VAULT, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
