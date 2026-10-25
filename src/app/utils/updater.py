"""
Booklet_AI Desktop — Version Manifest & Update Pipeline (STEP-4.1)

Provides non-blocking update checking against a static JSON manifest,
SHA-256 verification of downloaded update artifacts, staging of verified
update packages, and relaunch swap scripts.

Technical Policies:
- Zero trust on downloaded update artifacts: computed SHA-256 must match
  manifest sha256 before staging (AC-4.1.1).
- Non-blocking: failures (network, invalid JSON, hash mismatch) are silently
  skipped and never raise errors to UI or block app usage (AC-4.1.2).
- Delta-free full replacement staged in temp directory.
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger("booklet_ai.updater")


class VersionManifest(BaseModel):
    """Declarative specification for an application update release."""

    model_config = ConfigDict(frozen=True)

    version: str
    download_url: str
    sha256: str
    release_notes_url: Optional[str] = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if len(cleaned) != 64 or not all(c in "0123456789abcdef" for c in cleaned):
            raise ValueError(f"Invalid SHA-256 hexadecimal hash: {v}")
        return cleaned

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Version cannot be empty.")
        return cleaned

    @field_validator("download_url")
    @classmethod
    def validate_download_url(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Download URL cannot be empty.")
        return cleaned


def get_default_staging_base_dir() -> Path:
    """Get the default directory for staging downloaded updates."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_dir = Path(local_app_data) / "Booklet_AI" / "updates" / "staging"
    else:
        base_dir = Path(tempfile.gettempdir()) / "Booklet_AI_updates_staging"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class UpdateChecker:
    """Non-blocking update checker and delivery pipeline.

    Any failure (network, malformed manifest, hash mismatch) is logged and
    silently skipped — update availability is never a precondition for using
    the application (AC-4.1.2).
    """

    def __init__(
        self,
        manifest_url: str,
        current_version: str,
        timeout: float = 5.0,
        staging_base_dir: Optional[Path] = None,
    ):
        self.manifest_url = manifest_url
        self.current_version = current_version.strip()
        self.timeout = timeout
        self.staging_base_dir = staging_base_dir or get_default_staging_base_dir()

    def check(self, client: Optional[httpx.Client] = None) -> Optional[VersionManifest]:
        """Check the remote manifest URL for available updates.

        Silently handles network failures, timeouts, HTTP errors, and malformed
        JSON. Returns VersionManifest if a new version is available, None otherwise.
        """
        try:
            if client is not None:
                resp = client.get(self.manifest_url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
            else:
                with httpx.Client(timeout=self.timeout) as default_client:
                    resp = default_client.get(self.manifest_url)
                    resp.raise_for_status()
                    data = resp.json()

            manifest = VersionManifest.model_validate(data)
            if manifest.version != self.current_version:
                logger.info(
                    "Update available: version %s (current: %s)",
                    manifest.version,
                    self.current_version,
                )
                return manifest
            return None
        except Exception as exc:
            logger.debug("Update check silently skipped: %s", exc)
            return None  # silent skip — see Technical Notes & AC-4.1.2

    def verify_download(self, file_bytes: bytes, expected_sha256: str) -> bool:
        """Verify that computed SHA-256 of file_bytes matches expected_sha256."""
        if not file_bytes:
            return False
        actual = hashlib.sha256(file_bytes).hexdigest()
        return actual.lower() == expected_sha256.strip().lower()

    def download_package(
        self,
        manifest: VersionManifest,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> Optional[bytes]:
        """Download update package from manifest download_url and verify its SHA-256 hash.

        If network fails, or if computed SHA-256 does NOT match manifest.sha256,
        the download is discarded and None is returned.
        """
        try:
            if client is not None:
                resp = client.get(manifest.download_url, timeout=timeout)
                resp.raise_for_status()
                content = resp.content
            else:
                with httpx.Client(timeout=timeout) as default_client:
                    resp = default_client.get(manifest.download_url)
                    resp.raise_for_status()
                    content = resp.content
        except Exception as exc:
            logger.warning("Failed to download update package: %s", exc)
            return None

        # Verify hash integrity immediately
        if not self.verify_download(content, manifest.sha256):
            logger.error(
                "Security alert [AC-4.1.1]: downloaded update package SHA-256 does not match manifest! "
                "Package discarded."
            )
            return None

        return content

    def stage_update(
        self,
        file_bytes: bytes,
        manifest: VersionManifest,
        staging_dir: Optional[Union[str, Path]] = None,
    ) -> Optional[Path]:
        """Stage a verified update package for install.

        CRITICAL [AC-4.1.1]: If computed SHA-256 does NOT match manifest.sha256,
        the package is discarded and NEVER staged for install.

        Returns:
            Path to staging directory on success, or None on failure.
        """
        # Strict pre-staging hash verification
        if not self.verify_download(file_bytes, manifest.sha256):
            logger.error(
                "Security rejection [AC-4.1.1]: update package hash mismatch! "
                "Refusing to stage update."
            )
            return None

        target_dir = Path(staging_dir) if staging_dir else (self.staging_base_dir / manifest.version)

        try:
            # Clear previous staging content to prevent partial-update corruption
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

            # Write package archive
            package_path = target_dir / "update_package.zip"
            package_path.write_bytes(file_bytes)

            # Write manifest metadata
            manifest_path = target_dir / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

            logger.info("Successfully staged update v%s at: %s", manifest.version, target_dir)
            return target_dir
        except Exception as exc:
            logger.error("Failed to stage update package: %s", exc)
            if target_dir.exists():
                try:
                    shutil.rmtree(target_dir)
                except Exception:
                    pass
            return None

    def download_and_stage(
        self,
        manifest: VersionManifest,
        staging_dir: Optional[Union[str, Path]] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> Optional[Path]:
        """Download, verify hash, and stage update package.

        Returns staging path if successful, None if download fails or hash mismatch.
        """
        package_bytes = self.download_package(manifest, client=client, timeout=timeout)
        if package_bytes is None:
            return None
        return self.stage_update(package_bytes, manifest, staging_dir=staging_dir)

    def discard_staging(self, staging_dir: Optional[Union[str, Path]] = None) -> None:
        """Safely discard any staged update files."""
        target_dir = Path(staging_dir) if staging_dir else self.staging_base_dir
        if target_dir.exists():
            try:
                shutil.rmtree(target_dir)
                logger.info("Discarded staging directory: %s", target_dir)
            except Exception as exc:
                logger.warning("Error discarding staging directory: %s", exc)

    def generate_swap_script(
        self,
        staging_dir: Path,
        target_install_dir: Path,
        exe_name: str = "Booklet_AI.exe",
        parent_pid: Optional[int] = None,
    ) -> Path:
        """Generate a Windows batch script to swap the --onedir package on relaunch.

        The script:
        1. Waits for parent process PID to terminate.
        2. Copies staged files over the target installation folder.
        3. Cleans up the staging directory.
        4. Relaunches the updated executable.
        """
        script_path = staging_dir / "apply_update.bat"
        pid_wait = (
            f'powershell -Command "while (Get-Process -Id {parent_pid} -ErrorAction SilentlyContinue) {{ Start-Sleep -Milliseconds 500 }}"'
            if parent_pid
            else "timeout /t 2 /nobreak >nul"
        )

        script_content = f"""@echo off
setlocal
echo Applying Booklet_AI update...

:: Wait for application process to exit
{pid_wait}

:: Copy staged files into installation directory (delta-free full replacement)
robocopy "{staging_dir}" "{target_install_dir}" /E /XD "{staging_dir}" /XF "apply_update.bat" "manifest.json" /R:3 /W:1 >nul

:: Clean up staging directory
rmdir /s /q "{staging_dir}" 2>nul

:: Relaunch updated application
start "" "{target_install_dir / exe_name}"
exit
"""
        script_path.write_text(script_content, encoding="utf-8")
        return script_path
