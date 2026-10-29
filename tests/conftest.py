from pydantic import BaseModel

from loggingx_ai.diagnosis.prompts import CodeFindings, Fix, LogFindings, Review, RootCause
from loggingx_ai.schemas import AgentStep


class FakeLlm:
    """Answers each agent with canned JSON and records what it was asked."""

    def __init__(self, reviews: list[Review] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reviews = list(reviews or [Review(approved=True, confidence="high")])

    async def ask[T: BaseModel](self, agent: str, system: str, user: str, schema: type[T]) -> tuple[T, AgentStep]:
        self.calls.append((agent, user))
        answers = {
            "log_analyst": LogFindings(error_category="空指针", symptoms=["NPE"], hypotheses=["user 为空"]),
            "code_analyst": CodeFindings(hypotheses=["未判空"]),
            "root_cause": RootCause(summary="getUser 返回 null 未判空", root_cause="...", confidence="high"),
            "fixer": Fix(suggestion="增加判空", diff="```diff\n--- a/x\n+++ b/x\n```"),
        }
        answer = self.reviews.pop(0) if agent == "reviewer" else answers[agent]
        assert isinstance(answer, schema)
        provider = "deepseek" if agent in {"log_analyst", "root_cause"} else "qwen"
        return answer, AgentStep(agent=agent, provider=provider, model="m", prompt_tokens=10, completion_tokens=5)
