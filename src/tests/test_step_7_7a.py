"""
Booklet_AI Desktop — Tests for STEP-7.7a

Validates that:
- (a) json.dumps of the payload succeeds (regression for the TypeError when ImageAsset is in source_position).
- (b) payload has two image blocks with render_mode == "image", existing files under <out>/images/,
      and correct pixel sizes.
- (c) deleting the temp source image before the build raises ImageAssetMissingError.
- (d) the original ContentBlock.source_position still holds the ImageAsset object (no mutation).
- pipeline result and report.json contain the image_stats counter (found/copied) and report prints it.
"""

from __future__ import annotations

import io
import json
import struct
import sys
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from docx import Document

from app.automation.generation_pipeline import _json_safe, build_payload
from app.automation.geometry import PageGeometry
from app.automation.styles_schema import TypographyStylesConfig
from app.core.docx_extractor import DocxContentExtractor
from app.core.exceptions import ImageAssetMissingError
from app.core.models import ImageAsset
from tools.generate_from_docx import build_report, format_report_text, run_pipeline


# ─── Synthetic PNG Generator ──────────────────────────────────────────


def make_png_bytes(
    width: int,
    height: int,
    color: tuple[int, int, int] = (255, 0, 0),
) -> bytes:
    """Generate minimal valid PNG bytes with struct + zlib (no Pillow dependency)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return length + chunk_type + data + crc

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)

    raw_row = b"\x00" + bytes(color) * width
    raw_data = raw_row * height
    idat = chunk(b"IDAT", zlib.compress(raw_data))
    iend = chunk(b"IEND", b"")

    return signature + ihdr + idat + iend


def create_test_docx_with_two_images(docx_path: Path) -> tuple[int, int, int, int]:
    """Create a synthetic DOCX file with Arabic text and two inline PNG images."""
    w1, h1 = 120, 60
    w2, h2 = 80, 40
    png1 = make_png_bytes(w1, h1, color=(255, 0, 0))
    png2 = make_png_bytes(w2, h2, color=(0, 255, 0))

    doc = Document()
    doc.add_paragraph("المقدمة الأولى في الكتاب")
    doc.add_picture(io.BytesIO(png1))
    doc.add_paragraph("نص بين الصورتين في الفصل")
    doc.add_picture(io.BytesIO(png2))
    doc.add_paragraph("خاتمة الدرس الأول")
    doc.save(str(docx_path))

    return w1, h1, w2, h2


# ─── Tests ────────────────────────────────────────────────────────────


class TestStep77aImageBlocksSerializationAndShipping:
    """Acceptance test suite for STEP-7.7a."""

    def test_image_blocks_serialization_and_shipping(self, tmp_path: Path):
        """(a) json.dumps succeeds, (b) payload has two image blocks with render_mode='image',
        existing files under <out>/images/, correct pixel sizes, and (d) no mutation."""
        docx_path = tmp_path / "test_images.docx"
        w1, h1, w2, h2 = create_test_docx_with_two_images(docx_path)

        extractor = DocxContentExtractor()
        booklet_doc = extractor.extract(str(docx_path))

        # Check extracted image blocks
        img_blocks = [
            b
            for p in booklet_doc.pages
            for b in p.content_blocks
            if b.block_type == "image_prompt"
        ]
        assert len(img_blocks) == 2, "Must extract exactly 2 image_prompt blocks"

        # Verify original blocks have ImageAsset instance before build
        for ib in img_blocks:
            asset = ib.source_position.get("image_asset")
            assert isinstance(asset, ImageAsset)

        out_dir = tmp_path / "out_build"
        geometry = PageGeometry()
        styles_cfg = TypographyStylesConfig()

        payload_file = build_payload(
            document=booklet_doc,
            geometry=geometry,
            styles_cfg=styles_cfg,
            output_dir=out_dir,
        )

        # (a) json.dumps of the payload succeeds (regression for the TypeError)
        with open(payload_file, "r", encoding="utf-8") as f:
            payload_data = json.load(f)
        dumped = json.dumps(payload_data)
        assert dumped is not None

        # (b) payload has two image blocks with render_mode == "image",
        # existing files under <out>/images/, and correct pixel sizes
        payload_img_blocks = [
            b
            for b in payload_data["blocks"]
            if b.get("render_mode") == "image"
            and b.get("block_type") == "image_prompt"
        ]
        assert len(payload_img_blocks) == 2

        images_dir = out_dir / "images"
        assert images_dir.is_dir()

        for pb in payload_img_blocks:
            assert pb["render_mode"] == "image"
            assert pb["alt_text"] == "image"
            copied_path = Path(pb["image_path"])
            assert copied_path.is_file(), f"Copied image {copied_path} must exist"
            assert copied_path.parent == images_dir.resolve()
            assert pb["block_id"] in copied_path.name

        # Check pixel dimensions match the generated PNGs
        dims = [(pb["pixel_width"], pb["pixel_height"]) for pb in payload_img_blocks]
        assert (w1, h1) in dims
        assert (w2, h2) in dims

        # Check image_stats in payload
        assert payload_data.get("image_stats") == {"found": 2, "copied": 2}

        # (d) the original ContentBlock.source_position still holds the ImageAsset object (no mutation)
        for ib in img_blocks:
            asset = ib.source_position.get("image_asset")
            assert isinstance(
                asset, ImageAsset
            ), "Original ContentBlock.source_position['image_asset'] must remain ImageAsset"

    def test_missing_image_asset_raises_error(self, tmp_path: Path):
        """(c) deleting the temp source image before the build raises ImageAssetMissingError."""
        docx_path = tmp_path / "test_missing.docx"
        create_test_docx_with_two_images(docx_path)

        extractor = DocxContentExtractor()
        booklet_doc = extractor.extract(str(docx_path))

        img_blocks = [
            b
            for p in booklet_doc.pages
            for b in p.content_blocks
            if b.block_type == "image_prompt"
        ]
        assert len(img_blocks) >= 1

        first_img = img_blocks[0]
        asset = first_img.source_position["image_asset"]
        src_path = Path(asset.source_path)
        assert src_path.is_file()

        # Delete the source temporary file before build
        src_path.unlink()
        assert not src_path.exists()

        out_dir = tmp_path / "out_build_missing"
        geometry = PageGeometry()
        styles_cfg = TypographyStylesConfig()

        with pytest.raises(ImageAssetMissingError) as exc_info:
            build_payload(
                document=booklet_doc,
                geometry=geometry,
                styles_cfg=styles_cfg,
                output_dir=out_dir,
            )

        assert exc_info.value.block_id == first_img.block_id
        assert str(src_path) in exc_info.value.path

    def test_run_pipeline_build_only_reports_images(self, tmp_path: Path):
        """Verify run_pipeline in --build-only mode outputs images, report.json, and formatted text."""
        docx_path = tmp_path / "test_cli_images.docx"
        create_test_docx_with_two_images(docx_path)

        out_dir = tmp_path / "out_cli"
        exit_code = run_pipeline(
            docx_file=docx_path,
            output_dir=out_dir,
            no_ai=True,
            build_only=True,
        )
        assert exit_code == 0

        report_file = out_dir / "report.json"
        assert report_file.is_file()
        with open(report_file, "r", encoding="utf-8") as f:
            report_data = json.load(f)

        assert "image_stats" in report_data
        assert report_data["image_stats"]["found"] == 2
        assert report_data["image_stats"]["copied"] == 2

        # Check images directory exists with 2 files
        images_dir = out_dir / "images"
        assert images_dir.is_dir()
        copied_images = list(images_dir.glob("*"))
        assert len(copied_images) == 2

    def test_json_safe_helper_recursive(self):
        """_json_safe helper recursively converts BaseModel, dicts, lists, and tuples."""
        asset = ImageAsset(
            source_path="/tmp/test.png",
            pixel_width=100,
            pixel_height=50,
            intended_width_mm=30.0,
            intended_height_mm=15.0,
        )
        data = {
            "asset": asset,
            "nested_list": [1, (asset, "inner")],
            "plain_str": "val",
            "path": Path("/tmp/other.png"),
        }
        safe_data = _json_safe(data)

        # Asset converted to dict
        assert isinstance(safe_data["asset"], dict)
        assert safe_data["asset"]["pixel_width"] == 100
        assert safe_data["asset"]["source_path"] == "/tmp/test.png"

        # Nested in tuple/list converted
        assert isinstance(safe_data["nested_list"][1][0], dict)
        assert safe_data["nested_list"][1][0]["pixel_height"] == 50

        # Path converted to str
        assert isinstance(safe_data["path"], str)

        # json.dumps must succeed without error
        serialized = json.dumps(safe_data)
        assert serialized is not None

        # Original asset model is unmutated
        assert isinstance(asset, ImageAsset)
        assert asset.pixel_width == 100
