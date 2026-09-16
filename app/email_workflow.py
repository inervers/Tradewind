"""Checkpointed LangGraph workflow for the single-email compose path."""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from app.email_agent import (
    _ensure_signature,
    check_generation_draft,
    generate_draft,
    judge_generation_draft,
    retrieve_generation_context,
)
from app.memory import save_email_once


class EmailWorkflowState(TypedDict, total=False):
    workflow_id: str
    customer_id: str
    customer: str
    country: str
    product: str
    extra: str
    judge: bool
    language: str
    format: str
    template_record: dict | None
    company_profile: dict
    generation_context: dict
    draft: str
    previous_draft: str
    issues: list[str]
    scores: dict | None
    judge_status: str
    attempt: int
    max_attempts: int
    tokens: int
    started_at: float
    approval_status: str
    outreach_job_id: int
    history_id: int
    result: dict


@dataclass
class WorkflowContext:
    cancel_check: Callable[[], bool] | None = None
    stream_callback: Callable[[str], None] | None = None
    reset_stream_callback: Callable[[], None] | None = None
    now: Callable[[], float] = time.monotonic


class EmailWorkflowSteps:
    """Production adapter around the existing generation and persistence helpers."""

    def retrieve(self, state: EmailWorkflowState) -> dict:
        return retrieve_generation_context(
            state["customer"],
            state["country"],
            state["product"],
            state.get("extra", ""),
            state["language"],
            state["format"],
            state.get("template_record"),
            state.get("company_profile") or None,
        )

    def generate(
        self, state: EmailWorkflowState, context: WorkflowContext
    ) -> tuple[str, int]:
        draft, tokens = generate_draft(
            state["generation_context"],
            retry_issues=state.get("issues", []),
            previous_draft=state.get("previous_draft", ""),
            cancel_check=context.cancel_check,
            stream_callback=context.stream_callback,
        )
        generation_context = state["generation_context"]
        return (
            _ensure_signature(
                draft,
                generation_context.get("company_profile", {}),
                state["format"],
            ),
            tokens,
        )

    def check(self, state: EmailWorkflowState) -> list[str]:
        return check_generation_draft(state["draft"], state["generation_context"])

    def judge(
        self, state: EmailWorkflowState, context: WorkflowContext
    ) -> tuple[dict | None, str]:
        return judge_generation_draft(
            state["draft"], state.get("judge", False), context.cancel_check
        )

    def persist(self, state: EmailWorkflowState) -> int:
        scores = state.get("scores") or {}
        return save_email_once(
            state["workflow_id"],
            state["customer"],
            state["country"],
            state["product"],
            state["draft"],
            float(scores.get("overall", 0)),
            state["language"],
            state["format"],
        )


def after_rule_check(state: EmailWorkflowState) -> str:
    if state.get("issues") and state.get("attempt", 0) < state.get("max_attempts", 2):
        return "generate"
    return "judge"


