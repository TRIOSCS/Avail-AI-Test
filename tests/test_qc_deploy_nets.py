"""tests/test_qc_deploy_nets.py — QC 2026-08-08 cluster 4 (deploy safety nets).

Both nets were broken-on-use:
- deploy.yml verify-ci exact-matched a check named "test", but the CI test job
  is a 2-way matrix, so GitHub emits "test (0)"/"test (1)" — the gate matched
  nothing, waited the full window, then refused EVERY release-published deploy.
- deploy.sh rollback restored the old app IMAGE but not the DB; after a
  migration-bearing deploy the old image's `alembic upgrade head` hit
  "Can't locate revision" and crash-looped.

The gate's aggregation is shell awk, tested here behaviorally; the DB-aware
rollback is asserted by content (its live path needs docker + a failed deploy).

Called by: pytest autodiscovery
Depends on: PyYAML, the workflow + deploy.sh files, bash + awk on PATH.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"
DEPLOY_SH = REPO / "deploy.sh"


def _agg_awk() -> str:
    """Extract the agg_state awk program from deploy.yml (single source of truth).

    The awk body contains no single-quote, so the first \' after \"awk \'\" is the
    terminating quote.
    """
    text = DEPLOY_YML.read_text()
    m = re.search(r"awk '(.*?)'", text, re.DOTALL)
    assert m, "agg_state awk program not found in deploy.yml"
    return "awk '" + m.group(1) + "'"


def _run_agg(states: list[str]) -> str:
    prog = _agg_awk()
    proc = subprocess.run(
        ["bash", "-c", f"printf '%s\\n' {' '.join(repr(s) for s in states)} | {prog}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which("awk"), reason="awk required")
class TestGateAggregation:
    def test_all_success_is_success(self):
        assert _run_agg(["success", "success"]) == "success"

    def test_any_failure_is_failure(self):
        assert _run_agg(["success", "failure"]) == "failure"
        assert _run_agg(["cancelled", "success"]) == "failure"
        assert _run_agg(["timed_out"]) == "failure"

    def test_any_pending_is_pending(self):
        assert _run_agg(["success", "pending"]) == "pending"

    def test_no_runs_is_pending_not_success(self):
        # The old bug: zero matching runs must NEVER read as green.
        assert _run_agg([]) == "pending"

    def test_single_success_is_success(self):
        assert _run_agg(["success"]) == "success"


def test_gate_matches_matrix_shard_names():
    """The jq selector must accept BASE and 'BASE (N)' shard names, not only exact."""
    text = DEPLOY_YML.read_text()
    assert 'startswith(\\"$1 (\\")' in text, "gate no longer matches matrix shard check names"
    # And it must not have regressed to the exact-only matcher.
    assert 'select(.name==\\"$1\\") | (if .status' not in text


def test_deploy_yml_structure_intact():
    """The gate edit must keep deploy.yml a valid workflow with the verify-ci gate still
    guarding the deploy job."""
    wf = yaml.safe_load(DEPLOY_YML.read_text())
    jobs = wf["jobs"]
    assert "verify-ci" in jobs, "the CI gate job was removed"
    assert "deploy" in jobs, "the deploy job was removed"
    # The deploy job must still depend on the gate (needs: verify-ci).
    needs = jobs["deploy"].get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "verify-ci" in needs, "deploy no longer waits on the CI gate"


def test_rollback_is_db_aware():
    """Rollback must capture the pre-deploy revision and downgrade before restoring the
    old image (else the old entrypoint crash-loops)."""
    text = DEPLOY_SH.read_text()
    assert "PREV_DB_REVISION=" in text, "pre-deploy DB revision is not captured"
    assert "alembic downgrade" in text, "rollback never downgrades the DB"
    # Ordering: the downgrade must precede the image re-tag in rollback_app.
    downgrade_at = text.index("alembic downgrade")
    retag_at = text.index('docker tag "$PREV_APP_IMAGE_ID"')
    assert downgrade_at < retag_at, "DB downgrade must run BEFORE restoring the old image"


def test_deploy_sh_syntax_valid():
    proc = subprocess.run(["bash", "-n", str(DEPLOY_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, f"deploy.sh syntax error: {proc.stderr}"
