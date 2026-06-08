from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import DEFAULT_DB_PATH, DEFAULT_DB_SNAPSHOT_PATH, DEFAULT_PDF_DIR
from .store import db_summary, ensure_database_file, get_document, get_pdf_path, ingest_directory, precompute_summaries, search_documents, topic_extremes, trends

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


class AnalyzerHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH
    pdf_dir = DEFAULT_PDF_DIR

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook.
        parsed = urlparse(self.path)
        path = parsed.path
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}

        if path == "/":
            return self.serve_file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            return self.serve_file(STATIC_DIR / path.removeprefix("/static/"))
        if path == "/api/status":
            return self.json_response({"db_path": str(self.db_path), "pdf_dir": str(self.pdf_dir), "exists": self.db_path.exists()})
        if path == "/api/stats":
            return self.json_response(db_summary(self.db_path))
        if path == "/api/search":
            return self.json_response(search_documents(self.db_path, params))
        if path == "/api/trends":
            return self.json_response(trends(self.db_path))
        if path == "/api/topics":
            return self.json_response(topic_extremes(self.db_path))
        if path.startswith("/api/document/"):
            reference = unquote(path.removeprefix("/api/document/"))
            doc = get_document(self.db_path, reference, query=params.get("q", ""))
            if not doc:
                return self.not_found()
            return self.json_response(doc)
        if path.startswith("/pdf/"):
            reference = unquote(path.removeprefix("/pdf/")).removesuffix(".pdf")
            pdf_path = get_pdf_path(self.db_path, reference)
            if not pdf_path:
                return self.not_found()
            return self.serve_pdf(pdf_path)
        return self.not_found()

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            return self.not_found()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_pdf(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def not_found(self) -> None:
        self.json_response({"error": "not found"}, status=404)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(db_path: Path, pdf_dir: Path, host: str, port: int) -> None:
    ensure_database_file(db_path, DEFAULT_DB_SNAPSHOT_PATH)
    AnalyzerHandler.db_path = db_path
    AnalyzerHandler.pdf_dir = pdf_dir
    server = ThreadingHTTPServer((host, port), AnalyzerHandler)
    print(f"Serving Analizador Legal at http://{host}:{port}")
    print(f"Database: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="legal-analyzer")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser("ingest", help="Extract and index PDF reports")
    ingest.add_argument("--limit", type=int, default=None)
    ingest.add_argument("--force", action="store_true")

    summarize = subcommands.add_parser("summarize", help="Precompute cached legal summaries")
    summarize.add_argument("--limit", type=int, default=None)
    summarize.add_argument("--force", action="store_true")

    run = subcommands.add_parser("serve", help="Run the local web app")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))

    args = parser.parse_args(argv)
    if args.command == "ingest":
        result = ingest_directory(args.pdf_dir, args.db, limit=args.limit, force=args.force)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "summarize":
        result = precompute_summaries(args.db, limit=args.limit, force=args.force)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "serve":
        serve(args.db, args.pdf_dir, args.host, args.port)


if __name__ == "__main__":
    main()
