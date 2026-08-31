#!/usr/bin/env python3
"""JAINE Browser — multi-channel browser automation (CDP + AppleScript + macOS native).

Usage:
  cdp.py status                    — check if browser is running
  cdp.py screenshot [FILE] [--full-page] [--clip X Y W H] [--scale N] [--bind]
                                   — capture screenshot
                                     --full-page : whole document (below-fold included)
                                     --clip X Y W H : CSS-pixel region (mutex with --full-page)
                                     --scale N : DPR override (1 = CSS pixels)
                                     --bind : second stdout line
                                       "BIND url=… loader=… t=…" tying the capture
                                       to its navigation (verify-core)
                                     stdout always prints "PATH  W×H"
  cdp.py js 'EXPRESSION'           — execute JS in main world
  cdp.py js --ref N 'EXPR'         — execute EXPR with `el` bound to ref element
  cdp.py navigate URL [--wait [load|domcontentloaded|networkidle]] [--expect-url SUBSTR] [--timeout S]
                                   — navigate; --wait blocks until the lifecycle
                                     event + prints final URL & loaderId (verify-core)
  cdp.py open URL                  — open URL in new tab
  cdp.py tabs                      — list all tabs
  cdp.py title                     — get page title
  cdp.py html                      — get full page HTML
  cdp.py reload                    — reload current page (cache bypass)
  cdp.py wait [--js] SELECTOR_OR_EXPR [TIMEOUT]  — wait for CSS selector or JS expression (--js)
  cdp.py assert [--js] EXPR_OR_SELECTOR [--visible|--actionable] [--stable MS] [--timeout S]
                                   — verify-core assertion: condition must hold
                                     CONTINUOUSLY for --stable ms (default 500);
                                     --actionable = visible + enabled + hit-test;
                                     ASSERT_PASS/ASSERT_FAIL + exit 0/1, flap
                                     diagnostics distinguish flaky from absent
  cdp.py assert --ref N [--visible|--actionable] [--stable MS] [--timeout S]
                                   — assert by AX ref (default: node resolvable)
  cdp.py click SELECTOR [--require-trusted]
                                   — click element; --require-trusted refuses the
                                     untrusted el.click() fallback (exit 1, no click)
  cdp.py click --ref N             — click by AX ref (always trusted, no fallback)
  cdp.py fill --ref N VALUE        — fill by AX ref + dispatch events
  cdp.py hover SELECTOR             — hover element (triggers CSS :hover, CDP only)
  cdp.py hover --ref N              — hover by AX ref
  cdp.py drag SRC_SEL DST_SEL [--html5 | --cancel]
                                   — drag element (CDP only); mouse-series default,
                                     --html5 for native DnD, --cancel for Esc mid-drag
  cdp.py drag --ref N --to-ref M [--html5 | --cancel]
                                   — drag by AX ref pair
  cdp.py key --ref N KEY           — send key to ref-focused element (ref-only)
                                     supported: Enter, Escape, Tab, ArrowDown, ArrowUp
  cdp.py fill SELECTOR VALUE       — fill input/textarea
  cdp.py console [--gate]          — console messages + uncaught exceptions;
                                     --gate: exit 1 if any error/exception (verify-core)
  cdp.py network                   — recent network requests
  cdp.py pdf [FILE]                — save page as PDF
  cdp.py viewport WIDTH HEIGHT     — change viewport size
  cdp.py window [bounds|upper|lower|activate] — window management
  cdp.py ax [--max-nodes N] [--raw] [--ref N]
                                   — accessibility tree snapshot (CDP only)
                                     --max-nodes N : limit rendered nodes (default 500, 0=all)
                                     --raw : disable all filters (show ignored/InlineTextBox)
                                     --ref N : scoped snapshot of subtree at ref N
                                     stdout: AX_OK header + Playwright-parity tree
  cdp.py [--target SEL] CMD ...    — pin every tab resolution in the call to SEL

Channels: CDP WebSocket (primary), AppleScript+DOM injection (fallback), macOS native (screenshot).
CDP_PORT env var overrides default 9333.
--target SEL (alias --tab) pins all of a call's commands to one tab: a full
target id, its 12-char prefix (as shown by `tabs`/`status`), or a url substring.
Requires the CDP/websocket channel (the AppleScript fallback drives the active tab).
Log: ~/.claude/hooks/bulldozer-look.log
"""
import json, sys, os, time, base64, hashlib, subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from urllib.error import URLError

# Bundled websocket-client: try vendor/ first, then system
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

try:
    CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
except ValueError:
    print("ERROR: CDP_PORT must be a number", file=sys.stderr)
    sys.exit(1)

CDP_BASE = "http://localhost:{}".format(CDP_PORT)
LOG_FILE = os.environ.get("BULLDOZER_LOOK_LOG") or os.path.expanduser(
    "~/.claude/hooks/bulldozer-look.log")
# SP1 (#164): AppleScript/Quartz app name. Drive lanes set "Google Chrome for
# Testing"; default stays the stock daily browser. All 9 callsites read this symbol.
# Same guard launch.sh applies: the name is spliced into AppleScript string
# literals — a quote/backslash/newline would break out of them (injection surface).
CHROME_APP = os.environ.get("CHROME_APP_NAME", "Google Chrome")
if any(c in CHROME_APP for c in ('"', "\\", "\n")):
    print("ERROR: CHROME_APP_NAME must not contain a double quote, backslash or newline",
          file=sys.stderr)
    sys.exit(1)
# Global tab selector, set by main() from --target/--tab. None → first page (default).
TARGET = None

# --- Logging ---
# Canonical grammar via the shared helper (#322 PR1). On import failure the line is
# DROPPED (one warning) — never appended raw, so unsanitized/multiline records can't
# re-enter the log (spec: 2026-07-11-bulldozer-log-grammar-design.md).
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__)))))  # realpath: survive symlinked skill dirs
sys.path.insert(0, os.path.join(_PLUGIN_ROOT, "lib"))
try:
    from bulldozer_log import append_line as _append_line
except Exception:  # ANY import-time failure (SyntaxError incl.) — /look must stay usable
    _append_line = None
_HELPER_WARNED = False


def log(event, **kw):
    global _HELPER_WARNED
    if _append_line is None:
        if not _HELPER_WARNED:
            print("warning: bulldozer_log helper unavailable — log line dropped",
                  file=sys.stderr)
            _HELPER_WARNED = True
        return
    fields = {"port": CDP_PORT}
    if TARGET is not None:
        fields["target"] = _redact_target(TARGET)
    fields.update(kw)
    _append_line(LOG_FILE, event, **fields)


# D2 (#322): the log is long-lived and unencrypted — JS source and URL
# query/fragment/userinfo carry secrets (tokens, basic-auth), so log() values
# for them are redacted at the call site: JS → length+hash, URL → origin+path.


def _sha12(text):
    # surrogatepass: undecodable argv bytes arrive as lone surrogates
    # (surrogateescape) — strict utf-8 would raise mid-log and turn a
    # completed command into an exit=crash (codex #329 r1)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


# Schemes whose path part is location, not payload. data:/javascript:/blob:
# (and anything unknown) carry the document/script itself in .path; view-source:
# nests a FULL url (incl. userinfo/query) in .path — all hashed wholesale,
# never logged verbatim (codex #329 r1+r2).
_LOCATION_SCHEMES = frozenset((
    "", "http", "https", "file", "ws", "wss", "about", "chrome",
    "chrome-extension", "devtools",
))


def _redact_target(sel):
    """--target may be a URL substring — ?/#/@ mark query/fragment/userinfo
    territory where secrets live; such selectors are hashed wholesale (codex
    #329 r3). Id-prefixes and host/path substrings (the common cases) stay
    readable."""
    if any(c in sel for c in "?#@"):
        return "<redacted:len={},sha={}>".format(len(sel), _sha12(sel))
    return sel[:120]


