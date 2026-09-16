# pm-harness

Experimental terminal coding-agent harness. Python 3.10+, stdlib only, no dependencies.

Talks to chat backends through a pluggable provider interface and runs local
tools (read / list / grep / write / run) against the working directory, with
confirmation prompts for anything that mutates state.

```
python3 ctx.py --help
python3 ctx.py ask "what does this project do"
python3 tests/run_tests.py     # offline, no network needed
```

## Using the M365 Copilot backend

The default provider drives the M365 Copilot chat backend over its SignalR
websocket, as the web client does. There is no public API, so ctx has to send
what the browser sends - and what that is varies by tenant and drifts over
time. `ctx setup` copies it from your own browser session:

```
python3 ctx.py setup
```

It walks you through devtools and reads two things off your clipboard: the
websocket connection url (query parameters and a token) and one outgoing chat
message (the request body ctx uses as a template). Then it checks the backend
accepts them. Nothing leaves the machine.

Requires Chrome devtools access to `m365.cloud.microsoft/chat`, and a
clipboard tool - `powershell.exe` under WSL, `pbpaste` on macOS, or
`wl-paste`/`xclip`/`xsel` on Linux.

### When it stops working

- **401 at the upgrade** - the token expired, they last about an hour and
  cannot be refreshed. Re-run `ctx setup`, or `ctx adopt --clipboard` for just
  the url.
- **InvalidRequest** - Microsoft changed the request shape. Re-run
  `ctx setup`; `ctx adopt --frame` prints a diff of what moved.
- `ctx status` shows what is currently captured, and `ctx adopt --reset`
  returns to the built-in defaults.

Device code auth (`ctx login`) also exists, but conditional access policies
commonly block it, which is why setup captures a token from the browser
instead.
