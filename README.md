# vault-chat

A private AI setup that runs entirely on your own machine: offline coding help,
a knowledge system built on your own notes, a chat screen, and an agent you can
call whenever you need it. No accounts, no subscription, no data leaving the
computer.

Everything here is the Python standard library plus NumPy. The only network
destination is a model runtime on `localhost`.

---

## The one idea worth understanding first

On a CPU, generating a token means reading every weight the model uses out of
memory. So speed is roughly **memory bandwidth ÷ active bytes per token**. Not
core count, not how much RAM you have.

Three consequences shape every decision in this project:

1. **A smaller model is not just cheaper, it is the difference between usable
   and unusable.** An older laptop streams memory at roughly 20–25 GB/s. A 7B
   model at 4-bit is about 4.7 GB, which lands near 4 tokens per second.
2. **Mixture-of-experts models change the maths in your favour.** A "30B-A3B"
   model holds 30 billion weights but touches only about 3 billion per token.
   You pay the large size in RAM, which you probably have spare, and the small
   size in speed, which you do not.
3. **Prefill — the pause before the first word, while the model reads your
   prompt — is the real tax.** This is why the tools retrieve a few relevant
   passages instead of pasting whole files. The design that makes answers
   accurate and citable is the same one that makes them arrive quickly.

Run `./hardware.py` and it will measure your actual bandwidth and tell you what
your machine can run, rather than guessing.

### Retrieval, not fine-tuning

A common instinct is to train a model on your notes. For personal knowledge
that is the wrong tool, and on a CPU it is not a close call. Fine-tuning teaches
a model **how to behave** — a tone, a format, a style. It is bad at teaching it
**facts**: they smear across billions of weights, come back half-remembered, and
cannot cite a source. Retrieval does the opposite, and updating it means saving
a file.

---

## Requirements

