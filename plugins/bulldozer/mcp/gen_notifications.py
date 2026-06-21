#!/usr/bin/env python3
"""Maintainer tool: regenerate mcp/codex-notifications.json from the
live codex app-server protocol schema (the ServerNotification method set).

Usage:  python3 mcp/gen_notifications.py
Requires codex installed. Run after a codex upgrade alongside bumping
LAST_VERIFIED_CODEX_VERSION; commit the regenerated fixture.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))   # mcp/
OUT = os.path.join(HERE, "codex-notifications.json")  # sibling of codex_server.py (ships in cache)


def _walk_consts(node, acc):
    """Collect every {'method': {'const': X}} / {'method': {'enum': [X]}} value."""
    if isinstance(node, dict):
        m = node.get("method")
        if isinstance(m, dict):
            if "const" in m and isinstance(m["const"], str):
                acc.add(m["const"])
            elif isinstance(m.get("enum"), list) and len(m["enum"]) == 1:
                acc.add(m["enum"][0])
        for v in node.values():
            _walk_consts(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_consts(v, acc)


def main():
    codex = os.environ.get("JAINE_CODEX_BIN") or "codex"
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([codex, "app-server", "generate-json-schema", "--out", d], check=True)
        methods = set()
        for fn in os.listdir(d):
            try:
                doc = json.load(open(os.path.join(d, fn)))
            except Exception:
                continue
            # ONLY walk the ServerNotification union — NEVER fall through to whole-doc,
            # or ClientRequest.json (85)/ServerRequest.json (10)/ClientNotification.json (1)
            # pollute the set → 162 method names instead of 66 (empirically verified).
            defs = doc.get("definitions") or doc.get("$defs") or {}
            if "ServerNotification" in defs:
                # combined-schema file: restrict to the ServerNotification subtree
                _walk_consts(defs["ServerNotification"], methods)
            elif doc.get("title") == "ServerNotification":
                # ServerNotification.json: the top-level oneOf IS the union
                _walk_consts(doc, methods)
            # else: a request/other schema file — skip entirely
    if not methods:
        print("ERROR: no notification methods extracted — schema shape changed?", file=sys.stderr)
        sys.exit(1)
    payload = {"server_notifications": sorted(methods)}
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"wrote {len(methods)} notifications → {OUT}")


if __name__ == "__main__":
    main()
