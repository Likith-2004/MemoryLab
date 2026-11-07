"""Allowlist of providers and models the run gateway accepts (we are never a generic proxy)."""

PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
}
PROVIDER_HOSTS: dict[str, str] = {"anthropic": "api.anthropic.com"}


def check_model(provider: str, model_id: str) -> None:
    if provider not in PROVIDER_MODELS:
        raise ValueError("provider is not allowlisted")
    if model_id not in PROVIDER_MODELS[provider]:
        raise ValueError("model is not allowlisted for this provider")
