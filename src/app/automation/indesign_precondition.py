"""
Booklet_AI Desktop — InDesign Startup Precondition Check (STEP-4.1)

Fail-Fast precondition check, run once before any document generation job starts —
not discovered mid-job. Attempts a lightweight COM handshake.
"""
import logging
from typing import Any, Optional

from app.core.exceptions import BookletError, InDesignUnavailableError

logger = logging.getLogger("booklet_ai.indesign_precondition")


def verify_indesign_available(app_dispatch: Optional[Any] = None) -> None:
    """Fail-Fast precondition check, run once before any generation job starts —

    not discovered mid-job. Attempts a lightweight COM handshake.

    Args:
        app_dispatch: Optional override for testing / dependency injection.
            If None, dispatches 'InDesign.Application' via win32com.client.

    Raises:
        InDesignUnavailableError: If InDesign is not installed, not licensed,
            or not reachable via COM.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        if app_dispatch is not None:
            app = app_dispatch
        else:
            app = win32com.client.Dispatch("InDesign.Application")
        _ = app.Name  # forces a live round-trip, not just object creation
        logger.info("InDesign COM handshake succeeded: %s", getattr(app, "Name", "InDesign"))
    except Exception as exc:
        raise InDesignUnavailableError(
            "Adobe InDesign is not installed or not reachable via COM. "
            "Booklet_AI requires a licensed InDesign installation on this machine."
        ) from exc
    finally:
        pythoncom.CoUninitialize()
