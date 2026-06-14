from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gpt-5.4-mini"
RESPONSES_URL = "https://api.openai.com/v1/responses"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 6,
        },
        "conclusions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 5,
        },
    },
    "required": ["overview", "key_points", "conclusions"],
    "additionalProperties": False,
}


class LLMSummaryError(RuntimeError):
    pass


def summarize_with_openai(
    chunks: list[dict[str, Any]],
    metadata: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    api_key = get_api_key()
    selected_model = model or os.environ.get("OPENAI_SUMMARY_MODEL", DEFAULT_MODEL)
    evidence = build_evidence(chunks)
    if not evidence:
        raise LLMSummaryError("El dictamen no contiene texto suficiente para resumir.")

    payload = {
        "model": selected_model,
        "instructions": (
            "Eres un jurista especializado en proteccion de datos. Resume dictamenes de la AEPD "
            "con precision y lenguaje claro. Usa exclusivamente el texto facilitado. No inventes "
            "hechos, articulos ni conclusiones. Distingue el objeto de la consulta, los criterios "
            "juridicos principales y la conclusion efectiva. Conserva condiciones, excepciones y "
            "matices que puedan cambiar el sentido juridico."
        ),
        "input": make_input(metadata, evidence),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "aepd_legal_summary",
                "strict": True,
                "schema": SUMMARY_SCHEMA,
            }
        },
        "max_output_tokens": 1800,
    }
    response = call_responses_api(payload, api_key)
    result = parse_structured_output(response)
    return {
        "overview": clean_text(result["overview"]),
        "key_points": clean_items(result["key_points"], 6),
        "conclusions": clean_items(result["conclusions"], 5),
        "method": f"openai:{selected_model}",
    }


def get_api_key() -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        return api_key
    load_env_file(ROOT / ".env.local")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise LLMSummaryError("OPENAI_API_KEY no esta configurada.")
    return api_key


def load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("\"'")


def build_evidence(chunks: list[dict[str, Any]], max_chars: int = 18000) -> str:
    sections: list[str] = []
    used = 0
    for chunk in chunks:
        text = " ".join(str(chunk.get("text", "")).split())
        if not text:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        excerpt = text[: min(2800, remaining)]
        chunk_number = int(chunk.get("chunk_index", 0)) + 1
        sections.append(f"[Fragmento {chunk_number}]\n{excerpt}")
        used += len(excerpt)
    return "\n\n".join(sections)


def make_input(metadata: dict[str, Any], evidence: str) -> str:
    header = (
        f"Referencia: {metadata.get('reference', '')}\n"
        f"Titulo: {metadata.get('title', '')}\n"
        f"Anio: {metadata.get('year') or 'sin fecha'}\n"
        f"Materia clasificada: {metadata.get('materia', '')}\n"
        f"Base legal clasificada: {metadata.get('base_legal', '')}\n"
        f"Regimen clasificado: {metadata.get('regimen', '')}"
    )
    return (
        f"{header}\n\n"
        "Elabora un resumen juridico autocontenido. En los puntos principales identifica las "
        "cuestiones analizadas y el razonamiento. En las conclusiones expresa la posicion final "
        "de la AEPD y sus condiciones. Si el texto no permite afirmar algo, indicalo.\n\n"
        f"TEXTO DEL DICTAMEN:\n{evidence}"
    )


def call_responses_api(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = f"OpenAI API devolvio HTTP {exc.code}."
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("error", {}).get("message")
            if detail:
                detail_lower = detail.lower()
                if exc.code == 429 and ("current quota" in detail_lower or "billing" in detail_lower):
                    message = "El proyecto de OpenAI no tiene cuota o saldo API disponible."
                elif exc.code == 401:
                    message = "La clave de OpenAI no es valida o ya no esta activa."
                else:
                    message = f"{message} {detail}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise LLMSummaryError(message) from exc
    except urllib.error.URLError as exc:
        raise LLMSummaryError(f"No se pudo conectar con OpenAI: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMSummaryError("La solicitud a OpenAI excedio el tiempo de espera.") from exc


def parse_structured_output(response: dict[str, Any]) -> dict[str, Any]:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return parse_json_text(output_text)

    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise LLMSummaryError("El modelo rechazo elaborar el resumen.")
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return parse_json_text(text)
    raise LLMSummaryError("OpenAI no devolvio un resumen util.")


def parse_json_text(text: str) -> dict[str, Any]:
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMSummaryError("OpenAI devolvio un resumen con formato invalido.") from exc
    if not isinstance(result, dict):
        raise LLMSummaryError("OpenAI devolvio un resumen con formato invalido.")
    required = ("overview", "key_points", "conclusions")
    if not all(key in result for key in required):
        raise LLMSummaryError("El resumen de OpenAI esta incompleto.")
    return result


def clean_text(value: Any) -> str:
    return " ".join(str(value).split()).strip()


def clean_items(values: Any, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [clean_text(value) for value in values if clean_text(value)][:limit]
