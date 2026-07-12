"""#322 PR5: the /drive channel — invoke marker, lane lifecycle, cookie-seed audit."""
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
INVOKE_HOOK = PLUGIN_ROOT / "hooks" / "log_skill_invoke.py"
COOKIE_SEED = PLUGIN_ROOT / "skills" / "drive" / "scripts" / "cookie_seed.py"
LAUNCH = PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh"


def test_drive_invoke_marker(tmp_path):
    env = os.environ.copy()
    env.pop("BULLDOZER_DRIVE_LOG", None)  # the hook gives it precedence — keep hermetic (#328 r6)
    env.update({"BULLDOZER_INVOKE_LOG_DIR": str(tmp_path),
                "CLAUDE_CODE_SESSION_ID": "cafebabe99"})
    payload = json.dumps({"prompt": "/bulldozer:drive http://x test", "cwd": str(tmp_path)})
    r = subprocess.run([sys.executable, str(INVOKE_HOOK)], input=payload,
                       capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 0, r.stderr
    line = (tmp_path / "bulldozer-drive.log").read_text().strip()
    assert " | event=drive-invoke | " in line and " | session=cafebabe | " in line


def test_cookie_seed_failure_writes_audit_line(tmp_path):
    # unreachable source port → exit 1; the attempt must still leave a line
    env = os.environ.copy()
    env["BULLDOZER_DRIVE_LOG"] = str(tmp_path / "drive.log")
    env.pop("CDP_PORT", None)
    r = subprocess.run(
        [sys.executable, str(COOKIE_SEED), "--domains", "example.com",
         "--to-port", "9377", "--from-port", "9399"],
        capture_output=True, text=True, timeout=15, env=env)
    assert r.returncode == 1
    line = (tmp_path / "drive.log").read_text().strip()
    assert " | event=cookie-seed | " in line
    assert "from_port=9399" in line and "to_port=9377" in line
    assert "ok=no" in line
    assert "domains=example.com" in line


def test_cookie_seed_validation_error_writes_audit_line(tmp_path):
    env = os.environ.copy()
    env["BULLDOZER_DRIVE_LOG"] = str(tmp_path / "drive.log")
    env.pop("CDP_PORT", None)
    r = subprocess.run(
        [sys.executable, str(COOKIE_SEED), "--domains", "example.com",
         "--to-port", "9333"],  # daily browser → refused
        capture_output=True, text=True, timeout=15, env=env)
    assert r.returncode == 2
    line = (tmp_path / "drive.log").read_text().strip()
    assert "event=cookie-seed" in line and "ok=no" in line


def test_launch_sh_has_lane_lifecycle_logging():
    # behavioral lane-start verification needs a live Chrome (covered by the lanes
    # e2e); pin the shim wiring structurally so it can't be silently removed.
    src = LAUNCH.read_text()
    assert "bulldozer_log.py" in src
    assert "lane-start" in src
    assert "lane-fail" in src
