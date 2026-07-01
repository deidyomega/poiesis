"""Selectable models for the #spice (OpenAI-compatible) channel — drives the UI picker.

Each preset bundles the model id and its base_url, so switching in the UI can cross
providers (OpenRouter ↔ local Ollama). The API key stays env-level
(POIESIS_SPICE_API_KEY); Ollama ignores it, OpenRouter uses it.
"""

from __future__ import annotations

SPICE_MODELS: list[dict[str, str]] = [
    {"id": "euryale-l31", "label": "Euryale 70B · L3.1 (fast)",
     "model": "sao10k/l3.1-euryale-70b", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "euryale-l33", "label": "Euryale 70B · L3.3 (smarter, slower)",
     "model": "sao10k/l3.3-euryale-70b", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "hermes4-70", "label": "Hermes-4 70B",
     "model": "nousresearch/hermes-4-70b", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "magnum-v4-72", "label": "Magnum v4 72B (prose)",
     "model": "anthracite-org/magnum-v4-72b", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "mistral-local", "label": "Mistral 24B (local · free/instant)",
     "model": "huihui_ai/mistral-small-abliterated:latest",
     "base_url": "http://192.168.1.54:11434/v1"},
]


def by_id(model_id: str) -> dict[str, str] | None:
    return next((m for m in SPICE_MODELS if m["id"] == model_id), None)


def id_for_model(model: str | None) -> str | None:
    """Reverse-lookup a preset id from a model string (to preselect the dropdown)."""
    return next((m["id"] for m in SPICE_MODELS if m["model"] == model), None)
