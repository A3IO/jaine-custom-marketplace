#!/usr/bin/env python3
"""JAINE Browser CDP client — screenshot, JS execution, navigate, DOM inspect.
Usage:
  cdp.py screenshot [FILE]         — capture page screenshot (default: /tmp/jaine-screenshot.png)
  cdp.py js 'JS_EXPRESSION'        — execute JS in page context, print result
  cdp.py navigate URL              — navigate current tab to URL
  cdp.py open URL                  — open URL in new tab
  cdp.py tabs                      — list all tabs
  cdp.py title                     — get page title
  cdp.py html                      — get page HTML (outerHTML)
  cdp.py wait SELECTOR [TIMEOUT]   — wait for CSS selector (default 10s)
  cdp.py status                    — check if browser is running
  cdp.py reload                    — reload current page

CDP_PORT env var overrides default 9333.
Log: ~/.claude/hooks/bulldozer-look.log
"""
import json, sys, os, time, base64
from urllib.request import urlopen
from urllib.error import URLError

try:
    CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
except ValueError:
    print("ERROR: CDP_PORT must be a number, got: " + os.environ.get("CDP_PORT", ""), file=sys.stderr)
    sys.exit(1)
CDP_BASE = "http://localhost:{}".format(CDP_PORT)
LOG_FILE = os.path.expanduser("~/.claude/hooks/bulldozer-look.log")

def log(event, **kw):
    parts = [time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event={}".format(event)]
    parts.extend("{}={}".format(k, v) for k, v in kw.items())
    line = " | ".join(parts)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

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

def ws_send(ws_url, method, params=None):
    try:
        import websocket
    except ImportError:
        print("ERROR: websocket-client not installed. Run: uv pip install websocket-client", file=sys.stderr)
        sys.exit(1)
    ws = websocket.create_connection(ws_url, timeout=30)
    try:
        msg = {"id": 1, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        result = json.loads(ws.recv())
    finally:
        ws.close()
    if "error" in result:
        err = result["error"]
        print("CDP error: {} (code {})".format(err.get("message", "unknown"), err.get("code", "?")), file=sys.stderr)
    return result

def cmd_status(args):
    tabs = cdp_get("/json/list")
    if tabs is None:
        print("OFFLINE — browser not running on port " + str(CDP_PORT))
        return 1
    pages = [t for t in tabs if t.get("type") == "page"]
    print("ONLINE — {} tabs on port {}".format(len(pages), CDP_PORT))
    for t in pages:
        print("  {} {}".format(t["id"][:12], t.get("url", "?")[:80]))
    return 0

def cmd_tabs(args):
    tabs = cdp_get("/json/list") or []
    for t in tabs:
        if t.get("type") == "page":
            print("{}  {:40s}  {}".format(t["id"][:12], t.get("title", "?")[:40], t.get("url", "?")[:60]))

def cmd_screenshot(args):
    path = args[0] if args else "/tmp/jaine-screenshot.png"
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "png"})
    data = r.get("result", {}).get("data", "")
    if not data:
        print("ERROR: empty screenshot", file=sys.stderr)
        return 1
    img = base64.b64decode(data)
    with open(path, "wb") as f:
        f.write(img)
    print(path)
    log("screenshot", path=path, size=len(img), url=tab.get("url", "?")[:80])
    return 0

def cmd_js(args):
    if not args:
        print("Usage: cdp.py js 'expression'")
        return 1
    expr = args[0]
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
    })
    result = r.get("result", {}).get("result", {})
    val = result.get("value")
    if val is not None:
        print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    else:
        desc = result.get("description", result.get("type", "undefined"))
        print(desc)
    log("js", expr=expr[:80], type=result.get("type", "?"))
    return 0

def cmd_navigate(args):
    if not args:
        print("Usage: cdp.py navigate URL")
        return 1
    url = args[0]
    tab = get_tab()
    ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url})
    log("navigate", url=url[:80])
    print("Navigated to " + url)
    return 0

def cmd_open(args):
    if not args:
        print("Usage: cdp.py open URL")
        return 1
    url = args[0]
    r = cdp_get("/json/new?" + url)
    if not r:
        print("ERROR: could not open tab — browser not running?", file=sys.stderr)
        return 1
    print("Opened {} in tab {}".format(url, r.get("id", "?")[:12]))
    log("open", url=url[:80])
    return 0

def cmd_title(args):
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {"expression": "document.title"})
    print(r.get("result", {}).get("result", {}).get("value", "?"))
    return 0

def cmd_html(args):
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
        "expression": "document.documentElement.outerHTML",
        "returnByValue": True,
    })
    print(r.get("result", {}).get("result", {}).get("value", ""))
    return 0

def cmd_wait(args):
    if not args:
        print("Usage: cdp.py wait SELECTOR [TIMEOUT]")
        return 1
    selector = args[0]
    timeout = int(args[1]) if len(args) > 1 else 10
    tab = get_tab()
    start = time.time()
    while time.time() - start < timeout:
        r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
            "expression": "!!document.querySelector({})".format(json.dumps(selector)),
        })
        if r.get("result", {}).get("result", {}).get("value") is True:
            elapsed = int((time.time() - start) * 1000)
            print("Found '{}' in {}ms".format(selector, elapsed))
            log("wait", selector=selector, elapsed_ms=elapsed)
            return 0
        time.sleep(0.5)
    print("Timeout: '{}' not found after {}s".format(selector, timeout), file=sys.stderr)
    return 1

def cmd_reload(args):
    tab = get_tab()
    ws_send(tab["webSocketDebuggerUrl"], "Page.reload", {"ignoreCache": True})
    log("reload", url=tab.get("url", "?")[:80])
    print("Reloaded")
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
    "wait": cmd_wait,
    "reload": cmd_reload,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("Unknown: {}. Available: {}".format(cmd, ", ".join(COMMANDS)), file=sys.stderr)
        sys.exit(1)
    sys.exit(COMMANDS[cmd](sys.argv[2:]) or 0)
