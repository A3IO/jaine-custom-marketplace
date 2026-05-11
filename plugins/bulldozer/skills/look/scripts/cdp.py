#!/usr/bin/env python3
"""JAINE Browser — multi-channel browser automation (CDP + AppleScript + macOS native).

Usage:
  cdp.py status                    — check if browser is running
  cdp.py screenshot [FILE]         — capture page screenshot
  cdp.py js 'EXPRESSION'           — execute JS in main world
  cdp.py navigate URL              — navigate to URL
  cdp.py open URL                  — open URL in new tab
  cdp.py tabs                      — list all tabs
  cdp.py title                     — get page title
  cdp.py html                      — get full page HTML
  cdp.py reload                    — reload current page (cache bypass)
  cdp.py wait SELECTOR [TIMEOUT]   — wait for CSS selector (default 10s)
  cdp.py click SELECTOR            — click element by CSS selector
  cdp.py fill SELECTOR VALUE       — fill input/textarea
  cdp.py console                   — read console messages
  cdp.py network                   — recent network requests
  cdp.py pdf [FILE]                — save page as PDF
  cdp.py viewport WIDTH HEIGHT     — change viewport size
  cdp.py window [bounds|upper|lower|activate] — window management

Channels: CDP WebSocket (primary), AppleScript+DOM injection (fallback), macOS native (screenshot).
CDP_PORT env var overrides default 9333.
Log: ~/.claude/hooks/bulldozer-look.log
"""
import json, sys, os, time, base64, subprocess
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
LOG_FILE = os.path.expanduser("~/.claude/hooks/bulldozer-look.log")
CHROME_APP = "Google Chrome"

# --- Logging ---

