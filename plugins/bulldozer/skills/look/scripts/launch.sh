#!/usr/bin/env bash
# JAINE Browser — dedicated Chrome instance with CDP + AppleScript
# Separate profile, no extensions, dark theme, remote debugging
# Usage: jaine-browser [URL]   (LOOK_DRY_RUN=1 prints resolved config + argv, no launch)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# #322 A1: lane lifecycle audit → bulldozer-drive.log (env override BULLDOZER_DRIVE_LOG).
# Non-9333 lanes only (the daily browser is /look's, not a drive lane). Canonical
# writer (sanitization/rotation/session); fail-open — never blocks the launch.
_log_lane() {
  [[ "${CDP_PORT:-9333}" == "9333" ]] && return 0
  # #187: auto-lane lines carry the marker; no-flag lines stay byte-compatible.
  if (( ${AUTO_LANE:-0} )); then set -- "$@" "auto_lane=1"; fi
  python3 "$SCRIPT_DIR/../../../lib/bulldozer_log.py"     "${BULLDOZER_DRIVE_LOG:-${HOME:-}/.claude/hooks/bulldozer-drive.log}" "$@" || true
}

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
# unconditional daily-profile gate (#160) and the insecure (R1-F1) / automation (R1-C) /
# cert-spki gates. Echoes 1/0; fail-CLOSED: any error → 1 (treated AS the daily profile).
#
# Identity (samefile), not canonical STRING: /0 is case-insensitive APFS, where
# /0/.JAINE/.browser/profile IS the daily directory — yet realpath preserves the caller's
# casing, so a string compare calls them different and lets the alias through (codex P1,
# reproduced live). samefile stats both, so case aliases, symlinks and hardlinks all
# collapse to the same inode. Falls back to the realpath compare when a path does not
# exist (samefile would raise) — a nonexistent dir cannot BE the live daily profile.
_resolves_to_daily_profile() {
  python3 - "$1" <<'PY' 2>/dev/null || echo 1
import os, sys
p, daily = sys.argv[1], "/0/.jaine/.browser/profile"
try:
    if os.path.exists(p) and os.path.exists(daily):
        print(1 if os.path.samefile(p, daily) else 0)
    else:
        print(1 if os.path.realpath(p) == os.path.realpath(daily) else 0)
except OSError:
    print(1)
PY
}

# ── Config ──
# #187 Phase A: presence snapshot BEFORE the defaulting line below destroys the
# unset-vs-set distinction (the auto-lane exclusions are presence-based; a
# set-but-empty value still counts as set — spec §4.1).
_CDP_PORT_WAS_SET=${CDP_PORT+x}
_LOOK_PROFILE_DIR_WAS_SET=${LOOK_PROFILE_DIR+x}
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

# ── The daily profile belongs to port 9333 and to NOTHING else (#160) ──
# KILL_MATCH is scoped by --user-data-dir, not by port: a non-9333 lane whose profile
# resolves to the daily one pkills the user's LIVE browser. The three opt-in gates
# (automation/insecure/cert-spki) each rejected this, but a plain lane had no gate —
# so the flagless recipe was the destructive one. Unconditional, at resolution time.
# String compare (!= "0"), not (( )): a malformed capture would make (( )) fail OPEN.
if (( CDP_PORT != 9333 )) && [[ "$(_resolves_to_daily_profile "$PROFILE_DIR")" != "0" ]]; then
  echo "ERROR: profile resolves to the DAILY browser's profile (/0/.jaine/.browser/profile)" >&2
  echo "       on a non-9333 lane (port=$CDP_PORT profile=$PROFILE_DIR). This lane's restart" >&2
  echo "       kills by --user-data-dir, so it would kill the user's live browser. Refusing." >&2
  echo "       Use the daily lane (CDP_PORT=9333) or an isolated LOOK_PROFILE_DIR." >&2
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
AUTO_LANE_ARG=0
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
    --auto-lane) AUTO_LANE_ARG=1 ;;
    --cert-spki=*) CERT_SPKI_ARG="${a#--cert-spki=}" ;;
    --*)
      echo "ERROR: unknown flag '$a' (look launcher accepts --headless/--headful/--insecure/--automation/--auto-lane/--cert-spki=<PIN>)" >&2
      exit 1
      ;;
    *) if (( ! URL_SET )); then URL="$a"; URL_SET=1; fi ;;
  esac
