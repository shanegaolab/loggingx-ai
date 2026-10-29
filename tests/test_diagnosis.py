import asyncio

from loggingx_ai.config import Settings
from loggingx_ai.diagnosis import Diagnoser
from loggingx_ai.diagnosis.prompts import Review
from loggingx_ai.schemas import CodeRef, DiagnoseRequest
from tests.conftest import FakeLlm
from tests.test_stacktrace import JAVA

REF = CodeRef(path="com/shop/order/OrderService.java", start_line=80, focus_line=88, frame="submit", text="x")


def run(llm: FakeLlm, request: DiagnoseRequest, **settings):
    return asyncio.run(Diagnoser(llm, Settings(_env_file=None, **settings)).diagnose(request))


def test_all_agents_run_with_code():
    llm = FakeLlm()
    result = run(llm, DiagnoseRequest(log_text=JAVA, code_refs=[REF]))

    agents = [agent for agent, _ in llm.calls]
    assert set(agents[:2]) == {"log_analyst", "code_analyst"}
    assert agents[2:] == ["root_cause", "fixer", "reviewer"]
    assert result.confidence == "high"
    assert result.diff == "--- a/x\n+++ b/x"
    assert result.error_type == "java.lang.NullPointerException"
    assert result.error_category == "空指针"
    assert (result.provider, result.prompt_tokens, result.completion_tokens) == ("deepseek", 50, 25)


def test_code_analyst_skipped_without_code():
    llm = FakeLlm()
    run(llm, DiagnoseRequest(log_text=JAVA))
    assert "code_analyst" not in [agent for agent, _ in llm.calls]
    assert "没有关联的代码仓库" in llm.calls[0][1] or "没有关联的代码仓库" in llm.calls[1][1]


def test_rejected_fix_is_retried_then_flagged():
    rejected = Review(approved=False, issues=["diff 路径错误"], confidence="high")
    llm = FakeLlm(reviews=[rejected, rejected])
    result = run(llm, DiagnoseRequest(log_text=JAVA, code_refs=[REF]), max_review_rounds=1)

    agents = [agent for agent, _ in llm.calls]
    assert agents.count("fixer") == 2 and agents.count("reviewer") == 2
    assert "diff 路径错误" in [call for agent, call in llm.calls if agent == "fixer"][1]
    assert result.confidence == "medium"
    assert "评审未通过" in result.suggestion


def test_rejected_then_approved():
    llm = FakeLlm(reviews=[Review(approved=False, issues=["x"], confidence="low"),
                           Review(approved=True, confidence="medium")])
    result = run(llm, DiagnoseRequest(log_text=JAVA, code_refs=[REF]))
    assert result.confidence == "medium" and result.review_issues == []
