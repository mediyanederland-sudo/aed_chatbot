"""LLM provider abstraction for Anthropic, OpenAI, Gemini, and Groq.

Environment settings select query and chat models. Provider-specific helpers
handle structured responses, generation settings, and transient-error fallbacks."""

import os
import json
import time
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude")
LLM_MODEL_QUERY = os.getenv("LLM_MODEL_QUERY", "claude-haiku-4-5-20251001")
LLM_MODEL_CHAT = os.getenv("LLM_MODEL_CHAT", "claude-sonnet-4-6")
LLM_MODEL_GEMINI_FALLBACK = os.getenv("LLM_MODEL_GEMINI_FALLBACK", "gemini-2.5-flash-lite")
LLM_MODEL_GROQ_FALLBACK = os.getenv("LLM_MODEL_GROQ_FALLBACK", "llama-3.3-70b-versatile")

# Apply optional thinking settings per model family; unset values retain provider defaults.


GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "-1"))
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()


def _gemini_config(model, config):
    """Per-model generation config for one Gemini call (primary or fallback
    model — the retry chain mixes generations). Gemini 3 keeps its default
    temperature (Google: lowering it can cause looping / degraded output);
    only the 2.5 Flash family accepts a budget of 0 (2.5 Pro rejects it)."""
    config = dict(config)
    thinking = None
    if model.startswith("gemini-3"):
        config.pop("temperature", None)
        if GEMINI_THINKING_LEVEL:
            thinking = {"thinking_level": GEMINI_THINKING_LEVEL}
    elif model.startswith("gemini-2.5-flash") and GEMINI_THINKING_BUDGET >= 0:
        thinking = {"thinking_budget": GEMINI_THINKING_BUDGET}
    if thinking:
        config["thinking_config"] = thinking
    return config


# Provider clients (lazy-loaded)


_clients = {}


def _get_claude_client():
    if "claude" not in _clients:
        from anthropic import Anthropic
        _clients["claude"] = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _clients["claude"]


def _get_openai_client():
    if "openai" not in _clients:
        from openai import OpenAI
        _clients["openai"] = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _clients["openai"]


def _get_gemini_client():
    if "gemini" not in _clients:
        from google import genai
        _clients["gemini"] = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _clients["gemini"]


def _get_groq_client():
    if "groq" not in _clients:
        from groq import Groq
        _clients["groq"] = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _clients["groq"]


def _has_groq():
    key = os.getenv("GROQ_API_KEY", "").strip()
    return bool(key) and key not in ("your_groq_api_key_here", "your-groq-api-key-here")


# Unified call function


def call_llm(
    system_prompt,
    user_message,
    model_type="chat",
    temperature=0.3,
    max_tokens=1024,
    json_mode=False,
):
    """
    Call the configured LLM provider.

    Args:
        system_prompt: System instructions
        user_message: User's message (string)
        model_type: "query" (fast/cheap) or "chat" (smart)
        temperature: 0.0-1.0
        max_tokens: Max response tokens
        json_mode: If True, expect JSON response

    Returns:
        str: The LLM's response text
    """
    provider = LLM_PROVIDER.lower()

    if model_type == "query":
        model = LLM_MODEL_QUERY
    else:
        model = LLM_MODEL_CHAT

    # Use prompt-based JSON instructions where the provider path does not enforce a response schema.


    effective_system = system_prompt
    if json_mode and provider in ("claude", "gemini", "groq"):
        effective_system = system_prompt + "\n\nIMPORTANT: You MUST respond with valid JSON only. No markdown fences, no extra text."

    if provider == "claude":
        return _call_claude(effective_system, user_message, model, temperature, max_tokens)
    elif provider == "openai":
        return _call_openai(system_prompt, user_message, model, temperature, max_tokens, json_mode)
    elif provider == "gemini":
        return _call_gemini(effective_system, user_message, model, temperature, max_tokens)
    elif provider == "groq":
        return _call_groq_primary(effective_system, user_message, model, temperature, max_tokens, json_mode)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use 'claude', 'openai', 'gemini', or 'groq'.")


# Provider implementations


