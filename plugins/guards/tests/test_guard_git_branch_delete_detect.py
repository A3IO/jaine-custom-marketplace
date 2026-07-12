#!/usr/bin/env python3
"""Runtime tests for guard-git-branch-delete-detect.py.

Ported from STATUSLINE's pytest guard_branch_delete suite into the guards
plain-python/self-running test style. The detector is intentionally invoked the
way guards detectors are dispatched here: command text is argv[1].

Run: python3 tests/test_guard_git_branch_delete_detect.py   (exit 0 = all pass)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable


DETECT = str(Path(__file__).parent.parent / "hooks" / "guard-git-branch-delete-detect.py")


def _git_env(home: Path) -> dict[str, str]:
    """Return a git subprocess environment isolated to this test's temp dir."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the fixture repository and fail loudly on setup errors."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _commit_file(
    repo: Path,
    env: dict[str, str],
    relpath: str,
    content: str,
    message: str,
) -> None:
    """Write one file and commit it in the fixture repository."""
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, env, "add", relpath)
    _git(repo, env, "commit", "-m", message)


def _clear_worktree(repo: Path) -> None:
    """Remove non-.git files after creating an orphan branch."""
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _make_multi_orphan_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Create root main plus an unrelated prodx/main orphan product branch."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    env = _git_env(home)

    _git(repo, env, "init")
    _git(repo, env, "config", "user.name", "Guard Delete Tests")
    _git(repo, env, "config", "user.email", "guard-delete@example.invalid")
    _git(repo, env, "branch", "-M", "main")
    _commit_file(repo, env, "root.txt", "root\n", "root main")

    _git(repo, env, "checkout", "--orphan", "prodx/main")
    _clear_worktree(repo)
    _commit_file(repo, env, "prodx/base.txt", "product base\n", "prodx main")

    return repo, env


