from __future__ import annotations

from pathlib import Path
from typing import Any

from .classifier import classify, normalize
from .store import get_document, search_documents, tokenize_query

SUPPORT_MARKERS = (
    "resulta conforme",
    "se informa favorablemente",
    "se considera conforme",
    "base juridica",
    "interes legitimo",
    "obligacion legal",
    "legitimacion",
    "resulta adecuada",
)

QUESTION_MARKERS = (
    "no resulta conforme",
    "no se considera conforme",
    "se informa desfavorablemente",
    "no puede",
    "no podria",
    "no es posible",
    "quiebra",
    "vigilancia sistematica",
    "injustificada",
    "ilimitado",
)

CONDITION_MARKERS = (
    "siempre que",
    "debera",
    "debe",
    "garantias",
    "ponderacion",
    "evaluacion de impacto",
    "analisis de riesgos",
    "minimizacion",
    "transparencia",
    "informacion al interesado",
)


def analyze_treatment(db_path: Path, description: str, limit: int = 12) -> dict[str, Any]:
    description = description.strip()
    if not description:
        return {"error": "La descripcion del tratamiento esta vacia."}

    classification = classify(description)
    terms = tokenize_query(description)
    search = search_documents(db_path, {"q": description, "limit": str(limit), "page": "1"})

    reports = []
    support_count = 0
    question_count = 0
    condition_count = 0
    current_weight = 0
    obsolete_weight = 0

    for result in search["results"]:
        document = get_document(db_path, result["reference"], query=description)
        if not document:
            continue
        assessed = assess_report(result, document)
        reports.append(assessed)
        if assessed["stance"] == "apoya":
            support_count += 1
        elif assessed["stance"] == "cuestiona":
            question_count += 1
        else:
            condition_count += 1

        if assessed["vigencia"]["level"] in {"alta", "media"}:
            current_weight += 1
        else:
            obsolete_weight += 1

    orientation = make_orientation(support_count, question_count, condition_count, current_weight, obsolete_weight)
    risks = infer_risks(description, classification, reports)

    return {
        "description": description,
        "terms": terms,
        "detected": {
            "materia": classification.materia,
            "base_legal": classification.base_legal,
            "regimen": classification.regimen,
        },
        "orientation": orientation,
        "risks": risks,
        "supporting_reports": [report for report in reports if report["stance"] == "apoya"],
        "questioning_reports": [report for report in reports if report["stance"] == "cuestiona"],
        "conditional_reports": [report for report in reports if report["stance"] == "condiciona"],
        "all_reports": reports,
        "method": "corpus-rules-v1",
        "disclaimer": "Evaluacion orientativa basada en informes AEPD del corpus. No sustituye un analisis juridico profesional del caso concreto.",
    }


def assess_report(result: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    summary = document.get("summary") or {}
    evidence_text = " ".join(
        [
            result.get("snippet", ""),
            summary.get("overview", ""),
            " ".join(summary.get("conclusions", [])),
            " ".join(chunk.get("text", "")[:900] for chunk in document.get("chunks", [])[:2]),
        ]
    )
    normalized = normalize(evidence_text)
    support = marker_score(normalized, SUPPORT_MARKERS)
    question = marker_score(normalized, QUESTION_MARKERS)
    condition = marker_score(normalized, CONDITION_MARKERS)

    if question >= max(2, support + 1):
        stance = "cuestiona"
    elif support >= 2 and question == 0:
        stance = "apoya"
    else:
        stance = "condiciona"

    return {
        "reference": result["reference"],
        "title": result["title"],
        "year": result["year"],
        "materia": result["materia"],
        "base_legal": result["base_legal"],
        "regimen": result["regimen"],
        "snippet": result.get("snippet", ""),
        "summary": summary.get("overview", ""),
        "stance": stance,
        "stance_scores": {"apoya": support, "cuestiona": question, "condiciona": condition},
        "vigencia": assess_obsolescence(result["year"], result["regimen"]),
        "pdf_url": f"/pdf/{result['reference']}.pdf",
    }


def marker_score(text: str, markers: tuple[str, ...]) -> int:
    return sum(text.count(normalize(marker)) for marker in markers)


def assess_obsolescence(year: int | None, regimen: str) -> dict[str, Any]:
    if not year:
        return {"score": 35, "level": "incierta", "reason": "No consta año suficiente para valorar vigencia."}
    regimen_normalized = normalize(regimen)
    if year >= 2018 and ("rgpd" in regimen_normalized or "lopdgdd" in regimen_normalized):
        return {"score": 95, "level": "alta", "reason": "Informe posterior a la aplicacion del RGPD y alineado con regimen RGPD/LOPDGDD."}
    if year >= 2018:
        return {"score": 75, "level": "media", "reason": "Informe posterior a 2018, aunque el regimen clasificado no es claramente RGPD/LOPDGDD."}
    if year >= 2016:
        return {"score": 45, "level": "transitoria", "reason": "Informe anterior a la aplicacion del RGPD; util como antecedente, pero debe contrastarse con RGPD/LOPDGDD."}
    return {"score": 25, "level": "historica", "reason": "Informe pre-RGPD. Su valor principal es historico o doctrinal, no decisivo para un tratamiento actual."}


def make_orientation(support: int, question: int, condition: int, current: int, obsolete: int) -> dict[str, Any]:
    if question >= 2 and question >= support:
        label = "cuestionable"
        confidence = "media"
        rationale = "Hay varios informes que contienen criterios restrictivos o negativos para tratamientos similares."
    elif support >= 2 and question == 0:
        label = "probablemente viable con garantias"
        confidence = "media"
        rationale = "Los informes mas cercanos tienden a reconocer una base o encaje, normalmente sujeto a garantias."
    else:
        label = "requiere analisis especifico"
        confidence = "baja-media"
        rationale = "La muestra contiene criterios condicionados o insuficientes para concluir legalidad sin mas datos."

    if obsolete > current and current == 0:
        confidence = "baja"
        rationale += " Ademas, los informes localizados son mayoritariamente pre-RGPD."

    return {
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
        "counts": {"apoyan": support, "cuestionan": question, "condicionan": condition},
    }


def infer_risks(description: str, classification: Any, reports: list[dict[str, Any]]) -> list[str]:
    normalized = normalize(description)
    risks: list[str] = []
    if classification.base_legal == "No identificada":
        risks.append("No se identifica con claridad la base juridica del tratamiento.")
    if any(marker in normalized for marker in ("biometr", "inteligencia artificial", "perfil", "automatiz")):
        risks.append("Puede requerir evaluacion de impacto, gestion reforzada del riesgo y explicabilidad.")
    if any(marker in normalized for marker in ("trabajador", "empleado", "laboral")):
        risks.append("En contexto laboral, la proporcionalidad y la expectativa razonable del trabajador suelen ser centrales.")
    if any(marker in normalized for marker in ("menor", "alumno", "colegio")):
        risks.append("Al afectar a menores, se eleva la exigencia de necesidad, informacion y minimizacion.")
    if any(marker in normalized for marker in ("conservacion", "plazo", "documentacion")):
        risks.append("Debe justificarse el plazo de conservacion y prever supresion o revision periodica.")
    if any(report["vigencia"]["level"] in {"historica", "transitoria"} for report in reports[:5]):
        risks.append("Parte de la doctrina localizada es pre-RGPD o transitoria; debe ponderarse su obsolescencia.")
    return risks or ["No se detectan riesgos especificos por reglas; revisar los informes citados y las garantias aplicables."]
