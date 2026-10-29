import asyncio

import pytest
from pydantic import BaseModel

from loggingx_ai.config import Settings
from loggingx_ai.llm import ChatClient, LlmError, ModelSpec, parse_json
from loggingx_ai.schemas import AgentStep


class Answer(BaseModel):
    value: int


def test_model_spec_parse():
    assert ModelSpec.parse("qwen:qwen3-coder-plus") == ModelSpec("qwen", "qwen3-coder-plus")
    with pytest.raises(ValueError):
        ModelSpec.parse("openai:gpt")


def test_candidates_fall_back_to_configured_provider():
    both = ChatClient(Settings(_env_file=None, deepseek_api_key="d", qwen_api_key="q"))
    assert both.candidates("fixer") == [ModelSpec("qwen", "qwen3-coder-plus"), ModelSpec("deepseek", "deepseek-chat")]
    only_deepseek = ChatClient(Settings(_env_file=None, deepseek_api_key="d"))
    assert only_deepseek.candidates("fixer") == [ModelSpec("deepseek", "deepseek-chat")]


def test_ask_tries_next_provider_and_reports_all_failures():
    client = ChatClient(Settings(_env_file=None, deepseek_api_key="d", qwen_api_key="q"))
    tried = []

    async def fake_ask(spec, agent, system, user, schema):
        tried.append(spec.provider)
        if spec.provider == "qwen":
            raise RuntimeError("quota")
        return Answer(value=1), AgentStep(agent=agent, provider=spec.provider, model=spec.model)

    client._ask = fake_ask
    answer, step = asyncio.run(client.ask("fixer", "s", "u", Answer))
    assert tried == ["qwen", "deepseek"] and step.provider == "deepseek" and answer.value == 1

    with pytest.raises(LlmError, match="没有配置"):
        asyncio.run(ChatClient(Settings(_env_file=None)).ask("fixer", "s", "u", Answer))


def test_parse_json_ignores_fences():
    assert parse_json('```json\n{"value": 2}\n```') == {"value": 2}
    with pytest.raises(ValueError):
        parse_json("no json")
