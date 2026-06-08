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


def main(argv=None):
    p = argparse.ArgumentParser(description="Seed selected domains' cookies "
                                "from the daily browser into a /drive lane.")
    p.add_argument("--domains", required=True,
                   help="comma-separated domain list (subdomains match)")
    p.add_argument("--to-port", required=True, type=int)
    p.add_argument("--from-port", default=9333, type=int)
    p.add_argument("--dry-run", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit:
        return 2
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("ERROR: --domains must list at least one domain", file=sys.stderr)
        return 2
    daily_ports = {DAILY_PORT}
    try:
        daily_ports.add(int(os.environ.get("CDP_PORT", DAILY_PORT)))
    except ValueError:
        pass
    if args.to_port in daily_ports:
        print("ERROR: refusing to seed INTO the daily browser (port {}) — "
              "cookie-seed only flows daily → isolated lane".format(args.to_port),
              file=sys.stderr)
        return 2
    if args.to_port == args.from_port:
        print("ERROR: --from-port and --to-port must differ", file=sys.stderr)
        return 2

    src_ws = browser_ws_url(args.from_port)
    if not src_ws:
        print("ERROR: source browser not reachable on port {}".format(args.from_port),
              file=sys.stderr)
        return 1
    r = cdp.ws_send(src_ws, "Storage.getCookies", {})
    if r is None:
        return 1
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
        return 1
    if args.dry_run:
        print("DRY-RUN: would seed {} cookie(s) into port {}".format(
            len(selected), args.to_port))
        return 0

    dst_ws = browser_ws_url(args.to_port)
    if not dst_ws:
        print("ERROR: target lane not reachable on port {}".format(args.to_port),
              file=sys.stderr)
        return 1
    w = cdp.ws_send(dst_ws, "Storage.setCookies", {"cookies": selected})
    if w is None:
        return 1
    print("Seeded {} cookie(s) ({} domain(s)) into port {}".format(
        len(selected), sum(1 for v in counts.values() if v), args.to_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
