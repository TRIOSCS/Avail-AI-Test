# Tests for the database backup system
# What: Validates backup scripts exist, are executable, have correct structure
# Called by: pytest
# Depends on: scripts/backup.sh, scripts/restore.sh, scripts/backup-to-spaces.sh

import os
import shutil
import subprocess

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

ALL_SCRIPTS = [
    "backup.sh",
    "restore.sh",
    "backup-to-spaces.sh",
    "backup-cron.sh",
]


def _read_script(name):
    with open(os.path.join(SCRIPTS_DIR, name)) as f:
        return f.read()


class TestBackupScriptExists:
    """Verify all backup scripts exist and are well-formed."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_exists(self, script):
        path = os.path.join(SCRIPTS_DIR, script)
        assert os.path.isfile(path), f"Missing script: {path}"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_has_shebang(self, script):
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            first_line = f.readline()
        assert first_line.startswith("#!/"), f"{script} missing shebang line"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_has_set_euo_pipefail(self, script):
        """All scripts must use strict mode to fail fast on errors."""
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            content = f.read()
        assert "set -euo pipefail" in content, f"{script} must use 'set -euo pipefail' for safety"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_has_header_comment(self, script):
        """All scripts must have a header comment per CLAUDE.md rules."""
        path = os.path.join(SCRIPTS_DIR, script)
        with open(path) as f:
            content = f.read(500)
        assert "What:" in content, f"{script} missing 'What:' header comment"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
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

    def test_backup_checks_db_ready(self):
        content = _read_script("backup.sh")
        assert "pg_isready" in content, "backup.sh must check database readiness"

    def test_backup_uses_custom_format(self):
        content = _read_script("backup.sh")
        assert "--format=custom" in content, "backup.sh must use pg_dump custom format (supports parallel restore)"

    def test_backup_compresses_output(self):
        content = _read_script("backup.sh")
        assert "gzip" in content, "backup.sh must compress backups"

    def test_backup_creates_checksum(self):
        content = _read_script("backup.sh")
        assert "sha256sum" in content, "backup.sh must create SHA-256 checksum"

    def test_backup_verifies_dump(self):
        content = _read_script("backup.sh")
        assert "pg_restore --list" in content, "backup.sh must verify dump by listing contents"

    def test_backup_has_rotation(self):
        content = _read_script("backup.sh")
        assert "RETENTION_DAYS" in content, "backup.sh must support retention rotation"

    def test_backup_checks_minimum_size(self):
        content = _read_script("backup.sh")
        assert "suspiciously small" in content.lower() or "1024" in content, (
            "backup.sh must reject suspiciously small backups"
        )

    def test_backup_logs_row_counts(self):
        content = _read_script("backup.sh")
        assert "users" in content and "companies" in content, "backup.sh must log row counts for critical tables"

    def test_backup_writes_latest_marker(self):
        content = _read_script("backup.sh")
        assert "LATEST" in content, "backup.sh must write a LATEST marker file"

    def test_backup_supports_at_rest_encryption(self):
        """Optional gpg-symmetric AES-256 at-rest encryption, keyed from env."""
        content = _read_script("backup.sh")
        assert "BACKUP_GPG_PASSPHRASE" in content, "backup.sh must read the encryption key from BACKUP_GPG_PASSPHRASE"
        assert "--symmetric" in content and "AES256" in content, "backup.sh encryption must use gpg symmetric AES-256"

    def test_backup_encryption_is_opt_in(self):
        """Encryption must be guarded so an unset key still yields a usable backup."""
        content = _read_script("backup.sh")
        assert 'if [ -n "$GPG_PASSPHRASE" ]' in content, (
            "backup.sh must guard encryption on the passphrase being set (plaintext otherwise)"
        )

    def test_backup_verifies_before_encrypting(self):
        """pg_restore --list must run on the plaintext dump, before encryption."""
        content = _read_script("backup.sh")
        verify_idx = content.index("pg_restore --list")
        encrypt_idx = content.index("--symmetric")
        assert verify_idx < encrypt_idx, (
            "backup.sh must verify the dump before encrypting it (pg_restore can't read a .gpg blob)"
        )


class TestPassphraseNotOnCommandLine:
    """The gpg passphrase must be fed via stdin (--passphrase-fd 0), never as a CLI arg
    — a command-line --passphrase leaks the secret into argv/`ps`."""

    def _read_root(self):
        with open(os.path.join(REPO_ROOT, "backup.sh")) as f:
            return f.read()

    @pytest.mark.parametrize("name", ["backup.sh", "restore.sh"])
    def test_scripts_use_passphrase_fd(self, name):
        content = _read_script(name)
        assert "--passphrase-fd" in content, f"{name} must feed the gpg passphrase via --passphrase-fd (stdin)"
        # The command-line form `--passphrase <value>` (trailing space) must be
        # absent everywhere, including comments, so nothing demonstrates the leak.
        assert "--passphrase " not in content, f"{name} must NOT pass the passphrase on the gpg command line"
        assert "--pinentry-mode loopback" in content, (
            f"{name} needs --pinentry-mode loopback for a non-interactive fd passphrase under --batch"
        )

    def test_root_backup_uses_passphrase_fd(self):
        content = self._read_root()
        assert "--passphrase-fd" in content, "root backup.sh must feed the gpg passphrase via --passphrase-fd (stdin)"
        assert "--passphrase " not in content, "root backup.sh must NOT pass the passphrase on the gpg command line"

    def test_root_backup_keyed_from_canonical_env(self):
        """The host-cron variant must use the same BACKUP_GPG_PASSPHRASE knob."""
        content = self._read_root()
        assert "BACKUP_GPG_PASSPHRASE" in content, "root backup.sh must read BACKUP_GPG_PASSPHRASE"
        assert "UNENCRYPTED" in content, "root backup.sh must warn loudly when storing unencrypted"

    @pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")
    def test_gpg_symmetric_roundtrip_matches_script_invocation(self, tmp_path):
        """Functional proof that the exact gpg flags the scripts use encrypt and decrypt
        a payload correctly (passphrase via stdin fd 0) and reject a wrong key.

        Uses an isolated GNUPGHOME so it never touches a real keyring.
        """
        plaintext = tmp_path / "payload.bin"
        plaintext.write_bytes(b"AVAILAI-BACKUP-SENTINEL-" + os.urandom(64))
        enc = tmp_path / "payload.bin.gpg"
        key = "correct horse battery staple"
        gnupg = tmp_path / "gnupg"
        gnupg.mkdir(mode=0o700)
        env = {**os.environ, "GNUPGHOME": str(gnupg)}

        # Encrypt with the exact flags the scripts use (passphrase via stdin fd 0).
        enc_proc = subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--output",
                str(enc),
                str(plaintext),
            ],
            input=key.encode(),
            capture_output=True,
            env=env,
        )
        assert enc_proc.returncode == 0, enc_proc.stderr.decode()
        assert enc.exists()
        assert enc.read_bytes() != plaintext.read_bytes(), "ciphertext must differ from plaintext"

        # Correct key decrypts back to the original bytes.
        good = subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--decrypt",
                str(enc),
            ],
            input=key.encode(),
            capture_output=True,
            env=env,
        )
        assert good.returncode == 0, good.stderr.decode()
        assert good.stdout == plaintext.read_bytes()

        # Wrong key must fail.
        bad = subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--decrypt",
                str(enc),
            ],
            input=b"wrong key",
            capture_output=True,
            env=env,
        )
        assert bad.returncode != 0, "decryption with the wrong key must fail"


class TestRestoreScriptContent:
    """Verify restore.sh has required safety features."""

    def test_restore_requires_confirmation(self):
        content = _read_script("restore.sh")
        assert "RESTORE" in content and "confirm" in content, "restore.sh must require interactive confirmation"

    def test_restore_creates_safety_backup(self):
        content = _read_script("restore.sh")
        assert "pre_restore" in content, "restore.sh must create a safety backup before restoring"

    def test_restore_verifies_checksum(self):
        content = _read_script("restore.sh")
        assert "sha256sum" in content, "restore.sh must verify checksum"

    def test_restore_supports_list(self):
        content = _read_script("restore.sh")
        assert "--list" in content, "restore.sh must support --list flag"

    def test_restore_supports_verify(self):
        content = _read_script("restore.sh")
        assert "--verify" in content, "restore.sh must support --verify flag"

    def test_restore_terminates_connections(self):
        content = _read_script("restore.sh")
        assert "pg_terminate_backend" in content, "restore.sh must terminate existing connections before DROP"

    def test_restore_post_verification(self):
        content = _read_script("restore.sh")
        assert "RESTORE COMPLETE" in content, "restore.sh must verify and report after restore"

    def test_restore_decrypts_encrypted_backups(self):
        """restore.sh must transparently decrypt gpg-encrypted (.gpg) backups."""
        content = _read_script("restore.sh")
        assert "--decrypt" in content and "BACKUP_GPG_PASSPHRASE" in content, (
            "restore.sh must gpg --decrypt .gpg backups using BACKUP_GPG_PASSPHRASE"
        )
        assert ".gpg" in content, "restore.sh must recognise the .gpg encrypted-backup suffix"


class TestSpacesScriptContent:
    """Verify backup-to-spaces.sh handles missing config gracefully."""

    def test_spaces_skips_when_unconfigured(self):
        content = _read_script("backup-to-spaces.sh")
        assert "SKIP" in content or "not set" in content, "backup-to-spaces.sh must gracefully skip when not configured"

    def test_spaces_verifies_upload_size(self):
        content = _read_script("backup-to-spaces.sh")
        assert "mismatch" in content.lower() or "REMOTE_SIZE" in content, (
            "backup-to-spaces.sh must verify uploaded file size"
        )

    def test_spaces_has_remote_rotation(self):
        content = _read_script("backup-to-spaces.sh")
        assert "SPACES_RETENTION_DAYS" in content, "backup-to-spaces.sh must support remote rotation"

    def test_spaces_uses_server_side_encryption(self):
        """Off-site uploads must request server-side encryption (SSE-S3)."""
        content = _read_script("backup-to-spaces.sh")
        assert "--sse" in content, "backup-to-spaces.sh must request server-side encryption (--sse)"
        assert "AES256" in content, "backup-to-spaces.sh must default to AES256 server-side encryption"

    def test_spaces_sse_is_overridable(self):
        """SSE must be overridable (e.g. SPACES_SSE=none) so it degrades gracefully."""
        content = _read_script("backup-to-spaces.sh")
        assert "SPACES_SSE" in content, "backup-to-spaces.sh must expose SPACES_SSE for graceful override"
        assert '"none"' in content or "none" in content, (
            "backup-to-spaces.sh must let SPACES_SSE=none disable server-side encryption"
        )


class TestDockerComposeBackup:
    """Verify docker-compose.yml includes the backup service."""

    def _read_compose(self):
        compose_path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(compose_path) as f:
            return f.read()

    def _backup_section(self):
        """The db-backup service block (500 chars from its key)."""
        content = self._read_compose()
        idx = content.index("db-backup:")
        return content[idx : idx + 500]

    def test_backup_service_defined(self):
        content = self._read_compose()
        assert "db-backup:" in content, "docker-compose.yml must define db-backup service"

    def test_backup_volume_defined(self):
        content = self._read_compose()
        assert "pgbackups:" in content, "docker-compose.yml must define pgbackups volume"

    def test_backup_depends_on_db(self):
        section = self._backup_section()
        assert "db:" in section and "service_healthy" in section, "db-backup must depend on db being healthy"

    def test_backup_mounts_scripts(self):
        section = self._backup_section()
        assert "./scripts:/scripts:ro" in section, "db-backup must mount scripts as read-only"

    def test_backup_mounts_backup_volume(self):
        section = self._backup_section()
        assert "pgbackups:/backups" in section, "db-backup must mount pgbackups volume"
