# adversarial-review — Bug Report (first real run, 2026-05-09)

**Tested on:** jaine-speech repo, spec `preset-name-storage-unification-design.md`
**Result:** skill worked (3 rounds, 11 findings), but 3 bugs found in logging/state

---

## Bug 1: Cache served v0.1.0, not v2026.05.09

**Symptom:** Log writes to `~/.adversarial-review.log` instead of `~/.claude/hooks/adversarial-review.log`.

**Cause:** `~/.claude/plugins/cache/jaine-custom/adversarial-review/0.1.0/` contains the old version (before CalVer rename and log path fix). Cache was not rebuilt after commits `625623f` (CalVer) and `5944b26` (log path).

**Fix:** Clear cache and ensure CC rebuilds from current worktree source:
```bash
rm -rf ~/.claude/plugins/cache/jaine-custom/adversarial-review/
# Restart CC — cache will rebuild from plugins/adversarial-review/ worktree
```

**Verify:** After restart, check `~/.claude/plugins/cache/jaine-custom/adversarial-review/*/. claude-plugin/plugin.json` shows `"version": "2026.05.09"`.

---

## Bug 2: State history has only round 3, missing rounds 1-2

**Symptom:** `.adversarial-review/state.json` shows:
```json
{
  "round": 3,
  "findings_total": 1,   // should be 11 (7+3+1)
  "fixed_total": 1,       // should be 4 (0+3+1)
  "history": [
    {"round": 3, ...}     // rounds 1 and 2 missing
  ]
}
```

**Likely cause:** `update-state.py` recreates state from scratch each call instead of reading existing file, OR the agent only called it on round 3. Check:
1. Was `update-state.py` called after rounds 1 and 2? (grep CC session transcript)
2. If called — does the script correctly read existing `state.json`? The `state_file.exists()` check at line 25 should preserve prior history.

**Possible root cause:** The script's `state["findings_total"] += findings` uses `+=` on a value loaded from JSON (which Python types as `int`). This is fine. More likely the agent simply didn't call the script after every round — the SKILL.md says to call it but doesn't enforce it.

**Fix options:**
- (a) Make SKILL.md more explicit: "You MUST call update-state.py AFTER EVERY round, not just the last one"
- (b) Move state update into `log-round.sh` so one script does both (fewer steps for Claude to forget)

---

## Bug 3: Hook fires from unrelated contexts (project=/tmp)

**Symptom:** 10 out of 17 log entries have `project=/tmp`:
```
2026-05-09T15:47:42+07:00 | event=invoke | ... | project=/tmp
```

**Cause:** The `UserPromptSubmit` hook matcher `^/adversarial-review\b` fires on the slash command, but also on ANY user prompt starting with `/adversarial-review`. The `/tmp` entries suggest the hook fires in other CC sessions where the user typed the command but wasn't in a git repo (CWD=/tmp fallback from `git rev-parse --show-toplevel`).

This isn't a false match — these are real invocations from other sessions/tabs. The hook correctly logged them. The `/tmp` just means the CC session had no git repo context.

**Verdict:** Not a real bug — hook works correctly. The `/tmp` entries are from sessions where the user was testing the command outside a repo. Could add `[[ -d .git ]] || exit 0` guard if non-repo invocations should be silent, but logging them is arguably useful for debugging.

---

## Bug 4: `update-state.py` path doesn't match SKILL.md review directory layout

**Symptom:** `update-state.py` always writes to `.adversarial-review/state.json` (hardcoded at line 22), but SKILL.md v2 describes per-review dirs: `.adversarial-review/${SESSION}-${ARTIFACT_NAME}/state.json`.

**Cause:** `update-state.py` was written before per-review dirs were added in commit `868b85d`. The script was never updated to accept a review directory path.

**Consequence:** All reviews in the same repo overwrite the same `state.json`. If two reviews run concurrently (different artifacts or sessions), their state collides. After switching to a new artifact, `state.json` still contains the previous review's history.

**Fix:** `update-state.py` should accept `--review-dir` argument (defaulting to `.adversarial-review/` for backward compat). `log-round.sh` should pass the review dir through. SKILL.md step 6 already shows the correct call site — the scripts just need to respect the per-review dir.

```
update-state.py --review-dir "$REVIEW_DIR" ROUND VERDICT FINDINGS FIXED [FP] [ARTIFACT] [DEPTH] [REVIEWER]
```

Or simpler: accept `REVIEW_DIR` as env var (like `ADVERSARIAL_REVIEW_LOG` pattern already used for log path).

---

## Summary

| # | Severity | Bug | Fix | Status |
|---|----------|-----|-----|--------|
| 1 | **High** | Stale cache serves v0.1.0 | `rm -rf` cache dir + restart | **FIXED** |
| 2 | **Medium** | State missing rounds 1-2 | `log-round.sh` auto-calls `update-state.py` | **FIXED** |
| 3 | **Low** | /tmp in hook logs | Not a real bug | **CLOSED** |
| 4 | **Medium** | state.json path ignores per-review dirs | `update-state.py` needs `--review-dir` | **OPEN** |

---

*First live test: 2026-05-09, jaine-speech repo, standard depth, 3 rounds*