def _run_hook(
    repo: Path,
    env: dict[str, str],
    command: str,
) -> subprocess.CompletedProcess[str]:
    """Invoke the detector with command text as argv[1]."""
    return subprocess.run(
        [sys.executable, DETECT, command],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _make_unmerged_branch(repo: Path, env: dict[str, str], name: str) -> None:
    """Create a branch off prodx/main with one unique commit, return to prodx/main."""
    _git(repo, env, "checkout", "-b", name)
    _commit_file(repo, env, f"{name.replace('/', '_')}.txt", "unique\n", f"unique work on {name}")
    _git(repo, env, "checkout", "prodx/main")


def _make_merged_branch(repo: Path, env: dict[str, str], name: str) -> None:
    """Create a branch off prodx/main and merge it back with --no-ff."""
    _git(repo, env, "checkout", "-b", name)
    _commit_file(repo, env, f"{name.replace('/', '_')}.txt", "merged\n", f"merged work on {name}")
    _git(repo, env, "checkout", "prodx/main")
    _git(repo, env, "merge", "--no-ff", name, "-m", f"merge {name}")


def test_flat_branch_merged_to_product_main_is_allowed(tmp_path: Path) -> None:
    """Regression: flat branch topology-merged to prodx/main must not compare to root main."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _git(repo, env, "checkout", "-b", "feat/1-x")
    _commit_file(repo, env, "feature.txt", "merged feature\n", "feature")
    _git(repo, env, "checkout", "prodx/main")
    _git(repo, env, "merge", "--no-ff", "feat/1-x", "-m", "merge feature")

    result = _run_hook(repo, env, "git branch -d feat/1-x")

    assert result.returncode == 0, result.stderr


def test_flat_branch_unique_commit_blocks_against_true_product_base(tmp_path: Path) -> None:
    """Flat branch with unique work should block against prodx/main with count 1."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _git(repo, env, "checkout", "-b", "feat/2-x")
    _commit_file(repo, env, "unique.txt", "unique\n", "unique feature")
    _git(repo, env, "checkout", "prodx/main")

    result = _run_hook(repo, env, "git branch -D feat/2-x")

    assert result.returncode == 2
    assert "not on prodx/main" in result.stderr
    assert "has 1 unmerged commit(s)" in result.stderr
    assert "has 2 unmerged commit(s)" not in result.stderr


def test_name_prefix_rule_blocks_then_allows_after_merge(tmp_path: Path) -> None:
    """Prefixed prodx/... branches still compare directly to prodx/main."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _git(repo, env, "checkout", "-b", "prodx/feature")
    _commit_file(repo, env, "prefixed.txt", "prefixed\n", "prefixed feature")
    _git(repo, env, "checkout", "prodx/main")

    blocked = _run_hook(repo, env, "git branch -D prodx/feature")
    assert blocked.returncode == 2
    assert "not on prodx/main" in blocked.stderr
    assert "has 1 unmerged commit(s)" in blocked.stderr

    _git(repo, env, "merge", "--no-ff", "prodx/feature", "-m", "merge prefixed")
    allowed = _run_hook(repo, env, "git branch -d prodx/feature")
    assert allowed.returncode == 0, allowed.stderr


def test_deleting_product_main_compares_against_root_main(tmp_path: Path) -> None:
    """Deleting prodx/main itself remains guarded against root main/master."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _git(repo, env, "checkout", "main")

    result = _run_hook(repo, env, "git branch -D prodx/main")

    assert result.returncode == 2
    assert "not on main" in result.stderr


def test_escape_hatch_allows_delete_command(tmp_path: Path) -> None:
    """GUARD_BRANCH_DELETE_OK=1 short-circuits delete commands."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _git(repo, env, "checkout", "-b", "feat/ok")
    _commit_file(repo, env, "ok.txt", "ok\n", "ok feature")
    _git(repo, env, "checkout", "prodx/main")

    result = _run_hook(repo, env, "GUARD_BRANCH_DELETE_OK=1 git branch -D feat/ok")

    assert result.returncode == 0, result.stderr


def test_non_delete_command_is_allowed(tmp_path: Path) -> None:
    """Commands outside the delete matcher stay on the fast allow path."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "git status")

    assert result.returncode == 0, result.stderr


def test_missing_local_branch_is_allowed(tmp_path: Path) -> None:
    """Deleting a branch absent from local refs is allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "git branch -d nonexistent-branch-zzz")

    assert result.returncode == 0, result.stderr


# --- #541: git global options / long-form --delete must not bypass the matcher ---


def test_dash_c_form_blocks_unmerged(tmp_path: Path) -> None:
    """git -C <repo> branch -D with unique commits must block (#541 live incident)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/c-form")

    result = _run_hook(repo, env, f"git -C {repo} branch -D feat/c-form")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_cross_repo_dash_c_blocks_using_target_repo(tmp_path: Path) -> None:
    """From a non-repo cwd, checks must run inside the -C target repo (#541 layer 2)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/cross-block")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(elsewhere, env, f"git -C {repo} branch -D feat/cross-block")

    assert result.returncode == 2
    assert "not on prodx/main" in result.stderr
    assert "has 1 unmerged commit(s)" in result.stderr


def test_cross_repo_dash_c_allows_merged_in_target(tmp_path: Path) -> None:
    """From a non-repo cwd, a branch merged inside the -C target repo is allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/cross-merged")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(elsewhere, env, f"git -C {repo} branch -d feat/cross-merged")

    assert result.returncode == 0, result.stderr


def test_long_form_delete_blocks_unmerged(tmp_path: Path) -> None:
    """git branch --delete (long form) must match the guard like -d does."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/long-form")

    result = _run_hook(repo, env, "git branch --delete feat/long-form")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_long_form_delete_force_blocks_unmerged(tmp_path: Path) -> None:
    """git branch --delete --force must match the guard like -D does."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/long-force")

    result = _run_hook(repo, env, "git branch --delete --force feat/long-force")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_config_global_option_blocks_unmerged(tmp_path: Path) -> None:
    """git -c key=value branch -D must not slip past the matcher."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/config-opt")

    result = _run_hook(repo, env, "git -c core.pager=cat branch -D feat/config-opt")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_combined_global_options_block_unmerged(tmp_path: Path) -> None:
    """Stacked global options (-c … -C <repo>) from a foreign cwd must still block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/combo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(
        elsewhere, env, f"git -c core.pager=cat -C {repo} branch -D feat/combo"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_no_pager_flag_blocks_unmerged(tmp_path: Path) -> None:
    """Argument-less global flags (--no-pager) before the subcommand must not bypass."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/no-pager")

    result = _run_hook(repo, env, "git --no-pager branch -D feat/no-pager")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_double_space_blocks_unmerged(tmp_path: Path) -> None:
    """Extra whitespace between tokens must not defeat the matcher."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/spaces")

    result = _run_hook(repo, env, "git branch  -D feat/spaces")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_colon_refspec_blocks_unmerged(tmp_path: Path) -> None:
    """git push origin :branch (colon refspec delete) must block like push --delete."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/colon")

    result = _run_hook(repo, env, "git push origin :feat/colon")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_git_dir_form_blocks_unmerged(tmp_path: Path) -> None:
    """git --git-dir=<repo>/.git from a foreign cwd must check the target repo."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/git-dir")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(
        elsewhere, env, f"git --git-dir={repo}/.git branch -D feat/git-dir"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_work_tree_form_blocks_unmerged(tmp_path: Path) -> None:
    """git --work-tree=<p> branch -D from the repo cwd must still match and block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/work-tree")

    result = _run_hook(repo, env, f"git --work-tree={repo} branch -D feat/work-tree")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_compound_command_with_dash_c_blocks(tmp_path: Path) -> None:
    """A -C delete hidden inside a compound command must still block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/compound-c")

    result = _run_hook(
        repo, env, f"echo checking && git -C {repo} branch -D feat/compound-c"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_compound_command_literal_still_blocks(tmp_path: Path) -> None:
    """Pinning: literal delete inside a compound command keeps blocking after the fix."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/compound-lit")

    result = _run_hook(
        repo, env, "git pull --ff-only && git branch -d feat/compound-lit"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_dash_c_form_allows_merged(tmp_path: Path) -> None:
    """git -C <repo> branch -d of a merged branch stays allowed (no new FPs)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/c-merged")

    result = _run_hook(repo, env, f"git -C {repo} branch -d feat/c-merged")

    assert result.returncode == 0, result.stderr


def test_escape_hatch_with_dash_c_form(tmp_path: Path) -> None:
    """GUARD_BRANCH_DELETE_OK=1 keeps short-circuiting the -C form too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/c-hatch")

    result = _run_hook(
        repo, env, f"GUARD_BRANCH_DELETE_OK=1 git -C {repo} branch -D feat/c-hatch"
    )

    assert result.returncode == 0, result.stderr


def test_dash_c_non_delete_stays_allowed(tmp_path: Path) -> None:
    """git -C <repo> status must stay on the fast allow path."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, f"git -C {repo} status")

    assert result.returncode == 0, result.stderr


def test_push_delete_flag_before_remote_blocks(tmp_path: Path) -> None:
    """git push --delete <remote> <branch> (flag before remote) must block too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/push-order")

    result = _run_hook(repo, env, "git push --delete origin feat/push-order")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


# --- #541 round 2: adversarial findings (codex red-team, independently verified) ---


def test_quoted_branch_name_blocks(tmp_path: Path, quote: str) -> None:
    """AI agents quote branch names constantly — quoted operand must still block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/quoted")

    result = _run_hook(repo, env, f"git branch -D {quote}feat/quoted{quote}")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_quoted_dash_c_path_blocks(tmp_path: Path) -> None:
    """A quoted -C path must still be extracted and checked."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/qpath")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(elsewhere, env, f"git -C '{repo}' branch -D feat/qpath")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_quoted_git_dir_path_blocks(tmp_path: Path) -> None:
    """A quoted space-separated --git-dir path must still be extracted and checked."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/qgitdir")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run_hook(
        elsewhere, env, f"git --git-dir '{repo}/.git' branch -D feat/qgitdir"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_backslash_newline_continuation_blocks(tmp_path: Path) -> None:
    """Line-continuation formatting must not defeat the matcher."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/contline")

    result = _run_hook(repo, env, "git branch -D \\\nfeat/contline")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_variable_branch_token_blocks_conservatively(tmp_path: Path) -> None:
    """A $VAR branch operand cannot be verified lexically — guard fails CLOSED."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/vartok")

    result = _run_hook(repo, env, "BR=feat/vartok; git branch -D $BR")

    assert result.returncode == 2
    assert "cannot verify" in result.stderr


def test_multiple_branches_second_unmerged_blocks(tmp_path: Path) -> None:
    """git branch -D <merged> <unmerged> must check EVERY operand."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/multi-m")
    _make_unmerged_branch(repo, env, "feat/multi-u")

    result = _run_hook(repo, env, "git branch -D feat/multi-m feat/multi-u")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_multiple_branches_all_merged_allowed(tmp_path: Path) -> None:
    """Batch delete of merged-only branches stays allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/batch-a")
    _make_merged_branch(repo, env, "feat/batch-b")

    result = _run_hook(repo, env, "git branch -d feat/batch-a feat/batch-b")

    assert result.returncode == 0, result.stderr


def test_push_short_d_blocks(tmp_path: Path) -> None:
    """git push -d <remote> <branch> (documented short form) must block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/push-short")

    result = _run_hook(repo, env, "git push -d origin feat/push-short")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_delete_multiple_second_unmerged_blocks(tmp_path: Path) -> None:
    """git push origin --delete <merged> <unmerged> must check EVERY operand."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/pdm-m")
    _make_unmerged_branch(repo, env, "feat/pdm-u")

    result = _run_hook(
        repo, env, "git push origin --delete feat/pdm-m feat/pdm-u"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_colon_multiple_second_unmerged_blocks(tmp_path: Path) -> None:
    """git push origin :<merged> :<unmerged> must check EVERY refspec."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/pcm-m")
    _make_unmerged_branch(repo, env, "feat/pcm-u")

    result = _run_hook(repo, env, "git push origin :feat/pcm-m :feat/pcm-u")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_colon_full_ref_blocks(tmp_path: Path) -> None:
    """git push origin :refs/heads/<branch> must resolve to the local branch."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fullref")

    result = _run_hook(repo, env, "git push origin :refs/heads/feat/fullref")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_delete_before_separator_blocks(tmp_path: Path) -> None:
    """A push delete followed by && must not lose the already-found operand."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/push-sep")

    result = _run_hook(
        repo, env, "git push origin --delete feat/push-sep && echo ok"
    )

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_push_colon_before_separator_blocks(tmp_path: Path) -> None:
    """A colon refspec delete followed by && must not lose the operand."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/colon-sep")

    result = _run_hook(repo, env, "git push origin :feat/colon-sep && echo ok")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_embedded_variable_operand_blocks_conservatively(tmp_path: Path) -> None:
    """A $VAR anywhere inside a branch operand is unverifiable — fail closed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/embvar")

    result = _run_hook(repo, env, "B=embvar; git branch -D feat/$B")

    assert result.returncode == 2
    assert "cannot verify" in result.stderr


def test_variable_context_path_blocks_conservatively(tmp_path: Path) -> None:
    """A $VAR in -C/--git-dir on a DELETE command is unverifiable — fail closed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/varctx")

    result = _run_hook(
        repo, env, f'R={repo}; git -C "$R" branch -D feat/varctx'
    )

    assert result.returncode == 2
    assert "cannot verify" in result.stderr


