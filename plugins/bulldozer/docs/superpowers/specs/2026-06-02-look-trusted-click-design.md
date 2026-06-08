# #140 — Trusted click via CDP Input (bulldozer:look)

- **Status:** Design (brainstorm complete) — 2026-06-02 — awaiting review
- **Issue:** `A3IO/jaine-plugins#140`. Resolves the AudioContext half of `#93` (same root cause + same fix the reporter proposed); #93 stays open for its LAN-fetch half. The headless + multi-session isolation requests are decomposed to `#141`.
- **Author:** JAINE + Crís
- **Scope:** `cmd_click` only.

---

## 1. Problem

`/look`'s `cmd_click` (`cdp.py`) issues `el.click()` from page-script context — via `Runtime.evaluate` on the websocket channel, or `as_js_main_world` on the AppleScript channel. Both produce a **synthetic, untrusted** event (`isTrusted === false`) that does **not** grant [user activation](https://developer.mozilla.org/en-US/docs/Web/Security/User_activation).

Consequence: every user-activation-gated browser API stays locked when driven through `/look` — `AudioContext.resume()`, autoplay, clipboard write, pointer lock, fullscreen. The #140 reporter (VRHOT) hit this verifying a Web-Audio TTS UI: clicking "Озвучить" via `cdp click` left `ctx.resume()` unsettled, so `prodStream()` hung before its `fetch` and the server never received the request. A real human click was the only workaround. #93 independently reported the same AudioContext-stays-suspended symptom and proposed the same fix.

## 2. Root cause (verified in code)

`cmd_click` builds `expr = "(function(){ var el=document.querySelector(SEL); if(!el) return 'NOT_FOUND'; el.click(); return 'clicked '+el.tagName })()"` and runs it through:

- websocket: `cdp_js(expr)` → `ws_send(ws_url, "Runtime.evaluate", {expression: expr, returnByValue: True})`
- else: `as_js_main_world(expr)` (AppleScript DOM injection)

`el.click()` invokes the handler directly without a real input event → `isTrusted=false`, no user-activation token. The gated APIs above require a **trusted input event** (e.g. CDP `Input.dispatchMouseEvent`), which `el.click()` is not.

## 3. Design (trusted-by-default with hittability fallback)

Chosen approach **A1**: trusted CDP-Input click is the default on the websocket channel, with an automatic, *warned* fallback to `el.click()` when the element is not hittable — so existing clicks on hidden/occluded elements never regress. Rejected: **A2** (explicit `--trusted` flag — the consumer asked for trusted-by-default; discoverability cost) and **A3** (trusted-only, no fallback — regresses clicks on `display:none`/off-screen/occluded elements that `el.click()` does blind).

### 3.1 Flow — websocket channel (`has_websocket()`)

One `Runtime.evaluate` resolves + scrolls + measures + hit-tests in a single round-trip:

1. `el = document.querySelector(sel)` → if null, return sentinel `NOT_FOUND`.
2. `el.scrollIntoView({block:'center', inline:'center', behavior:'instant'})`. **`behavior:'instant'` is REQUIRED** — the default (`auto`) honors a page's computed CSS `scroll-behavior:smooth` and animates *asynchronously*, so the immediate `getBoundingClientRect()`/`elementFromPoint()` in steps 3-4 would read mid-animation (stale) coordinates and misclassify a below-fold target as not-hittable → a wrong untrusted fallback (the exact failure #140 fixes). `instant` forces a synchronous jump; layout is updated before the same evaluate returns. (Chrome-only CDP context — `instant` is fully supported.)
3. Read `r = el.getBoundingClientRect()` **after** the scroll → `cx = r.left + r.width/2`, `cy = r.top + r.height/2`, `w = r.width`, `h = r.height`.
4. Hit-test: `hit = document.elementFromPoint(cx, cy)`; `onTarget = (hit === el) || el.contains(hit)`.
5. Return `{found:true, cx, cy, w, h, onTarget, tag: el.tagName}`.

Decision on the result:

- **Not hittable** — `w === 0 || h === 0` (zero box: `display:none`, detached, collapsed) **or** `!onTarget` (center occluded by an overlay, or off-viewport so `elementFromPoint` returned null) → **fallback**: run the old `el.click()` via `Runtime.evaluate`; print `clicked <TAG> (fallback: el.click, untrusted)` to stdout; **WARN on stderr** that user activation was not granted; `return 0`.
- **Hittable** — dispatch a **trusted** click at `(cx, cy)` via CDP `Input.dispatchMouseEvent`: `mousePressed` then `mouseReleased` (`button:"left"`, `clickCount:1`). Print `clicked <TAG> (trusted)`; `return 0`.
- `NOT_FOUND` → stderr error, `return 1` (unchanged from today).
- CDP/transport error (`ws_send_seq`/`cdp_js` returns `None`) → `return 1` (unchanged).