- Python 3.9 or newer
- About 2 GB of disk for the tools, plus whatever your models need (5–20 GB)
- A model runtime: [LM Studio](https://lmstudio.ai) (recommended) or
  [Ollama](https://ollama.com)

LM Studio is recommended for one reason bigger than its nicer interface: since
version 0.4.0 it can execute MCP tool servers **for** the model over its API, so
`agent.py` gets real tool use without implementing an agent loop. The tradeoff is
that LM Studio is free but closed source; Ollama is open source but brings no MCP.
Both work here, and both can be installed side by side.

---

## Install

### Linux and macOS

```bash
./install.sh
```

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### Any platform, directly

```bash
python3 install.py
```

The installer creates a private virtual environment, installs NumPy, measures
your hardware, scaffolds a notes vault at `~/vault`, writes a config tuned to
your machine, generates launcher scripts in `./bin`, and runs the self-test. It
never edits your `PATH`, never installs anything system-wide, and never installs
a model runtime behind your back — where one is needed it prints the command.

| Flag | Effect |
|---|---|
| `--yes` | Accept every prompt (non-interactive) |
| `--vault PATH` | Put notes somewhere other than `~/vault` |
| `--schedule` | Also set up nightly re-indexing |
| `--quick` | Skip the memory bandwidth benchmark |
| `--no-verify` | Skip the self-test |
| `--check` | Report what is set up and change nothing |

### Then load two models

The installer prints the exact commands for *your* machine. They look like:

```bash
lms load qwen3-coder-30b-a3b-instruct --yes      # or whatever it recommended
lms load text-embedding-nomic-embed-text-v1.5 --yes
lms server start
```

You need both: a chat model for answering, and an embedding model for searching
your notes.

---

## The scripts

Each one runs standalone with `--help`. After install, the `bin/` launchers wrap
them with the right Python, so `bin/vault-ask` and `python3 ask.py` do the same
thing.

### `hardware.py` — what can this machine run?

Measures memory bandwidth with a short NumPy streaming benchmark, detects cores,
RAM, disk and GPU, and recommends a model, quantisation, context length and
thread count.

```bash
./hardware.py              # report and recommendations
./hardware.py --quick      # skip the benchmark (about a second faster)
./hardware.py --json       # machine-readable; this is what install.py consumes
```

It picks the largest model that both fits in memory **and** stays above roughly
8 tokens per second, then spends any leftover memory on precision rather than
more parameters — a 7B at Q6 beats a 14B at Q4 that runs half as fast. If nothing
can be quick it optimises for the fastest option rather than the biggest. If a
discrete GPU is present it sizes against VRAM instead and skips the CPU estimate,
which no longer applies.

### `index.py` — build the search index

Walks the vault and embeds anything that changed into a local SQLite database.

```bash
./index.py              # refresh changed files
./index.py --rebuild    # start over (needed if you change embedding model)
./index.py --stats      # what is in the index right now
```

It hashes file contents rather than trusting timestamps, so a nightly run over an
unchanged vault costs a few hundred milliseconds. It splits on markdown headings
and keeps the heading with each passage, so citations can point at a section
rather than just a file. Deleted notes are purged automatically.

**One guard worth knowing:** if you change the embedding model it refuses to run
and asks you to `--rebuild`. Vectors from two different models are not comparable,
and mixing them degrades every future search in a way that is hard to notice.

### `ask.py` — question your notes

```bash
./ask.py "what did I decide about retries?"
./ask.py --search "terraform"        # show matches without calling the model
./ask.py --show-context "..."        # print exactly what was sent
./ask.py                             # interactive loop
```

Answers carry inline citations and a source list with similarity scores.
`--search` is instant because it never touches the model, and it is what you will
reach for half the time. `--show-context` tells you whether a bad answer came
from bad retrieval or a bad model.

Useful flags: `-k` (how many passages), `--min-score` (similarity floor),
`--max-context-chars` (prompt budget — this is your speed dial).

### `agent.py` — the on-demand personal agent

```bash
./agent.py "what did I decide about retry budgets?"     # grounded in notes
./agent.py -p code --file src/handler.py "why twice?"   # grounded in a file
./agent.py -p tools "summarise today's commits"         # with MCP tools
./agent.py --save "explain SQLite WAL mode"             # append to today's note
./agent.py --list                                       # show profiles
```

A **profile** bundles a model, a system prompt, whether your notes are searched,
and which tools are reachable. Three ship by default:

| Profile | Notes searched | Tools | For |
|---|---|---|---|
| `ask` | yes | none | Daily driver, fastest |
| `code` | no | none | Coding questions; attach files with `--file` |
| `tools` | no | MCP | Anything needing outside data |

Add your own in `profiles.json` (see **Configuration**), using the same shape.

> **Safety:** when LM Studio executes MCP tools it does so server-side, which
> means **nothing pauses to ask you before a tool runs**. `agent.py` therefore
> refuses to run any profile whose MCP servers do not declare an `allowed_tools`
> allowlist. Override with `--allow-any` only if you mean it. Keep the allowlist
> to five or six tools: small models pick badly from long menus, and every tool
> schema costs prefill on every single turn.

### `chat.py` — a chat screen in your browser

```bash
./chat.py --open          # serve and open a browser
./chat.py --port 9000
```

One file, standard library only, no build step and no CDN — it works with the
network cable unplugged. Streams tokens as they generate, shows which notes an
answer drew on, and switches between the same profiles the agent uses.

Keyboard: Enter sends, Shift+Enter makes a new line, Escape stops generating.

It binds to `127.0.0.1` on purpose. There is **no authentication**, so anyone who
can reach the address can use your model and read answers drawn from your private
notes. If you need it from another device, tunnel to it (Tailscale's free tier,
or SSH port forwarding) rather than passing `--host 0.0.0.0`.

### `quiz.py` — remember what you wrote

```bash
./quiz.py              # one note that is due
./quiz.py -n 3         # three notes
./quiz.py --due        # what is waiting, without starting
```

Generates recall questions from notes you have not reviewed, makes you answer
before revealing, and schedules the next review on an expanding ladder — 1 day,
3, 7, 16, 35, 75, 160 — resetting if you miss.

This is the step most people skip and the one that compounds. An assistant that
answers everything for you will make you *feel* more knowledgeable while making
you less so, because you have outsourced the exact mental muscle you were trying
to build. For things you want to **know**, let it quiz you. For things you only
want to **look up**, let it answer.

### `smoke_test.py` — prove the plumbing works

```bash
python3 smoke_test.py
```

Stubs out the model entirely, so it needs no runtime and no downloads. Run it
first on a new machine. It covers indexing, incremental updates, deletion,
retrieval ranking, review scheduling, agent profiles, the tool allowlist, the
chat server over real HTTP, and the hardware recommendation logic.

---

## Configuration

Settings resolve in this order: **environment variable → config file →
built-in default**.

The config file lives at:

- Linux / macOS: `~/.config/vault-chat/config.json`
- Windows: `%APPDATA%\vault-chat\config.json`

```json
{
  "VAULT_DIR": "/home/you/vault",
  "VAULT_BACKEND": "lmstudio",
  "VAULT_CHAT_MODEL": "qwen3-coder-30b-a3b-instruct",
  "VAULT_EMBED_MODEL": "text-embedding-nomic-embed-text-v1.5",
  "VAULT_NUM_CTX": "8192",
  "VAULT_THREADS": "7"
}
```

| Key | Default | Notes |
|---|---|---|
| `VAULT_DIR` | `~/vault` | Where your notes live |
| `VAULT_DB` | `<vault>/.index/vault.db` | The search index |
| `VAULT_BACKEND` | `auto` | `auto`, `lmstudio` or `ollama` |
| `LMSTUDIO_HOST` | `http://127.0.0.1:1234` | |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | |
| `LM_API_TOKEN` | empty | Required for MCP over the LM Studio API |
| `VAULT_CHAT_MODEL` | per backend | Set by the installer from `hardware.py` |
| `VAULT_EMBED_MODEL` | per backend | Changing this requires `index.py --rebuild` |
| `VAULT_NUM_CTX` | `8192` | Bigger costs RAM *and* a longer prefill pause |
| `VAULT_THREADS` | `0` (runtime decides) | Physical cores, not logical ones |

Agent profiles go in `profiles.json` next to `config.json`, merged over the
built-in ones:

```json
{
  "research": {
    "description": "Read-only lookups against local files",
    "system": "Use the tools to answer. Say plainly if a tool fails.",
    "vault": false,
    "integrations": [
      { "type": "plugin", "id": "mcp/filesystem", "allowed_tools": ["read_file", "list_directory"] }
    ]
  }
}
```

### Re-tuning after a hardware change

```bash
./hardware.py                                   # see what it now recommends
python3 install.py --check                      # see what is configured
```

The installer never overwrites values already in your config, so to adopt new
recommendations delete the keys you want recomputed (or the whole file) and run
it again.

---

## Running it as a service

So the model is resident whenever you call it, rather than reloading from disk.

### Linux

```bash
cp systemd/lmstudio.service    ~/.config/systemd/user/
cp systemd/vault-index.service ~/.config/systemd/user/
cp systemd/vault-index.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lmstudio.service vault-index.timer
loginctl enable-linger $USER      # keep running after logout
```

`vault-index.service` sets `IPAddressAllow=localhost` and `IPAddressDeny=any`,
so the nightly indexer is *structurally* unable to reach the internet rather
than merely not configured to.

### macOS and Windows

`python3 install.py --schedule` sets up a launchd agent or a scheduled task for
the nightly re-index. For the model runtime, LM Studio has a "run the LLM server
on login" setting in its preferences.

---

## Security

- **Make offline enforceable, not assumed.** Block outbound network access for
  the inference process at the firewall — LuLu on macOS, a network namespace on
  Linux, an outbound block rule in Defender Firewall on Windows.
- **Leave everything bound to `127.0.0.1`.** Nothing here has authentication.
- **Prefer GGUF and safetensors.** Both are pure data. Older PyTorch `.bin` and
  `.pt` files are Python pickles, and loading one can execute arbitrary code.
- **Treat retrieved text as untrusted input.** If your agent both reads outside
  content and holds tools that touch your files, those capabilities combine into
  a real vulnerability — a document can carry instructions aimed at the model.
  The defence is architectural: keep reading untrusted content and holding tools
  in separate runs.
- **Back up the vault, not the models.** Notes are irreplaceable; weights are a
  download away. Keep the vault in a local git repository and the `.index/`
  folder out of it.
- **Keep work and personal separate.** Separate vault, separate machine, no
  shared index. Drawing this line on day one costs nothing; redrawing it later
  means auditing everything.

---

## Troubleshooting

**"No model runtime found"** — start one: `lms server start`, or `ollama serve`.
Check with `python3 install.py --check`.

**"Index is empty"** — run `./index.py`. If it reports 0 files, check `VAULT_DIR`
and remember that only `.md`, `.markdown` and `.txt` files are indexed, and that
dot-directories are skipped.

**"Index was built with X but the embed model is now Y"** — expected. Run
`./index.py --rebuild`.

**Answers are slow to start** — that is prefill, not generation. Lower
`--max-context-chars`, lower `-k`, or lower `VAULT_NUM_CTX`.

**Answers ignore the notes** — run `./ask.py --search "same question"`. If the
right note is not in the results, retrieval is the problem; try `--min-score 0.1`
or rephrase to match the words you actually wrote.

**Tool calls misfire** — reduce `allowed_tools`. Small models pick badly once
there are more than about six.

**numpy missing** — `python3 -m pip install --user numpy`, or re-run the
installer, which puts it in a private virtual environment.

---

## Uninstall

```bash
rm -rf ~/.config/vault-chat          # config  (%APPDATA%\vault-chat on Windows)
rm -rf /path/to/vault-chat           # the tools, venv and launchers
# your notes in ~/vault are untouched — delete them yourself if you want
systemctl --user disable --now vault-index.timer lmstudio.service   # Linux
```

Nothing was installed system-wide, so there is nothing else to undo.
