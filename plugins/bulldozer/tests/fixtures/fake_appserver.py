#!/usr/bin/env python3
"""Scripted fake `codex app-server` for offline reactor tests.

Speaks jsonrpc_lite (newline-delimited JSON, NO "jsonrpc":"2.0" field) on
stdin/stdout, exactly like real codex 0.141.0 app-server.

Controlled by the FAKE_SCRIPT env var:
  "basic"         — initialize → thread/start → done (no turn)
  "with_approval" — full turn sequence including a server→client approval
                    request (item/commandExecution/requestApproval) so the
                    reactor test can see a request frame.

Wire shapes match the verified live codex 0.141 schema
(/0/SANDBOX/CODEX-SETTINGS/codex-schema-0.141-experimental/).

Key wire facts (distinct from the TypeScript type names in the schema):
  - InitializeResponse: {userAgent, codexHome, platformFamily, platformOs}
    (NO "serverInfo" — that's the MCP 2.0 field, not codex app-server)
  - ThreadStartResponse: {thread:{id,...}, model, cwd, approvalPolicy,
    sandbox, ...} — client reads result["thread"]["id"]
  - TurnStartResponse: {turn:{id,...}} — sent FIRST as a response to the
    turn/start request id, THEN the notification stream follows
  - item/agentMessage/delta → params {delta, threadId, turnId, itemId}
  - item/completed  → params {item:{...}, threadId, turnId, completedAtMs}
  - turn/completed  → params {threadId, turn:{...}} — NO final text
  - server→client REQUEST: both "id" AND "method" present (jsonrpc_lite)
    e.g. {id:"req-1", method:"item/commandExecution/requestApproval",
          params:{threadId, turnId, itemId, startedAtMs, command}}
"""
import json
import os
import sys
import time
import threading


def _write(msg: dict):
    """Emit one jsonrpc_lite frame to stdout (no "jsonrpc" field)."""
    line = json.dumps(msg)
    sys.stdout.buffer.write((line + "\n").encode())
    sys.stdout.buffer.flush()


def _respond(rid, result: dict):
    _write({"id": rid, "result": result})


def _notify(method: str, params: dict):
    _write({"method": method, "params": params})


def _server_request(rid, method: str, params: dict):
    """Emit a server→client REQUEST (has both id and method — not a notification)."""
    _write({"id": rid, "method": method, "params": params})


# ── Scripted sequences ────────────────────────────────────────────────────


def _handle_initialize(req_id):
    _respond(req_id, {
        "userAgent": "codex/fake-0.141.0",
        "codexHome": "/tmp/fake-codex-home",
        "platformFamily": "unix",
        "platformOs": "macos",
    })


def _handle_thread_start(req_id):
    _respond(req_id, {
        "thread": {
            "id": "T1",
            "sessionId": "S1",
            "forkedFromId": None,
            "parentThreadId": None,
            "preview": "",
            "ephemeral": True,
            "modelProvider": "openai",
            "createdAt": int(time.time()),
            "updatedAt": int(time.time()),
            "status": "idle",
            "path": None,
            "cwd": "/tmp",
            "cliVersion": "0.141.0",
            "source": "app_server",
            "threadSource": None,
            "agentNickname": None,
            "agentRole": None,
            "gitInfo": None,
            "name": None,
            "turns": [],
        },
        "model": "gpt-4o",
        "modelProvider": "openai",
        "serviceTier": None,
        "cwd": "/tmp",
        "runtimeWorkspaceRoots": [],
        "instructionSources": [],
        "approvalPolicy": "never",
        "approvalsReviewer": "cli",
        "sandbox": "read-only",
        "activePermissionProfile": None,
        "reasoningEffort": None,
    })


def _handle_turn_start_basic(req_id, thread_id="T1"):
    """Basic turn: reply, then agentMessage/delta + item/completed + turn/completed."""
    turn_id = "TURN1"
    item_id = "ITEM1"

    # 1. TurnStartResponse (required — answers the turn/start request id)
    _respond(req_id, {
        "turn": {
            "id": turn_id,
            "items": [],
            "itemsView": "loaded",
            "status": "running",
            "error": None,
            "startedAt": int(time.time()),
            "completedAt": None,
            "durationMs": None,
        }
    })

    # 2. Text arrives via delta notifications (NOT in turn/completed)
    _notify("item/agentMessage/delta", {
        "delta": "Hello from fake!",
        "threadId": thread_id,
        "turnId": turn_id,
        "itemId": item_id,
    })

    # 3. Item completed (final text lives in item.content here)
    _notify("item/completed", {
        "item": {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello from fake!"}],
        },
        "threadId": thread_id,
        "turnId": turn_id,
        "completedAtMs": int(time.time() * 1000),
    })

    # 4. Turn completed (carries NO final text — only the end signal)
    _notify("turn/completed", {
        "threadId": thread_id,
        "turn": {
            "id": turn_id,
            "items": [],
            "itemsView": "loaded",
            "status": "completed",
            "error": None,
            "startedAt": int(time.time()),
            "completedAt": int(time.time()),
            "durationMs": 100,
        },
    })


