#!/usr/bin/env bash
# spike/run_spike.sh — self-contained: starts fixture + a fresh lane, runs 3 configs x10,
# checks console parity, tears everything down via trap. Idempotent (per-PID profile + own ports).
# Playwright scenarios run via the venv python ($PW); cdp.py scenarios via system python3.
set -u
PORT=9360; FIXPORT=9401
FIX="http://127.0.0.1:$FIXPORT/async-page.html"; CDP_URL="http://127.0.0.1:$PORT"
PROFILE="/tmp/spike-profile-$$"
PW="${PW:-.venv-spike/bin/python}"            # venv python with Playwright (connect_over_cdp)
FIXPID=""
cleanup() {
  pkill -f "user-data-dir=$PROFILE" 2>/dev/null
  [ -n "$FIXPID" ] && kill "$FIXPID" 2>/dev/null
  rm -rf "$PROFILE"
}
trap cleanup EXIT
# Preflight (R3-F1): refuse to run if our fixed ports are already occupied — otherwise we'd
# attach to a stale lane on $PORT (which would pass the readiness poll) or measure a foreign
# fixture server on $FIXPORT (our own bind would fail), producing invalid data.
for p in "$PORT" "$FIXPORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FATAL: port $p already in use — refusing to measure a stale/foreign endpoint; free it and retry." >&2
    exit 1
  fi
done
# fixture server (own PID)
( cd spike && exec python3 -m http.server "$FIXPORT" >/dev/null 2>&1 ) &
FIXPID=$!
# fresh lane, single tab = fixture (R1-F4)
CDP_PORT=$PORT LOOK_PROFILE_DIR="$PROFILE" LOOK_HEADLESS=1 \
  skills/look/scripts/launch.sh "$FIX" >/dev/null 2>&1 &
# wait for CDP up (poll cdp.py status), then ABORT if still down — no false 0/10 tallies (R2-F3)
for i in $(seq 1 20); do
  CDP_PORT=$PORT python3 skills/look/scripts/cdp.py status >/dev/null 2>&1 && break
  sleep 0.5
done
CDP_PORT=$PORT python3 skills/look/scripts/cdp.py status >/dev/null 2>&1 || {
  echo "FATAL: lane not ready on $PORT after readiness poll — aborting (no measurements)" >&2; exit 1; }
naive_pass=0; best_pass=0; pw_pass=0
for i in $(seq 1 10); do
  python3 spike/scenario_cdp.py naive "$PORT" "$FIX" >/dev/null 2>&1 && naive_pass=$((naive_pass+1))
  python3 spike/scenario_cdp.py best  "$PORT" "$FIX" >/dev/null 2>&1 && best_pass=$((best_pass+1))
  "$PW" spike/scenario_playwright.py "$CDP_URL" "$FIX" >/dev/null 2>&1 && pw_pass=$((pw_pass+1))
done
echo "cdp.py naive:  $naive_pass/10 passed"
echo "cdp.py best:   $best_pass/10 passed"
echo "playwright:    $pw_pass/10 passed"
echo "--- console-gate detection (MEASURED — cdp.py one-shot may MISS vs Playwright subscribe, R2-F2) ---"
python3 spike/scenario_cdp.py best "$PORT" "$FIX" --expect-console-error 2>&1 | tail -1
"$PW" spike/scenario_playwright.py "$CDP_URL" "$FIX" --expect-console-error 2>&1 | tail -1
echo "--- ergonomics ---"
echo "cdp.py scenario LOC:     $(wc -l < spike/scenario_cdp.py)"
echo "playwright scenario LOC: $(wc -l < spike/scenario_playwright.py)"
