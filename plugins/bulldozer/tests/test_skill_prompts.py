"""All three reviewer prompt templates must require LEDGER_PATCH-for-GO.

Regression test for #104: Round-N continuation prompt was missing the
"GO MUST be expressed as LEDGER_PATCH" directive that both Round-1
templates carry. Without it, a reviewer reaching GO on round N>1 falls
back to bare "GO" → parser exits 1 → manual prose extraction kicks in,
defeating PR1a (#98/#101) at the most common terminal state in
exhaustive depth.
"""
import json
import re
import shutil
import subprocess

import pytest

from conftest import PLUGIN_ROOT

SKILL_MD = PLUGIN_ROOT / "skills" / "check" / "SKILL.md"
E1_SCHEMA = PLUGIN_ROOT / "skills" / "check" / "data" / "e1-evidence-schema.json"

GO_DIRECTIVE_MARKER = "GO MUST be expressed"

ROUND_1_QUICK_HEADER = "### Round 1 — quick"
ROUND_1_STANDARD_HEADER = "### Round 1 — standard / exhaustive"
ROUND_N_HEADER = "### Round N (continuation with ledger)"
LEDGER_PATCH_PROTOCOL_HEADER = "### LEDGER_PATCH Protocol"

# Normalize em-dash (U+2014), en-dash (U+2013), and hyphen-minus (U+002D) so
# that an IDE autocorrect on either the SKILL.md side or the test-constant
# side does not produce a confusing 'section header not found' assertion
# (#100 case #14). Surrounding whitespace is collapsed to a single space
# around the canonical dash so "Round 1 - quick" / "Round 1 — quick" /
# "Round 1–quick" all canonicalize identically.
_DASH_RE = re.compile(r"\s*[—–-]\s*")


def _normalize_dashes(s: str) -> str:
    return _DASH_RE.sub(" - ", s)


def _extract_section(text: str, header: str) -> str:
    target = _normalize_dashes(header)
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _normalize_dashes(line.strip()) == target:
            start = i
            break
    assert start is not None, f"section header not found in SKILL.md: {header!r}"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") or lines[j].startswith("### "):
            end = j
            break
    return "\n".join(lines[start:end])


class TestRoundNRecheckInvariantPreserved:
    """Round-N-specific clause that must survive any future refactor.

    PR1b (#102) consolidated the LEDGER_PATCH directive into a shared
    Protocol section, but Round-N still carries its own dual-content
    requirement: it rechecks prior-round findings (Round-1 templates don't
    have prior findings to recheck).

    #271 REFINED the contract: the LEDGER_PATCH findings list no longer
    carries terminal rechecks (they crash the parser AND inflate
    findings_count). The Round-N section must still tell the reviewer to
    recheck prior findings AND route them — terminal statuses to prose,
    still_open re-emitted as full findings in the block.
    """

    def test_round_n_continuation_preserves_recheck_invariant(self):
        section = _extract_section(SKILL_MD.read_text(encoding="utf-8"), ROUND_N_HEADER)
        low = section.lower()
        assert "recheck" in low, (
            "Round-N continuation prompt no longer mentions rechecking prior "
            "findings — the Round-N-specific recheck invariant has regressed."
        )
        # #271: the section must surface the prose-vs-block routing split, not
        # the old undifferentiated 'covering both recheck results and new
        # findings' wording that told reviewers to put terminal rechecks
        # (id+status+note) into the parsed findings list (exit 3).
        assert "still_open" in low and "prose" in low, (
            "Round-N section must route rechecks by status (#271): terminal "
            "rechecks to PROSE, still_open re-emitted as a full finding in the "
            "block. Missing the 'still_open' / 'prose' routing language."
        )
        assert "covering both recheck results and new findings" not in low, (
            "Round-N still carries the #271 bug wording 'covering both recheck "
            "results and new findings' — that directive tells the reviewer to "
            "put terminal rechecks (id+status+note) inside LEDGER_PATCH.findings, "
            "which the parser rejects (exit 3)."
        )


# Terminal recheck statuses: a status update with no natural severity/title.
# They must NEVER appear inside LEDGER_PATCH.findings — the parser requires
# severity+title (exit 3 otherwise) AND findings_count = len(findings) would
# count a RESOLVED item as still-open, corrupting trajectory + B6 pivot +
# verdict inference. (#271)
TERMINAL_STATUSES = ("verified", "false_positive", "wontfix")


