"""Chat models of DeepSeek and Qwen (DashScope), both through their OpenAI-compatible APIs."""

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from loggingx_ai.config import Settings
from loggingx_ai.schemas import AgentStep

log = logging.getLogger(__name__)

PROVIDERS = ("deepseek", "qwen")


class LlmError(RuntimeError):
    """No configured model produced a usable answer."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> ModelSpec:
        provider, sep, model = value.partition(":")
        provider = provider.strip().lower()
        if not sep or provider not in PROVIDERS or not model.strip():
            raise ValueError(f"Model must look like 'deepseek:<model>' or 'qwen:<model>', got {value!r}")
        return cls(provider, model.strip())


class LlmClient(Protocol):
    async def ask[T: BaseModel](self, agent: str, system: str, user: str, schema: type[T]) -> tuple[T, AgentStep]:
        """Asks the agent's model for a JSON object matching the schema."""
        ...


class ChatClient:
    """Routes each agent to its configured model and falls back to the other provider when it fails."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._models: dict[ModelSpec, ChatOpenAI] = {}

    def configured_providers(self) -> list[str]:
        return [provider for provider in PROVIDERS if self._key(provider) is not None]

    def _key(self, provider: str) -> str | None:
        secret = self._settings.deepseek_api_key if provider == "deepseek" else self._settings.qwen_api_key
        return secret.get_secret_value() if secret is not None and secret.get_secret_value() else None

    def candidates(self, agent: str) -> list[ModelSpec]:
        """The agent's model first, then the other provider's default model."""

        primary = ModelSpec.parse(getattr(self._settings, f"{agent}_model"))
        defaults = {
            "deepseek": ModelSpec("deepseek", self._settings.deepseek_model),
            "qwen": ModelSpec("qwen", self._settings.qwen_model),
        }
        ordered = [primary, *(defaults[p] for p in PROVIDERS if p != primary.provider)]
        return [spec for spec in ordered if self._key(spec.provider) is not None]

    def _chat(self, spec: ModelSpec) -> ChatOpenAI:
        model = self._models.get(spec)
        if model is None:
            settings = self._settings
            base_url = settings.deepseek_base_url if spec.provider == "deepseek" else settings.qwen_base_url
            model = ChatOpenAI(
                model=spec.model,
                api_key=self._key(spec.provider),
                base_url=base_url,
                temperature=settings.temperature,
                timeout=settings.request_timeout,
                max_retries=settings.max_retries,
            )
            self._models[spec] = model
        return model

    async def ask[T: BaseModel](self, agent: str, system: str, user: str, schema: type[T]) -> tuple[T, AgentStep]:
        candidates = self.candidates(agent)
        if not candidates:
            raise LlmError("没有配置 AI 提供方：请设置 LOGGINGX_AI_DEEPSEEK_API_KEY 或 LOGGINGX_AI_QWEN_API_KEY")
        failures: list[str] = []
        for spec in candidates:
            try:
                return await self._ask(spec, agent, system, user, schema)
            except Exception as error:  # noqa: BLE001 - any provider failure moves on to the next provider
                log.warning("event=llm.failed agent=%s provider=%s model=%s error=%s", agent, spec.provider,
                            spec.model, error)
                failures.append(f"{spec.provider}:{spec.model}: {error}")
        raise LlmError(f"{agent} 调用模型失败：" + "；".join(failures))

    async def _ask[T: BaseModel](
        self, spec: ModelSpec, agent: str, system: str, user: str, schema: type[T]
    ) -> tuple[T, AgentStep]:
        model = self._chat(spec).bind(response_format={"type": "json_object"})
        messages = [SystemMessage(system), HumanMessage(user)]
        step = AgentStep(agent=agent, provider=spec.provider, model=spec.model)
        started = time.monotonic()
        # One retry with the validation error when the model's JSON does not match the schema.
        for attempt in range(2):
            reply = await model.ainvoke(messages)
            usage = reply.usage_metadata or {}
            step.prompt_tokens += usage.get("input_tokens", 0)
            step.completion_tokens += usage.get("output_tokens", 0)
            try:
                result = schema.model_validate(parse_json(reply.text))
            except (ValueError, ValidationError) as error:
                if attempt == 1:
                    raise LlmError(f"模型返回的 JSON 不符合要求：{error}") from error
                messages += [reply, HumanMessage(f"上面的输出不是要求的 JSON：{error}。请只输出符合要求的 JSON 对象。")]
                continue
            step.duration_ms = int((time.monotonic() - started) * 1000)
            return result, step
        raise AssertionError("unreachable")


def parse_json(text: str) -> dict:
    """The JSON object in a reply, ignoring code fences or text around it."""

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中没有 JSON 对象")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("回复不是 JSON 对象")
    return value

