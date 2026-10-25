"""
Booklet_AI Desktop — COM Supervisor

Process-level isolation for InDesign COM automation. All COM/InDesign
work runs in a dedicated child process (via multiprocessing), never a
thread within the main application process.

Architecture (STEP-1.1 §3):
- COMSupervisor launches a child process for each job.
- The child performs its own CoInitialize/CoUninitialize lifecycle.
- Communication is via COMJobRequest (primitive fields only).
- A heartbeat timestamp is monitored; stale heartbeat → kill child
  AND the owned InDesign.exe process.
- Pre-existing InDesign instances are NOT auto-killed on timeout.

Windows frozen-executable requirement: multiprocessing.freeze_support()
must be called in the main entry script (STEP-4.1).
"""

import json
import logging
import multiprocessing
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.automation.job_protocol import COMJobRequest
from app.core.exceptions import COMOperationTimeout, InDesignUnavailableError

logger = logging.getLogger(__name__)


class COMSupervisor:
    """Supervisor for COM worker child processes.

    Launches a child process for each job, monitors its heartbeat,
    and performs two-stage cleanup (Python child + InDesign.exe) on
    timeout. After cleanup, a fresh child process is used for the
    next job — failed jobs are never silently retried.

    Attributes:
        heartbeat_timeout: Seconds before a stale heartbeat triggers
                           termination (default: 30.0).
    """

    def __init__(self, heartbeat_timeout_seconds: float = 30.0) -> None:
        self.heartbeat_timeout = heartbeat_timeout_seconds
        self.process: Optional[multiprocessing.Process] = None

        # Shared with the child process via multiprocessing.Value:
        self.heartbeat = multiprocessing.Value("d", 0.0)

        # Records the InDesign.exe PID if this job launched a new
        # instance. 0 = not recorded / attached to pre-existing instance.
        self.indesign_pid = multiprocessing.Value("i", 0)
        self.last_preflight_report = None

    def run_job(self, job_request: COMJobRequest) -> Any:
        """Execute a COM job in an isolated child process.

        Blocks until the job completes or the heartbeat times out.
        On timeout, terminates the child and owned InDesign process,
        then raises COMOperationTimeout.

        Args:
            job_request: The COMJobRequest specifying the command and
                         payload file path.

        Raises:
            COMOperationTimeout: If the worker's heartbeat goes stale.
        """
        self.heartbeat.value = time.time()
        self.indesign_pid.value = 0

        self.process = multiprocessing.Process(
            target=_com_worker_entrypoint,
            args=(self.heartbeat, self.indesign_pid, job_request),
            daemon=True,
        )
        self.process.start()
        logger.info(
            "COM worker process started (PID=%d) for command '%s'",
            self.process.pid,
            job_request.job_command,
        )

        try:
            while self.process.is_alive():
                elapsed = time.time() - self.heartbeat.value
                if elapsed > self.heartbeat_timeout:
                    cleanup_state = self._terminate_and_cleanup()
                    # STEP-6.2: attach frozen SupervisorRetryState to
                    # the exception so is_auto_retryable() can evaluate
                    # whether a retry is safe. Local import to avoid
                    # top-level circular dependency.
                    from app.utils.auto_recovery import SupervisorRetryState
                    _state = SupervisorRetryState(**cleanup_state)
                    _exc = COMOperationTimeout(
                        f"COM worker process unresponsive for "
                        f"{self.heartbeat_timeout}s — likely a blocked "
                        f"modal dialog or unresponsive InDesign instance. "
                        f"Worker and owned InDesign process terminated."
                    )
                    _exc.supervisor_state = _state  # type: ignore[attr-defined]
                    raise _exc
                time.sleep(1.0)

            self.process.join()
            logger.info("COM worker process completed (exit code: %s)", self.process.exitcode)

            if self.process.exitcode != 0:
                # STEP-6.2: call cleanup to handle InDesign process,
                # then attach state to the exception.
                cleanup_state = self._terminate_and_cleanup()
                from app.utils.auto_recovery import SupervisorRetryState
                _state = SupervisorRetryState(**cleanup_state)
                _exc = COMOperationTimeout(
                    f"COM worker process exited with non-zero code: "
                    f"{self.process.exitcode}"
                )
                _exc.supervisor_state = _state  # type: ignore[attr-defined]
                raise _exc

            # STEP-6.4 (AC-6.4.4b): Read and parse post-generation PreflightReport if requested
            if job_request.preflight_output_path and Path(job_request.preflight_output_path).exists():
                from app.core.post_generation_preflight import read_post_generation_report
                from app.core.preflight import PreflightReport, PreflightIssue
                parsed = read_post_generation_report(job_request.preflight_output_path)
                report = PreflightReport(
                    issues=[PreflightIssue(**iss) for iss in parsed["issues"]],
                    stage=parsed["stage"],
                )
                self.last_preflight_report = report
                return report

            return None
        except COMOperationTimeout:
            raise
        except Exception as exc:
            logger.error("Unexpected error monitoring COM worker: %s", exc)
            self._terminate_and_cleanup()
            raise

    def _terminate_and_cleanup(self) -> dict:
        """Two-stage cleanup: kill the Python child, THEN separately
        verify and kill the InDesign process it owned.

        Killing the Python child alone is NOT sufficient recovery —
        InDesign.exe is a separate out-of-process COM server that
        remains running (and possibly hung) after the Python client dies.

        Returns:
            dict with cleanup state facts for SupervisorRetryState
            construction (STEP-6.2). These facts were previously
            computed and discarded after logging — now captured for
            the auto-retry decision in run_job_with_retry().
        """
        owned_pid = self.indesign_pid.value
        child_process_exited = False
        owned_indesign_confirmed_terminated = False
        attached_to_pre_existing = (owned_pid == 0)

        # Stage 1: Terminate the Python child process
        if self.process is not None and self.process.is_alive():
            logger.warning("Terminating COM worker process (PID=%d)", self.process.pid)
            self.process.terminate()
            self.process.join(timeout=5.0)
            if self.process.is_alive():
                logger.warning("Worker did not terminate gracefully, killing...")
                self.process.kill()
                self.process.join(timeout=5.0)

        child_process_exited = (self.process is None or not self.process.is_alive())

        # Stage 2: Terminate the owned InDesign.exe process
        if owned_pid != 0:
            try:
                import psutil

                indesign_proc = psutil.Process(owned_pid)
                if indesign_proc.name().lower().startswith("indesign"):
                    logger.warning(
                        "Terminating owned InDesign.exe process (PID=%d)",
                        owned_pid,
                    )
                    indesign_proc.kill()
                    indesign_proc.wait(timeout=10.0)
                    owned_indesign_confirmed_terminated = True
                    logger.info("InDesign.exe process terminated successfully.")
                else:
                    logger.warning(
                        "PID %d is not InDesign (name='%s'), skipping kill.",
                        owned_pid,
                        indesign_proc.name(),
                    )
            except Exception as exc:
                # psutil.NoSuchProcess or other — process already gone
                owned_indesign_confirmed_terminated = True
                logger.info(
                    "InDesign process (PID=%d) already terminated: %s",
                    owned_pid,
                    exc,
                )
        else:
            # indesign_pid was 0: job attached to a pre-existing InDesign
            # instance that was NOT launched by this application.
            # We do NOT kill it — it may be the user's own session.
            logger.warning(
                "No owned InDesign PID recorded — a pre-existing InDesign "
                "instance may still be running. Manual verification required."
            )

        cleanup_state = {
            "child_process_exited": child_process_exited,
            "owned_indesign_pid": owned_pid,
            "owned_indesign_confirmed_terminated": owned_indesign_confirmed_terminated,
            "attached_to_pre_existing_instance": attached_to_pre_existing,
            "partial_output_path": None,
        }
        logger.info("Cleanup state: %s", cleanup_state)
        return cleanup_state

    def confirms_clean_retryable_state(self) -> bool:
        """Check whether the system is in a clean state for retry.

        Used by STEP-6.2's run_job_with_retry() to determine if
        a single automatic retry is safe after a COMOperationTimeout.

        Returns:
            True if no worker process is running AND no owned InDesign
            process remains, indicating a clean state for retry.
        """
        # Check worker process
        if self.process is not None and self.process.is_alive():
            return False

        # Check owned InDesign
        owned_pid = self.indesign_pid.value
        if owned_pid != 0:
            try:
                import psutil

                proc = psutil.Process(owned_pid)
                if proc.is_running() and proc.name().lower().startswith("indesign"):
                    return False
            except Exception:
                pass  # Process gone — clean

        return True


