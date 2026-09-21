from __future__ import annotations

import json
import os
from typing import Any, Type

import httpx
from pydantic import BaseModel


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class AgentModelRouter:
    """Cliente OpenRouter usado solo por la capa conversacional."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.getenv("OPENROUTER_MAIN_API_KEY", "").strip()
            or os.getenv("OPENROUTER_API_KEY", "").strip()
        )
        self.model = (
            model
            or os.getenv("OPENROUTER_MAIN_MODEL", "").strip()
            or os.getenv("MODELO_AGENTE_PRINCIPAL", "").strip()
            or os.getenv("OPENROUTER_MODEL", "").strip()
            or "google/gemini-2.5-flash-lite"
        )
        self.fallback_model = (
            fallback_model
            or os.getenv("OPENROUTER_FALLBACK_MODEL", "").strip()
            or os.getenv("MODELO_AGENTE_RESPALDO", "").strip()
            or "openai/gpt-4o-mini"
        )
        self.timeout = timeout or float(os.getenv("OPENROUTER_TIMEOUT", "35"))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("Falta OPENROUTER_API_KEY.")
        return {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://www.mettryc.com",
            "X-Title": "Mettryc Realty - Agente Virtual",
        }

    async def completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
    ) -> str:
        selected = model or self.model
        if not selected:
            raise RuntimeError("No hay modelo configurado para OpenRouter.")

        payload: dict[str, Any] = {
            "model": selected,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning": {"exclude": True},
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers=self._headers(),
                json=payload,
            )

        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter no devolvió una respuesta.")

        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )

        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter devolvió una respuesta vacía.")

        return content.strip()

    async def completion_with_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 900,
    ) -> str:
        errors: list[str] = []

        for model in dict.fromkeys([self.model, self.fallback_model]):
            try:
                return await self.completion(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                errors.append(
                    model + ": " + type(exc).__name__ + ": " + str(exc)[:160]
                )

        raise RuntimeError("Fallaron los modelos de OpenRouter. " + " | ".join(errors))

    async def json_completion(
        self,
        schema: Type[BaseModel],
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 900,
    ) -> BaseModel:
        errors: list[str] = []

        for model in dict.fromkeys([self.model, self.fallback_model]):
            for response_format in (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": schema.model_json_schema(),
                    },
                },
                {"type": "json_object"},
            ):
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": response_format,
                    "reasoning": {"exclude": True},
                }

                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(
                            OPENROUTER_URL,
                            headers=self._headers(),
                            json=payload,
                        )

                    response.raise_for_status()
                    content = (
                        response.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if isinstance(content, list):
                        content = "".join(
                            item.get("text", "")
                            for item in content
                            if isinstance(item, dict)
                        )

                    cleaned = self._clean_json(str(content))
                    return schema.model_validate_json(cleaned)

                except Exception as exc:
                    errors.append(
                        model + "/" + response_format["type"] + ": "
                        + type(exc).__name__ + ": " + str(exc)[:120]
                    )

        raise RuntimeError(" | ".join(errors[-6:]))

    @staticmethod
    def _clean_json(content: str) -> str:
        text = content.strip()
        fence = chr(96) * 3

        if text.startswith(fence):
            text = text[len(fence):].strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
            if text.endswith(fence):
                text = text[:-len(fence)].strip()

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]

        json.loads(text)
        return text
