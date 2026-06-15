from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlparse
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
    timeout_seconds: int = 180,
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
    response = call_chat_completions(payload, api_key, timeout_seconds=timeout_seconds)
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


def get_chat_completions_url() -> str:
    explicit_url = os.environ.get("NVIDIA_CHAT_COMPLETIONS_URL", "").strip()
    if explicit_url:
        return explicit_url
    base_url = os.environ.get("NVIDIA_API_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}/v1/chat/completions"
    return CHAT_COMPLETIONS_URL


def validate_chat_completions_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise NvidiaSummaryError(f"El endpoint de NVIDIA no es valido: {url}")


def call_chat_completions(payload: dict[str, Any], api_key: str, timeout_seconds: int = 180) -> dict[str, Any]:
    chat_completions_url = get_chat_completions_url()
    validate_chat_completions_url(chat_completions_url)
    request = urllib.request.Request(
        chat_completions_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise NvidiaSummaryError(build_http_error_message(exc.code, exc.read().decode("utf-8", errors="replace"))) from exc
    except urllib.error.URLError as exc:
        if is_dns_resolution_error(exc.reason):
            return call_chat_completions_with_curl(
                payload,
                api_key,
                chat_completions_url,
                timeout_seconds=timeout_seconds,
            )
        raise NvidiaSummaryError(f"No se pudo conectar con NVIDIA: {exc.reason}") from exc
    except TimeoutError as exc:
        raise NvidiaSummaryError("La solicitud a NVIDIA excedio el tiempo de espera.") from exc


def call_chat_completions_with_curl(
    payload: dict[str, Any],
    api_key: str,
    chat_completions_url: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    marker = "__CODEX_HTTP_STATUS__:"
    completed = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            chat_completions_url,
            "-H",
            f"Authorization: Bearer {api_key}",
            "-H",
            "Content-Type: application/json",
            "--max-time",
            str(timeout_seconds),
            "--data-binary",
            json.dumps(payload),
            "--write-out",
            f"\n{marker}%{{http_code}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "curl fallo sin detalle."
        raise NvidiaSummaryError(f"No se pudo conectar con NVIDIA: {detail}")

    body, _, status_text = completed.stdout.rpartition(f"\n{marker}")
    if not status_text:
        raise NvidiaSummaryError("NVIDIA devolvio una respuesta incompleta.")
    try:
        status_code = int(status_text.strip())
    except ValueError as exc:
        raise NvidiaSummaryError("NVIDIA devolvio un codigo HTTP invalido.") from exc
    if status_code >= 400:
        raise NvidiaSummaryError(build_http_error_message(status_code, body))
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise NvidiaSummaryError("NVIDIA devolvio JSON invalido.") from exc


def is_dns_resolution_error(reason: object) -> bool:
    if isinstance(reason, socket.gaierror):
        return True
    text = str(reason).lower()
    return "nodename nor servname provided" in text or "name or service not known" in text


def is_nvidia_connectivity_error(message: str) -> bool:
    text = message.lower()
    return (
        "no se pudo resolver el host de nvidia" in text
        or "could not resolve host" in text
        or "nodename nor servname provided" in text
        or "name or service not known" in text
    )


def build_http_error_message(status_code: int, body_text: str) -> str:
    message = f"NVIDIA API devolvio HTTP {status_code}."
    try:
        body = json.loads(body_text)
        detail = body.get("detail") or body.get("message")
        if isinstance(body.get("error"), dict):
            detail = body["error"].get("message") or detail
        if detail:
            message = f"{message} {detail}"
    except json.JSONDecodeError:
        pass
    return message


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
