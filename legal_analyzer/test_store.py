from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import store


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    return con


def insert_document(con: sqlite3.Connection, reference: str = "2026-0001") -> sqlite3.Row:
    con.execute(
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
            f"{reference}.pdf",
            f"/tmp/{reference}.pdf",
            "abc123",
            100,
            1.0,
            2026,
            "Servicio Juridico",
            2,
            500,
            "IA, perfiles y decisiones automatizadas",
            "Consentimiento",
            "RGPD",
            0,
            "[]",
            0,
            2.0,
        ),
    )
    con.commit()
    return con.execute("SELECT * FROM documents WHERE reference = ?", (reference,)).fetchone()


def insert_summary(
    con: sqlite3.Connection,
    document_id: int,
    method: str,
    generated_at: float = 10.0,
) -> None:
    con.execute(
        """
        INSERT INTO summaries (
            document_id, overview, key_points_json, conclusions_json, method, generated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            "Resumen anterior",
            json.dumps(["Punto anterior"], ensure_ascii=False),
            json.dumps(["Conclusion anterior"], ensure_ascii=False),
            method,
            generated_at,
        ),
    )
    con.commit()


class StoreNvidiaSummaryTests(unittest.TestCase):
    def test_create_llm_summary_promotes_nvidia_and_preserves_openai_variant(self) -> None:
        con = make_connection()
        doc = insert_document(con)
        insert_summary(con, doc["id"], "openai:gpt-5.4-mini", generated_at=123.0)
        provider_result = {
            "overview": "Resumen NVIDIA",
            "key_points": ["Punto NVIDIA"],
            "conclusions": ["Conclusion NVIDIA"],
            "method": "nvidia:mistralai/mistral-medium-3.5-128b",
            "provider": "nvidia",
            "model": "mistralai/mistral-medium-3.5-128b",
        }

        with patch("legal_analyzer.store.summarize_with_nvidia", return_value=provider_result):
            result = store.create_llm_summary(
                con,
                doc,
                [{"chunk_index": 0, "text": "Texto juridico suficiente para el resumen."}],
            )

        self.assertEqual(result["method"], "nvidia:mistralai/mistral-medium-3.5-128b")
        main = con.execute("SELECT * FROM summaries WHERE document_id = ?", (doc["id"],)).fetchone()
        self.assertEqual(main["method"], "nvidia:mistralai/mistral-medium-3.5-128b")
        variant = con.execute(
            "SELECT * FROM summary_variants WHERE document_id = ? AND provider = ?",
            (doc["id"], "openai"),
        ).fetchone()
        self.assertIsNotNone(variant)
        self.assertEqual(variant["model"], "gpt-5.4-mini")
        self.assertEqual(variant["generated_at"], 123.0)

    def test_generate_document_llm_summary_reuses_existing_nvidia_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            con = store.connect(db_path)
            con.executescript(store.SCHEMA)
            doc = insert_document(con)
            insert_summary(con, doc["id"], "nvidia:mistralai/mistral-medium-3.5-128b")
            con.close()

            with patch("legal_analyzer.store.summarize_with_nvidia") as summarize:
                result = store.generate_document_llm_summary(db_path, "2026-0001", force=False)

        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "nvidia:mistralai/mistral-medium-3.5-128b")
        summarize.assert_not_called()

    def test_precompute_nvidia_summaries_auto_logs_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            log_path = Path(directory) / "runs.jsonl"
            con = store.connect(db_path)
            con.executescript(store.SCHEMA)
            insert_document(con, "2026-0001")
            con.close()
            provider_result = {
                "overview": "Resumen NVIDIA",
                "key_points": ["Punto NVIDIA"],
                "conclusions": ["Conclusion NVIDIA"],
                "method": "nvidia:mistralai/mistral-medium-3.5-128b",
                "provider": "nvidia",
                "model": "mistralai/mistral-medium-3.5-128b",
            }

            with patch("legal_analyzer.store.summarize_with_nvidia", return_value=provider_result) as summarize:
                result = store.precompute_nvidia_summaries_auto(
                    db_path,
                    max_attempts=1,
                    max_successes=1,
                    sleep_seconds=0,
                    log_path=log_path,
                    update_snapshot=False,
                    timeout_seconds=45,
                )

            self.assertEqual(result["summarized"], 1)
            self.assertEqual(result["references"], ["2026-0001"])
            summarize.assert_called_once()
            self.assertEqual(summarize.call_args.kwargs["timeout_seconds"], 45)
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["reference"], "2026-0001")
            self.assertEqual(events[0]["status"], "ok")

    def test_precompute_nvidia_summaries_auto_stops_after_consecutive_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            log_path = Path(directory) / "runs.jsonl"
            con = store.connect(db_path)
            con.executescript(store.SCHEMA)
            insert_document(con, "2026-0002")
            insert_document(con, "2026-0001")
            con.close()

            with patch(
                "legal_analyzer.store.summarize_with_nvidia",
                side_effect=store.NvidiaSummaryError("La solicitud a NVIDIA excedio el tiempo de espera."),
            ):
                result = store.precompute_nvidia_summaries_auto(
                    db_path,
                    max_attempts=2,
                    max_successes=2,
                    sleep_seconds=0,
                    stop_after_timeouts=2,
                    log_path=log_path,
                    update_snapshot=False,
                )

            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["timeouts"], 2)
            self.assertTrue(result["stopped_early"])
            self.assertEqual(result["stop_reason"], "2 timeouts consecutivos")
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["timeout", "timeout"])

    def test_precompute_nvidia_summaries_auto_stops_after_connectivity_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            log_path = Path(directory) / "runs.jsonl"
            con = store.connect(db_path)
            con.executescript(store.SCHEMA)
            insert_document(con, "2026-0002")
            insert_document(con, "2026-0001")
            con.close()

            with patch(
                "legal_analyzer.store.summarize_with_nvidia",
                side_effect=store.NvidiaSummaryError(
                    "No se pudo resolver el host de NVIDIA (integrate.api.nvidia.com). "
                    "Revisa NVIDIA_CHAT_COMPLETIONS_URL, NVIDIA_API_BASE_URL o la conectividad DNS."
                ),
            ):
                result = store.precompute_nvidia_summaries_auto(
                    db_path,
                    max_attempts=2,
                    max_successes=2,
                    sleep_seconds=0,
                    log_path=log_path,
                    update_snapshot=False,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(result["stopped_early"])
            self.assertIn("No se pudo resolver el host de NVIDIA", result["stop_reason"])
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["error"])


if __name__ == "__main__":
    unittest.main()
