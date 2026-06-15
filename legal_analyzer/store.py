from __future__ import annotations

import hashlib
import gzip
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from .classifier import DOCTRINAL_CHANGE_PATTERNS, classify, normalize
from .nvidia_summarizer import NvidiaSummaryError, is_nvidia_connectivity_error, summarize_with_nvidia
from .pdf_extract import chunk_text, extract_pdf_text, infer_reference_from_name, infer_title, infer_year_from_name
from .summarizer import summarize_document


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_at REAL NOT NULL,
    year INTEGER,
    title TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    materia TEXT NOT NULL,
    base_legal TEXT NOT NULL,
    regimen TEXT NOT NULL,
    doctrinal_change_score INTEGER NOT NULL,
    tags_json TEXT NOT NULL,
    needs_ocr INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    overview TEXT NOT NULL,
    key_points_json TEXT NOT NULL,
    conclusions_json TEXT NOT NULL,
    method TEXT NOT NULL,
    generated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS summary_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    overview TEXT NOT NULL,
    key_points_json TEXT NOT NULL,
    conclusions_json TEXT NOT NULL,
    method TEXT NOT NULL,
    generated_at REAL NOT NULL,
    UNIQUE(document_id, provider, model)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    document_id UNINDEXED,
    chunk_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(year);
