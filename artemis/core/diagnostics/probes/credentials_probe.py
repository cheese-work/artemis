# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""LLM & Multimodal Vision Credentials Readiness Probe."""

from typing import Any
from artemis.config import settings
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


class LLMCredentialsProbe(BaseProbe):
    """Probe verifying Gemini and multi-provider multimodal LLM credentials."""

    @property
    def probe_id(self) -> str:
        return "gemini_api_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return True

    def _mask_key(self, key_str: str) -> str:
        """Helper to safely mask an API credential for display."""
        return f"****{key_str[-4:]}" if len(key_str) > 4 else "****"

    async def probe(self) -> ProbeResult:
        import os

        from artemis.config.settings import is_placeholder_key

        gemini_key = settings.get_api_key("google")
        openai_key = settings.get_api_key("openai")
        claude_key = settings.get_api_key("anthropic")
        openrouter_key = settings.get_api_key("openrouter")
        xai_key = settings.get_api_key("xai")
        ocr_key = settings.get_api_key("ocr")

        configured_providers: list[dict[str, Any]] = []
        if gemini_key and not is_placeholder_key(gemini_key):
            g_val = gemini_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "google",
                    "label": "Gemini",
                    "is_set": True,
                    "masked": self._mask_key(g_val),
                }
            )
        if openai_key and not is_placeholder_key(openai_key):
            o_val = openai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openai",
                    "label": "ChatGPT",
                    "is_set": True,
                    "masked": self._mask_key(o_val),
                }
            )
        if claude_key and not is_placeholder_key(claude_key):
            c_val = claude_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "anthropic",
                    "label": "Claude",
                    "is_set": True,
                    "masked": self._mask_key(c_val),
                }
            )
        if openrouter_key and not is_placeholder_key(openrouter_key):
            or_val = openrouter_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openrouter",
                    "label": "OpenRouter",
                    "is_set": True,
                    "masked": self._mask_key(or_val),
                }
            )
        if xai_key and not is_placeholder_key(xai_key):
            x_val = xai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "xai",
                    "label": "xAI (Grok)",
                    "is_set": True,
                    "masked": self._mask_key(x_val),
                }
            )

        # Detect any custom model endpoints or environment variables defined in files
        for env_var, label, prov_id in [
            ("DEEPSEEK_API_KEY", "DeepSeek", "deepseek"),
            ("GROQ_API_KEY", "Groq", "groq"),
            ("OLLAMA_BASE_URL", "Local Ollama", "ollama"),
            ("VLLM_BASE_URL", "vLLM Endpoint", "vllm"),
            ("VERTEX_AI_PROJECT", "Google Cloud Vertex AI", "vertexai"),
        ]:
            val = os.environ.get(env_var)
            if val and val.strip() and not is_placeholder_key(val.strip()):
                configured_providers.append(
                    {
                        "provider": prov_id,
                        "label": label,
                        "is_set": True,
                        "masked": self._mask_key(val.strip()),
                    }
                )

        # Preserve endpoint-only diagnosis when a compatible URL exists without
        # its provider key; keyed providers are verified against their own URL above.
        openai_base_url = os.environ.get("OPENAI_BASE_URL")
        if (
            openai_base_url
            and openai_base_url.strip()
            and not is_placeholder_key(openai_base_url.strip())
            and not openai_key
        ):
            configured_providers.append(
                {
                    "provider": "custom",
                    "label": "Custom OpenAI Endpoint",
                    "is_set": True,
                    "masked": self._mask_key(openai_base_url.strip()),
                }
            )

        metadata = {
            "configured_count": len(configured_providers),
            "providers": configured_providers,
            "is_set": bool(configured_providers),
        }

        # Case 1: Gemini API Key configured (Standard / Recommended)
        if gemini_key and not is_placeholder_key(gemini_key):
            masked = self._mask_key(gemini_key.get_secret_value())
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="Active (Gemini)",
                description=f"Gemini multimodal API credential is active ({masked}) and ready for vision and reasoning.",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Provider Active",
                        payload="Gemini 2.5 Flash / Pro Multimodal Engine is enabled.",
                    )
                ],
            )

        # Case 2: Other LLM provider configured
        if configured_providers:
            first_p = configured_providers[0]
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Active ({first_p['label']})",
                description=f"{first_p['label']} multimodal API credential is active ({first_p['masked']}).",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Provider Active",
                        payload=f"{first_p['label']} multimodal vision endpoint is active.",
                    )
                ],
            )

        # Case 3: No LLM key configured
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Multimodal LLM API Key",
            status=ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary="Key Missing",
            description="No Multimodal LLM credential (e.g. GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY) found in environment or .env file.",
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="command",
                    label="Run Artemis Init",
                    payload="artemis init",
                ),
                ProbeAction(
                    action_type="link",
                    label="Get Google AI Studio Key",
                    payload="https://aistudio.google.com/app/apikey",
                ),
            ],
        )


class VisionOCRProbe(BaseProbe):
    """Probe verifying optional Google Cloud Vision OCR credentials."""

    @property
    def probe_id(self) -> str:
        return "vision_ocr_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return False

    def _mask_key(self, key_str: str) -> str:
        """Helper to safely mask an API credential for display."""
        return f"****{key_str[-4:]}" if len(key_str) > 4 else "****"

    async def probe(self) -> ProbeResult:
        from artemis.utils.ocr_api import is_ocr_configured

        ocr_key = settings.get_api_key("ocr")
        from artemis.config.settings import is_placeholder_key

        is_configured = (
            is_ocr_configured() and ocr_key is not None and not is_placeholder_key(ocr_key)
        )

        if is_configured and ocr_key:
            val = ocr_key.get_secret_value()
            masked = self._mask_key(val)
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Vision OCR API Key (Optional)",
                status=ProbeStatus.PASS,
                is_blocker=False,
                summary="Active & Configured",
                description=f"Google Cloud Vision OCR ({masked}) is active for image text recognition.",
                metadata={"is_set": True, "masked": masked},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="OCR Enabled",
                        payload="OCR is active and will fuse text with UI XML hierarchy.",
                    )
                ],
            )

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Vision OCR API Key (Optional)",
            status=ProbeStatus.PASS,
            is_blocker=False,
            summary="Not Configured (Optional)",
            description="OCR_API_KEY is not set. Perception uses the UI XML hierarchy without OCR.",
            metadata={"is_set": False, "masked": None},
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="Standard XML Perception",
                    payload="Artemis uses pure UI layout parsing and Set-of-Marks visual grounding.",
                )
            ],
        )