class TestRoundNRecheckRouting:
    """#271: the LEDGER_PATCH Protocol (single source) must specify how Round-N
    rechecks are routed so a faithful reviewer never triggers exit 3 and the
    per-round findings_count stays equal to the number of CURRENTLY-OPEN
    findings (still_open + new).

    Data-flow rationale these tests lock in:
      - parse-ledger-patch.py requires id+severity+title on every findings entry.
      - bulldozer-round.sh computes findings_count = len(parsed.findings) and
        feeds it to emit-pivot.py (B6 avg-last-3 >= 3.0), the trajectory, and
        the verdict inference (go if not findings else no_go).
    """

    def _protocol(self) -> str:
        return _extract_section(SKILL_MD.read_text(encoding="utf-8"), LEDGER_PATCH_PROTOCOL_HEADER)

    def test_protocol_routes_terminal_rechecks_to_prose(self):
        section = self._protocol().lower()
        assert "prose" in section, (
            "Protocol must route terminal rechecks to PROSE (#271)."
        )
        for status in TERMINAL_STATUSES:
            assert status in section, (
                f"Protocol routing rule must name terminal status {status!r} "
                f"(#271) — these go in prose, never in the findings list."
            )

    def test_protocol_keeps_terminal_rechecks_out_of_findings_list(self):
        section = self._protocol().lower()
        # The count-inflation rationale must be present so the rule reads as
        # load-bearing, not arbitrary — terminal rechecks would be counted.
        assert "findings_count" in section or "count" in section, (
            "Protocol must explain WHY terminal rechecks stay out of the "
            "findings list (they inflate findings_count → corrupt pivot/verdict)."
        )

    def test_protocol_requires_still_open_as_full_finding(self):
        section = self._protocol()
        low = section.lower()
        assert "still_open" in low, (
            "Protocol must document the still_open recheck path (#271)."
        )
        # still_open IS open → must be re-emitted with full fields (severity)
        # so it is parsed AND counted.
        assert "re-emit" in low or "re-emitted" in low, (
            "Protocol must say still_open rechecks are RE-EMITTED as full "
            "findings (severity+title+files) inside the block (#271)."
        )
        assert "severity" in low

    def test_protocol_go_requires_empty_findings(self):
        # GO must not coexist with a non-empty findings list (a re-emitted
        # still_open makes the list non-empty → not a GO round).
        section = self._protocol()
        assert "findings: []" in section, (
            "Protocol GO shape (`verdict: go` + `findings: []`) must remain — "
            "GO requires zero open findings (no still_open, no new) (#271)."
        )


class TestStillOpenRecheckCommandsProvenance:
    """codex_review P2 (round 2, #271): a still_open re-emit may omit
    required_recheck/original_verdict_excerpt (the parser accepts the short
    shape and the finding's recheck commands already persist in the ledger).
    But Step 4 runs required_recheck.commands from parsed-rN.json, so a
    still_open-only round would have no per-finding commands there. Step 4 must
    direct the caller to the EXISTING ledger entry for a still_open re-emit's
    recheck commands.
    """

    def _step4(self) -> str:
        text = SKILL_MD.read_text(encoding="utf-8")
        start = text.index("**4. Verify each finding")
        end = text.index("**5. Apply findings", start)
        return text[start:end]

    def test_step4_still_open_recheck_commands_from_ledger(self):
        s = self._step4().lower()
        assert "still_open" in s and "ledger" in s, (
            "Step 4 must direct the caller to take a still_open re-emit's "
            "required_recheck.commands from the existing ledger entry when they "
            "are absent from parsed-rN.json (codex_review P2 round 2, #271)."
        )


class TestStep5LedgerUpsert:
    """#271: a re-emitted still_open recheck carries its ORIGINAL id (e.g.
    R1-F1). Step 5 must UPDATE the existing ledger entry, not blindly append a
    duplicate — the parser only rejects duplicate ids WITHIN a single block
    (parse-ledger-patch.py), so cross-round repeated ids reach Step 5 and would
    duplicate the ledger if Step 5 still says 'append each finding'.
    """

    def _step5(self) -> str:
        text = SKILL_MD.read_text(encoding="utf-8")
        start = text.index("**5. Apply findings to the ledger")
        end = text.index("**6.", start)
        return text[start:end]

    def test_step5_updates_existing_ledger_entry_by_id(self):
        s = self._step5().lower()
        assert "update" in s or "upsert" in s, (
            "Step 5 must UPDATE an existing ledger entry when a finding's id "
            "already exists (re-emitted still_open), not blindly append (#271)."
        )
        assert "already" in s or "existing" in s, (
            "Step 5 must condition the update on the id ALREADY existing in the "
            "ledger (#271)."
        )

    def test_step5_reads_terminal_rechecks_from_prose(self):
        s = self._step5().lower()
        # Terminal rechecks are NOT in parsed-rN.json (they live in prose) — Step
        # 5 must tell Claude to read them from the verdict prose and apply the
        # terminal status to the matching ledger entry.
        assert "prose" in s, (
            "Step 5 must note that terminal rechecks come from the verdict PROSE "
            "(not parsed-rN.json) and are applied to the matching ledger entry (#271)."
        )

    def test_step5_verifies_terminal_rechecks_before_closing(self):
        # codex_review P2 (#271): routing terminal rechecks to prose moved them
        # OUT of parsed-rN.json, so Step 4's "verify every finding" loop no longer
        # covers them. A reviewer's prose recheck is a CLAIM, not proof — Step 5
        # must re-verify (re-run the ledger entry's required_recheck.commands)
        # before closing the entry, or a hallucinated "verified" silently closes a
        # still-open finding.
        s = self._step5().lower()
        assert "required_recheck" in s, (
            "Step 5 must require re-running the ledger entry's required_recheck "
            "before applying a terminal prose recheck — never close a finding on "
            "the reviewer's word alone (codex_review P2, #271)."
        )


