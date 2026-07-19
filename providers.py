"""argbench provider adapters.

Raw HTTPS to each provider (requests only, no SDKs), plus a claude-CLI
executor and an offline mock provider. Every adapter returns the same
record shape via _record(). Secrets are read from the environment inside
this module only, are never written to disk, and every error/log path is
passed through redact().
"""

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

import requests

# ---------------------------------------------------------------------------
# secrets & redaction
# ---------------------------------------------------------------------------

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
}

HTTP_TIMEOUT_S = 240          # per HTTP attempt
CLI_TIMEOUT_S = 600           # per claude-CLI attempt


def _key_for(provider):
    return os.environ.get(KEY_ENV.get(provider, ""), "")


def redact(text):
    """Scrub secret values and auth-header patterns from any string.

    Applied to every error message, log line and saved artifact that could
    conceivably contain request metadata. Belt and braces: exact env values
    first, then common header/token shapes.
    """
    if text is None:
        return None
    s = str(text)
    for env_name in KEY_ENV.values():
        v = os.environ.get(env_name)
        if v:
            s = s.replace(v, "[REDACTED:" + env_name + "]")
    s = re.sub(r"(?i)(authorization\s*[:=]\s*)\S[^\r\n]*", r"\1[REDACTED]", s)
    s = re.sub(r"(?i)(x-api-key\s*[:=]\s*)\S+", r"\1[REDACTED]", s)
    s = re.sub(r"(?i)(x-goog-api-key\s*[:=]\s*)\S+", r"\1[REDACTED]", s)
    s = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}", "Bearer [REDACTED]", s)
    s = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "[REDACTED]", s)
    s = re.sub(r"\bAIza[A-Za-z0-9_\-]{10,}", "[REDACTED]", s)
    s = re.sub(r"\bxai-[A-Za-z0-9_\-]{8,}", "[REDACTED]", s)
    return s


class CallFailed(Exception):
    """Terminal call failure after retries. Message is already redacted."""

    def __init__(self, message, http_status=None, retries=0):
        super().__init__(redact(message))
        self.http_status = http_status
        self.retries = retries


def _record(text, input_tokens, output_tokens, model, latency_ms,
            http_status, retries, **extra):
    rec = {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "retries": retries,
    }
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# HTTP with retries
# ---------------------------------------------------------------------------

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 522, 524, 529}


def _post_json(url, headers, payload, max_retries):
    """POST with exponential backoff + jitter on 429/5xx/timeouts.

    Returns (parsed_json, http_status, retries_used). Raises CallFailed
    after max_retries additional attempts. Never logs headers.
    """
    last_err = "unknown error"
    last_status = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=HTTP_TIMEOUT_S)
            last_status = resp.status_code
            if resp.status_code < 400:
                try:
                    return resp.json(), resp.status_code, attempt
                except ValueError:
                    last_err = "HTTP %d but response body was not JSON: %s" % (
                        resp.status_code, resp.text[:300])
                    raise CallFailed(last_err, resp.status_code, attempt)
            body = resp.text[:500]
            last_err = "HTTP %d from %s: %s" % (resp.status_code, url, body)
            if resp.status_code not in RETRYABLE_STATUS:
                raise CallFailed(last_err, resp.status_code, attempt)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = "network error calling %s: %s" % (url, type(exc).__name__)
            last_status = None
        if attempt < max_retries:
            base = 2.0 ** attempt
            time.sleep(base + random.uniform(0, base / 2))
    raise CallFailed("retries exhausted (%d attempts): %s"
                     % (max_retries + 1, last_err), last_status, max_retries)


def _get_json(url, headers, max_retries=1):
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code not in RETRYABLE_STATUS or attempt == max_retries:
                raise CallFailed("HTTP %d from %s: %s"
                                 % (resp.status_code, url, resp.text[:300]),
                                 resp.status_code, attempt)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_retries:
                raise CallFailed("network error calling %s: %s"
                                 % (url, type(exc).__name__), None, attempt)
        time.sleep(2.0 ** attempt)
    raise CallFailed("unreachable", None, max_retries)


# ---------------------------------------------------------------------------
# provider adapters
# ---------------------------------------------------------------------------

def _call_anthropic_api(model, prompt, system, temperature, max_tokens,
                        max_retries, json_mode):
    key = _key_for("anthropic")
    if not key:
        raise CallFailed("ANTHROPIC_API_KEY not set (executor=api)")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    t0 = time.monotonic()
    data, status, retries = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "content-type": "application/json"},
        payload, max_retries)
    latency = int((time.monotonic() - t0) * 1000)
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    usage = data.get("usage") or {}
    return _record(text, usage.get("input_tokens"), usage.get("output_tokens"),
                   data.get("model", model), latency, status, retries,
                   stop_reason=data.get("stop_reason"))