def test_variable_context_path_non_delete_stays_allowed(tmp_path: Path) -> None:
    """$VAR context on a NON-delete branch/push FORM must stay on the allow path.

    Uses `branch --list` (not `status`) so the command passes the substring
    pre-gate and actually runs parse_global_opts + branch_delete_operands: this
    proves the context-unverifiable ($) check fires ONLY on a real delete match,
    not on any command that merely carries `-C "$VAR"`.
    """
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, 'R=/some/where; git -C "$R" branch --list')

    assert result.returncode == 0, result.stderr


def test_combined_short_flags_block(tmp_path: Path, flags: str) -> None:
    """Combined short flag clusters containing d/D are valid delete forms."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/cluster")

    result = _run_hook(repo, env, f"git branch {flags} feat/cluster")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_fast_path_spawns_no_git(tmp_path: Path) -> None:
    """Non-delete commands must stay on the fast path and spawn zero git subprocesses."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "git-called.marker"
    fake_git = fake_bin / "git"
    fake_git.write_text('#!/bin/sh\necho called >> "$MARKER"\nexit 0\n')
    fake_git.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = _git_env(home)
    env["MARKER"] = str(marker)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    for command in [
        "git status",
        "git branch --list",
        "git branch foo",
        "git push origin main",
    ]:
        if marker.exists():
            marker.unlink()
        result = _run_hook(tmp_path, env, command)
        assert result.returncode == 0, result.stderr
        assert not marker.exists()

    # Positive control: prove the shim is load-bearing. A delete form DOES reach
    # git (rev-parse --verify), so the marker MUST appear — otherwise the assertions
    # above could pass spuriously by falling through to the real system git (e.g. a
    # noexec tmp or a chmod that didn't take).
    if marker.exists():
        marker.unlink()
    result = _run_hook(tmp_path, env, "git branch -d nonexistent-zzz-branch")
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "fake-git shim did not intercept — fast-path proof is unverified"


# --- #302: transparent wrapper prefixes (command/env/xargs/sudo/nice/nohup) ---


def test_command_wrapper_blocks_unmerged(tmp_path: Path) -> None:
    """`command git branch -D <unmerged>` must block (#302 hard requirement)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-command")

    result = _run_hook(repo, env, "command git branch -D feat/wrap-command")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_sudo_arg_flag_wrapper_blocks_unmerged(tmp_path: Path) -> None:
    """`sudo -u user git branch -D <unmerged>`: -u eats its arg and still reaches git."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-sudo")

    result = _run_hook(repo, env, "sudo -u nobody git branch -D feat/wrap-sudo")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_xargs_replace_flag_wrapper_blocks_unmerged(tmp_path: Path) -> None:
    """`xargs -I{} git branch -D <literal>`: glued -I{} skipped, literal operand checked."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-xargs")

    result = _run_hook(repo, env, "xargs -I{} git branch -D feat/wrap-xargs")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_env_wrapper_blocks_push_delete(tmp_path: Path) -> None:
    """`env -i VAR=val git push origin --delete <unmerged>` reaches the push matcher."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-env")

    result = _run_hook(repo, env, "env -i VAR=val git push origin --delete feat/wrap-env")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_nice_wrapper_blocks_unmerged(tmp_path: Path) -> None:
    """`nice -n 10 git branch -D <unmerged>`: -n eats its numeric arg."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-nice")

    result = _run_hook(repo, env, "nice -n 10 git branch -D feat/wrap-nice")

    assert result.returncode == 2
    assert "has 1 unmerged commit(s)" in result.stderr


def test_wrapper_variable_branch_fails_closed(tmp_path: Path) -> None:
    """A $VAR operand behind a wrapper keeps the fail-closed rule."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "sudo git branch -D $BR")

    assert result.returncode == 2


def test_wrapper_escape_hatch_still_allows(tmp_path: Path) -> None:
    """GUARD_BRANCH_DELETE_OK=1 short-circuits wrapper forms too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-hatch")

    result = _run_hook(repo, env, "GUARD_BRANCH_DELETE_OK=1 sudo git branch -D feat/wrap-hatch")

    assert result.returncode == 0, result.stderr


def test_xargs_stdin_documented_allow(tmp_path: Path) -> None:
    """DOCUMENTED LIMIT (#302): the branch name arrives on stdin — lexically invisible
    to source AND port alike. Pinned here so red-team doesn't re-litigate it."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-stdin")

    result = _run_hook(repo, env, "echo feat/wrap-stdin | xargs git branch -D")

    assert result.returncode == 0, result.stderr


def test_non_wrapper_prefix_stays_allowed(tmp_path: Path) -> None:
    """git behind a NON-wrapper command is an argument, not an invocation — no FP."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/wrap-fp")

    result = _run_hook(repo, env, "foo bar git branch -D feat/wrap-fp")

    assert result.returncode == 0, result.stderr


def test_remotes_flag_targets_remote_tracking_not_local(tmp_path: Path) -> None:
    """#299: `-d -r` deletes a REMOTE-TRACKING ref; a local branch that happens to be
    named origin/x must not false-block it (remote-tracking refs are re-fetchable)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "origin/x")

    result = _run_hook(repo, env, "git branch -d -r origin/x")

    assert result.returncode == 0, result.stderr


def test_remotes_flag_clustered_allowed(tmp_path: Path) -> None:
    """#299: clustered `-Dr` is the same remote-tracking delete — no false-block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "origin/x")

    result = _run_hook(repo, env, "git branch -Dr origin/x")

    assert result.returncode == 0, result.stderr


def test_remotes_long_flag_allowed(tmp_path: Path) -> None:
    """#299: long `--remotes` form of the remote-tracking delete — no false-block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "origin/x")

    result = _run_hook(repo, env, "git branch --delete --remotes origin/x")

    assert result.returncode == 0, result.stderr


