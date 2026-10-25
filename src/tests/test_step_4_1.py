"""
Booklet_AI Desktop — STEP-4.1 Test Suite
PyInstaller Build & Update Pipeline Acceptance Criteria Verification

Tests cover:
- [AC-4.1.1] Discarding update packages whose computed SHA-256 does not match manifest.
- [AC-4.1.2] Update check failures (network, malformed manifest) never raise or block.
- [AC-4.1.3] verify_indesign_available() raises InDesignUnavailableError before job start.
- [AC-4.1.4] PyInstaller --onedir build packaging, assets bundling, and Authenticode signing pipeline.
"""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.automation.com_supervisor import _run_verify_indesign
from app.automation.indesign_precondition import (
    InDesignUnavailableError,
    verify_indesign_available,
)
from app.core.exceptions import BookletError
from app.utils.updater import (
    UpdateChecker,
    VersionManifest,
    get_default_staging_base_dir,
)
from build_pipeline import (
    generate_release_manifest,
    locate_signtool,
    package_distribution,
    sign_binary,
)
from main import startup_check_indesign, startup_check_updates


# ═══════════════════════════════════════════════════════════════════════
# 1. VersionManifest Model Tests
# ═══════════════════════════════════════════════════════════════════════


class TestVersionManifest:
    """Tests for VersionManifest data model."""

    def test_valid_version_manifest(self):
        valid_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        manifest = VersionManifest(
            version="1.1.0",
            download_url="https://updates.example.com/v1.1.0.zip",
            sha256=valid_sha,
            release_notes_url="https://updates.example.com/notes.html",
        )
        assert manifest.version == "1.1.0"
        assert manifest.download_url == "https://updates.example.com/v1.1.0.zip"
        assert manifest.sha256 == valid_sha
        assert manifest.release_notes_url == "https://updates.example.com/notes.html"

    def test_version_manifest_immutability(self):
        valid_sha = "a" * 64
        manifest = VersionManifest(
            version="1.0.0",
            download_url="https://example.com/pkg.zip",
            sha256=valid_sha,
        )
        with pytest.raises(ValidationError):
            manifest.version = "2.0.0"

    def test_invalid_sha256_length(self):
        with pytest.raises(ValidationError):
            VersionManifest(
                version="1.0.0",
                download_url="https://example.com/pkg.zip",
                sha256="tooshort",
            )

    def test_invalid_sha256_characters(self):
        with pytest.raises(ValidationError):
            VersionManifest(
                version="1.0.0",
                download_url="https://example.com/pkg.zip",
                sha256="z" * 64,  # 'z' is not a valid hex digit
            )

    def test_empty_version_fails(self):
        with pytest.raises(ValidationError):
            VersionManifest(
                version="",
                download_url="https://example.com/pkg.zip",
                sha256="a" * 64,
            )

    def test_empty_download_url_fails(self):
        with pytest.raises(ValidationError):
            VersionManifest(
                version="1.0.0",
                download_url="   ",
                sha256="a" * 64,
            )

    def test_sha256_normalized_to_lowercase(self):
        upper_sha = "A" * 64
        manifest = VersionManifest(
            version="1.0.0",
            download_url="https://example.com/pkg.zip",
            sha256=upper_sha,
        )
        assert manifest.sha256 == "a" * 64


