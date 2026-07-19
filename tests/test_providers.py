"""Offline unit tests for providers.py — every network/subprocess touch is
stubbed. These cover the adapter logic the --mock gate cannot reach:
retry/backoff decisions, response parsing, parameter fallbacks, CLI cap-hit
detection and secret redaction."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import providers


def _resp(status=200, body=None, text=""):
    r = mock.MagicMock()
    r.status_code = status
    r.text = text or (json.dumps(body) if body is not None else "")
    if body is not None:
        r.json.return_value = body
    else:
        r.json.side_effect = ValueError("not json")
    return r


class TestRedact(unittest.TestCase):

    def test_env_value_and_patterns(self):
        with mock.patch.dict(os.environ,
                             {"OPENAI_API_KEY": "sk-live-abcdef123456789"}):
            s = providers.redact(
                "err sk-live-abcdef123456789 and Authorization: Bearer "
                "tok_1234567890 and AIzaSyD-aaaaaaaaaa and xai-abcdefgh1234")
            self.assertNotIn("sk-live-abcdef123456789", s)
            self.assertIn("[REDACTED:OPENAI_API_KEY]", s)
            self.assertNotIn("tok_1234567890", s)
            self.assertNotIn("AIzaSyD-aaaaaaaaaa", s)
            self.assertNotIn("xai-abcdefgh1234", s)

    def test_none_passthrough(self):
        self.assertIsNone(providers.redact(None))

    def test_callfailed_message_is_redacted(self):
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "xai-secret-99999"}):
            exc = providers.CallFailed("boom xai-secret-99999", 500, 2)
            self.assertNotIn("xai-secret-99999", str(exc))
            self.assertEqual(exc.http_status, 500)
            self.assertEqual(exc.retries, 2)


class TestPostJson(unittest.TestCase):

    def test_retries_on_429_then_succeeds(self):
        with mock.patch.object(providers.requests, "post") as post, \
                mock.patch.object(providers.time, "sleep") as slept:
            post.side_effect = [_resp(429, text="rate"),
                                _resp(200, {"ok": 1})]
            data, status, retries = providers._post_json(
                "http://x", {}, {}, max_retries=3)
            self.assertEqual(data, {"ok": 1})
            self.assertEqual(status, 200)
            self.assertEqual(retries, 1)
            self.assertEqual(slept.call_count, 1)

    def test_400_is_terminal_no_retry(self):
        with mock.patch.object(providers.requests, "post") as post:
            post.return_value = _resp(400, text="bad request")
            with self.assertRaises(providers.CallFailed) as cm:
                providers._post_json("http://x", {}, {}, max_retries=3)
            self.assertEqual(cm.exception.http_status, 400)
            self.assertEqual(post.call_count, 1)

    def test_2xx_non_json_body_fails(self):
        with mock.patch.object(providers.requests, "post") as post:
            post.return_value = _resp(200, body=None, text="<html>")
            with self.assertRaises(providers.CallFailed):
                providers._post_json("http://x", {}, {}, max_retries=0)

    def test_exhausted_retries_raise(self):
        with mock.patch.object(providers.requests, "post") as post, \
                mock.patch.object(providers.time, "sleep"):
            post.return_value = _resp(503, text="down")
            with self.assertRaises(providers.CallFailed) as cm:
                providers._post_json("http://x", {}, {}, max_retries=2)
            self.assertEqual(post.call_count, 3)
            self.assertIn("retries exhausted", str(cm.exception))


class TestOpenAIStyle(unittest.TestCase):

    OK = {"choices": [{"message": {"content": "hi"},
                       "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 5},
          "model": "m-1"}

    def _call(self):
        return providers._call_openai_style(
            "openai", "http://x", "m-1", "p", "s",
            temperature=0.7, max_tokens=100, max_retries=0, json_mode=False)

    def test_parses_usage_and_text(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-t-12345678"}), \
                mock.patch.object(providers, "_post_json",
                                  return_value=(self.OK, 200, 0)):
            rec = self._call()
            self.assertEqual(rec["text"], "hi")
            self.assertEqual(rec["input_tokens"], 10)
            self.assertEqual(rec["output_tokens"], 5)

    def test_max_completion_tokens_fallback(self):
        calls = []

        def fake(url, headers, payload, max_retries):
            calls.append(dict(payload))
            if "max_tokens" in payload:
                raise providers.CallFailed(
                    "use max_completion_tokens instead", 400, 0)
            return self.OK, 200, 0

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-t-12345678"}), \
                mock.patch.object(providers, "_post_json", side_effect=fake):
            rec = self._call()
            self.assertEqual(rec["param_note"],
                             "max_tokens renamed to max_completion_tokens")
            self.assertNotIn("max_tokens", calls[-1])
            self.assertEqual(calls[-1]["max_completion_tokens"], 100)

    def test_temperature_fallback(self):
        def fake(url, headers, payload, max_retries):
            if "temperature" in payload:
                raise providers.CallFailed(
                    "temperature not supported", 400, 0)
            return self.OK, 200, 0

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-t-12345678"}), \
                mock.patch.object(providers, "_post_json", side_effect=fake):
            rec = self._call()
            self.assertIn("temperature unsupported", rec["param_note"])

    def test_missing_key_fails_fast(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(providers.CallFailed):
                self._call()


class TestAnthropicAndGoogle(unittest.TestCase):

    def test_anthropic_parses_text_blocks(self):
        body = {"content": [{"type": "text", "text": "a"},
                            {"type": "tool_use"},
                            {"type": "text", "text": "b"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "model": "claude-x", "stop_reason": "end_turn"}
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_API_KEY": "sk-ant-12345678"}), \
                mock.patch.object(providers, "_post_json",
                                  return_value=(body, 200, 0)):
            rec = providers._call_anthropic_api(
                "claude-x", "p", "s", 0.7, 100, 0, False)
            self.assertEqual(rec["text"], "ab")
            self.assertEqual(rec["input_tokens"], 7)
            self.assertEqual(rec["stop_reason"], "end_turn")

    def test_google_parses_candidates(self):
        body = {"candidates": [{"content": {"parts": [{"text": "g"}]},
                                "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 4,
                                  "candidatesTokenCount": 2},
                "modelVersion": "gemini-x"}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaTest12345"}), \
                mock.patch.object(providers, "_post_json",
                                  return_value=(body, 200, 0)):
            rec = providers._call_google("gemini-x", "p", "s", 0.7, 100, 0,
                                         False)
            self.assertEqual(rec["text"], "g")
            self.assertEqual(rec["input_tokens"], 4)
            self.assertEqual(rec["output_tokens"], 2)

    def test_google_omitted_usage_is_null(self):
        body = {"candidates": [{"content": {"parts": [{"text": "g"}]}}]}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaTest12345"}), \
                mock.patch.object(providers, "_post_json",
                                  return_value=(body, 200, 0)):
            rec = providers._call_google("gemini-x", "p", "s", 0.7, 100, 0,
                                         False)
            self.assertIsNone(rec["input_tokens"])
            self.assertIsNone(rec["output_tokens"])


class TestClaudeCli(unittest.TestCase):

    def _proc(self, payload):
        p = mock.MagicMock()
        p.stdout = json.dumps(payload)
        p.stderr = ""
        p.returncode = 0
        return p

    def test_success_folds_cache_tokens_into_input(self):
        payload = {"result": "text", "is_error": False,
                   "total_cost_usd": 0.01, "stop_reason": "end_turn",
                   "usage": {"input_tokens": 10, "output_tokens": 5,
                             "cache_creation_input_tokens": 100,
                             "cache_read_input_tokens": 1000,
                             "iterations": [{}]}}
        with mock.patch.object(providers.subprocess, "run",
                               return_value=self._proc(payload)):
            rec = providers._call_claude_cli("sonnet", "p", "s", None, 4000,
                                             0, False)
            self.assertEqual(rec["input_tokens"], 1110)
            self.assertEqual(rec["output_tokens"], 5)
            self.assertEqual(rec["cli_reported_cost_usd"], 0.01)

    def test_cap_hit_multi_iteration_fails_loudly(self):
        payload = {"result": "only the tail", "is_error": False,
                   "usage": {"input_tokens": 10, "output_tokens": 4000,
                             "iterations": [{}, {}]}}
        with mock.patch.object(providers.subprocess, "run",
                               return_value=self._proc(payload)):
            with self.assertRaises(providers.CallFailed) as cm:
                providers._call_claude_cli("sonnet", "p", "s", None, 4000,
                                           0, False)
            self.assertIn("output-token cap", str(cm.exception))

    def test_is_error_is_terminal(self):
        payload = {"result": "overloaded", "is_error": True}
        with mock.patch.object(providers.subprocess, "run",
                               return_value=self._proc(payload)) as run:
            with self.assertRaises(providers.CallFailed):
                providers._call_claude_cli("sonnet", "p", "s", None, 4000,
                                           3, False)
            self.assertEqual(run.call_count, 1)

    def test_non_json_output_retries_then_fails(self):
        p = mock.MagicMock()
        p.stdout = "garbage"
        p.stderr = ""
        p.returncode = 1
        with mock.patch.object(providers.subprocess, "run",
                               return_value=p) as run:
            with self.assertRaises(providers.CallFailed):
                providers._call_claude_cli("sonnet", "p", "s", None, 4000,
                                           2, False)
            self.assertEqual(run.call_count, 3)


class TestDispatcher(unittest.TestCase):

    def test_unknown_provider(self):
        with self.assertRaises(providers.CallFailed):
            providers.call_model("nope", "m", "p", temperature=0,
                                 max_tokens=1, max_retries=0)

    def test_mock_missing_fixture(self):
        with self.assertRaises(providers.CallFailed):
            providers.call_model("mock", "m", "p", temperature=0,
                                 max_tokens=1, max_retries=0, mock=True,
                                 fixture_dir="/nonexistent", fixture="x.txt")


if __name__ == "__main__":
    unittest.main()
