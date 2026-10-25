"""
Booklet_AI Desktop — COM Job Protocol

Defines the strict IPC contract for communication between the main
application process and the COM worker child process.

COMJobRequest is the ONLY data structure that may cross the process
boundary into the COM worker. It contains no function objects, no Qt
objects, no COM references — just primitive fields that are fully
picklable under Windows' 'spawn' start method.

The job_command field selects a handler from a fixed top-level registry
inside the child process via dict lookup — the command name travels
across the boundary, not the function itself.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class COMJobRequest(BaseModel):
    """The ONLY data structure that may cross the process boundary
    into the COM worker.

    No function objects, no Qt objects, no COM references — job_command
    is dispatched via a fixed registry inside the child process, never
    transmitted as code.

    Attributes:
        job_command: One of the registered command names. The child
                     process dispatches this to a top-level function
                     via a plain dict lookup.
        payload_file_path: Absolute path to a JSON file containing
                           the job's input data. File-based IPC
                           avoids pickling complex object graphs.
        preflight_output_path: Where the child process writes its
                               PreflightReport (as JSON) after post-
                               generation checks complete (STEP-6.4).
                               A file-based handoff keeping the
                               "primitive values only across the process
                               boundary" rule intact.  Optional — only
                               used for generate_booklet jobs.
    """

    model_config = ConfigDict(frozen=True)

    job_command: Literal[
        "generate_booklet",
        "add_filler_pages",
        "verify_indesign",
    ]
    payload_file_path: str
    preflight_output_path: Optional[str] = None
