#!/usr/bin/env python3
"""ctx - local context helper.

A minimal coding-agent CLI that talks to a Microsoft 365 Copilot chat
backend over its SignalR WebSocket protocol, authenticating with the
organizational account via OAuth2 device code flow (completed in the
Windows host browser when running under WSL).

Stdlib only. No third-party dependencies, no telemetry, no writes
outside the project directory and the config directory.
"""

import argparse
import base64
import difflib
import fnmatch
import getpass
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

RS = "\x1e"
PROG = "ctx"
DEFAULT_HOME = Path.home() / ".config" / "ctx"

AUTHORITY = "https://login.microsoftonline.com/common"
CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
SCOPES = [
    "https://substrate.office.com/sydney/M365Chat.Read",
    "https://substrate.office.com/sydney/sydney.readwrite",
    "openid",
    "profile",
    "offline_access",
]

WS_BASE_DEFAULT = "wss://substrate.office.com/m365Copilot/Chathub"
WS_HEADERS = {
    "Origin": "https://m365.cloud.microsoft",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

VARIANTS = ",".join([
    "EnableMcpServerWidgets",
    "feature.EnableMcpServerWidgets",
    "feature.EnableLuForChatCIQ",
    "feature.enableChatCIQPlugin",
    "EnableRequestPlugins",
    "feature.EnableSensitivityLabels",
    "EnableUnsupportedUrlDetector",
    "feature.IsCustomEngineCopilotEnabled",
    "feature.bizchatfluxv3",
    "feature.enablechatpages",
    "feature.enableCodeCanvas",
    "feature.turnOnWorkTabRecommendation",
    "turnOffWorkTabUpsellFromClient",
    "feature.turnOnDARecommendation",
    "feature.IsStreamingModeInChatRequestEnabled",
    "IncludeSourceAttributionsConcise",
    "SkipPublishEmptyMessage",
    "feature.EnableDeduplicatingSourceAttributions",
    "Enable3PActionProgressMessages",
    "feature.enableClientWebRtc",
    "feature.EnableMeetingRecapOfSeriesMeetingWithCiq",
    "feature.EnableReferencesListCompleteSignal",
    "feature.StorageMessageSplitDisabled",
    "feature.EnableCuaTakeControlApi",
    "feature.cwcallowedos",
    "feature.disabledisallowedmsgs",
    "feature.enableCitationsForSynthesisData",
    "feature.enableGenerateGraphicArtOptionsSet",
    "cdximagen",
    "feature.EnableUpdatedUXForConfirmationDialog",
    "feature.EnableClientFileURLSupportForOfficeWebPaidCopilot",
    "feature.EnableDesignEditorImageGrounding",
    "feature.EnableDesignerEditor",
    "feature.OfficeWebToHelix",
    "feature.OfficeDesktopToHelix",
    "feature.M365TeamsHubToHelix",
    "feature.OwaHubToHelix",
    "feature.MonarchHubToHelix",
    "feature.Win32OutlookHubToHelix",
    "feature.MacOutlookHubToHelix",
    "Agt_bizchat_enableGpt5ForHelix",
])

ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GenerateContentQuery",
    "SearchQuery",
    "EndOfRequest",
    "ReferencesListComplete",
]

DEFAULT_MODELS = {
    "auto": "magic",
    "quick": "Gpt_Quick",
    "think-deeper": "Gpt_Reasoning",
    "gpt-5.6": "Gpt_5_6_Reasoning",
    "gpt-5.5": "Gpt_5_5_Chat",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
    "gpt-5.4-think-deeper": "Gpt_5_4_Reasoning",
    "gpt-5.3-quick": "Gpt_5_3_Quick",
    "gpt-5.3-think-deeper": "Gpt_5_3_Reasoning",
    "claude-sonnet": "Claude_Sonnet",
    "claude-sonnet-think-deeper": "Claude_Sonnet_Reasoning",
    "claude-opus": "Claude_Opus",
}

PROVIDERS = ("m365", "chat", "messages", "responses")
PROVIDER_PATHS = {"chat": "/chat/completions", "messages": "/messages",
                  "responses": "/responses"}
SUBCOMMANDS = ("login", "logout", "status", "ask")

DEFAULT_CONFIG = {
    "model": "gpt-5.6",
    "models": dict(DEFAULT_MODELS),
    "context_kb": 48,
    "max_iter": 8,
    "turn_timeout_s": 240,
    "ws_base": WS_BASE_DEFAULT,
    "provider": "m365",
    "base_url": "",
    "api_key": "",
    "max_tokens": 8192,
    "graphify_cmd": "",
}

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", "vendor", ".tox", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", ".ruff_cache", "coverage",
    ".next", ".cache", "graphify-out",
}
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".exe", ".dll",
    ".so", ".dylib", ".bin", ".o", ".a", ".pyc", ".class", ".jar", ".woff",
    ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".avi", ".mov", ".sqlite",
    ".db", ".wasm", ".lock",
}
MAX_FILE_BYTES = 96_000
MAX_LIST_ENTRIES = 500
MAX_READ_CHARS = 40_000
MAX_TOOL_RESULT_CHARS = 12_000
MAX_RUN_OUTPUT_CHARS = 16_000
MAX_DIFF_LINES = 200
MAX_INSTRUCTION_CHARS = 20_000
MAX_GRAPH_OUTPUT_CHARS = 8_000
GRAPH_TIMEOUT = 90
GRAPH_JSON = "graphify-out/graph.json"

TOOL_CALL_START = "<tool" + "_call>"
TOOL_CALL_END = "</tool" + "_call>"

CONFAB_PATTERNS = [
    r"can.?t\s+(access|see|read|run|edit|inspect|open|list)",
    r"don.?t\s+have\s+(access|a\s+(shell|terminal|file\s*system))",
    r"paste\s+(the\s+)?(contents?|files?|code)",
    r"no\s+(access|tools?)\s+(to|available)",
    r"/mnt/data",
    r"not\s+available\s+(to\s+me|in\s+this\s+(session|environment))",
    r"doesn.?t\s+(have|expose)\s+(access|a\s+file\s*system|tools)",
]

USAGE = """examples:
  ctx login              first-time auth (device code shown for the host browser)
  ctx login --paste      auth by pasting a token grabbed from browser devtools
  ctx status             token expiry, model, config location
  ctx                    interactive session in the current folder
  ctx ask "summarize ."  one question, one answer, then exit

session commands:
  /help /model [name] /add GLOB /drop GLOB /ctx /graph /issues [id]
  /clear /save [file] /sessions /q

flags (before the subcommand):
  --model gpt-5.6   pick model for this run (see /model for the list)
  --yes             skip confirmation prompts for write/run tools
  --new             fresh server conversation for every question
  --resume [id]     continue the most recent (or the given) saved session
  --timeout N       per-request timeout in seconds

environment:
  CTX_HOME   config+token dir        (default ~/.config/ctx)
  CTX_TOKEN  access token override   (bypasses the token cache)
  CTX_MODEL  default model override

extras:
  AGENTS.md in the project root or the config dir is prepended to every prompt
  (project file is git-ignored by convention). If graphify-out/graph.json exists,
  the agent gains graph_query / graph_path / graph_explain tools backed by the
  graphify CLI. CTX_GRAPHIFY_CMD overrides how that CLI is invoked.

other backends (any openai- or anthropic-compatible endpoint):
  CTX_PROVIDER=chat|messages|responses CTX_BASE_URL=https://host/v1 \\
  CTX_API_KEY=... ctx --model <model-id> ask "..."
  provider chat     -> POST {base}/chat/completions   (openai-compatible)
  provider messages -> POST {base}/messages           (anthropic-compatible)
  provider responses-> POST {base}/responses          (openai responses api)
"""


class CtxError(Exception):
    pass


class WsClosed(Exception):
    pass


class TurnTimeout(Exception):
    pass


def b64url_decode(data):
    return base64.urlsafe_b64decode(data + "=" * ((4 - len(data) % 4) % 4))


