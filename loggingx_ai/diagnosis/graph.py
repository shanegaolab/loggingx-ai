"""The diagnosis workflow: five agents on a LangGraph state graph.

    parse -> locate_code -> log_analyst  ─┐
                         -> code_analyst ─┴-> root_cause -> fixer -> reviewer -> finalize
                                                              ^________|  (rejected, rounds left)

log_analyst and code_analyst run in parallel; code_analyst is skipped when no code was found.
"""

import operator
from collections.abc import AsyncIterator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from loggingx_ai import code
from loggingx_ai.config import Settings
from loggingx_ai.diagnosis import prompts
from loggingx_ai.diagnosis.prompts import CodeFindings, Fix, LogFindings, Review, RootCause
from loggingx_ai.llm import LlmClient
from loggingx_ai.schemas import AgentStep, CodeRef, DiagnoseRequest, DiagnoseResponse
from loggingx_ai.stacktrace import ParsedError, parse

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


class DiagnosisState(TypedDict, total=False):
    request: DiagnoseRequest
    parsed: ParsedError
    code_refs: list[CodeRef]
    code_note: str
    log_findings: LogFindings
    code_findings: CodeFindings
    root_cause: RootCause
    fix: Fix
    review: Review
    review_rounds: int
    steps: Annotated[list[AgentStep], operator.add]
    result: DiagnoseResponse


