#!/usr/bin/env python3
"""GATING probe (plan Task 1): prove `thread/resume` round-trips ACROSS a process
restart, so cross-session resume is buildable.

Extends the shipped approval PoC (`appserver_probe.py`, which proved the #18268
approval fix). Here we prove the OTHER pillar: a thread started by one
`codex app-server` child can be resumed by a DIFFERENT child and the model still
recalls context planted in the first turn.

Flow:
  child A: initialize -> initialized -> thread/start (NON-ephemeral) ->
           turn/start "remember codeword X" -> wait turn/completed -> kill A
  child B: initialize -> initialized -> thread/resume {threadId} ->
           turn/start "what was the codeword?" -> assert X in answer

Both children share the default CODEX_HOME (~/.codex), so child A's rollout file
persists on disk for child B to load by thread_id — exactly the cross-session
case (new CC session -> new server -> new app-server child). Isolation in the
real server is via baseInstructions+config, NOT a relocated CODEX_HOME, so
rollouts persist and by-id resume works cross-session.

Run: python3 mcp/appserver_resume_probe.py            # by-id (default)
     python3 mcp/appserver_resume_probe.py --by-path  # UNSTABLE fallback
Expect: "PERSISTED ACROSS RESTART: True" and gate decision.

GATE RESULT (verified 2026-06-19, codex 0.141.0): both mechanisms recall the
codeword across a process restart.
  - by-id   (PREFERRED, stable, schema-exposed): {"threadId": <id>} -> recalled ✓
  - by-path (UNSTABLE fallback, path computed from thread_id): recalled ✓
DECISION: cross-session resume is BUILDABLE via by-id. Task 3's
AppServerManager.resume_thread uses method='by-id'. No descope (plan Step 5).

Wire format verified vs codex 0.141 schema
(/0/SANDBOX/CODEX-SETTINGS/codex-schema-0.141-experimental/v2/):
  - UserInput text variant: {"type":"text","text":..., "text_elements":[]} (snake_case)
  - ThreadResumeParams: threadId primary; path/history UNSTABLE (history=cloud DO-NOT-USE)
  - thread/start sandbox = SandboxMode string ("read-only")
"""
import glob
import json
import os
import select
import subprocess
import sys
import time

CODEX = os.environ.get("JAINE_CODEX_BIN", "/opt/homebrew/bin/codex")
SCRATCH = "/tmp/codex-resume-probe-scratch"
CODEWORD = "BANANA-4242-ZEBRA"
DEADLINE_PER_TURN = 180  # seconds


