#!/usr/bin/env python3
"""Offline unit tests for skills/drive/scripts/cookie_seed.py — pure functions
(domain matching, CookieParam projection, guard rails). No browser needed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "skills", "drive", "scripts"))
import cookie_seed  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_drive_log(tmp_path, monkeypatch):
    """#328 r3: cookie_seed.main() writes an audit line — unit tests must not
    mutate the user's real ~/.claude/hooks/bulldozer-drive.log."""
    monkeypatch.setenv("BULLDOZER_DRIVE_LOG", str(tmp_path / "drive-audit.log"))


class TestDomainMatches:
    def test_exact(self):
        assert cookie_seed.domain_matches("github.com", "github.com")

    def test_subdomain(self):
        assert cookie_seed.domain_matches("api.github.com", "github.com")

    def test_leading_dot_cookie_domain(self):
        assert cookie_seed.domain_matches(".github.com", "github.com")

    def test_leading_dot_wanted(self):
        assert cookie_seed.domain_matches("github.com", ".github.com")

    def test_case_insensitive(self):
        assert cookie_seed.domain_matches("GitHub.COM", "github.com")

    def test_suffix_attack_rejected(self):
        """evilgithub.com must NOT match github.com — dot-anchored suffix only."""
        assert not cookie_seed.domain_matches("evilgithub.com", "github.com")

    def test_unrelated(self):
        assert not cookie_seed.domain_matches("example.org", "github.com")

    def test_empty(self):
        assert not cookie_seed.domain_matches("", "github.com")


class TestProjectCookie:
    def test_projects_param_fields_only(self):
        src = {"name": "s", "value": "v", "domain": "x.com", "path": "/",
               "secure": True, "httpOnly": True, "sameSite": "Lax",
               "expires": 9999999999.0, "size": 12, "session": False,
               "priority": "Medium"}
        out = cookie_seed.project_cookie(src)
        assert out["name"] == "s" and out["value"] == "v"
        assert "size" not in out and "session" not in out

    def test_session_cookie_expires_dropped(self):
        """getCookies reports expires=-1 for session cookies; CookieParam treats
        a MISSING expires as session — shipping -1 would be a past date."""
        out = cookie_seed.project_cookie({"name": "s", "value": "v",
                                          "domain": "x.com", "expires": -1})
        assert out is not None and "expires" not in out

    def test_already_expired_cookie_skipped_entirely(self):
        """Review pack C: an epoch-or-earlier expiry (0, not the -1 sentinel)
        means the cookie is already EXPIRED — seeding it as a session cookie
        would resurrect a logically-dead auth cookie. project_cookie → None."""
        assert cookie_seed.project_cookie({"name": "s", "value": "v",
                                           "domain": "x.com", "expires": 0}) is None

    def test_future_expiry_preserved(self):
        out = cookie_seed.project_cookie({"name": "s", "value": "v",
                                          "domain": "x.com",
                                          "expires": 9999999999.0})
        assert out is not None and out["expires"] == 9999999999.0


class TestGuards:
    def test_refuses_seeding_into_daily(self, capsys):
        rc = cookie_seed.main(["--domains", "x.com", "--to-port", "9333"])
        assert rc == 2
        assert "daily" in capsys.readouterr().err.lower()

    def test_refuses_same_ports(self, capsys):
        rc = cookie_seed.main(["--domains", "x.com",
                               "--from-port", "9355", "--to-port", "9355"])
        assert rc == 2

    def test_refuses_empty_domains(self, capsys):
        rc = cookie_seed.main(["--domains", " , ", "--to-port", "9359"])
        assert rc == 2

    def test_refuses_env_overridden_daily_port(self, capsys, monkeypatch):
        """Review sweep: a dev whose daily browser runs on a non-default port
        (CDP_PORT env) must be equally protected from seeding INTO it."""
        monkeypatch.setenv("CDP_PORT", "9777")
        rc = cookie_seed.main(["--domains", "x.com", "--to-port", "9777"])
        assert rc == 2
        assert "daily" in capsys.readouterr().err.lower()