class Diagnoser:
    """Builds the graph once and runs one diagnosis per request."""

    def __init__(self, llm: LlmClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings
        self.graph = self._build()

    def _build(self):
        graph = StateGraph(DiagnosisState)
        graph.add_node("parse", self._parse)
        graph.add_node("locate_code", self._locate_code)
        graph.add_node("log_analyst", self._log_analyst)
        graph.add_node("code_analyst", self._code_analyst)
        graph.add_node("root_cause", self._root_cause)
        graph.add_node("fixer", self._fixer)
        graph.add_node("reviewer", self._reviewer)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "parse")
        graph.add_edge("parse", "locate_code")
        graph.add_conditional_edges("locate_code", self._analysts, ["log_analyst", "code_analyst"])
        graph.add_edge("log_analyst", "root_cause")
        graph.add_edge("code_analyst", "root_cause")
        graph.add_edge("root_cause", "fixer")
        graph.add_edge("fixer", "reviewer")
        graph.add_conditional_edges("reviewer", self._after_review, ["fixer", "finalize"])
        graph.add_edge("finalize", END)
        return graph.compile()

    async def diagnose(self, request: DiagnoseRequest) -> DiagnoseResponse:
        state = await self.graph.ainvoke({"request": request, "review_rounds": 0, "steps": []})
        return state["result"]

    async def stream(self, request: DiagnoseRequest) -> AsyncIterator[tuple[str, Any]]:
        """Yields (node, update) as each agent finishes; the last update holds the result."""

        async for chunk in self.graph.astream(
            {"request": request, "review_rounds": 0, "steps": []}, stream_mode="updates"
        ):
            for node, update in chunk.items():
                yield node, update

    # Nodes

    def _parse(self, state: DiagnosisState) -> dict:
        return {"parsed": parse(state["request"].log_text)}

    def _locate_code(self, state: DiagnosisState) -> dict:
        request = state["request"]
        if request.code_refs:
            return {"code_refs": request.code_refs}
        repo = code.store_repo(self._settings.repo_root, request.store_id)
        if repo is None:
            return {"code_refs": [], "code_note": "没有关联的代码仓库，只能依据日志分析。"}
        parsed = state["parsed"]
        # Application frames first, then library frames as a last resort.
        frames = parsed.app_frames() + [frame for frame in parsed.frames if frame.library]
        refs = code.snippets(repo, frames, self._settings.max_code_refs, self._settings.code_context_lines,
                             self._settings.max_code_chars)
        return {"code_refs": refs, "code_note": "" if refs else "仓库中没有找到堆栈对应的代码。"}

    def _analysts(self, state: DiagnosisState) -> list[str]:
        return ["log_analyst", "code_analyst"] if state.get("code_refs") else ["log_analyst"]

    async def _log_analyst(self, state: DiagnosisState) -> dict:
        findings, step = await self._llm.ask("log_analyst", prompts.LOG_ANALYST, self._log_section(state), LogFindings)
        return {"log_findings": findings, "steps": [step]}

    async def _code_analyst(self, state: DiagnosisState) -> dict:
        user = self._log_section(state) + "\n\n" + self._code_section(state)
        findings, step = await self._llm.ask("code_analyst", prompts.CODE_ANALYST, user, CodeFindings)
        return {"code_findings": findings, "steps": [step]}

    async def _root_cause(self, state: DiagnosisState) -> dict:
        user = "\n\n".join([self._log_section(state), self._code_section(state), self._findings_section(state)])
        root, step = await self._llm.ask("root_cause", prompts.ROOT_CAUSE, user, RootCause)
        return {"root_cause": root, "steps": [step]}

    async def _fixer(self, state: DiagnosisState) -> dict:
        root = state["root_cause"]
        parts = [
            self._log_section(state),
            self._code_section(state),
            f"## 根本原因\n{root.summary}\n\n{root.root_cause}",
        ]
        review = state.get("review")
        if review is not None and not review.approved:
            previous = state["fix"]
            parts.append(f"## 上一版修复\n{previous.suggestion}\n\n```diff\n{previous.diff}\n```")
            parts.append("## 评审意见（必须解决）\n" + "\n".join(f"- {issue}" for issue in review.issues))
        fix, step = await self._llm.ask("fixer", prompts.FIXER, "\n\n".join(parts), Fix)
        return {"fix": Fix(suggestion=fix.suggestion.strip(), diff=_strip_fence(fix.diff)), "steps": [step]}

    async def _reviewer(self, state: DiagnosisState) -> dict:
        root, fix = state["root_cause"], state["fix"]
        user = "\n\n".join([
            self._log_section(state),
            self._code_section(state),
            f"## 诊断\n结论：{root.summary}\n\n根本原因：{root.root_cause}\n\n证据：\n"
            + "\n".join(f"- {item}" for item in root.evidence),
            f"## 修复方案\n{fix.suggestion}\n\n```diff\n{fix.diff or '（无 diff）'}\n```",
        ])
        review, step = await self._llm.ask("reviewer", prompts.REVIEWER, user, Review)
        return {"review": review, "review_rounds": state.get("review_rounds", 0) + 1, "steps": [step]}

    def _after_review(self, state: DiagnosisState) -> str:
        review = state["review"]
        # review_rounds counts reviews, so the fixer gets max_review_rounds extra attempts.
        if not review.approved and state["review_rounds"] <= self._settings.max_review_rounds:
            return "fixer"
        return "finalize"

    def _finalize(self, state: DiagnosisState) -> dict:
        parsed, root, fix, review = state["parsed"], state["root_cause"], state["fix"], state["review"]
        steps = state.get("steps", [])
        confidence = min(root.confidence, review.confidence, key=_CONFIDENCE_ORDER.__getitem__)
        suggestion = fix.suggestion
        if not review.approved:
            # The reviewer's open issues stay visible, and the result is not trusted beyond medium.
            confidence = min(confidence, "medium", key=_CONFIDENCE_ORDER.__getitem__)
            suggestion += "\n\n> **评审未通过，请人工确认：**\n" + "\n".join(f"> - {issue}" for issue in review.issues)
        root_step = next((step for step in steps if step.agent == "root_cause"), None)
        log_findings = state.get("log_findings")
        result = DiagnoseResponse(
            summary=root.summary.strip()[:500],
            root_cause=root.root_cause.strip(),
            suggestion=suggestion,
            diff=fix.diff or None,
            confidence=confidence,
            error_type=parsed.error_type,
            fingerprint=parsed.fingerprint(),
            error_category=log_findings.error_category if log_findings else None,
            code_refs=state.get("code_refs", []),
            review_issues=review.issues,
            provider=root_step.provider if root_step else None,
            model=root_step.model if root_step else None,
            prompt_tokens=sum(step.prompt_tokens for step in steps),
            completion_tokens=sum(step.completion_tokens for step in steps),
            steps=steps,
        )
        return {"result": result}

    # Prompt sections

    @staticmethod
    def _log_section(state: DiagnosisState) -> str:
        request, parsed = state["request"], state["parsed"]
        lines = ["## 错误日志"]
        for label, value in (
            ("日志库", request.store_name),
            ("主机", request.host),
            ("来源", request.source),
            ("时间", request.time.isoformat(sep=" ", timespec="seconds") if request.time else None),
            ("异常类型", parsed.error_type),
            ("出错位置", location.label() if (location := parsed.location()) else None),
        ):
            if value:
                lines.append(f"{label}：{value}")
        lines.append(f"```\n{request.log_text}\n```")
        return "\n".join(lines)

    @staticmethod
    def _code_section(state: DiagnosisState) -> str:
        refs = state.get("code_refs") or []
        if not refs:
            return "## 相关代码\n" + (state.get("code_note") or "没有可用的代码。")
        commit = state["request"].repo_commit
        title = "## 相关代码" + (f"（提交 {commit[:10]}）" if commit else "")
        return f"{title}\n每行前是行号，> 标出堆栈指向的行。\n\n{code.render(refs)}"

    @staticmethod
    def _findings_section(state: DiagnosisState) -> str:
        parts = []
        if log_findings := state.get("log_findings"):
            parts.append("## 日志分析结论\n" + log_findings.model_dump_json(indent=2))
        if code_findings := state.get("code_findings"):
            parts.append("## 代码分析结论\n" + code_findings.model_dump_json(indent=2))
        return "\n\n".join(parts)


def _strip_fence(diff: str) -> str:
    text = diff.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text[:-3]
    return text.strip("\n")