# ─── Child Process Entrypoint ──────────────────────────────────────────


def _com_worker_entrypoint(
    heartbeat: multiprocessing.Value,
    indesign_pid: multiprocessing.Value,
    job_request: COMJobRequest,
) -> None:
    """Runs entirely inside the child process.

    Its own CoInitialize/CoUninitialize lifecycle. Dispatches
    job_request.job_command via a fixed top-level registry — never
    a transmitted function object.

    Before connecting to InDesign, checks whether an instance is
    already running (pre-existing, not owned by us) versus needing
    to be launched (owned by this job, PID recorded for cleanup).
    """
    import pythoncom
    import psutil
    import win32com.client

    pythoncom.CoInitialize()
    try:
        heartbeat.value = time.time()

        # Check for pre-existing InDesign instances
        pre_existing = any(
            p.name().lower().startswith("indesign")
            for p in psutil.process_iter(["name"])
        )

        # Connect to / launch InDesign
        app = win32com.client.Dispatch("InDesign.Application")

        # Suppress all modal dialogs — prevents headless COM hangs
        # (see STEP-1.1 §3: Modal Dialog Deadlock Prevention)
        app.ScriptPreferences.UserInteractionLevel = 1699640946  # NEVER_INTERACT

        heartbeat.value = time.time()

        if not pre_existing:
            # We launched this InDesign instance — record its PID
            for p in psutil.process_iter(["name", "pid"]):
                if p.info["name"] and p.info["name"].lower().startswith("indesign"):
                    indesign_pid.value = p.info["pid"]
                    break

        # Dispatch to the registered handler
        handler = JOB_REGISTRY.get(job_request.job_command)
        if handler is None:
            raise ValueError(
                f"Unknown job command: {job_request.job_command}. "
                f"Valid commands: {list(JOB_REGISTRY.keys())}"
            )
        handler(app, job_request.payload_file_path, heartbeat)

        # STEP-6.4: Execute post-generation preflight checks inside the child process
        if job_request.preflight_output_path:
            from app.core.post_generation_preflight import run_post_generation_checks
            from app.core.preflight import get_required_fonts
            doc = None
            try:
                if getattr(app, "Documents", None) and app.Documents.Count > 0:
                    doc = app.ActiveDocument
            except Exception as e:
                logger.warning("Could not access InDesign document for post-gen preflight: %s", e)

            run_post_generation_checks(
                indesign_app=app,
                indesign_doc=doc,
                required_fonts=get_required_fonts(),
                preflight_output_path=job_request.preflight_output_path,
            )

    finally:
        pythoncom.CoUninitialize()