def _call_claude_cli(model, prompt, system, temperature, max_tokens,
                     max_retries, json_mode):
    """Shell out to `claude -p` headless mode on the user's existing auth.

    Flags verified against claude CLI 2.1.211:
      -p / --print               non-interactive, print result and exit
      --output-format json      single JSON result object on stdout
      --model <id>              model selection (alias or full id)
      --tools ""                disable ALL built-in tools
      --no-session-persistence  do not write a resumable session
      --system-prompt <s>       replace the default (large) system prompt
    max_tokens is enforced via CLAUDE_CODE_MAX_OUTPUT_TOKENS. The CLI does
    NOT truncate at the cap the way the raw API does: it either errors
    (is_error) or internally continues across multiple iterations and
    returns ONLY the final segment in `result` — silently losing the
    earlier text. Both cases are treated as a loud FAILURE here (checked
    via usage.iterations), never as usable content. Set max_tokens
    generously with this executor, or use executor=api for true
    truncation semantics. temperature is NOT settable through the CLI;
    it is recorded as null in call params.
    Retries here cover subprocess timeouts and unparseable output only —
    the CLI already retries transient API errors internally.
    """
    cmd = ["claude", "-p", "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--strict-mcp-config",
           "--model", model, "--system-prompt", system]
    env = dict(os.environ)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    # Isolation: run from a fresh empty directory so the CLI cannot pick up
    # CLAUDE.md, project memory, or a repo's git status from wherever
    # argbench was invoked; --strict-mcp-config (with no --mcp-config given)
    # disables all configured MCP servers. A small CLI-injected preamble
    # still exists and is NOT captured in the saved prompt files — the
    # report carries a caveat; use executor=api for strict reproducibility.
    workdir = tempfile.mkdtemp(prefix="argbench-cli-")
    try:
        return _run_claude_cli(cmd, prompt, model, max_tokens, max_retries,
                               env, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_claude_cli(cmd, prompt, model, max_tokens, max_retries, env,
                    workdir):
    last_err = "unknown error"
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True,
                                  text=True, timeout=CLI_TIMEOUT_S, env=env,
                                  cwd=workdir)
        except subprocess.TimeoutExpired:
            last_err = "claude CLI timed out after %ds" % CLI_TIMEOUT_S
            continue
        latency = int((time.monotonic() - t0) * 1000)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            last_err = ("claude CLI returned non-JSON output (exit %s): %s"
                        % (proc.returncode, (proc.stdout or proc.stderr)[:300]))
            continue
        if data.get("is_error"):
            # terminal: the CLI has already done its own API retries
            raise CallFailed("claude CLI error: %s" % data.get("result", ""),
                             None, attempt)
        usage = data.get("usage") or {}
        iterations = usage.get("iterations") or []
        if len(iterations) > 1:
            # the response hit the output-token cap and the CLI continued
            # internally; `result` holds only the last segment, so the
            # full text is unrecoverable — fail loudly, never save a
            # silent fragment
            raise CallFailed(
                "claude CLI response hit the %d output-token cap and was "
                "internally continued across %d iterations; only the final "
                "segment is returned, so the complete text is lost. Marking "
                "FAILED — raise max_tokens for this executor or use "
                "executor=api." % (max_tokens, len(iterations)),
                None, attempt)
        in_tok = usage.get("input_tokens")
        cache_create = usage.get("cache_creation_input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        if in_tok is not None:
            in_tok = in_tok + cache_create + cache_read
        return _record(data.get("result", ""), in_tok,
                       usage.get("output_tokens"), model, latency, None,
                       attempt,
                       stop_reason=data.get("stop_reason"),
                       cli_reported_cost_usd=data.get("total_cost_usd"),
                       cache_creation_input_tokens=cache_create,
                       cache_read_input_tokens=cache_read)
    raise CallFailed("retries exhausted (%d attempts): %s"
                     % (max_retries + 1, last_err), None, max_retries)


def _call_openai_style(provider, base_url, model, prompt, system, temperature,
                       max_tokens, max_retries, json_mode):
    """OpenAI-compatible chat/completions: openai, deepseek, mistral, xai."""
    key = _key_for(provider)
    if not key:
        raise CallFailed("%s not set" % KEY_ENV[provider])
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": "Bearer " + key,
               "Content-Type": "application/json"}
    t0 = time.monotonic()
    param_note = None
    try:
        data, status, retries = _post_json(base_url, headers, payload,
                                           max_retries)
    except CallFailed as exc:
        # Compatibility fallback for newer OpenAI models that reject
        # max_tokens / non-default temperature. Deterministic, single
        # re-issue per rejected parameter, recorded in the call params.
        msg = str(exc)
        if exc.http_status == 400 and "max_completion_tokens" in msg:
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = max_tokens
            param_note = "max_tokens renamed to max_completion_tokens"
            try:
                data, status, retries = _post_json(base_url, headers, payload,
                                                   max_retries)
            except CallFailed as exc2:
                if exc2.http_status == 400 and "temperature" in str(exc2):
                    payload.pop("temperature", None)
                    param_note += "; temperature unsupported, provider default used"
                    data, status, retries = _post_json(base_url, headers,
                                                       payload, max_retries)
                else:
                    raise
        elif exc.http_status == 400 and "temperature" in msg:
            payload.pop("temperature", None)
            param_note = "temperature unsupported, provider default used"
            data, status, retries = _post_json(base_url, headers, payload,
                                               max_retries)
        else:
            raise
    latency = int((time.monotonic() - t0) * 1000)
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return _record(text, usage.get("prompt_tokens"),
                   usage.get("completion_tokens"),
                   data.get("model", model), latency, status, retries,
                   finish_reason=choice.get("finish_reason"),
                   param_note=param_note)


