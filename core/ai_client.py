"""Unified AI client — routes bot calls to any supported provider.

Two call modes:
    call_ai_vision(content_parts, ...)  — multimodal: text + chart images
    call_ai_text(prompt, ...)           — text-only: no charts needed

Supported providers (set AI_PROVIDER in .env):
    anthropic — Claude Haiku / Sonnet (paid)        console.anthropic.com
    google    — Gemini Flash/Pro (FREE 1 500/day)   aistudio.google.com
    xai       — Grok vision (≈$25 free credits/mo)  console.x.ai
    groq      — Llama / Mixtral (FREE 14 400/day)   console.groq.com
               NOTE: Groq is text-only — no image support.

Set AI_MODE=text to skip chart generation and use pure text prompts.
(Required for Groq; optional for others — text mode is faster.)

Per-provider env var for the API key:
    ANTHROPIC_API_KEY  /  GOOGLE_AI_API_KEY  /  XAI_API_KEY  /  GROQ_API_KEY
"""

import os
import base64

# ── Provider metadata ─────────────────────────────────────────────────────────

PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "google":    "gemini-2.0-flash",
    "xai":       "grok-2-vision-1212",
    "groq":      "llama-3.3-70b-versatile",
}

PROVIDER_ENV_KEY: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google":    "GOOGLE_AI_API_KEY",
    "xai":       "XAI_API_KEY",
    "groq":      "GROQ_API_KEY",
}

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic (Claude)",
    "google":    "Google (Gemini) — FREE · vision",
    "xai":       "xAI (Grok) — vision",
    "groq":      "Groq (Llama) — FREE · text-only",
}

PROVIDER_MODELS: dict[str, list[str]] = {
    # V4.6.102 — current Claude lineup (newest first), including Fable 5.
    "anthropic": [
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
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
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ],
}

# Providers that cannot send images — automatically forces text mode
_TEXT_ONLY_PROVIDERS = {"groq"}


def provider_supports_vision(provider: str) -> bool:
    """True if provider can accept image content in prompts."""
    return provider not in _TEXT_ONLY_PROVIDERS


def load_ai_config() -> tuple[str, str, str, str]:
    """Return (provider, model, api_key, mode) from the process environment.

    mode is 'vision' or 'text'. Text-only providers always return 'text'
    regardless of the AI_MODE env var.
    """
    provider = os.getenv("AI_PROVIDER", "anthropic").lower().strip()
    if provider not in PROVIDER_DEFAULTS:
        print(f"[ai_client] Unknown AI_PROVIDER '{provider}', falling back to anthropic")
        provider = "anthropic"

    default_model = PROVIDER_DEFAULTS[provider]
    model   = (os.getenv("AI_MODEL", default_model) or default_model).strip()
    env_var = PROVIDER_ENV_KEY[provider]
    api_key = os.getenv(env_var, "")
    if not api_key:
        print(f"[ai_client] Warning: {env_var} is not set — AI calls will fail")

    # Determine call mode
    env_mode = os.getenv("AI_MODE", "vision").lower().strip()
    if not provider_supports_vision(provider):
        mode = "text"   # override — text-only provider
    elif env_mode == "text":
        mode = "text"
    else:
        mode = "vision"

    return provider, model, api_key, mode


# ── Vision call (text + chart images) ────────────────────────────────────────

def call_ai_vision(content_parts: list, provider: str, model: str,
                   api_key: str, max_tokens: int = 800) -> str:
    """Send a multimodal content list (text + base64 chart images) to the
    chosen provider and return the raw text response.

    content_parts follows Anthropic's format:
        [{"type":"text","text":"..."},
         {"type":"image","source":{"type":"base64","media_type":"image/png",
                                    "data":"<b64>"}}]

    If the provider doesn't support vision, image items are silently
    dropped and a text-only call is made.
    """
    if not provider_supports_vision(provider):
        # Strip images, fall back to text path
        text_prompt = "\n".join(
            item["text"] for item in content_parts if item.get("type") == "text"
        )
        return call_ai_text(text_prompt, provider, model, api_key, max_tokens)

    if provider == "anthropic":
        return _call_anthropic(content_parts, model, api_key, max_tokens)
    if provider == "google":
        return _call_google(content_parts, model, api_key, max_tokens)
    if provider == "xai":
        return _call_openai_compat(content_parts, model, api_key, max_tokens,
                                   base_url="https://api.x.ai/v1")
    raise ValueError(f"[ai_client] Unknown provider: {provider}")


# ── Text-only call (no charts, works with any provider) ──────────────────────

def call_ai_text(prompt: str, provider: str, model: str,
                 api_key: str, max_tokens: int = 800) -> str:
    """Send a plain text prompt to the chosen provider.

    Works with ALL providers, including text-only ones like Groq.
    """
    if provider == "anthropic":
        return _call_anthropic([{"type": "text", "text": prompt}],
                               model, api_key, max_tokens)
    if provider == "google":
        return _call_google([{"type": "text", "text": prompt}],
                            model, api_key, max_tokens)
    if provider == "xai":
        return _call_openai_compat([{"type": "text", "text": prompt}],
                                   model, api_key, max_tokens,
                                   base_url="https://api.x.ai/v1")
    if provider == "groq":
        return _call_openai_compat([{"type": "text", "text": prompt}],
                                   model, api_key, max_tokens,
                                   base_url="https://api.groq.com/openai/v1")
    raise ValueError(f"[ai_client] Unknown provider: {provider}")


# ── Provider implementations ──────────────────────────────────────────────────

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


def _call_google(content_parts: list, model: str,
                 api_key: str, max_tokens: int) -> str:
    """Convert Anthropic content list → Gemini parts.

    Requires: pip install google-generativeai pillow
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai is not installed. Run: pip install google-generativeai"
        )
    import io, PIL.Image

    genai.configure(api_key=api_key)
    gemini = genai.GenerativeModel(model)

    parts = []
    for item in content_parts:
        if item.get("type") == "text":
            parts.append(item["text"])
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                img = PIL.Image.open(io.BytesIO(base64.b64decode(src["data"])))
                parts.append(img)

    resp = gemini.generate_content(
        parts,
        generation_config=genai.types.GenerationConfig(max_output_tokens=max_tokens),
    )
    return resp.text.strip()


def _call_openai_compat(content_parts: list, model: str,
                        api_key: str, max_tokens: int,
                        base_url: str) -> str:
    """OpenAI-compatible endpoint (xAI Grok, Groq, etc.).

    Requires: pip install openai
    Image items are included only if they're present — Groq callers
    will never have image items (call_ai_text strips them upstream).
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai package is not installed. Run: pip install openai")

    client = OpenAI(api_key=api_key, base_url=base_url)

    oai_content = []
    for item in content_parts:
        if item.get("type") == "text":
            oai_content.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                media_type = src.get("media_type", "image/png")
                oai_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{src['data']}"
                    },
                })

    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": oai_content}],
    )
    return resp.choices[0].message.content.strip()
