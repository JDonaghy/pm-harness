"""Offline test suite for ctx.py.

Unit tests cover the parser, folding, JWT handling and context builder.
Integration tests run ctx.py as a subprocess against a local mock
substrate server, exercising the full SignalR + tool loop.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from contextlib import redirect_stdout
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

        gproj = tmp / "gproj"
        gproj.mkdir()
        (gproj / "graphify-out").mkdir()
        (gproj / "graphify-out" / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "a"}, {"id": "b"}],
                        "edges": [{"source": "a", "target": "b"}]}))
        check("graph detection on", ctxmod.graph_available(gproj))
        check("graph detection off", not ctxmod.graph_available(tmp))

        sp = ctxmod.system_prompt(gproj, "BE-NICE-MARKER", True)
        check("prompt: instructions and graph advertised",
              "BE-NICE-MARKER" in sp and "KNOWLEDGE GRAPH" in sp
              and "graph_query" in sp and "graph_explain" in sp)
        sp2 = ctxmod.system_prompt(tmp, "", False)
        check("prompt: no graph section when absent",
              "graph_query" not in sp2 and "KNOWLEDGE GRAPH" not in sp2)

        (tmp / "home").mkdir(exist_ok=True)
        (tmp / "home" / "AGENTS.md").write_text("global-rule-77\n")
        (gproj / "AGENTS.md").write_text("project-rule-88\n")
        instr = ctxmod.load_instructions(cfg, gproj)
        check("instructions global+project loaded in order",
              "global-rule-77" in instr and "project-rule-88" in instr
              and instr.index("global-rule-77") < instr.index("project-rule-88"))
        check("instructions missing when no files",
              ctxmod.load_instructions(ctxmod.Config(tmp / "home2"), tmp) == "")

        saved = {k: os.environ.get(k) for k in
                 ("CTX_GRAPHIFY_CMD", "FAKE_GRAPHIFY_LOG")}
        glog = tmp / "glog.jsonl"
        os.environ["CTX_GRAPHIFY_CMD"] = f"{sys.executable} {ROOT / 'tests' / 'fake_graphify.py'}"
        os.environ["FAKE_GRAPHIFY_LOG"] = str(glog)
        try:
            gcfg = ctxmod.Config(tmp / "home3")
            gapp = ctxmod.App(gcfg, gproj)
            out = gapp.tools.dispatch("graph_query", {"question": "how does auth work"})
            check("graph_query via fake cli",
                  "GRAPH-ANSWER[how does auth work]" in out, out)
            out = gapp.tools.dispatch("graph_path", {"from": "Auth", "to": "Database"})
            check("graph_path arg mapping", "GRAPH-PATH: Auth -> Database" in out, out)
            out = gapp.tools.dispatch("graph_explain", {"node": "SwinTransformer"})
            check("graph_explain arg mapping", "GRAPH-EXPLAIN: SwinTransformer" in out, out)
            logged = [json.loads(l) for l in glog.read_text().splitlines()]
            check("graph cli argv for query",
                  logged[0] == ["query", "how does auth work", "--budget", "2000"],
                  str(logged[0]))
            check("graph cli argv for path",
                  logged[1] == ["path", "Auth", "Database"], str(logged[1]))
            napp = ctxmod.App(gcfg, tmp)
            out = napp.tools.dispatch("graph_query", {"question": "x"})
            check("graph tool without graph errors", "no knowledge graph" in out, out)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        itrack = tmp / "itrack"
        itrack.mkdir()
        check("issues: missing file", ctxmod.load_issues(itrack) is None)
        (itrack / "issues.json").write_text("{not json")
        check("issues: invalid json", ctxmod.load_issues(itrack) is None)
        (itrack / "issues.json").write_text('{"nested": true}')
        check("issues: non-array rejected", ctxmod.load_issues(itrack) is None)
        (itrack / "issues.json").write_text(
            json.dumps([{"id": "x", "title": "t", "status": "open"}]))
        check("issues: array loaded",
              ctxmod.load_issues(itrack) == [{"id": "x", "title": "t", "status": "open"}])
        brief = ctxmod.format_issue_brief(
            {"id": "x", "title": "t", "status": "open", "priority": "high",
             "summary": "sum", "acceptance": ["a1", "a2"], "notes": "n"})
        check("issues: brief renders all fields",
              all(s in brief for s in ("x - t", "status  : open", "priority: high",
                                       "summary : sum", "accept1: a1", "notes   : n")))

        espec = next((s for s in ctxmod.TOOL_SPECS if s[0] == "edit"), None)
        check("edit: tool spec params",
              espec is not None
              and espec[2] == "path*, old*, new*, replace_all (optional bool)",
              str(espec))
        check("edit: advertised in prompt",
              "- edit:" in ctxmod.system_prompt(tmp, "", False))

        eproj = tmp / "eproj"
        eproj.mkdir()
        (eproj / "notes.txt").write_text("alpha bravo charlie\n")
        eapp = ctxmod.App(ctxmod.Config(tmp / "ehome"), eproj, auto_yes=True)
        ecap = io.StringIO()
        with redirect_stdout(ecap):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "bravo", "new": "delta"})
        check("edit: applies single match",
              "1 replacement" in out
              and (eproj / "notes.txt").read_text() == "alpha delta charlie\n", out)
        check("edit: renders unified diff",
              "-alpha bravo charlie" in ecap.getvalue()
              and "+alpha delta charlie" in ecap.getvalue(), ecap.getvalue()[:200])
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "zebra", "new": "x"})
        check("edit: no-match errors clearly", "not found" in out, out)
        (eproj / "notes.txt").write_text("alpha bravo alpha charlie\n")
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "alpha", "new": "delta"})
        check("edit: ambiguous match errors clearly",
              "2 times" in out and "replace_all" in out
              and (eproj / "notes.txt").read_text() == "alpha bravo alpha charlie\n",
              out)
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "alpha", "new": "delta",
                                               "replace_all": True})
        check("edit: replace_all applies every match",
              "2 replacements" in out
              and (eproj / "notes.txt").read_text() == "delta bravo delta charlie\n",
              out)
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "gone.txt",
                                               "old": "a", "new": "b"})
        check("edit: missing file errors", out.startswith("ERROR:"), out)
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"old": "a", "new": "b"})
        check("edit: missing args error", "requires path" in out, out)
        (eproj / "notes.txt").write_text("alpha bravo alpha charlie\n")
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "alpha", "new": "delta",
                                               "replace_all": "true"})
        check("edit: string replace_all coerced true",
              "2 replacements" in out, out)
        (eproj / "notes.txt").write_text("alpha bravo alpha charlie\n")
        with redirect_stdout(io.StringIO()):
            out = eapp.tools.dispatch("edit", {"path": "notes.txt",
                                               "old": "alpha", "new": "delta",
                                               "replace_all": "false"})
        check("edit: string replace_all coerced false",
              "2 times" in out, out)

        nr = ctxmod.normalize_resume_argv
        check("resume argv: bare flag", nr(["--resume"]) == ["--resume="])
        check("resume argv: before subcommand",
              nr(["--resume", "ask", "hi"]) == ["--resume=", "ask", "hi"])
        check("resume argv: before option",
              nr(["--resume", "--dir", "x"]) == ["--resume=", "--dir", "x"])
        check("resume argv: id consumed", nr(["--resume", "ab12"]) == ["--resume", "ab12"])
        check("resume argv: inline form untouched", nr(["--resume=ab12"]) == ["--resume=ab12"])
        check("resume argv: passthrough",
              nr(["--dir", "x", "ask", "hi"]) == ["--dir", "x", "ask", "hi"])

        shome = tmp / "shome"
        sapp = ctxmod.App(ctxmod.Config(shome), tmp)
        sapp.history = [{"role": "user", "text": "hello"}]
        sid = sapp.save_session()
        sp = shome / "sessions" / (sid + ".json")
        check("session: save mints id and writes file",
              bool(sid) and len(sid) == 8 and sp.is_file(), str(sid))
        sapp.history.append({"role": "assistant", "text": "hi"})
        check("session: resave keeps slot", sapp.save_session() == sid)
        sdata = json.loads(sp.read_text())
        check("session: file holds history and meta",
              len(sdata["history"]) == 2 and sdata["model"] == sapp.model
              and sdata["provider"] == "m365" and sdata["root"] == str(tmp)
              and sdata["created"] and sdata["updated"], str(sdata)[:200])
        check("session: save skipped on empty history",
              ctxmod.App(ctxmod.Config(tmp / "shome9"), tmp).save_session() is None)

        rapp = ctxmod.App(ctxmod.Config(shome), tmp)
        n = rapp.restore_session({
            "id": sid, "created": "c",
            "history": [{"role": "user", "text": "a"},
                        {"role": "bogus", "text": "x"},
                        {"text": "no role"},
                        {"role": "tool", "text": "t"}],
            "model": "auto", "conversation_id": "conv-1",
            "session_id": "sess-1", "turn_count": 3})
        check("session: restore sanitizes history", n == 2 and len(rapp.history) == 2)
        check("session: restore slot, model and m365 ids",
              rapp.session_slot == sid and rapp.model == "auto"
              and rapp.session.conversation_id == "conv-1"
              and rapp.session.session_id == "sess-1"
              and rapp.session.turn_count == 3)
        rapp.history.append({"role": "user", "text": "more"})
        check("session: save after restore keeps slot", rapp.save_session() == sid)
        rapp.clear()
        check("session: clear detaches",
              rapp.session_slot is None and rapp.history == [] and rapp.session is None)

        capp = ctxmod.App(ctxmod.Config(shome), tmp)
        capp.restore_session({"id": "cc", "history": [{"role": "user", "text": "a"}],
                              "conversation_id": "conv-2", "turn_count": "x",
                              "session_id": 12})
        check("session: restore coerces bad fields",
              capp.session.turn_count == 0
              and isinstance(capp.session.session_id, str) and capp.session.session_id)

        mapp = ctxmod.App(ctxmod.Config(shome), tmp)
        mapp.restore_session({"id": "mm", "history": [{"role": "user", "text": "a"}],
                              "model": "not-a-tone"})
        check("session: unknown m365 model falls back",
              mapp.model == ctxmod.Config(shome).data["model"] and mapp.session is None)

        pcfg = ctxmod.Config(tmp / "phome")
        pcfg.data["provider"] = "chat"
        papp = ctxmod.App(pcfg, tmp)
        papp.restore_session({"id": "pp", "history": [{"role": "user", "text": "a"}],
                              "model": "any-model-id", "conversation_id": "conv-9",
                              "turn_count": 5})
        check("session: http restore takes model, ignores conversation",
              papp.model == "any-model-id" and papp.session is None)

        lhome = tmp / "lhome"
        ldir = lhome / "sessions"
        ldir.mkdir(parents=True)
        (ldir / "aaa.json").write_text(json.dumps(
            {"id": "aaa", "updated": "2026-01-01T00:00:00Z", "model": "m1",
             "history": [{"role": "user", "text": "x"}]}))
        (ldir / "bbb.json").write_text(json.dumps(
            {"id": "bbb", "updated": "2026-02-01T00:00:00Z", "model": "m2",
             "history": [{"role": "user", "text": "y"}]}))
        (ldir / "ccc.json").write_text("{broken")
        (ldir / "ddd.json").write_text(json.dumps(
            {"updated": "2026-03-01T00:00:00Z", "history": []}))
        (ldir / "eee.json").write_text(json.dumps(
            {"updated": "2026-04-01T00:00:00Z",
             "history": [{"role": "user", "text": "z"}]}))
        lcfg = ctxmod.Config(lhome)
        found = ctxmod.find_sessions(lcfg)
        check("sessions: newest first, bad files skipped",
              [s["id"] for s in found] == ["eee", "bbb", "aaa"],
              str([s["id"] for s in found]))
        check("sessions: stem fallback id", found[0]["id"] == "eee")
        check("sessions: resume latest", ctxmod.resume_session(lcfg, "")["id"] == "eee")
        check("sessions: resume by id", ctxmod.resume_session(lcfg, "bbb")["id"] == "bbb")
        check("sessions: resume by id with .json suffix",
              ctxmod.resume_session(lcfg, "bbb.json")["id"] == "bbb")
        try:
            ctxmod.resume_session(lcfg, "zzz")
            check("sessions: unknown id raises", False)
        except ctxmod.CtxError as e:
            check("sessions: unknown id raises", "no saved session 'zzz'" in str(e), str(e))
        try:
            ctxmod.resume_session(ctxmod.Config(tmp / "lhome-empty"), "")
            check("sessions: none saved raises", False)
        except ctxmod.CtxError as e:
            check("sessions: none saved raises", "no saved sessions" in str(e), str(e))
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
                        "path": self.path,
                        "hist": any("The notes say" in content_text(m["content"])
                                    for m in items)})
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

        def run_ctx(args, stdin=None, home=None, extra_env=None):
            env = dict(env_base, CTX_HOME=str(home or (tmp / "home")))
            if extra_env:
                env.update(extra_env)
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

        proj = fresh_project("p_edit")
        r = run_ctx(["--dir", str(proj), "ask", "Edit notes.txt. TOUCHSTONE-EDIT"],
                    stdin="y\n")
        check("edit confirmed: file updated",
              (proj / "notes.txt").read_text() == "alpha delta charlie\n",
              (proj / "notes.txt").read_text())
        check("edit confirmed: tool shown", "-> edit notes.txt" in r.stdout,
              r.stdout[-400:])
        check("edit confirmed: diff preview shown",
              "-alpha bravo charlie" in r.stdout and "+alpha delta charlie" in r.stdout,
              r.stdout[-400:])
        check("edit confirmed: model ack", "EDITED_OK_1" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_edit_deny")
        r = run_ctx(["--dir", str(proj), "ask", "Edit notes.txt. TOUCHSTONE-EDIT"],
                    stdin="\n")
        check("edit denied: file untouched",
              (proj / "notes.txt").read_text() == "alpha bravo charlie\n")
        check("edit denied: model ack", "EDIT_DENIED_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_edit_nomatch")
        r = run_ctx(["--dir", str(proj), "ask", "Edit notes.txt. TOUCHSTONE-EDIT-NOMATCH"],
                    stdin="")
        check("edit no-match: file untouched",
              (proj / "notes.txt").read_text() == "alpha bravo charlie\n")
        check("edit no-match: model ack", "EDIT_NOMATCH_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_edit_ambig")
        (proj / "notes.txt").write_text("alpha bravo alpha charlie\n")
        r = run_ctx(["--dir", str(proj), "ask", "Edit notes.txt. TOUCHSTONE-EDIT-AMBIG"],
                    stdin="")
        check("edit ambiguous: file untouched",
              (proj / "notes.txt").read_text() == "alpha bravo alpha charlie\n")
        check("edit ambiguous: model ack", "EDIT_AMBIG_OK" in r.stdout, r.stdout[-400:])

        proj = fresh_project("p_edit_all")
        (proj / "notes.txt").write_text("alpha bravo alpha charlie\n")
        r = run_ctx(["--dir", str(proj), "ask", "Edit every match. TOUCHSTONE-EDIT-ALL"],
                    stdin="y\n")
        check("edit replace_all: both replaced",
              (proj / "notes.txt").read_text() == "delta bravo delta charlie\n",
              (proj / "notes.txt").read_text())
        check("edit replace_all: model ack", "EDITED_OK_2" in r.stdout, r.stdout[-400:])

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

        proj = fresh_project("p_graph")
        (proj / "AGENTS.md").write_text("Always cite file paths.\n")
        gdir = proj / "graphify-out"
        gdir.mkdir()
        (gdir / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "a"}, {"id": "b"}],
                        "edges": [{"source": "a", "target": "b"}]}))
        fake_log = tmp / "gfake.jsonl"
        r = run_ctx(["--dir", str(proj), "ask", "Use the graph. TOUCHSTONE-GRAPH"],
                    extra_env={
                        "CTX_GRAPHIFY_CMD":
                            f"{sys.executable} {ROOT / 'tests' / 'fake_graphify.py'}",
                        "FAKE_GRAPHIFY_LOG": str(fake_log),
                    })
        check("graph: tool executed", "-> graph_query" in r.stdout, r.stdout[-400:])
        check("graph: final answer",
              "The graph says: entrypoint is main.py" in r.stdout, r.stdout[-400:])
        check("graph: prompt advertised graph",
              any(e.get("graph_adv") for e in server.entries))
        check("graph: AGENTS.md reached the prompt",
              any(e.get("agents_md") for e in server.entries))
        flines = [json.loads(l) for l in fake_log.read_text().splitlines()]
        check("graph: cli invoked with question",
              flines and flines[0][:2] == ["query", "what is the entry point"],
              str(flines[:1]))

        r = run_ctx(["--dir", str(proj)], stdin="/issues\n/q\n")
        check("issues: no tracker message", "no readable issues.json" in r.stdout,
              r.stdout[-400:])

        proj = fresh_project("p_issues")
        (proj / "issues.json").write_text(json.dumps([
            {"id": "alpha", "title": "first thing", "status": "open",
             "priority": "high", "summary": "do the first thing"},
            {"id": "beta", "title": "second thing", "status": "done",
             "priority": "low", "summary": "already handled",
             "acceptance": ["it works"]},
        ]))
        r = run_ctx(["--dir", str(proj)], stdin="/issues\n/issues alpha\n/q\n")
        check("issues: list shown", "2 issues (1 open)" in r.stdout
              and "alpha" in r.stdout and "beta" in r.stdout, r.stdout[-600:])
        check("issues: detail for id",
              "alpha - first thing" in r.stdout and "do the first thing" in r.stdout,
              r.stdout[-600:])
        check("issues: other detail not shown", "it works" not in r.stdout)
        r = run_ctx(["--dir", str(proj)], stdin="/issues nope\n/q\n")
        check("issues: unknown id hint", "no issue 'nope'" in r.stdout
              and "known ids: alpha, beta" in r.stdout, r.stdout[-400:])

        proj_s = fresh_project("p_sess")
        shome = tmp / "home-sess"
        r = run_ctx(["--dir", str(proj_s), "ask", "Read notes.txt please. TOUCHSTONE-READ"],
                    home=shome)
        sdir = shome / "sessions"
        saved = sorted(sdir.glob("*.json"))
        check("sessions: ask auto-saves", len(saved) == 1, str(saved))
        sdata_a = json.loads(saved[0].read_text()) if saved else {}
        sid_a = sdata_a.get("id")
        conv_a = sdata_a.get("conversation_id")
        check("sessions: history and m365 ids persisted",
              bool(sid_a) and bool(conv_a) and sdata_a.get("model") == "gpt-5.6"
              and sdata_a.get("provider") == "m365"
              and sdata_a.get("root") == str(proj_s)
              and sdata_a.get("turn_count", 0) >= 1
              and any(h.get("text") == "Read notes.txt please. TOUCHSTONE-READ"
                      for h in sdata_a.get("history", [])),
              str(sdata_a)[:200])

        proj_s2 = fresh_project("p_sess2")
        r = run_ctx(["--dir", str(proj_s2), "ask", "Read notes.txt please. TOUCHSTONE-READ"],
                    home=shome)
        by_root = {}
        for p in sdir.glob("*.json"):
            d = json.loads(p.read_text())
            by_root[d.get("root")] = d
        sdata_b = by_root.get(str(proj_s2), {})
        sid_b = sdata_b.get("id")
        conv_b = sdata_b.get("conversation_id")
        check("sessions: second ask gets its own file",
              bool(sid_b) and sid_b != sid_a and bool(conv_b) and conv_b != conv_a)

        r = run_ctx(["--dir", str(proj_s2), "--resume"],
                    stdin="TOUCHSTONE-SESS2\n/q\n", home=shome)
        check("resume latest: picks newest session",
              f"resumed session {sid_b}" in r.stdout, r.stdout[-400:])
        check("resume latest: reply", "RESUMED_OK" in r.stdout, r.stdout[-400:])
        last = server.entries[-1] if server.entries else {}
        check("resume latest: reuses stored conversation id",
              last.get("conversation_id") == conv_b, str(last))
        check("resume latest: continues server session",
              last.get("is_start") is False, str(last))

        r = run_ctx(["--dir", str(proj_s), "--resume", sid_a],
                    stdin="TOUCHSTONE-SESS2\n/q\n", home=shome)
        check("resume by id: picks named session",
              f"resumed session {sid_a}" in r.stdout, r.stdout[-400:])
        last = server.entries[-1] if server.entries else {}
        check("resume by id: reuses stored conversation id",
              last.get("conversation_id") == conv_a, str(last))

        r = run_ctx(["--dir", str(proj_s), "--resume", sid_a],
                    stdin="/sessions\n/q\n", home=shome)
        check("sessions: list shows ids and model",
              sid_a in r.stdout and sid_b in r.stdout and "gpt-5.6" in r.stdout,
              r.stdout[-600:])
        check("sessions: list marks current", "<- current" in r.stdout, r.stdout[-600:])

        r = run_ctx(["--dir", str(proj_s), "--resume", sid_a],
                    stdin="TOUCHSTONE-SESS3\n/clear\nTOUCHSTONE-SESS4\n/q\n", home=shome)
        check("clear: reset message", "conversation reset" in r.stdout, r.stdout[-600:])
        snap = {}
        for p in sdir.glob("*.json"):
            d = json.loads(p.read_text())
            snap[d["id"]] = d
        new_ids = set(snap) - {sid_a, sid_b}
        check("clear: next turn gets a fresh slot", len(new_ids) == 1, str(set(snap)))
        nid = new_ids.pop() if new_ids else None
        check("clear: fresh session holds only the new turn",
              nid and len(snap[nid]["history"]) == 2, str(snap.get(nid))[:200])
        check("clear: detached from old slot",
              nid and not any("TOUCHSTONE-SESS4" in h.get("text", "")
                              for h in snap[sid_a]["history"]))

        r = run_ctx(["--dir", str(proj_s), "--resume", "nope"], stdin="/q\n", home=shome)
        check("resume: unknown id errors",
              r.returncode == 1 and "no saved session 'nope'" in r.stderr, r.stderr[-300:])
        r = run_ctx(["--dir", str(proj_s), "--resume"], stdin="/q\n",
                    home=tmp / "home-empty-sess")
        check("resume: no sessions errors",
              r.returncode == 1 and "no saved sessions" in r.stderr, r.stderr[-300:])

        r = run_ctx(["--dir", str(proj_s), "--resume", "ask", "TOUCHSTONE-SESS2"],
                    home=shome)
        check("resume: bare flag before subcommand",
              r.returncode == 0 and "RESUMED_OK" in r.stdout, r.stdout[-400:])
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

                    shome = tmp / "home-chat-sess"
                    proj_s = tmp / "proj-chat-sess"
                    proj_s.mkdir()
                    (proj_s / "notes.txt").write_text("alpha bravo charlie\n")

                    def run_sess(args, stdin=None):
                        return subprocess.run(
                            [sys.executable, ctx_py] + args, input=stdin,
                            capture_output=True, text=True,
                            env=dict(env, CTX_HOME=str(shome)), timeout=90)

                    r = run_sess(["--dir", str(proj_s), "ask",
                                  "Read notes.txt please. TOUCHSTONE-READ"])
                    check("http chat: ask saves session",
                          len(list((shome / "sessions").glob("*.json"))) == 1,
                          r.stdout[-300:])
                    before = len(log)
                    r = run_sess(["--dir", str(proj_s), "--resume"],
                                 stdin="TOUCHSTONE-SESS2\n/q\n")
                    check("http chat: resume reply",
                          "RESUMED_OK" in r.stdout, r.stdout[-400:])
                    check("http chat: resume sends full history",
                          log[before:] and all(e.get("hist") for e in log[before:]),
                          str(log[before:]))
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