# ═══════════════════════════════════════════════════════════════════════
# 2. UpdateChecker: Non-blocking check tests [AC-4.1.2]
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateCheckerCheck:
    """Verifies that update checks never raise or block application usage [AC-4.1.2]."""

    def test_check_returns_manifest_when_newer_version(self):
        manifest_data = {
            "version": "1.2.0",
            "download_url": "https://example.com/update-1.2.0.zip",
            "sha256": "b" * 64,
            "release_notes_url": "https://example.com/notes",
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = manifest_data
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)

        assert result is not None
        assert result.version == "1.2.0"
        assert result.sha256 == "b" * 64

    def test_check_returns_none_when_same_version(self):
        manifest_data = {
            "version": "1.0.0",
            "download_url": "https://example.com/update-1.0.0.zip",
            "sha256": "b" * 64,
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = manifest_data
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)
        assert result is None

    def test_check_network_connect_error_returns_none_silently(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        # Must NOT raise exception (AC-4.1.2)
        result = checker.check(client=mock_client)
        assert result is None

    def test_check_network_timeout_returns_none_silently(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.TimeoutException("Read timed out")

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)
        assert result is None

    def test_check_http_404_error_returns_none_silently(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=mock_resp
        )
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)
        assert result is None

    def test_check_malformed_json_returns_none_silently(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>404</html>", 0)
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)
        assert result is None

    def test_check_invalid_manifest_schema_returns_none_silently(self):
        # Missing required sha256 and download_url
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"version": "2.0.0"}
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", current_version="1.0.0")
        result = checker.check(client=mock_client)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# 3. Update Staging & Verification Tests [AC-4.1.1]
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateStagingAndVerification:
    """Verifies SHA-256 verification and discard behavior [AC-4.1.1]."""

    @pytest.fixture
    def temp_staging_dir(self):
        tmp = tempfile.mkdtemp(prefix="test_staging_")
        yield Path(tmp)
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_download_exact_match(self):
        payload = b"Booklet_AI update binary content"
        expected_sha = hashlib.sha256(payload).hexdigest()
        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        assert checker.verify_download(payload, expected_sha) is True

    def test_verify_download_case_insensitive(self):
        payload = b"Booklet_AI update binary content"
        expected_sha = hashlib.sha256(payload).hexdigest().upper()
        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        assert checker.verify_download(payload, expected_sha) is True

    def test_verify_download_tampered_payload_returns_false(self):
        payload = b"Legitimate update content"
        expected_sha = hashlib.sha256(payload).hexdigest()
        tampered_payload = b"Malicious or corrupted content"
        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        assert checker.verify_download(tampered_payload, expected_sha) is False

    def test_verify_download_empty_bytes_returns_false(self):
        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        assert checker.verify_download(b"", "a" * 64) is False

    def test_stage_update_with_valid_hash_succeeds(self, temp_staging_dir):
        payload = b"Valid update package ZIP content"
        valid_sha = hashlib.sha256(payload).hexdigest()
        manifest = VersionManifest(
            version="1.2.0",
            download_url="https://example.com/update.zip",
            sha256=valid_sha,
        )

        checker = UpdateChecker(
            "https://example.com/manifest.json",
            "1.0.0",
            staging_base_dir=temp_staging_dir,
        )
        staged_path = checker.stage_update(payload, manifest)

        assert staged_path is not None
        assert staged_path.exists()
        package_file = staged_path / "update_package.zip"
        assert package_file.exists()
        assert package_file.read_bytes() == payload

        manifest_file = staged_path / "manifest.json"
        assert manifest_file.exists()
        staged_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert staged_manifest["version"] == "1.2.0"
        assert staged_manifest["sha256"] == valid_sha

    def test_stage_update_mismatched_hash_discarded_and_never_staged_ac_4_1_1(
        self, temp_staging_dir
    ):
        """[AC-4.1.1] A downloaded update package whose computed SHA-256 does not match

        the manifest's sha256 field is discarded and never staged for install.
        """
        legitimate_sha = "c" * 64
        corrupted_payload = b"Corrupted bytes with non-matching hash"

        manifest = VersionManifest(
            version="1.2.0",
            download_url="https://example.com/update.zip",
            sha256=legitimate_sha,
        )

        target_stage_dir = temp_staging_dir / "1.2.0"
        checker = UpdateChecker(
            "https://example.com/manifest.json",
            "1.0.0",
            staging_base_dir=temp_staging_dir,
        )

        # Attempt to stage corrupted payload
        result = checker.stage_update(
            corrupted_payload, manifest, staging_dir=target_stage_dir
        )

        # Verification: must return None
        assert result is None
        # Verification: staging directory must NOT have been created or populated with package
        assert not (target_stage_dir / "update_package.zip").exists()

    def test_download_package_mismatched_hash_returns_none(self):
        corrupted_payload = b"Tampered data received over network"
        expected_sha = "d" * 64

        manifest = VersionManifest(
            version="1.3.0",
            download_url="https://example.com/update.zip",
            sha256=expected_sha,
        )

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = corrupted_payload
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        result = checker.download_package(manifest, client=mock_client)

        # Must discard and return None
        assert result is None

    def test_download_and_stage_end_to_end_mismatch_never_staged(self, temp_staging_dir):
        """End-to-end download_and_stage with tampered content discards everything."""
        corrupted_payload = b"Mismatched hash download bytes"
        expected_sha = "e" * 64

        manifest = VersionManifest(
            version="1.4.0",
            download_url="https://example.com/pkg.zip",
            sha256=expected_sha,
        )

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = corrupted_payload
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_resp

        target_stage_dir = temp_staging_dir / "1.4.0"
        checker = UpdateChecker(
            "https://example.com/manifest.json",
            "1.0.0",
            staging_base_dir=temp_staging_dir,
        )
        result = checker.download_and_stage(
            manifest, staging_dir=target_stage_dir, client=mock_client
        )

        assert result is None
        assert not target_stage_dir.exists()

    def test_generate_swap_script(self, temp_staging_dir):
        staging_dir = temp_staging_dir / "stage"
        staging_dir.mkdir(parents=True)
        target_dir = temp_staging_dir / "install"
        target_dir.mkdir(parents=True)

        checker = UpdateChecker("https://example.com/manifest.json", "1.0.0")
        script_path = checker.generate_swap_script(
            staging_dir=staging_dir,
            target_install_dir=target_dir,
            exe_name="Booklet_AI.exe",
            parent_pid=1234,
        )

        assert script_path.exists()
        content = script_path.read_text(encoding="utf-8")
        assert "Booklet_AI.exe" in content
        assert "robocopy" in content
        assert "1234" in content


