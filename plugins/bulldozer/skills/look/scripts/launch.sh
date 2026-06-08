#!/usr/bin/env bash
# JAINE Browser — dedicated Chrome instance with CDP + AppleScript
# Separate profile, no extensions, dark theme, remote debugging
# Usage: jaine-browser [URL]   (LOOK_DRY_RUN=1 prints resolved config + argv, no launch)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Backslash-escape ERE metacharacters so an arbitrary profile path matches
# literally in pkill -f (A.5). Realistic path metachars: . [ ] ( ) { } ^ $ * + ? |
# (a backslash/newline path is rejected fail-loud at profile resolution — see guard).
_escape_ere() {
  printf '%s' "$1" | sed -E 's/([].[(){}^$*+?|])/\\\1/g'
}

# Timeout-guarded osascript (hole J): a `tell application "<name>"` with an unknown
# name raises the LaunchServices "Where is <name>?" picker and hangs osascript
# forever. launch.sh AppleScript is pid-targeted via System Events (no app
# resolution) AND every call is killed after a hard cap. python3 is already a
# launch.sh dependency (profile canonicalization, Local State patch).
_osascript_to() {  # _osascript_to <seconds> <applescript>
  python3 - "$1" "$2" <<'PY'
import subprocess, sys
try:
    subprocess.run(["osascript", "-e", sys.argv[2]],
                   capture_output=True, timeout=float(sys.argv[1]))
except (subprocess.TimeoutExpired, OSError):
    pass
PY
}

# Canonical "does this profile resolve to the daily profile?" check — shared by the
# insecure gate (R1-F1) and the automation gate (R1-C). Echoes 1/0; fail-CLOSED:
# canonicalization error → 1 (treated AS the daily profile).
_resolves_to_daily_profile() {
  python3 - "$1" <<'PY' 2>/dev/null || echo 1
import os, sys
print(1 if os.path.realpath(sys.argv[1]) == os.path.realpath("/0/.jaine/.browser/profile") else 0)
PY
}

# ── Config ──
CDP_PORT="${CDP_PORT:-9333}"

# Port must be an integer in 1..65535. The {1,5} digit bound rejects non-numeric AND
# over-long input BEFORE arithmetic: a huge numeric string would otherwise overflow
# bash's 64-bit (( )) via 10# and wrap into 1..65535 (R2-F1). 10# then canonicalizes
# (a leading-zero "08" would hit bash's octal trap AND diverge from cdp.py int("08")=8).
if ! [[ "$CDP_PORT" =~ ^[0-9]{1,5}$ ]]; then
  echo "ERROR: CDP_PORT must be an integer in 1..65535 (got: $CDP_PORT)" >&2
  exit 1