class EmailWorkflow:
    def __init__(
        self,
        checkpointer: Any,
        steps: EmailWorkflowSteps | Any | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.steps = steps or EmailWorkflowSteps()
        self._connection = connection
        builder = StateGraph(EmailWorkflowState, context_schema=WorkflowContext)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("generate", self._generate)
        builder.add_node("rule_check", self._rule_check)
        builder.add_node("judge", self._judge)
        builder.add_node("human_approval", self._human_approval)
        builder.add_node("persist", self._persist)
        builder.add_edge(START, "retrieve")
        builder.add_edge("retrieve", "generate")
        builder.add_edge("generate", "rule_check")
        builder.add_conditional_edges(
            "rule_check",
            after_rule_check,
            {"generate": "generate", "judge": "judge"},
        )
        builder.add_conditional_edges(
            "judge",
            self._after_judge,
            {"human_approval": "human_approval", "persist": "persist"},
        )
        builder.add_conditional_edges(
            "human_approval",
            self._after_approval,
            {"persist": "persist", "end": END},
        )
        builder.add_edge("persist", END)
        self.graph = builder.compile(checkpointer=checkpointer)

    @staticmethod
    def _config(workflow_id: str) -> dict:
        return {"configurable": {"thread_id": workflow_id}}

    @staticmethod
    def _context(runtime: Runtime[WorkflowContext]) -> WorkflowContext:
        return runtime.context or WorkflowContext()

    def _retrieve(self, state: EmailWorkflowState) -> dict:
        return {"generation_context": self.steps.retrieve(state)}

    def _generate(
        self, state: EmailWorkflowState, runtime: Runtime[WorkflowContext]
    ) -> dict:
        context = self._context(runtime)
        attempt = state.get("attempt", 0) + 1
        previous_draft = state.get("draft", "")
        next_state = dict(state)
        next_state["attempt"] = attempt
        next_state["previous_draft"] = previous_draft
        if attempt > 1 and context.reset_stream_callback:
            context.reset_stream_callback()
        draft, tokens = self.steps.generate(next_state, context)
        return {
            "attempt": attempt,
            "previous_draft": previous_draft,
            "draft": draft,
            "tokens": state.get("tokens", 0) + int(tokens),
        }

    def _rule_check(self, state: EmailWorkflowState) -> dict:
        return {"issues": self.steps.check(state)}

    def _judge(
        self, state: EmailWorkflowState, runtime: Runtime[WorkflowContext]
    ) -> dict:
        context = self._context(runtime)
        scores, judge_status = self.steps.judge(state, context)
        elapsed = max(0.0, context.now() - state.get("started_at", context.now()))
        result = {
            "workflow_id": state["workflow_id"],
            "customer_id": state.get("customer_id", ""),
            "customer": state["customer"],
            "country": state["country"],
            "product": state["product"],
            "email": state["draft"],
            "issues": state.get("issues", []),
            "scores": scores,
            "judge_status": judge_status,
            "tokens": state.get("tokens", 0),
            "time_s": round(elapsed, 1),
            "language": state["language"],
            "format": state["format"],
            "templates_used": state["generation_context"].get("templates_used", []),
        }
        return {"scores": scores, "judge_status": judge_status, "result": result}

    @staticmethod
    def _after_judge(state: EmailWorkflowState) -> str:
        return "human_approval" if state.get("customer_id") else "persist"

    @staticmethod
    def _human_approval(state: EmailWorkflowState) -> dict:
        decision = interrupt({
            "workflow_id": state["workflow_id"],
            "customer": state["customer"],
            "issues": state.get("issues", []),
        })
        action = str(decision.get("action", "reject")) if isinstance(decision, dict) else "reject"
        return {
            "approval_status": "approved" if action == "approve" else "rejected",
            "outreach_job_id": int(decision.get("outreach_job_id", 0)) if isinstance(decision, dict) else 0,
        }

    @staticmethod
    def _after_approval(state: EmailWorkflowState) -> str:
        return "persist" if state.get("approval_status") == "approved" else "end"

    def _persist(self, state: EmailWorkflowState) -> dict:
        history_id = int(self.steps.persist(state))
        return {
            "history_id": history_id,
            "result": {**state.get("result", {}), "history_id": history_id},
        }

    def start(
        self,
        workflow_id: str,
        inputs: dict,
        context: WorkflowContext,
    ) -> dict:
        workflow_id = workflow_id.strip()
        if not workflow_id:
            raise ValueError("workflow_id 不能为空")
        config = self._config(workflow_id)
        existing = self.graph.get_state(config)
        if existing.values:
            return self.get_task_projection(workflow_id)
        initial: EmailWorkflowState = {
            **inputs,
            "workflow_id": workflow_id,
            "customer_id": str(inputs.get("customer_id", "")),
            "attempt": 0,
            "max_attempts": 2,
            "tokens": 0,
            "issues": [],
            "draft": "",
            "previous_draft": "",
            "scores": None,
            "judge_status": "disabled",
            "approval_status": "pending" if inputs.get("customer_id") else "not_required",
            "outreach_job_id": 0,
            "history_id": 0,
            "started_at": context.now(),
        }
        self.graph.invoke(initial, config, context=context)
        return self.get_task_projection(workflow_id)

    def resume_approval(
        self, workflow_id: str, action: str, outreach_job_id: int = 0
    ) -> dict:
        workflow_id = workflow_id.strip()
        if action not in {"approve", "reject"}:
            raise ValueError("人工确认动作无效")
        config = self._config(workflow_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            return {"status": "not_found"}
        if not snapshot.next:
            return self.get_task_projection(workflow_id)
        self.graph.invoke(
            Command(
                resume={"action": action, "outreach_job_id": int(outreach_job_id)}
            ),
            config,
            context=WorkflowContext(),
        )
        return self.get_task_projection(workflow_id)

    def get_task_projection(self, workflow_id: str) -> dict:
        snapshot = self.graph.get_state(self._config(workflow_id))
        state = snapshot.values
        if not state:
            return {"status": "not_found"}
        result = dict(state.get("result") or {})
        if result:
            result["workflow_id"] = state.get("workflow_id", workflow_id)
            result["customer_id"] = state.get("customer_id", "")
        return {
            "status": "done",
            "total": 1,
            "done": 1,
            "current": state.get("customer", ""),
            "results": [],
            "result": result or None,
            "error": "",
            "stream": state.get("draft", ""),
        }

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def build_sqlite_workflow(path: Path) -> EmailWorkflow:
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    return EmailWorkflow(saver, connection=connection)
