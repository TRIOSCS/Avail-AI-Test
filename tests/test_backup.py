# Tests for the database backup system
# What: Validates backup scripts exist, are executable, have correct structure
# Called by: pytest
# Depends on: scripts/backup.sh, scripts/restore.sh, scripts/backup-to-spaces.sh

import os
import subprocess

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")


class TestBackupScriptExists:
    """Verify all backup scripts exist and are well-formed."""

    @pytest.mark.parametrize(
        "script",
        [
            "backup.sh",
            "restore.sh",
            "backup-to-spaces.sh",
            "backup-cron.sh",
        ],
    )
    def test_script_exists(self, script):
        path = os.path.join(SCRIPTS_DIR, script)
        assert os.path.isfile(path), f"Missing script: {path}"

    @pytest.mark.parametrize(
        "script",
        [
            "backup.sh",
            "restore.sh",
            "backup-to-spaces.sh",
            "backup-cron.sh",
        ],
    )
    def test_script_has_shebang(self, script):
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            first_line = f.readline()
        assert first_line.startswith("#!/"), f"{script} missing shebang line"

    @pytest.mark.parametrize(
        "script",
        [
            "backup.sh",
            "restore.sh",
            "backup-to-spaces.sh",
            "backup-cron.sh",
        ],
    )
    def test_script_has_set_euo_pipefail(self, script):
        """All scripts must use strict mode to fail fast on errors."""
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            content = f.read()
        assert "set -euo pipefail" in content, f"{script} must use 'set -euo pipefail' for safety"

    @pytest.mark.parametrize(
        "script",
        [
            "backup.sh",
            "restore.sh",
            "backup-to-spaces.sh",
            "backup-cron.sh",
        ],
    )
    def test_script_has_header_comment(self, script):
        """All scripts must have a header comment per CLAUDE.md rules."""
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            content = f.read(500)
        assert "What:" in content, f"{script} missing 'What:' header comment"

    @pytest.mark.parametrize(
        "script",
        [
            "backup.sh",
            "restore.sh",
            "backup-to-spaces.sh",
            "backup-cron.sh",
        ],
    )
    def test_script_valid_bash_syntax(self, script):
        """Check scripts pass bash syntax validation."""
        path = os.path.join(SCRIPTS_DIR, script)
        result = subprocess.run(
            ["bash", "-n", path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script} has syntax errors:\n{result.stderr}"


class TestBackupScriptContent:
    """Verify backup.sh has required safety features."""

    def _read_script(self, name):
        with open(os.path.join(SCRIPTS_DIR, name)) as f:
            return f.read()

    def test_backup_checks_db_ready(self):
        content = self._read_script("backup.sh")
        assert "pg_isready" in content, "backup.sh must check database readiness"

    def test_backup_uses_custom_format(self):
        content = self._read_script("backup.sh")
        assert "--format=custom" in content, "backup.sh must use pg_dump custom format (supports parallel restore)"

    def test_backup_compresses_output(self):
        content = self._read_script("backup.sh")
        assert "gzip" in content, "backup.sh must compress backups"

    def test_backup_creates_checksum(self):
        content = self._read_script("backup.sh")
        assert "sha256sum" in content, "backup.sh must create SHA-256 checksum"

    def test_backup_verifies_dump(self):
        content = self._read_script("backup.sh")
        assert "pg_restore --list" in content, "backup.sh must verify dump by listing contents"

    def test_backup_has_rotation(self):
        content = self._read_script("backup.sh")
        assert "RETENTION_DAYS" in content, "backup.sh must support retention rotation"

    def test_backup_checks_minimum_size(self):
        content = self._read_script("backup.sh")
        assert "suspiciously small" in content.lower() or "1024" in content, (
            "backup.sh must reject suspiciously small backups"
        )

    def test_backup_logs_row_counts(self):
        content = self._read_script("backup.sh")
        assert "users" in content and "companies" in content, "backup.sh must log row counts for critical tables"

    def test_backup_writes_latest_marker(self):
        content = self._read_script("backup.sh")
        assert "LATEST" in content, "backup.sh must write a LATEST marker file"


class TestRestoreScriptContent:
    """Verify restore.sh has required safety features."""

    def _read_script(self):
        with open(os.path.join(SCRIPTS_DIR, "restore.sh")) as f:
            return f.read()

    def test_restore_requires_confirmation(self):
        content = self._read_script()
        assert "RESTORE" in content and "confirm" in content, "restore.sh must require interactive confirmation"

    def test_restore_creates_safety_backup(self):
        content = self._read_script()
        assert "pre_restore" in content, "restore.sh must create a safety backup before restoring"

    def test_restore_verifies_checksum(self):
        content = self._read_script()
        assert "sha256sum" in content, "restore.sh must verify checksum"

    def test_restore_supports_list(self):
        content = self._read_script()
        assert "--list" in content, "restore.sh must support --list flag"

    def test_restore_supports_verify(self):
        content = self._read_script()
        assert "--verify" in content, "restore.sh must support --verify flag"

    def test_restore_terminates_connections(self):
        content = self._read_script()
        assert "pg_terminate_backend" in content, "restore.sh must terminate existing connections before DROP"

    def test_restore_post_verification(self):
        content = self._read_script()
        assert "RESTORE COMPLETE" in content, "restore.sh must verify and report after restore"


class TestSpacesScriptContent:
    """Verify backup-to-spaces.sh handles missing config gracefully."""

    def _read_script(self):
        with open(os.path.join(SCRIPTS_DIR, "backup-to-spaces.sh")) as f:
            return f.read()

    def test_spaces_skips_when_unconfigured(self):
        content = self._read_script()
        assert "SKIP" in content or "not set" in content, "backup-to-spaces.sh must gracefully skip when not configured"

    def test_spaces_verifies_upload_size(self):
        content = self._read_script()
        assert "mismatch" in content.lower() or "REMOTE_SIZE" in content, (
            "backup-to-spaces.sh must verify uploaded file size"
        )

    def test_spaces_has_remote_rotation(self):
        content = self._read_script()
        assert "SPACES_RETENTION_DAYS" in content, "backup-to-spaces.sh must support remote rotation"


class TestDockerComposeBackup:
    """Verify docker-compose.yml includes the backup service."""

    def _read_compose(self):
        compose_path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(compose_path) as f:
            return f.read()

    def test_backup_service_defined(self):
        content = self._read_compose()
        assert "db-backup:" in content, "docker-compose.yml must define db-backup service"

    def test_backup_volume_defined(self):
        content = self._read_compose()
        assert "pgbackups:" in content, "docker-compose.yml must define pgbackups volume"

    def test_backup_depends_on_db(self):
        content = self._read_compose()
        # Find the db-backup section and check it depends on db
        idx = content.index("db-backup:")
        section = content[idx : idx + 500]
        assert "db:" in section and "service_healthy" in section, "db-backup must depend on db being healthy"

    def test_backup_mounts_scripts(self):
        content = self._read_compose()
        idx = content.index("db-backup:")
        section = content[idx : idx + 500]
        assert "./scripts:/scripts:ro" in section, "db-backup must mount scripts as read-only"

    def test_backup_mounts_backup_volume(self):
        content = self._read_compose()
        idx = content.index("db-backup:")
        section = content[idx : idx + 500]
        assert "pgbackups:/backups" in section, "db-backup must mount pgbackups volume"
