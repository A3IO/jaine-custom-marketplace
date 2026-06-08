# /look-v2 sub-D: LAN web-security lane flag (`launch.sh` opt-in `--insecure`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an *isolated* `/look` lane opt into Chrome's `--disable-web-security` (via `--insecure` arg / `LOOK_INSECURE` env) so a `file://` page can `fetch('http://<LAN>')` (the #93 case), while the default lane — and the daily 9333 browser — can NEVER be launched web-security-relaxed.

**Architecture:** `launch.sh` already reserves `--insecure`/`LOOK_INSECURE` and fail-louds on both (sub-A). This sub-project replaces that reservation with a gated read: a single isolation gate (run before the dry-run/real fork, so dry-run and real share it) permits the flag only when `CDP_PORT != 9333` **AND** an explicit non-default `LOOK_PROFILE_DIR` is set; otherwise it fail-louds. On the permitted path it warns loudly and appends `--disable-web-security` to the one shared `CHROME_ARGV` array. `cdp.py` is untouched.

**Tech Stack:** bash (`launch.sh`), pytest (`LOOK_DRY_RUN` unit tests + browser e2e), the `jaine_browser`/`test_server` conftest fixtures, Chrome `--headless=new` + `--disable-web-security`, Chrome DevTools Protocol via `cdp.py`.

---

## Spike result (D.1 — DONE, 2026-06-03)

Empirical, reproducible (n=2, both reps identical). A `file://` page fetching a no-CORS local http server, driven through an isolated headless lane:

| Condition | `127.0.0.1` | real LAN IP `192.168.31.116` |
|---|---|---|
| baseline (no flag) | `FAIL:Failed to fetch` | `FAIL:Failed to fetch` |
| **`--disable-web-security`** | **`OK:pong`** | **`OK:pong`** |
| `--allow-file-access-from-files` | `FAIL:Failed to fetch` | `FAIL:Failed to fetch` |

**Conclusions that drive this plan:**
1. The reproduction is valid — baseline FAILs (the CORS/SOP block is real).
2. **`--disable-web-security` is THE flag** — it unblocks both `127.0.0.1` **and** a real LAN IP (so it covers the Private-Network-Access layer, not just localhost). It requires a non-default `--user-data-dir`, which the gate's explicit-`LOOK_PROFILE_DIR` requirement guarantees.
3. `--allow-file-access-from-files` does **not** help (it governs file→file, not file→http).
4. → **flag-shipped branch** of spec §7 D (not docs-only).

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `tests/fixtures/look-insecure-fetch.html` | **Create** | `file://` repro: fetch the URL in `location.hash`, write `OK:`/`FAIL:` to `#r`. |
| `tests/fixtures/lan-probe.txt` | **Create** | The served "LAN service" payload — a known token (`PONG-LOOK-93`). |
| `skills/look/scripts/launch.sh` | **Modify** | Remove the pre-D reserved-env guard; recognize `--insecure`; add the isolation gate; append `--disable-web-security`; print `insecure=` in dry-run. |
| `tests/test_launch.py` | **Modify** | New gate unit tests (`LOOK_DRY_RUN`); rewrite the 3 pre-D reservation tests; SKILL-doc structural test. |
| `tests/test_e2e.py` | **Modify** | `insecure_lane` fixture + 2 e2e (insecure unblocks fetch; secure-lane control blocks it). |
| `skills/look/SKILL.md` | **Modify** | Document `--insecure`/`LOOK_INSECURE` as isolated-trusted-LAN-only + the safety boundary. |

---

## The gate (single definition — referenced by every task below)

```
INSECURE_REQUESTED = (--insecure arg present)  OR  (LOOK_INSECURE matches ^(1|true|yes)$ , case-insensitive)

PERMIT  ⇔  INSECURE_REQUESTED
           AND  CDP_PORT != 9333
           AND  PROFILE_OVERRIDDEN == 1            (LOOK_PROFILE_DIR was explicitly set)
           AND  PROFILE_DIR does NOT resolve to /0/.jaine/.browser/profile   (canonicalized — alias-proof: trailing slash, //, /./, .., symlink all rejected)

PERMIT                      → loud stderr warning + append --disable-web-security to CHROME_ARGV ; INSECURE=1
INSECURE_REQUESTED & !PERMIT → fail-loud (non-zero exit; stderr names port + profile)
!INSECURE_REQUESTED          → unchanged (no flag, INSECURE=0)
```

