"""Minimal stub of goat's gemini_native_adapter — ONLY the two symbols that
agent/gemini_files.py imports. Copied verbatim from the real adapter so the
spike exercises the genuine gemini_files.py unchanged."""

DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def is_native_gemini_base_url(base_url: str) -> bool:
    """Return True when the endpoint speaks Gemini's native REST API."""
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if not normalized:
        return False
    if "generativelanguage.googleapis.com" not in normalized:
        return False
    return not normalized.endswith("/openai")
