"""Transport contract over the provider SDK: mapping, extraction, failure modes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import httpx2
import openai

import _models  # noqa: E402
import chat_completion as chat  # noqa: E402

OPENAI_ENDPOINT = "https://example.invalid/v1/chat/completions"
BOUND = {"direct_api": "fixture", "direct_api_base": OPENAI_ENDPOINT, "api_env": "TEST_API_KEY"}


def _openai_payload(content: str = "answer", finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }


def _client(payload: dict | Exception):
    """A stand-in SDK client whose raw-response create returns JSON text."""
    create = mock.Mock()
    if isinstance(payload, Exception):
        create.side_effect = payload
    else:
        create.return_value = SimpleNamespace(text=json.dumps(payload))
    raw = SimpleNamespace(create=create)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=raw))), create


def _status_error(cls, status: int, message: str):
    body = {"error": {"message": message}}
    request = httpx2.Request("POST", OPENAI_ENDPOINT)
    return cls("boom", response=httpx2.Response(status, request=request, json=body), body=body)


def _argv(*extra: str, endpoint: str = OPENAI_ENDPOINT) -> list[str]:
    return ["--endpoint", endpoint, "--api-model", "fixture", "--api-key-env", "TEST_API_KEY",
            "--prompt", "private prompt", *extra]


def _run(argv: list[str], client, entry: dict | None = None):
    """Run main() against a stand-in client. Returns exit, stdout, stderr, client kwargs."""
    made: dict = {}
    out, err = io.StringIO(), io.StringIO()
    binding = mock.patch.object(_models, "resolve", return_value=entry) if entry else contextlib.nullcontext()
    with mock.patch.object(chat, "_openai_client", side_effect=lambda **kw: made.update(kw) or client), \
            mock.patch.dict(os.environ, {"TEST_API_KEY": "secret"}), binding, \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = chat.main(argv)
    return code, out.getvalue(), err.getvalue(), made


class RequestMappingTest(unittest.TestCase):
    def test_openai_request_and_client_parameters(self) -> None:
        client, create = _client(_openai_payload())
        code, _, _, made = _run(
            _argv("--system", "be terse", "--max-tokens", "99",
                  "--extras-json", '{"thinking": {"type": "enabled"}}'), client)
        self.assertEqual(code, 0)
        self.assertEqual(made["base_url"], "https://example.invalid/v1")
        self.assertEqual((made["api_key"], made["timeout"], made["max_retries"]), ("secret", 120.0, 2))
        sent = create.call_args.kwargs
        self.assertEqual((sent["model"], sent["max_tokens"]), ("fixture", 99))
        self.assertEqual(sent["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(sent["messages"], [{"role": "system", "content": "be terse"},
                                            {"role": "user", "content": "private prompt"}])

    def test_openai_omits_max_tokens_when_uncapped(self) -> None:
        client, create = _client(_openai_payload())
        code, _, _, _ = _run(_argv("--max-tokens", "0"), client)
        self.assertEqual(code, 0)
        self.assertNotIn("max_tokens", create.call_args.kwargs)
        self.assertIsNone(create.call_args.kwargs["extra_body"])

    def test_provider_selection_and_base_url_stripping(self) -> None:
        for provider in ("", " OPENAI "):
            client, _ = _client(_openai_payload())
            code, _, _, made = _run(["--model", "bound", "--prompt", "hi"], client,
                                    entry=BOUND | {"direct_api_provider": provider})
            self.assertEqual((code, made["base_url"]), (0, "https://example.invalid/v1"))
        for endpoint, expected in (
            ("https://h/chat/completions", "https://h"),
            ("https://h/v1", "https://h/v1"),
            ("https://h/v1/chat/completions///", "https://h/v1"),
            ("https://h/chat/completions/chat/completions", "https://h/chat/completions"),
            ("https://h/chat/completions?key=value", "https://h/chat/completions?key=value"),
        ):
            client, _ = _client(_openai_payload())
            code, _, _, made = _run(_argv(endpoint=endpoint), client)
            self.assertEqual((code, made["base_url"]), (0, expected))

    def test_unknown_provider_is_a_config_error(self) -> None:
        client, _ = _client(_openai_payload())
        code, _, err, _ = _run(["--model", "bound", "--prompt", "hi"], client,
                               entry=BOUND | {"direct_api_provider": "gemini"})
        self.assertEqual(code, 2)
        self.assertIn("unknown provider 'gemini'", err)

    def test_removed_messages_api_binding_is_rejected(self) -> None:
        """A binding for the retired messages-API adapter is now an unsupported-provider config error."""
        client, _ = _client(_openai_payload())
        code, _, err, _ = _run(["--model", "bound", "--prompt", "hi"], client,
                               entry=BOUND | {"direct_api_provider": "messages-api"})
        self.assertEqual(code, 2)
        self.assertIn("unknown provider 'messages-api'", err)
        self.assertIn("must be one of openai", err)

    def test_binding_timeout_is_used_when_no_flag(self) -> None:
        client, _ = _client(_openai_payload())
        code, _, _, made = _run(["--model", "bound", "--prompt", "hi"], client,
                                entry=BOUND | {"direct_api_timeout": 240})
        self.assertEqual((code, made["timeout"]), (0, 240.0))

    def test_max_attempts_maps_to_sdk_retries(self) -> None:
        client, _ = _client(_openai_payload())
        for attempts, expected in ((3, 2), (1, 0), (0, 0)):
            _, _, _, made = _run(_argv("--max-attempts", str(attempts)), client)
            self.assertEqual(made["max_retries"], expected, attempts)

    def test_real_clients_carry_the_configured_retry_and_timeout(self) -> None:
        client = chat._openai_client(base_url="https://h", api_key="k", timeout=240.0, max_retries=2)
        self.assertEqual((client.max_retries, client.timeout), (2, 240.0))
        # The stand-in client above impersonates this call surface; assert it exists.
        self.assertTrue(hasattr(client.chat.completions.with_raw_response, "create"))


class ExtractionTest(unittest.TestCase):
    def test_success_writes_no_prompt_or_response_log(self) -> None:
        client, _ = _client(_openai_payload())
        with mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(Path, "open") as path_open, \
                mock.patch.object(Path, "write_text") as write_text:
            code, out, err, _ = _run(_argv(), client)
        self.assertEqual((code, out, err), (0, "answer", ""))
        mkdir.assert_not_called()
        path_open.assert_not_called()
        write_text.assert_not_called()

    def test_truncation_remains_caller_visible(self) -> None:
        client, _ = _client(_openai_payload("partial", "length"))
        code, out, err, _ = _run(_argv(), client)
        self.assertEqual((code, out), (5, "partial"))
        self.assertIn("response truncated", err)

    def test_empty_completion_fails(self) -> None:
        client, _ = _client(_openai_payload("", "length"))
        code, out, err, _ = _run(_argv("--max-tokens", "0"), client)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("empty completion", err)

    def test_json_mode_emits_text_and_usage(self) -> None:
        client, _ = _client(_openai_payload())
        code, out, _, _ = _run(_argv("--json"), client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["usage"], {"prompt_tokens": 2, "completion_tokens": 1})

    def test_session_round_trip(self) -> None:
        client, create = _client(_openai_payload())
        with TemporaryDirectory() as tmp:
            session = Path(tmp) / "s.json"
            session.write_text(json.dumps([{"role": "user", "content": "earlier"}]), encoding="utf-8")
            code, _, _, _ = _run(_argv("--session", str(session)), client)
            saved = json.loads(session.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(create.call_args.kwargs["messages"][0], {"role": "user", "content": "earlier"})
        self.assertEqual(saved[-1], {"role": "assistant", "content": "answer"})


class FailureTest(unittest.TestCase):
    def test_malformed_response_reports_missing_fields(self) -> None:
        code, _, err, _ = _run(_argv(), _client({"choices": []})[0])
        self.assertEqual(code, 1)
        self.assertIn("malformed response", err)

    def test_non_json_body_is_an_api_error(self) -> None:
        client, create = _client(_openai_payload())
        create.return_value = SimpleNamespace(text="<html>gateway</html>")
        code, _, err, _ = _run(_argv(), client)
        self.assertEqual(code, 1)
        self.assertIn("response not JSON", err)

    def test_status_errors_report_code_and_body_without_a_script_side_loop(self) -> None:
        for cls, status, detail in ((openai.AuthenticationError, 401, "bad key"),
                                    (openai.RateLimitError, 429, "slow down")):
            client, create = _client(_status_error(cls, status, detail))
            code, _, err, _ = _run(_argv(), client)
            self.assertEqual(code, 1)
            self.assertIn(f"HTTP {status}", err)
            self.assertIn(detail, err)
            # The SDK owns retry policy (and never retries 401); the script adds no loop.
            self.assertEqual(create.call_count, 1)

    def test_timeout_and_connection_errors_split_exit_codes(self) -> None:
        request = httpx2.Request("POST", OPENAI_ENDPOINT)
        client, _ = _client(openai.APITimeoutError(request=request))
        code, _, err, _ = _run(_argv("--timeout", "7"), client)
        self.assertEqual(code, 3)
        self.assertIn("timed out after 7.0s", err)
        client, _ = _client(openai.APIConnectionError(message="unreachable", request=request))
        code, _, err, _ = _run(_argv(), client)
        self.assertEqual(code, 1)
        self.assertIn("network error", err)


class BindingResolutionTest(unittest.TestCase):
    def test_bindings_overlay_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            schema, bindings = Path(tmp) / "models.toml", Path(tmp) / "profile.toml"
            schema.write_text('[models.demo]\nreasoning_tier = "deep"\n[models.unbound]\n', encoding="utf-8")
            bindings.write_text(
                '[models.demo]\ndirect_api = "demo-v1"\ndirect_api_base = "https://h/chat/completions"\n'
                'api_env = "DEMO_KEY"\ndirect_api_extras = { thinking = { type = "enabled" } }\n',
                encoding="utf-8")
            # the overlay moved into the shared resolver; patch it there
            with mock.patch.object(_models, "SCHEMA_TOML", schema), mock.patch.object(_models, "BINDINGS_TOML", bindings):
                entry, unbound = _models.resolve("demo"), _models.resolve("unbound")
                self.assertIsNone(_models.resolve("absent"))
        self.assertEqual(entry["reasoning_tier"], "deep")
        self.assertEqual((entry["direct_api"], entry["api_env"]), ("demo-v1", "DEMO_KEY"))
        self.assertEqual(chat._resolve_extras(entry, None), {"thinking": {"type": "enabled"}})
        self.assertEqual(chat._resolve_extras(entry, '{"thinking": 1}'), {"thinking": 1})
        self.assertEqual(unbound, {})

    def test_committed_identities_resolve(self) -> None:
        self.assertIsNotNone(_models.resolve("deepseek_pro"))
        self.assertIsNone(_models.resolve("no_such_identity"))


if __name__ == "__main__":
    unittest.main()