def _handle_turn_start_with_approval(req_id, approval_reply_waiter, thread_id="T1"):
    """Turn with approval: emits a server→client REQUEST then waits for the reply."""
    turn_id = "TURN1"
    item_id = "ITEM1"
    approval_req_id = "APPROVAL-1"

    # 1. TurnStartResponse
    _respond(req_id, {
        "turn": {
            "id": turn_id,
            "items": [],
            "itemsView": "loaded",
            "status": "running",
            "error": None,
            "startedAt": int(time.time()),
            "completedAt": None,
            "durationMs": None,
        }
    })

    # 2. Approval REQUEST (server→client, has both id and method)
    _server_request(approval_req_id, "item/commandExecution/requestApproval", {
        "threadId": thread_id,
        "turnId": turn_id,
        "itemId": item_id,
        "startedAtMs": int(time.time() * 1000),
        "command": "echo hello",
        "cwd": "/tmp",
        "approvalId": None,
        "reason": None,
        "availableDecisions": ["accept", "decline"],
    })

    # 3. Wait for the client reply (with timeout)
    replied = approval_reply_waiter(approval_req_id, timeout=5.0)
    if not replied:
        # Timed out — just emit turn/completed so the test doesn't hang
        _notify("turn/completed", {
            "threadId": thread_id,
            "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                     "status": "failed", "error": None,
                     "startedAt": int(time.time()), "completedAt": int(time.time()),
                     "durationMs": 0},
        })
        return

    # 4. Resumed after approval — emit assistant text
    _notify("item/agentMessage/delta", {
        "delta": "Approved!",
        "threadId": thread_id,
        "turnId": turn_id,
        "itemId": item_id,
    })

    _notify("item/completed", {
        "item": {
            "id": item_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Approved!"}],
        },
        "threadId": thread_id,
        "turnId": turn_id,
        "completedAtMs": int(time.time() * 1000),
    })

    _notify("turn/completed", {
        "threadId": thread_id,
        "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                 "status": "completed", "error": None,
                 "startedAt": int(time.time()), "completedAt": int(time.time()),
                 "durationMs": 100},
    })


# ── Main dispatcher ───────────────────────────────────────────────────────

def main():
    script = os.environ.get("FAKE_SCRIPT", "basic")

    # stderr flood to exercise reactor's drain (small — enough to verify drain works)
    for i in range(50):
        print(f"fake-appserver stderr line {i}", file=sys.stderr, flush=True)

    # Approval-reply tracking for with_approval script
    _approval_events: dict = {}  # req_id -> threading.Event
    _approval_results: dict = {}  # req_id -> received reply dict
    _approval_threads: list = []  # keep refs to in-flight handler threads

    def _approval_waiter(req_id, timeout=5.0):
        ev = threading.Event()
        _approval_events[req_id] = ev
        return ev.wait(timeout=timeout)

    def _dispatch_approval_reply(msg):
        mid = msg.get("id")
        if mid and mid in _approval_events:
            _approval_results[mid] = msg
            _approval_events[mid].set()

    initialized = False
    thread_started = False

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        # Could be an approval reply (has id + result, no method)
        if mid and not method and ("result" in msg or "error" in msg):
            _dispatch_approval_reply(msg)
            continue

        if method == "initialize":
            _handle_initialize(mid)
            initialized = True

        elif method == "initialized":
            # client notification — no reply needed
            pass

        elif method == "thread/start":
            if initialized:
                _handle_thread_start(mid)
                thread_started = True

        elif method == "turn/start":
            thread_id = (msg.get("params") or {}).get("threadId", "T1")
            if script == "basic":
                _handle_turn_start_basic(mid, thread_id=thread_id)
            elif script == "with_approval":
                # Run concurrently and DO NOT join here: the handler blocks waiting for
                # the client's approval reply, which THIS main loop must read from stdin
                # and dispatch (the approval-reply branch above). Joining would deadlock —
                # the loop couldn't read the reply the handler is waiting on.
                t = threading.Thread(
                    target=_handle_turn_start_with_approval,
                    args=(mid, _approval_waiter),
                    kwargs={"thread_id": thread_id},
                    daemon=True,
                )
                _approval_threads.append(t)
                t.start()

        # Unknown methods silently ignored (fake doesn't need full coverage)


if __name__ == "__main__":
    main()