CREATE INDEX IF NOT EXISTS idx_documents_materia ON documents(materia);
CREATE INDEX IF NOT EXISTS idx_documents_base_legal ON documents(base_legal);
CREATE INDEX IF NOT EXISTS idx_documents_regimen ON documents(regimen);
"""

INTEREST_MARKERS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "criterio juridico",
        10,
        (
            "esta agencia considera",
            "esta agencia entiende",
            "debe concluirse",
            "cabe concluir",
            "se informa favorablemente",
            "se informa desfavorablemente",
            "no resulta conforme",
            "resulta conforme",
            "resulta necesario",
            "procede analizar",
        ),
    ),
    (
        "fundamento normativo",
        8,
        (
            "articulo 6",
            "articulo 9",
            "articulo 22",
            "articulo 35",
            "reglamento (ue) 2016/679",
            "ley organica 3/2018",
            "ley organica 15/1999",
            "base juridica",
            "legitimacion",
            "licitud del tratamiento",
        ),
    ),
    (
        "base legal",
        8,
        (
            "consentimiento",
            "obligacion legal",
            "interes legitimo",
            "mision realizada en interes publico",
            "ejercicio de poderes publicos",
            "ejecucion de un contrato",
        ),
    ),
    (
        "riesgo o garantia",
        6,
        (
            "evaluacion de impacto",
            "analisis de riesgos",
            "medidas de seguridad",
            "minimizacion",
            "proporcionalidad",
            "transparencia",
            "derechos de los interesados",
            "datos especialmente protegidos",
            "categorias especiales",
        ),
    ),
    (
        "cambio doctrinal",
        14,
        DOCTRINAL_CHANGE_PATTERNS,
    ),
)

SPANISH_STOPWORDS = {
    "a",
    "al",
    "ante",
    "bajo",
    "cabe",
    "con",
    "contra",
    "de",
    "del",
    "desde",
    "durante",
    "el",
    "ella",
    "ellas",
    "ellos",
    "en",
    "entre",
    "e",
    "la",
    "las",
    "lo",
    "los",
    "o",
    "para",
    "por",
    "que",
    "se",
    "segun",
    "sin",
    "sobre",
    "su",
    "sus",
    "tras",
    "un",
    "una",
    "unas",
    "unos",
    "y",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ensure_database_file(db_path: Path, snapshot_path: Path | None = None) -> None:
    if db_path.exists():
        return
    if not snapshot_path or not snapshot_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(snapshot_path, "rb") as source, db_path.open("wb") as target:
        shutil.copyfileobj(source, target)


def init_db(db_path: Path) -> None:
    with connect(db_path) as con:
        con.executescript(SCHEMA)


def ingest_directory(pdf_dir: Path, db_path: Path, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    init_db(db_path)
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]

    stats = {"seen": len(pdfs), "indexed": 0, "skipped": 0, "failed": 0, "failures": []}
    with connect(db_path) as con:
        for path in pdfs:
            try:
                if ingest_pdf(con, path, force=force):
                    stats["indexed"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - batch import should continue.
                stats["failed"] += 1
                stats["failures"].append({"file": path.name, "error": str(exc)})
                print(f"[WARN] {path.name}: {exc}")
    return stats


def ingest_pdf(con: sqlite3.Connection, path: Path, force: bool = False) -> bool:
    sha = sha256_file(path)
    existing = con.execute("SELECT id, sha256 FROM documents WHERE reference = ?", (path.stem,)).fetchone()
    if existing and existing["sha256"] == sha and not force:
        return False
    if existing:
        con.execute("DELETE FROM chunks_fts WHERE document_id = ?", (existing["id"],))
        con.execute("DELETE FROM chunks WHERE document_id = ?", (existing["id"],))
        con.execute("DELETE FROM documents WHERE id = ?", (existing["id"],))

    text, page_count = extract_pdf_text(path)
    reference = infer_reference_from_name(path.name)
    year = infer_year_from_name(path.name)
    title = infer_title(text, reference)
    classification = classify(text)
    chunks = chunk_text(text)
    needs_ocr = 1 if len(text.strip()) < 250 else 0

    cur = con.execute(
        """
        INSERT INTO documents (
            reference, filename, path, sha256, size_bytes, modified_at, year, title,
            page_count, char_count, materia, base_legal, regimen,
            doctrinal_change_score, tags_json, needs_ocr, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reference,
            path.name,
            str(path),
            sha,
            path.stat().st_size,
            path.stat().st_mtime,
            year,
            title,
            page_count,
            len(text),
            classification.materia,
            classification.base_legal,
            classification.regimen,
            classification.doctrinal_change_score,
            json.dumps(classification.tags, ensure_ascii=False),
            needs_ocr,
            time.time(),
        ),
    )
    document_id = int(cur.lastrowid)
    for index, chunk in enumerate(chunks):
        chunk_cur = con.execute(
            "INSERT INTO chunks (document_id, chunk_index, text) VALUES (?, ?, ?)",
            (document_id, index, chunk),
        )
        con.execute(
            "INSERT INTO chunks_fts (text, document_id, chunk_id) VALUES (?, ?, ?)",
            (chunk, document_id, int(chunk_cur.lastrowid)),
        )
    con.commit()
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def db_summary(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as con:
        total = scalar(con, "SELECT COUNT(*) FROM documents")
        chunks = scalar(con, "SELECT COUNT(*) FROM chunks")
        years = rows(con, "SELECT year, COUNT(*) AS count FROM documents WHERE year IS NOT NULL GROUP BY year ORDER BY year")
        materias = rows(con, "SELECT materia AS label, COUNT(*) AS count FROM documents GROUP BY materia ORDER BY count DESC")
        bases = rows(con, "SELECT base_legal AS label, COUNT(*) AS count FROM documents GROUP BY base_legal ORDER BY count DESC")
        regimes = rows(con, "SELECT regimen AS label, COUNT(*) AS count FROM documents GROUP BY regimen ORDER BY count DESC")
        change_candidates = rows(
            con,
            """
            SELECT reference, year, title, materia, doctrinal_change_score
            FROM documents
            WHERE doctrinal_change_score > 0
            ORDER BY doctrinal_change_score DESC, year DESC
            LIMIT 15
            """,
        )
    return {
        "documents": total,
        "chunks": chunks,
        "years": years,
        "materias": materias,
        "bases": bases,
        "regimes": regimes,
        "change_candidates": change_candidates,
    }


def search_documents(db_path: Path, params: dict[str, str]) -> dict[str, Any]:
    query = params.get("q", "").strip()
    limit = int(params.get("limit", "40"))
    page = max(1, int(params.get("page", "1")))
    offset = (page - 1) * limit
    window_limit = offset + limit + 1
    filters, values = build_filters(params)
    main_terms = tokenize_query(query)
    with connect(db_path) as con:
        if main_terms:
            ranked_rows = ranked_term_search(con, filters, values, main_terms, window_limit)
            result_rows = ranked_rows[offset : offset + limit]
            has_more = len(ranked_rows) > offset + limit
        else:
            sql = f"""
                SELECT d.*, 0 AS rank, 0 AS hits, '' AS snippet
                FROM documents d
                WHERE 1=1 {filters}
                ORDER BY d.year DESC, d.reference DESC
                LIMIT ?
                OFFSET ?
            """
            result_rows = rows(con, sql, [*values, limit, offset])
            has_more = len(result_rows) == limit and scalar_filtered_count(con, filters, values) > offset + limit
            for row in result_rows:
                row["search_terms"] = []
                row["matched_terms"] = []
                row["missing_terms"] = []
                row["match_ratio"] = 0
                row["match_label"] = "sin busqueda"
    return {
        "query": query,
        "main_terms": main_terms,
        "page": page,
        "limit": limit,
        "has_more": has_more,
        "results": [hydrate_document(row) for row in result_rows],
    }


def ranked_term_search(
    con: sqlite3.Connection,
    filters: str,
    values: list[Any],
    main_terms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    documents: dict[int, dict[str, Any]] = {}
    snippets: dict[int, list[tuple[float, int, str]]] = {}
    seen_chunks: set[int] = set()
    strict_limit = max(500, limit * 25)
    partial_limit = max(1200, limit * 70)

    strict_rows = fetch_fts_candidates(con, filters, values, main_terms, operator="AND", candidate_limit=strict_limit)
    absorb_candidate_rows(strict_rows, documents, snippets, seen_chunks, main_terms)

    if len(documents) < limit:
        partial_rows = fetch_fts_candidates(con, filters, values, main_terms, operator="OR", candidate_limit=partial_limit)
        absorb_candidate_rows(partial_rows, documents, snippets, seen_chunks, main_terms)

    ranked: list[dict[str, Any]] = []
    for document_id, document in documents.items():
        matched_terms = sorted(document["matched_terms"], key=main_terms.index)
        missing_terms = [term for term in main_terms if term not in document["matched_terms"]]
        match_ratio = len(matched_terms) / len(main_terms)
        best_snippet = sorted(snippets[document_id], key=lambda item: (-item[1], item[0]))[0][2]
        document["snippet"] = best_snippet
        document["search_terms"] = main_terms
        document["matched_terms"] = matched_terms
        document["missing_terms"] = missing_terms
        document["match_ratio"] = round(match_ratio, 3)
        document["match_label"] = "completa" if not missing_terms else "parcial"
        ranked.append(document)

    ranked.sort(
        key=lambda item: (
            len(item["missing_terms"]),
            -len(item["matched_terms"]),
            float(item["rank"]),
            -(item["year"] or 0),
        )
    )
    return ranked[:limit]


def fetch_fts_candidates(
    con: sqlite3.Connection,
    filters: str,
    values: list[Any],
    main_terms: list[str],
    operator: str,
    candidate_limit: int,
) -> list[dict[str, Any]]:
    return rows(
        con,
        f"""
        SELECT d.*,
               chunks_fts.chunk_id AS matched_chunk_id,
               chunks_fts.rank AS chunk_rank,
               chunks_fts.text AS chunk_text,
               snippet(chunks_fts, 0, '<mark>', '</mark>', '...', 28) AS snippet
        FROM chunks_fts
        JOIN documents d ON d.id = chunks_fts.document_id
        WHERE chunks_fts MATCH ? {filters}
        ORDER BY chunks_fts.rank ASC
        LIMIT ?
        """,
        [make_fts_query(main_terms, operator=operator, prefix=True), *values, candidate_limit],
    )


def absorb_candidate_rows(
    candidate_rows: list[dict[str, Any]],
    documents: dict[int, dict[str, Any]],
    snippets: dict[int, list[tuple[float, int, str]]],
    seen_chunks: set[int],
    main_terms: list[str],
) -> None:
    for row in candidate_rows:
        chunk_id = int(row["matched_chunk_id"])
        if chunk_id in seen_chunks:
            continue
        seen_chunks.add(chunk_id)
        document_id = int(row["id"])
        matched = matched_query_terms(row["chunk_text"], main_terms)
        if document_id not in documents:
            document = {key: value for key, value in row.items() if key not in {"matched_chunk_id", "chunk_rank", "chunk_text"}}
            document["rank"] = row["chunk_rank"]
            document["hits"] = 0
            document["matched_terms"] = set()
            documents[document_id] = document
            snippets[document_id] = []

        document = documents[document_id]
        document["rank"] = min(float(document["rank"]), float(row["chunk_rank"]))
        document["hits"] += 1
        document["matched_terms"].update(matched)
        snippets[document_id].append((float(row["chunk_rank"]), len(matched), row["snippet"]))


def get_document(db_path: Path, reference: str, query: str = "") -> dict[str, Any] | None:
    with connect(db_path) as con:
        ensure_summary_schema(con)
        ensure_summary_variants_schema(con)
        doc = con.execute("SELECT * FROM documents WHERE reference = ?", (reference,)).fetchone()
        if not doc:
            return None
        chunks = rows(
            con,
            "SELECT chunk_index, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
            [doc["id"]],
        )
        summary = get_or_create_summary(con, doc, chunks)
        summary_variants = [
            hydrate_summary_variant(row)
            for row in con.execute(
                """
                SELECT provider, model, overview, key_points_json,
                       conclusions_json, method, generated_at
                FROM summary_variants
                WHERE document_id = ?
                ORDER BY generated_at DESC
                """,
                (doc["id"],),
            ).fetchall()
        ]
    hydrated = hydrate_document(dict(doc))
    hydrated["summary"] = summary
    hydrated["summary_variants"] = summary_variants
    hydrated["chunks"] = select_key_chunks(chunks, query=query, limit=5)
    return hydrated


def precompute_summaries(db_path: Path, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    with connect(db_path) as con:
        ensure_summary_schema(con)
        query = """
            SELECT d.*
            FROM documents d
            LEFT JOIN summaries s ON s.document_id = d.id
            WHERE (? OR s.document_id IS NULL)
            ORDER BY d.year, d.reference
        """
        docs = con.execute(query, (1 if force else 0,)).fetchall()
        if limit:
            docs = docs[:limit]
        stats = {"seen": len(docs), "summarized": 0}
        for doc in docs:
            chunks = rows(
                con,
                "SELECT chunk_index, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                [doc["id"]],
            )
            get_or_create_summary(con, doc, chunks, force=force)
            stats["summarized"] += 1
        return stats


def precompute_llm_summaries(db_path: Path, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    with connect(db_path) as con:
        ensure_summary_schema(con)
        ensure_summary_variants_schema(con)
        query = """
            SELECT d.*
            FROM documents d
            LEFT JOIN summaries s ON s.document_id = d.id
            WHERE d.needs_ocr = 0
              AND (? OR s.method IS NULL OR s.method NOT LIKE 'nvidia:%')
            ORDER BY d.year DESC, d.reference DESC
        """
        docs = con.execute(query, (1 if force else 0,)).fetchall()
        if limit:
            docs = docs[:limit]
        stats: dict[str, Any] = {
            "seen": len(docs),
            "attempted": 0,
            "summarized": 0,
            "failed": 0,
            "references": [],
            "failures": [],
        }
        for doc in docs:
            stats["attempted"] += 1
            chunks = rows(
                con,
                "SELECT chunk_index, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                [doc["id"]],
            )
            try:
                create_llm_summary(con, doc, chunks)
                stats["summarized"] += 1
                stats["references"].append(doc["reference"])
            except NvidiaSummaryError as exc:
                stats["failed"] += 1
                stats["failures"].append({"reference": doc["reference"], "error": str(exc)})
                error_text = str(exc).lower()
                if "nvidia_api_key" in error_text or "limite" in error_text or "quota" in error_text:
                    stats["stopped_early"] = True
                    stats["stop_reason"] = str(exc)
                    break
        return stats


def precompute_nvidia_summaries(db_path: Path, limit: int = 5, force: bool = False) -> dict[str, Any]:
    return precompute_llm_summaries(db_path, limit=limit, force=force)


def precompute_nvidia_summaries_auto(
    db_path: Path,
    snapshot_path: Path | None = None,
    max_attempts: int = 12,
    max_successes: int = 6,
    timeout_seconds: int = 90,
    sleep_seconds: float = 10.0,
    stop_after_timeouts: int = 2,
    log_path: Path = Path("data/nvidia_summary_runs.jsonl"),
    force: bool = False,
    update_snapshot: bool = True,
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {
        "seen": 0,
        "attempted": 0,
        "summarized": 0,
        "failed": 0,
        "timeouts": 0,
        "consecutive_timeouts": 0,
        "references": [],
        "failures": [],
        "log_path": str(log_path),
    }
    with connect(db_path) as con:
        ensure_summary_schema(con)
        ensure_summary_variants_schema(con)
        candidates = con.execute(
            """
            SELECT d.*
            FROM documents d
            LEFT JOIN summaries s ON s.document_id = d.id
            WHERE d.needs_ocr = 0
              AND (? OR s.method IS NULL OR s.method NOT LIKE 'nvidia:%')
            ORDER BY d.year DESC, d.reference DESC
            LIMIT ?
            """,
            (1 if force else 0, max_attempts),
        ).fetchall()
        stats["seen"] = len(candidates)
        for doc in candidates:
            if stats["attempted"] >= max_attempts or stats["summarized"] >= max_successes:
                break
            stats["attempted"] += 1
            started_at = time.time()
            chunks = rows(
                con,
                "SELECT chunk_index, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                [doc["id"]],
            )
            try:
                create_llm_summary(con, doc, chunks, timeout_seconds=timeout_seconds)
                duration = time.time() - started_at
                stats["summarized"] += 1
                stats["consecutive_timeouts"] = 0
                stats["references"].append(doc["reference"])
                append_nvidia_run_log(log_path, doc["reference"], "ok", duration)
            except NvidiaSummaryError as exc:
                duration = time.time() - started_at
                error = str(exc)
                is_timeout = "excedio el tiempo de espera" in error.lower()
                stats["failed"] += 1
                stats["failures"].append({"reference": doc["reference"], "error": error})
                if is_timeout:
                    stats["timeouts"] += 1
                    stats["consecutive_timeouts"] += 1
                else:
                    stats["consecutive_timeouts"] = 0
                append_nvidia_run_log(log_path, doc["reference"], "timeout" if is_timeout else "error", duration, error)
                error_text = error.lower()
                if "nvidia_api_key" in error_text or "limite" in error_text or "quota" in error_text:
                    stats["stopped_early"] = True
                    stats["stop_reason"] = error
                    break
                if is_nvidia_connectivity_error(error):
                    stats["stopped_early"] = True
                    stats["stop_reason"] = error
                    break
                if stats["consecutive_timeouts"] >= stop_after_timeouts:
                    stats["stopped_early"] = True
                    stats["stop_reason"] = f"{stop_after_timeouts} timeouts consecutivos"
                    break
            if sleep_seconds > 0 and stats["attempted"] < max_attempts and stats["summarized"] < max_successes:
                time.sleep(sleep_seconds)
    if update_snapshot and snapshot_path and stats["summarized"]:
        refresh_database_snapshot(db_path, snapshot_path)
        stats["snapshot_updated"] = True
    else:
        stats["snapshot_updated"] = False
    return stats


def generate_document_llm_summary(db_path: Path, reference: str, force: bool = False) -> dict[str, Any] | None:
    with connect(db_path) as con:
        ensure_summary_schema(con)
        doc = con.execute("SELECT * FROM documents WHERE reference = ?", (reference,)).fetchone()
        if not doc:
            return None
        if not force:
            existing = con.execute("SELECT * FROM summaries WHERE document_id = ?", (doc["id"],)).fetchone()
            if existing and str(existing["method"]).startswith("nvidia:"):
                return hydrate_summary(existing)
        chunks = rows(
            con,
            "SELECT chunk_index, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
            [doc["id"]],
        )
        return create_llm_summary(con, doc, chunks)


def create_llm_summary(
    con: sqlite3.Connection,
    doc: sqlite3.Row,
    chunks: list[dict[str, Any]],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    ensure_summary_variants_schema(con)
    key_chunks = select_key_chunks(chunks, limit=7)
    metadata = {
        "reference": doc["reference"],
        "title": doc["title"],
        "year": doc["year"],
        "materia": doc["materia"],
        "base_legal": doc["base_legal"],
        "regimen": doc["regimen"],
    }
    summary = summarize_with_nvidia(key_chunks, metadata, timeout_seconds=timeout_seconds)
    generated_at = time.time()
    existing = con.execute("SELECT * FROM summaries WHERE document_id = ?", (doc["id"],)).fetchone()
    if existing and str(existing["method"]).startswith("openai:"):
        save_existing_summary_variant(con, doc["id"], existing)
    con.execute(
        """
        INSERT INTO summaries (document_id, overview, key_points_json, conclusions_json, method, generated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            overview = excluded.overview,
            key_points_json = excluded.key_points_json,
            conclusions_json = excluded.conclusions_json,
            method = excluded.method,
            generated_at = excluded.generated_at
        """,
        (
            doc["id"],
            summary["overview"],
            json.dumps(summary["key_points"], ensure_ascii=False),
            json.dumps(summary["conclusions"], ensure_ascii=False),
            summary["method"],
            generated_at,
        ),
    )
    con.commit()
    return {**summary, "generated_at": generated_at}


def append_nvidia_run_log(
    log_path: Path,
    reference: str,
    status: str,
    duration_seconds: float,
    error: str | None = None,
) -> None:
    event: dict[str, Any] = {
        "generated_at": time.time(),
        "reference": reference,
        "status": status,
        "duration_seconds": round(duration_seconds, 3),
    }
    if error:
        event["error"] = error
    with log_path.open("a", encoding="utf-8") as target:
        target.write(json.dumps(event, ensure_ascii=False) + "\n")


def refresh_database_snapshot(db_path: Path, snapshot_path: Path) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    temp_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
    with db_path.open("rb") as source, gzip.open(temp_path, "wb", compresslevel=9) as target:
        shutil.copyfileobj(source, target)
    temp_path.replace(snapshot_path)


def ensure_summary_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            overview TEXT NOT NULL,
            key_points_json TEXT NOT NULL,
            conclusions_json TEXT NOT NULL,
            method TEXT NOT NULL,
            generated_at REAL NOT NULL
        )
        """
    )


def ensure_summary_variants_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            overview TEXT NOT NULL,
            key_points_json TEXT NOT NULL,
            conclusions_json TEXT NOT NULL,
            method TEXT NOT NULL,
            generated_at REAL NOT NULL,
            UNIQUE(document_id, provider, model)
        )
        """
    )


def save_summary_variant(con: sqlite3.Connection, document_id: int, summary: dict[str, Any]) -> None:
    generated_at = float(summary.get("generated_at") or time.time())
    con.execute(
        """
        INSERT INTO summary_variants (
            document_id, provider, model, overview, key_points_json,
            conclusions_json, method, generated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, provider, model) DO UPDATE SET
            overview = excluded.overview,
            key_points_json = excluded.key_points_json,
            conclusions_json = excluded.conclusions_json,
            method = excluded.method,
            generated_at = excluded.generated_at
        """,
        (
            document_id,
            summary["provider"],
            summary["model"],
            summary["overview"],
            json.dumps(summary["key_points"], ensure_ascii=False),
            json.dumps(summary["conclusions"], ensure_ascii=False),
            summary["method"],
            generated_at,
        ),
    )
    con.commit()


def save_existing_summary_variant(con: sqlite3.Connection, document_id: int, summary: sqlite3.Row) -> None:
    method = str(summary["method"])
    if ":" not in method:
        return
    provider, model = method.split(":", 1)
    if provider not in {"openai", "nvidia"} or not model:
        return
    save_summary_variant(
        con,
        document_id,
        {
            "provider": provider,
            "model": model,
            "overview": summary["overview"],
            "key_points": json.loads(summary["key_points_json"]),
            "conclusions": json.loads(summary["conclusions_json"]),
            "method": method,
            "generated_at": summary["generated_at"],
        },
    )


def get_or_create_summary(
    con: sqlite3.Connection,
    doc: sqlite3.Row,
    chunks: list[dict[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    if not force:
        row = con.execute("SELECT * FROM summaries WHERE document_id = ?", (doc["id"],)).fetchone()
        if row:
            return hydrate_summary(row)

    metadata = {
        "reference": doc["reference"],
        "materia": doc["materia"],
        "base_legal": doc["base_legal"],
        "regimen": doc["regimen"],
    }
    summary = summarize_document(chunks, metadata)
    con.execute(
        """
        INSERT INTO summaries (document_id, overview, key_points_json, conclusions_json, method, generated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            overview = excluded.overview,
            key_points_json = excluded.key_points_json,
            conclusions_json = excluded.conclusions_json,
            method = excluded.method,
            generated_at = excluded.generated_at
        """,
        (
            doc["id"],
            summary["overview"],
            json.dumps(summary["key_points"], ensure_ascii=False),
            json.dumps(summary["conclusions"], ensure_ascii=False),
            summary["method"],
            time.time(),
        ),
    )
    con.commit()
    return summary


def get_pdf_path(db_path: Path, reference: str) -> Path | None:
    with connect(db_path) as con:
        row = con.execute("SELECT path FROM documents WHERE reference = ?", (reference,)).fetchone()
    if not row:
        return None
    path = Path(row["path"])
    if not path.exists() or path.suffix.lower() != ".pdf":
        return None
    return path


def select_key_chunks(chunks: list[dict[str, Any]], query: str = "", limit: int = 5) -> list[dict[str, Any]]:
    query_terms = tokenize_query(query)
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        text = chunk["text"]
        normalized = normalize(text)
        score = 0
        reasons: list[str] = []

        matched_terms = [term for term in query_terms if term in normalized]
        if matched_terms:
            score += len(matched_terms) * 12
            reasons.append("coincide con la busqueda")

        for label, weight, markers in INTEREST_MARKERS:
            hits = sum(normalized.count(normalize(marker)) for marker in markers)
            if hits:
                score += min(hits, 4) * weight
                reasons.append(label)

        if len(text) > 800:
            score += 3
        if chunk["chunk_index"] > 0:
            score += 2

        scored.append(
            {
                "chunk_index": chunk["chunk_index"],
                "text": text,
                "interest_score": score,
                "interest_reasons": sorted(set(reasons)) or ["fragmento representativo"],
            }
        )

    if not scored:
        return []
    selected = sorted(scored, key=lambda item: (-item["interest_score"], item["chunk_index"]))[:limit]
    return sorted(selected, key=lambda item: item["chunk_index"])


def tokenize_query(query: str) -> list[str]:
    tokens = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ-]+", query)
    normalized = [normalize(token) for token in tokens]
    terms: list[str] = []
    for token in normalized:
        if len(token) <= 2 or token in SPANISH_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def trends(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as con:
        by_materia = rows(
            con,
            """
            SELECT year, materia AS label, COUNT(*) AS count
            FROM documents
            WHERE year IS NOT NULL
            GROUP BY year, materia
            ORDER BY year, count DESC
            """,
        )
        by_base = rows(
            con,
            """
            SELECT year, base_legal AS label, COUNT(*) AS count
            FROM documents
            WHERE year IS NOT NULL
            GROUP BY year, base_legal
            ORDER BY year, count DESC
            """,
        )
    return {"by_materia": by_materia, "by_base": by_base}


def topic_extremes(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as con:
        common = rows(
            con,
            "SELECT materia AS label, COUNT(*) AS count FROM documents GROUP BY materia ORDER BY count DESC LIMIT 8",
        )
        rare = rows(
            con,
            """
            SELECT materia AS label, COUNT(*) AS count
            FROM documents
            WHERE materia != 'Sin clasificar'
            GROUP BY materia
            ORDER BY count ASC, label ASC
            LIMIT 8
            """,
        )
    return {"common": common, "rare": rare}


def build_filters(params: dict[str, str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    filter_map = {
        "materia": "d.materia",
        "base_legal": "d.base_legal",
        "regimen": "d.regimen",
    }
    for key, column in filter_map.items():
        value = params.get(key, "").strip()
        if value:
            clauses.append(f"AND {column} = ?")
            values.append(value)
    if params.get("year_from"):
        clauses.append("AND d.year >= ?")
        values.append(int(params["year_from"]))
    if params.get("year_to"):
        clauses.append("AND d.year <= ?")
        values.append(int(params["year_to"]))
    return " ".join(clauses), values


def matched_query_terms(text: str, main_terms: list[str]) -> list[str]:
    tokens = [token for token in re.findall(r"[\w-]+", normalize(text)) if len(token) > 2]
    matched: list[str] = []
    for term in main_terms:
        term_singular = singularize(term)
        if any(token == term or token.startswith(term) or singularize(token) == term or token == term_singular for token in tokens):
            matched.append(term)
    return matched


def singularize(token: str) -> str:
    if len(token) > 5 and token.endswith("es"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def make_fts_query(terms: list[str], operator: str = "AND", prefix: bool = False) -> str:
    cleaned = ["".join(ch for ch in term if ch.isalnum() or ch in "_-") for term in terms]
    cleaned = [token for token in cleaned if len(token) > 1]
    if prefix:
        cleaned = [f"{token}*" for token in cleaned]
    joiner = f" {operator} "
    return joiner.join(cleaned)


def hydrate_document(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["tags"] = json.loads(data.pop("tags_json", "[]"))
    return data


def hydrate_summary(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "overview": data["overview"],
        "key_points": json.loads(data["key_points_json"]),
        "conclusions": json.loads(data["conclusions_json"]),
        "method": data["method"],
        "generated_at": data["generated_at"],
    }


def hydrate_summary_variant(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "provider": data["provider"],
        "model": data["model"],
        "overview": data["overview"],
        "key_points": json.loads(data["key_points_json"]),
        "conclusions": json.loads(data["conclusions_json"]),
        "method": data["method"],
        "generated_at": data["generated_at"],
    }


def rows(con: sqlite3.Connection, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def scalar(con: sqlite3.Connection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def scalar_filtered_count(con: sqlite3.Connection, filters: str, values: list[Any]) -> int:
    sql = f"SELECT COUNT(*) FROM documents d WHERE 1=1 {filters}"
    return int(con.execute(sql, values).fetchone()[0])