class TestExit11ManualExtractionMirrorsRouting:
    """#271 (caught by self-review R1-F1): the exit-11 manual-extraction fallback
    (Step 7, when a Round-N reviewer omits the LEDGER_PATCH block entirely) must
    mirror the same routing as the normal Step 5 path. Otherwise a still_open
    prose recheck for R1-F1 gets blindly appended as a fresh R2-F1 (duplicate),
    terminal prose rechecks are counted in K, and the cumulative-ledger identity
    breaks on the documented manual path.
    """

    def _exit11_branch(self) -> str:
        text = SKILL_MD.read_text(encoding="utf-8")
        start = text.index("Wrapper exited 11 (manual-extraction branch)")
        end = text.index("Why this protocol exists", start)
        return text[start:end]

    def test_exit11_still_open_keeps_original_id_via_upsert(self):
        branch = self._exit11_branch().lower()
        assert "still_open" in branch, (
            "exit-11 branch must mirror #271 routing: a still_open recheck in "
            "prose keeps its ORIGINAL id, not a fresh R{N}-F{M}."
        )
        assert "upsert" in branch or "original id" in branch, (
            "exit-11 branch must upsert still_open prose rechecks under the "
            "original id (not blind-append a duplicate) — mirror Step 5 (#271)."
        )

    def test_exit11_K_excludes_terminal_rechecks(self):
        branch = self._exit11_branch().lower()
        # K (replace-extraction) must be the OPEN count (still_open + new), not
        # inflated by terminal rechecks narrated in the same prose.
        assert "still_open + new" in branch, (
            "exit-11 branch must define K as still_open + new (terminal rechecks "
            "are not open findings and must not inflate K) (#271)."
        )

    def test_exit11_verifies_terminal_rechecks_before_closing(self):
        # codex_review P2 mirror: the manual path also closes ledger entries from
        # prose rechecks, so it must re-verify (run required_recheck) before
        # applying a terminal status — same discipline as Step 5.
        branch = self._exit11_branch().lower()
        assert "required_recheck" in branch, (
            "exit-11 branch must re-verify a terminal prose recheck (run the "
            "ledger entry's required_recheck) before closing it (codex_review P2, #271)."
        )


class TestExtractSectionDashTolerance:
    """Issue #100 case #14: section-header comparison was exact-match against
    em-dash (U+2014) constants. Contributor IDEs that autocorrect '—' → '-'
    (macOS Pages default, some VSCode configs) silently replace the dash on
    save, producing confusing 'section header not found in SKILL.md' errors
    while the actual GO-directive content under the section is intact.

    _extract_section must normalize dash variants so exact byte comparison
    against header constants survives the autocorrect.
    """

    def test_em_dash_header_matches_hyphen_in_text(self):
        sample_text = (
            "### Round 1 - quick\n"           # hyphen — simulates IDE autocorrect
            "body line 1\n"
            "body line 2\n"
            "### Next section\n"
        )
        section = _extract_section(sample_text, "### Round 1 — quick")  # em-dash
        assert "body line 1" in section, (
            "_extract_section must tolerate em-dash ↔ hyphen drift. Mismatch "
            "between SKILL.md autocorrected header and the test constant should "
            "NOT produce a confusing 'section not found' error when the body "
            "content is intact."
        )

    def test_hyphen_header_matches_em_dash_in_text(self):
        # Reverse case: text has em-dash, constant has hyphen.
        sample_text = (
            "### Round 1 — quick\n"
            "alpha\n"
            "### Next\n"
        )
        section = _extract_section(sample_text, "### Round 1 - quick")
        assert "alpha" in section

    def test_en_dash_also_normalized(self):
        # IDE could also produce U+2013 (en-dash).
        sample_text = (
            "### Round 1 – quick\n"     # en-dash
            "beta\n"
            "### Next\n"
        )
        section = _extract_section(sample_text, "### Round 1 — quick")
        assert "beta" in section

    def test_exact_match_still_works_unchanged(self):
        # Regression guard: when text and constant agree, behavior is unchanged.
        sample_text = "### Round 1 — quick\nbody\n### Next\n"
        section = _extract_section(sample_text, "### Round 1 — quick")
        assert "body" in section