def log(event, **kw):
    try:
        parts = [time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event={}".format(event)]
        parts.extend("{}={}".format(k, v) for k, v in kw.items())
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(" | ".join(parts) + "\n")
    except OSError:
        pass

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

def get_tab(url_filter=None):
    tabs = cdp_get("/json/list")
    if not tabs:
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        sys.exit(1)
    for t in tabs:
        if t.get("type") != "page":
            continue
        if url_filter and url_filter not in t.get("url", ""):
            continue
        return t
    return tabs[0] if tabs else None

# --- CDP WebSocket ---

def ws_send(ws_url, method, params=None):
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    try:
        msg = {"id": 1, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        result = json.loads(ws.recv())
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

def cdp_js(expr):
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
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

def native_screenshot(path):
    _quartz_code = (
        "import Quartz as Q; ws=Q.CGWindowListCopyWindowInfo("
        "Q.kCGWindowListOptionOnScreenOnly|Q.kCGWindowListExcludeDesktopElements,"
        "Q.kCGNullWindowID);\n"
        "wid=next((w['kCGWindowNumber'] for w in ws "
        "if '{owner}' in str(w.get('kCGWindowOwnerName','')) "
        "and w.get('kCGWindowName','')),None)\n"
        "print(wid if wid is not None else '')"
    ).format(owner=CHROME_APP.split()[0])
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
    r = subprocess.run(["screencapture", "-l", wid, "-o", "-x", path],
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
    return 0

def cmd_tabs(args):
    tabs = cdp_get("/json/list") or []
    for t in tabs:
        if t.get("type") == "page":
            print("{}  {:40s}  {}".format(
                t["id"][:12], t.get("title", "?")[:40], t.get("url", "?")[:60]))

def cmd_screenshot(args):
    path = args[0] if args else "/tmp/jaine-screenshot.png"
    if has_websocket():
        tab = get_tab()
        r = ws_send(tab["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "png"})
        data = (r or {}).get("result", {}).get("data", "")
        if not data:
            print("ERROR: empty screenshot", file=sys.stderr)
            return 1
        img = base64.b64decode(data)
        with open(path, "wb") as f:
            f.write(img)
        log("screenshot", channel="cdp", path=path, size=len(img), url=tab.get("url", "?")[:80])
    else:
        if not native_screenshot(path):
            print("ERROR: native screenshot failed", file=sys.stderr)
            return 1
        log("screenshot", channel="native", path=path)
    print(path)
    return 0

def cmd_js(args):
    if not args:
        print("Usage: cdp.py js 'expression'")
        return 1
    expr = args[0]
    if has_websocket():
        result = cdp_js(expr)
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
    log("js", channel=channel(), expr=expr[:80])
    return 0

def cmd_navigate(args):
    if not args:
        print("Usage: cdp.py navigate URL")
        return 1
    url = args[0]
    if has_websocket():
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url}) is None:
            return 1
    else:
        if not as_navigate(url):
            return 1
    log("navigate", channel=channel(), url=url[:80])
    print("Navigated to " + url)
    return 0

def cmd_open(args):
    if not args:
        print("Usage: cdp.py open URL")
        return 1
    url = args[0]
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
    log("open", url=url[:80])
    return 0

def cmd_title(args):
    if has_websocket():
        result = cdp_js("document.title")
        if result is None:
            return 1
        print(result.get("value", "?"))
    else:
        val = as_js_main_world("document.title")
        print(val or "?")
    return 0

def cmd_html(args):
    if has_websocket():
        result = cdp_js("document.documentElement.outerHTML")
        if result is None:
            return 1
        print(result.get("value", ""))
    else:
        print("ERROR: html requires websocket-client (too large for AppleScript bridge)", file=sys.stderr)
        return 1
    return 0

def cmd_wait(args):
    if not args:
        print("Usage: cdp.py wait SELECTOR [TIMEOUT]")
        return 1
    selector = args[0]
    try:
        timeout = int(args[1]) if len(args) > 1 else 10
    except ValueError:
        print("ERROR: TIMEOUT must be an integer, got: {}".format(args[1]), file=sys.stderr)
        return 1
    expr = "!!document.querySelector({})".format(json.dumps(selector))
    start = time.time()
    while time.time() - start < timeout:
        if has_websocket():
            r = cdp_js(expr)
            if r is None:
                return 1
            found = r.get("value") is True
        else:
            val = as_js_main_world(expr)
            found = val is True or val == "true"
        if found:
            elapsed = int((time.time() - start) * 1000)
            print("Found '{}' in {}ms".format(selector, elapsed))
            log("wait", selector=selector, elapsed_ms=elapsed)
            return 0
        time.sleep(0.5)
    print("Timeout: '{}' not found after {}s".format(selector, timeout), file=sys.stderr)
    return 1

def cmd_reload(args):
    if has_websocket():
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Page.reload", {"ignoreCache": True}) is None:
            return 1
    else:
        if not as_reload():
            return 1
    log("reload", channel=channel())
    print("Reloaded")
    return 0

def cmd_click(args):
    if not args:
        print("Usage: cdp.py click SELECTOR")
        return 1
    selector = args[0]
    expr = "(function(){{ var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.click(); return 'clicked ' + el.tagName }})()".format(sel=json.dumps(selector))
    if has_websocket():
        result = cdp_js(expr)
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
    log("click", channel=channel(), selector=selector)
    return 0

def cmd_fill(args):
    if len(args) < 2:
        print("Usage: cdp.py fill SELECTOR VALUE")
        return 1
    selector, value = args[0], args[1]
    expr = "(function(){{ var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.value={val}; el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}})); return 'filled ' + el.tagName }})()".format(
        sel=json.dumps(selector), val=json.dumps(value))
    if has_websocket():
        result = cdp_js(expr)
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
    if not has_websocket():
        print("ERROR: console requires websocket-client (CDP Console domain)", file=sys.stderr)
        return 1
    tab = get_tab()
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Console.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
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
    log("console", count=len(messages))
    return 0

def cmd_network(args):
    if not has_websocket():
        print("ERROR: network requires websocket-client (CDP Network domain)", file=sys.stderr)
        return 1
    tab = get_tab()
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
    tab = get_tab()
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
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 2, "mobile": False,
        }) is None:
            return 1
    else:
        if osascript('tell application "{}" to set bounds of window 1 to {{0, 0, {}, {}}}'.format(
            CHROME_APP, w, h)) is None:
            return 1
    print("Viewport set to {}x{}".format(w, h))
    log("viewport", width=w, height=h)
    return 0

def cmd_window(args):
    action = args[0] if args else "bounds"
    if action == "bounds":
        r = osascript('tell application "{}" to get bounds of window 1'.format(CHROME_APP))
        if r is None:
            return 1
        print(r)
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

COMMANDS = {
    "status": cmd_status,
    "tabs": cmd_tabs,
    "screenshot": cmd_screenshot,
    "js": cmd_js,
    "navigate": cmd_navigate,
    "open": cmd_open,
    "title": cmd_title,
    "html": cmd_html,
    "reload": cmd_reload,
    "wait": cmd_wait,
    "click": cmd_click,
    "fill": cmd_fill,
    "console": cmd_console,
    "network": cmd_network,
    "pdf": cmd_pdf,
    "viewport": cmd_viewport,
    "window": cmd_window,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("Unknown: {}. Available: {}".format(cmd, ", ".join(sorted(COMMANDS))), file=sys.stderr)
        sys.exit(1)
    sys.exit(COMMANDS[cmd](sys.argv[2:]) or 0)