def _redact_url(url):
    """scheme://host:port/path survives (minable); userinfo, query and fragment
    are dropped — one `?<redacted>` marker records that something was there.
    Scheme-relative strings (about:blank) round-trip unchanged."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable:len={}".format(len(url))
    if parts.scheme.lower() not in _LOCATION_SCHEMES:
        return "{}:<redacted:len={},sha={}>".format(parts.scheme, len(url), _sha12(url))
    netloc = parts.netloc.rpartition("@")[2]  # strip user:pass@
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if parts.query or parts.fragment:
        # marker appended AFTER the cap so truncation can't eat it — a
        # redacted-query URL must stay distinguishable from a long path
        return base[:109] + "?<redacted>"
    return base[:120]

# --- Channel detection ---

HAS_WEBSOCKET = None
def has_websocket():
    global HAS_WEBSOCKET
    if HAS_WEBSOCKET is None:
        try:
            import websocket  # noqa: F401
            HAS_WEBSOCKET = True
        except ImportError:
            HAS_WEBSOCKET = False
    return HAS_WEBSOCKET

def channel():
    return "cdp" if has_websocket() else "applescript"

# --- CDP HTTP (works without websocket) ---

def cdp_get(path):
    try:
        return json.loads(urlopen("{0}{1}".format(CDP_BASE, path), timeout=5).read())
    except (URLError, json.JSONDecodeError, OSError):
        return None

def get_tab(target=None):
    """Resolve a single tab. target=None → first page (backward-compat). Otherwise
    SEL resolves by: exact id → unique 12-char id-prefix → unique url substring.
    Ambiguous (≥2) or no match → fail loud (sys.exit) — never drive the wrong tab."""
    tabs = cdp_get("/json/list")
    if not tabs:
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        sys.exit(1)
    pages = [t for t in tabs if t.get("type") == "page"]
    if target is None:
        return pages[0] if pages else tabs[0]
    # 1. exact id
    for t in pages:
        if t.get("id") == target:
            return t
    # 2. unique id-prefix — only at/above the 12-char displayed granularity
    #    (cmd_tabs/status/open print id[:12]); a <12-char selector is NOT an id
    #    prefix (it would collide too easily) → falls through to the url check.
    pref = ([t for t in pages if t.get("id", "").startswith(target)]
            if len(target) >= 12 else [])
    if len(pref) == 1:
        return pref[0]
    if len(pref) > 1:
        print("ERROR: --target {!r} is an ambiguous id prefix — {} tabs match ({}). "
              "Use more characters of the id.".format(
                  target, len(pref), ", ".join(t.get("id", "?")[:12] for t in pref)),
              file=sys.stderr)
        sys.exit(1)
    # 3. unique url substring (subsumes the old url_filter)
    urls = [t for t in pages if target in t.get("url", "")]
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        print("ERROR: --target {!r} matches {} tab URLs ({}). Be more specific.".format(
                  target, len(urls), ", ".join(t.get("url", "?")[:50] for t in urls)),
              file=sys.stderr)
        sys.exit(1)
    # 4. no match → fail loud
    print("ERROR: --target {!r} matched no tab (not an id, id-prefix, or url substring). "
          "Run `tabs` to list open tabs.".format(target), file=sys.stderr)
    sys.exit(1)

# --- arg helpers (review pack D: the strip-a-flag / pop-a-numeric-value
# pattern was hand-rolled per command and had started to drift) ---

def _pop_flag(args, name):
    """(args_without_flag, was_present) — boolean flag extraction."""
    return [a for a in args if a != name], name in args

def _pop_num(args, name, cast, default):
    """Extract `name VALUE` casting VALUE via cast. Returns (args, value) or
    (args, None) after printing an error when VALUE is missing/malformed."""
    if name not in args:
        return args, default
    args = list(args)
    i = args.index(name)
    try:
        value = cast(args[i + 1])
    except (IndexError, ValueError):
        unit = "an integer (ms)" if cast is int else "a numeric (seconds)"
        print("ERROR: {} needs {} argument".format(name, unit), file=sys.stderr)
        return args, None
    del args[i:i + 2]
    return args, value

# --- CDP WebSocket ---

def _recv_for_id(ws, call_id, events=None):
    """Read frames until the response with `id == call_id` arrives. Event frames
    (those carrying a 'method') are appended to `events` when a buffer is given,
    else dropped. Review pack A: a single bare recv() is id-blind — on a noisy
    endpoint (browser-level Target.* events, a tab with previously-enabled
    domains) the first frame is often NOT the response, and treating it as one
    yields false results (e.g. cookie-seed reporting success without writing)."""
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == call_id:
            return r
        if events is not None and "method" in r:
            events.append(r)

def ws_send(ws_url, method, params=None):
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    try:
        msg = {"id": 1, "method": method}
        if params is not None:
            msg["params"] = params
        ws.send(json.dumps(msg))
        result = _recv_for_id(ws, 1)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return None
    finally:
        ws.close()
    if "error" in result:
        err = result["error"]
        print("CDP error: {} (code {})".format(
            err.get("message", "unknown"), err.get("code", "?")), file=sys.stderr)
        return None
    return result

def ws_send_seq(ws_url, calls):
    """Send a sequence of CDP methods over ONE connection (e.g. mousePressed +
    mouseReleased for a single trusted click). Returns the list of result dicts,
    or None on any transport/CDP error. Mirrors ws_send's error contract.

    `calls` is a list of (method, params) tuples. Each response is matched to its
    request id; any unsolicited event on the connection is skipped (defensive —
    a fresh connection with no enabled domains shouldn't receive events).
    """
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    results = []
    try:
        for i, (method, params) in enumerate(calls, start=1):
            msg = {"id": i, "method": method}
            if params:
                msg["params"] = params
            ws.send(json.dumps(msg))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") == i:
                    break
            if "error" in result:
                err = result["error"]
                print("CDP error: {} (code {})".format(
                    err.get("message", "unknown"), err.get("code", "?")), file=sys.stderr)
                return None
            results.append(result)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return None
    finally:
        ws.close()
    return results

def ws_navigate_and_wait(ws_url, url, wait_event, timeout_s):
    """Page.enable + setLifecycleEventsEnabled → Page.navigate → block until the
    lifecycle event for OUR loaderId. One connection for the whole flow. Returns
    {"ok": True, "final_url", "loader_id", "elapsed_ms"} on success,
    {"ok": False, "reason": "..."} on navigation error/timeout,
    None on transport error (mirrors ws_send's contract).

    wait_event: "load" | "domcontentloaded" | "networkidle".

    ALL three modes match Page.lifecycleEvent filtered by OUR loaderId — NOT the
    bare Page.loadEventFired/domContentEventFired, which carry no loaderId
    (R1-F2): a still-loading PREVIOUS page can fire its load-class event inside
    our enable→navigate window; a loaderId-less match would accept it as ours
    and green-light asserts against stale DOM — exactly the R2-N race this
    helper exists to close. With the loaderId filter, buffering pre-response
    events is safe: a foreign event simply never matches.

    Events that arrive while we are waiting for a command RESPONSE are buffered
    (a data:/cached page can fire its lifecycle event BEFORE the Page.navigate
    response is read — discarding them would hang us to timeout)."""
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    start = time.time()
    events = []  # buffered events seen while draining command responses

    def call(call_id, method, params=None):
        msg = {"id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        ws.send(json.dumps(msg))
        return _recv_for_id(ws, call_id, events)

    try:
        call(1, "Page.enable")
        call(2, "Page.setLifecycleEventsEnabled", {"enabled": True})
        nav = call(3, "Page.navigate", {"url": url})
        if "error" in nav:
            return {"ok": False,
                    "reason": "CDP error: " + nav["error"].get("message", "?")}
        nav_result = nav.get("result", {})
        if nav_result.get("errorText"):
            return {"ok": False, "reason": nav_result["errorText"]}
        loader_id = nav_result.get("loaderId", "")

        # lifecycleEvent names: load / DOMContentLoaded / networkIdle (CDP casing)
        want_name = {"load": "load",
                     "domcontentloaded": "DOMContentLoaded",
                     "networkidle": "networkIdle"}[wait_event]
        deadline = start + timeout_s

        def matches(msg):
            if msg.get("method") != "Page.lifecycleEvent":
                return False
            p = msg.get("params", {})
            # loaderId filter is the R1-F2 race guard; same-document navigations
            # can yield an empty loaderId in the navigate response → degrade to
            # name-only matching for them (no prior-loader ambiguity there).
            return (p.get("name") == want_name
                    and (not loader_id or p.get("loaderId") == loader_id))

        fired = any(matches(e) for e in events)
        while not fired and time.time() < deadline:
            ws.settimeout(max(0.1, min(1.0, deadline - time.time())))
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if matches(msg):
                fired = True
        if not fired:
            return {"ok": False,
                    "reason": "timeout: {} not fired within {}s".format(
                        wait_event, timeout_s)}
        # Reset the socket timeout: the event loop above left it as low as 0.1s,
        # and SPA pages often emit more lifecycle events right after load —
        # call() would re-recv under that tiny budget and a timeout there
        # surfaces as a false NAVIGATE_FAIL for a navigation that SUCCEEDED
        # (review pack A).
        ws.settimeout(10)
        fin = call(9, "Runtime.evaluate",
                   {"expression": "location.href", "returnByValue": True})
        final_url = (fin.get("result", {}).get("result", {}) or {}).get("value", "")
        return {"ok": True, "final_url": final_url, "loader_id": loader_id,
                "elapsed_ms": int((time.time() - start) * 1000)}
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return None
    finally:
        ws.close()

def cdp_js(expr, ws_url):
    """Evaluate expr in the tab at ws_url. The caller resolves the tab ONCE via
    get_tab(TARGET) and threads ws_url here — cdp_js never re-resolves get_tab()
    (so a --target pin can't drift between a command's measure and follow-up calls)."""
    r = ws_send(ws_url, "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
    })
    if r is None:
        return None
    return r.get("result", {}).get("result", {})

# --- AppleScript bridge (DOM injection → main world) ---

def osascript(script):
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        print("ERROR: AppleScript timed out — Chrome may be unresponsive", file=sys.stderr)
        return None
    if r.returncode != 0:
        err = r.stderr.strip()
        if "AppleScript" in err and any(m in err for m in ("отключено", "turned off", "disabled", "not allowed")):
            print("ERROR: AppleScript JS disabled in Chrome. Enable: View > Developer > Allow JavaScript from Apple Events", file=sys.stderr)
            return None
        print("AppleScript error: " + err, file=sys.stderr)
        return None
    return r.stdout.strip()

def as_js_main_world(expr):
    """Execute JS in main world via DOM injection bridge.
    Chrome AppleScript runs in isolated world (known Chromium issue #543437).
    Workaround: inject <script> tag which runs in main world, write result to dataset."""
    safe_expr = json.dumps(expr)[1:-1].replace("'", "\\'")

    script = '''
tell application "{app}"
    tell window 1
        tell active tab
            execute javascript "delete document.body.dataset._jresult;var _s=document.createElement('script');_s.textContent='document.body.dataset._jresult=JSON.stringify((function(){{try{{return {expr}}}catch(e){{return \\"ERR: \\"+e.message}}}})())';document.head.appendChild(_s);_s.remove();"
            set r to execute javascript "document.body.dataset._jresult"
        end tell
    end tell
end tell
return r'''.format(app=CHROME_APP, expr=safe_expr)
    raw = osascript(script)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw

def as_navigate(url):
    safe_url = url.replace('"', '\\"')
    return osascript('''
tell application "{app}"
    tell window 1
        tell active tab
            set URL to "{url}"
        end tell
    end tell
end tell'''.format(app=CHROME_APP, url=safe_url)) is not None

def as_reload():
    return osascript('''
tell application "{app}"
    tell window 1
        tell active tab
            execute javascript "location.reload(true)"
        end tell
    end tell
end tell'''.format(app=CHROME_APP)) is not None

# --- macOS native screenshot ---

def _chrome_pid_for_port(port, _runner=subprocess.run):
    """Browser-process pid owning this lane's CDP port, or None (hole B, R1).

    Lane-precise: two Chrome instances (stock + CfT, or two CfT lanes) both match
    any name heuristic; only ONE owns --remote-debugging-port=<port>. Helper
    processes carry --type=… and no debugging port, so the anchored pgrep -f
    pattern matches exactly the browser process of THIS lane.
    """
    pat = r"--remote-debugging-port={}($|[[:space:]])".format(port)
    try:
        # "--" is load-bearing: the pattern starts with "--" and BSD pgrep would
        # otherwise parse it as a flag (usage error rc=2 → silent name-fallback).
        r = _runner(["pgrep", "-f", "--", pat], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    pids = r.stdout.split()
    try:
        return int(pids[0]) if pids else None
    except ValueError:
        return None


def native_screenshot(path):
    # Hole B: match THIS lane's browser pid (precise across stock/CfT/parallel
    # lanes); fall back to exact owner-name == CHROME_APP — never a substring
    # ('Google' used to match Google Drive and CfT alike).
    pid = _chrome_pid_for_port(CDP_PORT)
    if pid is not None:
        _match = "w.get('kCGWindowOwnerPID')=={}".format(pid)
    else:
        # Surfacing, not silence: with several same-named browsers running the
        # name match may capture the WRONG window — say so instead of silently
        # returning a plausible screenshot of another browser.
        print("WARNING: lane pid lookup failed — falling back to app-name match "
              "{!r} (may target the wrong browser if several run)".format(CHROME_APP),
              file=sys.stderr)
        _match = "str(w.get('kCGWindowOwnerName',''))=={!r}".format(CHROME_APP)
    _quartz_code = (
        "import Quartz as Q; ws=Q.CGWindowListCopyWindowInfo("
        "Q.kCGWindowListOptionOnScreenOnly|Q.kCGWindowListExcludeDesktopElements,"
        "Q.kCGNullWindowID);\n"
        "wid=next((w['kCGWindowNumber'] for w in ws "
        "if " + _match + " "
        "and w.get('kCGWindowName','')),None)\n"
        "print(wid if wid is not None else '')"
    )
    wid = ""
    for pybin in [sys.executable, "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]:
        if pybin != sys.executable and not os.path.exists(pybin):
            continue
        r = subprocess.run([pybin, "-c", _quartz_code],
                           capture_output=True, text=True, timeout=5)
        wid = r.stdout.strip() if r.returncode == 0 else ""
        if wid:
            break
    if not wid:
        print("ERROR: cannot find Chrome CGWindowID for screenshot", file=sys.stderr)
        return False
    ext = os.path.splitext(path)[1].lower()
    fmt = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "pdf": "pdf", "tiff": "tiff"}.get(ext.lstrip("."), "png")
    r = subprocess.run(["screencapture", "-l", wid, "-o", "-x", "-t", fmt, path],
                       capture_output=True, timeout=10)
    return r.returncode == 0

# --- Commands ---

def cmd_status(args):
    tabs = cdp_get("/json/list")
    if tabs is None:
        print("OFFLINE — browser not running on port " + str(CDP_PORT))
        return 1
    pages = [t for t in tabs if t.get("type") == "page"]
    ws = "yes" if has_websocket() else "no"
    print("ONLINE — {} tabs on port {} (websocket: {}, channel: {})".format(
        len(pages), CDP_PORT, ws, channel()))
    for t in pages:
        print("  {} {}".format(t["id"][:12], t.get("url", "?")[:80]))
    log("status", tabs=len(pages), channel=channel())
    return 0

def cmd_tabs(args):
    tabs = cdp_get("/json/list")
    if tabs is None:  # unreachable ≠ zero tabs — fail like cmd_status (#324 r3)
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        return 1
    pages = [t for t in tabs if t.get("type") == "page"]
    for t in pages:
        print("{}  {:40s}  {}".format(
            t["id"][:12], t.get("title", "?")[:40], t.get("url", "?")[:60]))
    log("tabs", count=len(pages))

def cmd_close(args):
    """Close a tab by SEL. Opening without closing is how a browser fills up.

    Why this exists: cdp.py could open tabs but not close them, so every caller had
    to remember cleanup — and nobody did. Twelve dead tabs accumulated on one port
    in a single day, and measurements degraded run over run, reddening on healthy
    code. A tool that can only grow its own state makes cleanup a memory problem.

    Why /json/close and NOT window.close(): window.close() on the last (or an
    unopened-by-script) tab tears down the whole browser, taking every OTHER
    session's tabs with it. The HTTP endpoint closes exactly one target.

    Last-tab guard: closing the only remaining page ends the browser process on
    most builds. That is almost never what a cleanup step wants, so it needs
    --even-if-last said out loud.
    """
    args = list(args)
    force_last = "--even-if-last" in args
    if force_last:
        args.remove("--even-if-last")
    tabs = cdp_get("/json/list")
    if tabs is None:
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        return 1
    pages = [t for t in tabs if t.get("type") == "page"]
    tab = get_tab(args[0] if args else None)
    if len(pages) <= 1 and not force_last:
        print("REFUSED: {} is the only page on port {} — closing it ends the browser."
              .format(tab["id"][:12], CDP_PORT), file=sys.stderr)
        print("         Say --even-if-last if that is what you want.", file=sys.stderr)
        return 1
    if cdp_get("/json/close/" + tab["id"]) is None:
        # /json/close answers with the bare word "Target is closing", which is not
        # JSON — so cdp_get returns None on SUCCESS too. Settle it by looking at
        # the tab list, i.e. by the WORLD, not by the reply.
        pass
    after = cdp_get("/json/list") or []
    if any(t["id"] == tab["id"] for t in after):
        print("ERROR: tab {} still present after close".format(tab["id"][:12]),
              file=sys.stderr)
        return 1
    log("close", tab=tab["id"][:12], left=len([t for t in after if t.get("type") == "page"]))
    return 0


def _image_dimensions(path):
    """Read W×H from a JPEG or PNG file by parsing its header. Returns (w, h) or None.

    Avoids a hard dependency on Pillow — we already produce only JPEG (CDP) and
    PNG (native screencapture fallback), so a few bytes of header parsing is enough.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
    except OSError:
        return None
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        n = len(data)
        while i < n - 8:
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            # SOF markers, excluding DHT/JPG/DAC
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = (data[i + 5] << 8) | data[i + 6]
                w = (data[i + 7] << 8) | data[i + 8]
                return (w, h) if w > 0 and h > 0 else None
            length = (data[i + 2] << 8) | data[i + 3]
            if length < 2:
                return None
            i += 2 + length
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return (w, h) if w > 0 and h > 0 else None
    return None


def cmd_screenshot(args):
    args = list(args)

    full_page = "--full-page" in args
    args = [a for a in args if a != "--full-page"]

    args, bind = _pop_flag(args, "--bind")
    bind_line = None

    # --clip X Y W H (CSS pixels). Mutually exclusive with --full-page.
    clip = None
    if "--clip" in args:
        i = args.index("--clip")
        try:
            vals = [float(v) for v in args[i + 1 : i + 5]]
            if len(vals) != 4:
                raise ValueError("expected 4 numeric args (X Y W H)")
        except (IndexError, ValueError) as e:
            print("ERROR: --clip needs 4 numbers (X Y W H): {}".format(e), file=sys.stderr)
            return 1
        if vals[2] <= 0 or vals[3] <= 0:
            print("ERROR: --clip width and height must be positive (got W={}, H={})".format(vals[2], vals[3]), file=sys.stderr)
            return 1
        clip = {"x": vals[0], "y": vals[1], "width": vals[2], "height": vals[3], "scale": 1}
        del args[i : i + 5]
    if full_page and clip:
        print("ERROR: --clip and --full-page are mutually exclusive", file=sys.stderr)
        return 1

    # --scale N — output at N × CSS-pixel resolution.
    # Implemented via clip.scale = N / native_dpr. setDeviceMetricsOverride
    # does NOT change Page.captureScreenshot output size (empirically verified:
    # on a Retina display, even after viewport sets deviceScaleFactor=1, capture
    # still returns native-device pixels). clip.scale is the only CDP knob that
    # affects output dimensions.
    scale_override = None
    if "--scale" in args:
        i = args.index("--scale")
        try:
            scale_override = float(args[i + 1])
        except (IndexError, ValueError) as e:
            print("ERROR: --scale needs a numeric arg: {}".format(e), file=sys.stderr)
            return 1
        import math
        if not math.isfinite(scale_override):
            print("ERROR: --scale must be a finite number (got {!r})".format(args[i + 1]), file=sys.stderr)
            return 1
        if scale_override <= 0:
            print("ERROR: --scale must be positive (got {})".format(scale_override), file=sys.stderr)
            return 1
        del args[i : i + 2]

    path = args[0] if args else "/tmp/jaine-screenshot.jpg"

    if has_websocket():
        tab = get_tab(TARGET)
        ws_url = tab["webSocketDebuggerUrl"]
        params = {"format": "jpeg", "quality": 80}

        if clip:
            params["clip"] = clip
        elif full_page:
            metrics = cdp_js("JSON.stringify({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})", ws_url)
            if not metrics or not metrics.get("value"):
                print("WARNING: --full-page could not read page dimensions, capturing viewport only", file=sys.stderr)
            else:
                try:
                    dims = json.loads(metrics["value"])
                    w, h = int(dims["w"]), int(dims["h"])
                    if w > 0 and h > 0:
                        params["captureBeyondViewport"] = True
                        params["clip"] = {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}
                    else:
                        print("WARNING: --full-page got {}x{}, capturing viewport only".format(w, h), file=sys.stderr)
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                    print("WARNING: --full-page metrics parse failed ({}), capturing viewport only".format(e), file=sys.stderr)

        if scale_override is not None:
            # Read DPR via Runtime.evaluate on the SAME ws_url as the eventual capture.
            # Opening a separate WebSocket via a fresh get_tab() lookup would risk
            # targeting a different tab in a multi-tab JAINE Browser.
            dpr_response = ws_send(ws_url, "Runtime.evaluate", {
                "expression": "window.devicePixelRatio",
                "returnByValue": True,
            })
            dpr_r = (dpr_response or {}).get("result", {}).get("result")
            if dpr_r is None:
                print(
                    "WARNING: --scale could not read window.devicePixelRatio (CDP error). "
                    "Assuming 1.0 — output may be larger than expected on Retina.",
                    file=sys.stderr,
                )
                native_dpr = 1.0
            else:
                try:
                    native_dpr = float(dpr_r.get("value", 1))
                except (TypeError, ValueError):
                    print(
                        "WARNING: --scale got unexpected devicePixelRatio response {!r}, assuming 1.0".format(dpr_r),
                        file=sys.stderr,
                    )
                    native_dpr = 1.0
            if native_dpr <= 0:
                print(
                    "WARNING: --scale got non-positive devicePixelRatio={}, assuming 1.0".format(native_dpr),
                    file=sys.stderr,
                )
                native_dpr = 1.0
            effective_scale = scale_override / native_dpr
            if "clip" in params:
                params["clip"]["scale"] = effective_scale
            else:
                vp_response = ws_send(ws_url, "Runtime.evaluate", {
                    "expression": "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
                    "returnByValue": True,
                })
                vp = (vp_response or {}).get("result", {}).get("result")
                if not vp or not vp.get("value"):
                    print("ERROR: --scale could not read viewport to build implicit clip", file=sys.stderr)
                    return 1
                try:
                    d = json.loads(vp["value"])
                    params["clip"] = {"x": 0, "y": 0, "width": int(d["w"]), "height": int(d["h"]), "scale": effective_scale}
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                    print("ERROR: --scale viewport parse failed: {}".format(e), file=sys.stderr)
                    return 1
            print("NOTE: --scale {} via clip.scale={:.3f} (native devicePixelRatio={})".format(
                scale_override, effective_scale, native_dpr), file=sys.stderr)

        r = ws_send(ws_url, "Page.captureScreenshot", params)
        data = (r or {}).get("result", {}).get("data", "")
        if not data:
            print("ERROR: empty screenshot", file=sys.stderr)
            return 1
        img = base64.b64decode(data)
        with open(path, "wb") as f:
            f.write(img)
        log("screenshot", channel="cdp", path=path, size=len(img), url=_redact_url(tab.get("url", "?")), clip=bool(clip), scale=scale_override, bind=bind)
        if bind:
            # Bind the capture to its navigation: final URL + loaderId + wall-clock,
            # read over the SAME ws_url as the capture (no tab drift). The loaderId
            # pairs with navigate --wait's printed loader= token: equal → the shot
            # belongs to that navigation; different → something navigated since.
            # ONE ws_send_seq connection for both reads (review pack B: separate
            # cdp_js + ws_send opened two extra connections and widened the
            # url-vs-loader TOCTOU window).
            seq = ws_send_seq(ws_url, [
                ("Runtime.evaluate",
                 {"expression": "JSON.stringify({url: location.href, t: Date.now()})",
                  "returnByValue": True}),
                ("Page.getFrameTree", None),
            ])
            if seq is None:
                bi, loader = {}, "?"
            else:
                ev, ft = seq
                loader = ((ft or {}).get("result", {}).get("frameTree", {})
                          .get("frame", {}) or {}).get("loaderId", "?")
                try:
                    bi = json.loads(((ev.get("result", {}).get("result", {}) or {})
                                     .get("value")) or "{}")
                except (json.JSONDecodeError, TypeError):
                    bi = {}
            bind_line = "BIND url={} loader={} t={}".format(
                bi.get("url", "?"), loader, bi.get("t", "?"))
    else:
        if clip is not None or scale_override is not None:
            print("ERROR: --clip and --scale require CDP (websocket-client unavailable)", file=sys.stderr)
            return 1
        if bind:
            print("ERROR: --bind requires CDP (websocket-client unavailable)",
                  file=sys.stderr)
            return 1
        if not native_screenshot(path):
            print("ERROR: native screenshot failed", file=sys.stderr)
            return 1
        log("screenshot", channel="native", path=path, size=os.path.getsize(path))

    dims = _image_dimensions(path)
    if dims:
        print("{}  {}×{}".format(path, dims[0], dims[1]))
    else:
        print("WARNING: could not parse image dimensions from {}".format(path), file=sys.stderr)
        print(path)
    if bind_line is not None:
        print(bind_line)
    return 0

def cmd_js(args):
    args, ref_val = _pop_num(list(args), "--ref", int, None)
    if ref_val is not None:
        if not args:
            print("Usage: cdp.py js --ref N 'EXPR'")
            return 1
        expr = args[0]
        if not has_websocket():
            print("ERROR: js --ref requires websocket-client", file=sys.stderr)
            return 1
        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        try:
            result = _ref_resolve(ws, ref_val)
            if result is None:
                return 1
            obj_id, tag = result
            cid = 10
            cid += 1
            ws.send(json.dumps({"id": cid, "method": "Runtime.callFunctionOn",
                "params": {"objectId": obj_id, "returnByValue": True,
                    "functionDeclaration": "function(){{ var el=this; return ({}); }}".format(expr)}}))
            r = _recv_for_id(ws, cid)
            if "error" in r:
                print(_REF_STALE_MSG.format(ref_val))
                return 1
            val = (r.get("result", {}).get("result", {}) or {}).get("value")
            if val is not None:
                print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
            else:
                desc = (r.get("result", {}).get("result", {}) or {}).get("description",
                    (r.get("result", {}).get("result", {}) or {}).get("type", "undefined"))
                print(desc)
            log("js", channel="cdp", ref=ref_val, expr_len=len(expr), expr_sha=_sha12(expr))
            return 0
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()
    if not args:
        print("Usage: cdp.py js 'expression'")
        return 1
    expr = args[0]
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js(expr, tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        val = result.get("value")
        if val is not None:
            print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
        else:
            print(result.get("description", result.get("type", "undefined")))
    else:
        val = as_js_main_world(expr)
        if val is not None:
            print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
        else:
            return 1
    log("js", channel=channel(), expr_len=len(expr), expr_sha=_sha12(expr))
    return 0

def normalize_url(url):
    """Bare absolute path to an existing *regular file* → file:// URL (issue #60).

    CDP `Page.navigate` rejects a bare path (`/tmp/x.html`) with
    "Cannot navigate to invalid URL"; viewing a just-created local file is a
    common reason to call this skill, so normalize it. `Path.as_uri()`
    percent-encodes spaces and reserved chars (naive "file://" + path would be
    invalid for paths with spaces, `#`, `?`). Guard on `os.path.isfile` (NOT
    `os.path.exists`) so only regular files normalize — a directory, FIFO,
    socket, or device node (`/dev/null`, an unwritten named pipe) is left
    verbatim: a FIFO would hang Chrome and `/dev/null` renders blank. Everything
    else — http(s)://, file://, host:port forms, non-existent paths, non-files —
    passes through verbatim (CDP surfaces the error rather than us silently
    rewriting it)."""
    if url.startswith("/") and os.path.isfile(url):
        return Path(url).as_uri()
    return url

def cmd_normalize_url(args):
    """Print normalize_url(arg). Single source of truth for the bare-path rule so
    launch.sh can call it instead of re-implementing the guard + as_uri in bash (#60)."""
    if not args:
        print("Usage: cdp.py normalize-url URL", file=sys.stderr)
        return 1
    print(normalize_url(args[0]))
    return 0

NAVIGATE_EVENTS = ("load", "domcontentloaded", "networkidle")

def cmd_navigate(args):
    args = list(args)
    wait_event = None
    if "--wait" in args:
        i = args.index("--wait")
        if i + 1 < len(args) and args[i + 1] in NAVIGATE_EVENTS:
            wait_event = args[i + 1]
            del args[i:i + 2]
        elif (i + 1 < len(args)
              and args[i + 1].lower() in NAVIGATE_EVENTS):
            # case-typo guard (review sweep): "--wait networkIdle" silently
            # defaulting to load would wait for the WRONG event.
            print("ERROR: --wait got {!r} — event names are lowercase "
                  "({})".format(args[i + 1], "|".join(NAVIGATE_EVENTS)),
                  file=sys.stderr)
            return 1
        else:
            wait_event = "load"
            del args[i]
    expect_url = None
    if "--expect-url" in args:
        i = args.index("--expect-url")
        try:
            expect_url = args[i + 1]
        except IndexError:
            print("ERROR: --expect-url needs a substring argument", file=sys.stderr)
            return 1
        if expect_url.startswith("--"):
            # flag-swallow guard (review pack A): "--expect-url --timeout 5"
            # would otherwise consume "--timeout" as the substring.
            print("ERROR: --expect-url needs a substring argument, got the flag "
                  "{!r}".format(expect_url), file=sys.stderr)
            return 1
        del args[i:i + 2]
    args, timeout_s = _pop_num(args, "--timeout", float, 15.0)
    if timeout_s is None:
        return 1
    if not args:
        print("Usage: cdp.py navigate URL [--wait [load|domcontentloaded|networkidle]]"
              " [--expect-url SUBSTR] [--timeout S]")
        return 1
    url = normalize_url(args[0])

    if wait_event is None:
        if expect_url is not None:
            print("ERROR: --expect-url requires --wait (final URL is only known "
                  "after the load settles)", file=sys.stderr)
            return 1
        # legacy fire-and-forget path — byte-identical /look behavior
        if has_websocket():
            tab = get_tab(TARGET)
            if ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url}) is None:
                return 1
        else:
            if not as_navigate(url):
                return 1
        log("navigate", channel=channel(), url=_redact_url(url))
        print("Navigated to " + url)
        return 0

    # verify-core path (SP2): wait for the lifecycle event + final-URL check
    if not has_websocket():
        print("ERROR: navigate --wait requires websocket-client (CDP lifecycle events)",
              file=sys.stderr)
        return 1
    tab = get_tab(TARGET)
    res = ws_navigate_and_wait(tab["webSocketDebuggerUrl"], url, wait_event, timeout_s)
    if res is None:
        # ok= is a strict yes/no vocabulary (dispatcher contract); the detail
        # rides in reason= (Copilot #329 — was ok="transport_fail")
        log("navigate", channel="cdp", url=_redact_url(url), wait=wait_event,
            ok="no", reason="transport_fail")
        return 1
    # Verdict markers go to STDOUT (review pack C: one grammar for the whole
    # verify-core — stdout carries the machine-readable verdict, stderr only
    # tool errors).
    if not res["ok"]:
        print("NAVIGATE_FAIL: {}".format(res["reason"]))
        log("navigate", channel="cdp", url=_redact_url(url), wait=wait_event, ok="no")
        return 1
    final_url = res["final_url"]
    if expect_url is not None and expect_url not in final_url:
        print("NAVIGATE_URL_MISMATCH: expected '{}' in '{}'".format(
            expect_url, final_url))
        log("navigate", channel="cdp", url=_redact_url(url), wait=wait_event,
            ok="no", mismatch="yes")
        return 1
    print("Navigated to {} ({} fired in {}ms, loader={})".format(
        final_url, wait_event, res["elapsed_ms"], res["loader_id"] or "?"))
    log("navigate", channel="cdp", url=_redact_url(url), wait=wait_event,
        elapsed_ms=res["elapsed_ms"], ok="yes")
    return 0

def cmd_open(args):
    if not args:
        print("Usage: cdp.py open URL")
        return 1
    url = normalize_url(args[0])
    try:
        from urllib.request import Request
        req = Request("{}/json/new?{}".format(CDP_BASE, url), method="PUT")
        r = json.loads(urlopen(req, timeout=5).read())
    except (URLError, json.JSONDecodeError, OSError):
        r = cdp_get("/json/new?" + url)
    if not r:
        print("ERROR: could not open tab", file=sys.stderr)
        return 1
    print("Opened {} in tab {}".format(url, r.get("id", "?")[:12]))
    log("open", url=_redact_url(url))
    return 0

def cmd_title(args):
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js("document.title", tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        print(result.get("value", "?"))
    else:
        val = as_js_main_world("document.title")
        print(val or "?")
    log("title", channel=channel())
    return 0

def cmd_html(args):
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js("document.documentElement.outerHTML", tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        html = result.get("value", "")
        print(html)
        # encoded size, not chars; "replace" matches stdout's error policy — a lone
        # UTF-16 surrogate from CDP must not crash the size accounting (#324 P2 ×2)
        log("html", bytes=len(html.encode("utf-8", "replace")))
    else:
        print("ERROR: html requires websocket-client (too large for AppleScript bridge)", file=sys.stderr)
        return 1
    return 0

def cmd_wait(args):
    if not args:
        print("Usage: cdp.py wait SELECTOR [TIMEOUT]")
        return 1
    is_js = "--js" in args
    filtered = [a for a in args if a != "--js"]
    selector = filtered[0] if filtered else ""
    if not selector:
        print("Usage: cdp.py wait [--js] SELECTOR_OR_EXPR [TIMEOUT]")
        return 1
    try:
        timeout = int(filtered[1]) if len(filtered) > 1 else 10
    except ValueError:
        print("ERROR: TIMEOUT must be an integer, got: {}".format(filtered[1]), file=sys.stderr)
        return 1
    if is_js:
        expr = "!!({})".format(selector)
    else:
        expr = "!!document.querySelector({})".format(json.dumps(selector))
    ws_url = get_tab(TARGET)["webSocketDebuggerUrl"] if has_websocket() else None
    start = time.time()
    while time.time() - start < timeout:
        if has_websocket():
            r = cdp_js(expr, ws_url)
            if r is None:
                return 1
            found = r.get("value") is True
        else:
            val = as_js_main_world(expr)
            found = val is True or val == "true"
        if found:
            elapsed = int((time.time() - start) * 1000)
            print("Found '{}' in {}ms".format(selector, elapsed))
            log("wait", channel=channel(), selector=selector, elapsed_ms=elapsed)
            return 0
        time.sleep(0.5)
    print("Timeout: '{}' not found after {}s".format(selector, timeout), file=sys.stderr)
    return 1

# Shared visibility predicate body (review pack D: --visible and --actionable
# used to carry diverging copies; cmd_click's measure keeps its own dict-shaped
# variant — cross-ref there). Expects `el` in scope; returns false when not
# rendered/visible.

# --- AX renderer (§3.1-3.2) ---

INTERACTIVE_ROLES = frozenset({
    "button", "link", "checkbox", "textbox", "combobox", "option",
    "menuitem", "radio", "switch", "tab", "slider", "searchbox",
})

_AX_ATTRS = {
    "disabled": lambda v: "[disabled]" if v is True else None,
    "checked": lambda v: "[checked=mixed]" if v == "mixed" else (
        "[checked]" if v in (True, "true") else None),
    "expanded": lambda v: "[expanded]" if v is True else None,
    "selected": lambda v: "[selected]" if v is True else None,
    "required": lambda v: "[required]" if v is True else None,
    "readonly": lambda v: "[readonly]" if v is True else None,
    "multiline": lambda v: "[multiline]" if v is True else None,
    "invalid": lambda v: "[invalid={}]".format(v) if v and v != "false" else None,
    "level": lambda v: "[level={}]".format(v) if v else None,
}


def _sanitize_name(name, max_len=200):
    import re as _re
    s = _re.sub(r'\s+', ' ', name).strip()
    s = s.replace('"', "'")
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _render_ax_tree(frame_node_lists, max_nodes, raw, shadow_map,
                    frame_urls=None):
    """Pure-function AX renderer. Returns (lines, meta).

    frame_node_lists: list of node-lists, one per frame (main first).
    shadow_map: {backendDOMNodeId: "open"|"closed"} from DOM.getDocument.
    frame_urls: parallel list of frame URLs (empty string for main).
    meta: {nodes, shown, frames, truncated, oopif_count}.
    """
    lines = []
    total_raw_nodes = sum(len(f) for f in frame_node_lists)
    shown = 0
    truncated = False
    iframe_role_count = 0

    for frame_idx, nodes in enumerate(frame_node_lists):
        if not nodes:
            continue
        by_id = {n["nodeId"]: n for n in nodes}
        roots = [n for n in nodes
                 if "parentId" not in n or n["parentId"] not in by_id]
        visited = set()

        if frame_idx > 0 and frame_urls and frame_idx < len(frame_urls):
            lines.append("")
            lines.append("frame: {}".format(frame_urls[frame_idx]))

        def rec(n, depth):
            nonlocal shown, truncated, iframe_role_count
            nid = n["nodeId"]
            if nid in visited:
                return
            visited.add(nid)
            if max_nodes > 0 and shown >= max_nodes:
                if not truncated:
                    truncated = True
                    lines.append("… [truncated: shown {} of {} nodes"
                                 " — re-run with --max-nodes 0]".format(
                                     shown, total_raw_nodes))
                return

            role = (n.get("role") or {}).get("value", "unknown")
            name_raw = ((n.get("name") or {}).get("value") or "").strip()
            name = _sanitize_name(name_raw) if name_raw else ""
            backend_id = n.get("backendDOMNodeId")
            is_shadow_host = backend_id is not None and backend_id in shadow_map

            if role == "Iframe":
                iframe_role_count += 1

            skip = False
            if not raw:
                if n.get("ignored"):
                    skip = True
                elif role == "InlineTextBox":
                    skip = True
                elif role in ("generic", "none", "presentation") and not name:
                    if not is_shadow_host:
                        skip = True
                elif role == "StaticText":
                    if not name:
                        skip = True
                    else:
                        parent = by_id.get(n.get("parentId", ""))
                        if parent:
                            pname = ((parent.get("name") or {}).get("value") or "").strip()
                            if name == _sanitize_name(pname):
                                skip = True
                elif role == "RootWebArea" and not name:
                    skip = True

            child_depth = depth
            if not skip:
                role_out = "text" if role == "StaticText" else role
                line = "  " * depth + "- " + role_out
                if role_out == "text":
                    line += ": " + name
                elif name:
                    line += ' "{}"'.format(name)

                attrs = []
                if raw and n.get("ignored"):
                    attrs.append("[ignored]")
                for prop in (n.get("properties") or []):
                    pname = prop.get("name", "")
                    pval = (prop.get("value") or {}).get("value")
                    fmt = _AX_ATTRS.get(pname)
                    if fmt:
                        a = fmt(pval)
                        if a:
                            attrs.append(a)
                if is_shadow_host:
                    attrs.append("[shadow={}]".format(shadow_map[backend_id]))
                if attrs:
                    line += " " + " ".join(attrs)

                if role in INTERACTIVE_ROLES and backend_id is not None:
                    line += " [ref={}]".format(backend_id)

                val_raw = ((n.get("value") or {}).get("value") or "")
                if val_raw:
                    val = _sanitize_name(str(val_raw))
                    line += ": " + val

                lines.append(line)
                shown += 1
                child_depth = depth + 1

            for cid in (n.get("childIds") or []):
                child = by_id.get(cid)
                if child is not None:
                    rec(child, child_depth)

        for r in roots:
            rec(r, 0)

    frames_walked = len([f for f in frame_node_lists if f])
    oopif_count = max(0, iframe_role_count - (frames_walked - 1)) if frames_walked > 0 else 0

    meta = {
        "nodes": total_raw_nodes,
        "shown": shown,
        "frames": frames_walked,
        "truncated": truncated,
        "oopif_count": oopif_count,
    }
    return lines, meta


_VISIBLE_PRED_JS = ("var r=el.getBoundingClientRect();"
                    "if(r.width<=0||r.height<=0)return false;"
                    "var s=getComputedStyle(el);"
                    "if(s.visibility==='hidden'||s.display==='none'"
                    "||parseFloat(s.opacity||'1')<=0)return false;")

# --- Ref-bridge helpers (§4) ---

_REF_STALE_MSG = "REF_STALE: ref {} not resolvable — re-run ax for fresh refs"

_HIT_TEST_JS = ("function(){"
    "var el=this; el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
    "var r=el.getBoundingClientRect();"
    "var cx=r.left+r.width/2, cy=r.top+r.height/2;"
    "var hit=document.elementFromPoint(cx,cy);"
    "var ok=r.width>0 && r.height>0 && !!hit;"
    "if(ok){ok=(hit===el||el.contains(hit));"
    "if(!ok){var h=el; while(h&&h.getRootNode){var rn=h.getRootNode();"
    "if(rn.host){if(hit===rn.host||rn.host.contains(hit)){ok=true;break;}"
    "h=rn.host;}else break;}}}"
    "return {cx:cx, cy:cy, tag:el.tagName, hittable:ok};}")


def _ref_hit_test(ws, ref):
    """Resolve ref, scroll, hit-test. Returns (cx, cy, tag, hittable, obj_id) or None."""
    call_id = [0]

    def call(method, params):
        call_id[0] += 1
        ws.send(json.dumps({"id": call_id[0], "method": method, "params": params}))
        return _recv_for_id(ws, call_id[0])

    r = call("DOM.getDocument", {})
    if "error" in r:
        print(_REF_STALE_MSG.format(ref))
        return None
    rr = call("DOM.resolveNode", {"backendNodeId": ref})
    if "error" in rr:
        print(_REF_STALE_MSG.format(ref))
        return None
    obj_id = rr.get("result", {}).get("object", {}).get("objectId")
    hr = call("Runtime.callFunctionOn", {
        "objectId": obj_id, "returnByValue": True,
        "functionDeclaration": _HIT_TEST_JS})
    hres = (hr.get("result", {}).get("result", {}) or {}).get("value", {})
    if not isinstance(hres, dict):
        print(_REF_STALE_MSG.format(ref))
        return None
    return hres.get("cx", 0), hres.get("cy", 0), hres.get("tag", "?"), hres.get("hittable", False), obj_id


def _ref_resolve(ws, ref):
    """Resolve ref to objectId + tag. Returns (obj_id, tag) or None (prints REF_STALE)."""
    call_id = [0]

    def call(method, params):
        call_id[0] += 1
        ws.send(json.dumps({"id": call_id[0], "method": method, "params": params}))
        return _recv_for_id(ws, call_id[0])

    call("DOM.getDocument", {})
    rr = call("DOM.resolveNode", {"backendNodeId": ref})
    if "error" in rr:
        print(_REF_STALE_MSG.format(ref))
        return None
    obj_id = rr.get("result", {}).get("object", {}).get("objectId")
    tr = call("Runtime.callFunctionOn", {
        "objectId": obj_id, "returnByValue": True,
        "functionDeclaration": "function(){ return this.tagName; }"})
    tag = (tr.get("result", {}).get("result", {}) or {}).get("value", "?")
    return obj_id, tag


def cmd_assert(args):
    """Verify-core assertion: condition must hold true CONTINUOUSLY for
    --stable ms within --timeout s. Emits ASSERT_PASS/ASSERT_FAIL (stdout) +
    exit 0/1, with flap diagnostics distinguishing flaky from absent.
    Polls at 100ms over ONE websocket connection (flaps shorter than the
    polling interval are invisible — documented in drive SKILL.md)."""
    if not has_websocket():
        print("ERROR: assert requires websocket-client (fine-grained polling)",
              file=sys.stderr)
        return 1
    args, is_js = _pop_flag(args, "--js")
    args, visible = _pop_flag(args, "--visible")
    args, actionable = _pop_flag(args, "--actionable")
    if sum((is_js, visible, actionable)) > 1:
        print("ERROR: --js, --visible and --actionable are mutually exclusive",
              file=sys.stderr)
        return 1
    args, stable_ms = _pop_num(args, "--stable", int, 500)
    if stable_ms is None:
        return 1
    args, timeout_s = _pop_num(args, "--timeout", float, 10.0)
    if timeout_s is None:
        return 1
    args, ref_val = _pop_num(args, "--ref", int, None)
    if ref_val is not None:
        if is_js:
            print("ERROR: --js and --ref are mutually exclusive",
                  file=sys.stderr)
            return 1
        if args:
            print("Usage: cdp.py assert --ref N [--visible|--actionable] "
                  "[--stable MS] [--timeout S]")
            return 1
        if visible:
            ref_pred = (_VISIBLE_PRED_JS + "return true;")
            what = "visible-ref: ref={}".format(ref_val)
        elif actionable:
            ref_pred = (_VISIBLE_PRED_JS +
                "if(el.disabled===true)return false;"
                "var cx=r.left+r.width/2, cy=r.top+r.height/2;"
                "var hit=document.elementFromPoint(cx,cy);"
                "var ok=!!hit&&(hit===el||el.contains(hit));"
                "if(!ok){var h=el; while(h&&h.getRootNode){var rn=h.getRootNode();"
                "if(rn.host){if(hit===rn.host||rn.host.contains(hit)){ok=true;break;}"
                "h=rn.host;}else break;}}"
                "return ok;")
            what = "actionable-ref: ref={}".format(ref_val)
        else:
            ref_pred = "return true;"
            what = "present-ref: ref={}".format(ref_val)
        what_log = what  # ref labels carry no user JS — log as-is (D2 parity)

        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        start = time.time()
        deadline = start + timeout_s
        streak_start = None
        longest_ms = 0.0
        flaps = 0
        ever_true = False
        call_id = 0

        def ref_poll():
            nonlocal call_id
            call_id += 1
            ws.send(json.dumps({"id": call_id, "method": "DOM.resolveNode",
                "params": {"backendNodeId": ref_val}}))
            ws.settimeout(max(0.1, min(1.0, deadline - time.time())))
            try:
                r = _recv_for_id(ws, call_id)
            except websocket.WebSocketTimeoutException:
                return None
            if "error" in r:
                return "stale"
            obj_id = r.get("result", {}).get("object", {}).get("objectId")
            if not obj_id:
                return "stale"
            call_id += 1
            ws.send(json.dumps({"id": call_id, "method": "Runtime.callFunctionOn",
                "params": {"objectId": obj_id, "returnByValue": True,
                    "functionDeclaration": "function(){{ var el=this; {} }}".format(ref_pred)}}))
            try:
                pr = _recv_for_id(ws, call_id)
            except websocket.WebSocketTimeoutException:
                return None
            return (pr.get("result", {}).get("result", {}) or {}).get("value") is True

        try:
            if actionable:
                call_id += 1
                ws.send(json.dumps({"id": call_id, "method": "DOM.scrollIntoViewIfNeeded",
                    "params": {"backendNodeId": ref_val}}))
                sr = _recv_for_id(ws, call_id)
                if "error" in sr:
                    print(_REF_STALE_MSG.format(ref_val))
                    return 1
            while True:
                val = ref_poll()
                now = time.time()
                if val == "stale":
                    print(_REF_STALE_MSG.format(ref_val))
                    return 1
                if val is not None:
                    if val:
                        ever_true = True
                        if streak_start is None:
                            streak_start = now
                        held_ms = (now - streak_start) * 1000
                        longest_ms = max(longest_ms, held_ms)
                        if held_ms >= stable_ms:
                            total = int((now - start) * 1000)
                            print("ASSERT_PASS {} held {}ms (total {}ms{})".format(
                                what, int(held_ms), total,
                                ", flapped {}x first".format(flaps) if flaps else ""))
                            log("assert", what=what_log[:60], result="pass",
                                held_ms=int(held_ms), flaps=flaps)
                            return 0
                    else:
                        if streak_start is not None:
                            flaps += 1
                        streak_start = None
                if now >= deadline:
                    break
                time.sleep(0.1)
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()
        if not ever_true:
            reason = "never true within {}s".format(timeout_s)
        elif flaps:
            reason = ("unstable: flapped {}x (longest true streak {}ms < stable {}ms)"
                      .format(flaps, int(longest_ms), stable_ms))
        else:
            reason = "true but held only {}ms < stable {}ms at timeout".format(
                int(longest_ms), stable_ms)
        print("ASSERT_FAIL {} — {}".format(what, reason))
        log("assert", what=what_log[:60], result="fail", flaps=flaps)
        return 1
    if not args:
        print("Usage: cdp.py assert [--js] EXPR_OR_SELECTOR [--visible|--actionable] "
              "[--stable MS] [--timeout S]")
        return 1
    selector = args[0]
    if is_js:
        expr = "!!({})".format(selector)
        what = "js: " + selector
    elif visible:
        expr = ("(function(){{var el=document.querySelector({sel});"
                "if(!el)return false;" + _VISIBLE_PRED_JS +
                "return true;}})()").format(sel=json.dumps(selector))
        what = "visible: " + selector
    elif actionable:
        # R1-F3 / spec §4.3 actionability: visible + enabled + hit-test — the
        # center of the box must actually receive events (same point-on-target
        # semantics as cmd_click's hittable check). visible-but-occluded or
        # disabled elements are NOT actionable. The scroll-into-view happens
        # ONCE before the polling loop (review pack B: scrolling inside the
        # 100ms loop fought the human's scroll in co-pilot headful and thrashed
        # layout) — the polled expression itself is scroll-free.
        expr = ("(function(){{var el=document.querySelector({sel});"
                "if(!el)return false;" + _VISIBLE_PRED_JS +
                "if(el.disabled===true)return false;"
                "var cx=r.left+r.width/2, cy=r.top+r.height/2;"
                "var hit=document.elementFromPoint(cx,cy);"
                "return !!hit&&(hit===el||el.contains(hit));}})()").format(
                    sel=json.dumps(selector))
        what = "actionable: " + selector
    else:
        expr = "!!document.querySelector({})".format(json.dumps(selector))
        what = "present: " + selector
    # D2 (#322): --js `what` carries user JS source — the log gets a hashed twin;
    # selectors are not secret-bearing and stay readable. stdout keeps `what`.
    what_log = "js:len={},sha={}".format(len(selector), _sha12(selector)) if is_js else what

    tab = get_tab(TARGET)
    import websocket
    try:
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    start = time.time()
    deadline = start + timeout_s
    streak_start = None
    longest_ms = 0.0
    flaps = 0
    ever_true = False
    call_id = 0

    def poll(pexpr):
        """One Runtime.evaluate with a deadline-bounded recv budget (review
        pack A: a tab with previously-enabled domains streams events on this
        connection; an unbounded recv would blow straight through --timeout).
        Returns the response dict, or None when the recv budget ran out (the
        outer loop re-checks the deadline)."""
        nonlocal call_id
        call_id += 1
        ws.settimeout(max(0.1, min(1.0, deadline - time.time())))
        ws.send(json.dumps({"id": call_id, "method": "Runtime.evaluate",
                            "params": {"expression": pexpr, "returnByValue": True}}))
        try:
            return _recv_for_id(ws, call_id)
        except websocket.WebSocketTimeoutException:
            return None

    try:
        if actionable:
            # scroll-once (cmd_click's measure line, same arguments)
            poll("(function(){{var el=document.querySelector({sel});"
                 "if(el)el.scrollIntoView({{block:'center',inline:'center',"
                 "behavior:'instant'}});return true;}})()".format(
                     sel=json.dumps(selector)))
        while True:
            r = poll(expr)
            now = time.time()  # the moment we KNOW the value (post-recv) —
            # streak/deadline math on pre-send stamps under/over-counted by
            # one round-trip (review pack B)
            if r is not None:
                val = (r.get("result", {}).get("result", {}) or {}).get("value") is True
                if val:
                    ever_true = True
                    if streak_start is None:
                        streak_start = now
                    held_ms = (now - streak_start) * 1000
                    longest_ms = max(longest_ms, held_ms)
                    if held_ms >= stable_ms:
                        total = int((now - start) * 1000)
                        print("ASSERT_PASS {} held {}ms (total {}ms{})".format(
                            what, int(held_ms), total,
                            ", flapped {}x first".format(flaps) if flaps else ""))
                        log("assert", what=what_log[:60], result="pass",
                            held_ms=int(held_ms), flaps=flaps)
                        return 0
                else:
                    if streak_start is not None:
                        flaps += 1
                    streak_start = None
            if now >= deadline:
                break
            time.sleep(0.1)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()
    if not ever_true:
        reason = "never true within {}s".format(timeout_s)
    elif flaps:
        reason = ("unstable: flapped {}x (longest true streak {}ms < stable {}ms)"
                  .format(flaps, int(longest_ms), stable_ms))
    else:
        reason = "true but held only {}ms < stable {}ms at timeout".format(
            int(longest_ms), stable_ms)
    print("ASSERT_FAIL {} — {}".format(what, reason))
    log("assert", what=what_log[:60], result="fail", flaps=flaps)
    return 1

def cmd_reload(args):
    if has_websocket():
        tab = get_tab(TARGET)
        if ws_send(tab["webSocketDebuggerUrl"], "Page.reload", {"ignoreCache": True}) is None:
            return 1
    else:
        if not as_reload():
            return 1
    log("reload", channel=channel())
    print("Reloaded")
    return 0

def cmd_click(args):
    args, require_trusted = _pop_flag(args, "--require-trusted")
    args, ref_val = _pop_num(args, "--ref", int, None)
    if ref_val is not None:
        if args:
            print("Usage: cdp.py click --ref N (no selector with --ref)")
            return 1
        if not has_websocket():
            print("ERROR: click --ref requires websocket-client", file=sys.stderr)
            return 1
        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        try:
            result = _ref_hit_test(ws, ref_val)
            if result is None:
                return 1
            cx, cy, tag, hittable, obj_id = result
            if not hittable:
                print("CLICK_REF_NOT_HITTABLE: ref {} (hidden/occluded)"
                      " — refusing dispatch".format(ref_val))
                return 1
            call_id = 100
            for evt_type, buttons in [("mousePressed", 1), ("mouseReleased", 0)]:
                call_id += 1
                ws.send(json.dumps({"id": call_id, "method": "Input.dispatchMouseEvent",
                    "params": {"type": evt_type, "x": cx, "y": cy,
                               "button": "left", "buttons": buttons, "clickCount": 1}}))
                _recv_for_id(ws, call_id)
            print("clicked {} (trusted, ref={})".format(tag, ref_val))
            log("click", channel="cdp", ref=ref_val, trusted="yes")
            return 0
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()
    if not args:
        print("Usage: cdp.py click SELECTOR [--require-trusted]")
        return 1
    selector = args[0]
    sel = json.dumps(selector)
    # untrusted el.click() expression — shared by the AppleScript channel and the
    # websocket not-hittable fallback (both deliberately untrusted).
    click_expr = ("(function(){ var el=document.querySelector(" + sel + ");"
                  " if(!el) return 'NOT_FOUND'; el.click(); return 'clicked '+el.tagName })()")

    # AppleScript channel: no CDP Input domain → untrusted el.click() only.
    if not has_websocket():
        if require_trusted:
            print("CLICK_REQUIRE_TRUSTED_FAIL: AppleScript channel cannot dispatch "
                  "trusted input — use the CDP/websocket channel")
            return 1
        val = as_js_main_world(click_expr)
        if val is None:
            return 1
        if val == "NOT_FOUND":
            print("ERROR: '{}' not found".format(selector), file=sys.stderr)
            return 1
        print(val + " (untrusted: AppleScript channel)")
        print("WARN: AppleScript channel cannot grant user activation — "
              "trusted click needs the CDP/websocket channel", file=sys.stderr)
        log("click", channel=channel(), selector=selector, trusted="no")
        return 0

    # websocket channel: capture ONE ws_url (from the pinned get_tab(TARGET)) and use
    # it for measure, fallback, AND the trusted press+release — same target, one
    # connection. cdp_js is NOT used here: it issues a single Runtime.evaluate and
    # can't express the press+release sequence (that needs ws_send_seq on one ws);
    # threading the captured ws_url keeps measure and dispatch on the same tab (R1-F1).
    # Mirrors cmd_screenshot's --scale DPR read (same-ws_url pattern).
    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]

    measure = ("(function(){ var el=document.querySelector(" + sel + ");"
               " if(!el) return {found:false};"
               " el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
               " var r=el.getBoundingClientRect();"
               " var cx=r.left+r.width/2, cy=r.top+r.height/2;"
               " var hit=document.elementFromPoint(cx,cy);"
               " return {found:true,cx:cx,cy:cy,w:r.width,h:r.height,"
               " onTarget:(!!hit&&(hit===el||el.contains(hit))),tag:el.tagName}; })()")
    mr = ws_send(ws_url, "Runtime.evaluate", {"expression": measure, "returnByValue": True})
    if mr is None:
        return 1
    meas = (mr.get("result", {}).get("result") or {}).get("value")
    if not isinstance(meas, dict):
        print("ERROR: unexpected measure result for '{}'".format(selector), file=sys.stderr)
        return 1
    if not meas.get("found"):
        print("ERROR: '{}' not found".format(selector), file=sys.stderr)
        return 1
    tag = meas.get("tag", "?")
    hittable = meas.get("w", 0) > 0 and meas.get("h", 0) > 0 and meas.get("onTarget")

    if not hittable:
        if require_trusted:
            # R2-U: the verify-core reads the trust signal — refuse the untrusted
            # fallback outright (exit 1, NO click). Verdict marker on STDOUT
            # (review pack C grammar).
            print("CLICK_REQUIRE_TRUSTED_FAIL: '{}' not hittable (hidden/occluded/"
                  "off-viewport) — refusing untrusted fallback".format(selector))
            log("click", channel=channel(), selector=selector, trusted="refused")
            return 1
        # zero-box / occluded / off-viewport → untrusted el.click() fallback (SAME ws_url).
        fr = ws_send(ws_url, "Runtime.evaluate", {"expression": click_expr, "returnByValue": True})
        if fr is None:
            return 1
        fr_r = fr.get("result", {}).get("result")
        if not isinstance(fr_r, dict):
            print("ERROR: '{}' fallback click could not be confirmed (malformed CDP result)".format(selector), file=sys.stderr)
            return 1
        if fr_r.get("value") == "NOT_FOUND":
            print("ERROR: '{}' not found".format(selector), file=sys.stderr)
            return 1
        print("clicked {} (fallback: el.click, untrusted)".format(tag))
        print("WARN: '{}' not hittable (hidden/occluded/off-viewport) — fell back to "
              "el.click(); user activation NOT granted".format(selector), file=sys.stderr)
        log("click", channel=channel(), selector=selector, trusted="no", fallback="yes")
        return 0

    # hittable → trusted press+release at the box center, SAME ws_url (one connection).
    cx, cy = meas["cx"], meas["cy"]
    seq = ws_send_seq(ws_url, [
        ("Input.dispatchMouseEvent",
         {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "buttons": 1, "clickCount": 1}),
        ("Input.dispatchMouseEvent",
         {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "buttons": 0, "clickCount": 1}),
    ])
    if seq is None:
        return 1
    print("clicked {} (trusted)".format(tag))
    log("click", channel=channel(), selector=selector, trusted="yes")
    return 0

def cmd_fill(args):
    args, ref_val = _pop_num(list(args), "--ref", int, None)
    if ref_val is not None:
        if len(args) != 1:
            print("Usage: cdp.py fill --ref N VALUE")
            return 1
        value = args[0]
        if not has_websocket():
            print("ERROR: fill --ref requires websocket-client", file=sys.stderr)
            return 1
        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        try:
            result = _ref_resolve(ws, ref_val)
            if result is None:
                return 1
            obj_id, tag = result
            call_id = 1
            call_id += 1
            ws.send(json.dumps({"id": call_id, "method": "Runtime.callFunctionOn",
                "params": {"objectId": obj_id, "returnByValue": True,
                    "functionDeclaration": "function(v){ this.value=v;"
                        " this.dispatchEvent(new Event('input',{bubbles:true}));"
                        " this.dispatchEvent(new Event('change',{bubbles:true}));"
                        " return 'filled ' + this.tagName; }",
                    "arguments": [{"value": value}]}}))
            r = _recv_for_id(ws, call_id)
            if "error" in r:
                print(_REF_STALE_MSG.format(ref_val))
                return 1
            val = (r.get("result", {}).get("result", {}) or {}).get("value", "?")
            print(val)
            log("fill", channel="cdp", ref=ref_val)
            return 0
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()
    if len(args) < 2:
        print("Usage: cdp.py fill SELECTOR VALUE")
        return 1
    selector, value = args[0], args[1]
    expr = "(function(){{ var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.value={val}; el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}})); return 'filled ' + el.tagName }})()".format(
        sel=json.dumps(selector), val=json.dumps(value))
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js(expr, tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        val = result.get("value", "?")
    else:
        val = as_js_main_world(expr)
        if val is None:
            return 1
    if val == "NOT_FOUND":
        print("ERROR: '{}' not found".format(selector), file=sys.stderr)
        return 1
    print(val)
    log("fill", channel=channel(), selector=selector)
    return 0

def cmd_console(args):
    args, gate = _pop_flag(args, "--gate")
    if not has_websocket():
        print("ERROR: console requires websocket-client (CDP Console domain)", file=sys.stderr)
        return 1
    tab = get_tab(TARGET)
    import websocket
    try:
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    console_errors = 0
    exceptions = 0
    log_errors = 0
    try:
        ws.send(json.dumps({"id": 1, "method": "Console.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
        ws.recv()
        # Log domain (review pack A, empirically confirmed): browser-generated
        # errors — CORS blocks, CSP violations, net::ERR_* — surface ONLY as
        # Log.entryAdded, never via Console/Runtime. Without this the gate
        # green-lights a page whose auth fetch was CORS-rejected.
        ws.send(json.dumps({"id": 4, "method": "Log.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {
            "expression": "console.log('__CDP_PING__')"
        }}))
        deadline = time.time() + 3
        messages = []
        while time.time() < deadline:
            ws.settimeout(1)
            try:
                msg = json.loads(ws.recv())
                if msg.get("method") == "Console.messageAdded":
                    entry = msg.get("params", {}).get("message", {})
                    text = entry.get("text", "")
                    if text != "__CDP_PING__":
                        level = entry.get("level", "log")
                        messages.append("[{}] {}".format(level, text))
                        if level == "error":
                            console_errors += 1
                elif msg.get("method") == "Log.entryAdded":
                    entry = msg.get("params", {}).get("entry", {})
                    level = entry.get("level", "info")
                    src = entry.get("source", "?")
                    # entry.url names the failing resource — without it a network
                    # 404/blocked line is undiagnosable (dogfood #2, VRHOT TTS).
                    url = entry.get("url", "")
                    messages.append("[log:{}:{}] {}{}".format(
                        src, level, entry.get("text", ""),
                        " ({})".format(url) if url else ""))
                    if level == "error":
                        log_errors += 1
                elif msg.get("method") == "Runtime.exceptionThrown":
                    exc = msg.get("params", {}).get("exceptionDetails") or {}
                    text = exc.get("text", "")
                    ex_obj = exc.get("exception") or {}
                    desc = ex_obj.get("description") or ex_obj.get("value") or ""
                    line_num = exc.get("lineNumber", 0) + 1
                    col_num = exc.get("columnNumber", 0) + 1
                    url = exc.get("url") or ""
                    if url and "/" in url:
                        loc = "{}:{}:{}".format(url.rsplit("/", 1)[-1], line_num, col_num)
                    else:
                        loc = "line {}:{}".format(line_num, col_num)
                    messages.append("[exception] {} — {}".format(desc or text or "(no description)", loc))
                    exceptions += 1
            except websocket.WebSocketTimeoutException:
                break
            except (json.JSONDecodeError, OSError) as e:
                print("console recv error: {}".format(e), file=sys.stderr)
                break
    finally:
        ws.close()
    if messages:
        print("\n".join(messages))
    else:
        print("(no console messages)")
    error_count = exceptions + console_errors + log_errors
    log("console", count=len(messages), gate=("yes" if gate else "no"),
        errors=error_count)
    if gate:
        # Verdict markers on STDOUT (review pack C: one grammar across the
        # verify-core); the breakdown names which leg fired — the legs have
        # different reliability guarantees (exceptions retro / console+log live).
        if error_count:
            print("CONSOLE_GATE_FAIL: {} ({} exception(s), {} console, {} log)"
                  .format(error_count, exceptions, console_errors, log_errors))
            return 1
        print("CONSOLE_GATE_OK")
    return 0

def cmd_network(args):
    if not has_websocket():
        print("ERROR: network requires websocket-client (CDP Network domain)", file=sys.stderr)
        return 1
    tab = get_tab(TARGET)
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}}))
        ws.recv()
        deadline = time.time() + 5
        requests = []
        while time.time() < deadline:
            ws.settimeout(1)
            try:
                msg = json.loads(ws.recv())
                if msg.get("method") == "Network.responseReceived":
                    resp = msg.get("params", {}).get("response", {})
                    requests.append("{} {} {}".format(
                        resp.get("status", "?"),
                        resp.get("mimeType", "?")[:20],
                        resp.get("url", "?")[:80]))
            except websocket.WebSocketTimeoutException:
                break
            except (json.JSONDecodeError, OSError) as e:
                print("network recv error: {}".format(e), file=sys.stderr)
                break
        ws.send(json.dumps({"id": 3, "method": "Network.disable"}))
        ws.recv()
    finally:
        ws.close()
    if requests:
        print("\n".join(requests))
    else:
        print("(no network activity captured)")
    log("network", count=len(requests))
    return 0

def cmd_pdf(args):
    if not has_websocket():
        print("ERROR: pdf requires websocket-client (CDP Page.printToPDF)", file=sys.stderr)
        return 1
    path = args[0] if args else "/tmp/jaine-page.pdf"
    tab = get_tab(TARGET)
    r = ws_send(tab["webSocketDebuggerUrl"], "Page.printToPDF", {
        "landscape": False, "printBackground": True,
    })
    data = (r or {}).get("result", {}).get("data", "")
    if not data:
        print("ERROR: empty PDF", file=sys.stderr)
        return 1
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    print(path)
    log("pdf", path=path)
    return 0

def cmd_viewport(args):
    if len(args) < 2:
        print("Usage: cdp.py viewport WIDTH HEIGHT")
        return 1
    try:
        w, h = int(args[0]), int(args[1])
    except ValueError:
        print("ERROR: WIDTH and HEIGHT must be numbers", file=sys.stderr)
        return 1
    if has_websocket():
        tab = get_tab(TARGET)
        if ws_send(tab["webSocketDebuggerUrl"], "Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False,
        }) is None:
            return 1
    else:
        if osascript('tell application "{}" to set bounds of window 1 to {{0, 0, {}, {}}}'.format(
            CHROME_APP, w, h)) is None:
            return 1
    print("Viewport set to {}x{}".format(w, h))
    log("viewport", width=w, height=h)
    return 0

def _applescript_bounds(raw):
    """Normalize AppleScript `get bounds of window 1` (x1, y1, x2, y2) to the CDP
    stdout contract `left,top,width,height`. Raises ValueError on malformed input
    (fail-loud — cdp.py's no-silent-fallback principle)."""
    tokens = raw.split(",")
    if len(tokens) != 4:
        raise ValueError("expected 4 bounds values, got {}: {!r}".format(len(tokens), raw))
    x1, y1, x2, y2 = (int(t.strip()) for t in tokens)
    return "{},{},{},{}".format(x1, y1, x2 - x1, y2 - y1)


def cmd_window(args):
    action = args[0] if args else "bounds"
    if action == "bounds":
        if has_websocket():
            tab = get_tab(TARGET)
            r = ws_send(tab["webSocketDebuggerUrl"], "Browser.getWindowForTarget",
                        {"targetId": tab["id"]})
            if r is None:
                return 1
            result = r.get("result") if isinstance(r, dict) else None
            b = result.get("bounds") if isinstance(result, dict) else None
            if not isinstance(b, dict) or not all(k in b for k in ("left", "top", "width", "height")):
                print("ERROR: unexpected getWindowForTarget response: {}".format(r), file=sys.stderr)
                return 1
            print("{},{},{},{}".format(b["left"], b["top"], b["width"], b["height"]))
        else:
            r = osascript('tell application "{}" to get bounds of window 1'.format(CHROME_APP))
            if r is None:
                return 1
            try:
                print(_applescript_bounds(r))
            except ValueError as e:
                print("ERROR: cannot parse AppleScript window bounds: {}".format(e), file=sys.stderr)
                return 1
    elif action == "upper":
        if osascript('tell application "{}" to set bounds of window 1 to {{0, -1080, 3840, 0}}'.format(CHROME_APP)) is None:
            return 1
        print("Moved to upper monitor")
    elif action == "lower":
        if osascript('tell application "{}" to set bounds of window 1 to {{0, 0, 3840, 1080}}'.format(CHROME_APP)) is None:
            return 1
        print("Moved to lower monitor")
    elif action == "activate":
        if osascript('tell application "{}" to activate'.format(CHROME_APP)) is None:
            return 1
        print("Activated")
    else:
        print("Usage: cdp.py window [bounds|upper|lower|activate]")
        return 1
    log("window", action=action)
    return 0

_DND_JS = """function(target){
  var dt = new DataTransfer();
  var fire = function(el, type){
    var ev = new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt});
    el.dispatchEvent(ev);
  };
  fire(this, 'dragstart');
  fire(target, 'dragenter');
  fire(target, 'dragover');
  fire(target, 'drop');
  fire(this, 'dragend');
  return 'dnd-dispatched';
}"""


def _measure_selector(ws_url, selector):
    """Measure element for hover/drag. Returns dict or None."""
    sel = json.dumps(selector)
    measure = ("(function(){ var el=document.querySelector(" + sel + ");"
               " if(!el) return {found:false};"
               " el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
               " var r=el.getBoundingClientRect();"
               " var cx=r.left+r.width/2, cy=r.top+r.height/2;"
               " var hit=document.elementFromPoint(cx,cy);"
               " return {found:true,cx:cx,cy:cy,tag:el.tagName,"
               " hittable:(r.width>0 && r.height>0 && !!hit && (hit===el||el.contains(hit)))}; })()")
    mr = ws_send(ws_url, "Runtime.evaluate", {"expression": measure, "returnByValue": True})
    if mr is None:
        return None
    return (mr.get("result", {}).get("result") or {}).get("value")


def cmd_drag(args):
    """Drag element — selector pair or --ref pair (§4.7). websocket-only."""
    if not has_websocket():
        print("ERROR: drag requires websocket-client (CDP Input domain)",
              file=sys.stderr)
        return 1
    args, html5 = _pop_flag(list(args), "--html5")
    args, cancel = _pop_flag(args, "--cancel")
    if html5 and cancel:
        print("Usage: --cancel and --html5 are mutually exclusive")
        return 1
    args, ref_val = _pop_num(args, "--ref", int, None)
    args, to_ref = _pop_num(args, "--to-ref", int, None)

    if ref_val is not None:
        if args or to_ref is None:
            print("Usage: cdp.py drag --ref N --to-ref M [--html5 | --cancel]")
            return 1
        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        cid = [0]

        def call(method, params):
            cid[0] += 1
            ws.send(json.dumps({"id": cid[0], "method": method, "params": params}))
            return _recv_for_id(ws, cid[0])

        try:
            call("DOM.getDocument", {})
            if html5:
                sr = call("DOM.resolveNode", {"backendNodeId": ref_val})
                if "error" in sr:
                    print(_REF_STALE_MSG.format(ref_val)); return 1
                src_oid = sr.get("result", {}).get("object", {}).get("objectId")
                if not src_oid:
                    print(_REF_STALE_MSG.format(ref_val)); return 1
                dr = call("DOM.resolveNode", {"backendNodeId": to_ref})
                if "error" in dr:
                    print(_REF_STALE_MSG.format(to_ref)); return 1
                dst_oid = dr.get("result", {}).get("object", {}).get("objectId")
                if not dst_oid:
                    print(_REF_STALE_MSG.format(to_ref)); return 1
                r = call("Runtime.callFunctionOn", {
                    "objectId": src_oid, "returnByValue": True,
                    "functionDeclaration": _DND_JS,
                    "arguments": [{"objectId": dst_oid}]})
                if "error" in r:
                    print(_REF_STALE_MSG.format(ref_val)); return 1
                sr2 = call("Runtime.callFunctionOn", {
                    "objectId": src_oid, "returnByValue": True,
                    "functionDeclaration": "function(){ return this.tagName; }"})
                dr2 = call("Runtime.callFunctionOn", {
                    "objectId": dst_oid, "returnByValue": True,
                    "functionDeclaration": "function(){ return this.tagName; }"})
                st = (sr2.get("result", {}).get("result", {}) or {}).get("value", "?")
                dt = (dr2.get("result", {}).get("result", {}) or {}).get("value", "?")
                print("dragged {} -> {} (html5)".format(st, dt))
                log("drag", mode="html5", ref=ref_val, to_ref=to_ref)
                return 0
            # mouse-series ref path
            src_ht = _ref_hit_test(ws, ref_val)
            if src_ht is None:
                return 1
            sx, sy, s_tag, s_hit, _ = src_ht
            if not s_hit:
                print("DRAG_NOT_HITTABLE: src ref {} (hidden/occluded)".format(ref_val))
                return 1
            dst_ht = _ref_hit_test(ws, to_ref)
            if dst_ht is None:
                return 1
            dx, dy, d_tag, d_hit, _ = dst_ht
            if not d_hit and not cancel:
                print("DRAG_NOT_HITTABLE: dst ref {} (hidden/occluded)".format(to_ref))
                return 1
            return _drag_mouse_dispatch(ws, cid, sx, sy, dx, dy, s_tag, d_tag, cancel, ref_val)
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()

    if to_ref is not None:
        print("Usage: --to-ref requires --ref (homogeneous pair)")
        return 1
    if len(args) < 2:
        print("Usage: cdp.py drag SRC_SEL DST_SEL [--html5 | --cancel]")
        return 1
    src_sel, dst_sel = args[0], args[1]
    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]

    if html5:
        sel_s = json.dumps(src_sel)
        sel_d = json.dumps(dst_sel)
        expr = ("(function(){{ var s=document.querySelector({ss});"
                " var d=document.querySelector({ds});"
                " if(!s) return {{err:'src not found'}};"
                " if(!d) return {{err:'dst not found'}};"
                " var dt=new DataTransfer();"
                " function fire(el,t){{el.dispatchEvent(new DragEvent(t,"
                "{{bubbles:true,cancelable:true,dataTransfer:dt}}));}}"
                " fire(s,'dragstart');fire(d,'dragenter');fire(d,'dragover');"
                " fire(d,'drop');fire(s,'dragend');"
                " return {{stag:s.tagName,dtag:d.tagName}}; }})()").format(
                    ss=sel_s, ds=sel_d)
        r = ws_send(ws_url, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if r is None:
            return 1
        val = (r.get("result", {}).get("result") or {}).get("value", {})
        if isinstance(val, dict) and "err" in val:
            print("ERROR: {}".format(val["err"]), file=sys.stderr)
            return 1
        st = val.get("stag", "?") if isinstance(val, dict) else "?"
        dt_tag = val.get("dtag", "?") if isinstance(val, dict) else "?"
        print("dragged {} -> {} (html5)".format(st, dt_tag))
        log("drag", mode="html5", src=src_sel, dst=dst_sel)
        return 0

    src_m = _measure_selector(ws_url, src_sel)
    if not isinstance(src_m, dict) or not src_m.get("found"):
        print("ERROR: src '{}' not found".format(src_sel), file=sys.stderr)
        return 1
    dst_m = _measure_selector(ws_url, dst_sel)
    if not isinstance(dst_m, dict) or not dst_m.get("found"):
        print("ERROR: dst '{}' not found".format(dst_sel), file=sys.stderr)
        return 1
    if not src_m.get("hittable"):
        print("DRAG_NOT_HITTABLE: src '{}' (hidden/occluded)".format(src_sel))
        return 1
    if not dst_m.get("hittable") and not cancel:
        print("DRAG_NOT_HITTABLE: dst '{}' (hidden/occluded)".format(dst_sel))
        return 1

    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    cid = [0]
    try:
        return _drag_mouse_dispatch(ws, cid,
            src_m["cx"], src_m["cy"], dst_m["cx"], dst_m["cy"],
            src_m.get("tag", "?"), dst_m.get("tag", "?"), cancel)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()


def _drag_mouse_dispatch(ws, cid, sx, sy, dx, dy, s_tag, d_tag, cancel, ref=None):
    """Shared mouse-series drag dispatch (default + --cancel)."""
    def send(method, params):
        cid[0] += 1
        ws.send(json.dumps({"id": cid[0], "method": method, "params": params}))
        _recv_for_id(ws, cid[0])

    send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": sx, "y": sy})
    send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": sx, "y": sy,
         "button": "left", "buttons": 1, "clickCount": 1})

    steps = 5
    end_step = steps // 2 if cancel else steps
    for i in range(1, end_step + 1):
        mx = sx + (dx - sx) * i / steps
        my = sy + (dy - sy) * i / steps
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": mx, "y": my,
             "buttons": 1})

    if cancel:
        send("Input.dispatchKeyEvent", {"type": "rawKeyDown",
             "windowsVirtualKeyCode": 27, "code": "Escape", "key": "Escape"})
        send("Input.dispatchKeyEvent", {"type": "keyUp",
             "windowsVirtualKeyCode": 27, "code": "Escape", "key": "Escape"})
        send("Input.dispatchMouseEvent", {"type": "mouseReleased",
             "x": mx, "y": my, "button": "left", "buttons": 0, "clickCount": 1})
        print("DRAG_CANCELLED {} (esc)".format(s_tag))
        log("drag", mode="cancel", src_tag=s_tag)
        return 0

    send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": dx, "y": dy,
         "button": "left", "buttons": 0, "clickCount": 1})
    print("dragged {} -> {} (mouse)".format(s_tag, d_tag))
    log("drag", mode="mouse", src_tag=s_tag, dst_tag=d_tag)
    return 0


