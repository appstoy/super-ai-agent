"""Magpie local gateway adapter for hosted and local models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.errors import ExternalDependencyError


@dataclass(frozen=True, slots=True)
class MagpieModel:
    id: str
    owned_by: str | None = None
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None


class MagpieModelGateway(OpenAICompatibleGateway):
    """OpenAI-compatible gateway pointed at a running Magpie instance."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://127.0.0.1:3425/v1",
        api_key: str = "magpie-super-ai-agent",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(base_url, api_key, model or "__catalog__", timeout=timeout)

    @property
    def gateway_url(self) -> str:
        return self.base_url.removesuffix("/v1")

    def hello(self) -> dict[str, object]:
        return self._get_json(self.gateway_url + "/api/hello")

    def list_models(self) -> tuple[MagpieModel, ...]:
        payload = self._get_json(self.base_url + "/models")
        raw_models = payload.get("data", [])
        if not isinstance(raw_models, list):
            raise ExternalDependencyError("Magpie model catalog has invalid data")
        models: list[MagpieModel] = []
        for raw_model in raw_models:
            if not isinstance(raw_model, dict) or not raw_model.get("id"):
                continue
            models.append(
                MagpieModel(
                    id=str(raw_model["id"]),
                    owned_by=str(raw_model["owned_by"]) if raw_model.get("owned_by") else None,
                    display_name=(
                        str(raw_model["display_name"])
                        if raw_model.get("display_name")
                        else None
                    ),
                    context_window=(
                        int(raw_model["context_window"])
                        if raw_model.get("context_window")
                        else None
                    ),
                    max_output_tokens=(
                        int(raw_model["max_output_tokens"])
                        if raw_model.get("max_output_tokens")
                        else None
                    ),
                )
            )
        return tuple(models)

    def _get_json(self, url: str) -> dict[str, object]:
        http_request = request.Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "super-ai-agent/0.1"},
            method="GET",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ExternalDependencyError(f"Magpie HTTP {exc.code}: {detail[:500]}") from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ExternalDependencyError(f"Magpie request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ExternalDependencyError("Magpie response is not an object")
        return payload