def _call_claude(system_prompt, user_message, model, temperature, max_tokens):
    client = _get_claude_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _call_openai(system_prompt, user_message, model, temperature, max_tokens, json_mode):
    client = _get_openai_client()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _gemini_is_retryable(e):
    """True for transient Gemini errors that should trigger model-fallback retry.

    Covers both 429 rate limits (ClientError) and 5xx server errors
    (ServerError: 502 Bad Gateway, 503 UNAVAILABLE / model overloaded,
    504 Deadline Exceeded). Falls back to string matching when the SDK
    exception classes aren't importable.
    """
    try:
        from google.genai.errors import ClientError, ServerError
        status = getattr(e, "status_code", None)
        if isinstance(e, ClientError) and status == 429:
            return True
        if isinstance(e, ServerError) and status in (502, 503, 504):
            return True
    except ImportError:
        pass
    msg = str(e).lower()
    return any(k in msg for k in (
        "resource_exhausted", "rate limit", "quota", "429",
        "unavailable", "overloaded", "503",
        "deadline exceeded", "504", "bad gateway", "502",
    ))


def _call_gemini_with_retry(generate_kwargs_fn, primary_model):
    """Try the primary and fallback Gemini models with bounded backoff.
    generate_kwargs_fn builds generation arguments for each attempted model."""
    client = _get_gemini_client()
    attempts = [
        (primary_model, 0.0),
        (LLM_MODEL_GEMINI_FALLBACK, 0.0),
        (primary_model, 0.5),
        (LLM_MODEL_GEMINI_FALLBACK, 1.0),
    ]
    last_err = None
    for model, wait in attempts:
        if wait > 0:
            time.sleep(wait)
        try:
            return client.models.generate_content(**generate_kwargs_fn(model)).text
        except Exception as e:
            if not _gemini_is_retryable(e):
                raise
            last_err = e
            print(f"[gemini] retryable error on {model} ({type(e).__name__}: {e}), falling through to next attempt")
    raise last_err