Conditions 2 + 3 together are exactly the spec's **"explicit non-default `LOOK_PROFILE_DIR`"** (set, AND not the daily profile path); condition 1 is the spec's **non-9333 `CDP_PORT`**. So the self-review's two-condition phrasing and this three-line gate are the same rule — the third line operationalizes the spec's "non-default" as "does not **resolve** to the daily profile", canonicalized via `realpath` so a trailing-slash / `//` / `/./` / symlink alias can't bypass it (R1-F1). The gate is therefore a faithful, stricter-than-the-letter implementation of spec §7 D.2, not a divergence from it.

The gate runs **before** the `LOOK_DRY_RUN` fork, so dry-run and the real launch share one code path. `LOOK_INSECURE` truthiness matches `LOOK_HEADLESS` (`1`/`true`/`yes`) — fail-closed: any other value = off (security stays on), never a silent relax.

---

## Task 1: #93 reproduction fixtures

**Files:**
- Create: `tests/fixtures/look-insecure-fetch.html`
- Create: `tests/fixtures/lan-probe.txt`
- Test: `tests/test_launch.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_launch.py` (end of file):

```python
def test_insecure_repro_fixtures_present():
    """D §8: the #93 reproduction fixtures exist — a file:// page that fetches the URL
    in its hash, and a served probe payload with a known token."""
    fx = PLUGIN_ROOT / "tests" / "fixtures"
    html = (fx / "look-insecure-fetch.html").read_text()
    probe = (fx / "lan-probe.txt").read_text()
    assert "location.hash" in html and 'id="r"' in html, "repro html must fetch location.hash into #r"
    assert "PONG-LOOK-93" in probe, "probe payload must carry the known token"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_launch.py::test_insecure_repro_fixtures_present -v`