def test_local_branch_named_like_remote_still_blocks(tmp_path: Path) -> None:
    """Overreach guard: WITHOUT -r the delete targets the LOCAL origin/x — keep blocking."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "origin/x")

    result = _run_hook(repo, env, "git branch -D origin/x")

    assert result.returncode == 2, result.stderr


def test_push_plus_colon_refspec_blocks_unmerged(tmp_path: Path) -> None:
    """#299: `+:branch` IS a delete (+ force marker, empty source) — must block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/plusdel")

    result = _run_hook(repo, env, "git push origin +:feat/plusdel")

    assert result.returncode == 2, result.stderr


def test_push_plus_colon_full_ref_blocks(tmp_path: Path) -> None:
    """#299: `+:refs/heads/x` strips the ref prefix like the bare-colon form."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/plusref")

    result = _run_hook(repo, env, "git push origin +:refs/heads/feat/plusref")

    assert result.returncode == 2, result.stderr


def test_push_plus_colon_merged_allowed(tmp_path: Path) -> None:
    """#299: `+:merged-branch` passes the semantic check — no false-block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/plusmerged")

    result = _run_hook(repo, env, "git push origin +:feat/plusmerged")

    assert result.returncode == 0, result.stderr


def test_push_plus_force_update_refspec_not_a_delete(tmp_path: Path) -> None:
    """Overreach guard: `+src:dst` (non-empty source) is a force UPDATE, not a delete —
    this detector must not match it (force-push is the destructive detector's domain)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/plusup")

    result = _run_hook(repo, env, "git push origin +feat/plusup:feat/plusup")

    assert result.returncode == 0, result.stderr


def _run_hook_at(cwd: Path, env: dict[str, str], command: str) -> subprocess.CompletedProcess[str]:
    """Invoke the detector from an arbitrary cwd (env-targeting cases, #299)."""
    return subprocess.run(
        [sys.executable, DETECT, command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_env_git_dir_targeting_blocks_unmerged(tmp_path: Path) -> None:
    """#299: GIT_DIR=<repo>/.git retargets the repo from a foreign cwd — the semantic
    check must follow it instead of silently checking the wrong (or no) repo."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envdir")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"GIT_DIR={repo}/.git git branch -D feat/envdir")

    assert result.returncode == 2, result.stderr


def test_env_git_dir_work_tree_pair_blocks_unmerged(tmp_path: Path) -> None:
    """#299: the classic GIT_DIR + GIT_WORK_TREE pair retargets the repo the same way."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envboth")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(
        foreign, env, f"GIT_DIR={repo}/.git GIT_WORK_TREE={repo} git branch -D feat/envboth"
    )

    assert result.returncode == 2, result.stderr


def test_env_git_dir_merged_allowed(tmp_path: Path) -> None:
    """#299: the env-targeted semantic check runs in the TARGET repo — merged branch passes."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/envmerged")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"GIT_DIR={repo}/.git git branch -d feat/envmerged")

    assert result.returncode == 0, result.stderr


def test_env_git_dir_variable_value_fails_closed(tmp_path: Path) -> None:
    """#299: GIT_DIR=$REPO cannot be resolved lexically — fail closed like a $-context path."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, "GIT_DIR=$REPO/.git git branch -D feat/x")

    assert result.returncode == 2, result.stderr


def test_env_wrapper_git_dir_targeting_blocks(tmp_path: Path) -> None:
    """#299: assignments passed as `env` wrapper operands retarget the repo too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envwrap")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"env GIT_DIR={repo}/.git git branch -D feat/envwrap")

    assert result.returncode == 2, result.stderr


def test_non_context_env_var_with_variable_stays_allowed(tmp_path: Path) -> None:
    """Overreach guard: $ in a NON-repo-targeting env var (GIT_TRACE=$T) must not
    trip the unverifiable-context fail-close; the merged delete stays allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/envbenign")

    result = _run_hook(repo, env, "GIT_TRACE=$T git branch -d feat/envbenign")

    assert result.returncode == 0, result.stderr


def test_branch_force_move_unmerged_blocks(tmp_path: Path) -> None:
    """#299: `git branch -f X <start>` moves X's tip and discards its old commits —
    same work-loss as a delete when X is unmerged."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fm")

    result = _run_hook(repo, env, "git branch -f feat/fm prodx/main")

    assert result.returncode == 2, result.stderr


def test_branch_force_move_long_flag_blocks(tmp_path: Path) -> None:
    """#299: long `--force` form of the branch force-move."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fmlong")

    result = _run_hook(repo, env, "git branch --force feat/fmlong prodx/main")

    assert result.returncode == 2, result.stderr


def test_branch_force_move_missing_branch_allowed(tmp_path: Path) -> None:
    """#299: force-'moving' a branch that does not exist is a plain create — no loss."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "git branch -f feat/brand-new prodx/main")

    assert result.returncode == 0, result.stderr


def test_branch_force_move_merged_allowed(tmp_path: Path) -> None:
    """#299: force-moving a merged branch discards nothing unmerged — allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/fmmerged")

    result = _run_hook(repo, env, "git branch -f feat/fmmerged prodx/main")

    assert result.returncode == 0, result.stderr


def test_branch_force_move_upstream_flag_not_a_force_move(tmp_path: Path) -> None:
    """#309 (corrects #299): `-u <upstream>` puts `git branch` in set-upstream mode —
    the branch tip is never reset regardless of `-f` (verified UNCHANGED live), so this
    is NOT a force-move and must be allowed. The prior test pinned the false-block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fmup")

    result = _run_hook(repo, env, "git branch -f -u prodx/main feat/fmup")

    assert result.returncode == 0, result.stderr


def test_branch_force_rename_out_of_scope_allowed(tmp_path: Path) -> None:
    """Scope pin: rename/copy forms (-M/-C/--move/--copy) are documented out-of-scope
    for #299 (the issue names branch -f / checkout -B / switch -C only)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fmren")

    result = _run_hook(repo, env, "git branch -M feat/fmren feat/fmren2")

    assert result.returncode == 0, result.stderr


def test_checkout_force_create_unmerged_blocks(tmp_path: Path) -> None:
    """#299: `git checkout -B X` resets an existing X to HEAD/<start> — work-loss."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/cofm")

    result = _run_hook(repo, env, "git checkout -B feat/cofm")

    assert result.returncode == 2, result.stderr


def test_checkout_force_create_glued_blocks(tmp_path: Path) -> None:
    """#299: glued `-Bname` binds the branch name to the flag."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/coglue")

    result = _run_hook(repo, env, "git checkout -Bfeat/coglue")

    assert result.returncode == 2, result.stderr


def test_checkout_lowercase_b_stays_allowed(tmp_path: Path) -> None:
    """Overreach guard: `-b` is non-forcing — git itself refuses to reuse an existing
    name, so nothing is lost. Must not block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/cosafe")

    result = _run_hook(repo, env, "git checkout -b feat/cosafe")

    assert result.returncode == 0, result.stderr


def test_switch_force_create_unmerged_blocks(tmp_path: Path) -> None:
    """#299: `git switch -C X` is checkout -B's twin — same work-loss."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/swfm")

    result = _run_hook(repo, env, "git switch -C feat/swfm")

    assert result.returncode == 2, result.stderr


def test_switch_long_force_create_blocks(tmp_path: Path) -> None:
    """#299: long `--force-create` (spaced and = forms) of switch."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/swlong")

    spaced = _run_hook(repo, env, "git switch --force-create feat/swlong")
    joined = _run_hook(repo, env, "git switch --force-create=feat/swlong")

    assert spaced.returncode == 2, spaced.stderr
    assert joined.returncode == 2, joined.stderr


def test_switch_plain_stays_allowed(tmp_path: Path) -> None:
    """Overreach guard: a plain `git switch X` moves nothing — allowed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/swplain")

    result = _run_hook(repo, env, "git switch feat/swplain")

    assert result.returncode == 0, result.stderr


def test_checkout_force_create_variable_operand_fails_closed(tmp_path: Path) -> None:
    """#299: `checkout -B $BR` cannot be verified lexically — fail closed."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "git checkout -B $BR")

    assert result.returncode == 2, result.stderr


def test_branch_force_move_sort_arg_eaten(tmp_path: Path) -> None:
    """#299 red-team: required-arg long opts (--sort <key>) must EAT their argument —
    else the key displaces the operand and the real move is checked against 'committerdate'.
    (Verified live: git happily force-moves with --sort present.)"""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fmsort")

    result = _run_hook(repo, env, "git branch -f --sort committerdate feat/fmsort prodx/main")

    assert result.returncode == 2, result.stderr


def test_branch_force_move_format_arg_eaten(tmp_path: Path) -> None:
    """#299 red-team: --format <fmt> is the same required-arg displacement vehicle."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/fmfmt")

    result = _run_hook(repo, env, "git branch -f --format '%(refname)' feat/fmfmt prodx/main")

    assert result.returncode == 2, result.stderr


def test_env_i_wrapper_clears_stale_git_dir(tmp_path: Path) -> None:
    """#299 red-team: `env -i` clears the environment — a GIT_DIR set BEFORE it never
    reaches git, which operates on the cwd repo. The check must follow the cwd repo."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envi")
    other = tmp_path / "other"
    other.mkdir()

    result = _run_hook(repo, env, f"GIT_DIR={other}/.git env -i git branch -D feat/envi")

    assert result.returncode == 2, result.stderr


def test_env_dash_clears_stale_git_dir(tmp_path: Path) -> None:
    """#299: `env -` is the historic spelling of `env -i` — same clearing, and the
    lexer must not stop the wrapper scan at the bare dash (git DOES run after it)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envdash")
    other = tmp_path / "other"
    other.mkdir()

    result = _run_hook(repo, env, f"GIT_DIR={other}/.git env - git branch -D feat/envdash")

    assert result.returncode == 2, result.stderr


def test_sudo_default_drops_stale_git_dir(tmp_path: Path) -> None:
    """#299: sudo's default env_reset strips GIT_* vars set BEFORE it — git operates
    on the cwd repo, so the check must too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/sudodrop")
    other = tmp_path / "other"
    other.mkdir()

    result = _run_hook(repo, env, f"GIT_DIR={other}/.git sudo git branch -D feat/sudodrop")

    assert result.returncode == 2, result.stderr


def test_sudo_preserve_env_keeps_git_dir(tmp_path: Path) -> None:
    """Pin against over-clearing: `sudo -E` preserves the environment, so the
    GIT_DIR set before it DOES reach git — keep threading it."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/sudokeep")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"GIT_DIR={repo}/.git sudo -E git branch -D feat/sudokeep")

    assert result.returncode == 2, result.stderr


