"""Fake graphify CLI for offline tests.

Logs its argv as JSON lines to $FAKE_GRAPHIFY_LOG and prints deterministic
answers shaped like the real graphify query/path/explain output.
"""

import json
import os
import sys


def main():
    log = os.environ.get("FAKE_GRAPHIFY_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")
    args = sys.argv[1:]
    if args and args[0] == "query":
        q = args[1] if len(args) > 1 else ""
        print(f"GRAPH-ANSWER[{q}]: entrypoint is main.py; auth module depends on core")
    elif args and args[0] == "path":
        a = args[1] if len(args) > 1 else "?"
        b = args[2] if len(args) > 2 else "?"
        print(f"GRAPH-PATH: {a} -> {b} via 3 hops")
    elif args and args[0] == "explain":
        n = args[1] if len(args) > 1 else "?"
        print(f"GRAPH-EXPLAIN: {n} is a god node with 12 neighbors")
    else:
        print("GRAPH-OK")


if __name__ == "__main__":
    main()
