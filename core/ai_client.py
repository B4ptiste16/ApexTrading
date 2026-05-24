"""Unified AI client — routes bot vision calls to any supported provider.

Supported providers (set via .env):
    AI_PROVIDER = anthropic | google | xai   (default: anthropic)
    AI_MODEL    = <model name>               (default: see PROVIDER_DEFAULTS)

Per-provider API keys:
    ANTHROPIC_API_KEY   — get at console.anthropic.com (paid)
    GOOGLE_AI_API_KEY   — get FREE at aistudio.google.com (1 500 req/day)
    XAI_API_KEY         — get at console.x.ai ($25 free credits/mo new accounts)
"""

import os
import base64

# ── Defaults ──────────────────────────────────────────────────────────────────
PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "google":    "gemini-2.0-flash",
    "xai":       "grok-2-vision-1212",
}

PROVIDER_ENV_KEY: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google":    "GOOGLE_AI_API_KEY",
    "xai":       "XAI_API_KEY",
}

# Human-readable names for the UI
PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic (Claude)",
    "google":    "Google (Gemini) — FREE",
    "xai":       "xAI (Grok)",
}

# Available models per provider, shown in UI dropdown
PROVIDER_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
        "claude-opus-4-5",
    ],
    "google": [
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    ],
    "xai": [
        "grok-2-vision-1212",
        "grok-2-mini",
    ],
}


def load_ai_config() -> tuple[str, str, str]:
    """Read (provider, model, api_key) from the process environment.

    Call this at bot startup — env vars are already loaded from .env by
    dotenv at that point.
    """
    provider = os.getenv("AI_PROVIDER", "anthropic").lower().strip()
    if provider not in PROVIDER_DEFAULTS:
        print(f"[ai_client] Unknown AI_PROVIDER '{provider}', falling back to anthropic")
        provider = "anthropic"
    default_model = PROVIDER_DEFAULTS[provider]
    model    = (os.getenv("AI_MODEL", default_model) or default_model).strip()
    env_var  = PROVIDER_ENV_KEY[provider]
    api_key  = os.getenv(env_var, "")
    if not api_key:
        print(f"[ai_client] Warning: {env_var} is not set — AI calls will fail")
    return provider, model, api_key


def call_ai_vision(content_parts: list, provider: str, model: str,
                   api_key: str, max_tokens: int = 800) -> str:
    """Send a multimodal prompt to the chosen AI provider and return the
    raw text response.

    content_parts follows Anthropic's format:
        [
            {"type": "text",  "text": "..."},
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/png",
                                          "data": "<b64>"}},
            ...
        ]
    The function converts to each provider's native format internally.
    """
    if provider == "anthropic":
        return _call_anthropic(content_parts, model, api_key, max_tokens)
    if provider == "google":
        return _call_google(content_parts, model, api_key, max_tokens)
    if provider == "xai":
        return _call_xai(content_parts, model, api_key, max_tokens)
    raise ValueError(f"[ai_client] Unknown provider: {provider}")


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _call_anthropic(content_parts: list, model: str,
                    api_key: str, max_tokens: int) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content_parts}],
    )
    return resp.content[0].text.strip()


# ── Google Gemini ─────────────────────────────────────────────────────────────

def _call_google(content_parts: list, model: str,
                 api_key: str, max_tokens: int) -> str:
    """Convert Anthropic content list → Gemini parts and call the API.

    Requires: pip install google-generativeai pillow
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai is not installed.\n"
            "Run: pip install google-generativeai"
        )
    import io
    import PIL.Image

    genai.configure(api_key=api_key)
    gemini = genai.GenerativeModel(model)

    parts = []
    for item in content_parts:
        if item.get("type") == "text":
            parts.append(item["text"])
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                img_bytes = base64.b64decode(src["data"])
                img = PIL.Image.open(io.BytesIO(img_bytes))
                parts.append(img)

    resp = gemini.generate_content(
        parts,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
        ),
    )
    return resp.text.strip()


# ── xAI / Grok  (OpenAI-compatible API) ───────────────────────────────────────

def _call_xai(content_parts: list, model: str,
              api_key: str, max_tokens: int) -> str:
    """Convert Anthropic content list → OpenAI message format and call xAI.

    Requires: pip install openai
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package is not installed.\n"
            "Run: pip install openai"
        )

    client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")

    oai_content = []
    for item in content_parts:
        if item.get("type") == "text":
            oai_content.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                media_type = src.get("media_type", "image/png")
                data       = src["data"]
                oai_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                })

    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": oai_content}],
    )
    return resp.choices[0].message.content.strip()