fi
CDP_PORT=$((10#$CDP_PORT))
# 0 = SP4 ephemeral lane (OS-assigned port) — gated to --automation below.
if (( CDP_PORT < 0 || CDP_PORT > 65535 )); then
  echo "ERROR: CDP_PORT must be an integer in 1..65535, or 0 for the ephemeral automation lane (got: $CDP_PORT)" >&2
  exit 1
fi

# Profile: LOOK_PROFILE_DIR verbatim, else derive from port
if [[ -n "${LOOK_PROFILE_DIR:-}" ]]; then
  PROFILE_DIR="$LOOK_PROFILE_DIR"
  PROFILE_OVERRIDDEN=1
elif [[ "$CDP_PORT" == "9333" ]]; then
  PROFILE_DIR="/0/.jaine/.browser/profile"
  PROFILE_OVERRIDDEN=0
else
  PROFILE_DIR="/0/.jaine/.browser/profile-${CDP_PORT}"
  PROFILE_OVERRIDDEN=0
fi

# A profile path with a backslash or newline can't be safely ERE-escaped for the
# lane-scoped pkill (A.5: _escape_ere handles . [ ] ( ) { } ^ $ * + ? | but not \).
# Fail loud rather than silently garble the kill pattern (no-silent-fallback principle).
if [[ "$PROFILE_DIR" == *\\* || "$PROFILE_DIR" == *$'\n'* ]]; then
  echo "ERROR: LOOK_PROFILE_DIR must not contain a backslash or newline (got: $PROFILE_DIR)" >&2
  exit 1
fi

WINDOW_WIDTH=1440
WINDOW_HEIGHT=900

# Window position (headful only): derived from port, normalized non-negative + capped.
# bash % keeps the dividend's sign, so a port below 9333 would go negative without
# the ((x % CAP)+CAP)%CAP normalization.
if [[ "$CDP_PORT" == "9333" ]]; then
  WINDOW_X=100
  WINDOW_Y=100
else
  _off=$(( (CDP_PORT - 9333) * 40 ))
  _norm=$(( ((_off % 1200) + 1200) % 1200 ))
  WINDOW_X=$(( 100 + _norm ))
  WINDOW_Y=$(( 100 + _norm ))
fi
WINDOW_POSITION="${WINDOW_X},${WINDOW_Y}"
# Chrome binary: single change-point shared with tests/conftest.py (CHROME const).
# DEFAULTED flag lets the automation lane (SP1) swap in the CfT default without
# overriding an explicit env CHROME_BIN.
CHROME_BIN_DEFAULTED=0
if [[ -z "${CHROME_BIN:-}" ]]; then
  CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  CHROME_BIN_DEFAULTED=1
fi

# AppleScript app/process name (SP1, #164): stock Chrome by default; the automation
# lane defaults it to "Google Chrome for Testing". Consumed by cdp.py (env) for its
# AppleScript JS channel; launch.sh itself targets the GUI by pid, not by name. A
# double quote or backslash would break AppleScript-string/bash quoting downstream —
# fail loud (same principle as the PROFILE_DIR backslash guard above).
CHROME_APP_NAME_DEFAULTED=0
if [[ -z "${CHROME_APP_NAME:-}" ]]; then
  CHROME_APP_NAME="Google Chrome"
  CHROME_APP_NAME_DEFAULTED=1
fi
if [[ "$CHROME_APP_NAME" == *'"'* || "$CHROME_APP_NAME" == *\\* || "$CHROME_APP_NAME" == *$'\n'* ]]; then
  echo "ERROR: CHROME_APP_NAME must not contain a double quote, backslash or newline (got: $CHROME_APP_NAME)" >&2
  exit 1
fi

# Pinned Chrome for Testing (the automation-lane default binary; update-cft.sh is
# the only mover of `current`).
CFT_BIN="/0/.jaine/.browser/cft/current/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"

# chrome.log: 9333 → global path (unchanged); LOOK_PROFILE_DIR override → inside the
# (temp) profile so the fixture's rmtree removes it; else chrome-<port>.log next to
# the derived per-port profile.
if (( PROFILE_OVERRIDDEN )); then
  LOG="$PROFILE_DIR/chrome.log"
elif [[ "$CDP_PORT" == "9333" ]]; then
  LOG="/0/.jaine/.browser/chrome.log"
else
  LOG="$(dirname "$PROFILE_DIR")/chrome-${CDP_PORT}.log"
fi

# ── Argument parsing ──
# Recognize --headless/--headful (NOT --insecure — that flag is D's; until
# sub-project D it is an unknown flag → fail-loud). A `--` terminator forces the
# next token to be the URL (lets a URL legitimately starting with -- through).
# First non-flag token is the URL; unknown --flag → fail-loud (no silent fallback).
HEADLESS_ARG=""    # "", "0" (headful) or "1" (headless)
INSECURE_ARG=0
AUTOMATION_ARG=0
CERT_SPKI_ARG=""
URL=""
URL_SET=0
SAW_TERMINATOR=0
for a in "$@"; do
  if (( SAW_TERMINATOR )); then
    if (( ! URL_SET )); then URL="$a"; URL_SET=1; fi
    continue
  fi
  case "$a" in
    --)        SAW_TERMINATOR=1 ;;
    --headless) HEADLESS_ARG=1 ;;
    --headful)  HEADLESS_ARG=0 ;;
    --insecure) INSECURE_ARG=1 ;;
    --automation) AUTOMATION_ARG=1 ;;
    --cert-spki=*) CERT_SPKI_ARG="${a#--cert-spki=}" ;;
    --*)
      echo "ERROR: unknown flag '$a' (look launcher accepts --headless/--headful/--insecure/--automation/--cert-spki=<PIN>)" >&2
      exit 1
      ;;
    *) if (( ! URL_SET )); then URL="$a"; URL_SET=1; fi ;;
  esac
