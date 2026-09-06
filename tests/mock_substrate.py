"""Mock M365 Copilot substrate server for offline tests.

Speaks just enough of the SignalR-over-WebSocket protocol used by
substrate.office.com: HTTP upgrade, JSON handshake, RS-framed messages,
a type-4 chat invocation, and streamed type-1/type-2/type-3 responses.

Scripted replies are selected by markers in the incoming message text so
the harness can be driven end-to-end without any network access.
"""

import base64
import hashlib
import json
import re
import socket
import threading
from pathlib import Path

RS = "\x1e"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TOOL_OPEN = "<tool" + "_call>"
TOOL_CLOSE = "</tool" + "_call>"

CONFAB_REPLY = ("I'm sorry, I don't have access to the files in this session. "
                "Please paste the contents of the files you want me to look at.")


def tool_call_reply(name, args):
    return TOOL_OPEN + "\n" + json.dumps({"name": name, "arguments": args}) + "\n" + TOOL_CLOSE


class Conn:
    def __init__(self, sock, addr, server):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.buf = b""

    def pump(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_until(self, marker):
        while marker not in self.buf:
            if not self.pump():
                return None
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def take(self, n):
        while len(self.buf) < n:
            if not self.pump():
                return None
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += n.to_bytes(2, "big")
        else:
            header.append(127)
            header += n.to_bytes(8, "big")
        self.sock.sendall(bytes(header) + payload)

    def send_close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x00]))
        except OSError:
            pass

    def recv_ws(self):
        while True:
            b1 = self.take(1)
            if b1 is None:
                return None
            op = b1[0] & 0x0F
            fin = b1[0] & 0x80
            b2 = self.take(1)
            if b2 is None:
                return None
            masked = b2[0] & 0x80
            ln = b2[0] & 0x7F
            if ln == 126:
                h = self.take(2)
                if h is None:
                    return None
                ln = int.from_bytes(h, "big")
            elif ln == 127:
                h = self.take(8)
                if h is None:
                    return None
                ln = int.from_bytes(h, "big")
            mask = self.take(4) if masked else None
            data = self.take(ln) if ln else b""
            if data is None:
                return None
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                return None
            if op == 0x9:
                try:
                    self.sock.sendall(bytes([0x8A, len(data)]) + data)
                except OSError:
                    pass
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x0) and fin:
                return data.decode("utf-8", "replace")

    def run(self):
        head = self.read_until(b"\r\n\r\n")
        if not head:
            return
        req = head.decode("latin-1")
        key = ""
        path = ""
        for line in req.split("\r\n"):
            low = line.lower()
            if low.startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
            if line.startswith("GET "):
                path = line.split(" ")[1]
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n")
        self.sock.sendall(resp.encode())
        while True:
            msg = self.recv_ws()
            if msg is None:
                break
            for part in msg.split(RS):
                part = part.strip()
                if not part:
                    continue
                try:
                    frame = json.loads(part)
                except json.JSONDecodeError:
                    continue
                if "protocol" in frame:
                    self.send_text("{}" + RS)
                    continue
                ftype = frame.get("type")
                if ftype == 6:
                    continue
                if ftype == 4 and frame.get("target") == "chat":
                    args = frame["arguments"][0]
                    self.handle_chat(args, path)
                    return

    def handle_chat(self, args, path):
        tone = args.get("tone")
        text = args.get("message", {}).get("text", "")
        is_start = args.get("isStartOfSession")
        self.server.log({"path": path, "tone": tone, "is_start": is_start,
                         "head": text[:100],
                         "agents_md": "USER INSTRUCTIONS" in text,
                         "graph_adv": "KNOWLEDGE GRAPH" in text})
        reply = self.server.script_reply(text, tone)
        self.send_text(json.dumps({"type": 6}) + RS)
        self.send_text(json.dumps({
            "type": 1, "target": "update",
            "arguments": [{"writeAtCursor": reply[:7]}],
        }) + RS)
        self.send_text(json.dumps({
            "type": 1, "target": "update",
            "arguments": [{"messages": [{"author": "bot", "text": reply}]}],
        }) + RS)
        self.send_text(json.dumps({
            "type": 2,
            "item": {
                "messages": [{"author": "bot", "text": reply}],
                "throttling": {
                    "numUserMessagesInConversation": 2,
                    "maxNumUserMessagesInConversation": 30,
                },
            },
        }) + RS)
        self.send_text(json.dumps({"type": 3, "invocationId": "0"}) + RS)
        self.send_close()


def script_reply(text, tone):
    if "You DO have" in text:
        return "RECOVERED"
    if "[Result of read]" in text:
        m = re.search(r"1\| (.*)", text)
        return "The notes say: " + (m.group(1).strip() if m else "?")
    if "[Result of write]" in text:
        return "DENIED_OK" if "DENIED" in text else "WROTE_OK"
    if "[Result of edit]" in text:
        if "DENIED" in text:
            return "EDIT_DENIED_OK"
        if "not found" in text:
            return "EDIT_NOMATCH_OK"
        if " times in " in text:
            return "EDIT_AMBIG_OK"
        m = re.search(r"\((\d+) replacements?\)", text)
        return f"EDITED_OK_{m.group(1)}" if m else "EDIT_UNPARSED"
    if "[Result of run]" in text:
        return "RAN_OK"
    if "[Result of graph_query]" in text:
        m = re.search(r"GRAPH-ANSWER\[[^\]]*\]: (.*)", text)
        return "The graph says: " + (m.group(1).strip() if m else "?")
    if "TOUCHSTONE-READ" in text:
        return tool_call_reply("read", {"path": "notes.txt"})
    if "TOUCHSTONE-WRITE" in text:
        return tool_call_reply("write", {"path": "hello.txt", "content": "world"})
    if "TOUCHSTONE-EDIT-NOMATCH" in text:
        return tool_call_reply("edit", {"path": "notes.txt", "old": "zebra",
                                        "new": "delta"})
    if "TOUCHSTONE-EDIT-AMBIG" in text:
        return tool_call_reply("edit", {"path": "notes.txt", "old": "alpha",
                                        "new": "delta"})
    if "TOUCHSTONE-EDIT-ALL" in text:
        return tool_call_reply("edit", {"path": "notes.txt", "old": "alpha",
                                        "new": "delta", "replace_all": True})
    if "TOUCHSTONE-EDIT" in text:
        return tool_call_reply("edit", {"path": "notes.txt", "old": "bravo",
                                        "new": "delta"})
    if "TOUCHSTONE-RUN" in text:
        return tool_call_reply("run", {"command": "echo mockrun"})
    if "TOUCHSTONE-GRAPH" in text:
        return tool_call_reply("graph_query", {"question": "what is the entry point"})
    if "TOUCHSTONE-CONFAB" in text:
        return CONFAB_REPLY
    if "TOUCHSTONE-TONE" in text:
        return f"CURRENT_TONE={tone}"
    return f"TONE={tone}"


class MockSubstrate:
    def __init__(self, log_path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.entries = []
        self._stop = threading.Event()
        self.sock = None
        self.port = None

    def start(self, port=0):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                c, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=Conn(c, addr, self).run, daemon=True).start()

    def log(self, entry):
        self.entries.append(entry)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def script_reply(self, text, tone):
        return script_reply(text, tone)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