def _call_google(model, prompt, system, temperature, max_tokens,
                 max_retries, json_mode):
    key = _key_for("google")
    if not key:
        raise CallFailed("GEMINI_API_KEY not set")
    # key goes in a header, never in the URL, so it cannot leak via
    # error messages that embed the URL
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent" % model)
    gen_cfg = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    t0 = time.monotonic()
    data, status, retries = _post_json(
        url, {"x-goog-api-key": key, "Content-Type": "application/json"},
        payload, max_retries)
    latency = int((time.monotonic() - t0) * 1000)
    cands = data.get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    text = "".join(p.get("text", "") for p in parts)
    usage = data.get("usageMetadata") or {}
    return _record(text, usage.get("promptTokenCount"),
                   usage.get("candidatesTokenCount"),
                   data.get("modelVersion", model), latency, status, retries,
                   finish_reason=(cands[0].get("finishReason") if cands else None))


# ---------------------------------------------------------------------------
# mock provider (offline, deterministic, zero network)
# ---------------------------------------------------------------------------

def _call_mock(fixture_dir, fixture, prompt):
    path = os.path.join(fixture_dir, fixture)
    if not os.path.exists(path):
        raise CallFailed("mock fixture missing: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    # deterministic pseudo-usage: 1 token per 4 characters, exact same
    # numbers on every run so the mock gate arithmetic is checkable by hand
    return _record(text, len(prompt) // 4, len(text) // 4, "mock-model",
                   0, 200, 0)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------

OPENAI_STYLE_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
}

SYSTEM_PROMPT = ("You are a careful analyst. Follow the instructions in the "
                 "user message exactly. Output only what is asked for.")


def call_model(provider, model, prompt, *, temperature, max_tokens,
               max_retries, json_mode=False, executor="api",
               mock=False, fixture_dir=None, fixture=None):
    """Uniform entry point. Returns the adapter record; raises CallFailed."""
    if mock:
        return _call_mock(fixture_dir, fixture, prompt)
    if provider == "anthropic":
        if executor == "claude_cli":
            return _call_claude_cli(model, prompt, SYSTEM_PROMPT, temperature,
                                    max_tokens, max_retries, json_mode)
        return _call_anthropic_api(model, prompt, SYSTEM_PROMPT, temperature,
                                   max_tokens, max_retries, json_mode)
    if provider == "google":
        return _call_google(model, prompt, SYSTEM_PROMPT, temperature,
                            max_tokens, max_retries, json_mode)
    if provider in OPENAI_STYLE_URLS:
        return _call_openai_style(provider, OPENAI_STYLE_URLS[provider], model,
                                  prompt, SYSTEM_PROMPT, temperature,
                                  max_tokens, max_retries, json_mode)
    raise CallFailed("unknown provider: %s" % provider)


# ---------------------------------------------------------------------------
# list-models endpoints
# ---------------------------------------------------------------------------

def list_models(provider):
    """Return a list of model id strings, or raise CallFailed."""
    key = _key_for(provider)
    if not key:
        raise CallFailed("%s not set" % KEY_ENV.get(provider, provider))
    if provider == "anthropic":
        data = _get_json("https://api.anthropic.com/v1/models?limit=100",
                         {"x-api-key": key,
                          "anthropic-version": "2023-06-01"})
        return [m.get("id") for m in data.get("data", [])]
    if provider == "google":
        data = _get_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            {"x-goog-api-key": key})
        return [m.get("name", "").replace("models/", "")
                for m in data.get("models", [])]
    urls = {
        "openai": "https://api.openai.com/v1/models",
        "deepseek": "https://api.deepseek.com/models",
        "mistral": "https://api.mistral.ai/v1/models",
        "xai": "https://api.x.ai/v1/models",
    }
    if provider not in urls:
        raise CallFailed("unknown provider: %s" % provider)
    data = _get_json(urls[provider], {"Authorization": "Bearer " + key})
    return [m.get("id") for m in data.get("data", [])]
