"""
Booklet_AI Desktop — Application Main Entrypoint (STEP-4.1)

PyInstaller frozen-executable entrypoint.
Guarantees multiprocessing.freeze_support() on Windows,
performs startup update checks, and verifies InDesign COM preconditions.
"""
import logging
import multiprocessing
import os
import sys
from typing import List, Optional

from app.automation.indesign_precondition import (
    InDesignUnavailableError,
    verify_indesign_available,
)
from app.utils.logging_config import setup_logging
from app.utils.updater import UpdateChecker, VersionManifest

logger = logging.getLogger("booklet_ai")

CURRENT_VERSION = "1.0.0"
DEFAULT_MANIFEST_URL = "https://updates.booklet-ai.example.com/manifest.json"


def startup_check_indesign(fail_fast: bool = True) -> bool:
    """Run the startup InDesign COM precondition check.

    Fail-Fast check before any document generation job starts (AC-4.1.3).

    Args:
        fail_fast: If True, raises InDesignUnavailableError if InDesign is not
            reachable. If False, logs and returns False.

    Returns:
        True if InDesign is reachable, False if not (when fail_fast=False).

    Raises:
        InDesignUnavailableError: If fail_fast is True and InDesign is unreachable.
    """
    try:
        verify_indesign_available()
        logger.info("Startup check: InDesign COM is reachable.")
        return True
    except InDesignUnavailableError as exc:
        logger.error("Startup InDesign check failed: %s", exc)
        if fail_fast:
            raise
        return False


def startup_check_updates(
    manifest_url: Optional[str] = None,
    current_version: str = CURRENT_VERSION,
) -> Optional[VersionManifest]:
    """Perform non-blocking update check at startup.

    Any error (network, malformed manifest, hash mismatch) is silently skipped
    and never raises to the UI or blocks normal application startup (AC-4.1.2).

    Returns:
        VersionManifest if an update is available, None otherwise.
    """
    url = manifest_url or os.environ.get("BOOKLET_AI_UPDATE_URL", DEFAULT_MANIFEST_URL)
    checker = UpdateChecker(manifest_url=url, current_version=current_version)
    manifest = checker.check()
    if manifest:
        logger.info(
            "New update available: %s (current: %s) -> %s",
            manifest.version,
            current_version,
            manifest.download_url,
        )
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    """Main application lifecycle entrypoint."""
    setup_logging()
    logger.info("Initializing Booklet_AI Desktop v%s...", CURRENT_VERSION)

    # 1. Non-blocking update check (AC-4.1.2)
    startup_check_updates()

    # 2. InDesign availability check (Fail-Fast policy, AC-4.1.3)
    # At initial application launch, we log status; before document generation jobs
    # startup_check_indesign(fail_fast=True) is enforced.
    try:
        startup_check_indesign(fail_fast=False)
    except Exception as exc:
        logger.warning("InDesign is not currently available: %s", exc)

    # 3. Initialize persisted UI language and translation system (AC-6.6.3)
    try:
        from app.state.app_store import AppStore
        from app.ui.i18n import get_translation_manager

        store = AppStore()
        tm = get_translation_manager()
        tm.bind_app_store(store)
    except Exception as exc:
        logger.debug("I18n startup initialization skipped: %s", exc)

    logger.info("Booklet_AI Desktop core initialized successfully.")
    return 0


if __name__ == "__main__":
    # MANDATORY: Windows frozen-executable requirement (STEP-1.1 §3, STEP-4.1 §1)
    # Must be called immediately inside `if __name__ == '__main__':` before any
    # worker process logic or UI loop runs.
    multiprocessing.freeze_support()
    sys.exit(main())
