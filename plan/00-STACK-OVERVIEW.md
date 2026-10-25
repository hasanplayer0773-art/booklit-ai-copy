# Booklet_AI Desktop — Master Plan Document

**Target:** Automated production of Iraqi academic booklets (ملازم) — parses Word/PDF source files, classifies content (via AI or rule-based fallback), and injects structured, editable content into Adobe InDesign, preserving 100% of original text with zero mutation.

**Stack:** Python 3.11, PySide6 (Qt6), Pydantic v2, pywin32 (InDesign COM), PyMuPDF, python-docx, lxml.

---

## PHASE 1: Core Engine & Ingestion
