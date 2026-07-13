#!/usr/bin/env python3
"""Scripted fake worker for facade tests (#344, spec §5 offline unit).

Speaks the same NDJSON JSON-RPC the real codex_server.py speaks to CC.
Behavior is scripted per-call via the tools/call arguments' `_fake` dict:

    sleep:  float — hold the call open this long (cancellable)
    elicit: dict  — send a server→client `elicitation/create` with params=dict,
                    WAIT for the reply, embed it as result["elicit_reply"]
    die:    true  — exit(1) without replying (crash simulation)
    park:   {token} — reply {"status":"awaiting_approval","park_token":token}
                    and remember the token (park simulation, #277)
    result: dict  — merged into the reply payload

A `codex_approve` tools/call resumes: replies {"status":"completed",
"resumed": <token was parked HERE>, "pid": ...} — the pid proves the facade
routed the approve to the SAME worker process that parked.

Every instance starts its server→client request ids at 1000 — DELIBERATE:
two fake workers are guaranteed to collide, so the facade's id remap is
load-bearing in every multi-worker elicitation test.

On stdin EOF: if $FAKE_WORKER_EOF_MARKER is set, writes "eof-clean" there
(proves the facade's graceful close-stdin-first teardown reached us), exit 0.
"""

import json
import os
import queue
import sys
import threading
import time

_wlock = threading.Lock()
_idlock = threading.Lock()
_next_srv_id = 1000                 # SAME in every instance → collision by construction
_pending = {}                       # srv request id -> Queue for the reply
_inflight = {}                      # tools/call id -> cancel Event
_parked = set()                     # park tokens parked in THIS process
_park_origin = {}                   # cc call id that parked -> token


def _write(frame):
    with _wlock:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()


def _reply(mid, result):
    _write({"jsonrpc": "2.0", "id": mid, "result": result})


def _tool_reply(mid, payload, is_error=False):
    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        result["isError"] = True
    _reply(mid, result)


def _handle_call(mid, params):
    args = params.get("arguments") or {}
    fake = args.get("_fake") or {}
    cancel = _inflight[mid]
    payload = {"ok": True, "pid": os.getpid(), "tool": params.get("name")}
    payload.update(fake.get("result") or {})

    if params.get("name") == "codex_approve":
        token = args.get("park_token")
        deadline = time.monotonic() + float(fake.get("sleep") or 0)
        while time.monotonic() < deadline and not cancel.is_set():
            time.sleep(0.01)   # the resumed turn takes time too (death window)
        scripted = fake.get("result") or {}
        if "error" in scripted:
            # The real engine validates park_token / decision_id BEFORE gen.send:
            # a bad decision_id is RETRYABLE and leaves the park INTACT.
            if "expired" not in scripted["error"]:
                pass                      # park kept
            else:
                _parked.discard(token)
            _tool_reply(mid, dict(scripted, pid=os.getpid()), is_error=True)
            _inflight.pop(mid, None)
            return
        was_parked = token in _parked
        _parked.discard(token)
        for k in [k for k, t in _park_origin.items() if t == token]:
            _park_origin.pop(k, None)
        if fake.get("park") is not None:
            # multi-approval: the resumed turn parks AGAIN under a new token
            new_token = fake["park"]["token"]
            _parked.add(new_token)
            _park_origin[mid] = new_token   # re-park rebinds to THIS approve call
            payload = {"status": "awaiting_approval", "park_token": new_token,
                       "resumed": was_parked, "pid": os.getpid()}
        else:
            payload = {"status": "completed", "resumed": was_parked,
                       "pid": os.getpid()}
        _tool_reply(mid, payload)
        _inflight.pop(mid, None)
        return

    if params.get("name") == "codex_info" and _parked:
        # Mirror the engine: while a turn is PARKED every tool except
        # codex_approve is busy-blocked with this DISTINCT message (#277). The
        # facade's liveness probe relies on exactly this.
        _tool_reply(mid, {"error": "codex turn parked — resume with codex_approve"},
                    is_error=True)
        _inflight.pop(mid, None)
        return

    if fake.get("elicit") is not None:
        global _next_srv_id
        with _idlock:
            eid = _next_srv_id
            _next_srv_id += 1
        q = queue.Queue()
        _pending[eid] = q
        _write({"jsonrpc": "2.0", "id": eid, "method": "elicitation/create",
                "params": fake["elicit"]})
        try:
            payload["elicit_reply"] = q.get(timeout=10)
        except queue.Empty:
            payload["elicit_reply"] = "TIMEOUT"

    payload["t_start"] = time.time()   # wall clock: comparable ACROSS processes
    deadline = time.monotonic() + float(fake.get("sleep") or 0)
    while time.monotonic() < deadline and not cancel.is_set():
        time.sleep(0.01)
    payload["t_end"] = time.time()

    if cancel.is_set():
        _tool_reply(mid, {"status": "interrupted", "interrupted_by": "cancel",
                          "pid": os.getpid()})
        _inflight.pop(mid, None)
        return
    if fake.get("die"):
        os._exit(1)
    if fake.get("park") is not None:
        token = fake["park"]["token"]
        _parked.add(token)
        _park_origin[mid] = token      # the engine binds the park to THIS call
        payload = {"status": "awaiting_approval", "park_token": token,
                   "pid": os.getpid()}
    _tool_reply(mid, payload)
    _inflight.pop(mid, None)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = frame.get("method")
        mid = frame.get("id")
        params = frame.get("params") or {}
        if method == "initialize":
            _reply(mid, {"protocolVersion": params.get("protocolVersion", "1"),
                         "serverInfo": {"name": "fake-worker", "version": "0"},
                         "capabilities": {"tools": {}},
                         "instructions": "fake"})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _reply(mid, {"tools": [{"name": "codex_run"}]})
        elif method == "tools/call":
            ev = threading.Event()
            _inflight[mid] = ev
            threading.Thread(target=_handle_call, args=(mid, params),
                             daemon=True).start()
        elif method == "notifications/cancelled":
            rid = params.get("requestId")
            ev = _inflight.get(rid)
            if ev is not None:
                ev.set()
            token = _park_origin.pop(rid, None)
            if token is not None:
                _parked.discard(token)   # the engine tears its park down
        elif method == "__fake_emit_raw__":
            # Emit a syntactically valid but NON-OBJECT JSON line (e.g. `[]`) —
            # the facade's worker reader must survive it.
            with _wlock:
                sys.stdout.write(json.dumps(params.get("value")) + "\n")
                sys.stdout.flush()
        elif method == "__fake_drop_park__":
            # Simulate the engine ending a park with NO MCP frame (inner-child
            # death / cap): the park is simply gone from the worker's state.
            _parked.clear()
            _park_origin.clear()
        elif method is None and mid is not None:
            q = _pending.pop(mid, None)
            if q is not None:
                q.put(frame.get("result"))
    marker = os.environ.get("FAKE_WORKER_EOF_MARKER")
    if marker:
        with open(marker, "w") as f:
            f.write("eof-clean")


if __name__ == "__main__":
    main()
