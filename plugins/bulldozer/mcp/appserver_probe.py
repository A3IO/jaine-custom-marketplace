#!/usr/bin/env python3
"""Empirical proof: drive `codex app-server` directly and show interactive
streaming + a CORRECT approval round-trip (the thing #18268 breaks in mcp-server).

Killer test: ask codex to write a file OUTSIDE its workspace (needs escalation).
With approvalPolicy=on-request the server sends us an approval REQUEST; we reply
{decision:"accept"}; if app-server honors it, the file is actually created —
proving accept propagates (unlike the stock mcp-server where accept→Denied).
"""
import json
import os
import select
import subprocess
import sys
import time

CODEX = os.environ.get("JAINE_CODEX_BIN", "/opt/homebrew/bin/codex")
SCRATCH = "/tmp/codex-as-scratch"
PROBE = os.path.expanduser("~/codex-appserver-probe.txt")
DEADLINE = 150  # seconds

os.makedirs(SCRATCH, exist_ok=True)
if os.path.exists(PROBE):
    os.remove(PROBE)

proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
_id = 0
start = time.monotonic()


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()
    print(f">>> {obj.get('method', obj.get('id'))}", flush=True)


def req(method, params=None):
    global _id
    _id += 1
    msg = {"id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    send(msg)
    return _id


def reply(rid, result):
    send({"id": rid, "result": result})


def readline_to(timeout):
    """Read one line from app-server stdout with a timeout; None on timeout."""
    r, _, _ = select.select([proc.stdout], [], [], timeout)
    if not r:
        return None
    line = proc.stdout.readline()
    return line.strip() or None


def pump(until):
    """Read+dispatch until `until(msg)` returns truthy or deadline hit. Returns the matching msg."""
    while True:
        if time.monotonic() - start > DEADLINE:
            print("!!! DEADLINE", flush=True)
            return None
        line = readline_to(2)
        if line is None:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print("  (non-json):", line[:120], flush=True)
            continue
        method = msg.get("method")
        # server -> client REQUEST (has both id and method): must reply
        if method and "id" in msg:
            print(f"<<< SERVER-REQ {method} (id={msg['id']})", flush=True)
            if method in ("item/commandExecution/requestApproval", "execCommandApproval",
                          "item/fileChange/requestApproval", "applyPatchApproval",
                          "item/permissions/requestApproval"):
                cmd = (msg.get("params") or {}).get("command")
                print(f"    APPROVAL asked for: {cmd} -> replying decision=accept", flush=True)
                reply(msg["id"], {"decision": "accept"})
            elif method == "mcpServer/elicitation/request":
                reply(msg["id"], {"action": "accept", "content": {}, "_meta": None})
            else:
                reply(msg["id"], {"decision": "decline"})
            continue
        # notification (method only)
        if method:
            p = msg.get("params") or {}
            if method == "item/agentMessage/delta":
                sys.stdout.write(p.get("delta", "")); sys.stdout.flush()
            elif method in ("turn/started", "turn/completed", "item/started", "item/completed",
                            "thread/started", "error", "item/commandExecution/outputDelta"):
                tag = method
                extra = p.get("status") or (p.get("item") or {}).get("type") or ""
                print(f"  ~ {tag} {extra}", flush=True)
            if until(msg):
                return msg
            continue
        # response (id + result/error)
        if "id" in msg:
            if "error" in msg:
                print(f"<<< ERROR id={msg['id']}: {msg['error']}", flush=True)
            else:
                print(f"<<< OK id={msg['id']}", flush=True)
            if until(msg):
                return msg


def main():
    rid = req("initialize", {"clientInfo": {"name": "jaine_probe", "title": "JAINE probe", "version": "0.1"},
                             "capabilities": {"experimentalApi": True}})
    init = pump(lambda m: m.get("id") == rid and ("result" in m or "error" in m))
    if not init or "error" in (init or {}):
        print("initialize FAILED — stderr:", proc.stderr.read()[-800:]); return
    print("  initialize result:", json.dumps(init.get("result", {}))[:200], flush=True)
    send({"method": "initialized"})

    rid = req("thread/start", {"cwd": SCRATCH, "approvalPolicy": "on-request", "sandbox": "workspace-write"})
    ts = pump(lambda m: m.get("id") == rid and ("result" in m or "error" in m))
    if not ts or "error" in (ts or {}):
        print("thread/start FAILED:", ts, "\nstderr:", proc.stderr.read()[-800:]); return
    thread = ts["result"].get("thread") or {}
    tid = thread.get("id") or thread.get("threadId")
    print("  threadId:", tid, flush=True)

    prompt = (f"Create a file at {PROBE} containing exactly PROBE. It is OUTSIDE your workspace, "
              "so the sandbox will block it — request approval to run the command, and once approved, "
              "perform the write. Then report whether the file was created.")
    rid = req("turn/start", {"threadId": tid,
                             "input": [{"type": "text", "text": prompt, "text_elements": []}]})
    print("\n--- streaming turn ---", flush=True)
    pump(lambda m: m.get("method") == "turn/completed")

    print("\n\n=== RESULT ===", flush=True)
    exists = os.path.exists(PROBE)
    print("probe file created:", exists, flush=True)
    if exists:
        print("contents:", repr(open(PROBE).read()), flush=True)
    proc.stdin.close()
    proc.terminate()


if __name__ == "__main__":
    main()
