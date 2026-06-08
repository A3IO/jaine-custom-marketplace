#!/usr/bin/env python3
"""Read depth parameters from data/depth-config.json (B1, #110).

Usage: read-depth-config.py <config_path> <depth>

Prints "max_rounds<TAB>reasoning<TAB>ephemeral<TAB>prompt_prefix" to stdout for
the given depth. prompt_prefix is LAST so its significant trailing space (quick
depth's "SKIP SKILLS. ") survives `IFS=$'\t' read` in the wrapper — only TAB is
an IFS delimiter there, so the embedded/ trailing space is preserved.

This is the single reader the wrapper uses to derive max_rounds, the codex
reasoning effort, the --ephemeral toggle, and the quick-depth prompt prefix —
replacing three duplicated `case "$DEPTH"` blocks (B1). The SKILL.md "Depth
Levels" table mirrors the same JSON (guarded by TestDepthConfigContract).

Exit codes:
    0  ok — TAB-delimited line written to stdout
    2  unknown depth (not a key in the config) — wrapper maps to usage 64
    3  config missing / unreadable / corrupt JSON / malformed entry —
         wrapper maps to _emit_stop 70
"""
import json
import sys


def main(argv):
    if len(argv) != 3:
        print("usage: read-depth-config.py <config_path> <depth>", file=sys.stderr)
        return 3
    config_path, depth = argv[1], argv[2]
    try:
        # encoding="utf-8" + UnicodeDecodeError in the except: a non-UTF8 file
        # otherwise raises UnicodeDecodeError (a ValueError subclass, NOT
        # OSError/JSONDecodeError) UNCAUGHT — exit 1 traceback instead of the
        # documented exit-3 corruption contract (R1-F1, PR-3b dogfood R2).
        with open(config_path, encoding="utf-8") as fp:
            config = json.load(fp)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"error: cannot read depth config {config_path}: {exc}", file=sys.stderr)
        return 3
    if not isinstance(config, dict):
        print(f"error: depth config {config_path} is not a JSON object", file=sys.stderr)
        return 3
    # R3-F1(b) (PR-3b dogfood R3): the shipped depth-config.json always carries
    # all three built-in depths. A config missing any of them is corrupt/
    # truncated, not a user error — fail closed as corruption (exit 3 → wrapper
    # 70) BEFORE lookup, so even a request for a present depth fails when its
    # siblings are gone. Checked before the per-depth lookup below.
    required = {"quick", "standard", "exhaustive"}
    absent = required - set(config)
    if absent:
        print(f"error: depth config {config_path} missing required depth(s): "
              f"{sorted(absent)}", file=sys.stderr)
        return 3
    # Distinguish a genuinely-absent depth (user typo in --depth, e.g. "bogus",
    # while the config is complete → exit 2 → wrapper usage 64) from a present-
    # but-corrupt entry (null/non-object → exit 3 → wrapper 70). `config.get`
    # conflated present-null with absent (R1-F1 R2).
    if depth not in config:
        print(f"error: unknown depth '{depth}' (keys: {sorted(config)})", file=sys.stderr)
        return 2
    params = config[depth]
    if not isinstance(params, dict):
        print(f"error: entry for depth '{depth}' is not an object (got {params!r})",
              file=sys.stderr)
        return 3
    # R1-F1 (PR-3b dogfood): STRICT type validation — fail closed, never coerce.
    # Python's bool("false") is True and int(True) is 1, so the previous
    # str(bool(...)) / int(...) path silently turned a malformed-but-parseable
    # value into a valid-looking one (string "false" → ephemeral=true; bool
    # max_rounds → 1) with rc 0. A refactor must not change wrapper behavior on
    # bad data — exit 3 (→ wrapper _emit_stop 70) so the operator fixes the
    # config instead of getting a silently-wrong run. bool is rejected for
    # max_rounds explicitly because it is an int subclass (isinstance(True, int)).
    missing = [k for k in ("max_rounds", "reasoning", "ephemeral", "prompt_prefix")
               if k not in params]
    if missing:
        print(f"error: depth '{depth}' missing key(s): {missing}", file=sys.stderr)
        return 3
    mr = params["max_rounds"]
    if not isinstance(mr, int) or isinstance(mr, bool) or mr < 1:
        print(f"error: depth '{depth}' max_rounds must be a positive integer "
              f"(got {mr!r})", file=sys.stderr)
        return 3
    if not isinstance(params["reasoning"], str) or not params["reasoning"]:
        print(f"error: depth '{depth}' reasoning must be a non-empty string "
              f"(got {params['reasoning']!r})", file=sys.stderr)
        return 3
    if not isinstance(params["ephemeral"], bool):
        print(f"error: depth '{depth}' ephemeral must be a JSON boolean "
              f"(got {params['ephemeral']!r})", file=sys.stderr)
        return 3
    if not isinstance(params["prompt_prefix"], str):
        print(f"error: depth '{depth}' prompt_prefix must be a string "
              f"(got {params['prompt_prefix']!r})", file=sys.stderr)
        return 3
    # Reject TAB/CR/LF in the two string fields: output is TAB-delimited and
    # single-line, so an embedded TAB corrupts the wrapper's `IFS=$'\t' read`
    # (extra field) and a CR/LF breaks the single-line read contract (R1-F1 R2).
    for fld in ("reasoning", "prompt_prefix"):
        if any(c in params[fld] for c in ("\t", "\n", "\r")):
            print(f"error: depth '{depth}' {fld} must not contain TAB/CR/LF "
                  f"(got {params[fld]!r})", file=sys.stderr)
            return 3
    ephemeral = "true" if params["ephemeral"] else "false"
    sys.stdout.write(
        f"{mr}\t{params['reasoning']}\t{ephemeral}\t{params['prompt_prefix']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