done
if (( ! URL_SET )); then URL="about:blank"; fi

# ── #187 Phase B: auto-lane exclusion preflight — BEFORE the ephemeral and
#    automation gates, so a conflicting request dies with AUTO-LANE attribution
#    (spec §4.1: `CDP_PORT=0 --auto-lane` must not surface the SP4 gate text,
#    and `--automation --auto-lane` must not run the automation arm first).
#    The automation request is computed LOCALLY here — the canonical
#    AUTOMATION_REQUESTED resolution runs later, after the ephemeral block. ──
if (( AUTO_LANE_ARG )); then
  if [[ -n "$_CDP_PORT_WAS_SET" ]]; then
    echo "ERROR: --auto-lane owns port selection (OS-assigned via port 0) — unset CDP_PORT." >&2
    echo "       (env CDP_PORT was set; any value, including empty, conflicts with --auto-lane)" >&2
    exit 1
  fi
  if [[ -n "$_LOOK_PROFILE_DIR_WAS_SET" ]]; then
    echo "ERROR: --auto-lane owns the lane profile (session-keyed temp dir) — unset LOOK_PROFILE_DIR." >&2
    exit 1
  fi
  shopt -s nocasematch
  _alb_env_auto=0
  [[ "${LOOK_AUTOMATION:-}" =~ ^(1|true|yes)$ ]] && _alb_env_auto=1
  shopt -u nocasematch
  if (( AUTOMATION_ARG )) || (( _alb_env_auto )); then
    echo "ERROR: --auto-lane is the stock-Chrome /look lane; the CfT automation path has its" >&2
    echo "       own isolated-lane mechanics — use CDP_PORT=0 --automation instead." >&2
    exit 1
  fi
fi

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

