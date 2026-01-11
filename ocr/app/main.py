from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, HttpUrl
import requests

from app.ocr import OCRSettings, extract_text_from_pdf

app = FastAPI(title="OCR API", version="1.0.0")


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional_env_int(name: str) -> Optional[int]:
    value = _get_int_env(name, 0)
    return value if value > 0 else None


DEFAULT_LANGS = os.getenv("OCR_LANGS", "eng")
DEFAULT_DPI = _get_int_env("OCR_DPI", 300)
DEFAULT_MIN_TEXT_CHARS = _get_int_env("MIN_TEXT_CHARS", 30)
DEFAULT_MAX_PAGES = _optional_env_int("MAX_PAGES")
MAX_FILE_MB = _get_int_env("MAX_FILE_MB", 25)
MAX_FILE_BYTES = max(1, MAX_FILE_MB) * 1024 * 1024


class OCRRequest(BaseModel):
    url: HttpUrl
    langs: Optional[str] = None
    dpi: Optional[int] = None
    min_text_chars: Optional[int] = None
    force_ocr: bool = False


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _build_settings(
    langs: Optional[str],
    dpi: Optional[int],
    min_text_chars: Optional[int],
    force_ocr: bool,
) -> OCRSettings:
    return OCRSettings(
        langs=langs if langs is not None else DEFAULT_LANGS,
        dpi=dpi if dpi is not None else DEFAULT_DPI,
        min_text_chars=(
            min_text_chars if min_text_chars is not None else DEFAULT_MIN_TEXT_CHARS
        ),
        force_ocr=force_ocr,
        max_pages=DEFAULT_MAX_PAGES,
    )


def _download_pdf(url: str, dest_path: str) -> None:
    headers = {"User-Agent": "ocr-api/1.0"}
    with requests.get(url, headers=headers, stream=True, timeout=(10, 60)) as resp:
        resp.raise_for_status()
        total = 0
        with open(dest_path, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="PDF too large",
                    )
                handle.write(chunk)


def _save_upload(file: UploadFile, dest_path: str) -> None:
    total = 0
    with open(dest_path, "wb") as handle:
        while True:
            chunk = file.file.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="PDF too large",
                )
            handle.write(chunk)


def _run_ocr(pdf_path: str, settings: OCRSettings, source: dict[str, str]) -> dict:
    try:
        pages, text, stats = extract_text_from_pdf(pdf_path, settings)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "source": source,
        "text": text,
        "pages": pages,
        "stats": stats,
    }


@app.post("/ocr")
def ocr_url(payload: OCRRequest) -> dict:
    settings = _build_settings(
        payload.langs,
        payload.dpi,
        payload.min_text_chars,
        payload.force_ocr,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        _download_pdf(str(payload.url), pdf_path)
        return _run_ocr(pdf_path, settings, {"url": str(payload.url)})


@app.post("/ocr/file")
def ocr_file(
    file: UploadFile = File(...),
    langs: Optional[str] = None,
    dpi: Optional[int] = None,
    min_text_chars: Optional[int] = None,
    force_ocr: bool = False,
) -> dict:
    settings = _build_settings(langs, dpi, min_text_chars, force_ocr)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        _save_upload(file, pdf_path)
        return _run_ocr(pdf_path, settings, {"filename": file.filename or ""})
