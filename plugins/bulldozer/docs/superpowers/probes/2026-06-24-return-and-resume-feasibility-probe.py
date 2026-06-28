"""Make-or-break Probe A: does the codex app-server child survive an UNANSWERED approval
across an IDLE gap (mirroring the dispatcher idle between two MCP calls — NO pumping during
the wait), then accept a LATE decision and run the turn to completion?

GO if: child survives >= ~30s (one model round-trip), accepts the late accept (no stale-id
error), turn reaches completed, PROBE.txt exists. NO-GO if child dies / rejects the late id.

==============================================================================================
RESULT (2026-06-24, codex 0.141, run live from the main session) = **GO**
  capture 28.5s -> child alive @30/60/90s (last measured ~93s idle, same pid, NO pumping) ->
  LATE accept honored (reused id=0 after ~64s true idle) -> turn/completed 108s -> PROBE.txt written.
  => return-and-resume is FEASIBLE. The unproven assumption that gated the #277 design is closed.
  Honest bound: tested to ~93s idle (no death); real ceiling higher but unmeasured. 93s >> ~30s
  one-round-trip threshold. This is the durable, re-runnable proof (the issue's prototype_spec is
  the description; THIS is the working harness). See A3IO/jaine-plugins#277.

TODO for the Tier 2 build (deferred): Probe B (late-decline teardown) + Probe C
  (interrupt-while-pending / #218 interaction); then promote to an @pytest.mark.slow case.
Run: python3 docs/superpowers/probes/2026-06-24-return-and-resume-feasibility-probe.py
  (interactive/authenticated session only -- a nested `claude -p` cannot auth; ~3-4 min).
=============================================================================================="""
import sys, os, tempfile, time

MCP = "/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/mcp"
sys.path.insert(0, MCP)
import codex_server as srv

T0 = time.time()
def log(m): print(f"[{time.time()-T0:6.1f}s] {m}", flush=True)

m = srv.AppServerManager()
log("ensuring real codex...")
m.ensure()
child = m._child
pid = child._proc.pid
log(f"ensured pid={pid} codex_ver={getattr(srv,'LAST_VERIFIED_CODEX_VERSION','?')}")

cwd = tempfile.mkdtemp(prefix="probe-a-")
tid = m.start_thread(sandbox="read-only", approval_policy="on-request", cwd=cwd, base_instructions="")
reactor = m._reactor
mid = m._next_id()
prompt = ("Create a file named PROBE.txt containing the text OK in the current working "
          "directory. Do exactly that and nothing else.")
m._write({"id": mid, "method": "turn/start",
          "params": {"threadId": tid, "input": srv._turn_input(prompt)}})
log(f"turn/start sent (thread {tid}); pumping for the approval...")

# ---- capture the first approval request ----
req = None
cap_deadline = time.time() + 150
while time.time() < cap_deadline and req is None:
    if child.poll() is not None:
        log("CHILD DIED before approval"); sys.exit(3)
    for fr in reactor.pump(timeout=0.3):
        if not isinstance(fr, dict) or "__cc__" in fr:
            continue
        if srv.classify(fr) == "request":
            req = fr; break
        if (fr.get("params", {}) or {}).get("status") == "completed" or fr.get("method") == "turn/completed":
            log("turn completed with NO approval — trigger failed"); sys.exit(4)
if req is None:
    log("no approval captured within 150s — trigger/timeout issue");
    try: child.kill()
    except Exception: pass
    sys.exit(7)
req_id = req.get("id")
req_method = req.get("method")
log(f"CAPTURED approval id={req_id} method={req_method} cmd={ (req.get('params') or {}).get('command')!r }")
assert child.poll() is None, "child died at capture"

# ---- IDLE survival gap: do NOT pump (mirror the idle dispatcher between two MCP calls) ----
last_alive = 0.0
for mark in (30, 60, 90):
    while time.time() - T0 < mark + 2:  # sleep toward the mark (capture happened ~30s in)
        time.sleep(1.0)
        if child.poll() is not None:
            break
    alive = child.poll() is None
    log(f"  survival @~{mark}s wall: child_alive={alive} (pid {pid})")
    if alive:
        last_alive = time.time() - T0
    else:
        log(f"CHILD DIED during the idle gap (~{mark}s) — return-and-resume would lose the turn")
        break

if child.poll() is not None:
    log(f"RESULT = NO-GO (child died in the gap; last_alive≈{last_alive:.0f}s)")
    sys.exit(5)

# ---- LATE accept (the exact bridge envelope for commandExecution/fileChange) ----
if req_method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
    reply = {"id": req_id, "result": {"decision": "accept"}}
elif req_method in ("execCommandApproval", "applyPatchApproval"):
    reply = {"id": req_id, "result": {"decision": "approved"}}
else:
    reply = {"id": req_id, "result": {"decision": "accept"}}
log(f"writing LATE accept after idle gap: {reply}")
m._write(reply)

# ---- pump to completion ----
completed = False
err = None
comp_deadline = time.time() + 150
while time.time() < comp_deadline:
    if child.poll() is not None:
        log("child died after late accept (before completion)"); break
    for fr in reactor.pump(timeout=0.3):
        if not isinstance(fr, dict) or "__cc__" in fr:
            continue
        meth = fr.get("method", "")
        st = (fr.get("params", {}) or {}).get("status")
        if meth == "turn/completed" or st == "completed":
            completed = True; log(f"TURN COMPLETED: {str(fr)[:160]}"); break
        if meth == "error":
            err = str(fr)[:200]; log(f"error frame after accept: {err}")
        if srv.classify(fr) == "request":
            # a SECOND approval (e.g. codex asks again) — accept it too to reach completion
            rid = fr.get("id")
            log(f"  second approval id={rid} — accepting to reach completion")
            m._write({"id": rid, "result": {"decision": "accept"}})
    if completed:
        break

probe_txt = os.path.join(cwd, "PROBE.txt")
file_ok = os.path.isfile(probe_txt) and "OK" in open(probe_txt).read()
log(f"PROBE.txt exists+OK = {file_ok}  (path {probe_txt})")
log(f"child still same pid alive = {child.poll() is None and child._proc.pid == pid}")

try:
    child.kill()
except Exception:
    pass

if completed and file_ok and not err:
    log(f"RESULT = GO ✅ — child survived the idle gap (last_alive≈{last_alive:.0f}s), accepted the "
        f"LATE decision, turn completed, file written. RETURN-AND-RESUME IS FEASIBLE on codex 0.141.")
    sys.exit(0)
else:
    log(f"RESULT = PARTIAL/NO-GO — completed={completed} file_ok={file_ok} err={err}")
    sys.exit(6)