def jwt_decode(token):
    parts = token.split(".")
    if len(parts) < 2:
        raise CtxError("token is not a JWT")
    return json.loads(b64url_decode(parts[1]).decode("utf-8"))


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def http_form(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        code = payload.get("error", "")
        desc = payload.get("error_description") or str(e)
        raise CtxError(f"{code}: {desc}".strip(" :")) from None
    except urllib.error.URLError as e:
        raise CtxError(f"network error talking to {url.split('/')[2]}: {e.reason}") from None


class Config:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / "config.json"
        self.token_path = self.home / "token.json"
        self.transcript_dir = self.home / "transcripts"
        self.session_dir = self.home / "sessions"
        self.data = dict(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                self.data.update({k: v for k, v in loaded.items() if v is not None})
            except Exception:
                pass
        if not isinstance(self.data.get("models"), dict) or not self.data["models"]:
            self.data["models"] = dict(DEFAULT_MODELS)
        if os.environ.get("CTX_WS_BASE"):
            self.data["ws_base"] = os.environ["CTX_WS_BASE"]
        if os.environ.get("CTX_MODEL"):
            self.data["model"] = os.environ["CTX_MODEL"]
        for var, key in (("CTX_PROVIDER", "provider"), ("CTX_BASE_URL", "base_url"),
                         ("CTX_API_KEY", "api_key"), ("CTX_GRAPHIFY_CMD", "graphify_cmd")):
            if os.environ.get(var):
                self.data[key] = os.environ[var]
        if os.environ.get("CTX_MAX_TOKENS"):
            try:
                self.data["max_tokens"] = int(os.environ["CTX_MAX_TOKENS"])
            except ValueError:
                pass

    def save(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", "utf-8")

    def model_tone(self, name):
        models = self.data["models"]
        if name in models:
            return models[name]
        if name in models.values():
            return name
        raise CtxError(f"unknown model '{name}' - available: " + ", ".join(sorted(models)))

    @property
    def model_names(self):
        return sorted(self.data["models"])


class TokenStore:
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        if os.environ.get("CTX_TOKEN"):
            token = os.environ["CTX_TOKEN"].strip()
            claims = jwt_decode(token)
            return {"access_token": token, "refresh_token": None,
                    "expires_at": claims.get("exp", 0), "claims": claims,
                    "from_env": True}
        if not self.cfg.token_path.exists():
            return None
        try:
            data = json.loads(self.cfg.token_path.read_text("utf-8"))
        except Exception:
            return None
        token = data.get("access_token")
        if not token:
            return None
        try:
            data["claims"] = jwt_decode(token)
        except Exception:
            data["claims"] = {}
        return data

    def _write(self, data):
        self.cfg.home.mkdir(parents=True, exist_ok=True)
        self.cfg.token_path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        os.chmod(self.cfg.token_path, stat.S_IRUSR | stat.S_IWUSR)

    def save(self, access_token, refresh_token=None, expires_at=None, claims=None):
        if claims is None:
            try:
                claims = jwt_decode(access_token)
            except Exception:
                claims = {}
        if expires_at is None:
            expires_at = claims.get("exp") or int(time.time() + 3600)
        data = {
            "access_token": access_token,
            "expires_at": int(expires_at),
            "account": claims.get("preferred_username") or claims.get("upn") or "",
        }
        if refresh_token:
            data["refresh_token"] = refresh_token
        self._write(data)

    def clear(self):
        if self.cfg.token_path.exists():
            self.cfg.token_path.unlink()

    def refresh(self, data):
        rt = data.get("refresh_token")
        if not rt:
            return None
        try:
            result = http_form(AUTHORITY + "/oauth2/v2.0/token", {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": rt,
                "scope": " ".join(SCOPES),
            })
        except CtxError:
            return None
        access = result.get("access_token")
        if not access:
            return None
        stored = dict(data)
        stored["access_token"] = access
        stored["expires_at"] = int(time.time()) + int(result.get("expires_in", 3600)) - 120
        if result.get("refresh_token"):
            stored["refresh_token"] = result["refresh_token"]
        try:
            stored["claims"] = jwt_decode(access)
            stored["account"] = stored["claims"].get("preferred_username", "")
        except Exception:
            pass
        self._write({k: v for k, v in stored.items() if k != "claims" and k != "from_env"})
        return stored

    def acquire(self):
        data = self.load()
        if data is None:
            raise CtxError("not logged in - run: ctx login")
        if data.get("expires_at", 0) - 90 > time.time():
            return data
        if data.get("from_env"):
            return data
        refreshed = self.refresh(data)
        if refreshed and refreshed.get("expires_at", 0) - 90 > time.time():
            return refreshed
        raise CtxError("token expired and could not be refreshed - run: ctx login")


def open_host_browser(url):
    try:
        if shutil.which("wslview"):
            subprocess.run(["wslview", url], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        pass
    for exe in ("explorer.exe", "/mnt/c/Windows/explorer.exe"):
        try:
            subprocess.run([exe, url], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def device_login(store):
    flow = http_form(AUTHORITY + "/oauth2/v2.0/devicecode", {
        "client_id": CLIENT_ID,
        "scope": " ".join(SCOPES),
    })
    if "user_code" not in flow:
        raise CtxError("device code flow rejected: " + str(flow.get("error_description", flow)))
    uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
    code = flow["user_code"]
    print()
    print("  1. open in your browser : " + uri)
    print("  2. enter the code       : " + code)
    print(f"  (code expires in {int(flow.get('expires_in', 900)) // 60} minutes)")
    if not open_host_browser(uri):
        print("  (could not auto-open a browser - open the URL manually)")
    print()
    interval = int(flow.get("interval", 5))
    deadline = time.time() + int(flow.get("expires_in", 900))
    device_code = flow["device_code"]
    while time.time() < deadline:
        time.sleep(interval)
        try:
            result = http_form(AUTHORITY + "/oauth2/v2.0/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            })
        except CtxError as e:
            msg = str(e)
            if "authorization_pending" in msg:
                continue
            if "slow_down" in msg:
                interval += 5
                continue
            if "expired_token" in msg:
                raise CtxError("device code expired - run login again") from None
            if "authorization_declined" in msg:
                raise CtxError("sign-in was declined") from None
            raise
        access = result.get("access_token")
        if not access:
            raise CtxError("no access token in response")
        expires_at = int(time.time()) + int(result.get("expires_in", 3600)) - 120
        try:
            claims = jwt_decode(access)
        except Exception:
            claims = {}
        store.save(access, result.get("refresh_token"), expires_at, claims)
        print("  signed in as: " + (claims.get("preferred_username") or "unknown"))
        return
    raise CtxError("device code expired - run login again")


def paste_login(store):
    print("Paste the access token (audience https://substrate.office.com/sydney).")
    print("In the browser: m365.cloud.microsoft/chat -> devtools -> Network -> WS ->")
    print("substrate.office.com frame URL -> copy the access_token query parameter.")
    token = getpass.getpass("token: ").strip()
    if not token:
        raise CtxError("empty token")
    claims = jwt_decode(token)
    if "substrate.office.com" not in str(claims.get("aud", "")):
        print(f"  warning: token audience is {claims.get('aud')} - expected substrate.office.com")
    if not claims.get("oid") or not claims.get("tid"):
        raise CtxError("token has no oid/tid claims - it will not work for chat")
    store.save(token, None, claims.get("exp"), claims)
    print("  token saved (no refresh token - re-paste when it expires)")
    print("  signed in as: " + (claims.get("preferred_username") or str(claims.get("oid"))))


class WebSocketClient:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, headers=None, sock_timeout=30):
        parsed = urllib.parse.urlparse(url)
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path
        if parsed.query:
            self.path += "?" + parsed.query
        self.headers = headers or {}
        self.sock_timeout = sock_timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=15)
        if self.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        elif self.scheme == "ws":
            self.sock = raw
        else:
            raise CtxError(f"unsupported scheme {self.scheme}")
        self.sock.settimeout(self.sock_timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n", time.monotonic() + 20)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            raise CtxError(f"websocket upgrade failed: {status.strip()}")

    def _pump(self, deadline):
        if deadline is not None and time.monotonic() > deadline:
            raise TurnTimeout("turn deadline exceeded")
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return True
        if not chunk:
            return False
        self.buf += chunk
        return True

    def _read_until(self, marker, deadline):
        while marker not in self.buf:
            if not self._pump(deadline) and marker not in self.buf:
                raise WsClosed("connection closed during handshake")
        idx = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:idx], self.buf[idx:]
        return out

    def _take(self, n, deadline):
        while len(self.buf) < n:
            if not self._pump(deadline):
                raise WsClosed("connection closed mid-frame")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, first_byte, payload):
        mask = os.urandom(4)
        header = bytearray([first_byte])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x81, text.encode("utf-8"))

    def _send_control(self, opcode, payload):
        try:
            self._send_frame(0x80 | opcode, payload)
        except OSError:
            pass

    def recv_text(self, deadline):
        fragments = []
        last_ping = time.monotonic()
        while True:
            if time.monotonic() - last_ping > 15:
                self._send_control(0x9, b"")
                last_ping = time.monotonic()
            b1 = self._take(1, deadline)[0]
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            b2 = self._take(1, deadline)[0]
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = int.from_bytes(self._take(2, deadline), "big")
            elif length == 127:
                length = int.from_bytes(self._take(8, deadline), "big")
            if length > 16 * 1024 * 1024:
                raise CtxError("oversized websocket frame")
            mask = self._take(4, deadline) if masked else None
            data = self._take(length, deadline) if length else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WsClosed("server sent close frame")
            if opcode == 0x1:
                fragments = [data]
            elif opcode == 0x0:
                fragments.append(data)
            else:
                raise CtxError(f"unexpected websocket opcode {opcode}")
            if fin:
                return b"".join(fragments).decode("utf-8", "replace")

    def close(self):
        if self.sock:
            self._send_control(0x8, b"")
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


def fold_text(answer, nxt):
    if len(nxt) <= len(answer):
        return answer, None
    if nxt.startswith(answer):
        return nxt, nxt[len(answer):]
    return nxt, None


def build_chat_frame(text, tone, session_id, request_id, is_start):
    gmtoff = time.localtime().tm_gmtoff or 0
    args = {
        "source": "officeweb",
        "clientCorrelationId": request_id,
        "sessionId": session_id,
        "optionsSets": [],
        "streamingMode": "ConciseWithPadding",
        "spokenTextMode": "None",
        "options": {},
        "extraExtensionParameters": {},
        "allowedMessageTypes": list(ALLOWED_MESSAGE_TYPES),
        "sliceIds": [],
        "threadLevelGptId": {},
        "traceId": request_id,
        "isStartOfSession": is_start,
        "clientInfo": {
            "clientPlatform": "mcmcopilot-web",
            "clientAppName": "Office",
            "clientEntrypoint": "mcmcopilot-officeweb",
            "clientSessionId": session_id,
            "clientAppType": "Web",
            "ProductCategory": "Chat",
            "productEntryPoint": "ChatPanel",
            "deviceOS": "Linux",
            "deviceType": "Desktop",
        },
        "message": {
            "author": "user",
            "inputMethod": "Keyboard",
            "text": text,
            "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
            "requestId": request_id,
            "locationInfo": {
                "timeZoneOffset": -(gmtoff // 60),
                "timeZone": time.tzname[0] or "UTC",
            },
            "locale": "en-us",
            "messageType": "Chat",
            "experienceType": "Default",
            "adaptiveCards": [],
            "clientPreferences": {},
        },
        "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
        "isSbsSupported": True,
        "tone": tone,
        "renderReferencesBehindEOS": True,
        "disconnectBehavior": "continue",
    }
    return {"arguments": [args], "invocationId": "0", "target": "chat", "type": 4}


def build_metrics_frame():
    stamp = iso_now()
    return {
        "arguments": [{"Timestamps": {
            "ConnectionStart": stamp,
            "UserInputStart": stamp,
            "ConnectionEstablished": stamp,
            "UserInputSubmit": stamp,
        }}],
        "target": "Metrics",
        "type": 1,
    }


class TurnResult:
    def __init__(self, text="", error=None, throttle=None, message_type=None):
        self.text = text
        self.error = error
        self.throttle = throttle
        self.message_type = message_type


def chat_once(cfg, token, claims, session, text, tone, timeout_s, on_frame=None):
    request_id = str(uuid.uuid4())
    params = urllib.parse.urlencode({
        "chatsessionid": request_id,
        "clientrequestid": request_id,
        "X-SessionId": session.session_id,
        "ConversationId": session.conversation_id,
        "access_token": token,
        "variants": VARIANTS,
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Starter",
        "agent": "web",
        "scenario": "OfficeWebIncludedCopilot",
    })
    url = f"{cfg.data['ws_base']}/{claims['oid']}@{claims['tid']}?{params}"
    ws = WebSocketClient(url, headers=WS_HEADERS, sock_timeout=30)
    answer = ""
    throttle = None
    last_message_type = None
    deadline = time.monotonic() + timeout_s
    try:
        ws.connect()
        ws.send_text(json.dumps({"protocol": "json", "version": 1}) + RS)
        raw = ws.recv_text(deadline)
        for part in raw.split(RS):
            part = part.strip()
            if not part:
                continue
            try:
                frame = json.loads(part)
            except json.JSONDecodeError:
                continue
            if frame.get("error"):
                return TurnResult(error="handshake error: " + str(frame["error"]))
        ws.send_text(json.dumps({"type": 6}) + RS)
        chat = build_chat_frame(text, tone, session.session_id, request_id,
                                session.turn_count == 0)
        ws.send_text(json.dumps(chat) + RS + json.dumps(build_metrics_frame()) + RS)
        while True:
            raw = ws.recv_text(deadline)
            for part in raw.split(RS):
                part = part.strip()
                if not part:
                    continue
                try:
                    frame = json.loads(part)
                except json.JSONDecodeError:
                    continue
                if on_frame:
                    on_frame()
                ftype = frame.get("type")
                if ftype == 6:
                    ws.send_text(json.dumps({"type": 6}) + RS)
                    continue
                if ftype == 7:
                    err = frame.get("error")
                    if err:
                        return TurnResult(error="server closed: " + str(err))
                    return TurnResult(answer, throttle=throttle, message_type=last_message_type)
                if ftype == 3:
                    if frame.get("error"):
                        return TurnResult(error="turn failed: " + str(frame["error"]))
                    return TurnResult(answer, throttle=throttle, message_type=last_message_type)
                if ftype == 2:
                    item = frame.get("item") or {}
                    thr = item.get("throttling")
                    if thr:
                        throttle = (thr.get("numUserMessagesInConversation"),
                                    thr.get("maxNumUserMessagesInConversation"))
                    for m in item.get("messages") or []:
                        if m.get("author") == "bot":
                            if m.get("messageType"):
                                last_message_type = m["messageType"]
                            elif m.get("text"):
                                answer, _ = fold_text(answer, m["text"])
                    return TurnResult(answer, throttle=throttle, message_type=last_message_type)
                if ftype == 1 and frame.get("target") == "update":
                    for arg in frame.get("arguments") or []:
                        if not isinstance(arg, dict):
                            continue
                        if "throttling" in arg:
                            thr = arg["throttling"]
                            throttle = (thr.get("numUserMessagesInConversation"),
                                        thr.get("maxNumUserMessagesInConversation"))
                        cursor = arg.get("writeAtCursor")
                        if cursor:
                            answer, _ = fold_text(answer, answer + cursor)
                        for m in arg.get("messages") or []:
                            if m.get("author") != "bot":
                                continue
                            if m.get("messageType"):
                                last_message_type = m["messageType"]
                                continue
                            if m.get("text"):
                                answer, _ = fold_text(answer, m["text"])
    except TurnTimeout:
        return TurnResult(error=f"timed out after {timeout_s}s (try --timeout or another model)")
    except WsClosed as e:
        if answer:
            return TurnResult(answer, throttle=throttle, message_type=last_message_type)
        return TurnResult(error=f"connection closed before any content ({e})")
    except (OSError, ssl.SSLError) as e:
        return TurnResult(error=f"connection error: {e}")
    finally:
        ws.close()


def strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text.strip()


def _coerce_args(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def parse_tool_calls(response_text):
    if not response_text:
        return [], ""
    text = response_text
    marker_pos = text.find(TOOL_CALL_START)
    if marker_pos > 0:
        text = text[marker_pos:]
    pattern = re.compile(
        re.escape(TOOL_CALL_START) + r"\s*(.*?)\s*" + re.escape(TOOL_CALL_END), re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        bare = strip_fences(response_text.strip())
        if bare.startswith("{") and bare.endswith("}"):
            try:
                parsed = json.loads(bare)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("name"):
                args = _coerce_args(parsed.get("arguments", parsed.get("args")))
                return [(parsed["name"], args)], ""
        return [], response_text.strip()
    calls = []
    leftover = []
    last_end = 0
    for m in matches:
        before = text[last_end:m.start()].strip()
        if before:
            leftover.append(before)
        last_end = m.end()
        raw = strip_fences(m.group(1))
        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*([}\]])", r"\1", raw)
            try:
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict) and parsed.get("name"):
            args = _coerce_args(parsed.get("arguments", parsed.get("args")))
            calls.append((parsed["name"], args))
    after = text[last_end:].strip()
    if after:
        leftover.append(after)
    return calls, "\n".join(leftover).strip()


def looks_like_confabulation(text):
    if not text or len(text.strip()) < 20:
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in CONFAB_PATTERNS)


def strip_thinking(text):
    lines = text.split("\n")
    kept = []
    skipping = True
    for line in lines:
        s = line.strip()
        if skipping:
            if not s:
                continue
            if re.fullmatch(r"\*{1,3}[^*]+\*{1,3}", s):
                continue
            skipping = False
        kept.append(line)
    return "\n".join(kept).strip()


def walk_files(root, cap=MAX_LIST_ENTRIES):
    out = []
    root = Path(root)
    stack = [(root, 0)]
    while stack and len(out) < cap:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            continue
        for e in entries:
            if len(out) >= cap:
                break
            name = e.name
            if name in SKIP_DIRS or (name.startswith(".") and depth == 0):
                continue
            if e.is_dir():
                if depth < 8:
                    stack.append((e, depth + 1))
            else:
                out.append(e)
    return out


def looks_binary(path):
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return True
    return b"\x00" in head


def graph_available(root):
    return (Path(root) / GRAPH_JSON).is_file()


def graphify_command(cfg, root):
    raw = cfg.data.get("graphify_cmd") or ""
    if raw:
        return shlex.split(raw)
    if shutil.which("graphify"):
        return ["graphify"]
    marker = Path(root) / "graphify-out" / ".graphify_python"
    if marker.is_file():
        try:
            interp = marker.read_text("utf-8").strip()
        except OSError:
            interp = ""
        if interp:
            return [interp, "-m", "graphify"]
    return None


def load_instructions(cfg, root):
    parts = []
    for label, path in (("global", Path(cfg.home) / "AGENTS.md"),
                        ("project", Path(root) / "AGENTS.md")):
        try:
            if path.is_file():
                text = path.read_text("utf-8", errors="replace").strip()
                if text:
                    parts.append(f"[{label}]\n{text}")
        except OSError:
            pass
    return "\n\n".join(parts)[:MAX_INSTRUCTION_CHARS]


def load_issues(root):
    path = Path(root) / "issues.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, list) else None


def format_issue_brief(issue):
    lines = [f"{issue.get('id', '?')} - {issue.get('title', '')}",
             f"  status  : {issue.get('status', 'open')}",
             f"  priority: {issue.get('priority', '?')}",
             f"  summary : {issue.get('summary', '')}"]
    ctx = issue.get("context")
    if ctx:
        lines.append(f"  context : {ctx}")
    for i, item in enumerate(issue.get("acceptance") or [], 1):
        lines.append(f"  accept{i}: {item}")
    notes = issue.get("notes")
    if notes:
        lines.append(f"  notes   : {notes}")
    return "\n".join(lines)


class ProjectContext:
    def __init__(self, root, budget_kb, extra=(), dropped=()):
        self.root = Path(root).resolve()
        self.budget = budget_kb * 1024
        self.extra = list(extra)
        self.dropped = list(dropped)
        self.files = []

    def rebuild(self):
        files = walk_files(self.root)
        files = [f for f in files
                 if not any(fnmatch.fnmatch(str(f.relative_to(self.root)), g)
                            or fnmatch.fnmatch(f.name, g) for g in self.dropped)]
        forced = []
        for g in self.extra:
            forced.extend(p for p in self.root.glob(g) if p.is_file())
        self.files = sorted(set(forced) | set(files),
                            key=lambda p: str(p.relative_to(self.root)))
        return self

    def render(self):
        rel = [str(p.relative_to(self.root)) for p in self.files]
        tree = "\n".join(rel) if rel else "(no files)"
        blocks = []
        used = len(tree)
        for p in self.files:
            if used >= self.budget:
                blocks.append(f"--- context budget reached, "
                              f"{len(rel) - len(blocks)} more files not shown ---")
                break
            try:
                if p.stat().st_size > MAX_FILE_BYTES or looks_binary(p):
                    continue
                content = p.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if len(content) + used > self.budget:
                content = content[: self.budget - used]
                blocks.append(f"--- file: {p.relative_to(self.root)} ---\n{content}\n(truncated)")
                used = self.budget
                break
            blocks.append(f"--- file: {p.relative_to(self.root)} ---\n{content}")
            used += len(content) + 32
        return tree, "\n\n".join(blocks)

    def stats(self):
        total = 0
        for p in self.files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return len(self.files), total


class ToolBox:
    def __init__(self, app):
        self.app = app

    def confirm(self, action):
        if self.app.auto_yes:
            return True
        try:
            line = input(f"  allow {action}? [y/N] ")
        except EOFError:
            return False
        return line.strip().lower() in ("y", "yes")

    def _resolve(self, path):
        p = Path(path)
        if not p.is_absolute():
            p = self.app.root / p
        return p

    def read(self, args):
        path = args.get("path") or args.get("filePath") or args.get("file")
        if not path:
            return "ERROR: read requires path"
        p = self._resolve(path)
        try:
            text = p.read_text("utf-8", errors="replace")
        except OSError as e:
            return f"ERROR: {e}"
        lines = text.split("\n")
        start = args.get("start")
        end = args.get("end")
        s0 = 0
        try:
            if start is not None:
                s0 = max(int(start) - 1, 0)
            if end is not None:
                lines = lines[s0:int(end)]
            elif start is not None:
                lines = lines[s0:]
        except (TypeError, ValueError):
            pass
        head = f"(showing lines {s0 + 1}-{s0 + len(lines)} of {p})\n" if (start or end) else ""
        body = "\n".join(f"{s0 + i + 1}| {l}" for i, l in enumerate(lines))
        out = head + body
        if len(out) > MAX_READ_CHARS:
            out = out[:MAX_READ_CHARS] + "\n...[truncated]"
        return out or "(empty file)"

    def list(self, args):
        path = args.get("path") or "."
        p = self._resolve(path)
        if not p.is_dir():
            return f"ERROR: not a directory: {path}"
        files = walk_files(p)
        out = []
        for f in files[:MAX_LIST_ENTRIES]:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append(f"{f.relative_to(p)}  ({size}B)")
        if not out:
            return "(empty)"
        note = f"\n({len(files)} entries)" if len(files) > MAX_LIST_ENTRIES else ""
        return "\n".join(out) + note

    def write(self, args):
        path = args.get("path") or args.get("filePath") or args.get("file")
        content = args.get("content", args.get("text"))
        if not path or content is None:
            return "ERROR: write requires path and content"
        content = str(content)
        p = self._resolve(path)
        inside = str(p.resolve()).startswith(str(self.app.root))
        marker = "" if inside else " (OUTSIDE project dir)"
        preview = content[:80].replace("\n", "\\n")
        if not self.confirm(f"write {len(content)}B to {p}{marker}: {preview!r}"):
            return "DENIED by user"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, "utf-8")
        except OSError as e:
            return f"ERROR: {e}"
        return f"wrote {len(content)} bytes to {p}"

    def edit(self, args):
        path = args.get("path") or args.get("filePath") or args.get("file")
        old = args.get("old")
        new = args.get("new")
        if not path or old is None or new is None:
            return "ERROR: edit requires path, old and new"
        old = str(old)
        new = str(new)
        if not old:
            return "ERROR: edit old must be non-empty"
        replace_all = args.get("replace_all", args.get("replaceAll", False))
        if isinstance(replace_all, str):
            replace_all = replace_all.strip().lower() in ("true", "1", "yes", "all")
        p = self._resolve(path)
        try:
            text = p.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"ERROR: {e}"
        count = text.count(old)
        if count == 0:
            return f"ERROR: old string not found in {p}"
        if count > 1 and not replace_all:
            return (f"ERROR: old string found {count} times in {p} - "
                    f"pass replace_all to replace every occurrence")
        new_text = text.replace(old, new)
        diff_lines = list(difflib.unified_diff(
            text.splitlines(), new_text.splitlines(),
            fromfile=str(p), tofile=str(p), lineterm=""))
        if len(diff_lines) > MAX_DIFF_LINES:
            diff_lines = diff_lines[:MAX_DIFF_LINES] + ["...[diff truncated]"]
        if diff_lines:
            print("\n".join(diff_lines))
        inside = str(p.resolve()).startswith(str(self.app.root))
        marker = "" if inside else " (OUTSIDE project dir)"
        if not self.confirm(f"edit {p} ({count} replacement"
                            f"{'s' if count > 1 else ''}){marker}"):
            return "DENIED by user"
        try:
            p.write_text(new_text, "utf-8")
        except OSError as e:
            return f"ERROR: {e}"
        return f"edited {p} ({count} replacement{'s' if count > 1 else ''})"

    def grep(self, args):
        pattern = args.get("pattern")
        if not pattern:
            return "ERROR: grep requires pattern"
        glob = args.get("glob") or "*"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex: {e}"
        matches = []
        for f in walk_files(self.root):
            rel = str(f.relative_to(self.app.root))
            if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f.name, glob)):
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES or looks_binary(f):
                    continue
                for i, line in enumerate(
                        f.read_text("utf-8", errors="replace").split("\n"), 1):
                    if rx.search(line):
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= 80:
                            matches.append("...[more matches truncated]")
                            return "\n".join(matches)
            except OSError:
                continue
        return "\n".join(matches) if matches else "(no matches)"

    def run(self, args):
        command = args.get("command") or args.get("cmd")
        if not command:
            return "ERROR: run requires command"
        try:
            timeout = min(int(args.get("timeout", 60)), 300)
        except (TypeError, ValueError):
            timeout = 60
        if not self.confirm(f"run: {command}"):
            return "DENIED by user"
        try:
            proc = subprocess.run(command, shell=True, cwd=str(self.app.root),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"TIMED OUT after {timeout}s"
        except OSError as e:
            return f"ERROR: {e}"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > MAX_RUN_OUTPUT_CHARS:
            out = out[:MAX_RUN_OUTPUT_CHARS] + "\n...[truncated]"
        return f"exit={proc.returncode}\n{out.strip() or '(no output)'}"

    def _graph_prelude(self):
        if not graph_available(self.app.root):
            return None, ("ERROR: no knowledge graph at graphify-out/graph.json - "
                          "build one first (graphify CLI) or answer from the files")
        cmd = graphify_command(self.app.cfg, self.app.root)
        if not cmd:
            return None, ("ERROR: graphify command not found - install graphify "
                          "or set CTX_GRAPHIFY_CMD")
        return cmd, None

    def _run_graph(self, cmd_args):
        cmd, err = self._graph_prelude()
        if err:
            return err
        try:
            proc = subprocess.run(cmd + cmd_args, cwd=str(self.app.root),
                                  capture_output=True, text=True, timeout=GRAPH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return f"ERROR: graphify timed out after {GRAPH_TIMEOUT}s"
        except OSError as e:
            return f"ERROR: {e}"
        out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        if len(out) > MAX_GRAPH_OUTPUT_CHARS:
            out = out[:MAX_GRAPH_OUTPUT_CHARS] + "\n...[truncated]"
        return out

    def graph_query(self, args):
        question = args.get("question") or args.get("q")
        if not question:
            return "ERROR: graph_query requires question"
        cmd_args = ["query", str(question)]
        mode = str(args.get("mode", "bfs")).lower()
        if mode in ("dfs", "deep"):
            cmd_args.append("--dfs")
        try:
            budget = int(args.get("budget", 2000))
        except (TypeError, ValueError):
            budget = 2000
        cmd_args += ["--budget", str(max(200, min(budget, 8000)))]
        return self._run_graph(cmd_args)

    def graph_path(self, args):
        a = args.get("from") or args.get("source") or args.get("start") or args.get("a")
        b = args.get("to") or args.get("target") or args.get("end") or args.get("b")
        if not a or not b:
            return "ERROR: graph_path requires from and to"
        return self._run_graph(["path", str(a), str(b)])

    def graph_explain(self, args):
        node = (args.get("node") or args.get("name")
                or args.get("topic") or args.get("concept"))
        if not node:
            return "ERROR: graph_explain requires node"
        return self._run_graph(["explain", str(node)])

    def dispatch(self, name, args):
        fn = getattr(self, name, None)
        if not callable(fn):
            return f"ERROR: unknown tool {name} (use read/list/write/edit/grep/run)"
        try:
            return str(fn(args or {}))
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"


TOOL_SPECS = [
    ("read", "read a file", "path*, start, end (1-based line numbers, optional)"),
    ("list", "list files under a path", "path (default '.')"),
    ("write", "create or overwrite a file (the user confirms)", "path*, content*"),
    ("edit", "replace an exact string in a file (the user confirms, diff preview)",
     "path*, old*, new*, replace_all (optional bool)"),
    ("grep", "regex search across project files", "pattern*, glob (default '*')"),
    ("run", "run a shell command in the project directory (the user confirms)",
     "command*, timeout (seconds, default 60)"),
]

GRAPH_TOOL_SPECS = [
    ("graph_query",
     "ask the codebase knowledge graph a question (architecture, data flow, dependencies)",
     "question*, mode (bfs|dfs, default bfs), budget"),
    ("graph_path", "shortest relationship path between two concepts in the graph",
     "from*, to*"),
    ("graph_explain", "plain-language explanation of one concept or node in the graph",
     "node*"),
]


def tool_spec_lines(has_graph=False):
    specs = list(TOOL_SPECS) + (list(GRAPH_TOOL_SPECS) if has_graph else [])
    return "\n".join(f"- {n}: {d} | {p}" for n, d, p in specs)


def system_prompt(root, instructions="", has_graph=False):
    tools = tool_spec_lines(has_graph)
    head = (f"You are a coding agent running in a terminal on Linux (WSL). "
            f"Working directory: {root}.\n")
    if instructions:
        head += f"\nUSER INSTRUCTIONS\nThese override the defaults where they conflict.\n{instructions}\n"
    if has_graph:
        head += ("\nKNOWLEDGE GRAPH\nA knowledge graph of this codebase is available "
                 "(graphify-out/graph.json). For architecture questions - how X works, "
                 "what depends on Y, where Z lives - prefer the graph tools over reading "
                 "many files. Use read/grep when you need exact source lines.\n")
    return head + f"""The user asks questions about the codebase and requests changes. You act through tool calls only.

TOOL PROTOCOL
When you need to inspect files, modify files, or run a command, respond with EXACTLY one tool call in this format and nothing else:
{TOOL_CALL_START}
{{"name": "read", "arguments": {{"path": "src/app.py"}}}}
{TOOL_CALL_END}
Tools (* marks a required param):
{tools}
Rules:
- Output ONLY the tool call block when calling a tool. No prose around it.
- The next message you receive will be "[Result of <name>]: ..." - then continue the task.
- Never claim you cannot access files or run commands: you can, via these tools.
- Never claim an action is done without issuing its tool call first.
- Do not invent or simulate tool results; do not run your own Python sandbox.
- When no tool is needed, answer in plain text with no tool call block.
"""


def first_message(ctx, user_text, instructions="", has_graph=False):
    tree, blocks = ctx.render()
    files = f"<files>\n{tree}\n\n{blocks}\n</files>" if blocks else f"<files>\n{tree}\n</files>"
    sys_part = system_prompt(ctx.root, instructions, has_graph)
    return (f"{sys_part}\nPROJECT FILES\n{files}\n\n"
            f"CONVERSATION\n<user>\n{user_text}\n</user>\n")


def delta_message(results):
    parts = []
    for name, meta, result in results:
        attrs = " ".join(f'{k}="{str(v)[:80]}"' for k, v in meta.items()) if meta else ""
        head = f'<tool_response tool="{name}"'
        if attrs:
            head += " " + attrs
        head += ">"
        parts.append(f"{head}\n[Result of {name}]: {result}\n</tool_response>")
    parts.append(f"Continue the task. Issue the next {TOOL_CALL_START} block if needed, "
                 "otherwise give your final answer as plain text.")
    return "\n\n".join(parts)


def nudge_message():
    return ("<notice>\nYou DO have these tools available in this environment: "
            "read, list, write, grep, run. The previous reply was a mistake. "
            f"Issue a {TOOL_CALL_START} block now to complete the task.\n</notice>")


def flatten_history(history, budget=40_000):
    parts = []
    used = 0
    for h in history:
        text = h["text"]
        if h["role"] == "tool" and len(text) > 4000:
            text = text[:1500] + "\n...[elided]...\n" + text[-1500:]
        entry = f"<{h['role']}>\n{text}\n</{h['role']}>"
        if used + len(entry) > budget and h["role"] == "tool":
            entry = f"<{h['role']}>\n[elided]\n</{h['role']}>"
        parts.append(entry)
        used += len(entry)
    return "\n\n".join(parts)


def http_post_json(url, headers, payload, timeout):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:
            detail = ""
        raise CtxError(f"HTTP {e.code} from {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise CtxError(f"network error talking to {url.split('/')[2]}: {e.reason}") from None
    except json.JSONDecodeError:
        raise CtxError("provider returned non-JSON response") from None


def build_http_messages(app):
    msgs = []
    pending = []

    def flush():
        if pending:
            msgs.append({"role": "user", "content": "\n\n".join(pending)})
            pending.clear()

    first_user_done = False
    for h in app.history:
        if h["role"] == "user":
            text = h["text"]
            if not first_user_done:
                first_user_done = True
                tree, blocks = app.ctx.render()
                files = (f"<files>\n{tree}\n\n{blocks}\n</files>" if blocks
                         else f"<files>\n{tree}\n</files>")
                text = f"{files}\n\n{text}"
            pending.append(text)
        elif h["role"] == "assistant":
            flush()
            msgs.append({"role": "assistant", "content": h["text"]})
        else:
            pending.append(h["text"])
    flush()
    return msgs


def extract_http_text(provider, data):
    try:
        if provider == "chat":
            return data["choices"][0]["message"]["content"] or ""
        if provider == "messages":
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        out = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for b in item.get("content", []):
                    if b.get("type") == "output_text":
                        out.append(b.get("text", ""))
        return "".join(out)
    except (KeyError, IndexError, TypeError) as e:
        raise CtxError(f"unexpected provider response shape: {e}") from None


class Session:
    def __init__(self):
        self.conversation_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.turn_count = 0

    def advance(self):
        self.turn_count += 1


def sanitize_history(raw):
    if not isinstance(raw, list):
        return []
    return [
        {"role": h["role"], "text": h["text"]}
        for h in raw
        if isinstance(h, dict) and h.get("role") in ("user", "assistant", "tool")
        and isinstance(h.get("text"), str)]


def load_session_file(path):
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("history"), list):
        return None
    data["history"] = sanitize_history(data["history"])
    data["id"] = str(data.get("id") or path.stem)
    return data


def find_sessions(cfg):
    found = []
    if cfg.session_dir.is_dir():
        for p in cfg.session_dir.glob("*.json"):
            data = load_session_file(p)
            if data and data["history"]:
                try:
                    mtime = p.stat().st_mtime_ns
                except OSError:
                    mtime = 0
                found.append((data, mtime))
    found.sort(key=lambda t: (str(t[0].get("updated") or ""), t[1]), reverse=True)
    return [d for d, _ in found]


def resume_session(cfg, ref):
    ref = (ref or "").strip()
    if ref.endswith(".json"):
        ref = ref[:-5]
    sessions = find_sessions(cfg)
    if not sessions:
        raise CtxError(f"no saved sessions under {cfg.session_dir}")
    if not ref:
        return sessions[0]
    for s in sessions:
        if s["id"] == ref:
            return s
    raise CtxError("no saved session '" + ref + "' - known: "
                   + ", ".join(s["id"] for s in sessions[:12]))


class App:
    def __init__(self, cfg, root, auto_yes=False, verbose=False):
        self.cfg = cfg
        self.root = Path(root).resolve()
        self.auto_yes = auto_yes
        self.verbose = verbose
        self.model = cfg.data["model"]
        if cfg.data.get("provider") not in PROVIDERS:
            raise CtxError(f"unknown provider '{cfg.data.get('provider')}' - "
                           "use m365, chat, messages or responses")
        self.store = TokenStore(cfg)
        self.tools = ToolBox(self)
        self.ctx = ProjectContext(self.root, cfg.data["context_kb"])
        self.ctx.rebuild()
        self.session = None
        self.history = []
        self.rotate_pending = False
        self.session_slot = None
        self.session_created = None

    @property
    def provider(self):
        return self.cfg.data["provider"]

    def model_ref(self):
        if self.provider == "m365":
            return self.cfg.model_tone(self.model)
        return self.model

    def instruction_text(self):
        return load_instructions(self.cfg, self.root)

    def has_graph(self):
        return graph_available(self.root)

    def sys_prompt(self):
        return system_prompt(self.root, self.instruction_text(), self.has_graph())

    def first_msg(self, user_text):
        return first_message(self.ctx, user_text, self.instruction_text(),
                             self.has_graph())

    def graphify_cmd(self):
        return graphify_command(self.cfg, self.root)

    def creds(self):
        data = self.store.acquire()
        token = data["access_token"]
        claims = data.get("claims") or jwt_decode(token)
        if not claims.get("oid") or not claims.get("tid"):
            raise CtxError("token missing oid/tid claims - re-login with: ctx login")
        return token, claims

    def new_session(self):
        self.session = Session()
        self.rotate_pending = False

    def save_session(self):
        if not self.history:
            return None
        if not self.session_slot:
            self.session_slot = uuid.uuid4().hex[:8]
        if not self.session_created:
            self.session_created = iso_now()
        data = {
            "id": self.session_slot,
            "created": self.session_created,
            "updated": iso_now(),
            "model": self.model,
            "provider": self.provider,
            "root": str(self.root),
            "history": self.history,
        }
        if self.session and self.provider == "m365":
            data["conversation_id"] = self.session.conversation_id
            data["session_id"] = self.session.session_id
            data["turn_count"] = self.session.turn_count
        try:
            self.cfg.session_dir.mkdir(parents=True, exist_ok=True)
            path = self.cfg.session_dir / (self.session_slot + ".json")
            path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        except OSError as e:
            eprint("warning: could not save session: " + str(e))
            return None
        return self.session_slot

    def restore_session(self, data):
        self.history = sanitize_history(data.get("history"))
        self.session_slot = data.get("id") or None
        self.session_created = data.get("created") or None
        model = data.get("model")
        if isinstance(model, str) and model:
            if self.provider != "m365":
                self.model = model
            else:
                try:
                    self.cfg.model_tone(model)
                    self.model = model
                except CtxError:
                    pass
        if self.provider == "m365" and isinstance(data.get("conversation_id"), str) \
                and data["conversation_id"]:
            s = Session()
            s.conversation_id = data["conversation_id"]
            s.session_id = data.get("session_id")
            if not isinstance(s.session_id, str) or not s.session_id:
                s.session_id = str(uuid.uuid4())
            try:
                s.turn_count = int(data.get("turn_count") or 0)
            except (TypeError, ValueError):
                s.turn_count = 0
            self.session = s
        return len(self.history)

    def chat(self, payload, tone, on_frame=None):
        if self.provider != "m365":
            return self.http_chat()
        token, claims = self.creds()
        result = chat_once(self.cfg, token, claims, self.session, payload, tone,
                           int(self.cfg.data["turn_timeout_s"]), on_frame=on_frame)
        self.session.advance()
        return result

    def http_chat(self):
        provider = self.provider
        base = self.cfg.data["base_url"].rstrip("/")
        key = self.cfg.data["api_key"]
        if not base or not key:
            raise CtxError("http provider needs CTX_BASE_URL and CTX_API_KEY")
        path = PROVIDER_PATHS[provider]
        url = base if base.endswith(path) else base + path
        sys_text = self.sys_prompt()
        msgs = build_http_messages(self)
        max_tokens = int(self.cfg.data["max_tokens"])
        headers = {"Authorization": "Bearer " + key}
        if provider == "messages":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            body = {"model": self.model, "system": sys_text, "messages": msgs,
                    "max_tokens": max_tokens}
        elif provider == "responses":
            headers["x-api-key"] = key
            inp = [{"role": m["role"],
                    "content": [{"type": "input_text" if m["role"] == "user"
                                 else "output_text", "text": m["content"]}]}
                   for m in msgs]
            body = {"model": self.model, "instructions": sys_text, "input": inp,
                    "max_output_tokens": max_tokens}
        else:
            body = {"model": self.model,
                    "messages": [{"role": "system", "content": sys_text}] + msgs,
                    "max_tokens": max_tokens}
        data = http_post_json(url, headers, body, int(self.cfg.data["turn_timeout_s"]))
        return TurnResult(text=extract_http_text(provider, data))

    def send(self, payload, tone, quiet=False):
        if self.provider != "m365":
            return self._send_http(quiet)
        state = {"last": time.monotonic(), "any": False}

        def on_frame():
            if quiet:
                return
            now = time.monotonic()
            if now - state["last"] >= 4:
                print(".", end="", flush=True)
                state["last"] = now
                state["any"] = True

        result = self.chat(payload, tone, on_frame=on_frame)
        if state["any"]:
            print()
        return result

    def _send_http(self, quiet):
        state = {"any": False}
        stop = threading.Event()

        def beat():
            while not stop.wait(4):
                if not quiet:
                    print(".", end="", flush=True)
                    state["any"] = True

        th = threading.Thread(target=beat, daemon=True)
        th.start()
        try:
            return self.http_chat()
        finally:
            stop.set()
            th.join(timeout=1)
            if state["any"] and not quiet:
                print()

    def run_task(self, user_text, quiet=False):
        tone = self.model_ref()
        nudged = False
        if self.session is None or self.rotate_pending:
            self.new_session()
            if self.history:
                payload = (f"{self.sys_prompt()}\nCONVERSATION SO FAR\n"
                           f"{flatten_history(self.history)}\n\n<user>\n{user_text}\n</user>\n")
            else:
                payload = self.first_msg(user_text)
        elif self.session.turn_count == 0:
            payload = self.first_msg(user_text)
        else:
            payload = f"<user>\n{user_text}\n</user>\n"
        self.history.append({"role": "user", "text": user_text})
        for _ in range(int(self.cfg.data["max_iter"])):
            result = self.send(payload, tone, quiet=quiet)
            if result.error:
                self._report_error(result)
                self.save_session()
                return
            if result.throttle:
                cur, mx = result.throttle
                if mx and cur is not None and cur >= mx - 2:
                    self.rotate_pending = True
            calls, _ = parse_tool_calls(result.text)
            if not calls:
                if not nudged and looks_like_confabulation(result.text):
                    nudged = True
                    self.history.append({"role": "assistant", "text": result.text})
                    payload = nudge_message()
                    self.history.append({"role": "user", "text": payload})
                    if not quiet:
                        eprint("  (model claimed it had no tools - nudging)")
                    continue
                self.history.append({"role": "assistant", "text": result.text})
                if not quiet:
                    print(strip_thinking(result.text) or "(empty response)")
                self.save_session()
                return
            self.history.append({"role": "assistant", "text": result.text})
            results = []
            for name, args in calls:
                if not quiet:
                    print(f"-> {name} {self._call_summary(name, args)}")
                out = self.tools.dispatch(name, args)
                if len(out) > MAX_TOOL_RESULT_CHARS:
                    out = out[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
                meta = {k: v for k, v in args.items()
                        if isinstance(v, (str, int, float)) and k != "content"}
                results.append((name, meta, out))
                if not quiet:
                    print(f"   {out.split(chr(10))[0][:120]}")
            for name, _meta, out in results:
                self.history.append({"role": "tool", "text": f"[Result of {name}]: {out}"})
            payload = delta_message(results)
        if not quiet:
            eprint("  reached max tool iterations - ask it to wrap up")
        self.save_session()

    def _call_summary(self, name, args):
        for k in ("path", "filePath", "file", "pattern", "command", "cmd"):
            if args.get(k):
                return str(args[k])[:80]
        if args.get("content") is not None:
            return f"({len(str(args.get('content')))}B content)"
        return ""

    def _report_error(self, result):
        err = result.error or "(empty response)"
        print(f"error: {err}")
        if result.message_type == "Disengaged":
            print("  the backend disengaged (content filter or payload too large)")
            print("  try: /drop to shrink context, /clear, or /model")
        elif "Failed to invoke" in err:
            print(f"  the tone '{self.cfg.model_tone(self.model)}' may be unavailable - try /model")
        elif "401" in err or "nauthorized" in err:
            print("  run: ctx login (m365) or check CTX_API_KEY (http provider)")

    def add_files(self, globs):
        self.ctx.extra.extend(globs)
        self.ctx.rebuild()

    def drop_files(self, globs):
        self.ctx.dropped.extend(globs)
        self.ctx.rebuild()

    def clear(self):
        self.session = None
        self.history = []
        self.rotate_pending = False
        self.session_slot = None
        self.session_created = None

    def save_transcript(self, path=None):
        self.cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
        if not path:
            path = str(self.cfg.transcript_dir / (time.strftime("%Y%m%d-%H%M%S") + ".md"))
        lines = [f"## {h['role']}\n\n{h['text']}\n" for h in self.history]
        Path(path).write_text("# transcript\n\n" + "\n".join(lines), "utf-8")
        return path


def cmd_login(args, cfg):
    store = TokenStore(cfg)
    if args.paste:
        paste_login(store)
    else:
        device_login(store)


def cmd_logout(args, cfg):
    TokenStore(cfg).clear()
    print("token removed")


def cmd_status(args, cfg):
    data = TokenStore(cfg).load()
    print(f"config   : {cfg.path}")
    provider = cfg.data.get("provider", "m365")
    if provider == "m365":
        print(f"ws base  : {cfg.data['ws_base']}")
    else:
        print(f"provider : {provider}   base: {cfg.data.get('base_url') or '(unset)'}")
        print(f"api key  : {'set' if cfg.data.get('api_key') else '(unset)'}")
    print(f"model    : {cfg.data['model']}")
    if provider != "m365":
        return
    if data is None:
        print("token    : none (run: ctx login)")
        return
    claims = data.get("claims") or {}
    remaining = int(data.get("expires_at", 0) - time.time())
    state = "valid" if remaining > 0 else "EXPIRED"
    print(f"account  : {data.get('account') or claims.get('preferred_username') or '?'}")
    print(f"token    : {state} ({remaining // 60} min remaining)")
    print(f"refresh  : {'yes' if data.get('refresh_token') else 'no'}")


HELP_TEXT = """commands:
  /help              this help
  /model [name]      show models or switch (sent as a per-request tone)
  /add GLOB...       force-include files in the context
  /drop GLOB...      exclude files from the context
  /ctx               context statistics
  /graph             knowledge graph status (graphify-out/)
  /issues [id]       local issue tracker (issues.json)
  /clear             reset conversation and history
  /save [file]       save transcript (default under the config dir)
  /sessions          list saved sessions (resume with: ctx --resume [id])
  /q                 quit
anything else is sent to the model."""


def repl(app):
    nfiles, _ = app.ctx.stats()
    print(f"model: {app.model}   files: {nfiles}   dir: {app.root}")
    if app.provider == "m365":
        try:
            data = app.store.load()
            if data and data.get("expires_at", 0) < time.time():
                print("note: token expired - run ctx login if requests fail")
        except Exception:
            pass
    print("type /help for commands\n")
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            continue
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            return
        if line == "/help":
            print(HELP_TEXT)
            continue
        if line.startswith("/model"):
            parts = line.split()
            if len(parts) == 1:
                print(f"current: {app.model}")
                if app.provider == "m365":
                    print("models : " + ", ".join(app.cfg.model_names))
                else:
                    print("models : any model id, sent as-is to the provider")
            else:
                try:
                    if app.provider == "m365":
                        app.cfg.model_tone(parts[1])
                    app.model = parts[1]
                    print(f"model set to {app.model}")
                except CtxError as e:
                    print(str(e))
            continue
        if line.startswith("/add"):
            globs = line.split()[1:]
            if not globs:
                print("usage: /add GLOB...")
                continue
            app.add_files(globs)
            print(f"context: {app.ctx.stats()[0]} files")
            continue
        if line.startswith("/drop"):
            globs = line.split()[1:]
            if not globs:
                print("usage: /drop GLOB...")
                continue
            app.drop_files(globs)
            print(f"context: {app.ctx.stats()[0]} files")
            continue
        if line == "/ctx":
            n, size = app.ctx.stats()
            tree, blocks = app.ctx.render()
            print(f"{n} files, {size} bytes on disk, "
                  f"prompt context {len(tree) + len(blocks)} chars")
            continue
        if line == "/graph":
            gj = app.root / GRAPH_JSON
            if not gj.is_file():
                print("no graph (graphify-out/graph.json missing)")
            else:
                try:
                    data = json.loads(gj.read_text("utf-8"))
                    print(f"graph: {len(data.get('nodes', []))} nodes, "
                          f"{len(data.get('edges', []))} edges")
                except Exception:
                    print(f"graph file present ({gj.stat().st_size}B) but not parsable")
            cmd = app.graphify_cmd()
            print("cli: " + (" ".join(cmd) if cmd else "not found (set CTX_GRAPHIFY_CMD)"))
            continue
        if line.startswith("/issues"):
            parts = line.split()
            issues = load_issues(app.root)
            if issues is None:
                print("no readable issues.json in the project root")
                continue
            if len(parts) > 1:
                match = [i for i in issues if i.get("id") == parts[1]]
                if not match:
                    ids = ", ".join(str(i.get("id", "?")) for i in issues)
                    print(f"no issue '{parts[1]}' - known ids: {ids}")
                else:
                    print(format_issue_brief(match[0]))
                continue
            if not issues:
                print("issue tracker is empty")
                continue
            open_n = sum(1 for i in issues if str(i.get("status", "open")) == "open")
            print(f"{len(issues)} issues ({open_n} open):")
            for i in issues:
                print(f"  [{str(i.get('status', 'open')):>11}] "
                      f"{str(i.get('priority', '?')):>6}  "
                      f"{str(i.get('id', '?')):<20} {i.get('title', '')}")
            print("detail: /issues <id>")
            continue
        if line == "/clear":
            slot = app.session_slot
            app.clear()
            note = f" (detached from saved session {slot})" if slot else ""
            print("conversation reset" + note)
            continue
        if line.startswith("/save"):
            parts = line.split()
            path = app.save_transcript(parts[1] if len(parts) > 1 else None)
            print(f"saved: {path}")
            continue
        if line == "/sessions":
            sessions = find_sessions(app.cfg)
            if not sessions:
                print(f"no saved sessions ({app.cfg.session_dir})")
            else:
                print(f"{len(sessions)} saved sessions (newest first):")
                for s in sessions:
                    cur = ("  <- current" if app.session_slot
                           and s["id"] == app.session_slot else "")
                    print(f"  {s['id']}  {s.get('updated') or '?'}  "
                          f"{s.get('model') or '?'}  {s.get('provider') or '?'}  "
                          f"{len(s['history'])} msgs  {s.get('root') or '?'}{cur}")
                print("resume with: ctx --resume [id]")
            continue
        if line.startswith("/"):
            print("unknown command - /help")
            continue
        try:
            app.run_task(line)
        except CtxError as e:
            print(f"error: {e}")
        except KeyboardInterrupt:
            print("\n(interrupted)")


def normalize_resume_argv(argv):
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--resume":
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if not nxt or nxt.startswith("-") or nxt in SUBCOMMANDS:
                out.append("--resume=")
                i += 1
                continue
        out.append(tok)
        i += 1
    return out


def main():
    parser = argparse.ArgumentParser(
        prog=PROG, epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="local context helper - coding agent CLI over M365 Copilot chat")
    parser.add_argument("--dir", default=os.getcwd(), help="project directory (default cwd)")
    parser.add_argument("--model", help="model name for this run")
    parser.add_argument("--yes", action="store_true", help="skip write/run confirmations")
    parser.add_argument("--new", action="store_true", help="fresh conversation for every question")
    parser.add_argument("--resume", nargs="?", const="", default=None, metavar="ID",
                        help="continue the most recent (or the given) saved session")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--timeout", type=int, help="per-request timeout in seconds")
    sub = parser.add_subparsers(dest="cmd")
    p_login = sub.add_parser("login", help="authenticate (device code flow)")
    p_login.add_argument("--paste", action="store_true",
                         help="paste a token from browser devtools instead")
    sub.add_parser("logout", help="remove the stored token")
    sub.add_parser("status", help="show config and token status")
    p_ask = sub.add_parser("ask", help="ask one question and exit")
    p_ask.add_argument("question", nargs="+", help="the question")
    args = parser.parse_args(normalize_resume_argv(sys.argv[1:]))

    home = Path(os.environ.get("CTX_HOME") or DEFAULT_HOME)
    cfg = Config(home)
    if not cfg.path.exists():
        cfg.save()

    if args.cmd == "login":
        cmd_login(args, cfg)
        return
    if args.cmd == "logout":
        cmd_logout(args, cfg)
        return
    if args.cmd == "status":
        cmd_status(args, cfg)
        return

    if args.model:
        cfg.data["model"] = args.model
    if args.timeout:
        cfg.data["turn_timeout_s"] = args.timeout
    auto_yes = args.yes or os.environ.get("CTX_YES") == "1"
    root = Path(args.dir).resolve()
    if not root.is_dir():
        eprint(f"not a directory: {root}")
        sys.exit(1)
    app = App(cfg, root, auto_yes=auto_yes, verbose=args.verbose)
    if args.new:
        app.rotate_pending = True
    if args.resume is not None:
        try:
            data = resume_session(cfg, args.resume)
        except CtxError as e:
            eprint(f"error: {e}")
            sys.exit(1)
        restored = app.restore_session(data)
        print(f"resumed session {data['id']} ({restored} messages, model {app.model})")

    if args.cmd == "ask":
        question = " ".join(args.question)
        try:
            app.run_task(question)
        except CtxError as e:
            eprint(f"error: {e}")
            sys.exit(1)
        return

    repl(app)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