# The byte-identical drift-detection test for Round-1 standard vs Round-N
# (added in #104 / case #16 of #100) is obsolete after PR1b (#102) extracted
# the LEDGER_PATCH directive into a single shared Protocol section: drift
# between templates is structurally impossible now because there's only one
# source. See TestLedgerPatchProtocolSection + TestPromptTemplatesReferenceProtocol
# below for the post-extraction regression coverage.


class TestLedgerPatchProtocolSection:
    """PR1b (#102): the LEDGER_PATCH directive is consolidated into a single
    shared section so the three reviewer prompt templates no longer drift
    on the wording (PR #106 code-review finding).
    """

    def test_protocol_section_exists(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, LEDGER_PATCH_PROTOCOL_HEADER)
        assert section, "### LEDGER_PATCH Protocol section must exist in SKILL.md"

    def test_protocol_section_contains_required_clause(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, LEDGER_PATCH_PROTOCOL_HEADER)
        assert "REQUIRED for both NO-GO and GO" in section, (
            "Protocol section must carry the 'REQUIRED for both NO-GO and GO' "
            "clause that was previously duplicated across all three templates."
        )

    def test_protocol_section_contains_go_shape(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, LEDGER_PATCH_PROTOCOL_HEADER)
        assert "findings: []" in section, (
            "Protocol section must show the explicit GO shape "
            "(`verdict: go, findings: []`) so reviewers don't emit bare GO."
        )

    def test_protocol_section_warns_against_bare_go(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, LEDGER_PATCH_PROTOCOL_HEADER)
        # The whole point of the protocol is to keep reviewers from emitting
        # bare "GO" (which parser exit-1 then forces back to manual extraction).
        assert "bare" in section.lower() or "manual extraction" in section.lower(), (
            "Protocol section must explain the bare-GO failure mode "
            "(otherwise reviewers may not understand why explicit findings: [] matters)."
        )

    def test_protocol_bare_go_description_matches_parser_behavior(self):
        """BUG-5: Protocol section incorrectly claimed bare GO → parser exit 1.

        Parser actually SYNTHESIZES a `{verdict: go, findings: []}` payload
        with `source: synthesized_bare_go` and exits 0 (see parse-ledger-
        patch.py bare-GO synthesis at lines ~627-641). The doc must reflect
        this — telling reviewers "bare GO breaks everything" is wrong when
        the parser actually has a graceful fallback. The "always use the
        structured block" recommendation stays (less drift, clearer intent),
        but the failure mode description must be accurate.
        """
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, LEDGER_PATCH_PROTOCOL_HEADER)
        # Must NOT make the false "exit 1" claim about bare GO.
        assert "parser exit 1" not in section, (
            "Protocol incorrectly says bare GO → parser exit 1. Parser "
            "actually synthesizes empty-findings GO and exits 0 (with a "
            "warning). Update the prose to reflect actual behavior."
        )
        # Should acknowledge synthesis exists (so reviewers understand the
        # fallback is graceful, not a hard failure).
        assert "synthesiz" in section.lower(), (
            "Protocol should mention that bare GO is auto-synthesized "
            "(with warnings) — otherwise reviewers think it's fatal."
        )


class TestPromptTemplatesReferenceProtocol:
    """Each template must reference the shared LEDGER_PATCH Protocol section
    instead of duplicating the directive. Catches drift regression at source.
    """

    @staticmethod
    def _assert_references_protocol(section: str, where: str):
        # Acceptable reference forms — be lenient on wording but require either
        # 'LEDGER_PATCH Protocol' or 'Protocol above/below'.
        ref_found = (
            "LEDGER_PATCH Protocol" in section
            or "Protocol above" in section
            or "Protocol below" in section
            or "Protocol section" in section
        )
        assert ref_found, (
            f"{where} template must reference the shared LEDGER_PATCH Protocol "
            f"section (e.g. 'see LEDGER_PATCH Protocol below')."
        )

    def test_round_1_quick_references_protocol(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_1_QUICK_HEADER)
        self._assert_references_protocol(section, "Round-1 quick")

    def test_round_1_standard_references_protocol(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_1_STANDARD_HEADER)
        self._assert_references_protocol(section, "Round-1 standard")

    def test_round_n_references_protocol(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_N_HEADER)
        self._assert_references_protocol(section, "Round-N")


