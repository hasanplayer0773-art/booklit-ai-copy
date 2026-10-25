"""
Booklet_AI Desktop — Generation Pipeline (STEP-7.1)

Orchestrates the conversion of a BookletDocument (blocks with final block_type,
tables, PageGeometry, styles config) into a real InDesign document and PDF.

Key Responsibilities:
1. build_payload(): Verifies canonical SHA-256 hash of every content block
   before writing (Fail-Fast, TextIntegrityError). Writes the payload JSON
   with UTF-8 encoding (ensure_ascii=False) containing all document blocks,
   tables, page geometry, typography styles, and output paths.
2. generate_booklet() / submit_generate_booklet_job(): Submits a
   COMJobRequest("generate_booklet", payload_path) to COMSupervisor for
   isolated child-process execution.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.automation.com_supervisor import COMSupervisor
from app.automation.geometry import PageGeometry
from app.automation.job_protocol import COMJobRequest
from app.automation.pagination_sync import BindingMethod, imposed_page_count
from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import TextIntegrityError
from app.core.models import BookletDocument, ContentBlock
from app.core.text_integrity import verify_text_integrity

logger = logging.getLogger(__name__)


def build_payload(
    document: BookletDocument,
    geometry: PageGeometry,
    styles_cfg: TypographyStylesConfig,
    output_dir: Union[Path, str],
    binding: Union[BindingMethod, str] = "saddle_stitch",
) -> Path:
    """Build and write the JSON payload for generate_booklet.jsx.

    Performs Fail-Fast verification of canonical SHA-256 hash for every
    ContentBlock before writing any data to disk. If any block's content
    has been tampered with or modified without using with_user_edit(),
    TextIntegrityError is raised immediately.

    The binding method is passed to JSX so that imposition target count
    can be calculated dynamically from the actual InDesign page count
    after content injection.

    Args:
        document: The complete BookletDocument containing pages, blocks, and tables.
        geometry: PageGeometry configuration (A4, facing pages, margins, etc.).
        styles_cfg: TypographyStylesConfig (fonts, font sizes, leading, direction).
        output_dir: Directory where the payload JSON, .indd, and .pdf will be stored.
        binding: Binding method used to calculate imposed signature multiples.

    Returns:
        Path to the written payload JSON file.

    Raises:
        TextIntegrityError: If any block fails canonical SHA-256 hash verification.
    """
    # ── 1. Fail-Fast Text Integrity Verification ───────────────────────
    # Every block's raw_content must match its content_hash before writing.
    for page in document.pages:
        for block in page.content_blocks:
            verify_text_integrity(
                block.raw_content,
                block.content_hash,
                context=f"block_id={block.block_id}, page={page.page_number}",
            )

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # ── 2. Binding Configuration & Output Paths ───────────────────────
    binding_str = str(binding)

    doc_id = document.doc_id or "booklet"
    payload_file = out_path / f"{doc_id}_payload.json"
    indd_file = out_path / f"{doc_id}.indd"
    pdf_file = out_path / f"{doc_id}.pdf"
    result_file = out_path / f"{doc_id}_result.json"

    # ── 3. Serialize Blocks, Tables, Items, and Pages ──────────────────
    blocks_list: List[Dict[str, Any]] = []
    items_list: List[Dict[str, Any]] = []
    pages_list: List[Dict[str, Any]] = []
    tables_list: List[Dict[str, Any]] = []

    for page in document.pages:
        p_blocks: List[Dict[str, Any]] = []
        for b in page.content_blocks:
            b_dict: Dict[str, Any] = {
                "block_id": b.block_id,
                "block_type": b.block_type,
                "raw_content": b.raw_content,
                "content_hash": b.content_hash,
                "user_edited": b.user_edited,
                "source_position": b.source_position,
            }
            blocks_list.append(b_dict)
            p_blocks.append(b_dict)
            items_list.append({
                "item_type": "block",
                **b_dict,
            })

        p_tables: List[Dict[str, Any]] = []
        for t in page.tables:
            t_dict: Dict[str, Any] = {
                "table_id": t.table_id,
                "headers": list(t.headers),
                "rows": [list(row) for row in t.rows],
                "source_bbox": t.source_bbox,
            }
            tables_list.append(t_dict)
            p_tables.append(t_dict)
            items_list.append({
                "item_type": "table",
                **t_dict,
            })

        pages_list.append({
            "page_number": page.page_number,
            "width": page.width,
            "height": page.height,
            "header_text": page.header_text,
            "footer_text": page.footer_text,
            "blocks": p_blocks,
            "tables": p_tables,
        })

    payload_data: Dict[str, Any] = {
        "doc_id": doc_id,
        "file_path": document.file_path,
        "file_type": document.file_type,
        "output_dir": str(out_path.resolve()),
        "output_indd": str(indd_file.resolve()),
        "output_pdf": str(pdf_file.resolve()),
        "result_file_path": str(result_file.resolve()),
        "binding": binding_str,
        "geometry": geometry.to_jsx_payload(),
        "styles": styles_cfg.model_dump(),
        "blocks": blocks_list,
        "tables": tables_list,
        "items": items_list,
        "pages": pages_list,
    }

    # ── 4. Write Payload JSON (UTF-8, ensure_ascii=False) ──────────────
    with open(payload_file, "w", encoding="utf-8") as f:
        json.dump(payload_data, f, ensure_ascii=False, indent=2)

    logger.info("Payload written successfully to %s", payload_file)
    return payload_file


def submit_generate_booklet_job(
    payload_path: Union[Path, str],
    supervisor: Optional[COMSupervisor] = None,
    preflight_output_path: Optional[str] = None,
) -> Any:
    """Submit a generate_booklet job to InDesign via COMSupervisor.

    Args:
        payload_path: Path to the generated payload JSON file.
        supervisor: Optional COMSupervisor instance (creates new if None).
        preflight_output_path: Optional path for post-generation preflight report.

    Returns:
        Result from COMSupervisor.run_job (report, result dict, or None).
    """
    if supervisor is None:
        supervisor = COMSupervisor()

    job_request = COMJobRequest(
        job_command="generate_booklet",
        payload_file_path=str(Path(payload_path).resolve()),
        preflight_output_path=preflight_output_path,
    )
    return supervisor.run_job(job_request)


def generate_booklet(
    document: BookletDocument,
    geometry: PageGeometry,
    styles_cfg: TypographyStylesConfig,
    output_dir: Union[Path, str],
    supervisor: Optional[COMSupervisor] = None,
    binding: Union[BindingMethod, str] = "saddle_stitch",
    preflight_output_path: Optional[str] = None,
) -> Any:
    """End-to-end booklet generation entrypoint.

    Builds the payload with Fail-Fast hash checking, then submits the job
    to COMSupervisor.

    Args:
        document: BookletDocument to generate.
        geometry: PageGeometry specification.
        styles_cfg: Typography configuration.
        output_dir: Output directory for JSON, INDD, and PDF.
        supervisor: Optional COMSupervisor instance.
        binding: Binding method for page imposition.
        preflight_output_path: Optional post-generation preflight path.

    Returns:
        Result from supervisor.run_job.
    """
    payload_path = build_payload(
        document=document,
        geometry=geometry,
        styles_cfg=styles_cfg,
        output_dir=output_dir,
        binding=binding,
    )
    return submit_generate_booklet_job(
        payload_path=payload_path,
        supervisor=supervisor,
        preflight_output_path=preflight_output_path,
    )
