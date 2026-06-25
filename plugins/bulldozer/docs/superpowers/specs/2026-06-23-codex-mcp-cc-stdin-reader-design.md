# Codex-MCP F4: Shared `os.read`+`JsonRpcStream` CC stdin reader — Design

**Issue:** A3IO/jaine-plugins#264 (follow-up to #218/#252 interruptible turns; deferred there as F4)

**Goal:** Replace the three `select() + sys.stdin.readline()` CC-stdin read sites in
`mcp/codex_server.py` with one shared raw-fd reader (`CCStream`) so a burst of multiple
JSON-RPC frames in a single CC OS write can no longer strand the 2nd+ frame in the
`TextIOWrapper` buffer (where `select` can't see it) — closing the F4 missed-cancel hole.

**Architecture:** A module-singleton `CCStream` owns CC stdin (fd 0). It wraps the existing,
battle-tested `JsonRpcStream` byte buffer plus a frame queue and an EOF flag. `os.read`
drains the OS fd fully each call; `JsonRpcStream` parses complete frames into the queue;
consumers (`main()`, `cc_read_fn`, `Reactor.pump(watch_cc=True)`) pull from the queue
**queue-first** (no `select` needed when a frame is already buffered), which is exactly what
makes a queued 2nd frame visible to the next read.

**Tech Stack:** Python 3.11+ stdlib only (`os`, `select`, `collections.deque`, `json`). No
new dependencies. Single file touched: `mcp/codex_server.py` (+ its test suite
`tests/test_codex_mcp_v2.py`).

## Global Constraints

- **Python 3.11+** (existing server floor; `tomllib`). No new deps.
- **stdout = JSON-RPC frames only**; all logging to stderr (existing invariant).
- **Swap ALL three CC-side `readline` at once.** `os.read(fd)` and `TextIOWrapper.readline()`
  must never both read the same fd — mixing duplicates/truncates/loses bytes. After this
  change there must be **zero** `sys.stdin.readline()` / `for line in sys.stdin` on the CC path.
- **CC-facing output contracts are byte-identical** to the pre-refactor code (validated by
  the equivalence tests below). This is a pure read-path refactor; no protocol change.
- **No manual `plugin.json` bump** (auto-calver on merge).
- **Full pytest incl. `-m slow` (~3 min real codex 0.141)** after the change — `codex_server.py`
  edits always trigger the slow e2e suite.

---

## Problem (F4, dogfood-confirmed, deferred by the #218 GO'd spec)

`mcp/codex_server.py` reads CC's stdin in three places. Two of them —
`cc_read_fn` and `Reactor.pump(watch_cc=True)` — use `select([sys.stdin],…) +
sys.stdin.readline()`; `main()` blocks on a **bare** `sys.stdin.readline()` between turns
(no `select`). All three must convert together (the `os.read`/`readline` no-mixing invariant
under Global Constraints):

1. **`main()` dispatcher loop** — **bare blocking** `sys.stdin.readline()` (no `select`);
   reads the next `tools/call` / `initialize` between turns.
2. **`cc_read_fn(timeout)` closure** — `select` + `readline`; the approval/elicitation reader.
   Returns a parsed dict, `None` (timeout / blank / bad JSON), or the `_CC_EOF` sentinel on EOF.
3. **`Reactor.pump(watch_cc=True)`** — `select`s `child_out_fd` AND `sys.stdin`, then `readline`;
   the turn pump. Tags a CC line as `{"__cc__": <parsed-or-None>}` or
   `{"__cc__": {"__eof__": True}}` on EOF; at most one CC line per call.

`select()` tests the **OS fd**, but `TextIOWrapper.readline()` can pull a **second** buffered
line into Python space while draining the fd and return only the first. If CC ever writes 2+
frames in one OS write mid-turn (e.g. a `ping`/`tools/list` immediately followed by
`notifications/cancelled`), the 2nd line is stranded in the `TextIOWrapper` buffer; the next
`select` reports the fd **not-ready** (no new OS bytes), so a queued **cancel can be missed**
until some unrelated frame arrives.

**Severity in practice:** the single mid-turn Esc-cancel (one frame) is unaffected and works
today; this only bites if CC batches frames in one write — **not an observed CC behavior**
(hypothetical). It degrades gracefully (the turn just doesn't interrupt; no crash/corruption).
Both the in-house `codex_review` and an independent reviewer flagged it during #218. Deferred
because the fix touches the core MCP dispatcher (`main()` read loop) — highest blast radius
in the server — so it warranted a deliberate, reviewed change.

---

## Design

### `CCStream` — the single CC-stdin owner

```python
class CCStream:
    """Single owner of CC stdin (fd 0) (#264). All CC-side reads route here so a burst of
    frames in one OS write is fully drained (os.read) and parsed (JsonRpcStream) into a queue
    — no TextIOWrapper-buffer stranding. os.read and TextIOWrapper.readline must NEVER both
    read this fd: every CC read goes through CCStream, none through readline."""

    def __init__(self):
        self._stream = JsonRpcStream()      # reuse the proven child-side byte buffer
        self._queue = collections.deque()   # complete parsed frames awaiting delivery
        self._eof = False

    def _fd(self) -> int:
        # Resolve LAZILY each call so a test that monkeypatches sys.stdin (os.fdopen(pipe))
        # is honored. The production fd is 0; never cached.
        return sys.stdin.fileno()

    def has_queued(self) -> bool:
        """True iff a parsed frame is already buffered (delivery needs no select).
        Used by pump to force a 0 child-select timeout so a queued CC frame is delivered
        promptly (no .queue leak — pump never touches the deque directly)."""
        return bool(self._queue)

    def _drain_ready(self, timeout) -> None:
        """select once; if the fd is ready, os.read the whole chunk and feed it to the
        JsonRpcStream, extending the queue. A 0-byte read => EOF (sticky)."""
        if self._eof:
            return
        ready, _, _ = select.select([self._fd()], [], [], timeout)
        if not ready:
            return
        try:
            chunk = os.read(self._fd(), 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            return
        self._queue.extend(self._stream.feed(chunk))

    def next_frame(self, timeout):
        """Return (kind, value):
           ('frame', dict) — a parsed frame (queue checked FIRST: a prior burst's 2nd frame
                             is delivered WITHOUT re-selecting — this is the F4 fix)
           ('none',  None) — timeout / only a partial frame this call (retry within deadline)
           ('eof',   None) — fd closed AND queue fully drained (real bytes always precede the
                             0-byte read on a pipe, so the queue empties before EOF surfaces)
        """
        if self._queue:
            return ("frame", self._queue.popleft())
        self._drain_ready(timeout)
        if self._queue:
            return ("frame", self._queue.popleft())
        if self._eof:
            return ("eof", None)
        return ("none", None)
```

Module singleton + test reset:

```python
_cc_stream = CCStream()                      # like _v2_manager / _v2_state_machine

def _reset_cc_stream() -> None:
    """Fresh CC buffer/queue/eof. Called at main() startup (harmless in prod) and by an
    autouse test fixture so same-process tests never inherit queued frames / _eof / _buf."""
    global _cc_stream
    _cc_stream = CCStream()
```

### Why the fix works

The burst `ping\n` + `notifications/cancelled\n` is read by **one** `os.read` → fed into
`JsonRpcStream` → **both** frames land in `_queue`. The consumer gets `ping` first; the next
`next_frame` checks the **queue first** and returns `cancel` **without** waiting for new fd
bytes. With `readline`, the 2nd line sat in the `TextIOWrapper` buffer that `select` could not
see, so the cancel was stranded.

### Consumer mapping (contracts preserved 1:1)

| Callsite | Today | After |
|---|---|---|
| `main()` loop | `line = readline()`; `""` → break; blank/bad-JSON → continue | `kind,frame = _cc_stream.next_frame(None)` (blocks between turns); `'eof'` → break; `'none'` → continue; `'frame'` → dispatch |
| `cc_read_fn(timeout)` | dict / `None` / `_CC_EOF` | `'frame'`→dict, `'none'`→`None`, `'eof'`→`_CC_EOF` |
| `Reactor.pump(watch_cc=True)` | `select([child_fd, sys.stdin])`; ≤1 CC line tagged `{"__cc__":…}` / `{"__cc__":{"__eof__":True}}` | `select([child_fd] + ([] if _cc_stream.has_queued() else [cc_fd]))`; child read unchanged; then `next_frame(0)` → same tag, still ≤1 CC frame/call. When `has_queued()`, child-select uses `timeout=0` so the queued CC frame is delivered promptly. |

`pump(watch_cc=False)` (the default, child-only path) is **untouched** — byte-identical select
list `[self._child_out_fd]` and child read. This preserves `test_reactor_pump_default_is_child_only`
(which asserts `rlist == [child.fileno()]`).

### `main()` — the dispatcher loop, converted whole

`CCStream` must be the **first and only** stdin reader in the process. `main()`'s first action
becomes `_reset_cc_stream()` then `next_frame(None)`; no `readline` runs before it, so no CC
bytes are ever eagerly buffered into the `TextIOWrapper` and lost. The loop's frame-dispatch
body (`initialize` / `tools/list` / `tools/call` / method-not-found / response-shaped-ignore)
is unchanged — only the read at the top changes.

---

## Equivalence guarantees (asserted by tests — must NOT regress)

| Behavior | Today (`readline`) | After (`CCStream`) | Test |
|---|---|---|---|
| **EOF** | `readline()`→`""` → `_CC_EOF` / `{"__cc__":{"__eof__":True}}` | `os.read`→`b""` sets sticky `_eof`; queue drains first (real bytes precede the 0-byte read) → same sentinels | new + existing `tags_eof` |
| **blank line** | `cc_read_fn`→`None`; `pump` appends **nothing** (`line.strip()`→`""`, the inner `if line:` is False — current code lines 557-558) | `JsonRpcStream.feed` skips blank lines → `cc_read_fn`→`None`; `pump` appends nothing. Identical. | new |
| **bad JSON** | `cc_read_fn`→`None` (JSONDecodeError); `pump`→`{"__cc__":None}` (only the non-empty-but-unparseable case) | `JsonRpcStream.feed` drops it → `cc_read_fn`→`None`; `pump` emits **no** `{"__cc__":None}`. Verified: **no test asserts `{"__cc__":None}`**, and `_route_cc_frame(None)` was already a no-op (never "interrupt") → equivalent | new |
| **≤1 CC frame / pump call** | one `readline` per call | one `next_frame(0)` per call, appends ≤1 `{"__cc__":…}` | existing watch_cc tests + new |
| **child-vs-CC order in a pump batch** | child frames, then ≤1 CC frame | identical (child read+feed first, CC frame appended after) | existing |
| **single Esc-cancel (1 frame)** | works | works (queue holds the one frame) | existing interrupt tests |

### The NEW regression test (RED first)

CC writes `ping\n` + `notifications/cancelled\n` for our turn in **one** `os.write` mid-turn;
drive `codex_run_v2` (or `pump(watch_cc=True)` across two iterations) and assert the cancel is
observed and interrupts the turn. Under the current `readline` code this strands the cancel
(the test fails RED); under `CCStream` the queued 2nd frame is delivered (GREEN).

**Scope precision (R1-F1):** #264 fixes the *stranding* — the 2nd frame becomes visible to the
next `pump` / `cc_read_fn` instead of sitting unseen in the `TextIOWrapper` buffer. It does NOT
change the existing #218 batch-ordering rule: within one pump batch child frames are processed
before the appended CC frame, and a CC cancel is *deferred to end-of-batch* (`cancel_pending`,
codex_server.py lines 2814-2818), so a turn that *completes* in the same batch returns its
completed result (only EOF has batch-priority — lines 2804-2809). The regression test therefore
drives the cancel while the turn is still streaming (no same-batch terminal child frame) so it
deterministically interrupts — that is the real F4 scenario. A turn finishing simultaneously
with the cancel and returning "completed" is correct, not a regression. **Bonus property the
shared queue gives for free:** a burst of `[elicitation-response, cancel]` during an approval
wait resolves the elicitation (via `cc_read_fn`) AND leaves the cancel queued for the turn
loop's next `pump` — under `readline` that trailing cancel was stranded. Worth a test.

---

## Panel-adopted refinements (consult find-holes, 2026-06-23 — codex+grok+agy, informed)

The 3-model panel read the real code and confirmed the 3 critical design points (force
child-select `timeout=0` on a queued frame; swap ALL `readline` at once; reset the singleton
between tests) and surfaced these refinements, all folded in above:

1. **Autouse pytest fixture** resets `_cc_stream` before every test (comprehensive vs. ~4
   manual resets — covers same-process leakage of queue/`_eof`/`_buf`). *(GPT#8, Grok#8)*
2. **`CCStream.has_queued()` predicate** — `pump` forces `timeout=0` via it without leaking the
   `.queue` abstraction. *(Gemini#2)*
3. **Doc inversion** — the stdin-ownership comment (`codex_server.py` module header, currently
   "MUST use sys.stdin.readline() consistently in the loop (NOT `for line in sys.stdin`)") and
   the `main()` docstring invert to "MUST route every CC read through `CCStream`; never
   `readline` on the CC fd". *(Grok#3, GPT#5)*
4. **Grep-guard test** — assert zero `sys.stdin.readline()` / `for line in sys.stdin` remain in
   the CC path (a structural test reading the source). *(Grok#3, Gemini "mixing")*
5. **`CCStream` is the first stdin reader** — `main()` converted whole so nothing buffers CC
   bytes into the `TextIOWrapper` before `CCStream` takes over. *(Gemini#3)*

### Triaged as pre-existing / false-positive (out of #264 scope)

- **`drained_frames` prepending order (GPT#3)** — existing #252 code; this refactor does not
  change child-vs-CC ordering within a pump batch.
- **approval-wait drops id-bearing CC frames (Grok#2 / codex R1-F2)** — `read_correlated`
  consumes and *skips* any non-elicitation CC frame today (line 1676); a mid-approval cancel for
  our turn IS handled (`cancel_during_approval`, not lost), but an id-bearing CC *request* (e.g. a
  `ping`) is dropped with **no reply**. #264 is **byte-equivalent** here: the shared queue has
  exactly one consumer at a time — during the approval wait only `cc_read_fn` reads CC stdin
  (`pump` is called `watch_cc=False`, line 1647), and after it returns only `pump(watch_cc=True)`
  reads — so no frame is double-consumed or misrouted. This is **pre-existing, NOT "safe"**: if CC
  ever sends an id-bearing request mid-approval it would hang waiting for the reply. Out of #264
  scope — flagged as a separate latent concern (candidate follow-up issue), not introduced or
  fixed here.
- **EOF partial-frame in `_buf` (GPT#4, Gemini#6)** — a trailing unterminated frame at EOF is
  dropped today too (`readline`→bad-JSON→`None`) and the new `_buf` remnant is GC'd with the
  stream. Behaviorally equivalent. *(Optional: log if `_buf` is non-empty at EOF for
  observability — low-priority, not load-bearing.)*
- **spurious `select`→`os.read` blocks (Gemini#8)** — theoretical for a single-reader pipe;
  documented assumption, no non-blocking-fd complexity unless tests show flakiness.
- **StringIO test crash (Gemini#4)** — N/A: every existing stdin test uses `os.fdopen(pipe)`
  (real `fileno()`), not `io.StringIO`. The new tests follow the same real-pipe pattern.

---

## Test plan

All tests offline/structural except the mandatory slow e2e re-run.

1. **`CCStream` unit tests** (new): single frame; **burst of 2 frames in one `os.write`** →
   two successive `next_frame` calls return both (the core F4 proof, no second write); partial
   frame across two writes → `'none'` then `'frame'`; bad-JSON dropped → `'none'`; EOF after a
   queued frame → `'frame'` then `'eof'`; EOF with empty queue → `'eof'`.
2. **Consumer equivalence** (new): `cc_read_fn` returns dict/`None`/`_CC_EOF` over a
   `CCStream`-backed pipe; `pump(watch_cc=True)` tags as today; the new mid-turn burst-cancel
   regression test on `codex_run_v2`.
3. **Existing tests migrated**: add the autouse `_reset_cc_stream()` fixture; the three
   stdin-monkeypatching tests (`test_reactor_pump_watch_cc_reads_cc_frame`,
   `…tags_eof`, `_drive_interrupt` helper) keep their pipe setup and pass with the fresh
   singleton. `test_reactor_pump_default_is_child_only` is unchanged (child-only path untouched).
4. **Grep-guard structural test**: no `readline`/`for line in sys.stdin` on the CC path.
5. **Full suite** incl. `-m slow` real-codex e2e (the dispatcher integration path
   `TestV2Dispatcher` exercises the converted `main()` loop end-to-end over a subprocess).

---

## Risks

| Risk | Mitigation |
|---|---|
| Core dispatcher break → server can't talk to CC at all | Output contracts byte-identical + equivalence tests + `TestV2Dispatcher` subprocess e2e + `/bulldozer:check` on this spec + codex_review on the diff + clean single-PR revert |
| EOF ordering / queue-first discipline wrong | Explicit EOF-after-queued and queue-first unit tests |
| A `readline` left on the CC path → buffer mix | Grep-guard structural test + the "swap all at once" constraint |
| Same-process test leakage of singleton state | Autouse reset fixture |
| Reordered/lost CC frame across the nested main→codex_run_v2→pump/cc_read_fn sharing | Single-threaded serial dispatcher: exactly one consumer reads stdin at a time; the burst-cancel regression test covers the nested path |

## Out of scope

- #251 (auto-accept approval on human-timeout) — blocked on a human-timing corpus + security GO.
- The optional EOF-`_buf` observability log — may land as a one-line drift warning, not required.
- Any change to `pump(watch_cc=False)`, child-side framing, or the approval/elicitation protocol.
