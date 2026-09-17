from __future__ import annotations

import json
import os
from typing import Any

import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    """Small, reusable OpenRouter client for the new AI engine.

    Configuration is read only from environment variables. No credentials are
    stored in the repository.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = (
            model
            or os.getenv("OPENROUTER_MAIN_MODEL", "").strip()
            or os.getenv("OPENROUTER_MODEL", "").strip()
        )
        self.fallback_model = (
            fallback_model
            or os.getenv("OPENROUTER_FALLBACK_MODEL", "").strip()
        )
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("Falta OPENROUTER_API_KEY en las variables de entorno.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://mettryc.com"),
            "X-Title": os.getenv("OPENROUTER_APP_TITLE", "Mettryc AI Engine"),
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        force_json: bool = False,
    ) -> str:
        selected_model = model or self.model
        if not selected_model:
            raise RuntimeError(
                "Falta el modelo de OpenRouter. Define OPENROUTER_MAIN_MODEL "
                "(o OPENROUTER_MODEL) en las variables de entorno."
            )

        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers=self._headers(),
                json=payload,
            )

        if response.status_code >= 400:
            detail = response.text[:1000]
            raise RuntimeError(
                f"OpenRouter respondió HTTP {response.status_code}: {detail}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter no devolvió ninguna respuesta del modelo.")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter devolvió una respuesta vacía.")
        return content.strip()

    async def chat_with_fallback(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        force_json: bool = False,
    ) -> str:
        try:
            return await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                force_json=force_json,
            )
        except Exception as primary_error:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            try:
                return await self.chat(
                    messages,
                    model=self.fallback_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    force_json=force_json,
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    "Fallaron el modelo principal y el modelo de respaldo de OpenRouter. "
                    f"Principal: {primary_error}. Respaldo: {fallback_error}"
                ) from fallback_error



def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object even when a model wraps it in markdown fences."""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("La respuesta del modelo no contiene un objeto JSON válido.")
        value = json.loads(cleaned[start : end + 1])

    if not isinstance(value, dict):
        raise ValueError("Se esperaba un objeto JSON.")
    return value