# ═══════════════════════════════════════════════════════════════════════
# 4. InDesign Precondition Check Tests [AC-4.1.3]
# ═══════════════════════════════════════════════════════════════════════


class TestInDesignPrecondition:
    """Verifies verify_indesign_available() Fail-Fast startup check [AC-4.1.3]."""

    def test_verify_indesign_available_success(self):
        """When InDesign COM responds with Name, verification succeeds without error."""
        mock_app = MagicMock()
        mock_app.Name = "Adobe InDesign"

        with patch("pythoncom.CoInitialize") as mock_init, patch(
            "pythoncom.CoUninitialize"
        ) as mock_uninit, patch("win32com.client.Dispatch", return_value=mock_app):

            # Should complete cleanly without raising
            verify_indesign_available()
            assert mock_init.called
            assert mock_uninit.called

    def test_verify_indesign_available_dispatch_failure_raises_ac_4_1_3(self):
        """[AC-4.1.3] When InDesign is not installed or Dispatch fails, raises InDesignUnavailableError."""
        with patch("pythoncom.CoInitialize") as mock_init, patch(
            "pythoncom.CoUninitialize"
        ) as mock_uninit, patch(
            "win32com.client.Dispatch", side_effect=Exception("Class not registered")
        ):

            with pytest.raises(InDesignUnavailableError) as exc_info:
                verify_indesign_available()

            assert "Adobe InDesign is not installed or not reachable via COM" in str(
                exc_info.value
            )
            # Must ensure CoUninitialize was called in finally
            assert mock_init.called
            assert mock_uninit.called

    def test_verify_indesign_available_property_round_trip_failure_raises(self):
        """When Dispatch creates an object but app.Name raises (hung/stale instance), raises InDesignUnavailableError."""
        mock_app = MagicMock()
        type(mock_app).Name = PropertyMock(side_effect=RuntimeError("Property access failed"))

        with patch("pythoncom.CoInitialize"), patch(
            "pythoncom.CoUninitialize"
        ) as mock_uninit, patch("win32com.client.Dispatch", return_value=mock_app):

            with pytest.raises(InDesignUnavailableError):
                verify_indesign_available()

            assert mock_uninit.called

    def test_indesign_unavailable_error_is_booklet_error(self):
        """Verify exception inheritance matches project architecture."""
        assert issubclass(InDesignUnavailableError, BookletError)

    def test_com_supervisor_verify_indesign_worker_round_trip(self):
        """Verifies _run_verify_indesign in com_supervisor checks Name and Version."""
        mock_app = MagicMock()
        mock_app.Name = "Adobe InDesign"
        mock_app.Version = "19.0"
        mock_heartbeat = MagicMock()

        _run_verify_indesign(mock_app, "/tmp/unused.json", mock_heartbeat)
        assert mock_heartbeat.value is not None

    def test_com_supervisor_verify_indesign_worker_failure_raises(self):
        mock_app = MagicMock()
        type(mock_app).Name = PropertyMock(side_effect=RuntimeError("Property access failed"))
        mock_heartbeat = MagicMock()

        with pytest.raises(InDesignUnavailableError):
            _run_verify_indesign(mock_app, "/tmp/unused.json", mock_heartbeat)

    def test_startup_check_indesign_fail_fast_flag(self):
        with patch(
            "main.verify_indesign_available",
            side_effect=InDesignUnavailableError("Not installed"),
        ):
            # fail_fast=True raises
            with pytest.raises(InDesignUnavailableError):
                startup_check_indesign(fail_fast=True)

            # fail_fast=False returns False silently
            result = startup_check_indesign(fail_fast=False)
            assert result is False


