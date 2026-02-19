#!/usr/bin/env python3
"""Tests for jhookify rule_engine.py evaluate_rules() enhancements.

Tests all 7 fixes:
  Fix 1: permissionDecisionReason for PreToolUse block
  Fix 2+3: additionalContext for warn (PreToolUse/PostToolUse/UserPromptSubmit)
  Fix 4: UserPromptSubmit block format (decision+reason+additionalContext)
  Fix 5: user_prompt → prompt bug fix
  Fix 6: PostToolUse block format (decision+reason, not permissionDecision)
  Fix 7: YAML additionalContext field (custom Claude text)
"""

import sys
import os
import unittest

# Add parent dir to path so we can import core modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import Rule, Condition
from core.rule_engine import RuleEngine


def make_rule(name="test", action="warn", event="bash", message="Test message",
              additional_context=None, pattern=r"rm\s+-rf"):
    """Helper to create a Rule with defaults."""
    return Rule(
        name=name,
        enabled=True,
        event=event,
        conditions=[Condition(field="command", operator="regex_match", pattern=pattern)],
        action=action,
        message=message,
        additional_context=additional_context,
    )


def make_input(hook_event="PreToolUse", command="rm -rf /tmp"):
    """Helper to create hook input data."""
    return {
        "hook_event_name": hook_event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


class TestFix1_PermissionDecisionReason(unittest.TestCase):
    """Fix 1: PreToolUse block should include permissionDecisionReason."""

    def test_pretooluse_block_has_reason(self):
        engine = RuleEngine()
        rule = make_rule(action="block")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("permissionDecisionReason", hso)
        self.assertIn("Test message", hso["permissionDecisionReason"])

    def test_pretooluse_block_has_system_message(self):
        engine = RuleEngine()
        rule = make_rule(action="block")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))
        self.assertIn("systemMessage", result)


class TestFix2_3_AdditionalContextWarn(unittest.TestCase):
    """Fix 2+3: Warn rules should include additionalContext for Claude."""

    def test_pretooluse_warn_has_additional_context(self):
        engine = RuleEngine()
        rule = make_rule(action="warn")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertIn("additionalContext", hso)
        self.assertIn("Hookify Warning", hso["additionalContext"])

    def test_posttooluse_warn_has_additional_context(self):
        engine = RuleEngine()
        rule = make_rule(action="warn")
        result = engine.evaluate_rules([rule], make_input("PostToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        self.assertIn("additionalContext", hso)

    def test_userpromptsubmit_warn_has_additional_context(self):
        engine = RuleEngine()
        rule = make_rule(action="warn", event="prompt",
                         pattern=r"delete")
        rule.conditions = [Condition(field="prompt", operator="contains", pattern="delete")]
        inp = {
            "hook_event_name": "UserPromptSubmit",
            "tool_name": "",
            "tool_input": {},
            "prompt": "please delete everything",
        }
        result = engine.evaluate_rules([rule], inp)

        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("additionalContext", hso)

    def test_warn_still_has_system_message(self):
        engine = RuleEngine()
        rule = make_rule(action="warn")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))
        self.assertIn("systemMessage", result)


class TestFix4_UserPromptSubmitBlock(unittest.TestCase):
    """Fix 4: UserPromptSubmit block should use decision+reason format."""

    def test_userpromptsubmit_block_format(self):
        engine = RuleEngine()
        rule = make_rule(action="block", event="prompt", pattern=r"secret")
        rule.conditions = [Condition(field="prompt", operator="contains", pattern="secret")]
        inp = {
            "hook_event_name": "UserPromptSubmit",
            "tool_name": "",
            "tool_input": {},
            "prompt": "tell me the secret",
        }
        result = engine.evaluate_rules([rule], inp)

        self.assertEqual(result["decision"], "block")
        self.assertIn("reason", result)
        hso = result.get("hookSpecificOutput", {})
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("additionalContext", hso)