# ── #187 Phase C: auto-lane mutation arm (spec §4.2/§4.3). Sits AFTER the
#    automation arm (its exclusions already rejected composition) and BEFORE the
#    insecure/cert gates (PROFILE_OVERRIDDEN=1 lets them pass on the isolated
#    profile). Recomputes EVERY profile/port-dependent value the config section
#    resolved with vacuous defaults. ──
AUTO_LANE=0
AUTO_LANE_REUSE=0
AUTO_LANE_REUSE_REASON=""
AUTO_LANE_REUSE_PORT=""
if (( AUTO_LANE_ARG )); then
  AUTO_LANE=1
  CDP_PORT=0            # internal port-0 launch; NOT the SP4 ephemeral arm (EPHEMERAL stays 0)
  _al_key="${CLAUDE_CODE_SESSION_ID:-}"
  if [[ -z "$_al_key" ]]; then _al_key="$PPID"; fi
  _al_crc=$(printf '%s' "$_al_key" | cksum | cut -d' ' -f1)
  _al_key8=$(printf '%08x' "$_al_crc")
  PROFILE_DIR="${TMPDIR:-/tmp}/look-lane-${_al_key8}"
  # Mirror of the top-of-file guard: this path is derived AFTER it ran (§4.3.3).
  if [[ "$PROFILE_DIR" == *\\* || "$PROFILE_DIR" == *$'\n'* ]]; then
    echo "ERROR: TMPDIR-derived auto-lane profile must not contain a backslash or newline (got: $PROFILE_DIR)" >&2
    exit 1
  fi
  # Whitespace guard (codex-review r2): ps -o command= flattens argv, so a
  # profile path containing spaces could embed switch-shaped text inside ONE
  # argv element and confuse the token-boundary signature derivation (§4.4).
  # Refuse fail-loud — the whole confusion class dies here (macOS TMPDIRs are
  # whitespace-free; spaced ones are exotic).
  if [[ "$PROFILE_DIR" == *[[:space:]]* ]]; then
    echo "ERROR: TMPDIR-derived auto-lane profile must not contain whitespace (got: $PROFILE_DIR)" >&2
    exit 1
  fi
  # Daily-profile re-check, fail-closed (§4.3.4): the config-time #160 gate ran
  # BEFORE this arm and checked the wrong values; a TMPDIR alias/symlink that
  # resolves into the daily profile must die here, not launch.
  if [[ "$(_resolves_to_daily_profile "$PROFILE_DIR")" != "0" ]]; then
    echo "ERROR: auto-lane profile resolves to the DAILY browser's profile" >&2
    echo "       ($PROFILE_DIR). Refusing (daily-profile fail-closed re-check)." >&2
    exit 1
  fi
  PROFILE_OVERRIDDEN=1
  LOG="$PROFILE_DIR/chrome.log"
  # Headless default ON for auto-lane (§4.6): arg > LOOK_HEADLESS PRESENCE >
  # auto-default 1. The earlier resolution already applied arg/env; only the
  # both-absent case flips.
  if [[ -z "$HEADLESS_ARG" && -z "${LOOK_HEADLESS+x}" ]]; then
    HEADLESS=1
  fi
  # Window position from key8 (§4.3.6): the OS port is unknown pre-launch, while
  # --window-position must be in CHROME_ARGV before Chrome starts (R1-F3).
  _al_off=$(( 16#$_al_key8 % 1200 ))
  _al_norm=$(( ((_al_off % 1200) + 1200) % 1200 ))
  WINDOW_X=$(( 100 + _al_norm ))
  WINDOW_Y=$(( 100 + _al_norm ))
  WINDOW_POSITION="${WINDOW_X},${WINDOW_Y}"
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

# ── #187 pass 0 (spec §4.4): reuse detection for the auto-lane. Pure reads
#    (pgrep/ps/file/curl) — runs in dry-run too, reported via the
#    auto_lane_reuse_reason key. Sits AFTER the insecure/cert gates so the
#    REQUEST signature (headless/insecure/cert) is final. ──

# Cmdline-derived config signature (§4.4): the one artifact a parallel-launch
# race cannot falsify. argv[0] is deliberately NOT a field.
_al_sig_of_cmd() {  # $1 = full command line
  # Token-boundary matching (codex-review F1): a URL argv token can CONTAIN
  # switch text (`?q=--disable-web-security`) without BEING the switch —
  # substring scans would misclassify it. Chrome switches are always
  # whitespace-delimited tokens; padding both sides gives every token a boundary.
  local h=0 i=0 c="" _padded=" $1 " _re='(^|[[:space:]])--ignore-certificate-errors-spki-list=([^[:space:]]*)'
  [[ "$_padded" == *" --headless=new "* ]] && h=1
  [[ "$_padded" == *" --disable-web-security "* ]] && i=1
  if [[ "$1" =~ $_re ]]; then c="${BASH_REMATCH[2]}"; fi
  printf 'headless=%s|insecure=%s|cert=%s' "$h" "$i" "$c"
}

_al_request_sig() {
  printf 'headless=%s|insecure=%s|cert=%s' "$HEADLESS" "$INSECURE" "$CERT_SPKI"
}

# Main browser process for the profile: pgrep match WITHOUT a --type= flag
# (Chromium convention: every child carries --type=…). Echoes "pid|cmdline"
# lines.
_al_main_processes() {
  local _pid _cmd
  pgrep -f -- "$KILL_MATCH" 2>/dev/null | while read -r _pid; do
    [[ -n "$_pid" ]] || continue
    _cmd=$(ps -o command= -p "$_pid" 2>/dev/null)
    [[ -n "$_cmd" ]] || continue
    [[ "$_cmd" == *" --type="* ]] && continue
    printf '%s|%s\n' "$_pid" "$_cmd"
  done
}

# Browser websocket path of the endpoint answering /json/version on $1 —
# the identity half of the reuse check (§4.4: must equal DevToolsActivePort
# line 2 exactly).
_al_ws_path() {
  curl -s -m 2 "http://localhost:$1/json/version" 2>/dev/null | python3 -c '
import json, sys
try:
    u = json.load(sys.stdin)["webSocketDebuggerUrl"]
    print("/" + u.split("/", 3)[3])
except Exception:
    pass'
}

if (( AUTO_LANE )); then
  _al_mains=$(_al_main_processes)
  _al_main_count=0
  if [[ -n "$_al_mains" ]]; then
    _al_main_count=$(printf '%s\n' "$_al_mains" | grep -c .)
  fi
  if (( _al_main_count == 0 )); then
    AUTO_LANE_REUSE_REASON="no-process"
  elif (( _al_main_count > 1 )); then
    AUTO_LANE_REUSE_REASON="unhealthy"
  else
    _al_main_cmd="${_al_mains#*|}"
    if [[ "$(_al_sig_of_cmd "$_al_main_cmd")" != "$(_al_request_sig)" ]]; then
      AUTO_LANE_REUSE_REASON="config-mismatch"
    else
      _al_dtap="$PROFILE_DIR/DevToolsActivePort"
      _al_line1=""; _al_line2=""
      if [[ -s "$_al_dtap" ]]; then
        { IFS= read -r _al_line1; IFS= read -r _al_line2; } < "$_al_dtap"
      fi
      if ! [[ "$_al_line1" =~ ^[0-9]{1,5}$ ]] || [[ "$_al_line2" != /devtools/browser/* ]]; then
        AUTO_LANE_REUSE_REASON="unhealthy"
      else
        _al_ws=$(_al_ws_path "$_al_line1")
        if [[ -z "$_al_ws" ]]; then
          AUTO_LANE_REUSE_REASON="unhealthy"
        elif [[ "$_al_ws" != "$_al_line2" ]]; then
          # A recycled port answered — an UNRELATED CDP endpoint, never reusable.
          AUTO_LANE_REUSE_REASON="identity-mismatch"
        else
          AUTO_LANE_REUSE_REASON="ok"
          AUTO_LANE_REUSE=1
          AUTO_LANE_REUSE_PORT="$_al_line1"
        fi
      fi
    fi
  fi
fi

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
  if (( AUTO_LANE )); then
    # #187: flag-only keys — the no-flag dry-run stdout stays byte-identical
    # (pinned by test_default_dryrun_full_stdout_is_byte_identical).
    echo "auto_lane=1"
    echo "auto_lane_reuse=$AUTO_LANE_REUSE"
    echo "auto_lane_reuse_reason=$AUTO_LANE_REUSE_REASON"
  fi
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

# ── #187 reuse exit (spec §4.4): a healthy, config-matching, identity-bound
#    lane is returned WITHOUT pkill/relaunch — the browser keeps its tabs and
#    page state. The URL argument is NOT applied (SKILL.md's branch always
#    navigates via cdp.py). ──
if (( AUTO_LANE )) && [[ "$AUTO_LANE_REUSE_REASON" == "ok" ]]; then
  CDP_PORT="$AUTO_LANE_REUSE_PORT"
  # Revalidate identity IMMEDIATELY before the contract (codex-review F2): a
  # concurrent same-session call can kill/relaunch this lane between pass 0 and
  # here — printing the pass-0 port then would hand the caller a dead lane with
  # exit 0. Re-read the file + re-bind the endpoint; any change → fail loud.
  _al_rv_line1=""; _al_rv_line2=""
  if [[ -s "$PROFILE_DIR/DevToolsActivePort" ]]; then
    { IFS= read -r _al_rv_line1; IFS= read -r _al_rv_line2; } < "$PROFILE_DIR/DevToolsActivePort"
  fi
  if [[ "$_al_rv_line1" != "$AUTO_LANE_REUSE_PORT" ]] || \
     [[ "$(_al_ws_path "$AUTO_LANE_REUSE_PORT")" != "$_al_rv_line2" ]]; then
    echo "LANE_FAIL: lane changed underneath between reuse check and contract" >&2
    echo "           (a concurrent call restarted it) — re-invoke." >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=lane changed underneath reuse"
    exit 1
  fi
  echo "JAINE Browser reused (CDP :$CDP_PORT, profile $PROFILE_DIR)"
  _log_lane lane-reuse "port=$CDP_PORT" "profile=$PROFILE_DIR"
  echo "LANE_REUSED=1"
  echo "CDP_PORT=$CDP_PORT"
  echo "LANE_PROFILE=$PROFILE_DIR"
  echo "LANE_KILL_MATCH=$KILL_MATCH"
  echo "LANE_BROWSER_BIN=$CHROME_BIN"
  exit 0
fi

# Binary preflight: fail loud with an actionable hint instead of a silent
# chrome.log-only death (the automation default points at the CfT pin, which may
# simply not be installed yet).
if [[ ! -x "$CHROME_BIN" ]]; then
  echo "ERROR: Chrome binary not found or not executable: $CHROME_BIN" >&2
  if (( AUTOMATION )); then
    echo "       Install the pinned Chrome for Testing: bash skills/look/scripts/update-cft.sh" >&2
  fi
  _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=Chrome binary missing: $CHROME_BIN"
  exit 1
fi

mkdir -p "$PROFILE_DIR" "$(dirname "$LOG")"

# Kill existing JAINE browser on this profile. Skipped for ephemeral lanes: the
# mktemp profile did not exist moments ago, so no process can match — the pkill
# would always no-op and the sleep would waste 1s per lane (code-review, PR #178).
if (( ! EPHEMERAL )); then
  _stop_reason="replaced by new launch"
  if (( AUTO_LANE )) && [[ "$AUTO_LANE_REUSE_REASON" == "config-mismatch" ]]; then
    _stop_reason="config-mismatch"
  fi
  # The stop record must carry the OLD lane's actual port (codex-review r3):
  # phase C already reset CDP_PORT to 0 for auto-lanes, and the new port is not
  # assigned until after this event — port=0 would break start/stop correlation.
  _stop_port="$CDP_PORT"
  if (( AUTO_LANE )) && [[ -s "$PROFILE_DIR/DevToolsActivePort" ]]; then
    IFS= read -r _al_old_port < "$PROFILE_DIR/DevToolsActivePort"
    if [[ "$_al_old_port" =~ ^[0-9]{1,5}$ ]]; then
      _stop_port="$_al_old_port"
    fi
  fi
  if pkill -f -- "$KILL_MATCH" 2>/dev/null; then
    # a prior lane on this profile was signaled — close its lifecycle so it does
    # not read as leaked (#328 r7), but only once the process is CONFIRMED gone:
    # a delivered SIGTERM is not a terminated process (#328 r8)
    for _k in 1 2 3 4 5; do
      pgrep -f -- "$KILL_MATCH" >/dev/null 2>&1 || break
      sleep 0.3
    done
    if ! pgrep -f -- "$KILL_MATCH" >/dev/null 2>&1; then
      _log_lane lane-stop "port=$_stop_port" "profile=$PROFILE_DIR" "reason=$_stop_reason"
    elif (( AUTO_LANE )); then
      # #187 fail-closed on a survivor (spec §4.4): a surviving old-config
      # Chrome would win the user-data-dir singleton and silently BE the lane.
      echo "LANE_FAIL: old lane on $PROFILE_DIR would not terminate; refusing to" >&2
      echo "           launch (the survivor would win the profile singleton)." >&2
      _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=old lane would not terminate"
      exit 1
    fi
  fi
  sleep 1
fi

# ── #187 stale-evidence guard (spec §4.5, R1-F1): the FIRST profile mutation of
#    the fresh-launch path. rm is NOT trusted (no `set -e`): a surviving stale
#    DevToolsActivePort would let readiness read a STALE port whose endpoint may
#    be answered by an UNRELATED browser. Fail-closed BEFORE spawn. ──
if (( AUTO_LANE )); then
  rm -f "$PROFILE_DIR/DevToolsActivePort" 2>/dev/null
  if [[ -e "$PROFILE_DIR/DevToolsActivePort" ]]; then
    echo "LANE_FAIL: stale $PROFILE_DIR/DevToolsActivePort could not be removed —" >&2
    echo "           refusing pre-spawn (a stale file must not satisfy readiness)." >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=stale DevToolsActivePort not removable"
    exit 1
  fi
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
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=DevToolsActivePort not written within 10s"
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
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=DevToolsActivePort line 1 not a port"
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
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=CDP never answered /json/version"
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
fi
# ── #187 auto-lane readiness (spec §4.5): SP4 mechanics on the deterministic
#    profile. DevToolsActivePort is written by port-0 launches (NOT fixed-port —
#    R1-F1, empirically confirmed); the pre-spawn rm makes its presence
#    attributable to THIS launch generation. The profile is NOT removed on
#    failure (deterministic + reusable, unlike SP4's mktemp lanes). ──
AL_VERIFIED=0
if (( AUTO_LANE )); then
  _dtap="$PROFILE_DIR/DevToolsActivePort"
  _al_deadline=$(( SECONDS + 10 ))
  while (( SECONDS < _al_deadline )) && [[ ! -s "$_dtap" ]]; do
    sleep 0.2
  done
  if [[ ! -s "$_dtap" ]]; then
    echo "LANE_FAIL: DevToolsActivePort not written within 10s (profile $PROFILE_DIR)" >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=DevToolsActivePort not written within 10s"
    kill "$CHROME_PID" 2>/dev/null
    exit 1
  fi
  IFS= read -r CDP_PORT < "$_dtap"
  if ! [[ "$CDP_PORT" =~ ^[0-9]{1,5}$ ]]; then
    echo "LANE_FAIL: DevToolsActivePort line 1 is not a port (got: $CDP_PORT)" >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=DevToolsActivePort line 1 not a port"
    kill "$CHROME_PID" 2>/dev/null
    exit 1
  fi
  _al_ok=0
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -m 2 "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1; then
      _al_ok=1; break
    fi
    sleep 0.3
  done
  if (( ! _al_ok )); then
    echo "LANE_FAIL: CDP on port $CDP_PORT never answered /json/version" >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=CDP never answered /json/version"
    kill "$CHROME_PID" 2>/dev/null
    exit 1
  fi
  # Post-readiness effective-config verification (spec §4.5, R2-F1 v3):
  # readiness proved "a browser with OUR profile is up", not "OUR configuration
  # is up" — in the parallel-launch race the survivor can be the OTHER call's.
  # Never print a contract that misdescribes the running browser. Grace-poll
  # while TWO mains are visible (the singleton loser takes a moment to exit) —
  # without it the WINNER can spuriously fail on the transient count, and the
  # loser would fail on count instead of the deciding signature compare.
  _al_mains=""
  _al_main_count=0
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    _al_mains=$(_al_main_processes)
    _al_main_count=0
    if [[ -n "$_al_mains" ]]; then
      _al_main_count=$(printf '%s\n' "$_al_mains" | grep -c .)
    fi
    (( _al_main_count == 1 )) && break
    sleep 0.3
  done
  if (( _al_main_count != 1 )) || \
     [[ "$(_al_sig_of_cmd "${_al_mains#*|}")" != "$(_al_request_sig)" ]]; then
    echo "LANE_FAIL: a different launch won this profile (effective config does not" >&2
    echo "           match this request); re-invoke to reuse or restart it." >&2
    _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=effective config mismatch after launch"
    kill "$CHROME_PID" 2>/dev/null
    exit 1
  fi
  # The verified survivor IS the lane — adopt its pid (a same-config racer's
  # own child may have handed off to the singleton winner and exited).
  CHROME_PID="${_al_mains%%|*}"
  AL_VERIFIED=1
fi
# Ephemeral lanes already proved CDP liveness above — the blind settle sleep would
# waste 3s per lane (× every calibration run; code-review, PR #178). Auto-lanes
# proved it via their own readiness block (#187).
if (( ! EPHEMERAL )) && (( ! AUTO_LANE )); then
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
  _log_lane lane-start "port=$CDP_PORT" "profile=$PROFILE_DIR" "headless=$HEADLESS" \
    "automation=$AUTOMATION" "ephemeral=$EPHEMERAL" "insecure=$INSECURE" "pid=$CHROME_PID"
  if (( EPHEMERAL )); then
    # SP4 §2.1 contract — parseable final lines for delegation consumers.
    echo "CDP_PORT=$CDP_PORT"
    echo "LANE_PROFILE=$PROFILE_DIR"
    echo "LANE_KILL_MATCH=$KILL_MATCH"
    echo "LANE_BROWSER_BIN=$CHROME_BIN"
  fi
  if (( AUTO_LANE )); then
    # #187 §4.6 contract — SP4 grammar + LANE_REUSED (fresh launch → 0).
    echo "LANE_REUSED=0"
    echo "CDP_PORT=$CDP_PORT"
    echo "LANE_PROFILE=$PROFILE_DIR"
    echo "LANE_KILL_MATCH=$KILL_MATCH"
    echo "LANE_BROWSER_BIN=$CHROME_BIN"
  fi
else
  echo "ERROR: Chrome failed to start — check $LOG" >&2
  _log_lane lane-fail "port=$CDP_PORT" "profile=$PROFILE_DIR" "reason=Chrome failed to start"
  if (( EPHEMERAL )); then
    # Mirror the LANE_FAIL cleanups: Chrome passed liveness but died before the
    # contract — the mktemp profile must not leak (code-review, PR #178).
    rm -rf "$PROFILE_DIR"
  fi
  exit 1
fi
