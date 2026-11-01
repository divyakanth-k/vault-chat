#!/usr/bin/env python3
"""A local chat screen for your model, in the shape you already know.

One file, standard library only, no build step and no CDN — so it works with
the network cable unplugged. Serves a web UI on localhost that streams tokens
as they generate, shows which of your notes an answer came from, and switches
between the same profiles the command-line agent uses.

    ./chat.py                 # then open http://127.0.0.1:8737
    ./chat.py --open          # and launch the browser for you
    ./chat.py --port 9000

It binds to 127.0.0.1 deliberately. See --host before changing that.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
import ask
import vaultdb as v

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vault Chat</title>
<style>
  :root {
    --bg: #131314; --panel: #1a1a1c; --edge: #2a2a2e; --text: #e6e6e8;
    --dim: #9a9aa2; --faint: #6a6a72; --accent: #7aa2f7; --warn: #e0af68;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  aside {
    width: 230px; flex: none; border-right: 1px solid var(--edge); padding: 16px;
    display: flex; flex-direction: column; gap: 14px; background: var(--panel);
  }
  aside h1 { font-size: 13px; margin: 0; letter-spacing: .04em; text-transform: uppercase; color: var(--dim); }
  label { font-size: 11px; color: var(--faint); display: block; margin-bottom: 5px;
          text-transform: uppercase; letter-spacing: .05em; }
  select, button {
    width: 100%; background: #202024; color: var(--text); border: 1px solid var(--edge);
    border-radius: 6px; padding: 7px 9px; font: inherit; font-size: 13px; cursor: pointer;
  }
  select:hover, button:hover { border-color: #3a3a40; }
  #desc { font-size: 12px; color: var(--faint); line-height: 1.5; }
  #meta { margin-top: auto; font-size: 11px; color: var(--faint); line-height: 1.7;
          border-top: 1px solid var(--edge); padding-top: 12px; }
  #meta b { color: var(--dim); font-weight: 500; }
  main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #log { flex: 1; overflow-y: auto; padding: 28px 0; }
  .turn { max-width: 780px; margin: 0 auto 26px; padding: 0 28px; }
  .who { font-size: 11px; color: var(--faint); text-transform: uppercase;
         letter-spacing: .06em; margin-bottom: 7px; }
  .body { white-space: pre-wrap; word-wrap: break-word; }
  .turn.user .body {
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: 8px; padding: 11px 14px;
  }
  .body code { background: #202024; padding: 1px 5px; border-radius: 4px;
               font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; }
  .body pre { background: #0e0e0f; border: 1px solid var(--edge); border-radius: 8px;
              padding: 12px 14px; overflow-x: auto; margin: 10px 0; }
  .body pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.55; }
  .cites { margin-top: 12px; border-top: 1px solid var(--edge); padding-top: 10px; }
  .cites div { font-size: 12px; color: var(--faint); padding: 2px 0; }
  .cites b { color: var(--dim); font-weight: 500; }
  .tool { font-size: 12px; color: var(--warn); font-family: ui-monospace, Menlo, monospace;
          padding: 3px 0; }
  .stats { font-size: 11px; color: var(--faint); margin-top: 9px; }
  .err { color: #f7768e; }
  footer { border-top: 1px solid var(--edge); padding: 16px 28px 20px; }
  .composer { max-width: 780px; margin: 0 auto; display: flex; gap: 10px; align-items: flex-end; }
  textarea {
    flex: 1; resize: none; background: var(--panel); color: var(--text); font: inherit;
    border: 1px solid var(--edge); border-radius: 8px; padding: 11px 13px;
    max-height: 200px; min-height: 44px;
  }
  textarea:focus { outline: none; border-color: #3a3a45; }
  .composer button { width: auto; padding: 11px 18px; }
  .hint { max-width: 780px; margin: 8px auto 0; font-size: 11px; color: var(--faint); }
  .blink::after { content: "▍"; color: var(--accent); animation: b 1s steps(2) infinite; }
  @keyframes b { 0%,50% { opacity: 1 } 51%,100% { opacity: 0 } }
</style>
</head>
<body>
<aside>
  <h1>Vault Chat</h1>
  <div>
    <label for="profile">Profile</label>
    <select id="profile"></select>
    <div id="desc" style="margin-top:8px"></div>
  </div>
  <button id="reset">New conversation</button>
  <div id="meta"></div>
</aside>
<main>
  <div id="log"></div>
  <footer>
    <div class="composer">
      <textarea id="input" rows="1" placeholder="Ask something…" autofocus></textarea>
      <button id="send">Send</button>
    </div>
    <div class="hint">Enter sends · Shift+Enter for a new line · Esc stops generating</div>
  </footer>
</main>
<script>
const log = document.getElementById("log");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const profileSel = document.getElementById("profile");
const descEl = document.getElementById("desc");
let history = [];
let profiles = {};
let controller = null;

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Escape first, then add structure — so model output can never inject markup.
function render(src) {
  let out = esc(src);
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => "<pre><code>" + code + "</code></pre>");
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return out;
}

function addTurn(who, cls) {
  const wrap = document.createElement("div");
  wrap.className = "turn " + cls;
  wrap.innerHTML = '<div class="who">' + who + '</div><div class="body"></div>';
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return wrap.querySelector(".body");
}

async function loadMeta() {
  const r = await fetch("/api/meta");
  const m = await r.json();
  profiles = m.profiles;
  profileSel.innerHTML = "";
  for (const [name, p] of Object.entries(profiles)) {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    profileSel.appendChild(o);
  }
  profileSel.value = m.default_profile;
  showDesc();
  document.getElementById("meta").innerHTML =
    "<b>runtime</b> " + m.backend + "<br><b>model</b> " + m.model +
    "<br><b>vault</b> " + m.vault + "<br><b>notes</b> " + m.chunks + " passages indexed";
}

function showDesc() {
  const p = profiles[profileSel.value] || {};
  let d = p.description || "";
  if (p.tools && p.tools.length) d += " Tools: " + p.tools.join(", ") + ".";
  descEl.textContent = d;
}
profileSel.onchange = showDesc;

document.getElementById("reset").onclick = () => {
  history = []; log.innerHTML = ""; input.focus();
};

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  if (e.key === "Escape" && controller) controller.abort();
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
});
sendBtn.onclick = () => controller ? controller.abort() : send();

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; input.style.height = "auto";
  addTurn("you", "user").textContent = text;
  history.push({ role: "user", content: text });

  const body = addTurn(profileSel.value, "bot");
  body.classList.add("blink");
  sendBtn.textContent = "Stop";
  controller = new AbortController();

  let answer = "", cites = null, tools = [], stats = null;
  const paint = () => {
    let html = render(answer);
    for (const t of tools) html = '<div class="tool">· ' + esc(t) + "</div>" + html;
    if (cites && cites.length) {
      html += '<div class="cites">' + cites.map(c =>
        "<div><b>[" + c.n + "]</b> " + esc(c.where) + " · " + c.score + "</div>").join("") + "</div>";
    }
    if (stats) html += '<div class="stats">' + esc(stats) + "</div>";
    body.innerHTML = html;
    log.scrollTop = log.scrollHeight;
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: profileSel.value, messages: history }),
      signal: controller.signal,
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "token") { answer += ev.text; paint(); }
        else if (ev.type === "citations") { cites = ev.items; paint(); }
        else if (ev.type === "tool") { tools.push(ev.text); paint(); }
        else if (ev.type === "stats") { stats = ev.text; paint(); }
        else if (ev.type === "error") {
          body.innerHTML = '<span class="err">' + esc(ev.text) + "</span>";
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") body.innerHTML = '<span class="err">' + esc(String(e)) + "</span>";
  }

  body.classList.remove("blink");
  sendBtn.textContent = "Send";
  controller = null;
  if (answer) history.push({ role: "assistant", content: answer });
  else history.pop();
  paint();
  input.focus();
}

loadMeta();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VaultChat"

    def log_message(self, fmt, *args):
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No CORS header on purpose: only this page, served from this origin,
        # may call the API.
        self.end_headers()
        self.wfile.write(body)

    def _event(self, payload: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
        self.wfile.flush()

    # -- routes

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/meta":
            self._send(200, json.dumps(self.meta()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send(400, b"bad json", "text/plain")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.stream_reply(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser hit Stop; nothing to clean up

    # -- work

    def meta(self) -> dict:
        profiles = agent.load_profiles()
        conn = v.connect()
        chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        conn.close()
        return {
            "backend": v.backend(),
            "model": v.chat_model(),
            "vault": str(v.VAULT),
            "chunks": chunks,
            "default_profile": self.server.profile,  # type: ignore[attr-defined]
            "profiles": {
                name: {
                    "description": body.get("description", ""),
                    "tools": [
                        t
                        for i in body.get("integrations") or []
                        if isinstance(i, dict)
                        for t in (i.get("allowed_tools") or [])
                    ],
                }
                for name, body in profiles.items()
            },
        }

    def stream_reply(self, payload: dict) -> None:
        profiles = agent.load_profiles()
        name = payload.get("profile") or self.server.profile  # type: ignore[attr-defined]
        profile = profiles.get(name)
        if not profile:
            self._event({"type": "error", "text": f"unknown profile '{name}'"})
            return

        messages = [m for m in payload.get("messages", []) if m.get("content")]
        if not messages:
            self._event({"type": "error", "text": "empty message"})
            return
        question = messages[-1]["content"]

        # Ground the newest question in the vault, exactly as the CLI does.
        prompt = question
        if profile.get("vault"):
            conn = v.connect()
            try:
                hits = ask.retrieve(
                    conn, question,
                    self.server.k,          # type: ignore[attr-defined]
                    self.server.min_score,  # type: ignore[attr-defined]
                )
            finally:
                conn.close()
            if hits:
                context, included = ask.build_context(
                    hits, self.server.max_context_chars  # type: ignore[attr-defined]
                )
                prompt = f"Notes:\n\n{context}\n\nQuestion: {question}"
                self._event({
                    "type": "citations",
                    "items": [
                        {
                            "n": n,
                            "where": row["path"] + (f" › {row['heading']}" if row["heading"] else ""),
                            "score": f"{score:.2f}",
                        }
                        for n, (score, row) in enumerate(included, start=1)
                    ],
                })

        integrations = profile.get("integrations") or []
        temperature = profile.get("temperature", 0.2)

        try:
            if integrations:
                agent.check_allowlists(profile, False)
                data = v.agent_chat(
                    prompt,
                    integrations=integrations,
                    system=profile.get("system"),
                    temperature=temperature,
                )
                for item in data.get("output") or []:
                    if item.get("type") == "tool_call":
                        self._event({"type": "tool", "text": f"called {item.get('tool')}"})
                self._event({"type": "token", "text": agent.render(data, False)})
                stats = data.get("stats") or {}
                if stats.get("tokens_per_second"):
                    self._event({
                        "type": "stats",
                        "text": f"{stats['tokens_per_second']:.1f} tok/s · "
                                f"{stats.get('time_to_first_token_seconds', 0):.1f}s to first token",
                    })
            else:
                convo = [{"role": "system", "content": profile.get("system", "")}]
                convo += messages[:-1]
                convo.append({"role": "user", "content": prompt})
                for piece in v.chat_stream(convo, temperature=temperature):
                    self._event({"type": "token", "text": piece})
        except SystemExit as exc:
            self._event({"type": "error", "text": str(exc)})
        except Exception as exc:  # a dead runtime should not kill the server
            self._event({"type": "error", "text": f"{type(exc).__name__}: {exc}"})

        self._event({"type": "done"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    profile = "ask"
    verbose = False
    k = 6
    min_score = 0.25
    max_context_chars = 6000


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8737)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address — anything but 127.0.0.1 exposes an "
                             "unauthenticated assistant to your network")
    parser.add_argument("-p", "--profile", default="ask", help="profile selected on load")
    parser.add_argument("--open", action="store_true", help="open a browser once serving")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    parser.add_argument("-k", type=int, default=6, help="notes to retrieve (default 6)")
    parser.add_argument("--min-score", type=float, default=0.25,
                        help="similarity floor for notes (default 0.25)")
    parser.add_argument("--max-context-chars", type=int, default=6000,
                        help="prompt budget for notes; raising it slows replies")
    args = parser.parse_args()

    if args.profile not in agent.load_profiles():
        sys.exit(f"Unknown profile '{args.profile}'. Try ./agent.py --list.")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"!  Binding to {args.host}. This server has no authentication, so\n"
            f"!  anyone who can reach that address can use your model and read\n"
            f"!  answers drawn from your notes.\n",
            file=sys.stderr,
        )

    httpd = Server((args.host, args.port), Handler)
    httpd.profile = args.profile
    httpd.verbose = args.verbose
    httpd.k = args.k
    httpd.min_score = args.min_score
    httpd.max_context_chars = args.max_context_chars
    url = f"http://{args.host}:{args.port}"
    print(f"Vault Chat on {url}   (Ctrl-C to stop)")

    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