# ═══════════════════════════════════════════════════════════════════════
# 5. PyInstaller Packaging & Build Pipeline Tests [AC-4.1.4]
# ═══════════════════════════════════════════════════════════════════════


class TestPackagingAndBuildPipeline:
    """Verifies PyInstaller spec configuration and build pipeline integrity [AC-4.1.4]."""

    def test_spec_file_onedir_configuration(self):
        """PyInstaller spec must specify --onedir mode (exclude_binaries=True in EXE and COLLECT block)."""
        spec_path = Path(__file__).resolve().parent.parent / "booklet_ai.spec"
        assert spec_path.exists(), f"Spec file missing at {spec_path}"

        content = spec_path.read_text(encoding="utf-8")

        # Must have exclude_binaries=True in EXE
        assert "exclude_binaries=True" in content
        # Must have COLLECT section producing the --onedir directory bundle
        assert "coll = COLLECT(" in content
        # Must bundle assets and migrations
        assert "assets" in content
        assert "migrations" in content
        # Must bundle sqlite-vec
        assert "sqlite_vec" in content
        # UPX should be false for antivirus false-positive avoidance
        assert "upx=False" in content

    def test_main_entrypoint_freeze_support(self):
        """main.py must include multiprocessing.freeze_support() guarded by __main__."""
        main_path = Path(__file__).resolve().parent.parent / "main.py"
        assert main_path.exists(), f"main.py missing at {main_path}"

        content = main_path.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in content
        assert "multiprocessing.freeze_support()" in content

    def test_sign_binary_without_cert_returns_false_and_logs(self, caplog):
        """Building without certificate logs unsigned development build notice."""
        dummy_exe = Path(tempfile.gettempdir()) / "test_unsigned.exe"
        dummy_exe.write_bytes(b"MZ\x90\x00")

        try:
            with patch("build_pipeline.locate_signtool", return_value=Path("signtool.exe")):
                # No cert and no thumbprint
                success = sign_binary(dummy_exe)
                assert success is False
        finally:
            if dummy_exe.exists():
                dummy_exe.unlink()

    def test_package_distribution_and_manifest_generation(self):
        """Verifies release package zip and manifest SHA-256 match exactly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create mock --onedir build directory
            dist_dir = tmp_path / "Booklet_AI"
            dist_dir.mkdir()
            (dist_dir / "Booklet_AI.exe").write_bytes(b"dummy executable content")
            (dist_dir / "data.txt").write_bytes(b"dummy data content")

            # Create zip package
            zip_target = tmp_path / "Booklet_AI_1.0.0.zip"
            zip_path, computed_sha = package_distribution(dist_dir, zip_target)

            assert zip_path.exists()
            assert computed_sha == hashlib.sha256(zip_path.read_bytes()).hexdigest()

            # Generate manifest
            manifest_file = tmp_path / "manifest.json"
            manifest = generate_release_manifest(
                version="1.0.0",
                download_url="https://releases.example.com/Booklet_AI_1.0.0.zip",
                package_sha256=computed_sha,
                release_notes_url="https://releases.example.com/notes.html",
                output_manifest_path=manifest_file,
            )

            assert manifest.sha256 == computed_sha
            assert manifest_file.exists()

            loaded_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            assert loaded_manifest["sha256"] == computed_sha
            assert loaded_manifest["version"] == "1.0.0"