def cmd_hover(args):
    """Hover over element — selector or --ref (§4.6). websocket-only."""
    if not has_websocket():
        print("ERROR: hover requires websocket-client (CDP Input domain)",
              file=sys.stderr)
        return 1
    args, ref_val = _pop_num(list(args), "--ref", int, None)
    if ref_val is not None:
        if args:
            print("Usage: cdp.py hover --ref N (no selector with --ref)")
            return 1
        tab = get_tab(TARGET)
        import websocket
        try:
            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
        except (websocket.WebSocketException, OSError, ConnectionError) as e:
            print("WebSocket connect failed: {}".format(e), file=sys.stderr)
            return 1
        try:
            result = _ref_hit_test(ws, ref_val)
            if result is None:
                return 1
            cx, cy, tag, hittable, obj_id = result
            if not hittable:
                print("HOVER_NOT_HITTABLE: ref {} (hidden/occluded)".format(ref_val))
                return 1
            cid = 200
            cid += 1
            ws.send(json.dumps({"id": cid, "method": "Input.dispatchMouseEvent",
                "params": {"type": "mouseMoved", "x": cx, "y": cy}}))
            _recv_for_id(ws, cid)
            print("hovered {} (ref={})".format(tag, ref_val))
            log("hover", channel="cdp", ref=ref_val)
            return 0
        except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
            print("WebSocket I/O error: {}".format(e), file=sys.stderr)
            return 1
        finally:
            ws.close()
    if not args:
        print("Usage: cdp.py hover SELECTOR | hover --ref N")
        return 1
    selector = args[0]
    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]
    meas = _measure_selector(ws_url, selector)
    if not isinstance(meas, dict) or not meas.get("found"):
        print("ERROR: '{}' not found".format(selector), file=sys.stderr)
        return 1
    tag = meas.get("tag", "?")
    hittable = meas.get("hittable", False)
    if not hittable:
        print("HOVER_NOT_HITTABLE: '{}' (hidden/occluded)".format(selector))
        return 1
    cx, cy = meas["cx"], meas["cy"]
    hr = ws_send(ws_url, "Input.dispatchMouseEvent",
                 {"type": "mouseMoved", "x": cx, "y": cy})
    if hr is None:
        return 1
    print("hovered {}".format(tag))
    log("hover", channel="cdp", selector=selector)
    return 0


