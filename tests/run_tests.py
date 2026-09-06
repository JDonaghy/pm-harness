"""Offline test suite for ctx.py.

Unit tests cover the parser, folding, JWT handling and context builder.
Integration tests run ctx.py as a subprocess against a local mock
substrate server, exercising the full SignalR + tool loop.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ctx as ctxmod
from mock_substrate import MockSubstrate, script_reply

OID = "11111111-2222-3333-4444-555555555555"
TID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
EXP = 4102444800

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def make_token():
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return (enc({"alg": "none", "typ": "JWT"}) + "." +
            enc({"oid": OID, "tid": TID, "exp": EXP,
                 "aud": "https://substrate.office.com/sydney",
                 "preferred_username": "tester@example.org"}) + ".sig")


def unit_tests():
    tc = ctxmod.TOOL_CALL_START
    te = ctxmod.TOOL_CALL_END

    resp = f'{tc}\n{{"name": "read", "arguments": {{"path": "notes.txt"}}}}\n{te}'
    calls, txt = ctxmod.parse_tool_calls(resp)
    check("parse standard block", calls == [("read", {"path": "notes.txt"})] and txt == "",
          f"got {calls!r} / {txt!r}")

    resp = f'{tc}\n```json\n{{"name": "grep", "arguments": {{"pattern": "x",}}}}\n```\n{te}'
    calls, _ = ctxmod.parse_tool_calls(resp)
    check("parse fenced with trailing comma",
          calls == [("grep", {"pattern": "x"})], f"got {calls!r}")

    calls, _ = ctxmod.parse_tool_calls('{"name": "list", "arguments": {"path": "."}}')
    check("parse bare json", calls == [("list", {"path": "."})], f"got {calls!r}")

    resp = f'**Evaluating**\nsome thinking\n\n{tc}\n{{"name": "read", "arguments": {{"path": "a"}}}}\n{te}'
    calls, _ = ctxmod.parse_tool_calls(resp)
    check("parse after thinking prefix",
          calls == [("read", {"path": "a"})], f"got {calls!r}")

    resp = tc + '\n{"name": "write", "arguments": "{\\"a\\": 1}"}\n' + te
    calls, _ = ctxmod.parse_tool_calls(resp)
    check("parse string arguments coerced",
          calls == [("write", {"a": 1})], f"got {calls!r}")

    calls, txt = ctxmod.parse_tool_calls("plain answer, no tools")
    check("parse no tools", calls == [] and txt == "plain answer, no tools",
          f"got {calls!r} / {txt!r}")

    check("fold growth", ctxmod.fold_text("", "abc") == ("abc", "abc"))
    check("fold append", ctxmod.fold_text("abc", "abcdef") == ("abcdef", "def"))
    check("fold snapshot diverges", ctxmod.fold_text("abc", "xyzabc") == ("xyzabc", None))
    check("fold shrink ignored", ctxmod.fold_text("abc", "ab") == ("abc", None))

    check("confab detected",
          ctxmod.looks_like_confabulation(
              "I don't have access to the files in this session. "
              "Please paste the contents."))
    check("confab not flagged on normal text",
          not ctxmod.looks_like_confabulation(
              "The function foo() in app.py returns 42 on line 3."))

    check("thinking stripped",
          ctxmod.strip_thinking("**Planning**\n\nDo the thing.") == "Do the thing.")

    claims = ctxmod.jwt_decode(make_token())
    check("jwt decode", claims["oid"] == OID and claims["tid"] == TID and claims["exp"] == EXP,
          f"got {claims!r}")

    cfg = ctxmod.Config(ROOT / "nonexistent-home-xyz")
    check("tone map default model", cfg.data["model"] == "gpt-5.6")
    check("tone map lookup", cfg.model_tone("gpt-5.6") == "Gpt_5_6_Reasoning"
          and cfg.model_tone("auto") == "magic")
    try:
        cfg.model_tone("nope")
        check("tone map rejects unknown", False)
    except ctxmod.CtxError:
        check("tone map rejects unknown", True)

    tmp = Path(tempfile.mkdtemp(prefix="ctxunit-"))
    try:
        (tmp / "notes.txt").write_text("alpha bravo charlie\n")
        (tmp / "src").mkdir()
        (tmp / "src" / "app.py").write_text("print('hi')\n")
        (tmp / "node_modules").mkdir()
        (tmp / "node_modules" / "skip.js").write_text("junk\n")
        (tmp / ".git").mkdir()
        (tmp / ".git" / "config").write_text("git\n")
        (tmp / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        pctx = ctxmod.ProjectContext(tmp, 8).rebuild()
        rels = [str(p.relative_to(tmp)) for p in pctx.files]
        check("walk skips node_modules and .git",
              "node_modules/skip.js" not in rels and ".git/config" not in rels
              and "notes.txt" in rels, f"got {rels!r}")
        tree, blocks = pctx.render()
        check("render includes text file",
              "--- file: notes.txt ---" in blocks and "alpha bravo charlie" in blocks)
        check("render excludes binary content", "blob.bin" not in blocks)
        pctx.dropped.append("*.py")
        pctx.rebuild()
        tree2, blocks2 = pctx.render()
        check("drop glob removes file", "app.py" not in blocks2 and "app.py" not in tree2)

        cfg = ctxmod.Config(tmp / "home")
        app = ctxmod.App(cfg, tmp)
        app.history = [
            {"role": "user", "text": "fix the bug"},
            {"role": "assistant", "text": ctxmod.TOOL_CALL_START +
             '\n{"name": "read", "arguments": {"path": "notes.txt"}}\n' +
             ctxmod.TOOL_CALL_END},
            {"role": "tool", "text": "[Result of read]: alpha bravo charlie"},
            {"role": "user", "text": "and now summarize"},
        ]
        msgs = ctxmod.build_http_messages(app)
        check("http messages roles", [m["role"] for m in msgs] ==
              ["user", "assistant", "user"], f"got {[m['role'] for m in msgs]}")
        check("http messages first user gets files",
              "--- file: notes.txt ---" in msgs[0]["content"]
              and "fix the bug" in msgs[0]["content"])
        check("http messages tool merged into user turn",
              "[Result of read]: alpha bravo charlie" in msgs[2]["content"]
              and "and now summarize" in msgs[2]["content"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_http_mock(provider):
    log = []

    def content_text(content):
        if isinstance(content, list):
            return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content or "")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            if provider == "responses":
                items = body.get("input", [])
            else:
                items = body.get("messages", [])
            last = content_text(items[-1]["content"]) if items else ""
            model = body.get("model")
            reply = script_reply(last, "http")
            log.append({"provider": provider, "model": model, "head": last[:100],
                        "auth": self.headers.get("Authorization", ""),
                        "path": self.path})
            if provider == "chat":
                out = {"choices": [{"message": {"role": "assistant", "content": reply}}]}
            elif provider == "messages":
                out = {"content": [{"type": "text", "text": reply}]}
            else:
                out = {"output": [{"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": reply}]}]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, log


def integration_tests():
    tmp = Path(tempfile.mkdtemp(prefix="ctxint-"))
    token = make_token()
    log_path = tmp / "mocklog.jsonl"
    server = MockSubstrate(log_path)
    server.start()
    ctx_py = str(ROOT / "ctx.py")
    try:
        env_base = dict(
            os.environ,
            CTX_TOKEN=token,
            CTX_WS_BASE=f"ws://127.0.0.1:{server.port}/m365Copilot/Chathub",
        )

        def run_ctx(args, stdin=None, home=None):
            env = dict(env_base, CTX_HOME=str(home or (tmp / "home")))
            return subprocess.run([sys.executable, ctx_py] + args,
                                  input=stdin, capture_output=True, text=True,
                                  env=env, timeout=90)

        def fresh_project(name):
            proj = tmp / name
            proj.mkdir()
            (proj / "notes.txt").write_text("alpha bravo charlie\n")
            return proj

        proj = fresh_project("p_read")
        r = run_ctx(["--dir", str(proj), "ask", "Read notes.txt please. TOUCHSTONE-READ"])
        check("ask read: exit 0", r.returncode == 0, r.stderr[-400:])
        check("ask read: tool executed", "-> read notes.txt" in r.stdout, r.stdout[-400:])
        check("ask read: final answer", "The notes say: alpha bravo charlie" in r.stdout,
              r.stdout[-400:])

        entries = server.entries
        check("mock saw 2 turns", len(entries) >= 2, f"{len(entries)} entries")
        if len(entries) >= 2:
            first, second = entries[-2], entries[-1]
            check("first turn isStartOfSession", first["is_start"] is True, str(first))
            check("second turn not start", second["is_start"] is False, str(second))
            check("default tone is gpt-5.6", first["tone"] == "Gpt_5_6_Reasoning",
                  str(first["tone"]))
            path = first["path"]
            check("ws path carries oid@tid", f"{OID}@{TID}" in path, path[:120])
            q = urllib.parse.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
            check("ws url carries the token", q.get("access_token") == [token])

        proj = fresh_project("p_write")
        r = run_ctx(["--dir", str(proj), "ask", "Create hello.txt. TOUCHSTONE-WRITE"],
                    stdin="y\n")
        wrote = (proj / "hello.txt")
        check("write confirmed: file exists", wrote.exists() and wrote.read_text() == "world",
              f"exists={wrote.exists()}")
        check("write confirmed: tool shown", "-> write hello.txt" in r.stdout, r.stdout[-400:])
        check("write confirmed: model ack", "WROTE_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_deny")
        r = run_ctx(["--dir", str(proj), "ask", "Create hello.txt. TOUCHSTONE-WRITE"],
                    stdin="\n")
        check("write denied: no file", not (proj / "hello.txt").exists())
        check("write denied: model ack", "DENIED_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_run")
        r = run_ctx(["--yes", "--dir", str(proj), "ask", "Run the command. TOUCHSTONE-RUN"])
        check("run tool: executed locally", "mockrun" in r.stdout, r.stdout[-400:])
        check("run tool: model ack", "RAN_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_repl")
        r = run_ctx(["--dir", str(proj)],
                    stdin="/model auto\nwhat tone is live? TOUCHSTONE-TONE\n/q\n")
        check("repl: model switch", "model set to auto" in r.stdout, r.stdout[-600:])
        check("repl: tone answer", "CURRENT_TONE=magic" in r.stdout, r.stdout[-600:])
        tone_entries = [e for e in server.entries if e.get("tone") == "magic"]
        check("repl: mock saw magic tone", len(tone_entries) >= 1)

        proj = fresh_project("p_confab")
        r = run_ctx(["--dir", str(proj), "ask", "Inspect the project. TOUCHSTONE-CONFAB"])
        check("confab: nudge fired", "nudging" in r.stderr, r.stderr[-400:])
        check("confab: recovered", "RECOVERED" in r.stdout, r.stdout[-400:])
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def integration_tests_http():
    tmp = Path(tempfile.mkdtemp(prefix="ctxhttp-"))
    ctx_py = str(ROOT / "ctx.py")
    try:
        for provider, expected_path in (("chat", "/v1/chat/completions"),
                                        ("messages", "/v1/messages"),
                                        ("responses", "/v1/responses")):
            server, log = make_http_mock(provider)
            tag = provider
            try:
                env = dict(os.environ,
                           CTX_PROVIDER=provider,
                           CTX_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}/v1",
                           CTX_API_KEY="testkey",
                           CTX_MODEL="test-model")

                def run_http(args, stdin=None):
                    return subprocess.run(
                        [sys.executable, ctx_py] + args, input=stdin,
                        capture_output=True, text=True,
                        env=dict(env, CTX_HOME=str(tmp / f"home-{tag}")), timeout=90)

                proj = tmp / f"proj-{tag}"
                proj.mkdir()
                (proj / "notes.txt").write_text("alpha bravo charlie\n")
                r = run_http(["--dir", str(proj), "ask",
                              "Read notes.txt please. TOUCHSTONE-READ"])
                check(f"http {provider}: exit 0", r.returncode == 0, r.stderr[-400:])
                check(f"http {provider}: tool executed",
                      "-> read notes.txt" in r.stdout, r.stdout[-400:])
                check(f"http {provider}: final answer",
                      "The notes say: alpha bravo charlie" in r.stdout, r.stdout[-400:])
                check(f"http {provider}: model id sent",
                      all(e["model"] == "test-model" for e in log) and len(log) >= 2,
                      f"{len(log)} entries")
                check(f"http {provider}: bearer auth",
                      all(e["auth"] == "Bearer testkey" for e in log))
                check(f"http {provider}: request path",
                      all(e["path"] == expected_path for e in log),
                      str({e["path"] for e in log}))

                if provider == "chat":
                    proj2 = tmp / f"proj-{tag}-write"
                    proj2.mkdir()
                    (proj2 / "notes.txt").write_text("alpha\n")
                    r = run_http(["--dir", str(proj2), "ask",
                                  "Create hello.txt. TOUCHSTONE-WRITE"], stdin="y\n")
                    check("http chat: write confirmed",
                          (proj2 / "hello.txt").exists()
                          and (proj2 / "hello.txt").read_text() == "world"
                          and "WROTE_OK" in r.stdout, r.stdout[-400:])

                    r = run_http(["--dir", str(proj)], stdin="/model my-custom-id\n/q\n")
                    check("http chat: /model passthrough",
                          "model set to my-custom-id" in r.stdout, r.stdout[-600:])

                    env_full = dict(env, CTX_BASE_URL=(
                        f"http://127.0.0.1:{server.server_address[1]}"
                        "/v1/chat/completions"))
                    before = len(log)
                    subprocess.run([sys.executable, ctx_py, "--dir", str(proj), "ask",
                                    "Read notes.txt please. TOUCHSTONE-READ"],
                                   capture_output=True, text=True,
                                   env=dict(env_full, CTX_HOME=str(tmp / "home-full")),
                                   timeout=90)
                    new_paths = {e["path"] for e in log[before:]}
                    check("http chat: full-url base not doubled",
                          new_paths == {"/v1/chat/completions"}, str(new_paths))
            finally:
                server.shutdown()
                server.server_close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"python {sys.version.split()[0]}   root={ROOT}\n")
    unit_tests()
    print()
    integration_tests()
    print()
    integration_tests_http()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
