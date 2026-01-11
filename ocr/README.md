# OCR API for PDFs

Simple OCR service for PDFs. It can read text PDFs directly and falls back to OCR for scanned pages. Output is JSON, so it works well with n8n.

## Endpoints

POST /ocr (JSON)

Body:
```
{
  "url": "https://.../file.pdf",
  "langs": "eng",
  "dpi": 300,
  "min_text_chars": 30,
  "force_ocr": false
}
```

POST /ocr/file (multipart form)

Form fields:
- file: PDF file
- langs, dpi, min_text_chars, force_ocr (optional query params)

Response:
```
{
  "source": {"url": "..."},
  "text": "full text...",
  "pages": [
    {"page": 1, "text": "...", "method": "ocr"}
  ],
  "stats": {"pages": 3, "ocr_pages": 3, "text_pages": 0, "elapsed_ms": 1234}
}
```

## Local run

Build and start:
```
docker compose up -d --build
```

Health check:
```
curl http://localhost:8000/health
```

Example OCR call:
```
curl -X POST http://localhost:8000/ocr \
  -H "Content-Type: application/json" \
  -d '{"url":"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20033574.pdf"}'
```

## n8n

Use an HTTP Request node:
- Method: POST
- URL: http://<host>:8000/ocr
- Body Content Type: JSON
- Body:
```
{
  "url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/20033574.pdf"
}
```

The response is JSON and can be used directly in later nodes.

## Portainer

1. Open Portainer and go to Stacks
2. Add Stack
3. Use the web editor and paste `docker-compose.yml` from this repo (or point Portainer to this repo if you prefer)
4. Deploy the stack
5. Open http://<host>:8000/health to confirm

## Environment variables

- OCR_LANGS: default language(s) for Tesseract (default: eng)
- OCR_DPI: DPI used for OCR images (default: 300)
- MIN_TEXT_CHARS: minimum chars for a page to count as text-only (default: 30)
- MAX_PAGES: max pages to process (default: 50; 0 or unset = no limit)
- MAX_FILE_MB: max PDF size in MB (default: 25)

If you need extra languages (like Dutch), install the language pack in the Dockerfile (for example: tesseract-ocr-nld) and set OCR_LANGS=nld.
