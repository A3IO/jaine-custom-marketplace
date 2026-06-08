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
    requirement: LEDGER_PATCH must cover BOTH the recheck of prior-round
    findings AND any new findings (Round-1 templates don't have prior
    findings to recheck). This clause cannot move into the shared Protocol
    section because it's Round-N-specific.
    """

    def test_round_n_continuation_preserves_recheck_invariant(self):
        section = _extract_section(SKILL_MD.read_text(encoding="utf-8"), ROUND_N_HEADER)
        assert "recheck results" in section.lower(), (
            "Round-N continuation prompt missing 'recheck results' wording — the "
            "Round-N-specific dual-content invariant ('LEDGER_PATCH covers both "
            "recheck results and new findings') has regressed. Restore the clause "
            "or update this test if the contract intentionally changed."
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
