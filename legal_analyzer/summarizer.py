from __future__ import annotations

import re
from typing import Any

from .classifier import normalize

POINT_MARKERS = (
    "tiene por objeto",
    "la consulta plantea",
    "se plantea",
    "el proyecto",
    "el anteproyecto",
    "la norma",
    "regula",
    "establece",
    "tratamiento de datos",
    "datos personales",
    "responsable del tratamiento",
    "base juridica",
    "legitimacion",
    "evaluacion de impacto",
    "medidas de seguridad",
    "conservacion",
    "documentacion",
)

CONCLUSION_MARKERS = (
    "por tanto",
    "en consecuencia",
    "en definitiva",
    "debe concluirse",
    "cabe concluir",
    "esta agencia considera",
    "esta agencia entiende",
    "se considera",
    "debera",
    "debe",
    "resulta necesario",
    "resulta conforme",
    "no resulta conforme",
    "se informa favorablemente",
    "se informa desfavorablemente",
    "se sugiere",
)


def summarize_document(chunks: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(chunk["text"] for chunk in chunks)
    sentences = split_sentences(text)
    if not sentences:
        return {
            "overview": f"No se ha podido elaborar resumen para {metadata.get('reference', 'este dictamen')}.",
            "key_points": [],
            "conclusions": [],
            "method": "extractive-v1",
        }

    overview = make_overview(sentences, metadata)
    key_points = select_sentences(sentences, POINT_MARKERS, limit=5, prefer_late=False)
    conclusions = select_sentences(sentences, CONCLUSION_MARKERS, limit=4, prefer_late=True)

    if not key_points:
        key_points = sentences[: min(4, len(sentences))]
    if not conclusions:
        conclusions = sentences[-min(3, len(sentences)) :]

    return {
        "overview": overview,
        "key_points": key_points,
        "conclusions": conclusions,
        "method": "extractive-v1",
    }


def split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text)
    raw = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ])", cleaned)
    sentences: list[str] = []
    for sentence in raw:
        sentence = clean_sentence(sentence)
        if is_useful_sentence(sentence):
            sentences.append(sentence)
    return sentences


def clean_sentence(sentence: str) -> str:
    sentence = re.sub(r"c\.\s*Jorge Juan\s*6\s*www\.aepd\.es\s*28001\s*Madrid\s*\d*", " ", sentence, flags=re.I)
    sentence = re.sub(r"\s+", " ", sentence)
    return sentence.strip(" \t\n\r-")


def is_useful_sentence(sentence: str) -> bool:
    if len(sentence) < 80 or len(sentence) > 850:
        return False
    lowered = normalize(sentence)
    noisy = ("gabinete juridico", "servicio juridico", "www.aepd.es", "jorge juan")
    return not any(marker in lowered for marker in noisy)


def make_overview(sentences: list[str], metadata: dict[str, Any]) -> str:
    reference = metadata.get("reference", "El dictamen")
    matter = metadata.get("materia", "materia no clasificada")
    basis = metadata.get("base_legal", "base legal no identificada")
    regime = metadata.get("regimen", "regimen no identificado")
    candidate = first_sentence_with(sentences, ("tiene por objeto", "consulta", "proyecto", "anteproyecto", "se plantea"))
    if candidate:
        return f"{reference} trata principalmente sobre {matter}. {candidate}"
    return f"{reference} trata principalmente sobre {matter}, con base legal {basis} y regimen {regime}."


def first_sentence_with(sentences: list[str], markers: tuple[str, ...]) -> str:
    for sentence in sentences[:35]:
        normalized = normalize(sentence)
        if any(marker in normalized for marker in markers):
            return sentence
    return sentences[0] if sentences else ""


def select_sentences(sentences: list[str], markers: tuple[str, ...], limit: int, prefer_late: bool) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    total = max(len(sentences), 1)
    for index, sentence in enumerate(sentences):
        normalized = normalize(sentence)
        marker_score = sum(1 for marker in markers if marker in normalized)
        legal_score = sum(
            1
            for marker in (
                "rgpd",
                "lopdgdd",
                "ley organica",
                "articulo",
                "proteccion de datos",
                "derechos",
                "responsable",
                "tratamiento",
            )
            if marker in normalized
        )
        position_score = int((index / total) * 4) if prefer_late else int((1 - index / total) * 2)
        score = marker_score * 8 + legal_score * 2 + position_score
        if score > 3:
            scored.append((score, index, sentence))

    selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]
    selected.sort(key=lambda item: item[1])
    return [sentence for _, _, sentence in selected]