def _rollout_path(thread_id):
    """Compute the on-disk rollout path from a thread_id (the by-path key).

    codex persists rollouts at $CODEX_HOME/sessions/YYYY/MM/DD/
    rollout-<ts>-<thread_id>.jsonl — so the path is derivable from thread_id
    alone (the runtime manager only holds thread_id). Returns the path or None.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    hits = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*-{thread_id}.jsonl"),
                     recursive=True)
    return hits[0] if hits else None


def resume_thread(client, thread_id, method="by-id"):
    """Verified `thread/resume` request (the shape Task 3's manager wraps).

    method='by-id'  -> {"threadId": id}                 (STABLE, schema-exposed, PREFERRED)
    method='by-path'-> {"threadId": id, "path": <rollout>} (UNSTABLE wire key; path
                       computed from thread_id; empirically verified, not in pinned schema)

    Returns the ThreadResumeResponse result dict. Raises on error/timeout.
    """
    if method == "by-id":
        params = {"threadId": thread_id}
    elif method == "by-path":
        path = _rollout_path(thread_id)
        if not path:
            raise RuntimeError(f"by-path: no rollout file found for {thread_id}")
        params = {"threadId": thread_id, "path": path}
    else:
        raise ValueError(f"unknown resume method: {method!r}")
    rid = client.req("thread/resume", params)
    rr, _ = client.pump(lambda m: m.get("id") == rid and ("result" in m or "error" in m))
    if not rr or "error" in (rr or {}):
        raise RuntimeError(f"thread/resume ({method}) failed: {rr}")
    return rr["result"]


class Client:
    """One `codex app-server` child + jsonrpc_lite helpers (snapshot of the
    working PoC's send/req/reply/pump, scoped to a single child)."""

    def __init__(self, label):
        self.label = label
        self.proc = subprocess.Popen(
            [CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        print(f"[{self.label}] >>> {obj.get('method', obj.get('id'))}", flush=True)

    def req(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return self._id

    def reply(self, rid, result):
        self.send({"id": rid, "result": result})

    def _readline_to(self, timeout):
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        return (self.proc.stdout.readline() or "").strip() or None

    def pump(self, until, deadline=DEADLINE_PER_TURN, collect_text=False):
        """Read+dispatch until until(msg) truthy or deadline. Auto-replies to any
        server->client request (decline approvals; this probe needs none).
        Returns (matching_msg, accumulated_assistant_text)."""
        start = time.monotonic()
        text = []
        while True:
            if time.monotonic() - start > deadline:
                print(f"[{self.label}] !!! DEADLINE", flush=True)
                return None, "".join(text)
            line = self._readline_to(2)
            if line is None:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"[{self.label}]  (non-json):", line[:120], flush=True)
                continue
            method = msg.get("method")
            if method and "id" in msg:  # server->client REQUEST: must reply
                print(f"[{self.label}] <<< SERVER-REQ {method} (id={msg['id']})", flush=True)
                self.reply(msg["id"], {"decision": "decline"})
                continue
            if method:  # notification
                p = msg.get("params") or {}
                if method == "item/agentMessage/delta":
                    d = p.get("delta", "")
                    if collect_text:
                        text.append(d)
                    sys.stdout.write(d)
                    sys.stdout.flush()
                elif method in ("turn/started", "turn/completed", "item/started",
                                "item/completed", "thread/started", "error"):
                    extra = p.get("status") or (p.get("item") or {}).get("type") or ""
                    print(f"[{self.label}]  ~ {method} {extra}", flush=True)
                if until(msg):
                    return msg, "".join(text)
                continue
            if "id" in msg:  # response
                if "error" in msg:
                    print(f"[{self.label}] <<< ERROR id={msg['id']}: {msg['error']}", flush=True)
                else:
                    print(f"[{self.label}] <<< OK id={msg['id']}", flush=True)
                if until(msg):
                    return msg, "".join(text)

    def initialize(self):
        # ClientInfo.title is a REQUIRED nullable key (verify null is accepted —
        # the real manager sends title:None).
        rid = self.req("initialize", {
            "clientInfo": {"name": "jaine_resume_probe", "title": None, "version": "0.1"},
            "capabilities": {"experimentalApi": True}})
        init, _ = self.pump(lambda m: m.get("id") == rid and ("result" in m or "error" in m))
        if not init or "error" in (init or {}):
            print(f"[{self.label}] initialize FAILED — stderr:",
                  self.proc.stderr.read()[-800:])
            return False
        self.send({"method": "initialized"})
        return True

    def turn(self, tid, prompt, collect_text=True):
        self.req("turn/start", {
            "threadId": tid,
            "input": [{"type": "text", "text": prompt, "text_elements": []}]})
        # turn/start returns a TurnStartResponse first; then the stream; final
        # text arrives via item/agentMessage/delta (NOT turn/completed).
        _, text = self.pump(lambda m: m.get("method") == "turn/completed", collect_text=collect_text)
        return text

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()

    def kill(self):
        self.proc.kill()
        self.proc.wait()


def round_trip(method="by-id"):
    os.makedirs(SCRATCH, exist_ok=True)

    # --- child A: start a thread, plant the codeword ---
    a = Client("A")
    if not a.initialize():
        return None, False
    rid = a.req("thread/start", {"cwd": SCRATCH, "approvalPolicy": "never",
                                 "sandbox": "read-only"})
    ts, _ = a.pump(lambda m: m.get("id") == rid and ("result" in m or "error" in m))
    if not ts or "error" in (ts or {}):
        print("thread/start FAILED:", ts, "\nstderr:", a.proc.stderr.read()[-800:])
        a.close()
        return None, False
    thread = ts["result"].get("thread") or {}
    tid = thread.get("id") or thread.get("threadId")
    print("\n  threadId:", tid, flush=True)

    print("\n--- child A: planting codeword ---", flush=True)
    a.turn(tid, f"Remember this codeword for later: {CODEWORD}. "
                f"Reply with only the word ACKNOWLEDGED.", collect_text=False)
    a.kill()  # simulate process restart / new session
    print("\n  child A killed (simulating new session)\n", flush=True)
    time.sleep(1)

    # --- child B: resume by thread_id, ask for the codeword ---
    b = Client("B")
    if not b.initialize():
        return tid, False
    try:
        result = resume_thread(b, tid, method=method)
    except RuntimeError as e:
        print(f"thread/resume ({method}) FAILED:", e,
              "\nstderr:", b.proc.stderr.read()[-800:])
        b.close()
        return tid, False
    print(f"  thread/resume ({method}) OK:", json.dumps(result)[:160], flush=True)

    print("\n--- child B: asking for codeword ---", flush=True)
    answer = b.turn(tid, "What exact codeword did I ask you to remember earlier? "
                         "Reply with only that codeword.", collect_text=True)
    b.close()
    recalled = CODEWORD in (answer or "")
    print(f"\n\n  child B answer: {answer!r}", flush=True)
    return tid, recalled


def main():
    method = "by-path" if "--by-path" in sys.argv else "by-id"
    print(f"=== GATING PROBE: thread/resume cross-restart ({method}) ===\n", flush=True)
    tid, recalled = round_trip(method=method)
    print("\n" + "=" * 60, flush=True)
    print(f"thread_id: {tid}", flush=True)
    print(f"method: {method}", flush=True)
    print(f"PERSISTED ACROSS RESTART: {recalled}", flush=True)
    if recalled:
        print(f"\nGATE DECISION: cross-session resume is buildable via {method}.", flush=True)
        print(f"Mechanism for AppServerManager.resume_thread: method='{method}'.", flush=True)
        sys.exit(0)
    else:
        print(f"\nGATE: {method} did NOT recall. Investigate (see plan Task 1 Step 2/4) "
              "or descope cross-session (Step 5).", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
