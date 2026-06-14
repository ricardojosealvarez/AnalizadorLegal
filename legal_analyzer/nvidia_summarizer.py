from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .llm_summarizer import (
    ROOT,
    build_evidence,
    clean_items,
    clean_text,
    load_env_file,
    make_input,
    parse_json_text,
)

DEFAULT_MODEL = "mistralai/mistral-medium-3.5-128b"
CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


class NvidiaSummaryError(RuntimeError):
    pass


def summarize_with_nvidia(
    chunks: list[dict[str, Any]],
    metadata: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    api_key = get_api_key()
    selected_model = model or os.environ.get("NVIDIA_SUMMARY_MODEL", DEFAULT_MODEL)
    evidence = build_evidence(chunks)
    if not evidence:
        raise NvidiaSummaryError("El dictamen no contiene texto suficiente para resumir.")

    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Eres un jurista especializado en proteccion de datos. Resume dictamenes de "
                    "la AEPD con precision y lenguaje claro. Usa exclusivamente el texto "
                    "facilitado. No inventes hechos, articulos ni conclusiones. Conserva las "
                    "condiciones, excepciones y matices que puedan cambiar el sentido juridico. "
                    "Responde solo con un objeto JSON valido con las claves overview, key_points "
                    "y conclusions. key_points y conclusions deben ser listas de textos."
                ),
            },
            {"role": "user", "content": make_input(metadata, evidence)},
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 1800,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    response = call_chat_completions(payload, api_key)
    result = parse_completion(response)
    return {
        "overview": clean_text(result["overview"]),
        "key_points": clean_items(result["key_points"], 6),
        "conclusions": clean_items(result["conclusions"], 5),
        "method": f"nvidia:{selected_model}",
        "provider": "nvidia",
        "model": selected_model,
    }


def get_api_key() -> str:
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if api_key:
        return api_key
    load_env_file(ROOT / ".env.local")
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise NvidiaSummaryError("NVIDIA_API_KEY no esta configurada.")
    return api_key


def call_chat_completions(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        CHAT_COMPLETIONS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = f"NVIDIA API devolvio HTTP {exc.code}."
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("detail") or body.get("message")
            if isinstance(body.get("error"), dict):
                detail = body["error"].get("message") or detail
            if detail:
                message = f"{message} {detail}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise NvidiaSummaryError(message) from exc
    except urllib.error.URLError as exc:
        raise NvidiaSummaryError(f"No se pudo conectar con NVIDIA: {exc.reason}") from exc
    except TimeoutError as exc:
        raise NvidiaSummaryError("La solicitud a NVIDIA excedio el tiempo de espera.") from exc


def parse_completion(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices", [])
    if not choices:
        raise NvidiaSummaryError("NVIDIA no devolvio un resumen util.")
    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise NvidiaSummaryError("NVIDIA no devolvio un resumen util.")
    try:
        return parse_json_text(strip_code_fence(content))
    except Exception as exc:  # noqa: BLE001 - convert provider format errors.
        raise NvidiaSummaryError(str(exc)) from exc


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()