# ─── Job Handler Stubs ─────────────────────────────────────────────────
# These are placeholder implementations. Full implementations will be
# added in subsequent STEPs (STEP-2.1+). They are top-level functions
# so that job_command (a string) selects the function via dict lookup —
# no function object ever crosses the process boundary.


def _run_generate_booklet(
    app: Any,
    payload_file_path: str,
    heartbeat: multiprocessing.Value,
) -> None:
    """Generate a complete booklet in InDesign from the payload.

    Full implementation in STEP-2.1 / STEP-2.2 / STEP-2.3.
    """
    heartbeat.value = time.time()
    logger.info("generate_booklet: reading payload from %s", payload_file_path)

    # Placeholder: verify the payload file exists and is valid JSON
    payload_path = Path(payload_file_path)
    if not payload_path.exists():
        raise FileNotFoundError(f"Payload file not found: {payload_file_path}")

    with open(payload_path, "r", encoding="utf-8") as f:
        _payload = json.load(f)

    heartbeat.value = time.time()
    logger.info("generate_booklet: payload loaded successfully (stub — full impl in STEP-2.x)")


def _run_add_filler_pages(
    app: Any,
    payload_file_path: str,
    heartbeat: multiprocessing.Value,
) -> None:
    """Add blank filler pages to meet binding requirements.

    Full implementation in STEP-2.1.
    """
    heartbeat.value = time.time()
    logger.info("add_filler_pages: stub — full implementation in STEP-2.1")


def _run_verify_indesign(
    app: Any,
    payload_file_path: str,
    heartbeat: multiprocessing.Value,
) -> None:
    """Verify InDesign is available and responsive via COM.

    Full implementation in STEP-4.1.
    """
    heartbeat.value = time.time()

    # Live round-trip verification: check Name and Version
    try:
        name = getattr(app, "Name", None)
        if name is None:
            # Direct COM property access
            name = app.Name
        version = app.Version
        logger.info("verify_indesign: InDesign %s (version %s) is available.", name, version)
    except Exception as exc:
        raise InDesignUnavailableError(
            "Adobe InDesign is not installed or not reachable via COM. "
            "Booklet_AI requires a licensed InDesign installation on this machine."
        ) from exc

    heartbeat.value = time.time()


# ─── Fixed, top-level registry ─────────────────────────────────────────
# job_command (a string) selects the function; no function object ever
# crosses the process boundary.

JOB_REGISTRY: Dict[str, Callable] = {
    "generate_booklet": _run_generate_booklet,
    "add_filler_pages": _run_add_filler_pages,
    "verify_indesign": _run_verify_indesign,
}
