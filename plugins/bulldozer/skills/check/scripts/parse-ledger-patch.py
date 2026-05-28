#!/usr/bin/env python3
"""Extract LEDGER_PATCH YAML block from a codex verdict file.

Usage:
    parse-ledger-patch.py < verdict-rN.txt          # stdin
    parse-ledger-patch.py --file verdict-rN.txt     # file

Output (stdout, JSON):
    {
      "findings": [...],
      "meta": {...},               # top-level LEDGER_PATCH fields except 'findings'
      "source": "ledger_patch" | "empty_findings" | "synthesized_bare_go",
      "warnings": ["..."]          # graceful-fallback notes (also on stderr)
    }

Exit codes:
    0  Parsed OK (may include warnings, may be synthesized from bare GO,
         may have fallen back from a malformed last block to an earlier valid one —
         see source/meta/warnings for provenance)
    1  No usable LEDGER_PATCH block — block absent, OR present but the
         LEDGER_PATCH value is not a mapping (e.g. "LEDGER_PATCH: TBD").
         Signal: caller may fall back to manual extraction.
    2  Malformed YAML — raw block saved next to input:
         --file MODE: <verdict-basename>.malformed.yml (with_suffix replacement, e.g. verdict-r1.txt → verdict-r1.malformed.yml)
         stdin MODE: stdin-ledger.malformed.yml in cwd
       When multiple blocks exist, exit 2 only fires if ALL blocks are
       malformed; otherwise an earlier valid block is used (warning in payload).
    3  Schema violation — required structure or strict-type contract broken:
         - findings key absent / null / not a list
         - finding not a mapping
         - missing id / severity, or unrecognized severity / non-string title
         - whitespace-only id or title
         - non-list/string/dict files OR non-string/non-mapping entry inside files list
         - duplicate finding id within the block
    4  Missing PyYAML dependency
    5  File / stdin io failure: --file mode path does not exist, or stdin
         pipe is broken/closed (sys.stdin.read raises OSError). Caller fixes
         the wiring/path and retries; NOT a patch-body problem.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    print(
        "ERROR: parse-ledger-patch.py requires PyYAML. "
        + "Install: pip install pyyaml (or: brew install python-pyyaml / apt install python3-yaml).",
        file=sys.stderr,
    )
    sys.exit(4)


class _StringTimestampLoader(yaml.SafeLoader):
    """SafeLoader that keeps YAML timestamp scalars as strings.

    Default SafeLoader resolves `2026-05-26` and similar implicit timestamp
    patterns to ``datetime.date`` / ``datetime.datetime``. These are not
    JSON-serializable, and worse — when they appear as DICT KEYS, ``json.dumps``
    cannot use the ``default=`` fallback (which only fires for values, not
    keys). Disabling implicit timestamp resolution at the loader layer
    eliminates the entire class of crashes for both values and keys.

    Surfaced by meta-dogfood rounds: first as date-in-value (commit 5ac4fc0,
    patched with json.dumps(default=str)), then as date-as-key (this fix,
    moves the safety net up to the parse layer).
    """


_UNSAFE_IMPLICIT_TAGS = frozenset({
    # Default SafeLoader resolves `2026-05-26` to datetime.date — not JSON-
    # serializable and crashes as dict KEYS (default= fallback only fires on
    # values). Disabling at the loader layer is the root-cause fix.
    "tag:yaml.org,2002:timestamp",
    # Default SafeLoader resolves `no/yes/true/false/on/off` to Python bool.
    # For our domain (status, verdict, title) these tokens are user-facing
    # strings — silent bool coercion is data corruption (Issue #100 case #7).
    "tag:yaml.org,2002:bool",
})


def _strip_unsafe_resolvers(loader_cls: type) -> None:
    """Remove YAML 1.1 implicit resolvers listed in ``_UNSAFE_IMPLICIT_TAGS``.

    Iterates the class-level ``yaml_implicit_resolvers`` mapping (first-char
    indexed list of (tag, regex) pairs) and filters out the unsafe entries.
    """
    resolvers: dict[str, list[tuple[str, Any]]] = getattr(loader_cls, "yaml_implicit_resolvers", {})
    new_resolvers: dict[str, list[tuple[str, Any]]] = {}
    for first_char, resolver_list in resolvers.items():
        filtered = [
            (tag, regex)
            for tag, regex in resolver_list
            if tag not in _UNSAFE_IMPLICIT_TAGS
        ]
        if filtered:
            new_resolvers[first_char] = filtered
    loader_cls.yaml_implicit_resolvers = new_resolvers


_strip_unsafe_resolvers(_StringTimestampLoader)


# Line-anchored bare-GO marker — empirically the only verdict shape that's
# safe to synthesize from prose. NO-GO with findings requires structured
# data we can't reverse-engineer from text. Pattern: "GO" alone on a line,
# allowing leading/trailing whitespace but nothing else.
_BARE_GO_RE = re.compile(r"^\s*GO\s*$", re.MULTILINE)

# Fenced markdown code blocks (``` ... ```) are stripped from the verdict
# body before running the bare-GO / NO-GO patterns. Reviewer prose often
# embeds illustrative GO / NO-GO lines inside fences ("the directive looks
# like: ```GO```"), which would otherwise either trigger spurious
# synthesis (D4) or spurious suppression. Code-review D4 / fence handling.
_FENCED_CODE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

# NO-GO suppression. If the verdict body contains NO-GO / NO GO / NO_GO /
# NOGO / NO–GO / NO—GO anywhere (case-insensitive), bare-GO synthesis MUST
# NOT trigger — synthesis would silently override an explicit NO-GO verdict
# and discard real findings (dogfood R1-F1).
#
# Pattern design (code-review A4 + C1 + D1 + E3 cluster):
# - `\bNO` — word-boundary anchor on the NO side
# - `[ \t]*[_–—\-]*[ \t]*` — same-line separator: spaces/tabs
#   on both sides of an optional dash/underscore/en-dash/em-dash, in any
#   count. Critically EXCLUDES `\n` so cross-paragraph NO/GO (false
#   positive C1) does not match.
# - `GO` — literal GO
# - `(?![_\w])` — negative lookahead instead of trailing `\b`: rejects
#   identifier-like suffixes (`NOGO_FLAG`, `NO_GO_STATE` — code-review
#   E3) where `_` would otherwise extend the word and break the boundary.
_NO_GO_RE = re.compile(
    r"\bNO[ \t]*[_–—\-]*[ \t]*GO(?![_\w])",
    re.IGNORECASE,
)


SEVERITY_ALIASES = {
    "critical": "blocker",
    "crit": "blocker",
    "block": "blocker",
    "warning": "low",
    "warn": "low",
    "note": "info",
}
VALID_SEVERITIES = {"blocker", "high", "medium", "low", "info"}

# TODO(A3IO/jaine-plugins#100): tighten strict validation for junk inputs.
# Open defensive edge cases (accepted with TODO at end of dogfood session 93392b64,
# R3-F1, after 3-round standard review hit cap):
#   - unknown severity → silent fallback to "medium" (consider exit 3)
#   - whitespace-only title → accepted (consider strip + require non-empty)
#   - mapping-valued title → str(dict) ends up as title (consider reject)
#   - files: <int> → silently ignored (consider reject or coerce explicitly)
# All four are junk-input cases that codex never emits in practice. Tracked
# separately so PR1a stays focused on the production-impact bugs (silent
# NO-GO→GO, indented-fence data loss, missing required fields).


def extract_ledger_blocks(text: str) -> list[str]:
    """Return raw YAML strings for every LEDGER_PATCH: block in text.

    Codex emits LEDGER_PATCH either inside a fenced code block or as plain YAML
    at end of verdict. The marker may itself be indented (when the block is
    nested inside markdown). We anchor extraction on the marker's column:
    every subsequent line strictly more indented stays in the block, including
    indented ``` fences inside block scalars like ``original_verdict_excerpt: |``.
    The block ends at a line at the marker's column or shallower (an outer fence
    close, blank-then-prose, or end of file).

    If the marker appears multiple times, return all blocks in source order;
    the caller picks the last one (closest to end of verdict).
    """
    blocks: list[str] = []
    lines = text.splitlines(keepends=False)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("LEDGER_PATCH:"):
            i += 1
            continue
        base_indent = len(line) - len(stripped)
        # Strip the marker's own indent so PyYAML sees the key at column 0.
        block_lines: list[str] = [stripped]
        i += 1
        while i < len(lines):
            lookahead = lines[i]
            if not lookahead.strip():
                # Blank lines may sit inside a block scalar — keep, don't terminate.
                block_lines.append("")
                i += 1
                continue
            lookahead_indent = len(lookahead) - len(lookahead[:].lstrip())
            if lookahead_indent <= base_indent:
                # Outer-scope content (fence close, next prose, new top-level key) ends the block.
                break
            block_lines.append(lookahead[base_indent:])
            i += 1
        blocks.append("\n".join(block_lines).rstrip())
    return blocks


def normalize_severity(value: object, warnings: list[str]) -> str | None:
    """Return canonical severity, or None if the value is not a recognized
    severity / alias.

    Caller (normalize_finding) treats None as a schema violation and emits
    exit 3 — symmetric with the 'severity key missing' contract. Aliases
    (critical → blocker, warning → low, …) still pass with a normalization
    warning. Issue #100 cases #1 + #12.
    """
    if not isinstance(value, str):
        # Covers explicit YAML null (severity: ~) and non-string types like int.
        return None
    canonical = value.strip().lower()
    if canonical in VALID_SEVERITIES:
        return canonical
    if canonical in SEVERITY_ALIASES:
        mapped = SEVERITY_ALIASES[canonical]
        warnings.append(f"severity '{value}' normalized to '{mapped}'")
        return mapped
    return None


def normalize_finding(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    """Apply schema-drift fallbacks. Return canonical finding dict, or None on
    a required-field violation (caller treats None as exit 3).

    Required fields: ``id``, ``severity``. Required-but-soft: title OR summary
    (one of them must produce a non-empty string).

    Documented schema drift that the parser tolerates with a warning:
    - ``summary`` used in place of ``title``
    - severity alias (``critical`` → ``blocker``, ``warning`` → ``low``, …)
    - ``files`` as a single string ``"path:line"`` rather than a list
    - per-file entries as ``"path:line"`` strings rather than ``{path, lines}`` dicts
    - per-mapping-entry ``path`` or ``lines`` non-string (e.g. unquoted
      ``lines: 318`` parsing as int) — ``str()``-converted with warning naming
      the field and its original type, keeping the canonical JSON output as
      strings per the SKILL.md type contract.

    Anything outside that list (missing id, non-mapping finding, ``files`` as a
    non-string non-list, etc.) is a structural violation — return None.
    """
    fid_raw = raw.get("id")
    if not isinstance(fid_raw, str) or not fid_raw.strip():
        # Dogfood R1-F3: empty / whitespace-only / non-string ids previously
        # produced blank ledger keys downstream and let whitespace variants
        # bypass duplicate detection. Reject + normalize to stripped form so
        # dedup compares semantic identity.
        print(
            f"ERROR: finding 'id' is missing, empty, or whitespace-only "
            + f"(raw={raw!r})",
            file=sys.stderr,
        )
        return None
    fid = fid_raw.strip()

    if "severity" not in raw:
        print(
            f"ERROR: finding {fid} missing required 'severity' field",
            file=sys.stderr,
        )
        return None

    finding: dict[str, Any] = {"raw": raw, "id": fid}
    sev = normalize_severity(raw.get("severity"), warnings)
    if sev is None:
        print(
            f"ERROR: finding {fid} has unrecognized severity {raw.get('severity')!r} "
            + "(must be one of blocker/high/medium/low/info, or a known alias)",
            file=sys.stderr,
        )
        return None
    finding["severity"] = sev

    # Title fallback chain: title → summary → problem.
    # SKILL.md round-1 prompt asks for "Problem/Impact/Required fix" fields,
    # so `problem:` is a documented drift the parser must accept.
    #
    # Strict validation rules (Issue #100 cases #2 + #3 + #10):
    #   - Explicit non-string title/summary/problem (dict, int, etc.) is a
    #     schema violation — reject immediately, do NOT silently fall through.
    #   - Whitespace-only string is treated as absent — fall through the chain.
    #   - Drift warning fires on any non-title source actually used, even when
    #     the title key was present but yielded no usable content.
    resolved_title: str | None = None
    chosen_source: str | None = None
    for source_key in ("title", "summary", "problem"):
        if source_key not in raw:
            continue
        value = raw[source_key]
        # Code-review A1: explicit YAML null (`title: ~`) is treated like a
        # missing key — fall through the chain. Old `or`-chain behavior also
        # fell through on falsy values, so this restores compatibility for
        # the common 'title: ~, summary: <real>' pattern. Other non-string
        # types (dict/int/list) still reject — those indicate junk input.
        if value is None:
            continue
        if not isinstance(value, str):
            print(
                f"ERROR: finding {fid} field {source_key!r} must be a string, "
                + f"got {type(value).__name__}",
                file=sys.stderr,
            )
            return None
        stripped = value.strip()
        if stripped:
            resolved_title = stripped
            chosen_source = source_key
            break

    if resolved_title is None:
        print(
            f"ERROR: finding {fid} has no usable title/summary/problem field "
            + "(all missing or whitespace-only)",
            file=sys.stderr,
        )
        return None
    if chosen_source != "title":
        warnings.append(
            f"finding {fid}: using '{chosen_source}' as title (schema drift)"
        )
    finding["title"] = resolved_title

    finding["status"] = raw.get("status", "open")

    files_raw = raw.get("files", [])
    # Coerce single-string OR single-dict drift to a one-item list.
    if isinstance(files_raw, str):
        warnings.append(
            f"finding {fid}: 'files' is a string, not a list; coerced to one-item list (schema drift)"
        )
        files_raw = [files_raw]
    elif isinstance(files_raw, dict):
        warnings.append(
            f"finding {fid}: 'files' is a single mapping, not a list; coerced to one-item list (schema drift)"
        )
        files_raw = [files_raw]

    files: list[dict[str, str]] = []
    if isinstance(files_raw, list):
        for entry in files_raw:
            if isinstance(entry, dict):
                # Dogfood R2-F1: SKILL.md output example contracts files[].path
                # and files[].lines as strings. Unquoted YAML like `lines: 318`
                # (int) or `lines: [4, 5]` (list) previously slipped through
                # unchanged, violating the type contract. Stringify with a
                # warning so downstream JSON consumers always see strings.
                entry_path = entry.get("path", "")
                entry_lines = entry.get("lines", "")
                # Code-review A2: explicit YAML null (`path: ~` / `lines: ~`)
                # previously stringified to the literal 'None'. Treat None
                # like the missing-key default — empty string — without the
                # spurious 'stringified from NoneType' warning.
                if entry_path is None:
                    entry_path = ""
                elif not isinstance(entry_path, str):
                    warnings.append(
                        f"finding {fid}: files entry 'path' was "
                        + f"{type(entry_path).__name__}, stringified"
                    )
                    entry_path = str(entry_path)
                if entry_lines is None:
                    entry_lines = ""
                elif not isinstance(entry_lines, str):
                    warnings.append(
                        f"finding {fid}: files entry 'lines' was "
                        + f"{type(entry_lines).__name__}, stringified"
                    )
                    entry_lines = str(entry_lines)
                files.append({"path": entry_path, "lines": entry_lines})
            elif isinstance(entry, str):
                # Accept "path:line" shorthand.
                if ":" in entry:
                    path, _, lines = entry.partition(":")
                    files.append({"path": path, "lines": lines})
                else:
                    files.append({"path": entry, "lines": ""})
            else:
                # Dogfood R1-F4: non-string/non-mapping entry inside the list
                # previously emitted a warning and skipped the entry — same
                # silent partial-finding problem as case #4 (files-as-int at
                # the top level), just nested. Reject the whole finding so a
                # reviewer that emitted bad data doesn't have it half-applied.
                print(
                    f"ERROR: finding {fid} 'files' contains invalid entry of type "
                    + f"{type(entry).__name__} (each entry must be a string or mapping)",
                    file=sys.stderr,
                )
                return None
    else:
        # Issue #100 case #4: non-list/string/dict files is a schema violation.
        # Previously silently produced empty files with a warning — that hid
        # junk like `files: 123` or `files: ~` (explicit null distinct from
        # missing key) from view. Reject loudly instead.
        print(
            f"ERROR: finding {fid} 'files' has invalid type "
            + f"{type(files_raw).__name__} (expected list, string, or dict)",
            file=sys.stderr,
        )
        return None
    finding["files"] = files

    # Issue #105: lift schema-promised fields to top level so downstream consumers
    # (Step 4 ledger update) don't have to traverse raw.* for SKILL.md schema fields.
    # raw.* still holds the unchanged original payload for forensic completeness.
    #
    # Code-review E4: deep-copy each lifted value so top-level and raw.* point
    # at independent Python objects. Without deepcopy, downstream mutation of
    # `finding['required_recheck']['commands']` would silently bleed into
    # `finding['raw']['required_recheck']['commands']`, breaking the
    # "raw preserves original payload" invariant for dict/list values.
    #
    # Code-review C2: type-validate each lifted field against SKILL.md
    # Review Ledger Format. Mismatched type → warn + skip lift (raw.*
    # still preserves the original for forensic inspection). Warn-and-skip
    # rather than reject because callers reading top-level fields will
    # detect absence cleanly, but rejecting the whole finding for a docs-
    # adjacent type drift would be too aggressive.
    _LIFT_TYPES: dict[str, tuple[type, ...]] = {
        "original_verdict_excerpt": (str,),
        "required_recheck": (dict,),
        "introduced_round": (int,),
        "last_seen_round": (int,),
    }
    for key, expected in _LIFT_TYPES.items():
        if key not in raw:
            continue
        value = raw[key]
        # bool is a subclass of int — explicitly exclude so `True` doesn't
        # silently lift as introduced_round.
        if isinstance(value, bool) and bool not in expected:
            warnings.append(
                f"finding {fid}: lifted field {key!r} expects "
                + f"{'/'.join(t.__name__ for t in expected)}, got bool — skipped lift"
            )
            continue
        if not isinstance(value, expected):
            warnings.append(
                f"finding {fid}: lifted field {key!r} expects "
                + f"{'/'.join(t.__name__ for t in expected)}, "
                + f"got {type(value).__name__} — skipped lift"
            )
            continue
        finding[key] = copy.deepcopy(value)

    return finding


def parse(raw_yaml: str, malformed_target: Path | None) -> tuple[int, dict[str, Any]]:
    """Parse one LEDGER_PATCH YAML body. Return (exit_code, payload).

    ``malformed_target=None`` suppresses the on-disk dump of malformed YAML —
    used by the multi-block fallback loop for non-final blocks where the
    primary diagnostic signal belongs to the LAST block's dump. Previously
    the loop passed ``Path("/dev/null")``, which produced a misleading
    "WARN: malformed YAML saved to /dev/null" claiming a save while the
    content was actually discarded (code-review finding E1).
    """
    warnings: list[str] = []

    try:
        # Use the custom loader that keeps timestamp scalars as strings — see
        # _StringTimestampLoader docstring for the meta-dogfood discovery chain.
        data = yaml.load(raw_yaml, Loader=_StringTimestampLoader)
    except yaml.YAMLError as exc:
        if malformed_target is not None:
            try:
                malformed_target.write_text(raw_yaml + "\n")
            except OSError as write_err:
                print(
                    f"WARN: could not save malformed YAML to {malformed_target}: {write_err}",
                    file=sys.stderr,
                )
            else:
                print(f"WARN: malformed YAML saved to {malformed_target}", file=sys.stderr)
        print(f"ERROR: malformed YAML: {exc}", file=sys.stderr)
        return 2, {}

    # Issue #100 case #6: when LEDGER_PATCH key holds a scalar / null / etc.
    # (e.g. `LEDGER_PATCH: TBD` — reviewer narrating instead of patching), the
    # block is well-formed YAML but not a usable structured patch. Route to
    # exit 1 (caller falls back to manual prose extraction), not exit 3
    # (structurally-wrong-patch STOP-and-ask).
    if not isinstance(data, dict):
        print(
            f"ERROR: LEDGER_PATCH body is not a mapping (got {type(data).__name__}) — no usable block",
            file=sys.stderr,
        )
        return 1, {}

    body = data.get("LEDGER_PATCH", data)
    if not isinstance(body, dict):
        print(
            f"ERROR: LEDGER_PATCH value is {type(body).__name__}, not a mapping — "
            + "no usable block (fall back to manual extraction)",
            file=sys.stderr,
        )
        return 1, {}

    if "findings" not in body:
        print(
            "ERROR: 'findings' key missing from LEDGER_PATCH "
            + "(GO must be represented explicitly as 'findings: []')",
            file=sys.stderr,
        )
        return 3, {}

    findings_raw = body["findings"]
    if findings_raw is None:
        print(
            "ERROR: 'findings' is null; GO must be represented explicitly as 'findings: []'",
            file=sys.stderr,
        )
        return 3, {}
    if not isinstance(findings_raw, list):
        print(
            f"ERROR: 'findings' must be a list (got {type(findings_raw).__name__})",
            file=sys.stderr,
        )
        return 3, {}

    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_f in findings_raw:
        if not isinstance(raw_f, dict):
            print(
                f"ERROR: finding entry is not a mapping (got {type(raw_f).__name__})",
                file=sys.stderr,
            )
            return 3, {}
        normalized = normalize_finding(raw_f, warnings)
        if normalized is None:
            # normalize_finding already printed the specific error to stderr.
            return 3, {}
        # Issue #100 case #13: duplicate id within a block silently overwrote
        # the first entry once Step 4 keyed the ledger by id. Reject strictly —
        # findings never deleted, only status changes (SKILL.md invariant).
        nid = normalized["id"]
        if nid in seen_ids:
            print(
                f"ERROR: duplicate finding id {nid!r} within LEDGER_PATCH block "
                + "(findings keyed by id; downstream ledger update would silently overwrite)",
                file=sys.stderr,
            )
            return 3, {}
        seen_ids.add(nid)
        findings.append(normalized)

    meta = {k: v for k, v in body.items() if k != "findings"}

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    return 0, {
        "findings": findings,
        "meta": meta,
        "source": "empty_findings" if not findings else "ledger_patch",
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract LEDGER_PATCH YAML block from codex verdict.",
        epilog="See exit codes in the script docstring.",
    )
    parser.add_argument("--file", type=Path, help="Read verdict from FILE instead of stdin.")
    args = parser.parse_args()

    if args.file:
        if not args.file.exists():
            # Issue #100 case #5: split file-not-found out of exit 3
            # (schema violation). Different caller action — fix the path / retry,
            # not "STOP and ask user about the patch body".
            print(f"ERROR: file not found: {args.file}", file=sys.stderr)
            return 5
        # Hotfix R3-F1 (issue #110 follow-up): wrap read_text so IO failures
        # (passing a directory, permission denied, broken pipe on a fifo,
        # decode errors) exit 5 instead of leaking as a raw Python traceback
        # with exit 1 — exit 1 means "no LEDGER_PATCH block" to the wrapper.
        try:
            text = args.file.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"ERROR: failed to read {args.file}: {exc}", file=sys.stderr)
            return 5
        # Replace the trailing suffix entirely (verdict-r1.txt → verdict-r1.malformed.yml),
        # matching the docstring and SKILL.md Step 4 contract.
        malformed_target = args.file.with_suffix(".malformed.yml")
    else:
        # Code-review AL5: symmetric exit-5 coverage for stdin io failure.
        # Without this, a closed/broken stdin pipe surfaces as an uncaught
        # OSError → Python traceback + exit 1, which SKILL.md Step 4 routes
        # to "extract from prose manually" — wrong action for a wiring bug.
        try:
            text = sys.stdin.read()
        except OSError as exc:
            print(f"ERROR: failed to read stdin: {exc}", file=sys.stderr)
            return 5
        # TODO(A3IO/jaine-plugins#100, bonus side-finding from case #11): stdin
        # mode writes the malformed dump into the caller's cwd — a side effect
        # on a nominally stateless transformer. Not hit by the skill flow (Step 4
        # uses --file mode), but ad-hoc stdin testing materializes
        # stdin-ledger.malformed.yml in whatever directory the user invoked from.
        # Revisit when PR1b composer wrapper (#102) lands so we can route the
        # malformed dump through the per-review .bulldozer/<session>/ directory.
        malformed_target = Path.cwd() / "stdin-ledger.malformed.yml"

    blocks = extract_ledger_blocks(text)
    if not blocks:
        # Issue #100 case #17 (altitude): if the reviewer wrote a bare "GO" line
        # without the structured block, synthesize empty findings rather than
        # forcing the caller into manual prose extraction. Bare GO is the only
        # verdict where prose is unambiguous enough to synthesize safely —
        # NO-GO + findings cannot be safely converted from prose.
        #
        # Dogfood R1-F1: NO-GO prose containing a stray bare "GO" line
        # (e.g. instructional "the reviewer should not write: GO") would
        # silently override the explicit NO-GO verdict, losing real findings.
        # Suppress synthesis when any NO-GO variant appears in the verdict.
        # Code-review D4: strip fenced markdown code blocks before checking
        # bare-GO and NO-GO patterns. Illustrative `GO` / `NO-GO` lines
        # inside ``` fences (reviewer quoting the directive format) must
        # not trigger synthesis or suppression — only the actual verdict
        # text outside fences counts.
        text_outside_fences = _FENCED_CODE_RE.sub("", text)
        if _BARE_GO_RE.search(text_outside_fences) and not _NO_GO_RE.search(text_outside_fences):
            warning = (
                "reviewer omitted LEDGER_PATCH on bare GO — "
                + "synthesized empty findings (Round-N prompt directive ignored)"
            )
            print(f"WARN: {warning}", file=sys.stderr)
            payload: dict[str, Any] = {
                "findings": [],
                "meta": {"verdict": "go", "synthesized": True},
                "source": "synthesized_bare_go",
                "warnings": [warning],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return 0
        print("ERROR: no LEDGER_PATCH block found in input", file=sys.stderr)
        return 1

    # Use the LAST block (closest to end of verdict — most-recent reviewer
    # output). Issue #100 case #11: if the last block has malformed YAML
    # (exit 2), fall back to earlier valid blocks rather than silently
    # discarding the entire batch. Schema violations / non-mapping bodies
    # (exits 1, 3) propagate immediately — those are "structurally wrong" or
    # "no usable block" signals that mean what they say regardless of position.
    total_blocks = len(blocks)
    code = 2
    used_idx: int | None = None
    payload: dict[str, Any] = {}
    for idx in range(total_blocks - 1, -1, -1):
        # Only the original "last block" attempt writes the malformed dump
        # file — earlier fallback attempts pass None so parse() suppresses
        # the on-disk dump entirely (code-review E1: avoid the misleading
        # "WARN: malformed YAML saved to /dev/null" the old devnull sentinel
        # produced).
        target: Path | None = malformed_target if idx == total_blocks - 1 else None
        code, payload = parse(blocks[idx], target)
        if code != 2:
            used_idx = idx
            break
        if idx > 0:
            print(
                f"WARN: LEDGER_PATCH block at index {idx} malformed; "
                + "falling back to earlier block",
                file=sys.stderr,
            )
    if code != 0:
        return code

    # Dogfood R1-F2: surface fallback provenance into the JSON payload, not
    # only stderr — downstream consumers reading parsed-rN.json need the
    # signal that they're applying a stale earlier block, not the latest.
    if used_idx is not None and used_idx != total_blocks - 1:
        fallback_msg = (
            f"last LEDGER_PATCH block (index {total_blocks - 1} of {total_blocks}) "
            + f"was malformed; used earlier valid block at index {used_idx}"
        )
        payload.setdefault("warnings", []).append(fallback_msg)
        meta = payload.setdefault("meta", {})
        # Code-review C4: detect collision with reviewer-supplied meta keys
        # so the silent overwrite of `used_block_index` / `total_blocks` is
        # surfaced as a warning. Fallback-injected values still win (the
        # caller needs reliable provenance), but the loss is visible.
        for key, new_value in (("used_block_index", used_idx), ("total_blocks", total_blocks)):
            if key in meta and meta[key] != new_value:
                payload["warnings"].append(
                    f"fallback metadata overwrote reviewer-supplied {key!r}={meta[key]!r}"
                )
        meta["used_block_index"] = used_idx
        meta["total_blocks"] = total_blocks

    # default=str is the secondary safety net for any non-JSON-serializable values
    # that survive past _StringTimestampLoader (e.g., bytes from explicit !!binary
    # tag, sets from explicit !!set tag). The primary fix for implicit timestamps
    # — both as values AND as dict keys — happens at the YAML loader layer above.
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
