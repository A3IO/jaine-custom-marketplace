# Code Verification (MANDATORY)

After generating draft changelog, MUST verify ALL technical claims against code.

## Why This Matters

Commit messages can contain:
- Incorrect method names (typos, renamed later)
- Outdated terminology
- Incomplete implementation details

**Verification catches these BEFORE publication.**

---

## Verification Process

### Step 1: Extract Verifiable Claims

Parse generated markdown and collect:

```python
claims = {
    "methods": [],        # method_name(), function_name()
    "endpoints": [],      # /api/endpoint-name
    "new_files": [],      # Files marked as NEW
    "events": [],         # SSE events, WebSocket messages
    "cli_options": [],    # --option-name
    "classes": [],        # class ClassName
    "per_file_stats": [], # +N/-M строк per file (from File-by-File Analysis)
    "issue_pr_refs": [],  # #N references — is it issue or PR?
}
```

Find in document:
- Method/function names: `method_name()`, `async def method_name`
- API endpoints: `/api/...`
- New files marked as NEW
- SSE/WebSocket events
- CLI options: `--option-name`
- Per-file line counts: `+N/-M строк` in File-by-File Analysis tables
- Issue/PR references: all `#N` patterns

### Step 2: Run Verification Commands

**Methods/Functions:**
```bash
# Must find EXACT match in source
grep -rn "def {method_name}\|async def {method_name}" src/
```

**API Endpoints:**
```bash
# Must find route decorator
grep -rn "@app\.\(post\|get\|put\|delete\).*{endpoint}" src/
```

**New Files:**
```bash
# Must exist
test -f {path} && echo "EXISTS" || echo "NOT FOUND"
```

**SSE/Protocol Events (CRITICAL - check BOTH sides!):**
```bash
# Server side
echo "=== SERVER ===" && grep "event: {name}" src/**/server.py

# Client side
echo "=== CLIENT ===" && grep "{name}" src/**/client.py

# If only server -> add warning note!
```

**CLI Options:**
```bash
grep -rn "\-\-{option}" src/**/cli/*.py
```

**Per-File Line Counts:**
```bash
# Must match git diff --numstat EXACTLY
git diff TARGET..HEAD --numstat | grep "filename"
# Compare with "+N/-M строк" claims in File-by-File Analysis
# NEVER trust LLM-estimated counts — numstat is the only truth
```

**Issue/PR References:**
```bash
# Forgejo: check if #N is issue or PR
curl -s -H "Authorization: token $FORGEJO_API_TOKEN" \
  "$FORGEJO_API_URL/repos/OWNER/REPO/issues/N" \
  | jq '{number: .number, is_pr: (.pull_request != null)}'
# is_pr=true → "PR #N" with /pulls/N URL, NOT in "Issues" table

# GitHub: same check
gh api repos/OWNER/REPO/issues/N --jq '.pull_request != null'
```

### Step 3: Classification

| Status | Meaning | Action |
|--------|---------|--------|
| VERIFIED | Found exactly as stated | Keep |
| PARTIAL | Exists but different | Update to correct |
| ONE-SIDED | Server-only or client-only | Add warning note |
| NOT FOUND | Claim not in code | Fix or remove |

### Step 4: Apply Corrections

1. Update incorrect method/class names
2. Add warning notes for partial implementations
3. Remove claims that don't exist
4. Update Known Issues with discovered gaps

### Step 5: Add Verification Report

Include in document footer:

```markdown
## Code Verification

### Verified (X claims)
| Claim | Command | Result |
|-------|---------|--------|
| `stream_and_play()` | `grep "def stream_and_play"` | Line 698 |

### Corrections Applied (X)
| Original | Corrected | Evidence |
|----------|-----------|----------|
| `stream_sse_and_play` | `stream_and_play` | daemon_client.py:698 |

### Warnings Added (X)
| Item | Issue | Note Added |
|------|-------|------------|
| `debug_info` event | Client ignores | Warning in SSE Events section |
```

---

## Example Verification Session

```bash
# Claim: stream_and_play() method exists
$ grep -rn "def stream_and_play" src/
src/jaine_speech/core/daemon_client.py:698:    async def stream_and_play(

# Result: VERIFIED at line 698

# Claim: debug_info SSE event
$ grep "event: debug_info" src/**/server.py
src/jaine_speech/daemon/server.py:845:    - event: debug_info

$ grep "debug_info" src/**/daemon_client.py
(no output)

# Result: PARTIAL - server sends, client ignores
# Action: Add warning note to SSE Events section
```