Coordinate note: `Input.dispatchMouseEvent` x/y are **CSS pixels** in the layout viewport — the same space as `getBoundingClientRect()`. **No device-pixel-ratio conversion** (the inverse of the screenshot `clip.scale` gotcha from #55: there scaling needed DPR; here none).

### 3.2 Components

- **`ws_send_seq(ws_url, [(method, params), ...])`** — new helper: open ONE websocket connection, send the sequence (press, release) in order, close. Press+release share a connection — avoids connect-per-event overhead and any inter-event race of calling `ws_send` twice. Same error contract as `ws_send` (returns `None` on transport error / CDP error).
- **`cmd_click`** — rewritten per §3.1; the measure/hit-test expression and the dispatch live here. The AppleScript branch (§3.3) keeps today's behavior.
- **Same-target `ws_url`** — `cmd_click` captures one `ws_url = get_tab()["webSocketDebuggerUrl"]` and uses direct `ws_send(ws_url, "Runtime.evaluate", …)` for BOTH the measure/hit-test and the fallback `el.click()`, then passes the SAME `ws_url` to `ws_send_seq`. It does NOT use `cdp_js` here — `cdp_js` re-resolves `get_tab()` on every call, so in a multi-tab browser the measure could land on tab A and the trusted dispatch on tab B. Mirrors `cmd_screenshot`'s `--scale` DPR read (guarded by `test_scale_reads_devicepixelratio_via_same_ws_connection`).

### 3.3 AppleScript channel (no websocket)

`as_js_main_world` injects DOM JS via Apple Events — it has **no access to the CDP Input domain**, so a trusted click is impossible there. It stays `el.click()`, prints `clicked <TAG> (untrusted: AppleScript channel)`, and WARNs on stderr that user activation is not granted in this channel. (Exactly as the #140 reporter suggested: "fall back to `el.click()` only in the AppleScript channel.")

## 4. Error handling / degradation

- Fallback (not hittable) is a **`return 0`** outcome — the click DID happen, just untrusted. The stderr WARN + the `(fallback: …)` / `(untrusted: …)` stdout marker communicate the degradation, per cdp.py's "no silent fallbacks — warn on stderr" principle (mirrors the screenshot-degradation pattern in #55). Callers needing to know whether activation was granted parse the marker.
- `NOT_FOUND` and transport errors keep today's `return 1` semantics — no behavior change.
- Degenerate boxes (element at the viewport edge, center off-screen) fall to the fallback rather than dispatching a click that would land on the wrong element.

## 5. Testing (TDD — visible RED before GREEN; CLAUDE.md cmd_* mandate)

`cmd_click` is not new, but its behavior changes substantially → extend the existing structural + e2e coverage.

- **`tests/fixtures/test-page.html`** — add: (a) a button whose handler records `window.__clickTrusted = event.isTrusted` and `window.__userActivation = navigator.userActivation.isActive`; (b) a `display:none` button with a handler that flips a flag (proves the fallback still fires the handler); (c) a button fully covered by a `position:absolute` overlay div with higher z-index (exercises occlusion → fallback); (d) a below-fold button on a page with CSS `scroll-behavior:smooth` active (regression guard for the instant-scroll requirement in §3.1).
- **`tests/test_e2e.py`** (real browser) — trusted click → `__clickTrusted === true` and activation active; hidden button → fallback + stderr WARN + handler flag still set; occluded button → `elementFromPoint` miss → fallback; **below-fold button on a `scroll-behavior:smooth` page → still trusted** (no fallback WARN, `__clickTrusted === true`, activation active) — guards the instant-scroll requirement.
- **`tests/test_cdp.py`** (offline structural) — `cmd_click` exercises the `Input.dispatchMouseEvent` path; `ws_send_seq` exists and is used; the fallback branch is present and reachable; `COMMANDS["click"]` entry unchanged.

## 6. SKILL.md updates

- `click` now produces a **trusted** gesture on the websocket channel → unblocks audio/autoplay/clipboard/pointer-lock/fullscreen verification.
- Document the fallback: hidden/occluded/off-viewport elements get an untrusted `el.click()` with a stderr warning; the AppleScript channel is always untrusted.
- Audio verification: click the play control (trusted) to start playback — there is no autoplay flag (trusted click already unblocks the audio path; the headless/autoplay discussion lives in #141).
- Active-tab note (#140 part 3): commands act on the active tab; if it has drifted (another effort navigated it), issue an explicit `navigate` or select the tab. (Structurally superseded once #141 multi-session isolation lands.)

## 7. Scope / non-goals

- **In:** `cmd_click` trusted-by-default + fallback (websocket); AppleScript stays untrusted; SKILL.md docs; tests.
- **Out:** `cmd_fill` (setting `.value` + dispatching `input`/`change` needs no user activation — YAGNI); the `--autoplay-policy` flag (trusted click fixes #140's scenario, and an always-on flag would reduce fidelity to real Chrome); headless + multi-session isolation (#141); the `file://`→LAN `http://` fetch / CORS problem (the other half of #93).

## 8. Closes / relates

- **Closes #140** (trusted click).
- **Resolves the AudioContext half of #93** (same root cause + same fix the reporter proposed); #93 stays open for its LAN-fetch half — cross-link on merge.
- Headless + isolation split out to **#141** (separate spec).

## 9. Honest limitations

- The hit-test uses the element's **center** point only. A target whose center is covered but whose corners are clickable (unusual) falls to the untrusted fallback rather than probing alternative points — acceptable; the fallback still clicks.
- `scrollIntoView` centers the element, but a target taller/wider than the viewport may still place its geometric center in view (fine) or, in pathological layouts, not (`elementFromPoint` → null → fallback). Not handled beyond the fallback.
- Trusted click reproduces a real pointer click; it does **not** simulate keyboard activation (Enter on a focused control). Out of scope — selectors target clickable elements.
- Trusted-vs-fallback is decided once, before dispatch; a layout that changes between the measure evaluate and the dispatch (rare, animation-driven) could dispatch at a now-stale point. The same single-round-trip measure is what keeps it cheap; re-measuring per dispatch is not worth it.