KEYDEFS = {
    "Enter": {"windowsVirtualKeyCode": 13, "code": "Enter", "key": "Enter", "text": "\r"},
    "Escape": {"windowsVirtualKeyCode": 27, "code": "Escape", "key": "Escape"},
    "Tab": {"windowsVirtualKeyCode": 9, "code": "Tab", "key": "Tab"},
    "ArrowDown": {"windowsVirtualKeyCode": 40, "code": "ArrowDown", "key": "ArrowDown"},
    "ArrowUp": {"windowsVirtualKeyCode": 38, "code": "ArrowUp", "key": "ArrowUp"},
}


def cmd_key(args):
    """Send keyboard event to a ref-focused element (§4.5). Ref-only."""
    if not has_websocket():
        print("ERROR: key requires websocket-client (CDP Input domain)",
              file=sys.stderr)
        return 1
    args, ref_val = _pop_num(list(args), "--ref", int, None)
    if ref_val is None:
        print("Usage: cdp.py key --ref N KEY\n"
              "Supported keys: {}".format(", ".join(sorted(KEYDEFS))))
        return 1
    if not args:
        print("Usage: cdp.py key --ref N KEY\n"
              "Supported keys: {}".format(", ".join(sorted(KEYDEFS))))
        return 1
    key_name = args[0]
    if key_name not in KEYDEFS:
        print("Unknown key: {}. Supported: {}".format(
            key_name, ", ".join(sorted(KEYDEFS))))
        return 1

    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    call_id = 0

    def call(method, params):
        nonlocal call_id
        call_id += 1
        ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        r = _recv_for_id(ws, call_id)
        if "error" in r:
            return None
        return r.get("result", {})

    try:
        call("DOM.getDocument", {})
        fr = call("DOM.focus", {"backendNodeId": ref_val})
        if fr is None:
            print(_REF_STALE_MSG.format(ref_val))
            return 1
        kd = KEYDEFS[key_name]
        down = dict(kd, type="rawKeyDown")
        text_char = down.pop("text", None)
        call("Input.dispatchKeyEvent", down)
        if text_char:
            call("Input.dispatchKeyEvent", {"type": "char", "text": text_char})
        up = {k: v for k, v in kd.items() if k != "text"}
        up["type"] = "keyUp"
        call("Input.dispatchKeyEvent", up)
        print("pressed {} (ref={})".format(key_name, ref_val))
        log("key", ref=ref_val, key=key_name)
        return 0
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()


