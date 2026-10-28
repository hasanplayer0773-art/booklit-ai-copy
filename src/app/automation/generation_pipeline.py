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
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel

from app.automation.com_supervisor import COMSupervisor
from app.automation.geometry import PageGeometry
from app.automation.job_protocol import COMJobRequest
from app.automation.pagination_sync import BindingMethod, imposed_page_count
from app.automation.styles_schema import TypographyStylesConfig
from app.core.exceptions import ImageAssetMissingError, TextIntegrityError
from app.core.math_renderer import extract_alt_text, render_mathml_to_svg
from app.core.models import BookletDocument, ContentBlock, ImageAsset
from app.core.text_integrity import verify_text_integrity

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Recursively convert Pydantic models, dicts, lists, and tuples into JSON-safe primitives."""
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump())
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def build_payload(
    document: BookletDocument,
    geometry: PageGeometry,
    styles_cfg: TypographyStylesConfig,
    output_dir: Union[Path, str],
    binding: Union[BindingMethod, str] = "saddle_stitch",
    footer_text: Optional[str] = None,
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

    math_dir = out_path / "math"
    math_size = getattr(styles_cfg, "math_size_pt", 12.0)
    math_rendered_count = 0
    math_failed_count = 0

    images_dir = out_path / "images"
    image_found_count = 0
    image_copied_count = 0

    for page in document.pages:
        p_blocks: List[Dict[str, Any]] = []
        for b in page.content_blocks:
            b_dict: Dict[str, Any] = {
                "block_id": b.block_id,
                "block_type": b.block_type,
                "raw_content": b.raw_content,
                "content_hash": b.content_hash,
                "user_edited": b.user_edited,
                "source_position": _json_safe(b.source_position),
            }

            if b.block_type == "math_formula":
                svg_file = math_dir / f"{b.block_id}.svg"
                try:
                    rendered_svg = render_mathml_to_svg(
                        mathml=b.raw_content,
                        out_path=svg_file,
                        size_pt=math_size,
                    )
                    b_dict["render_mode"] = "image"
                    b_dict["svg_path"] = str(rendered_svg.resolve())
                    b_dict["alt_text"] = extract_alt_text(b.raw_content)
                    math_rendered_count += 1
                except Exception as exc:
                    logger.warning("Math rendering failed for %s: %s", b.block_id, exc)
                    b_dict["render_mode"] = "placeholder"
                    b_dict["placeholder_text"] = "[معادلة تحتاج مراجعة]"
                    b_dict["alt_text"] = "equation"
                    fail_warning = f"MATH_RENDER_FAILED:{b.block_id}"
                    if fail_warning not in document.warnings:
                        document.warnings.append(fail_warning)
                    math_failed_count += 1

            sp = b.source_position if isinstance(b.source_position, dict) else {}
            asset = sp.get("image_asset")
            is_image_block = b.block_type == "image_prompt" or asset is not None

            if is_image_block:
                image_found_count += 1
                if asset is not None:
                    if isinstance(asset, ImageAsset):
                        src_path_str = asset.source_path
                        pixel_width = asset.pixel_width
                        pixel_height = asset.pixel_height
                        intended_width_mm = asset.intended_width_mm
                        intended_height_mm = asset.intended_height_mm
                    elif isinstance(asset, dict):
                        src_path_str = asset.get("source_path")
                        pixel_width = asset.get("pixel_width")
                        pixel_height = asset.get("pixel_height")
                        intended_width_mm = asset.get("intended_width_mm")
                        intended_height_mm = asset.get("intended_height_mm")
                    else:
                        src_path_str = getattr(asset, "source_path", None)
                        pixel_width = getattr(asset, "pixel_width", None)
                        pixel_height = getattr(asset, "pixel_height", None)
                        intended_width_mm = getattr(asset, "intended_width_mm", None)
                        intended_height_mm = getattr(asset, "intended_height_mm", None)
                else:
                    src_path_str = sp.get("source_path")
                    pixel_width = None
                    pixel_height = None
                    intended_width_mm = None
                    intended_height_mm = None

                src_file = Path(src_path_str) if src_path_str else None
                if src_file is None or not src_file.is_file():
                    raise ImageAssetMissingError(
                        block_id=b.block_id,
                        path=str(src_path_str or ""),
                    )

                images_dir.mkdir(parents=True, exist_ok=True)
                ext = src_file.suffix
                dest_file = (images_dir / f"{b.block_id}{ext}").resolve()
                shutil.copy2(src_file, dest_file)
                image_copied_count += 1

                b_dict["render_mode"] = "image"
                b_dict["image_path"] = str(dest_file)
                b_dict["pixel_width"] = pixel_width
                b_dict["pixel_height"] = pixel_height
                b_dict["intended_width_mm"] = intended_width_mm
                b_dict["intended_height_mm"] = intended_height_mm
                b_dict["alt_text"] = "image"

            blocks_list.append(b_dict)
            p_blocks.append(b_dict)

        p_tables: List[Dict[str, Any]] = []
        for t in page.tables:
            t_dict: Dict[str, Any] = {
                "table_id": t.table_id,
                "headers": list(t.headers),
                "rows": [list(row) for row in t.rows],
                "source_bbox": t.source_bbox,
                "body_index": t.body_index,
            }
            tables_list.append(t_dict)
            p_tables.append(t_dict)

        total_page_items = len(page.content_blocks) + len(page.tables)

        # 1. Merge and sort by (body_index, sub_index) when ALL items have body_index
        all_have_body_index = (
            total_page_items > 0
            and all(
                isinstance(b.source_position, dict)
                and b.source_position.get("body_index") is not None
                for b in page.content_blocks
            )
            and all(t.body_index is not None for t in page.tables)
        )

        def _get_block_y0(cb: ContentBlock) -> Optional[float]:
            sp = cb.source_position or {}
            bbox = sp.get("bbox") or sp.get("source_bbox")
            if bbox is not None and len(bbox) >= 2:
                try:
                    return float(bbox[1])
                except (ValueError, TypeError):
                    return None
            return None

        def _get_table_y0(td: TableData) -> Optional[float]:
            bbox = td.source_bbox
            if bbox is not None and len(bbox) >= 2:
                try:
                    return float(bbox[1])
                except (ValueError, TypeError):
                    return None
            return None

        all_have_y0 = (
            total_page_items > 0
            and all(_get_block_y0(b) is not None for b in page.content_blocks)
            and all(_get_table_y0(t) is not None for t in page.tables)
        )

        if all_have_body_index:
            page_ordered_items: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
            for b, b_dict in zip(page.content_blocks, p_blocks):
                b_idx = int(b.source_position.get("body_index", 0))
                s_idx = int(b.source_position.get("sub_index", 0))
                page_ordered_items.append(((b_idx, s_idx), {"item_type": "block", **b_dict}))
            for t, t_dict in zip(page.tables, p_tables):
                b_idx = int(t.body_index) if t.body_index is not None else 0
                page_ordered_items.append(((b_idx, 0), {"item_type": "table", **t_dict}))
            page_ordered_items.sort(key=lambda x: x[0])
            for _, item in page_ordered_items:
                items_list.append(item)

        elif all_have_y0:
            page_y0_items: List[Tuple[float, Dict[str, Any]]] = []
            for b, b_dict in zip(page.content_blocks, p_blocks):
                y0 = _get_block_y0(b)
                page_y0_items.append((y0 if y0 is not None else 0.0, {"item_type": "block", **b_dict}))
            for t, t_dict in zip(page.tables, p_tables):
                y0 = _get_table_y0(t)
                page_y0_items.append((y0 if y0 is not None else 0.0, {"item_type": "table", **t_dict}))
            page_y0_items.sort(key=lambda x: x[0])
            for _, item in page_y0_items:
                items_list.append(item)

        else:
            # Fallback: keep current behaviour (blocks first, then tables)
            for b_dict in p_blocks:
                items_list.append({
                    "item_type": "block",
                    **b_dict,
                })
            for t_dict in p_tables:
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

    # MathML Warning reconciliation (STEP-7.4 Requirement 5):
    # Remove MATHML_NOT_RENDERABLE when render succeeds; keep it only for placeholders.
    if math_rendered_count > 0 and math_failed_count == 0:
        while "MATHML_NOT_RENDERABLE" in document.warnings:
            document.warnings.remove("MATHML_NOT_RENDERABLE")
    elif math_failed_count > 0:
        if "MATHML_NOT_RENDERABLE" not in document.warnings:
            document.warnings.append("MATHML_NOT_RENDERABLE")

    first_page_footer = (
        pages_list[0]["footer_text"]
        if pages_list and pages_list[0].get("footer_text")
        else ""
    )
    resolved_footer_text = footer_text if footer_text is not None else first_page_footer

    payload_geometry = geometry.to_jsx_payload()
    payload_geometry["footer_text"] = resolved_footer_text or ""

    payload_data: Dict[str, Any] = {
        "doc_id": doc_id,
        "file_path": document.file_path,
        "file_type": document.file_type,
        "footer_text": resolved_footer_text or "",
        "output_dir": str(out_path.resolve()),
        "output_indd": str(indd_file.resolve()),
        "output_pdf": str(pdf_file.resolve()),
        "result_file_path": str(result_file.resolve()),
        "binding": binding_str,
        "geometry": payload_geometry,
        "styles": styles_cfg.model_dump(),
        "math_stats": {
            "rendered": math_rendered_count,
            "failed": math_failed_count,
            "total": math_rendered_count + math_failed_count,
        },
        "image_stats": {
            "found": image_found_count,
            "copied": image_copied_count,
        },
        "warnings": list(document.warnings),
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
    footer_text: Optional[str] = None,
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
        footer_text: Optional footer text override.

    Returns:
        Result from supervisor.run_job.
    """
    payload_path = build_payload(
        document=document,
        geometry=geometry,
        styles_cfg=styles_cfg,
        output_dir=output_dir,
        binding=binding,
        footer_text=footer_text,
    )
    return submit_generate_booklet_job(
        payload_path=payload_path,
        supervisor=supervisor,
        preflight_output_path=preflight_output_path,
    )
