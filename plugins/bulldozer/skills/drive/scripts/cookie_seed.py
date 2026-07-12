#!/usr/bin/env python3
"""Cookie-seed (SP2, spec §4.5): import cookies of SELECTED domains from the
daily browser (default 9333) into an isolated /drive lane. The CfT lane stays
clean/pinned/reproducible — it sees only the chosen domains' auth, never the
full daily profile.

Usage:
  cookie_seed.py --domains a.com,b.com --to-port 9359 [--from-port 9333] [--dry-run]

Guard rails:
  - NEVER seeds INTO the daily browser (--to-port 9333 is refused).
  - --domains is mandatory and non-empty: nothing is transferred implicitly.
  - Prints per-domain COUNTS only — never cookie names or values.
  - SP2 ships cookies only; localStorage seeding is deferred until a real
    test needs it (spec §4.5 "cookies/storage-state").

Exit codes: 0 seeded (or dry-run), 1 transport/CDP failure or zero matches,
2 usage/guard violation.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

# Reuse the engine's ws machinery (+ its vendored websocket-client): cdp.py's
# ws_send takes an explicit ws_url, so it is port-agnostic even though the
# module-level CDP_PORT default is single-port.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "look", "scripts"))
import cdp  # noqa: E402

# CookieParam whitelist (Storage.setCookies): Storage.getCookies returns extra
# read-only fields (size, session, …) that setCookies may reject — project
# down to the writable param surface.
_COOKIE_PARAM_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly",
                        "sameSite", "expires", "priority", "sourceScheme",
                        "sourcePort")

# The default daily-browser port. The runtime guard additionally honors a
# CDP_PORT env override (review sweep: a dev whose daily browser runs on a
# non-default port must be equally protected from seeding INTO it).
DAILY_PORT = 9333


def domain_matches(cookie_domain, wanted):
    """Dot-anchored suffix match: exact host or any subdomain of `wanted`.
    evilgithub.com does NOT match github.com."""
    d = (cookie_domain or "").lstrip(".").lower()
    w = (wanted or "").strip().lstrip(".").lower()
    if not d or not w:
        return False
    return d == w or d.endswith("." + w)


def project_cookie(c):
    """Project a Storage.getCookies cookie onto the CookieParam surface.
    Returns None for an already-expired cookie (expires in (0, past] but not
    the -1 session sentinel) — seeding a logically-expired auth cookie into
    the lane as a session cookie would resurrect it (review pack C)."""
    out = {k: c[k] for k in _COOKIE_PARAM_FIELDS if k in c}
    expires = out.get("expires")
    if expires is not None:
        if expires == -1:
            # session cookie: a MISSING expires in CookieParam means session —
            # drop the sentinel rather than ship a past date.
            out.pop("expires", None)
        elif expires <= 0:
            return None  # epoch-or-earlier expiry: already expired, skip
    return out


def browser_ws_url(port):
    """Browser-level CDP endpoint (NOT a tab) — Storage.* lives on it."""
    try:
        with urlopen("http://localhost:{}/json/version".format(port), timeout=5) as r:
            return json.loads(r.read()).get("webSocketDebuggerUrl")
    except (URLError, OSError, json.JSONDecodeError):
        return None


DRIVE_LOG_DEFAULT = os.path.join(os.path.expanduser("~"), ".claude", "hooks",
                                 "bulldozer-drive.log")


def _audit(event_kv):
    """#322 A2: a security-sensitive cross-lane cookie transfer previously left ZERO
    durable trace. Counts/ports/domains only — never cookie names or values (the
    script's own privacy discipline). Best-effort via the canonical writer."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
        from bulldozer_log import append_line
        append_line(os.environ.get("BULLDOZER_DRIVE_LOG") or DRIVE_LOG_DEFAULT,
                    "cookie-seed", **event_kv)
    except Exception:
        pass


def main(argv=None):
    try:
        rc, kv = _main_inner(argv)
    except BaseException as e:
        # an unexpected exception must not bypass the audit trail (#328 r5) —
        # record the failure, then re-raise (traceback behavior unchanged;
        # KeyboardInterrupt included). Skip when _main_inner already wrote the
        # context-rich record (#328 r7 sentinel).
        if not getattr(e, "_bdz_audited", False):
            _audit({"ok": "no", "reason": "unhandled exception: {}".format(type(e).__name__)})
        raise
    if kv is not None:
        _audit(kv)
    return rc


def _main_inner(argv=None):
    p = argparse.ArgumentParser(description="Seed selected domains' cookies "
                                "from the daily browser into a /drive lane.")
    p.add_argument("--domains", required=True,
                   help="comma-separated domain list (subdomains match)")
    p.add_argument("--to-port", required=True, type=int)
    p.add_argument("--from-port", default=9333, type=int)
    p.add_argument("--dry-run", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        # -h/--help exits SystemExit(0) — a SUCCESSFUL exit, not a seed attempt:
        # no audit line, standard exit code preserved (#328 r8). A genuine usage
        # error still leaves its audit line (#328 r2).
        if e.code in (0, None):
            return 0, None
        return 2, {"ok": "no", "reason": "usage error (argparse)"}

    def kv(ok, **extra):
        base = {"from_port": args.from_port, "to_port": args.to_port,
                "domains": ",".join(domains) if domains else args.domains, "ok": ok}
        base.update(extra)
        return base

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("ERROR: --domains must list at least one domain", file=sys.stderr)
        return 2, kv("no", reason="empty domains")
    daily_ports = {DAILY_PORT}
    try:
        daily_ports.add(int(os.environ.get("CDP_PORT", DAILY_PORT)))
    except ValueError:
        pass
    if args.to_port in daily_ports:
        print("ERROR: refusing to seed INTO the daily browser (port {}) — "
              "cookie-seed only flows daily → isolated lane".format(args.to_port),
              file=sys.stderr)
        return 2, kv("no", reason="refused: target is the daily browser")
    if args.to_port == args.from_port:
        print("ERROR: --from-port and --to-port must differ", file=sys.stderr)
        return 2, kv("no", reason="from_port == to_port")

    try:
        return _seed(args, domains, kv)
    except Exception as e:
        # transport/protocol crash AFTER parsing: keep the invocation context in
        # the audit record (#328 r7), then re-raise (traceback unchanged).
        # Sentinel prevents main()'s catch-all from writing a second line.
        _audit(kv("no", reason="unhandled exception: {}".format(type(e).__name__)))
        e._bdz_audited = True
        raise


def _seed(args, domains, kv):
    src_ws = browser_ws_url(args.from_port)
    if not src_ws:
        print("ERROR: source browser not reachable on port {}".format(args.from_port),
              file=sys.stderr)
        return 1, kv("no", reason="source unreachable")
    r = cdp.ws_send(src_ws, "Storage.getCookies", {})
    if r is None:
        return 1, kv("no", reason="Storage.getCookies failed")
    cookies = r.get("result", {}).get("cookies", [])

    selected = []
    counts = {w: 0 for w in domains}
    for c in cookies:
        for w in domains:
            if domain_matches(c.get("domain", ""), w):
                projected = project_cookie(c)
                if projected is not None:  # already-expired → skipped
                    selected.append(projected)
                    counts[w] += 1
                break
    for w in domains:
        print("{}: {} cookie(s)".format(w, counts[w]))
    if not selected:
        print("ERROR: no cookies matched --domains on the source browser — "
              "nothing to seed (are you logged in there?)", file=sys.stderr)
        return 1, kv("no", reason="no matching cookies")
    if args.dry_run:
        print("DRY-RUN: would seed {} cookie(s) into port {}".format(
            len(selected), args.to_port))
        return 0, kv("yes", cookies=len(selected), dry_run="true")

    dst_ws = browser_ws_url(args.to_port)
    if not dst_ws:
        print("ERROR: target lane not reachable on port {}".format(args.to_port),
              file=sys.stderr)
        return 1, kv("no", reason="target lane unreachable", cookies=len(selected))
    w = cdp.ws_send(dst_ws, "Storage.setCookies", {"cookies": selected})
    if w is None:
        return 1, kv("no", reason="Storage.setCookies failed", cookies=len(selected))
    print("Seeded {} cookie(s) ({} domain(s)) into port {}".format(
        len(selected), sum(1 for v in counts.values() if v), args.to_port))
    return 0, kv("yes", cookies=len(selected))


if __name__ == "__main__":
    sys.exit(main())