Expected: FAIL — `FileNotFoundError` (fixtures don't exist yet).

- [ ] **Step 3: Create the fixtures**

`tests/fixtures/lan-probe.txt` (exact content, single line + trailing newline):

```
PONG-LOOK-93
```

`tests/fixtures/look-insecure-fetch.html`:

```html
<!doctype html>
<meta charset="utf-8">
<title>look-insecure-fetch</title>
<!-- #93 reproduction: a file:// page fetching an http:// origin. The fetch target is
     passed in the URL hash so a test can point it at the random test_server port
     (normalize_url passes a file:// URL — hash included — through verbatim). With web
     security ON the fetch is cross-origin → FAIL; an --insecure lane
     (--disable-web-security) → OK. -->
<div id="r">PENDING</div>
<script>
  var target = location.hash.slice(1);
  fetch(target)
    .then(function (resp) { return resp.text(); })
    .then(function (body) { document.getElementById("r").textContent = "OK:" + body.trim(); })
    .catch(function (err) {
      document.getElementById("r").textContent =
        "FAIL:" + ((err && err.message) ? err.message : String(err));
    });
</script>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_launch.py::test_insecure_repro_fixtures_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/look-insecure-fetch.html tests/fixtures/lan-probe.txt tests/test_launch.py
git commit -m "test(look): add #93 insecure-lane reproduction fixtures"
```

---

## Task 2: `launch.sh` isolation gate + `--disable-web-security`

This is the core change. Write **all** the unit tests (new + the 3 rewrites) first → RED → one atomic `launch.sh` edit → GREEN. The `launch.sh` change is atomic on purpose: recognizing `--insecure` without the gate would leave an unsafe window where it could relax the daily browser.

**Files:**
- Modify: `tests/test_launch.py`
- Modify: `skills/look/scripts/launch.sh`

- [ ] **Step 1: Write the failing tests**

In `tests/test_launch.py`, **replace** the three pre-D reservation tests (`test_insecure_arg_rejected_as_unknown_flag`, `test_look_insecure_env_reserved_until_d`, `test_insecure_refused_by_two_distinct_mechanisms`) with their gated rewrites, and **add** the new gate tests:

```python
# ── D: --insecure / LOOK_INSECURE isolation gate (replaces the pre-D reservation) ──

def test_insecure_arg_rejected_on_default_lane():
    """D.2: --insecure is now RECOGNIZED (not an unknown flag), but the gate rejects it
    on the default lane (9333, no explicit profile) → fail-loud."""
    r = _run_launch(args=["--insecure"])
    assert r.returncode != 0, "--insecure on the default lane must fail loud (gate)"
    assert "insecure" in r.stderr.lower()
    assert "unknown" not in r.stderr.lower(), "--insecure must be recognized, not an unknown flag"


def test_insecure_env_rejected_on_default_lane():
    """D.2: LOOK_INSECURE=1 on the default lane is rejected BY THE GATE → fail-loud.
    Asserts the gate's 'isolated'-lane rationale — which is ABSENT from the pre-D
    reserved-guard message ('LOOK_INSECURE is reserved ...') — so this is a genuine RED
    against current launch.sh, not a test that passes both before and after the change."""
    r = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert r.returncode != 0, "LOOK_INSECURE=1 on the default lane must fail loud (gate)"
    assert "isolated" in r.stderr.lower(), \
        "must be rejected by the isolation gate (names 'isolated'), not the pre-D reserved guard"


def test_insecure_both_routes_gated_on_default_lane():
    """D.2: BOTH the --insecure arg and the LOOK_INSECURE env are gated by the SAME
    isolation check — the pre-D reserved-env guard string is gone."""
    arg = _run_launch(args=["--insecure"])
    env = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert arg.returncode != 0 and env.returncode != 0
    assert '[ -n "${LOOK_INSECURE:-}" ]' not in LAUNCH_TEXT, \
        "the pre-D reserved guard must be replaced by the isolation gate"


def test_insecure_permitted_on_isolated_lane_adds_flag():
    """D.2 (flag-shipped): non-9333 port + explicit non-default LOOK_PROFILE_DIR →
    --insecure permitted: --disable-web-security in argv, insecure=1, loud warning."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-ins"})
    assert r.returncode == 0, "isolated --insecure must be permitted: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "1"
    assert "--disable-web-security" in argv
    assert "RELAXED" in r.stderr or "web security" in r.stderr.lower(), "must warn loudly on the permitted path"


def test_insecure_env_permitted_on_isolated_lane():
    """D.2: the LOOK_INSECURE env route is permitted on the same isolated lane."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-ins",
                                  "LOOK_INSECURE": "1"})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "1"
    assert "--disable-web-security" in argv


def test_insecure_rejected_on_default_port_even_with_profile():
    """D.2: port 9333 can NEVER go insecure — even with an explicit non-default profile."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9333", "LOOK_PROFILE_DIR": "/tmp/lane-ins"})
    assert r.returncode != 0, "--insecure on 9333 must fail loud regardless of profile"
    assert "9333" in r.stderr or "isolated" in r.stderr.lower()


def test_insecure_rejected_without_explicit_profile():
    """D.2: a non-9333 lane with a DERIVED profile (LOOK_PROFILE_DIR unset) is not
    'provably isolated' → reject."""
    r = _run_launch(args=["--insecure"], env_override={"CDP_PORT": "9334"})
    assert r.returncode != 0, "--insecure without explicit LOOK_PROFILE_DIR must fail loud"
    assert "LOOK_PROFILE_DIR" in r.stderr or "isolated" in r.stderr.lower()


def test_insecure_rejected_when_profile_is_default_daily_path():
    """D.2: explicitly setting LOOK_PROFILE_DIR to the daily profile must NOT permit
    insecure (never relax the daily browser's profile)."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334",
                                  "LOOK_PROFILE_DIR": "/0/.jaine/.browser/profile"})
    assert r.returncode != 0, "explicit daily-profile path must not permit insecure"
    assert "isolated" in r.stderr.lower() or "profile" in r.stderr.lower()


def test_insecure_rejected_when_profile_aliases_daily_path():
    """R1-F1: a profile that RESOLVES to the daily profile (trailing slash, //, /./)
    must be rejected too — the gate canonicalizes (realpath), not exact-string-matches."""
    for alias in ("/0/.jaine/.browser/profile/", "/0/.jaine/.browser/./profile",
                  "/0/.jaine/.browser//profile"):
        r = _run_launch(args=["--insecure"],
                        env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": alias})
        assert r.returncode != 0, "daily-profile alias {!r} must fail loud".format(alias)
        assert "isolated" in r.stderr.lower() or "daily" in r.stderr.lower()


def test_no_insecure_request_has_no_flag():
    """A non-insecure isolated lane launches normally — no --disable-web-security."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "0"
    assert "--disable-web-security" not in argv


def test_default_lane_insecure_zero():
    """Regression: the default lane is insecure=0 with no --disable-web-security
    (complements test_default_invocation_argv_is_byte_identical)."""
    r = _run_launch()
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "0"
    assert "--disable-web-security" not in argv


def test_insecure_env_falsy_is_off_not_failloud():
    """D.2: LOOK_INSECURE in {0,false,no,''} is treated as OFF (fail-closed) — a normal
    launch, NOT a fail-loud, even on 9333."""
    for val in ("0", "false", "no", ""):
        r = _run_launch(env_override={"CDP_PORT": "9333", "LOOK_INSECURE": val})
        assert r.returncode == 0, "LOOK_INSECURE={!r} must be off, not fail-loud: {}".format(val, r.stderr)
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["insecure"] == "0"
        assert "--disable-web-security" not in argv


def test_disable_web_security_precedes_url():
    """The flag is a Chrome flag — it must come before the trailing URL token."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    _, argv = _parse_dryrun(r.stdout)
    assert "--disable-web-security" in argv
    assert argv.index("--disable-web-security") < len(argv) - 1, "flag must precede the URL"
    assert argv[-1] == "about:blank"


def test_insecure_gate_is_single_code_path():
    """D.2: the --disable-web-security append lives in the ONE shared CHROME_ARGV build
    (A.10), and the gate runs BEFORE the dry-run/real fork — so dry-run == real."""
    assert LAUNCH_TEXT.count("CHROME_ARGV+=(--disable-web-security)") == 1, \
        "exactly one argv-append site (the comment + warning also name the flag — R1-F2)"
    gate_pos = LAUNCH_TEXT.index("INSECURE_REQUESTED")
    dryrun_pos = LAUNCH_TEXT.index('"${LOOK_DRY_RUN:-}" == "1"')
    assert gate_pos < dryrun_pos, "the insecure gate must run before the dry-run/real fork"


def test_insecure_gate_works_under_bash_32():
    """The gate uses only bash-3.2-safe constructs (shopt nocasematch, [[ =~ ]], (( ))).""" 
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"},
                    bash="/bin/bash")
    assert r.returncode == 0, "bash 3.2 insecure dry-run failed: {}".format(r.stderr)
    _, argv = _parse_dryrun(r.stdout)
    assert "--disable-web-security" in argv
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_launch.py -k "insecure" -v`
Expected: the permit/gate tests FAIL — current `launch.sh` fail-louds on `--insecure` (unknown flag) and `LOOK_INSECURE` (reserved guard) **even on an isolated lane** (so the permit tests can't get rc 0) and never emits `insecure=`/`--disable-web-security`; its reject messages say `"unknown flag"` / `"reserved"` — NOT the gate's `isolated` rationale the rewritten reject tests now require — and it still contains `[ -n "${LOOK_INSECURE:-}" ]`. (Pre-existing non-insecure tests stay green.)

- [ ] **Step 3: Implement the gate in `skills/look/scripts/launch.sh`**

**(3a)** DELETE the pre-D reserved-env guard (the block beginning `# LOOK_INSECURE is reserved for sub-project D`):

```bash
# LOOK_INSECURE is reserved for sub-project D (web-security lane). Until D ships,
# refuse it loudly — the --insecure ARG is rejected by the arg parser (unknown
# flag); the ENV needs its own guard because the parser only sees argv (R3-F1).
if [ -n "${LOOK_INSECURE:-}" ]; then
  echo "ERROR: LOOK_INSECURE is reserved and not yet supported (sub-project D); refusing to launch. Unset it." >&2
  exit 1
fi
```

**(3b)** In the argument-parsing init, add `INSECURE_ARG=0` alongside the other inits:

```bash
HEADLESS_ARG=""    # "", "0" (headful) or "1" (headless)
INSECURE_ARG=0
URL=""
URL_SET=0
SAW_TERMINATOR=0
```

**(3c)** In the parser `case`, recognize `--insecure` **before** the `--*)` catch-all:

```bash
    --)        SAW_TERMINATOR=1 ;;
    --headless) HEADLESS_ARG=1 ;;
    --headful)  HEADLESS_ARG=0 ;;
    --insecure) INSECURE_ARG=1 ;;
    --*)
      echo "ERROR: unknown flag '$a' (look launcher accepts --headless/--headful/--insecure)" >&2
      exit 1
      ;;
```

**(3d)** Add the gate block **immediately after** the headless-resolution block (after the `HEADLESS="$_env_headless"` `fi`, before the `# ── pkill match` comment). `CDP_PORT`, `PROFILE_DIR`, `PROFILE_OVERRIDDEN`, and `INSECURE_ARG` are all resolved by this point:

```bash
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
  # /0/.jaine/.browser/profile and relax the daily browser's data (R1-F1). Canonicalize
  # with python3 (already a launch.sh dependency); realpath works on a not-yet-created leaf.
  # Fail-closed: if canonicalization fails (|| echo 1), treat the profile AS the daily one.
  _profile_is_daily=$(python3 - "$PROFILE_DIR" <<'PY' 2>/dev/null || echo 1
import os, sys
print(1 if os.path.realpath(sys.argv[1]) == os.path.realpath("/0/.jaine/.browser/profile") else 0)
PY
)
  if (( CDP_PORT == 9333 )) || (( ! PROFILE_OVERRIDDEN )) || (( _profile_is_daily )); then
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
```

**(3e)** In the `CHROME_ARGV` build, append the flag **immediately after** the `--headless=new` block, before the `# Chrome end-of-options` separator:

```bash
if (( HEADLESS )); then
  CHROME_ARGV+=(--headless=new)
fi
if (( INSECURE )); then
  CHROME_ARGV+=(--disable-web-security)
fi
```

**(3f)** In the `LOOK_DRY_RUN` printer, add an `insecure=` line (right after the `headless=` line):

```bash
  echo "headless=$HEADLESS"
  echo "insecure=$INSECURE"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_launch.py -v`
Expected: PASS — all insecure tests green **and** every pre-existing launch test still green (default argv byte-identical, single `CHROME_ARGV=(`, headless/window/port/kill_match unchanged).

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): D — opt-in --insecure web-security lane flag, gated to isolated lanes (#93)"
```

---

## Task 3: SKILL.md documentation

**Files:**
- Modify: `tests/test_launch.py` (structural doc test)
- Modify: `skills/look/SKILL.md`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_launch.py`:

```python
def test_skill_documents_insecure_lane():
    """A.12/D: SKILL.md documents --insecure / LOOK_INSECURE + the isolated-lane boundary."""
    skill = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    assert "--insecure" in skill and "LOOK_INSECURE" in skill
    assert "isolated" in skill.lower(), "must state the isolated-lane-only boundary"
    assert "--disable-web-security" in skill, "name the actual Chrome flag for transparency"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_launch.py::test_skill_documents_insecure_lane -v`
Expected: FAIL — SKILL.md doesn't mention `--insecure` yet.

- [ ] **Step 3: Add the SKILL.md section**

Add to `skills/look/SKILL.md`, in the lane documentation (near where `LOOK_HEADLESS` / the lane model is described):

```markdown
### Web-security lane (`--insecure` / `LOOK_INSECURE`) — isolated trusted-LAN testing only

By default a `file://` page cannot `fetch('http://<host>')` — it is cross-origin from
the null `file://` origin (this is #93). For trusted local/LAN testing, an **isolated**
lane may opt into Chrome's `--disable-web-security`:

    CDP_PORT=9334 LOOK_PROFILE_DIR=/tmp/look-lan LOOK_HEADLESS=1 \
      ./launch.sh --insecure http://localhost:8080/page

`--insecure` (or `LOOK_INSECURE=1`/`true`/`yes`) is **refused fail-loud** unless the lane
is provably isolated: a **non-9333 `CDP_PORT`** AND an **explicit, non-default
`LOOK_PROFILE_DIR`**. The default lane and the daily 9333 browser can never be launched
web-security-relaxed. On the permitted path `launch.sh` prints a loud stderr warning.

**Safety boundary:** a relaxed lane disables the same-origin policy for *all* content it
loads — use it ONLY for trusted local/LAN pages, never for untrusted/remote content.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_launch.py::test_skill_documents_insecure_lane -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/look/SKILL.md tests/test_launch.py
git commit -m "docs(look): document --insecure / LOOK_INSECURE isolated-lane flag (D)"
```

---

## Task 4: e2e — `--insecure` lane unblocks the fetch (+ secure-lane control)

The unit RED (Task 2) is the strict-TDD proof. This task is integration verification that the wiring works end-to-end through a real `launch.sh`-launched browser. The **secure-lane control** (same fetch → FAIL) is the causation guard: it proves `--disable-web-security` — not something else — is what unblocks the fetch (mirrors the spike's baseline). The `insecure` test passes only after Task 2 + Task 1; the secure-lane control is green after **Task 1 alone** — it never requests `--insecure`/`LOOK_INSECURE`, so it does not depend on Task 2 (R1-F3). That is the point: it is an always-on causation guard, not a RED feature test.

**Files:**
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Add the fixture + tests**

Extend the imports at the top of `tests/test_e2e.py`:

```python
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402
from conftest import run_cdp, CDP_PORT, FIXTURES_DIR, LAUNCH_SCRIPT, LANE_ENV_VARS, _kill_pattern  # noqa: E402
```

Append at the end of `tests/test_e2e.py`:

```python
# ── sub-D: --insecure web-security lane (#93) ──

# A dedicated insecure e2e port: fixed + in-range + never 9333, and always distinct from the
# secure lane's CDP_PORT (R1-F4). `CDP_PORT + 2` could land on 9333 (CDP_PORT=9331) or out of
# range (CDP_PORT=65534) (R2-F1) — so use a fixed in-range port and only step away if CDP_PORT
# happens to equal it. Both 9356 and 9358 are in 1..65535 and != 9333.
INSECURE_TEST_PORT = 9356 if CDP_PORT != 9356 else 9358


def _cdp_online(port):
    try:
        return urlopen("http://localhost:{}/json/version".format(port), timeout=3).status == 200
    except (URLError, OSError):
        return False


@pytest.fixture(scope="module")
def insecure_lane():
    """An isolated HEADLESS lane launched via launch.sh with LOOK_INSECURE=1
    (--disable-web-security). Dedicated non-9333 port + temp profile; torn down with
    pkill + rmtree. Fails loud on an unexpected pre-existing listener (isolation)."""
    if _cdp_online(INSECURE_TEST_PORT):
        pytest.fail("Unexpected CDP listener on insecure test port {0} — kill it "
                    "(pkill -f remote-debugging-port={0}) and re-run.".format(INSECURE_TEST_PORT))
    profile = tempfile.mkdtemp(prefix="jaine-insecure-{}-".format(INSECURE_TEST_PORT))
    env = os.environ.copy()
    for _v in LANE_ENV_VARS:
        env.pop(_v, None)
    env.update({
        "CDP_PORT": str(INSECURE_TEST_PORT),
        "LOOK_PROFILE_DIR": profile,
        "LOOK_HEADLESS": "1",
        "LOOK_INSECURE": "1",
    })
    kill_match = _kill_pattern(profile)
    subprocess.Popen(["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_online(INSECURE_TEST_PORT):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        shutil.rmtree(profile, ignore_errors=True)
        pytest.fail("insecure lane did not start on {} within 20s".format(INSECURE_TEST_PORT))
    yield INSECURE_TEST_PORT
    subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
    shutil.rmtree(profile, ignore_errors=True)


def _fetch_result(cdp_port, target_url, poll=5.0):
    """Navigate the lane at cdp_port to the file:// repro (target in #hash); poll #r."""
    repro = "file://" + str(Path(FIXTURES_DIR) / "look-insecure-fetch.html")
    nav = run_cdp(["navigate", repro + "#" + target_url], env_override={"CDP_PORT": str(cdp_port)})
    assert nav.returncode == 0, "navigate failed: {}".format(nav.stderr)
    deadline = time.time() + poll
    last = ""
    while time.time() < deadline:
        r = run_cdp(["js", "document.getElementById('r').textContent"],
                    env_override={"CDP_PORT": str(cdp_port)})
        last = r.stdout.strip()
        if last and last != "PENDING":
            return last
        time.sleep(0.25)
    return last


def test_insecure_lane_unblocks_file_fetch(insecure_lane, test_server):
    """D acceptance (flag-shipped): an --insecure lane lets a file:// page fetch an
    http:// origin (the #93 case) — fetch SUCCEEDS."""
    target = "http://127.0.0.1:{}/lan-probe.txt".format(test_server)
    result = _fetch_result(insecure_lane, target)
    assert result.startswith("OK:"), "insecure lane must unblock the fetch, got {!r}".format(result)
    assert "PONG-LOOK-93" in result


def test_secure_lane_blocks_file_fetch(jaine_browser, test_server):
    """Causation control: the DEFAULT (secure) lane CANNOT fetch http:// from file://
    — proves --disable-web-security, not something else, is what unblocks it."""
    target = "http://127.0.0.1:{}/lan-probe.txt".format(test_server)
    result = _fetch_result(CDP_PORT, target)  # CDP_PORT = the secure jaine_browser lane
    assert result.startswith("FAIL:"), "secure lane must block the cross-origin fetch, got {!r}".format(result)
```

- [ ] **Step 2: Run the e2e tests**

Run: `pytest tests/test_e2e.py -k "insecure_lane_unblocks or secure_lane_blocks" -v`
Expected: PASS — `test_insecure_lane_unblocks_file_fetch` → `OK:PONG-LOOK-93`; `test_secure_lane_blocks_file_fetch` → `FAIL:...`. (Launches two short-lived headless lanes; ~10-30s.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(look): e2e — --insecure lane unblocks file:// fetch + secure-lane control (#93)"
```

---

## Task 5: Full verification + finish

- [ ] **Step 1: Full offline suite**

Run: `pytest tests/ -p no:cacheprovider -q --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py`
Expected: all green (no regressions). (Offline excludes the two external-dep suites.)

- [ ] **Step 2: Full e2e suite (browser)**

Run: `pytest tests/test_e2e.py -q`
Expected: green (the sub-A/B/C e2e + the two new D tests).

- [ ] **Step 3: Finish**

Announce and use **superpowers:finishing-a-development-branch**. Present options; **create a PR (do NOT merge — Chris merges)**. PR base `bulldozer/main`; reference #93 in the body (orphan base → issue won't auto-close; close manually after merge). Do NOT bump `plugin.json` (auto-calver bumps on merge).

---

## Self-Review

**Spec coverage (§7 D):**
- D.1 spike → **done before this plan**; result table above; flag = `--disable-web-security`, flag-shipped branch. ✓
- D.2 opt-in, never default → the gate (Task 2): reserved guard removed, `--insecure` recognized, gated to non-9333 + explicit non-default `LOOK_PROFILE_DIR`, one code path (dry-run==real, `test_insecure_gate_is_single_code_path`), loud warning, daily 9333 + daily-profile can never relax. ✓
- D.3 docs-only alternative → not taken (spike succeeded); n/a. ✓
- D acceptance (flag-shipped) → `--insecure` on 9333 rejected (`test_insecure_rejected_on_default_port_even_with_profile`); isolated lane warns + adds only the spike-confirmed flag + #93 fetch succeeds (`test_insecure_lane_unblocks_file_fetch`); default lanes unchanged (`test_default_lane_insecure_zero`, byte-identical argv); SKILL.md documents the boundary (Task 3). ✓
- §8 testing → reproduction fixture (Task 1); `LOOK_DRY_RUN` unit tests for every new path (Task 2); e2e proves the flag unblocks fetch + control proves causation (Task 4); bash-3.2 (`test_insecure_gate_works_under_bash_32`). ✓

**Placeholder scan:** none — every step has full file content / exact edits / exact commands + expected output.

**Type/contract consistency:** dry-run key `insecure` (printer 3f ↔ `cfg["insecure"]` in tests); `INSECURE`/`INSECURE_REQUESTED`/`INSECURE_ARG`/`_env_insecure` consistent across 3b–3f; `--disable-web-security` appended once (`test_insecure_gate_is_single_code_path`) and asserted in argv; `_kill_pattern`/`LANE_ENV_VARS`/`FIXTURES_DIR`/`LAUNCH_SCRIPT`/`CDP_PORT` are real conftest exports (verified). The 3 rewritten tests replace (not duplicate) the pre-D names.

**Order safety:** Task 1 (fixtures, independent) → Task 2 (gate; atomic launch.sh edit, unit RED→GREEN) → Task 3 (docs) → Task 4 (e2e, needs Task 1+2) → Task 5 (verify+PR). The `launch.sh` edit is atomic so `--insecure` is never recognized-but-ungated.