def test_env_unset_drops_git_dir(tmp_path: Path) -> None:
    """#299: `env -u GIT_DIR` unsets the var — git runs at the (non-repo) cwd and
    errors, nothing is lost. Threading the stale GIT_DIR would false-block."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envunset")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"GIT_DIR={repo}/.git env -u GIT_DIR git branch -D feat/envunset")

    assert result.returncode == 0, result.stderr


def test_checkout_clustered_force_create_blocks(tmp_path: Path) -> None:
    """#299 red-team r2: -B inside a short cluster (-qB feat/x) still force-creates."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/clB")

    result = _run_hook(repo, env, "git checkout -qB feat/clB")

    assert result.returncode == 2, result.stderr


def test_checkout_clustered_glued_force_create_blocks(tmp_path: Path) -> None:
    """#299 red-team r2: glued value after the clustered flag (-fBname)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/clglue")

    result = _run_hook(repo, env, "git checkout -fBfeat/clglue")

    assert result.returncode == 2, result.stderr


def test_switch_clustered_force_create_blocks(tmp_path: Path) -> None:
    """#299 red-team r2: switch's -C clusters the same way (-fC feat/x)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/clsw")

    result = _run_hook(repo, env, "git switch -fC feat/clsw")

    assert result.returncode == 2, result.stderr


def test_checkout_clustered_lowercase_b_stays_allowed(tmp_path: Path) -> None:
    """Overreach guard: lowercase -b in a cluster (-qb) is still non-forcing."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/clsafe")

    result = _run_hook(repo, env, "git checkout -qb feat/clsafe")

    assert result.returncode == 0, result.stderr


def test_env_chdir_targeting_blocks(tmp_path: Path) -> None:
    """#299 red-team r2: `env -C <dir>` runs git IN <dir> — the semantic check must
    follow it (threaded as a leading git -C)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envc")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"env -C {repo} git branch -D feat/envc")

    assert result.returncode == 2, result.stderr


def test_env_chdir_variable_fails_closed(tmp_path: Path) -> None:
    """#299: `env -C $DIR` cannot be resolved lexically — fail closed."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, "env -C $DIR git branch -D feat/x")

    assert result.returncode == 2, result.stderr


def test_sudo_chdir_targeting_blocks(tmp_path: Path) -> None:
    """#299: sudo -D/--chdir is the same cwd retarget as env -C."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/sudoc")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    spaced = _run_hook_at(foreign, env, f"sudo -D {repo} git branch -D feat/sudoc")
    joined = _run_hook_at(foreign, env, f"sudo --chdir={repo} git branch -D feat/sudoc")

    assert spaced.returncode == 2, spaced.stderr
    assert joined.returncode == 2, joined.stderr


def test_env_clustered_chdir_targeting_blocks(tmp_path: Path) -> None:
    """#299 red-team r3: `env -iC <repo>` (bundle, arg-taker last) clears env AND
    chdirs — real env runs git in <repo> (verified live). Lexer must not stop at
    the bundle; the check must follow the chdir."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envic")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    result = _run_hook_at(foreign, env, f"env -iC {repo} git branch -D feat/envic")

    assert result.returncode == 2, result.stderr


def test_env_mid_bundle_unset_still_finds_git(tmp_path: Path) -> None:
    """Pin: `env -uC git …` unsets variable C (mid-bundle arg-taker binds the
    REMAINDER) — git is the very next token and the cwd repo is checked."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/envuc")

    result = _run_hook(repo, env, "env -uC git branch -D feat/envuc")

    assert result.returncode == 2, result.stderr


def test_checkout_soft_b_bundle_not_forcing(tmp_path: Path) -> None:
    """#299 red-team r3 FP: in `-bB` the FIRST arg-taker (-b) binds 'B' as its
    branch name — git creates branch 'B', feat/x is never reset (verified live)."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/softb")

    result = _run_hook(repo, env, "git checkout -bB feat/softb")

    assert result.returncode == 0, result.stderr


def test_switch_soft_c_bundle_not_forcing(tmp_path: Path) -> None:
    """#299 red-team r3 FP: same for switch — `-cC` creates branch 'C', non-forcing."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/softc")

    result = _run_hook(repo, env, "git switch -cC feat/softc")

    assert result.returncode == 0, result.stderr


