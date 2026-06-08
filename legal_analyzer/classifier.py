from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Classification:
    materia: str
    base_legal: str
    regimen: str
    doctrinal_change_score: int
    tags: list[str]


MATTER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Videovigilancia": (
        "videovigilancia",
        "camara",
        "camaras",
        "grabacion de imagenes",
        "control de acceso",
    ),
    "Laboral y recursos humanos": (
        "trabajador",
        "empleado",
        "nomina",
        "recursos humanos",
        "control laboral",
        "comite de empresa",
        "prevencion de riesgos",
    ),
    "Salud y datos sanitarios": (
        "historia clinica",
        "datos de salud",
        "sanitario",
        "paciente",
        "hospital",
        "centro de salud",
        "discapacidad",
    ),
    "Administraciones publicas": (
        "administracion publica",
        "ayuntamiento",
        "comunidad autonoma",
        "procedimiento administrativo",
        "padron",
        "sector publico",
        "empleado publico",
    ),
    "Transparencia y acceso a informacion": (
        "transparencia",
        "acceso a la informacion",
        "publicidad activa",
        "consejo de transparencia",
        "portal de transparencia",
    ),
    "Derechos RGPD": (
        "derecho de acceso",
        "derecho de rectificacion",
        "derecho de supresion",
        "derecho de oposicion",
        "portabilidad",
        "limitacion del tratamiento",
    ),
    "Marketing, cookies y comunicaciones": (
        "comunicaciones comerciales",
        "marketing",
        "publicidad",
        "cookies",
        "lssi",
        "prospectiva comercial",
        "newsletter",
    ),
    "Menores y educacion": (
        "menor",
        "menores",
        "colegio",
        "centro educativo",
        "alumno",
        "universidad",
        "educacion",
    ),
    "Cesion y comunicacion de datos": (
        "cesion de datos",
        "comunicacion de datos",
        "destinatario",
        "comunicar datos",
        "terceros",
    ),
    "Transferencias internacionales": (
        "transferencia internacional",
        "tercer pais",
        "clausulas contractuales tipo",
        "decision de adecuacion",
        "encargado establecido fuera",
    ),
    "Seguridad y brechas": (
        "brecha de seguridad",
        "violacion de seguridad",
        "medidas de seguridad",
        "confidencialidad",
        "integridad",
        "analisis de riesgos",
    ),
    "IA, perfiles y decisiones automatizadas": (
        "inteligencia artificial",
        "algoritmo",
        "perfilado",
        "decision automatizada",
        "tratamiento automatizado",
        "biometrico",
    ),
    "Consumo y servicios digitales": (
        "consumidores",
        "usuarios",
        "plataforma digital",
        "servicios digitales",
        "mercado digital",
        "resenas",
    ),
}

LEGAL_BASIS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Consentimiento": (
        "consentimiento",
        "consentimiento expreso",
        "consentimiento informado",
        "retirar el consentimiento",
    ),
    "Contrato": (
        "ejecucion de un contrato",
        "relacion contractual",
        "medidas precontractuales",
        "contrato",
    ),
    "Obligacion legal": (
        "obligacion legal",
        "cumplimiento de una obligacion",
        "deber legal",
        "norma con rango de ley",
    ),
    "Interes legitimo": (
        "interes legitimo",
        "ponderacion",
        "expectativas razonables",
        "articulo 6.1.f",
    ),
    "Mision publica / potestad publica": (
        "mision realizada en interes publico",
        "ejercicio de poderes publicos",
        "potestad publica",
        "articulo 6.1.e",
        "interes publico",
    ),
    "Intereses vitales": (
        "intereses vitales",
        "proteger intereses vitales",
        "vida o integridad",
    ),
}

REGIME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "RGPD": (
        "reglamento (ue) 2016/679",
        "reglamento general de proteccion de datos",
        "rgpd",
        "articulo 6.1",
    ),
    "LOPDGDD": (
        "ley organica 3/2018",
        "lopdgdd",
        "garantia de los derechos digitales",
    ),
    "LOPD 15/1999": (
        "ley organica 15/1999",
        "lopd",
        "real decreto 1720/2007",
    ),
    "LSSI / ePrivacy": (
        "ley 34/2002",
        "lssi",
        "servicios de la sociedad de la informacion",
        "cookies",
        "comunicaciones electronicas",
    ),
    "LED 7/2021": (
        "ley organica 7/2021",
        "prevencion, deteccion, investigacion",
        "infracciones penales",
    ),
    "Normativa sectorial": (
        "ley 39/2015",
        "ley 40/2015",
        "ley general tributaria",
        "ley de transparencia",
        "estatuto de los trabajadores",
        "ley general de sanidad",
    ),
}

DOCTRINAL_CHANGE_PATTERNS = (
    "cambio de criterio",
    "cambio doctrinal",
    "modificacion de criterio",
    "esta agencia ha venido",
    "doctrina de esta agencia",
    "criterio mantenido",
    "criterio anterior",
    "debe reconsiderarse",
    "a partir de la aplicacion del rgpd",
    "tras la entrada en vigor",
    "sin perjuicio de lo sostenido",
)


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", folded).lower()


def classify(text: str) -> Classification:
    normalized = normalize(text)
    materia = _best_label(normalized, MATTER_KEYWORDS, "Sin clasificar")
    base_legal = _best_label(normalized, LEGAL_BASIS_KEYWORDS, "No identificada")
    regimen = _best_label(normalized, REGIME_KEYWORDS, "No identificado")
    doctrinal_score = sum(normalized.count(pattern) for pattern in DOCTRINAL_CHANGE_PATTERNS)
    tags = _collect_tags(normalized)
    return Classification(materia, base_legal, regimen, doctrinal_score, tags)


def _best_label(text: str, taxonomy: dict[str, Iterable[str]], fallback: str) -> str:
    scores: list[tuple[int, str]] = []
    for label, keywords in taxonomy.items():
        score = sum(text.count(normalize(keyword)) for keyword in keywords)
        if score:
            scores.append((score, label))
    if not scores:
        return fallback
    return sorted(scores, reverse=True)[0][1]


def _collect_tags(text: str) -> list[str]:
    tags: list[str] = []
    for taxonomy in (MATTER_KEYWORDS, LEGAL_BASIS_KEYWORDS, REGIME_KEYWORDS):
        for label, keywords in taxonomy.items():
            if any(normalize(keyword) in text for keyword in keywords):
                tags.append(label)
    return sorted(set(tags))

