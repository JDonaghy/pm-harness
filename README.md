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

### How often each capture expires

`setup` captures three things, and they go stale on very different clocks:

| capture | lifetime | refresh with |
| --- | --- | --- |
| access token | about an hour, no refresh token | `ctx adopt --clipboard` |
| url parameters (`ws_params`) | weeks - until Microsoft rotates feature flags | `ctx adopt --clipboard` |
| chat frame (`frame_template`) | weeks to months - until the request shape changes | `ctx adopt --frame --clipboard` |

Day to day only the token expires, and copying the whole websocket url with
**Copy link address** refreshes the token and the url parameters together
without touching the frame template. `ctx setup` re-does all three and is only
needed on a new machine or when the frame has drifted.

ctx checks the token at startup and offers to take a fresh one from the
clipboard, and does the same mid-task if a turn fails, resending the turn
afterwards - so in practice the refresh happens when it is needed rather than
on a schedule.

### When it stops working

- **401 at the upgrade** - the token expired. `ctx adopt --clipboard`, or just
  answer the prompt when ctx offers.
- **InvalidRequest** - Microsoft changed the request shape, so the frame
  template is stale. `ctx adopt --frame --clipboard` prints a diff of what
  moved and adopts the new shape.
- `ctx status` shows what is currently captured and how long the token has
  left; `ctx adopt --reset` returns to the built-in defaults.

Device code auth (`ctx login`) also exists, but conditional access policies
commonly block it, which is why setup captures a token from the browser
instead.