def test_prev_branch_shorthand_delete_blocks(tmp_path: Path) -> None:
    """#299 red-team r4: `git branch -D @{-1}` deletes the PREVIOUS branch (verified
    live) — the guard must resolve the shorthand instead of failing the ref lookup."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/prev")  # leaves @{-1} = feat/prev

    result = _run_hook(repo, env, "git branch -D @{-1}")

    assert result.returncode == 2, result.stderr


def test_prev_branch_shorthand_force_create_blocks(tmp_path: Path) -> None:
    """#299 red-team r4: `git checkout -B @{-1}` force-resets the previous branch."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/prevB")

    result = _run_hook(repo, env, "git checkout -B @{-1}")

    assert result.returncode == 2, result.stderr


def test_prev_branch_shorthand_merged_allowed(tmp_path: Path) -> None:
    """Pin: @{-1} resolving to a MERGED branch passes the semantic check."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_merged_branch(repo, env, "feat/prevmerged")

    result = _run_hook(repo, env, "git branch -d @{-1}")

    assert result.returncode == 0, result.stderr


def test_prev_branch_shorthand_unresolvable_allowed(tmp_path: Path) -> None:
    """Pin: an out-of-range @{-9} does not resolve — git itself errors, nothing lost."""
    repo, env = _make_multi_orphan_repo(tmp_path)

    result = _run_hook(repo, env, "git branch -D @{-9}")

    assert result.returncode == 0, result.stderr


def test_push_heads_abbreviation_blocks(tmp_path: Path) -> None:
    """#299 red-team r5: `heads/<branch>` is a valid refname abbreviation — the remote
    resolves it to refs/heads/<branch> (verified live: the remote branch was deleted).
    All three delete spellings must normalize it."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/hd")

    colon = _run_hook(repo, env, "git push origin :heads/feat/hd")
    plus = _run_hook(repo, env, "git push origin +:heads/feat/hd")
    delete = _run_hook(repo, env, "git push origin --delete heads/feat/hd")

    assert colon.returncode == 2, colon.stderr
    assert plus.returncode == 2, plus.stderr
    assert delete.returncode == 2, delete.stderr


def test_xargs_replstr_lookalike_not_an_assignment(tmp_path: Path) -> None:
    """#299 codex-review P1: `xargs -I GIT_DIR=…` — the token is xargs' REPLACEMENT
    STRING, not an env assignment; the child git runs in the cwd repo (verified live).
    Collecting it as GIT_DIR would point the check at the wrong repo."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/xrepl")
    other = tmp_path / "other"
    other.mkdir()

    result = _run_hook(repo, env, f"echo x | xargs -I GIT_DIR={other}/.git git branch -D feat/xrepl")

    assert result.returncode == 2, result.stderr


def test_sudo_prompt_lookalike_not_an_assignment(tmp_path: Path) -> None:
    """#299 codex-review P1: `sudo -p GIT_DIR=…` — the token is sudo's PROMPT string;
    same wrong-repo displacement through a different wrapper flag."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/sprompt")
    other = tmp_path / "other"
    other.mkdir()

    result = _run_hook(repo, env, f"sudo -p GIT_DIR={other}/.git git branch -D feat/sprompt")

    assert result.returncode == 2, result.stderr


def test_local_branch_literally_named_heads_blocks(tmp_path: Path) -> None:
    """#299 codex-review P2: a local branch CAN be literally named heads/x and
    `git branch -D heads/x` deletes exactly it (verified live) — the heads/
    normalization must apply to push refspecs only, not local operands."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "heads/feat-lit")

    result = _run_hook(repo, env, "git branch -D heads/feat-lit")

    assert result.returncode == 2, result.stderr


# --- #309 multi-angle review findings -------------------------------------------------

def test_checkout_force_create_repeated_last_wins_blocks(tmp_path: Path) -> None:
    """#309: git resolves a repeated `-B` to the LAST occurrence — a decoy safe/new
    first operand must not hide the genuinely-unmerged last one."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/last")

    result = _run_hook(repo, env, "git checkout -B totally-new-safe -B feat/last")

    assert result.returncode == 2, result.stderr


def test_checkout_force_create_repeated_glued_last_wins_blocks(tmp_path: Path) -> None:
    """#309: the glued `-Bname` repeated form resolves to the last occurrence too."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/lastglue")

    result = _run_hook(repo, env, "git checkout -Btotally-new-safe -Bfeat/lastglue")

    assert result.returncode == 2, result.stderr


def test_switch_force_create_repeated_last_wins_blocks(tmp_path: Path) -> None:
    """#309: switch's `-C` repeated form is the checkout twin."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/lastsw")

    result = _run_hook(repo, env, "git switch -C totally-new-safe -C feat/lastsw")

    assert result.returncode == 2, result.stderr


def test_switch_long_force_create_repeated_last_wins_blocks(tmp_path: Path) -> None:
    """#309: repeated `--force-create=` resolves to the last occurrence."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/lastlong")

    result = _run_hook(
        repo, env,
        "git switch --force-create=totally-new-safe --force-create=feat/lastlong",
    )

    assert result.returncode == 2, result.stderr


def test_force_create_repeated_last_operand_fresh_branch_allowed(tmp_path: Path) -> None:
    """#309 last-wins the other direction: when the LAST `-B` targets a fresh branch
    git resets nothing, even though an earlier `-B` named an unmerged branch — ALLOW."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/decoy")

    result = _run_hook(repo, env, "git checkout -B feat/decoy -B totally-fresh")

    assert result.returncode == 0, result.stderr


def test_branch_set_upstream_glued_not_a_force_move_allowed(tmp_path: Path) -> None:
    """#309: `--set-upstream-to=<u>` puts branch in set-upstream mode — the tip is never
    reset regardless of `-f` (verified UNCHANGED live), so it is not a force-move."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/suglue")

    result = _run_hook(repo, env, "git branch --force --set-upstream-to=prodx/main feat/suglue")

    assert result.returncode == 0, result.stderr


def test_branch_unset_upstream_with_force_allowed(tmp_path: Path) -> None:
    """#309: `--unset-upstream` with `-f` only edits tracking config — non-destructive."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "feat/unsetup")

    result = _run_hook(repo, env, "git branch -f --unset-upstream feat/unsetup")

    assert result.returncode == 0, result.stderr