class TestPromptTemplatesReadClaudeMd:
    """PR1b U2-C (#98/#102): all 3 reviewer prompts must instruct codex to
    read CLAUDE.md at project root before classifying findings, so project
    conventions inform material-vs-defensive triage.
    """

    @staticmethod
    def _assert_has_claude_md_instruction(section: str, where: str):
        # Be lenient on exact wording — just require both 'CLAUDE.md' and a
        # 'read' verb in the same section.
        assert "CLAUDE.md" in section, (
            f"{where} template missing CLAUDE.md instruction (U2-C). "
            f"Reviewer needs to read project conventions before classifying findings."
        )
        assert "read" in section.lower(), (
            f"{where} template mentions CLAUDE.md but doesn't tell reviewer to READ it."
        )

    def test_round_1_quick_instructs_read_claude_md(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_1_QUICK_HEADER)
        self._assert_has_claude_md_instruction(section, "Round-1 quick")

    def test_round_1_standard_instructs_read_claude_md(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_1_STANDARD_HEADER)
        self._assert_has_claude_md_instruction(section, "Round-1 standard")

    def test_round_n_instructs_read_claude_md(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, ROUND_N_HEADER)
        self._assert_has_claude_md_instruction(section, "Round-N")


class TestSkillUsesWrapperComposer:
    """PR1b (#102) AC: 'SKILL.md Step 2 + Step 7 rewritten to use wrapper as
    the single call (Step 4 manual parsing becomes wrapper-internal).'

    Three structural invariants in the rewritten Step-by-step:
      (1) wrapper script is invoked by name from the step body
      (2) no inline `codex exec` command remains in the step body (it moves
          inside the wrapper)
      (3) no inline `python3 ... parse-ledger-patch.py` invocation remains
          (it's wrapper-internal too)
      (4) no inline `log-round.sh` invocation remains as a separate step
          (composed inside the wrapper)
    """

    def _step_by_step_body(self) -> str:
        text = SKILL_MD.read_text(encoding="utf-8")
        section = _extract_section(text, "### Step-by-step")
        return section

    def test_step_by_step_invokes_wrapper(self):
        body = self._step_by_step_body()
        assert "bulldozer-round.sh" in body, (
            "Step-by-step must invoke bulldozer-round.sh as the composer "
            "(replaces inline codex exec + parser + log-round chain)."
        )

    def test_no_inline_codex_exec_command(self):
        """`codex exec` invocation must move into the wrapper (Step 2 rewrite).

        Acceptable: prose mention of 'codex' (e.g. in the depth-table or
        diagram). Forbidden: an actual `codex exec` command in a bash fenced
        block within Step-by-step.
        """
        body = self._step_by_step_body()
        # Look at fenced bash blocks only — prose mentions are fine.
        in_bash = False
        bash_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("```bash"):
                in_bash = True
                continue
            if stripped == "```" and in_bash:
                in_bash = False
                continue
            if in_bash:
                bash_lines.append(line)
        bash_blob = "\n".join(bash_lines)
        assert "codex exec" not in bash_blob, (
            "Step-by-step still has an inline `codex exec` command in a bash "
            "block — Step 2 should delegate to the wrapper script. Found in:\n"
            f"{bash_blob}"
        )

    def test_no_inline_parse_ledger_patch_invocation(self):
        """Parser invocation must move into the wrapper (Step 4 rewrite)."""
        body = self._step_by_step_body()
        in_bash = False
        bash_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("```bash"):
                in_bash = True
                continue
            if stripped == "```" and in_bash:
                in_bash = False
                continue
            if in_bash:
                bash_lines.append(line)
        bash_blob = "\n".join(bash_lines)
        assert "parse-ledger-patch.py" not in bash_blob, (
            "Step-by-step still has an inline parse-ledger-patch.py invocation — "
            "Step 4 should now be wrapper-internal. Manual parsing only kicks "
            "in via wrapper exit-1 fallback, not as a step the user runs."
        )

    def test_no_inline_log_round_invocation(self):
        """log-round.sh invocation must move into the wrapper (Step 7 rewrite)."""
        body = self._step_by_step_body()
        in_bash = False
        bash_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("```bash"):
                in_bash = True
                continue
            if stripped == "```" and in_bash:
                in_bash = False
                continue
            if in_bash:
                bash_lines.append(line)
        bash_blob = "\n".join(bash_lines)
        assert "log-round.sh" not in bash_blob, (
            "Step-by-step still has an inline log-round.sh invocation — "
            "Step 7 should now be wrapper-internal. Forgetting to call it "
            "after a round is the discipline failure #98/#102 eliminates."
        )

    def test_wrapper_exit_codes_documented(self):
        """The rewritten steps must document the wrapper's exit-code branching
        (parser codes 0-5 + pivot code 10) so Claude knows how to react.
        """
        body = self._step_by_step_body()
        # Don't enforce exact wording — just that the exit codes show up.
        for code in ("0", "1", "2", "3", "4", "5", "10"):
            assert f"exit {code}" in body.lower() or f"exit code {code}" in body.lower() \
                or f"`{code}`" in body, (
                f"Step-by-step should document wrapper exit code {code} "
                f"(at least one of: 'exit {code}', 'exit code {code}', or `{code}`)."
            )