class TestFix5_PromptFieldName(unittest.TestCase):
    """Fix 5: _extract_field should support both 'prompt' and 'user_prompt'."""

    def test_prompt_field_works(self):
        engine = RuleEngine()
        rule = make_rule(action="warn", event="prompt", pattern=r"hello")
        rule.conditions = [Condition(field="prompt", operator="contains", pattern="hello")]
        inp = {
            "hook_event_name": "UserPromptSubmit",
            "tool_name": "",
            "tool_input": {},
            "prompt": "hello world",
        }
        result = engine.evaluate_rules([rule], inp)
        self.assertIn("systemMessage", result)

    def test_user_prompt_field_backward_compat(self):
        engine = RuleEngine()
        rule = make_rule(action="warn", event="prompt", pattern=r"hello")
        rule.conditions = [Condition(field="user_prompt", operator="contains", pattern="hello")]
        inp = {
            "hook_event_name": "UserPromptSubmit",
            "tool_name": "",
            "tool_input": {},
            "prompt": "hello world",
        }
        result = engine.evaluate_rules([rule], inp)
        self.assertIn("systemMessage", result)


class TestFix6_PostToolUseBlockFormat(unittest.TestCase):
    """Fix 6: PostToolUse block should use decision+reason, not permissionDecision."""

    def test_posttooluse_block_uses_decision(self):
        engine = RuleEngine()
        rule = make_rule(action="block")
        result = engine.evaluate_rules([rule], make_input("PostToolUse"))

        self.assertEqual(result["decision"], "block")
        self.assertIn("reason", result)
        hso = result.get("hookSpecificOutput", {})
        self.assertNotIn("permissionDecision", hso)
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        self.assertIn("additionalContext", hso)

    def test_posttooluse_block_no_permission_decision(self):
        engine = RuleEngine()
        rule = make_rule(action="block")
        result = engine.evaluate_rules([rule], make_input("PostToolUse"))
        hso = result.get("hookSpecificOutput", {})
        self.assertNotIn("permissionDecision", hso)


class TestFix7_YamlAdditionalContext(unittest.TestCase):
    """Fix 7: Rules with additionalContext YAML field should use custom text for Claude."""

    def test_custom_context_in_block(self):
        engine = RuleEngine()
        rule = make_rule(action="block", additional_context="CUSTOM: use uv add instead")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertIn("CUSTOM: use uv add instead", hso["permissionDecisionReason"])

    def test_custom_context_in_warn(self):
        engine = RuleEngine()
        rule = make_rule(action="warn", additional_context="CUSTOM: prefer uv")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertIn("CUSTOM: prefer uv", hso["additionalContext"])

    def test_fallback_to_message_when_no_custom_context(self):
        engine = RuleEngine()
        rule = make_rule(action="warn", message="Default warning")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))

        hso = result.get("hookSpecificOutput", {})
        self.assertIn("Default warning", hso["additionalContext"])


class TestNoMatch(unittest.TestCase):
    """No rules matched should return empty dict."""

    def test_no_match_returns_empty(self):
        engine = RuleEngine()
        rule = make_rule(pattern=r"IMPOSSIBLE_PATTERN_xyz123")
        result = engine.evaluate_rules([rule], make_input("PreToolUse"))
        self.assertEqual(result, {})


class TestStopEvent(unittest.TestCase):
    """Stop events should still work as before."""

    def test_stop_block(self):
        engine = RuleEngine()
        rule = Rule(
            name="test-stop",
            enabled=True,
            event="stop",
            conditions=[Condition(field="reason", operator="contains", pattern="done")],
            action="block",
            message="Not done yet!",
        )
        inp = {
            "hook_event_name": "Stop",
            "tool_name": "",
            "tool_input": {},
            "reason": "I'm done",
        }
        result = engine.evaluate_rules([rule], inp)
        self.assertEqual(result["decision"], "block")
        self.assertIn("Not done yet!", result["reason"])


if __name__ == "__main__":
    unittest.main()