def _call_groq_single(system_prompt, user_message, temperature, max_tokens, model=None, json_mode=False):
    client = _get_groq_client()
    kwargs = {
        "model": model or LLM_MODEL_GROQ_FALLBACK,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    completion = client.chat.completions.create(**kwargs)
    return completion.choices[0].message.content


def _call_groq_history(system_prompt, messages, temperature, max_tokens, model=None):
    client = _get_groq_client()
    groq_messages = [{"role": "system", "content": system_prompt}] + messages
    completion = client.chat.completions.create(
        model=model or LLM_MODEL_GROQ_FALLBACK,
        messages=groq_messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return completion.choices[0].message.content


def _groq_is_too_large(e):
    """413: single request exceeds the model's TPM cap. Retrying never helps —
    the request won't shrink — but the Gemini fallback can still serve it."""
    msg = str(e).lower()
    return "request too large" in msg or "error code: 413" in msg


def _groq_is_retryable(e):
    if _groq_is_too_large(e):
        return False
    msg = str(e).lower()
    return any(k in msg for k in (
        "rate limit", "rate_limit", "429", "unavailable", "503", "502", "504",
        "over capacity", "service unavailable",
    ))


def _has_gemini():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return bool(key) and not key.startswith("your")


def _call_groq_with_retry(call_fn):
    """Groq-primary retry chain: immediate, +0.5s, +1.5s, then Gemini if a key exists."""
    last_err = None
    for wait in (0.0, 0.5, 1.5):
        if wait > 0:
            time.sleep(wait)
        try:
            return call_fn()
        except Exception as e:
            if not _groq_is_retryable(e):
                raise
            last_err = e
            print(f"[groq] retryable error ({type(e).__name__}: {e}), falling through to next attempt")
    raise last_err


def _call_groq_primary(system_prompt, user_message, model, temperature, max_tokens, json_mode):
    try:
        return _call_groq_with_retry(
            lambda: _call_groq_single(system_prompt, user_message, temperature, max_tokens,
                                      model=model, json_mode=json_mode)
        )
    except Exception as e:
        if (_groq_is_retryable(e) or _groq_is_too_large(e)) and _has_gemini():
            print(f"[groq->gemini] groq failed ({type(e).__name__}), falling over to gemini/{LLM_MODEL_GEMINI_FALLBACK}")
            return _call_gemini(system_prompt, user_message, LLM_MODEL_GEMINI_FALLBACK, temperature, max_tokens)
        raise


def _call_groq_primary_history(system_prompt, messages, model, temperature, max_tokens):
    try:
        return _call_groq_with_retry(
            lambda: _call_groq_history(system_prompt, messages, temperature, max_tokens, model=model)
        )
    except Exception as e:
        if (_groq_is_retryable(e) or _groq_is_too_large(e)) and _has_gemini():
            print(f"[groq->gemini] groq failed ({type(e).__name__}), falling over to gemini/{LLM_MODEL_GEMINI_FALLBACK}")
            return _call_gemini_history(system_prompt, messages, LLM_MODEL_GEMINI_FALLBACK, temperature, max_tokens)
        raise


def _call_gemini(system_prompt, user_message, model, temperature, max_tokens):
    def kwargs_fn(m):
        return dict(model=m, contents=user_message, config=_gemini_config(m, {
            "system_instruction": system_prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }))
    try:
        return _call_gemini_with_retry(kwargs_fn, model)
    except Exception as e:
        if _gemini_is_retryable(e) and _has_groq():
            print(f"[gemini->groq] gemini chain exhausted, falling over to groq/{LLM_MODEL_GROQ_FALLBACK}")
            return _call_groq_single(system_prompt, user_message, temperature, max_tokens)
        raise


# Structured extraction: schema-enforced JSON (Gemini response_schema; other
# providers fall back to prompt-JSON). Used by lead_extractor — the shape is
# guaranteed on Gemini, so no salvage layer is needed there.


def call_llm_structured(system_prompt, user_message, schema: dict, temperature=0.0,
                        max_tokens=512, model_type="query"):
    """Return the parsed JSON object for `schema` (a JSON Schema dict).
    Gemini: enforced via response_mime_type/response_schema with the usual
    retry chain; others: json_mode prompt + json.loads (raises on garbage)."""
    provider = LLM_PROVIDER.lower()
    model = LLM_MODEL_QUERY if model_type == "query" else LLM_MODEL_CHAT
    if provider == "gemini":
        def kwargs_fn(m):
            return dict(model=m, contents=user_message, config=_gemini_config(m, {
                "system_instruction": system_prompt,
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json",
                "response_schema": schema,
            }))
        try:
            raw = _call_gemini_with_retry(kwargs_fn, model)
        except Exception as e:
            if _gemini_is_retryable(e) and _has_groq():
                raw = _call_groq_single(system_prompt + "\n\nRespond with valid JSON only.",
                                        user_message, temperature, max_tokens, json_mode=True)
            else:
                raise
    else:
        raw = call_llm(system_prompt, user_message, model_type=model_type,
                       temperature=temperature, max_tokens=max_tokens, json_mode=True)
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    return json.loads(raw)


# Convenience: call with conversation history (for multi-turn)


def call_llm_with_history(
    system_prompt,
    messages,
    model_type="chat",
    temperature=0.3,
    max_tokens=2048,
):
    """
    Call LLM with full conversation history.

    Args:
        system_prompt: System instructions
        messages: List of {"role": "user"|"assistant", "content": "..."}
        model_type: "query" or "chat"
        temperature: 0.0-1.0
        max_tokens: Max response tokens

    Returns:
        str: The LLM's response text
    """
    provider = LLM_PROVIDER.lower()

    if model_type == "query":
        model = LLM_MODEL_QUERY
    else:
        model = LLM_MODEL_CHAT

    if provider == "claude":
        return _call_claude_history(system_prompt, messages, model, temperature, max_tokens)
    elif provider == "openai":
        return _call_openai_history(system_prompt, messages, model, temperature, max_tokens)
    elif provider == "gemini":
        return _call_gemini_history(system_prompt, messages, model, temperature, max_tokens)
    elif provider == "groq":
        return _call_groq_primary_history(system_prompt, messages, model, temperature, max_tokens)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def _call_claude_history(system_prompt, messages, model, temperature, max_tokens):
    client = _get_claude_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def _call_openai_history(system_prompt, messages, model, temperature, max_tokens):
    client = _get_openai_client()
    oai_messages = [{"role": "system", "content": system_prompt}] + messages
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=oai_messages,
    )
    return response.choices[0].message.content


def _call_gemini_history(system_prompt, messages, model, temperature, max_tokens):
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    def kwargs_fn(m):
        return dict(model=m, contents=contents, config=_gemini_config(m, {
            "system_instruction": system_prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }))
    try:
        return _call_gemini_with_retry(kwargs_fn, model)
    except Exception as e:
        if _gemini_is_retryable(e) and _has_groq():
            print(f"[gemini->groq] gemini chain exhausted, falling over to groq/{LLM_MODEL_GROQ_FALLBACK}")
            return _call_groq_history(system_prompt, messages, temperature, max_tokens)
        raise
