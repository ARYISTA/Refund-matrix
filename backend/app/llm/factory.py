"""Pick the provider named by LLM_PROVIDER."""

from app.config import Settings, get_settings
from app.contracts.enums import ProviderName
from app.llm.errors import LLMConfigError
from app.llm.groq_provider import GroqProvider
from app.llm.provider import LLMProvider


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider is ProviderName.GROQ:
        return GroqProvider.from_settings(settings)
    raise LLMConfigError(
        f"LLM_PROVIDER={settings.llm_provider.value} is not available yet (Ollama arrives in Phase 10)"
    )