done
if (( ! URL_SET )); then URL="about:blank"; fi

# #60: normalize a bare absolute path to a file:// URL. Delegate to
# `cdp.py normalize-url` — the SINGLE source of truth. Cheap-skip non-slash URLs.
if [[ "$URL" == /* ]]; then
  normalized=$(python3 "$SCRIPT_DIR/cdp.py" normalize-url "$URL" 2>/dev/null)
  if [[ -n "$normalized" ]]; then
    URL="$normalized"
  fi
fi

# ── Headless placeholder — resolved in Task 6 from --headless/--headful + LOOK_HEADLESS.
#    MUST sit AFTER argument parsing so HEADLESS_ARG (set by the Task 5 parser) exists. ──
# Headless: arg wins over env; else LOOK_HEADLESS truthy (1/true/yes, case-insensitive).
shopt -s nocasematch
if [[ "${LOOK_HEADLESS:-}" =~ ^(1|true|yes)$ ]]; then
  _env_headless=1
else
  _env_headless=0
fi
shopt -u nocasematch
if [[ -n "$HEADLESS_ARG" ]]; then
  HEADLESS="$HEADLESS_ARG"
else
  HEADLESS="$_env_headless"
fi

# ── Ephemeral lane (SP4): CDP_PORT=0 → OS-assigned port + launcher-owned mktemp
#    profile (unique by construction — the profile IS the ownership token; holes
#    R1-H/R2-R closed structurally). Supported ONLY under --automation (fail-loud
#    edges, spec §2.1): a caller-supplied LOOK_PROFILE_DIR would break the
#    uniqueness invariant (two subagents sharing a dir would pkill each other). ──
EPHEMERAL=0
if (( CDP_PORT == 0 )); then
  if (( PROFILE_OVERRIDDEN )); then
    echo "ERROR: CDP_PORT=0 (ephemeral lane) with a caller-supplied LOOK_PROFILE_DIR breaks" >&2
    echo "       the uniqueness invariant — the launcher owns the ephemeral profile." >&2
    echo "       Unset LOOK_PROFILE_DIR or pick a fixed port. Refusing." >&2
    exit 1
  fi
  EPHEMERAL=1
fi

# ── Automation lane (SP1, #164): opt-in --automation / LOOK_AUTOMATION. CfT default
#    binary/app-name + --enable-automation + keychain isolation. Gate is fail-closed:
#    non-9333 port AND non-daily profile (R1-C: port alone is NOT sufficient, #160).
#    --enable-automation must NEVER reach the daily browser: it sets
#    navigator.webdriver, suppresses password-save UI, disables auto-reload.
#    Runs BEFORE the insecure gate so automation+insecure composes (the auto-temp
#    profile below satisfies insecure's explicit-isolated-profile requirement). ──
shopt -s nocasematch
if [[ "${LOOK_AUTOMATION:-}" =~ ^(1|true|yes)$ ]]; then
  _env_automation=1
else
  _env_automation=0
fi
shopt -u nocasematch
if (( AUTOMATION_ARG )) || (( _env_automation )); then
  AUTOMATION_REQUESTED=1
else
  AUTOMATION_REQUESTED=0
fi

if (( EPHEMERAL )) && (( ! AUTOMATION_REQUESTED )); then
  echo "ERROR: CDP_PORT=0 (ephemeral lane) is supported ONLY with --automation /" >&2
  echo "       LOOK_AUTOMATION — there is no /look use case for an ephemeral port." >&2
  exit 1
fi

AUTOMATION=0
if (( AUTOMATION_REQUESTED )); then
  if (( CDP_PORT == 9333 )); then
    echo "ERROR: --automation / LOOK_AUTOMATION is forbidden on the daily 9333 lane" >&2
    echo "       (automation flags must never reach the user's daily browser). Use a" >&2
    echo "       non-9333 CDP_PORT." >&2
    exit 1
  fi
  if (( ! PROFILE_OVERRIDDEN )); then
    if (( EPHEMERAL )); then
      # SP4 §2.1: the deterministic jaine-drive-${CDP_PORT} rule would collide as
      # jaine-drive-0 for EVERY ephemeral lane — mktemp is unique by construction.
      # The unique profile IS the ownership token (holes R1-H/R2-R).
      PROFILE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jaine-drive-eph-XXXXXX") || {
        echo "ERROR: mktemp failed for the ephemeral profile" >&2; exit 1; }
    else
      # Hole E (R1-E): drive lanes get a TEMP profile — deterministic per port so the
      # lane's pkill-by-profile restart contract still holds — not a persistent
      # profile-<port> accumulating under /0/.jaine. macOS cleans TMPDIR on reboot.
      PROFILE_DIR="${TMPDIR:-/tmp}/jaine-drive-${CDP_PORT}"
    fi
    # Mirror the top-of-file PROFILE_DIR guard: this assignment happens AFTER it
    # ran, and a backslash/newline (via exotic TMPDIR) can't be ERE-escaped for
    # the lane-scoped pkill (A.5).
    if [[ "$PROFILE_DIR" == *\\* || "$PROFILE_DIR" == *$'\n'* ]]; then
      echo "ERROR: TMPDIR-derived drive profile must not contain a backslash or newline (got: $PROFILE_DIR)" >&2
      exit 1
    fi
    PROFILE_OVERRIDDEN=1
    LOG="$PROFILE_DIR/chrome.log"   # mirror the PROFILE_OVERRIDDEN LOG rule above
  elif [[ "$(_resolves_to_daily_profile "$PROFILE_DIR")" != "0" ]]; then
    echo "ERROR: --automation profile resolves to the daily profile ($PROFILE_DIR). Refusing." >&2
    exit 1
  fi
  AUTOMATION=1
  if (( CHROME_BIN_DEFAULTED )); then
    CHROME_BIN="$CFT_BIN"
  fi
  if (( CHROME_APP_NAME_DEFAULTED )); then
    CHROME_APP_NAME="Google Chrome for Testing"
  fi
fi

# ── Web-security relax (D, #93): opt-in --insecure / LOOK_INSECURE, gated to a
#    provably-isolated lane. The D.1 spike confirmed --disable-web-security unblocks a
#    file:// page fetching http://<LAN>; it needs a non-default --user-data-dir, which
#    the explicit-LOOK_PROFILE_DIR requirement guarantees. NEVER the daily 9333 browser
#    or its profile. This gate runs BEFORE the dry-run/real fork → one code path. ──
shopt -s nocasematch
if [[ "${LOOK_INSECURE:-}" =~ ^(1|true|yes)$ ]]; then
  _env_insecure=1
else
  _env_insecure=0
fi
shopt -u nocasematch
if (( INSECURE_ARG )) || (( _env_insecure )); then
  INSECURE_REQUESTED=1
else
  INSECURE_REQUESTED=0
fi

INSECURE=0
if (( INSECURE_REQUESTED )); then
  # Reject any profile that RESOLVES to the daily browser's profile — not just the exact
  # string: a trailing slash, "//", "/./", ".." or a symlink would otherwise alias
  # /0/.jaine/.browser/profile and relax the daily browser's data (R1-F1). Shared
  # canonical check (SP1): _resolves_to_daily_profile — fail-closed on canonicalization
  # error (treated AS the daily profile).
  _profile_is_daily=$(_resolves_to_daily_profile "$PROFILE_DIR")
  # The daily-profile arm below is a STRING compare (!= "0"), not (( )) arithmetic:
  # (( )) on a malformed capture (e.g. multiline) errors to FALSE — the last || arm
  # would then fail OPEN (permit). The string form rejects anything but exactly "0".
  if (( CDP_PORT == 9333 )) || (( ! PROFILE_OVERRIDDEN )) || [[ "$_profile_is_daily" != "0" ]]; then
    echo "ERROR: --insecure / LOOK_INSECURE relaxes web security and is allowed ONLY on a" >&2
    echo "       provably-isolated lane: a non-9333 CDP_PORT AND an explicit non-default" >&2
    echo "       LOOK_PROFILE_DIR that does not resolve to the daily profile" >&2
    echo "       (got port=$CDP_PORT profile=$PROFILE_DIR). Refusing." >&2
    exit 1
  fi
  INSECURE=1
  echo "WARNING: web security RELAXED for this lane (--disable-web-security) — isolated" >&2
  echo "         trusted-LAN testing ONLY; never load untrusted content in this browser." >&2
fi

# ── Cert-pin lane (drive dogfood #1): opt-in --cert-spki=<PIN> / LOOK_CERT_SPKI.
#    Appends --ignore-certificate-errors-spki-list=<PIN>: Chrome skips cert errors ONLY
#    for certs whose SPKI SHA-256 matches a listed pin (self-signed LAN targets) — all
#    other TLS stays strict (this is NOT the blanket --ignore-certificate-errors).
#    Same fail-closed gate as --insecure: non-9333 port AND a provably-isolated profile;
#    --automation's auto temp profile satisfies it (runs earlier, sets PROFILE_OVERRIDDEN),
#    so automation+cert-spki composes — the standard drive-lane recipe. A malformed pin
#    would surface as a SILENT interstitial at navigate time — validate fail-loud here.
#    Arg wins over env (same precedence as --headless vs LOOK_HEADLESS). ──
CERT_SPKI=""
if [[ -n "$CERT_SPKI_ARG" ]]; then
  CERT_SPKI_REQUESTED="$CERT_SPKI_ARG"
else
  CERT_SPKI_REQUESTED="${LOOK_CERT_SPKI:-}"
fi
if [[ -n "$CERT_SPKI_REQUESTED" ]]; then
  # Each comma-separated element = base64 SHA-256 of the cert's SPKI (43 chars + '=').
  # Pattern kept in a variable: [[ =~ ]] with an unquoted var is the bash-3.2-safe ERE form.
  _spki_re='^[A-Za-z0-9+/]{43}=(,[A-Za-z0-9+/]{43}=)*$'
  if ! [[ "$CERT_SPKI_REQUESTED" =~ $_spki_re ]]; then
    echo "ERROR: --cert-spki / LOOK_CERT_SPKI must be a comma-separated list of base64" >&2
    echo "       SHA-256 SPKI fingerprints (43 base64 chars + '='). Compute one with:" >&2
    echo "       openssl s_client -connect HOST:PORT </dev/null | openssl x509 -pubkey -noout |" >&2
    echo "       openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64" >&2
    echo "       (got: $CERT_SPKI_REQUESTED)" >&2
    exit 1
  fi
  _profile_is_daily_cert=$(_resolves_to_daily_profile "$PROFILE_DIR")
  # String compare (!= "0"), not (( )) — same fail-CLOSED rationale as the insecure gate.
  if (( CDP_PORT == 9333 )) || (( ! PROFILE_OVERRIDDEN )) || [[ "$_profile_is_daily_cert" != "0" ]]; then
    echo "ERROR: --cert-spki / LOOK_CERT_SPKI relaxes TLS for the pinned cert(s) and is" >&2
    echo "       allowed ONLY on a provably-isolated lane: a non-9333 CDP_PORT AND an" >&2
    echo "       explicit non-default LOOK_PROFILE_DIR (or --automation's temp profile)" >&2
    echo "       that does not resolve to the daily profile" >&2
    echo "       (got port=$CDP_PORT profile=$PROFILE_DIR). Refusing." >&2
    exit 1
  fi
  CERT_SPKI="$CERT_SPKI_REQUESTED"
  echo "WARNING: cert errors IGNORED for the pinned SPKI fingerprint(s) on this lane" >&2
  echo "         (--ignore-certificate-errors-spki-list) — isolated lanes only; all" >&2
  echo "         other TLS validation stays strict." >&2
fi

# ── pkill match: anchored to an arg boundary + regex-escaped so the default
#    `…/profile` never kills `…/profile-9334` (A.5) ──
KILL_MATCH="--user-data-dir=$(_escape_ere "$PROFILE_DIR")(\$|[[:space:]])"

# ── Single Chrome argv array: one source for dry-run AND the real launch ──
CHROME_ARGV=(
  "$CHROME_BIN"
  "--user-data-dir=$PROFILE_DIR"
  "--remote-debugging-port=$CDP_PORT"
  "--remote-allow-origins=*"
  --no-first-run
  --no-default-browser-check
  --disable-extensions
  --disable-sync
  --disable-translate
  --disable-background-networking
  --disable-component-update
  "--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}"
  "--window-position=${WINDOW_POSITION}"
)
if (( HEADLESS )); then
  CHROME_ARGV+=(--headless=new)
fi
if (( INSECURE )); then
  CHROME_ARGV+=(--disable-web-security)
fi
if [[ -n "$CERT_SPKI" ]]; then
  CHROME_ARGV+=("--ignore-certificate-errors-spki-list=$CERT_SPKI")
fi
if (( AUTOMATION )); then
  # --enable-automation: suppresses the bad-flags infobar (CfT alone does NOT —
  # research-verified); --use-mock-keychain: no macOS keychain prompts/leak (hole K).
  CHROME_ARGV+=(--enable-automation --use-mock-keychain)
fi
# Chrome end-of-options: a URL beginning with -- must not be parsed as a Chrome flag
# (R1-F3). The --headless=new flag above MUST precede this -- separator, or Chrome
# would treat --headless=new itself as a positional (post---) argument.
if [[ "$URL" == --* ]]; then
  CHROME_ARGV+=(--)
fi
CHROME_ARGV+=("$URL")

# ── LOOK_DRY_RUN: print resolved config + argv, do NOT launch ──
if [[ "${LOOK_DRY_RUN:-}" == "1" ]]; then
  local_state_patch=$(( HEADLESS == 1 ? 0 : 1 ))
  osascript_steps=$(( HEADLESS == 1 ? 0 : 1 ))
  echo "LOOK_DRY_RUN"
  echo "port=$CDP_PORT"
  echo "profile=$PROFILE_DIR"
  echo "profile_overridden=$PROFILE_OVERRIDDEN"
  echo "headless=$HEADLESS"
  echo "insecure=$INSECURE"
  echo "automation=$AUTOMATION"
  echo "ephemeral=$EPHEMERAL"
  echo "cert_spki=$CERT_SPKI"
  echo "local_state_patch=$local_state_patch"
  echo "osascript=$osascript_steps"
  echo "window_position=$WINDOW_POSITION"
  echo "chrome_bin=$CHROME_BIN"
  echo "app_name=$CHROME_APP_NAME"
  echo "log=$LOG"
  echo "kill_match=$KILL_MATCH"
  echo "ARGV"
  printf '%s\n' "${CHROME_ARGV[@]}"
  if (( EPHEMERAL )); then
    rmdir "$PROFILE_DIR" 2>/dev/null  # mktemp created it; dry-run must not litter (empty by construction)
  fi
  exit 0
fi

# ── Real launch ──
# Binary preflight: fail loud with an actionable hint instead of a silent
# chrome.log-only death (the automation default points at the CfT pin, which may
# simply not be installed yet).
if [[ ! -x "$CHROME_BIN" ]]; then
  echo "ERROR: Chrome binary not found or not executable: $CHROME_BIN" >&2
  if (( AUTOMATION )); then
    echo "       Install the pinned Chrome for Testing: bash skills/look/scripts/update-cft.sh" >&2
  fi
  exit 1
fi

mkdir -p "$PROFILE_DIR" "$(dirname "$LOG")"

# Kill existing JAINE browser on this profile. Skipped for ephemeral lanes: the
# mktemp profile did not exist moments ago, so no process can match — the pkill
# would always no-op and the sleep would waste 1s per lane (code-review, PR #178).
if (( ! EPHEMERAL )); then
  pkill -f -- "$KILL_MATCH" 2>/dev/null
  sleep 1
fi

# Pre-patch Local State to enable AppleScript JS (headful only — no GUI when headless)
if [[ "$HEADLESS" != "1" ]]; then
  LOCAL_STATE="$PROFILE_DIR/Local State"
  if [ -f "$LOCAL_STATE" ]; then
    # Pass the path as argv (NOT string-interpolated) so a profile path containing a
    # quote/newline can't break the Python (silent SyntaxError → patch skipped).
    python3 - "$LOCAL_STATE" <<'PY'
import json, sys
p = sys.argv[1]
with open(p) as f: s = json.load(f)
s.setdefault('browser', {})['allow_javascript_apple_events'] = True
with open(p, 'w') as f: json.dump(s, f)
PY
  fi
fi

"${CHROME_ARGV[@]}" >> "$LOG" 2>&1 &

CHROME_PID=$!
if (( EPHEMERAL )); then
  # SP4 §2.1 readiness: Chrome writes <profile>/DevToolsActivePort — line 1 = port,
  # line 2 = ws path, NO trailing newline (spike-verified on CfT 149.0.7827.54).
  _dtap="$PROFILE_DIR/DevToolsActivePort"
  _eph_deadline=$(( SECONDS + 10 ))
  while (( SECONDS < _eph_deadline )) && [[ ! -s "$_dtap" ]]; do
    sleep 0.2
  done
  if [[ ! -s "$_dtap" ]]; then
    echo "LANE_FAIL: DevToolsActivePort not written within 10s (profile $PROFILE_DIR)" >&2
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
  IFS= read -r CDP_PORT < "$_dtap"   # head-1 semantics; no trailing-newline assumptions
  # Guard the Chrome-written value before it reaches the curl URL and the contract:
  # a truncated/garbled first line (write race, format change) must die loudly here,
  # not as 10 cryptic curl failures (code-review, PR #178).
  if ! [[ "$CDP_PORT" =~ ^[0-9]{1,5}$ ]]; then
    echo "LANE_FAIL: DevToolsActivePort line 1 is not a port (got: $CDP_PORT)" >&2
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
  _eph_ok=0
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -m 2 "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1; then
      _eph_ok=1; break
    fi
    sleep 0.3
  done
  if (( ! _eph_ok )); then
    echo "LANE_FAIL: CDP on port $CDP_PORT never answered /json/version" >&2
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
fi
# Ephemeral lanes already proved CDP liveness above — the blind settle sleep would
# waste 3s per lane (× every calibration run; code-review, PR #178).
if (( ! EPHEMERAL )); then
  sleep 3
fi

# AppleScript JS enablement (headful only — no GUI/menus when headless).
# Targets OUR lane's browser by unix id (CHROME_PID) via System Events — no
# LaunchServices name resolution (hole J: no "Where is" hang), lane-precise even
# when two same-named browsers run headful (unifies with hole B's pid-first).
# RU/EN menu trees (hole G: CfT ships English menus even on a Russian system)
# + hard timeout on every call. The name-based `tell application` remains ONLY in
# cdp.py's AppleScript JS channel where Chrome's dictionary requires it (guarded
# by its shipped timeout=10) — spec §6 J's "where possible" boundary.
if [[ "$HEADLESS" != "1" ]]; then
  _osascript_to 15 '
tell application "System Events"
    try
        set frontmost of (first application process whose unix id is '"$CHROME_PID"') to true
    end try
end tell'
  sleep 0.5
  # Auto-enable AppleScript JS via menu click (one-time, persists in profile)
  _osascript_to 15 '
tell application "System Events"
    tell (first application process whose unix id is '"$CHROME_PID"')
        try
            click menu item "Разрешить JavaScript из событий Apple" of menu 1 of menu item "Разработчикам" of menu "Вид" of menu bar 1
        end try
        try
            click menu item "Allow JavaScript from Apple Events" of menu 1 of menu item "Developer" of menu "View" of menu bar 1
        end try
    end tell
end tell'

  # Handle confirmation dialog if it appears
  sleep 1
  _osascript_to 15 '
tell application "System Events"
    tell (first application process whose unix id is '"$CHROME_PID"')
        try
            click button "Разрешить" of sheet 1 of window 1
        end try
        try
            click button "Allow" of sheet 1 of window 1
        end try
    end tell
end tell'
fi

if kill -0 "$CHROME_PID" 2>/dev/null; then
  echo "JAINE Browser started (PID $CHROME_PID, CDP :$CDP_PORT)"
  if (( EPHEMERAL )); then
    # SP4 §2.1 contract — parseable final lines for delegation consumers.
    echo "CDP_PORT=$CDP_PORT"
    echo "LANE_PROFILE=$PROFILE_DIR"
    echo "LANE_KILL_MATCH=$KILL_MATCH"
    echo "LANE_BROWSER_BIN=$CHROME_BIN"
  fi
else
  echo "ERROR: Chrome failed to start — check $LOG" >&2
  if (( EPHEMERAL )); then
    # Mirror the LANE_FAIL cleanups: Chrome passed liveness but died before the
    # contract — the mktemp profile must not leak (code-review, PR #178).
    rm -rf "$PROFILE_DIR"
  fi
  exit 1
fi
