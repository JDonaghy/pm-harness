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