class TestDigraphRefresh:
    """B7 (#110): the review_loop digraph must reflect the wrapper-driven flow
    (one bulldozer-round.sh node + exit-code branches incl. exit-11 manual
    extraction and exit-10 pivot), not the pre-wrapper
    Send->Read->Empty?->Extract architecture.
    """

    def _digraph(self) -> str:
        text = SKILL_MD.read_text(encoding="utf-8")
        m = re.search(r"```dot\n(digraph review_loop \{.*?\n\})\n```", text, re.DOTALL)
        assert m, "digraph review_loop block not found in SKILL.md"
        return m.group(1)

    def test_reflects_wrapper_driven_flow(self):
        g = self._digraph()
        assert "bulldozer-round.sh" in g
        assert "11" in g and "manual extraction" in g.lower()
        assert "10" in g
        assert "pivot" in g.lower()

    def test_drops_stale_pre_wrapper_nodes(self):
        g = self._digraph()
        for stale in ('"Empty?"', '"Rerun same round"',
                      '"Send to reviewer (FOREGROUND)"',
                      '"Extract LEDGER_PATCH"', '"Commit + log round"'):
            assert stale not in g, f"stale node {stale} still in digraph"

    @pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz not installed")
    def test_digraph_parses_with_graphviz(self):
        g = self._digraph()
        r = subprocess.run(["dot", "-Tsvg"], input=g,
                           capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr


class TestE1SchemaContract:
    """Drift guard for the E1 finding envelope (mirrors TestDepthConfigContract).

    Single source of truth for the consistency-auditor agent body and the
    verifier — if the envelope/anchor shapes drift from this file, the agent
    and verifier silently disagree. (#94)
    """

    def test_schema_exists_and_parses(self):
        data = json.loads(E1_SCHEMA.read_text())
        assert data["envelope"] == ["id", "class", "file", "quote", "anchor"]

    def test_four_classes_with_anchor_shapes(self):
        data = json.loads(E1_SCHEMA.read_text())
        anchors = data["anchor_by_class"]
        assert set(anchors) == {
            "dead_ref", "internal_contradiction", "cross_spec_drift", "stale_term"
        }
        assert anchors["internal_contradiction"] == ["quote_b"]
        assert anchors["cross_spec_drift"] == ["other_file", "other_quote"]
        assert anchors["dead_ref"] == ["ref"]
        assert anchors["stale_term"] == ["exclude_section"]


class TestE1Step:
    """Pins the E1 pre-review consistency-audit step in SKILL.md (drift guard, #94)."""

    def test_task_in_allowed_tools(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        # frontmatter allowed-tools must include Task (auditor dispatch)
        assert '"Task"' in text or "Task," in text or "Task]" in text

    def test_step_names_the_pieces(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        assert "consistency-auditor" in text          # agent dispatch
        assert "verify-audit-findings.py" in text     # verifier invocation
        assert "e1-verified-r" in text                # sole-licensed-fix input
        assert "audit_model" in text                  # config knob (default sonnet)

    def test_sole_licensed_fix_input_clause(self):
        text = SKILL_MD.read_text(encoding="utf-8").lower()
        # fix ONLY from e1-verified; raw e1-findings is forbidden as a fix source
        assert "e1-verified" in text and "e1-findings" in text
        assert "only" in text  # "fix only ... e1-verified"

    def test_agent_dispatch_has_inline_fallback(self):
        """Agents register at session-start / /reload-plugins, NOT from source like
        skills — so a just-shipped consistency-auditor is 'not found' until reload.
        Step 1.7 MUST document the inline fallback (observed in consumer session
        8eb7be75: Task -> 'agent type not found' -> consumer improvised inline)."""
        text = SKILL_MD.read_text(encoding="utf-8")
        low = text.lower()
        assert "not found" in low or "not registered" in low  # the failure signal
        assert "inline" in low                                 # the fallback action
        assert "reload-plugins" in low                         # the documented remedy


def test_skill_md_resolves_scripts_without_requiring_plugin_root_env():
    """#236 (sibling of #221): $CLAUDE_PLUGIN_ROOT is NOT exported to the Bash tool
    (empirically empty in CC 2.1.185), so the documented bulldozer-round.sh invocations
    AND the feedback `jq` version line must SELF-RESOLVE the plugin dir from the cache —
    honoring the var if set, but never hard-requiring it. Mirrors the consult fix (PR #235).
    Guards against reverting to the raw ${CLAUDE_PLUGIN_ROOT}/... form (the #236 bug)."""
    text = SKILL_MD.read_text(encoding="utf-8")
    # self-resolving fallback present (BULLDOZER_DIR resolver + feedback jq):
    assert "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/" in text
    assert '[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]' in text                # guarded honor-if-set
    assert "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json" in text
    # the OLD raw forms that break with an empty var must be gone:
    assert '"${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/bulldozer-round.sh"' not in text
    assert 'jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json"' not in text


class TestFeedbackRecipeConvention:
    """#186: a doctrine step that says 'file an issue' must ship the RECIPE next to it.

    The precedent that motivated the issue: a consumer session hit the doctrine step,
    guessed the labels, and filed #185 with `bulldozer` only — the skill-label and the
    kind-label were added post-hoc by a second session. `drive` was the one skill whose
    doctrine says 'STOP and file an issue' (the engine-wall rule) while carrying no
    Feedback section at all — i.e. the headline example of the issue was itself the gap.
    """

    SKILLS = ["check", "look", "drive", "consult", "workflow-swarms"]

    def _skill(self, name):
        return (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text()

    @pytest.mark.parametrize("skill", SKILLS)
    def test_every_skill_carries_a_feedback_recipe(self, skill):
        """No skill may tell a session to file an issue without saying how."""
        text = self._skill(skill)
        assert "## Feedback" in text, "{}: no Feedback section".format(skill)
        assert "gh issue create" in text, "{}: Feedback section has no command".format(skill)

    @pytest.mark.parametrize("skill", SKILLS)
    def test_recipe_pins_the_full_label_set(self, skill):
        """`bulldozer` + the skill label + a kind label — the convention #185 missed.
        Labels must be LITERAL in the recipe, not left to `gh label list` guesswork."""
        text = self._skill(skill)
        recipe = text.split("## Feedback", 1)[1]
        assert "bulldozer" in recipe, "{}: recipe omits the bulldozer label".format(skill)
        assert skill in recipe, "{}: recipe omits its own skill label".format(skill)
        assert re.search(r"feedback|enhancement", recipe), \
            "{}: recipe omits the kind label".format(skill)

    def test_drive_engine_wall_doctrine_points_at_the_recipe(self):
        """The engine-wall rule ('a demonstrated cdp.py wall files an issue') is the
        doctrine step #186 names — it must resolve to a recipe, not to a guess."""
        text = self._skill("drive")
        assert "STOP and file an" in text, "the engine-wall doctrine step vanished"
        recipe = text.split("## Feedback", 1)[1]
        assert "cdp.py" in recipe, "drive's Feedback section must cover the engine wall"

    def test_drive_engine_wall_variant_is_executable_with_its_own_labels(self):
        """codex #186 r1 (P2): the prose said 'same labels plus enhancement' while the only
        runnable command carried three labels — a session told to run it verbatim would file
        the capability request without the kind label. The variant must be its own command."""
        recipe = self._skill("drive").split("## Feedback", 1)[1]
        walls = [b for b in recipe.split("```") if "cdp.py wall" in b and "gh issue create" in b]
        assert walls, "no executable engine-wall invocation"
        assert "enhancement" in walls[0], "engine-wall command omits the enhancement label"

    def test_drive_grants_the_write_tool_its_recipe_needs(self):
        """codex #186 r2 (P1): the safe recipe builds the body with the Write tool, but the
        frontmatter listed only Bash/Read/AskUserQuestion — a contradiction that invites the
        agent back onto the shell path this fix removed."""
        head = self._skill("drive").split("---")[1]
        assert re.search(r'allowed-tools:.*"Write"', head), \
            "drive's recipe needs Write; the frontmatter must grant it"

    def test_drive_body_path_is_unique_per_invocation(self):
        """codex #186 r2 (P2): drive fans out across lanes and subagents. A fixed
        /tmp/drive-issue.md lets one session overwrite another's evidence between Write,
        append and gh — filing the wrong page's failure."""
        recipe = self._skill("drive").split("## Feedback", 1)[1]
        assert "mktemp" in recipe, "the issue-body path must be unique per invocation"
        assert "--body-file /tmp/drive-issue.md" not in recipe, "fixed body path still wired to gh"
        # codex #186 r3 (P2), reproduced live: BSD mktemp substitutes TRAILING X's only —
        # `mktemp /tmp/x-XXXXXX.md` exits 0 and prints the template VERBATIM, so the
        # "unique" path collides for every session, silently. And the file form CREATES the
        # file, which the Write tool then refuses as an unread existing file. -d + a fresh
        # file inside dodges both.
        cmds = [ln.split("#")[0].strip() for ln in recipe.splitlines()
                if ln.strip().startswith("mktemp")]
        # codex #186 r4: assert the command EXISTS before validating it — the prose also says
        # "mktemp", so a loop over zero command lines would report green vacuously.
        assert cmds, "the recipe explains mktemp but ships no runnable mktemp command"
        for cmd in cmds:
            assert cmd.endswith("XXXXXX"), \
                "BSD mktemp only substitutes TRAILING X's; template collides: {!r}".format(cmd)
            assert " -d " in cmd, "mktemp must make a DIR — the file form blocks the Write tool"

    def test_drive_recipe_never_shell_expands_untrusted_evidence(self):
        """codex #186 r1 (P1): drive pastes FAIL lines / console errors from the page UNDER
        TEST into the issue body. A page can put $(…) or backticks in an error message, so an
        unquoted heredoc into --body would execute it as the operator. The body must reach gh
        through --body-file, never through a shell-expanded heredoc."""
        recipe = self._skill("drive").split("## Feedback", 1)[1]
        assert "--body-file" in recipe, "drive recipe must file the body from a file"
        assert "--body \"$(cat <<" not in recipe, \
            "drive recipe still builds --body via a shell-expanded heredoc"
        assert "<<ISSUE" not in recipe, "drive recipe still carries an expanding heredoc"


class TestReadmeLogFormat:
    """#322 tail: the README is the first thing a future log-miner author reads. Its
    `## Log Format` section still showed the PRE-#322 grammar (no `event=` key, one log),
    which would send that author straight into writing a parser for a format that no
    longer exists. There are no miners in the repo yet — this is the contract they will
    be built against, so it has to be true."""

    LOGS = ["bulldozer.log", "bulldozer-codex.log", "bulldozer-consult.log",
            "bulldozer-look.log", "bulldozer-drive.log", "require-workflow-skill.log"]

    def _readme(self):
        return (PLUGIN_ROOT / "README.md").read_text()

    def test_sample_line_carries_the_event_key(self):
        """The canonical grammar is `{ts} | event={event} | session={sid} | k=v …` —
        every sample line in the README must show it."""
        section = self._readme().split("## Log Format", 1)[1].split("\n## ", 1)[0]
        samples = [ln for ln in section.splitlines()
                   if re.match(r"^\d{4}-\d{2}-\d{2}T", ln.strip())]
        assert samples, "no sample log line in the README"
        for ln in samples:
            assert "| event=" in ln, "sample line predates #322 (no event= key): {}".format(ln)

    def test_all_stable_logs_are_named(self):
        """Six stable channels ship today; a miner author must not discover them by grep."""
        section = self._readme().split("## Log Format", 1)[1].split("\n## ", 1)[0]
        for log in self.LOGS:
            assert log in section, "README's Log Format omits {}".format(log)

    def test_points_at_the_grammar_spec_and_the_writer(self):
        """Single source of truth, so the README cannot silently drift again."""
        section = self._readme().split("## Log Format", 1)[1].split("\n## ", 1)[0]
        assert "lib/bulldozer_log.py" in section
        assert "2026-07-11-bulldozer-log-grammar-design.md" in section

    def test_non_canonical_producers_are_named(self):
        """codex #186 r1 (P2): two writers are NOT on the shared helper — verified against
        the live logs: codex emits `ts | TURN_OK | k=v` (positional event, no session=, no
        offset) and consult_panel's completion line has no event=. A miner told 'every line
        uses the grammar' would silently DISCARD current records, not just old history."""
        section = self._readme().split("## Log Format", 1)[1].split("\n## ", 1)[0]
        assert "NOT on the shared writer" in section, \
            "README still implies every line goes through lib/bulldozer_log.py"
        assert "codex_server.py" in section, "README hides the codex log's positional-event shape"
        assert "consult_panel.py" in section, "README hides consult's un-migrated completion line"
        # codex #186 r2 (P2): a THIRD producer — consult/SKILL.md echoes completion lines
        # itself, with `model=` SINGULAR where the panel writes `models=` plural. A miner
        # supporting only the panel shape would drop every inline single-codex consult.
        assert "consult/SKILL.md" in section, "README hides the inline consult writer"
        assert "singular" in section, "README must warn about model= vs models="

    def test_redaction_claim_is_scoped_to_where_it_exists(self):
        """codex #186 r1 (P2): URL/JS redaction lives in cdp.py ONLY. Claiming it plugin-wide
        would tell a reader that bulldozer-codex.log is safe to share — it is not."""
        section = self._readme().split("## Log Format", 1)[1].split("\n## ", 1)[0]
        assert re.search(r"scoped to the look channel", section), \
            "README must scope the redaction guarantee to the channel that implements it"
