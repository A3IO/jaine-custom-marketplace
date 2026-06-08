# spike/scenario_cdp.py — drive scenario via cdp.py. Two modes for a fair R1-F2 comparison.
# Usage: scenario_cdp.py <naive|best> <PORT> <URL> [--expect-console-error]
import subprocess, sys, os, time
CDP = "skills/look/scripts/cdp.py"
MODE = sys.argv[1]                                  # "naive" | "best"
PORT = sys.argv[2]
URL  = sys.argv[3]
EXPECT_ERR = "--expect-console-error" in sys.argv   # R1-F3 parity

def cdp(*args):
    # cdp.py takes the port via CDP_PORT env, NOT a --port flag (R1-F1).
    # --target pins every command to the fixture tab by url substring (R1-F4).
    env = {**os.environ, "CDP_PORT": PORT}
    return subprocess.run(["python3", CDP, "--target", "async-page", *args],
                          capture_output=True, text=True, env=env)

def fail(m): print("CDP_FAIL " + MODE + ": " + m); sys.exit(1)

cdp("navigate", URL)
if MODE == "naive":
    time.sleep(2)                                   # blind sleep — guessed load
    cdp("js", "document.getElementById('load').click(); 'ok'")          # untrusted
    time.sleep(1)                                   # blind sleep — guessed < 800ms async
    txt = cdp("js", "document.getElementById('result').textContent")
    if "loaded" not in txt.stdout: fail("result not loaded (sleep race)")
    cdp("js", "document.getElementById('delayed').click(); 'ok'")       # fires before enabled
    dr = cdp("js", "document.getElementById('delayed-result').textContent")
    if "clicked" not in dr.stdout: fail("delayed not clicked (no actionability wait)")
else:  # best
    cdp("wait", "#load")                            # auto-wait present
    cdp("click", "#load")                           # trusted click (#140)
    w = cdp("wait", "--js", "document.getElementById('result').textContent==='loaded'")
    if w.returncode != 0: fail("result wait timed out")
    cdp("wait", "--js", "!document.getElementById('delayed').disabled")  # explicit actionability
    cdp("click", "#delayed")                        # trusted click
    d = cdp("wait", "--js", "document.getElementById('delayed-result').textContent==='clicked'")
    if d.returncode != 0: fail("delayed wait timed out")
# console gate — MEASURED detection (R2-F2), NOT a forced parity-pass. The cdp.py `console`
# read is a FRESH subprocess that subscribes AFTER #break already fired, so it depends on
# Console.enable replaying the buffered error — empirically unknown, so we REPORT detected/missed
# (exit 0 either way) and let the spike resolve it. A MISS here is a real cdp.py limitation.
if EXPECT_ERR:
    cdp("click", "#break")                          # trigger ReferenceError
    cdp("wait", "--js", "true")                     # one poll tick to let it surface
    con = cdp("console")
    has_err = "ReferenceError" in con.stdout or "exception" in con.stdout
    print("CDP_CONSOLE " + MODE + " " + ("DETECTED" if has_err else "MISSED")); sys.exit(0)
con = cdp("console")
if "ReferenceError" in con.stdout or "exception" in con.stdout: fail("unexpected console error")
print("CDP_PASS " + MODE); sys.exit(0)
