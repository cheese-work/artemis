from __future__ import annotations

import os
from typing import Any

from artemis.config import settings
from artemis.config.llm import parse_llm_config, validate_vertex_ai_credentials
from artemis.config.settings import is_placeholder_key
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import ProbeAction, ProbeCategory, ProbeResult, ProbeStatus


class LLMCredentialsProbe(BaseProbe):
    @property
    def probe_id(self) -> str:
        return "gemini_api_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return True

    @staticmethod
    def _mask_key(key: str) -> str:
        return f"****{key[-4:]}" if len(key) > 4 else "****"

    @classmethod
    def _configured_models(cls, value: Any, result: dict[str, set[str | None]]) -> None:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, dict):
            provider = value.get("provider")
            model = value.get("model")
            if isinstance(provider, str) and isinstance(model, str):
                result.setdefault(provider, set()).add(value.get("api_base"))
            for child in value.values():
                cls._configured_models(child, result)
        elif isinstance(value, (list, tuple)):
            for child in value:
                cls._configured_models(child, result)

    @staticmethod
    def _label(provider: str) -> str:
        return {
            "google": "Google",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
            "openrouter": "OpenRouter",
            "xai": "xAI",
            "vertexai": "Vertex AI",
            "ollama": "Ollama",
            "vllm": "vLLM",
            "custom": "Custom provider",
        }.get(provider, provider)

    async def probe(self) -> ProbeResult:
        try:
            llm_config = parse_llm_config()
        except Exception:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Configured model providers",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary="Model configuration unavailable",
                description="The active model configuration could not be parsed.",
                metadata={"configured_count": 0, "providers": [], "is_set": False},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Review model configuration",
                        payload="Open Admin Setup and correct the active model configuration.",
                    )
                ],
            )

        models: dict[str, set[str | None]] = {}
        self._configured_models(llm_config, models)
        endpoint_env = {
            "openai": "OPENAI_BASE_URL",
            "anthropic": "ANTHROPIC_BASE_URL",
            "ollama": "OLLAMA_BASE_URL",
            "vllm": "VLLM_BASE_URL",
            "custom": "CUSTOM_BASE_URL",
        }
        provider_status = []
        for provider in sorted(models):
            credential = settings.get_api_key(provider)
            credential_value = credential.get_secret_value() if credential else None
            if credential_value and is_placeholder_key(credential_value):
                credential_value = None
            bases = {base for base in models[provider] if isinstance(base, str) and base.strip()}
            base_url = (
                os.environ.get(endpoint_env.get(provider, "")) if provider in endpoint_env else None
            )
            has_endpoint = bool(bases or (base_url and not is_placeholder_key(base_url)))

            if provider == "vertexai":
                try:
                    validate_vertex_ai_credentials()
                    is_set = True
                except Exception:
                    is_set = False
            elif provider in {"ollama", "vllm", "custom"}:
                is_set = has_endpoint
            elif provider == "openai":
                is_set = bool(credential_value)
            else:
                is_set = bool(credential_value)

            provider_status.append(
                {
                    "provider": provider,
                    "label": self._label(provider),
                    "is_set": is_set,
                    "masked": self._mask_key(credential_value) if credential_value else None,
                }
            )
        missing = [item["label"] for item in provider_status if not item["is_set"]]
        is_set = bool(provider_status) and not missing
        description = (
            "Credentials are available for every configured model provider."
            if is_set
            else f"Missing required configuration for: {', '.join(missing) or 'the active model provider'}."
        )
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Configured model providers",
            status=ProbeStatus.PASS if is_set else ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary="Configured providers ready" if is_set else "Provider configuration incomplete",
            description=description,
            metadata={
                "configured_count": len(provider_status),
                "providers": provider_status,
                "is_set": is_set,
            },
            actions=[]
            if is_set
            else [
                ProbeAction(
                    action_type="hint",
                    label="Configure active providers",
                    payload="Add the missing provider credentials in Admin Setup.",
                )
            ],
        )


class VisionOCRProbe(BaseProbe):
    @property
    def probe_id(self) -> str:
        return "vision_ocr_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return False

    @staticmethod
    def _mask_key(key: str) -> str:
        return f"****{key[-4:]}" if len(key) > 4 else "****"

    async def probe(self) -> ProbeResult:
        from artemis.utils.ocr_api import is_ocr_configured

        ocr_key = settings.get_api_key("ocr")
        key_value = ocr_key.get_secret_value() if ocr_key else None
        is_configured = bool(
            is_ocr_configured() and key_value and not is_placeholder_key(key_value)
        )
        masked = self._mask_key(key_value) if is_configured and key_value else None
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Vision OCR API Key (Optional)",
            status=ProbeStatus.PASS,
            is_blocker=False,
            summary="Active & Configured" if is_configured else "Not Configured (Optional)",
            description=(
                f"Google Cloud Vision OCR ({masked}) is active for image text recognition."
                if is_configured
                else "OCR_API_KEY is not set. Perception uses the UI XML hierarchy without OCR."
            ),
            metadata={"is_set": is_configured, "masked": masked},
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="OCR Enabled" if is_configured else "OCR Optional",
                    payload=(
                        "OCR is active and will fuse text with UI XML hierarchy."
                        if is_configured
                        else "OCR is optional; UI XML hierarchy remains available."
                    ),
                )
            ],
        )