def cmd_ax(args):
    """Accessibility tree snapshot (§3). websocket-only."""
    if not has_websocket():
        print("ERROR: ax requires websocket-client (CDP Accessibility domain)",
              file=sys.stderr)
        return 1
    args, raw = _pop_flag(args, "--raw")
    args, max_nodes = _pop_num(args, "--max-nodes", int, 500)
    if max_nodes is None:
        return 1
    args, ref_val = _pop_num(args, "--ref", int, None)

    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    call_id = 0

    def call(method, params=None):
        nonlocal call_id
        call_id += 1
        msg = {"id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        ws.send(json.dumps(msg))
        r = _recv_for_id(ws, call_id)
        if "error" in r:
            err = r["error"]
            print("CDP error: {} (code {})".format(
                err.get("message", "unknown"), err.get("code", "?")),
                file=sys.stderr)
            return None
        return r.get("result", {})

    try:
        ft = call("Page.getFrameTree")
        if ft is None:
            return 1
        frame_tree = ft.get("frameTree", {})
        frame_ids = [frame_tree.get("frame", {}).get("id")]
        frame_urls = [""]

        def walk_children(tree_node):
            for child in tree_node.get("childFrames", []):
                f = child.get("frame", {})
                frame_ids.append(f.get("id"))
                frame_urls.append(f.get("url", ""))
                walk_children(child)
        walk_children(frame_tree)

        shadow_map = {}
        doc = call("DOM.getDocument", {"depth": -1, "pierce": True})
        if doc is not None:
            def _walk_dom(node):
                nid = node.get("backendNodeId")
                for sr in node.get("shadowRoots", []):
                    sr_type = sr.get("shadowRootType", "open")
                    if nid:
                        shadow_map[nid] = sr_type
                    _walk_dom(sr)
                for child in node.get("children", []):
                    _walk_dom(child)
            _walk_dom(doc.get("root", {}))

        frame_node_lists = []
        for fid in frame_ids:
            if fid is None:
                frame_node_lists.append([])
                continue
            r = call("Accessibility.getFullAXTree", {"frameId": fid})
            if r is None:
                if fid == frame_ids[0]:
                    return 1
                print("WARN: frame AX retrieval failed (skipped)",
                      file=sys.stderr)
                frame_node_lists.append([])
            else:
                frame_node_lists.append(r.get("nodes", []))

        if ref_val is not None:
            for fi, nodes in enumerate(frame_node_lists):
                target_node = next(
                    (n for n in nodes if n.get("backendDOMNodeId") == ref_val),
                    None)
                if target_node is not None:
                    by_id = {n["nodeId"]: n for n in nodes}
                    subtree = []
                    sub_visited = set()

                    def collect(n):
                        if n["nodeId"] in sub_visited:
                            return
                        sub_visited.add(n["nodeId"])
                        subtree.append(n)
                        for cid in (n.get("childIds") or []):
                            child = by_id.get(cid)
                            if child is not None:
                                collect(child)
                    collect(target_node)
                    subtree[0] = dict(target_node)
                    subtree[0].pop("parentId", None)

                    sub_lines, sub_meta = _render_ax_tree(
                        [subtree], max_nodes, raw, shadow_map)
                    sub_meta["frames"] = fi + 1
                    header = "AX_OK nodes={} shown={} frames={}".format(
                        sub_meta["nodes"], sub_meta["shown"], sub_meta["frames"])
                    if sub_meta["truncated"]:
                        header += " truncated=1"
                    print(header)
                    print("\n".join(sub_lines))
                    log("ax", ref=ref_val, nodes=sub_meta["nodes"],
                        shown=sub_meta["shown"])
                    return 0
            print("REF_STALE: ref {} not resolvable"
                  " — re-run ax for fresh refs".format(ref_val))
            return 1

        lines, meta = _render_ax_tree(
            frame_node_lists, max_nodes, raw, shadow_map,
            frame_urls=frame_urls)
        header = "AX_OK nodes={} shown={} frames={}".format(
            meta["nodes"], meta["shown"], meta["frames"])
        if meta["truncated"]:
            header += " truncated=1"
        print(header)
        print("\n".join(lines))
        if meta["oopif_count"] > 0:
            print("WARN: {} out-of-process iframe(s) not included".format(
                meta["oopif_count"]), file=sys.stderr)
        log("ax", nodes=meta["nodes"], shown=meta["shown"],
            frames=meta["frames"])
        return 0
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()


COMMANDS = {
    "status": cmd_status,
    "tabs": cmd_tabs,
    "close": cmd_close,
    "screenshot": cmd_screenshot,
    "js": cmd_js,
    "navigate": cmd_navigate,
    "open": cmd_open,
    "normalize-url": cmd_normalize_url,
    "title": cmd_title,
    "html": cmd_html,
    "reload": cmd_reload,
    "wait": cmd_wait,
    "assert": cmd_assert,
    "click": cmd_click,
    "fill": cmd_fill,
    "console": cmd_console,
    "network": cmd_network,
    "pdf": cmd_pdf,
    "viewport": cmd_viewport,
    "window": cmd_window,
    "ax": cmd_ax,
    "key": cmd_key,
    "hover": cmd_hover,
    "drag": cmd_drag,
}

def main(argv):
    """Parse the global --target/--tab selector (from anywhere in argv) into the
    module global TARGET, then dispatch the command. --target requires the
    CDP/websocket channel (fail loud otherwise)."""
    import io
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    elif isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, errors="replace",
            line_buffering=sys.stdout.line_buffering)
    global TARGET
    TARGET = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--target", "--tab"):
            if i + 1 >= len(argv):
                print("ERROR: {} requires a selector argument".format(a), file=sys.stderr)
                return 1
            TARGET = argv[i + 1]
            i += 2
            continue
        rest.append(a)
        i += 1
    if not rest or rest[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = rest[0]
    if cmd not in COMMANDS:
        print("Unknown: {}. Available: {}".format(cmd, ", ".join(sorted(COMMANDS))), file=sys.stderr)
        if cmd.startswith("--"):
            # #187: zsh does not word-split $VAR — `T="--target <id>"; cdp.py $T js`
            # arrives as ONE flag-like token and lands here, not in the flag parser.
            print("Hint: flag-like token received as the command. In zsh, $VAR does "
                  "not word-split — pass flags inline (python3 cdp.py --target ID "
                  "js ...) or use ${=VAR}.", file=sys.stderr)
        return 1
    if TARGET is not None and not has_websocket():
        print("ERROR: --target requires the CDP/websocket channel (the AppleScript/native "
              "fallback drives the active tab and cannot honor a target id)", file=sys.stderr)
        log(cmd, ok="no", exit=1)
        return 1
    # B6 (#322): every non-zero exit of a dispatched command leaves a durable
    # trace — success is inferable from the absence of a fail line. get_tab()
    # and friends fail loud via sys.exit, so SystemExit must be caught here or
    # those terminations would bypass the guarantee (codex review #324 P1).
    try:
        rc = COMMANDS[cmd](rest[1:]) or 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
        if rc != 0:
            log(cmd, ok="no", exit=rc)
        raise
    except KeyboardInterrupt:
        log(cmd, ok="no", exit="interrupted")  # Ctrl-C mid-command still leaves a trace
        raise
    except Exception:
        log(cmd, ok="no", exit="crash")  # unhandled crash still leaves a trace
        raise  # traceback behavior unchanged
    if rc != 0:
        log(cmd, ok="no", exit=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
