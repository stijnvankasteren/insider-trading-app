from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import pdfplumber
from pdf2image import convert_from_path
import pytesseract


@dataclass
class OCRSettings:
    langs: str
    dpi: int
    min_text_chars: int
    force_ocr: bool
    max_pages: int | None


def ocr_page(pdf_path: str, page_number: int, langs: str, dpi: int) -> str:
    images = convert_from_path(
        pdf_path,
        dpi=dpi,
        first_page=page_number,
        last_page=page_number,
        fmt="png",
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=langs)


def extract_text_from_pdf(
    pdf_path: str,
    settings: OCRSettings,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    start = time.time()
    pages_data: list[dict[str, Any]] = []
    ocr_pages = 0
    text_pages = 0

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        if settings.max_pages and total_pages > settings.max_pages:
            raise ValueError(
                f"PDF has {total_pages} pages, limit is {settings.max_pages}"
            )

        for index, page in enumerate(pdf.pages, start=1):
            text = ""
            method = "ocr"

            if not settings.force_ocr:
                text = page.extract_text() or ""
                if len(text.strip()) >= settings.min_text_chars:
                    method = "text"

            if method == "ocr":
                text = ocr_page(pdf_path, index, settings.langs, settings.dpi)

            pages_data.append({"page": index, "text": text, "method": method})
            if method == "ocr":
                ocr_pages += 1
            else:
                text_pages += 1

    combined_text = "\n\n".join(
        [page["text"].strip() for page in pages_data if page["text"]]
    )
    elapsed_ms = int((time.time() - start) * 1000)

    stats = {
        "pages": total_pages,
        "ocr_pages": ocr_pages,
        "text_pages": text_pages,
        "elapsed_ms": elapsed_ms,
    }
    return pages_data, combined_text, stats
