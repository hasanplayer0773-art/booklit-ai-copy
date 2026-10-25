# Automation: COM supervisor, InDesign bridge, JSX builders
from app.automation.batch_queue import (
    BatchJobResult,
    BatchQueue,
    BatchQueueSummary,
    JobScopedState,
    create_job_scoped_state,
    orchestrate_classification,
)
from app.automation.com_supervisor import COMSupervisor
from app.automation.indesign_precondition import (
    InDesignUnavailableError,
    verify_indesign_available,
)
from app.automation.job_retry import run_job_with_retry

__all__ = [
    "COMSupervisor",
    "InDesignUnavailableError",
    "verify_indesign_available",
    "run_job_with_retry",
    "BatchQueue",
    "JobScopedState",
    "create_job_scoped_state",
    "orchestrate_classification",
    "BatchQueueSummary",
    "BatchJobResult",
]
