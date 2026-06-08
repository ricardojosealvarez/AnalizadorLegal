from __future__ import annotations

import re
import logging
from pathlib import Path

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf_text(path: Path) -> tuple[str, int]:
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages).strip(), len(reader.pages)


def infer_year_from_name(filename: str) -> int | None:
    match = re.match(r"^(19|20)\d{2}", filename)
    if match:
        return int(filename[:4])
    match = re.search(r"(19|20)\d{2}", filename)
    if match:
        return int(match.group(0))
    return None


def infer_reference_from_name(filename: str) -> str:
    return Path(filename).stem


def infer_title(text: str, reference: str) -> str:
    lines = [clean_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) > 8]
    ignored = {
        "servicio juridico",
        "gabinete juridico",
        "agencia espanola de proteccion de datos",
    }
    candidates = [
        line
        for line in lines[:30]
        if line.lower() not in ignored and not re.fullmatch(r"\d{1,4}/\d{4}", line)
    ]
    if not candidates:
        return f"Informe {reference}"
    title = candidates[0]
    return title[:180]


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def chunk_text(text: str, target_words: int = 850, overlap_words: int = 100) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + target_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = max(end - overlap_words, start + 1)
    return chunks