def test_branch_delete_sort_arg_not_misread_as_operand(tmp_path: Path) -> None:
    """#309: in delete mode `--sort <key>` consumes its value (verified live); a local
    unmerged branch that shares the key's name must not be read as a delete operand."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    _make_unmerged_branch(repo, env, "committerdate")
    _make_merged_branch(repo, env, "feat/merged")

    # fixture sanity: committerdate really is unmerged (a direct delete blocks)
    guard = _run_hook(repo, env, "git branch -D committerdate")
    assert guard.returncode == 2, guard.stderr

    # the real command deletes only the merged feat/merged; --sort eats committerdate
    result = _run_hook(repo, env, "git branch -D --sort committerdate feat/merged")
    assert result.returncode == 0, result.stderr


def test_run_git_context_fifo_head_blocks_without_hanging(tmp_path: Path) -> None:
    """#309: a `--git-dir` context whose HEAD is a writer-less FIFO must not hang the
    detector — run_git carries a timeout. Since detection already matched a destructive
    command it could not clear, a verify timeout fails CLOSED (block), not open: a slow
    (not hung) repo would let the real `git branch -D` complete and destroy work."""
    repo, env = _make_multi_orphan_repo(tmp_path)
    fakegit = tmp_path / "fakegit"
    fakegit.mkdir()
    os.mkfifo(fakegit / "HEAD")
    env = dict(env, GUARD_GIT_TIMEOUT_S="3")

    try:
        result = subprocess.run(
            [sys.executable, DETECT, f"git --git-dir={fakegit} branch -D someunmerged"],
            cwd=repo, env=env, text=True, capture_output=True, check=False, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("detector hung on FIFO HEAD context — run_git has no timeout")

    assert result.returncode == 2, result.stderr


TestCallable = Callable[[Path], None]


TESTS: list[tuple[str, TestCallable]] = [
    ("test_flat_branch_merged_to_product_main_is_allowed", test_flat_branch_merged_to_product_main_is_allowed),
    ("test_flat_branch_unique_commit_blocks_against_true_product_base", test_flat_branch_unique_commit_blocks_against_true_product_base),
    ("test_name_prefix_rule_blocks_then_allows_after_merge", test_name_prefix_rule_blocks_then_allows_after_merge),
    ("test_deleting_product_main_compares_against_root_main", test_deleting_product_main_compares_against_root_main),
    ("test_escape_hatch_allows_delete_command", test_escape_hatch_allows_delete_command),
    ("test_non_delete_command_is_allowed", test_non_delete_command_is_allowed),
    ("test_missing_local_branch_is_allowed", test_missing_local_branch_is_allowed),
    ("test_dash_c_form_blocks_unmerged", test_dash_c_form_blocks_unmerged),
    ("test_cross_repo_dash_c_blocks_using_target_repo", test_cross_repo_dash_c_blocks_using_target_repo),
    ("test_cross_repo_dash_c_allows_merged_in_target", test_cross_repo_dash_c_allows_merged_in_target),
    ("test_long_form_delete_blocks_unmerged", test_long_form_delete_blocks_unmerged),
    ("test_long_form_delete_force_blocks_unmerged", test_long_form_delete_force_blocks_unmerged),
    ("test_config_global_option_blocks_unmerged", test_config_global_option_blocks_unmerged),
    ("test_combined_global_options_block_unmerged", test_combined_global_options_block_unmerged),
    ("test_no_pager_flag_blocks_unmerged", test_no_pager_flag_blocks_unmerged),
    ("test_double_space_blocks_unmerged", test_double_space_blocks_unmerged),
    ("test_push_colon_refspec_blocks_unmerged", test_push_colon_refspec_blocks_unmerged),
    ("test_git_dir_form_blocks_unmerged", test_git_dir_form_blocks_unmerged),
    ("test_work_tree_form_blocks_unmerged", test_work_tree_form_blocks_unmerged),
    ("test_compound_command_with_dash_c_blocks", test_compound_command_with_dash_c_blocks),
    ("test_compound_command_literal_still_blocks", test_compound_command_literal_still_blocks),
    ("test_dash_c_form_allows_merged", test_dash_c_form_allows_merged),
    ("test_escape_hatch_with_dash_c_form", test_escape_hatch_with_dash_c_form),
    ("test_dash_c_non_delete_stays_allowed", test_dash_c_non_delete_stays_allowed),
    ("test_push_delete_flag_before_remote_blocks", test_push_delete_flag_before_remote_blocks),
    ("test_quoted_branch_name_blocks[quote=']", lambda tmp_path: test_quoted_branch_name_blocks(tmp_path, "'")),
    ('test_quoted_branch_name_blocks[quote="]', lambda tmp_path: test_quoted_branch_name_blocks(tmp_path, '"')),
    ("test_quoted_dash_c_path_blocks", test_quoted_dash_c_path_blocks),
    ("test_quoted_git_dir_path_blocks", test_quoted_git_dir_path_blocks),
    ("test_backslash_newline_continuation_blocks", test_backslash_newline_continuation_blocks),
    ("test_variable_branch_token_blocks_conservatively", test_variable_branch_token_blocks_conservatively),
    ("test_multiple_branches_second_unmerged_blocks", test_multiple_branches_second_unmerged_blocks),
    ("test_multiple_branches_all_merged_allowed", test_multiple_branches_all_merged_allowed),
    ("test_push_short_d_blocks", test_push_short_d_blocks),
    ("test_push_delete_multiple_second_unmerged_blocks", test_push_delete_multiple_second_unmerged_blocks),
    ("test_push_colon_multiple_second_unmerged_blocks", test_push_colon_multiple_second_unmerged_blocks),
    ("test_push_colon_full_ref_blocks", test_push_colon_full_ref_blocks),
    ("test_push_delete_before_separator_blocks", test_push_delete_before_separator_blocks),
    ("test_push_colon_before_separator_blocks", test_push_colon_before_separator_blocks),
    ("test_embedded_variable_operand_blocks_conservatively", test_embedded_variable_operand_blocks_conservatively),
    ("test_variable_context_path_blocks_conservatively", test_variable_context_path_blocks_conservatively),
    ("test_variable_context_path_non_delete_stays_allowed", test_variable_context_path_non_delete_stays_allowed),
    ("test_combined_short_flags_block[flags=-fd]", lambda tmp_path: test_combined_short_flags_block(tmp_path, "-fd")),
    ("test_combined_short_flags_block[flags=-fD]", lambda tmp_path: test_combined_short_flags_block(tmp_path, "-fD")),
    ("test_combined_short_flags_block[flags=-Df]", lambda tmp_path: test_combined_short_flags_block(tmp_path, "-Df")),
    ("test_fast_path_spawns_no_git", test_fast_path_spawns_no_git),
    ("test_command_wrapper_blocks_unmerged", test_command_wrapper_blocks_unmerged),
    ("test_sudo_arg_flag_wrapper_blocks_unmerged", test_sudo_arg_flag_wrapper_blocks_unmerged),
    ("test_xargs_replace_flag_wrapper_blocks_unmerged", test_xargs_replace_flag_wrapper_blocks_unmerged),
    ("test_env_wrapper_blocks_push_delete", test_env_wrapper_blocks_push_delete),
    ("test_nice_wrapper_blocks_unmerged", test_nice_wrapper_blocks_unmerged),
    ("test_wrapper_variable_branch_fails_closed", test_wrapper_variable_branch_fails_closed),
    ("test_wrapper_escape_hatch_still_allows", test_wrapper_escape_hatch_still_allows),
    ("test_xargs_stdin_documented_allow", test_xargs_stdin_documented_allow),
    ("test_non_wrapper_prefix_stays_allowed", test_non_wrapper_prefix_stays_allowed),
    ("test_remotes_flag_targets_remote_tracking_not_local", test_remotes_flag_targets_remote_tracking_not_local),
    ("test_remotes_flag_clustered_allowed", test_remotes_flag_clustered_allowed),
    ("test_remotes_long_flag_allowed", test_remotes_long_flag_allowed),
    ("test_local_branch_named_like_remote_still_blocks", test_local_branch_named_like_remote_still_blocks),
    ("test_push_plus_colon_refspec_blocks_unmerged", test_push_plus_colon_refspec_blocks_unmerged),
    ("test_push_plus_colon_full_ref_blocks", test_push_plus_colon_full_ref_blocks),
    ("test_push_plus_colon_merged_allowed", test_push_plus_colon_merged_allowed),
    ("test_push_plus_force_update_refspec_not_a_delete", test_push_plus_force_update_refspec_not_a_delete),
    ("test_env_git_dir_targeting_blocks_unmerged", test_env_git_dir_targeting_blocks_unmerged),
    ("test_env_git_dir_work_tree_pair_blocks_unmerged", test_env_git_dir_work_tree_pair_blocks_unmerged),
    ("test_env_git_dir_merged_allowed", test_env_git_dir_merged_allowed),
    ("test_env_git_dir_variable_value_fails_closed", test_env_git_dir_variable_value_fails_closed),
    ("test_env_wrapper_git_dir_targeting_blocks", test_env_wrapper_git_dir_targeting_blocks),
    ("test_non_context_env_var_with_variable_stays_allowed", test_non_context_env_var_with_variable_stays_allowed),
    ("test_branch_force_move_unmerged_blocks", test_branch_force_move_unmerged_blocks),
    ("test_branch_force_move_long_flag_blocks", test_branch_force_move_long_flag_blocks),
    ("test_branch_force_move_missing_branch_allowed", test_branch_force_move_missing_branch_allowed),
    ("test_branch_force_move_merged_allowed", test_branch_force_move_merged_allowed),
    ("test_branch_force_move_upstream_flag_not_a_force_move", test_branch_force_move_upstream_flag_not_a_force_move),
    ("test_branch_force_rename_out_of_scope_allowed", test_branch_force_rename_out_of_scope_allowed),
    ("test_checkout_force_create_unmerged_blocks", test_checkout_force_create_unmerged_blocks),
    ("test_checkout_force_create_glued_blocks", test_checkout_force_create_glued_blocks),
    ("test_checkout_lowercase_b_stays_allowed", test_checkout_lowercase_b_stays_allowed),
    ("test_switch_force_create_unmerged_blocks", test_switch_force_create_unmerged_blocks),
    ("test_switch_long_force_create_blocks", test_switch_long_force_create_blocks),
    ("test_switch_plain_stays_allowed", test_switch_plain_stays_allowed),
    ("test_checkout_force_create_variable_operand_fails_closed", test_checkout_force_create_variable_operand_fails_closed),
    ("test_branch_force_move_sort_arg_eaten", test_branch_force_move_sort_arg_eaten),
    ("test_branch_force_move_format_arg_eaten", test_branch_force_move_format_arg_eaten),
    ("test_env_i_wrapper_clears_stale_git_dir", test_env_i_wrapper_clears_stale_git_dir),
    ("test_env_dash_clears_stale_git_dir", test_env_dash_clears_stale_git_dir),
    ("test_sudo_default_drops_stale_git_dir", test_sudo_default_drops_stale_git_dir),
    ("test_sudo_preserve_env_keeps_git_dir", test_sudo_preserve_env_keeps_git_dir),
    ("test_env_unset_drops_git_dir", test_env_unset_drops_git_dir),
    ("test_checkout_clustered_force_create_blocks", test_checkout_clustered_force_create_blocks),
    ("test_checkout_clustered_glued_force_create_blocks", test_checkout_clustered_glued_force_create_blocks),
    ("test_switch_clustered_force_create_blocks", test_switch_clustered_force_create_blocks),
    ("test_checkout_clustered_lowercase_b_stays_allowed", test_checkout_clustered_lowercase_b_stays_allowed),
    ("test_env_chdir_targeting_blocks", test_env_chdir_targeting_blocks),
    ("test_env_chdir_variable_fails_closed", test_env_chdir_variable_fails_closed),
    ("test_sudo_chdir_targeting_blocks", test_sudo_chdir_targeting_blocks),
    ("test_env_clustered_chdir_targeting_blocks", test_env_clustered_chdir_targeting_blocks),
    ("test_env_mid_bundle_unset_still_finds_git", test_env_mid_bundle_unset_still_finds_git),
    ("test_checkout_soft_b_bundle_not_forcing", test_checkout_soft_b_bundle_not_forcing),
    ("test_switch_soft_c_bundle_not_forcing", test_switch_soft_c_bundle_not_forcing),
    ("test_prev_branch_shorthand_delete_blocks", test_prev_branch_shorthand_delete_blocks),
    ("test_prev_branch_shorthand_force_create_blocks", test_prev_branch_shorthand_force_create_blocks),
    ("test_prev_branch_shorthand_merged_allowed", test_prev_branch_shorthand_merged_allowed),
    ("test_prev_branch_shorthand_unresolvable_allowed", test_prev_branch_shorthand_unresolvable_allowed),
    ("test_push_heads_abbreviation_blocks", test_push_heads_abbreviation_blocks),
    ("test_xargs_replstr_lookalike_not_an_assignment", test_xargs_replstr_lookalike_not_an_assignment),
    ("test_sudo_prompt_lookalike_not_an_assignment", test_sudo_prompt_lookalike_not_an_assignment),
    ("test_local_branch_literally_named_heads_blocks", test_local_branch_literally_named_heads_blocks),
    ("test_checkout_force_create_repeated_last_wins_blocks", test_checkout_force_create_repeated_last_wins_blocks),
    ("test_checkout_force_create_repeated_glued_last_wins_blocks", test_checkout_force_create_repeated_glued_last_wins_blocks),
    ("test_switch_force_create_repeated_last_wins_blocks", test_switch_force_create_repeated_last_wins_blocks),
    ("test_switch_long_force_create_repeated_last_wins_blocks", test_switch_long_force_create_repeated_last_wins_blocks),
    ("test_force_create_repeated_last_operand_fresh_branch_allowed", test_force_create_repeated_last_operand_fresh_branch_allowed),
    ("test_branch_set_upstream_glued_not_a_force_move_allowed", test_branch_set_upstream_glued_not_a_force_move_allowed),
    ("test_branch_unset_upstream_with_force_allowed", test_branch_unset_upstream_with_force_allowed),
    ("test_branch_delete_sort_arg_not_misread_as_operand", test_branch_delete_sort_arg_not_misread_as_operand),
    ("test_run_git_context_fifo_head_blocks_without_hanging", test_run_git_context_fifo_head_blocks_without_hanging),
]


def _run_one(name: str, fn: TestCallable) -> str | None:
    tmp = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    try:
        fn(tmp)
        return None
    except Exception:
        return traceback.format_exc().rstrip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    failures: list[tuple[str, str]] = []
    for name, fn in TESTS:
        failure = _run_one(name, fn)
        if failure is not None:
            failures.append((name, failure))

    for name, failure in failures:
        print(f"FAIL  {name}")
        print(failure)

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
