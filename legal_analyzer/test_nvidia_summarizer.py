from __future__ import annotations

import socket
import subprocess
import unittest
import urllib.error
import os
from unittest.mock import MagicMock, patch

from .nvidia_summarizer import NvidiaSummaryError, call_chat_completions, get_chat_completions_url


class NvidiaSummarizerTests(unittest.TestCase):
    def test_call_chat_completions_falls_back_to_curl_on_dns_error(self) -> None:
        payload = {"model": "m", "messages": []}
        response = '{"choices":[{"message":{"content":"{}"}}]}\n__CODEX_HTTP_STATUS__:200'
        with (
            patch(
                "legal_analyzer.nvidia_summarizer.urllib.request.urlopen",
                side_effect=urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known")),
            ),
            patch(
                "legal_analyzer.nvidia_summarizer.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["curl"], returncode=0, stdout=response, stderr=""),
            ) as run_mock,
        ):
            result = call_chat_completions(payload, "secret", timeout_seconds=12)

        self.assertEqual(result, {"choices": [{"message": {"content": "{}"}}]})
        run_mock.assert_called_once()
        args = run_mock.call_args.args[0]
        self.assertIn("curl", args[0])
        self.assertIn("--max-time", args)
        self.assertIn("12", args)

    def test_call_chat_completions_keeps_non_dns_url_error(self) -> None:
        payload = {"model": "m", "messages": []}
        with (
            patch(
                "legal_analyzer.nvidia_summarizer.urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ),
            patch("legal_analyzer.nvidia_summarizer.subprocess.run") as run_mock,
        ):
            with self.assertRaises(NvidiaSummaryError) as error:
                call_chat_completions(payload, "secret")

        self.assertIn("connection refused", str(error.exception))
        run_mock.assert_not_called()

    def test_call_chat_completions_curl_http_error_preserves_detail(self) -> None:
        payload = {"model": "m", "messages": []}
        http_error_body = '{"error":{"message":"invalid api key"}}\n__CODEX_HTTP_STATUS__:401'
        with (
            patch(
                "legal_analyzer.nvidia_summarizer.urllib.request.urlopen",
                side_effect=urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known")),
            ),
            patch(
                "legal_analyzer.nvidia_summarizer.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["curl"], returncode=0, stdout=http_error_body, stderr=""),
            ),
        ):
            with self.assertRaises(NvidiaSummaryError) as error:
                call_chat_completions(payload, "secret")

        self.assertIn("HTTP 401", str(error.exception))
        self.assertIn("invalid api key", str(error.exception))

    def test_call_chat_completions_rejects_invalid_endpoint_url(self) -> None:
        payload = {"model": "m", "messages": []}
        with (
            patch.dict(os.environ, {"NVIDIA_CHAT_COMPLETIONS_URL": "https:///broken"}, clear=False),
            patch("legal_analyzer.nvidia_summarizer.urllib.request.urlopen") as urlopen_mock,
            patch("legal_analyzer.nvidia_summarizer.subprocess.run") as run_mock,
        ):
            with self.assertRaises(NvidiaSummaryError) as error:
                call_chat_completions(payload, "secret")

        self.assertIn("El endpoint de NVIDIA no es valido", str(error.exception))
        urlopen_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_get_chat_completions_url_prefers_explicit_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NVIDIA_CHAT_COMPLETIONS_URL": "https://proxy.example.com/custom/chat",
                "NVIDIA_API_BASE_URL": "https://ignored.example.com",
            },
            clear=False,
        ):
            self.assertEqual(get_chat_completions_url(), "https://proxy.example.com/custom/chat")

    def test_get_chat_completions_url_builds_from_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {"NVIDIA_API_BASE_URL": "https://proxy.example.com/"},
            clear=False,
        ):
            with patch.dict(os.environ, {"NVIDIA_CHAT_COMPLETIONS_URL": ""}, clear=False):
                self.assertEqual(
                    get_chat_completions_url(),
                    "https://proxy.example.com/v1/chat/completions",
                )


if __name__ == "__main__":
    unittest.main()
