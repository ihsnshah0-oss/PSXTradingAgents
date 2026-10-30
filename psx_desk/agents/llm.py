"""Typed LLM plumbing: one schema in, one Pydantic instance out.

Two backends, both first-class:

* ``google-genai`` (default) — the official Google Gen AI SDK, called with
  ``response_mime_type="application/json"`` plus a JSON schema derived from the
  Pydantic model, then validated locally.
* ``langchain`` — ``ChatGoogleGenerativeAI`` (or any other chat model) through
  ``with_structured_output(schema)``.

Both implement :class:`StructuredInvoker`, so the graph does not care which one
is wired in, and a test can substitute a scripted invoker without touching a
single node.

Whatever the backend, the contract is the same: a node asks for a schema, gets
that schema or an exception.  There is no "free text fallback" — a report that
does not validate is a bug, not a paragraph.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from psx_desk.config import DeskConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Tier = Literal["deep", "quick"]


class StructuredOutputError(RuntimeError):
    """The model could not be made to return a valid instance of the schema."""


@runtime_checkable
class StructuredInvoker(Protocol):
    """Anything that can turn (schema, system, user) into a validated model."""

    def invoke(self, schema: type[T], *, system: str, user: str, tier: Tier = "quick") -> T: ...


# --------------------------------------------------------------------------- #
# Shared parsing / repair loop
# --------------------------------------------------------------------------- #


def _strip_fences(text: str) -> str:
    """Models occasionally wrap JSON in a markdown fence despite json mode."""

    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        return fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


class BaseStructuredInvoker:
    """Validation, one repair round-trip, and a hard failure after that."""

    def __init__(self, config: DeskConfig) -> None:
        self.config = config
        self.calls: list[tuple[str, str]] = []  # (model, schema) audit trail

    # -- backend seam ----------------------------------------------------- #

    def _generate(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> str:
        raise NotImplementedError

    def _model_for(self, tier: Tier) -> str:
        return self.config.deep_model if tier == "deep" else self.config.quick_model

    # -- public API ------------------------------------------------------- #

    def invoke(self, schema: type[T], *, system: str, user: str, tier: Tier = "quick") -> T:
        model = self._model_for(tier)
        self.calls.append((model, schema.__name__))
        last_error: Exception | None = None
        prompt = user

        for attempt in range(self.config.repairs + 1):
            raw = self._generate(model=model, system=system, user=prompt, schema=schema)
            try:
                parsed = schema.model_validate_json(_strip_fences(raw))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.config.repairs:
                    break
                logger.warning(
                    "%s: %s output failed validation (%s); retrying with the error",
                    schema.__name__,
                    model,
                    str(exc).splitlines()[0][:200],
                )
                prompt = (
                    f"{user}\n\n"
                    "Your previous answer did not satisfy the required JSON schema:\n"
                    f"{exc}\n"
                    "Return ONLY a single JSON object that satisfies the schema. "
                    "Do not explain, do not add prose, do not wrap it in markdown."
                )
                continue
            return parsed

        raise StructuredOutputError(
            f"{model} did not return a valid {schema.__name__} after "
            f"{self.config.repairs + 1} attempt(s): {last_error}"
        ) from last_error


# --------------------------------------------------------------------------- #
# google-genai backend
# --------------------------------------------------------------------------- #


class GoogleGenAIInvoker(BaseStructuredInvoker):
    """Official ``google-genai`` SDK backend with JSON-schema-constrained output."""

    def __init__(self, config: DeskConfig) -> None:
        super().__init__(config)
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise StructuredOutputError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc
        if not config.api_key:
            raise StructuredOutputError(
                "no API key: set GOOGLE_API_KEY (or PSXDESK_API_KEY) before running the desk."
            )
        self._genai = genai
        self._client = genai.Client(api_key=config.api_key)

    def _generate(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> str:
        cfg: dict[str, Any] = {
            "system_instruction": system,
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_output_tokens,
            "response_mime_type": "application/json",
        }
        # Prefer the raw JSON schema: it is produced by Pydantic itself and
        # avoids SDK-side schema translation quirks.
        try:
            config = self._genai.types.GenerateContentConfig(
                **cfg, response_json_schema=schema.model_json_schema()
            )
        except TypeError:  # older SDK builds only accept response_schema
            config = self._genai.types.GenerateContentConfig(**cfg, response_schema=schema)

        response = self._client.models.generate_content(model=model, contents=user, config=config)
        text = getattr(response, "text", None)
        if not text:
            raise StructuredOutputError(f"{model} returned an empty response")
        return text


# --------------------------------------------------------------------------- #
# LangChain backend
# --------------------------------------------------------------------------- #


class LangChainInvoker(BaseStructuredInvoker):
    """``with_structured_output`` backend for langchain chat models."""

    SUPPORTED = {
        "google_genai": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
        "openai": ("langchain_openai", "ChatOpenAI"),
        "anthropic": ("langchain_anthropic", "ChatAnthropic"),
    }

    def __init__(self, config: DeskConfig) -> None:
        super().__init__(config)
        provider = config.llm_provider.lower()
        if provider not in self.SUPPORTED:
            raise StructuredOutputError(
                f"unsupported langchain provider {provider!r}; expected {sorted(self.SUPPORTED)}"
            )
        module_name, class_name = self.SUPPORTED[provider]
        try:
            module = __import__(module_name, fromlist=[class_name])
        except ImportError as exc:  # pragma: no cover
            raise StructuredOutputError(
                f"{module_name} is not installed; pip install {module_name.replace('_', '-')}"
            ) from exc
        self._chat_class = getattr(module, class_name)
        self._models: dict[str, Any] = {
            "deep": self._build(self.config.deep_model),
            "quick": self._build(self.config.quick_model),
        }

    def _build(self, model: str):
        kwargs: dict[str, Any] = {"model": model, "temperature": self.config.temperature}
        if self.config.api_key:
            key_name = "google_api_key" if self.config.llm_provider == "google_genai" else "api_key"
            kwargs[key_name] = self.config.api_key
        return self._chat_class(**kwargs)

    def _generate(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> str:
        tier = "deep" if model == self.config.deep_model else "quick"
        chat = self._models[tier]
        try:
            structured = chat.with_structured_output(schema, method="json_schema")
        except (TypeError, NotImplementedError):  # provider lacks json_schema method
            structured = chat.with_structured_output(schema)
        result = structured.invoke([("system", system), ("human", user)])
        if isinstance(result, BaseModel):
            return result.model_dump_json()
        if isinstance(result, dict):
            return json.dumps(result)
        return str(result)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_invoker(config: DeskConfig) -> StructuredInvoker:
    """Pick the backend named in configuration."""

    backend = (config.llm_backend or "google-genai").lower()
    if backend in {"google-genai", "google_genai", "google", "genai"}:
        return GoogleGenAIInvoker(config)
    if backend.startswith("langchain"):
        return LangChainInvoker(config)
    raise StructuredOutputError(
        f"unknown llm_backend {backend!r}; use 'google-genai' or 'langchain'"
    )


def schema_hint(schema: type[BaseModel]) -> str:
    """Compact field list for a prompt (the schema itself is enforced separately)."""

    lines = []
    for name, field in schema.model_fields.items():
        required = "required" if field.is_required() else "optional"
        lines.append(f"- {name} ({required}): {field.description or ''}".rstrip())
    return "\n".join(lines)


__all__ = [
    "BaseStructuredInvoker",
    "GoogleGenAIInvoker",
    "LangChainInvoker",
    "StructuredInvoker",
    "StructuredOutputError",
    "build_invoker",
    "schema_hint",
]
