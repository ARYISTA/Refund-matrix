"""Refund case graph: validated agents, deterministic policy, human interrupt, and audit.

The graph deliberately receives its provider, database session factory, clock, and payout
callable as dependencies. This keeps graph tests deterministic and leaves the real payout
gateway for Phase 7.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_policy
from app.contracts.agents import RefundIntakeOutput, TriageOutput
from app.contracts.decisions import PolicyResult
from app.contracts.enums import CaseStatus, Intent, NodeName, NodeStatus, PolicyDecision, ReviewAction
from app.contracts.facts import RefundFacts
from app.contracts.payout import PayoutResult
from app.contracts.state import CaseState, ChatMessage, ReviewDecision
from app.db.models import AuditEvent, Case, PolicySetting, Refund
from app.db.queries import get_order_facts
from app.llm.errors import LLMError
from app.llm.provider import LLMProvider, ModelRole
from app.llm.tools import ToolContext, tools_for
from app.payout import MockPayoutGateway
from app.policy.engine import evaluate_refund

log = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    case_id: UUID
    thread_id: str
    customer_id: str
    messages: list[ChatMessage]
    status: CaseStatus
    current_node: NodeName | None
    last_node_status: NodeStatus | None
    triage: TriageOutput | None
    intake: RefundIntakeOutput | None
    facts: RefundFacts | None
    policy_result: PolicyResult | None
    review: ReviewDecision | None
    payout: PayoutResult | None
    resolution_text: str | None
    reply: str | None
    handoff_reason: str | None
    policy_version: str | None
    prompt_version: str | None
    trace_id: str | None
    created_at: datetime | None


SessionFactory = Callable[[], AbstractContextManager[Session]]
PayoutCallable = Callable[[CaseState, Session], PayoutResult]


class WorkflowDependencies:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: SessionFactory,
        payout: PayoutCallable | None = None,
        tool_context: ToolContext | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        triage_confidence_threshold: float = 0.7,
        prompt_version: str = "v1",
    ) -> None:
        self.provider = provider
        self.session_factory = session_factory
        self.payout = payout
        self.tool_context = tool_context
        self.now = now
        self.triage_confidence_threshold = triage_confidence_threshold
        self.prompt_version = prompt_version


def _state(raw: GraphState) -> CaseState:
    return CaseState.model_validate(raw)


def _audit(
    deps: WorkflowDependencies, state: GraphState, node: NodeName, status: NodeStatus, **detail: Any
) -> None:
    state_status = state.get("status", CaseStatus.OPEN)
    status_value = state_status.value if isinstance(state_status, CaseStatus) else state_status
    with deps.session_factory() as session:
        case = session.get(Case, str(state["case_id"]))
        if case is None:
            session.add(
                Case(
                    id=str(state["case_id"]),
                    thread_id=state["thread_id"],
                    customer_id=state["customer_id"],
                    status=status_value,
                    trace_id=state.get("trace_id"),
                )
            )
            session.flush()
        else:
            case.status = status_value
            case.trace_id = state.get("trace_id")
        session.add(
            AuditEvent(
                id=str(uuid4()),
                case_id=str(state["case_id"]),
                node=node.value,
                status=status.value,
                detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
                at=deps.now(),
            )
        )
        session.commit()


def _started(deps: WorkflowDependencies, state: GraphState, node: NodeName) -> dict[str, Any]:
    _audit(deps, state, node, NodeStatus.STARTED)
    return {"current_node": node, "last_node_status": NodeStatus.STARTED}


def _completed(
    deps: WorkflowDependencies, state: GraphState, node: NodeName, **detail: Any
) -> dict[str, Any]:
    _audit(deps, state, node, NodeStatus.COMPLETED, **detail)
    return {"current_node": node, "last_node_status": NodeStatus.COMPLETED}


async def triage(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    result = _started(deps, state, NodeName.TRIAGE)
    message = state["messages"][-1].content
    try:
        output = await deps.provider.structured(
            role=ModelRole.TRIAGE,
            system="Classify the customer's latest message and extract an order id when present.",
            schema=TriageOutput,
            untrusted_text=message,
            task="Classify this customer message.",
        )
    except LLMError as exc:
        reason = "The message could not be processed by the automated assistant."
        _audit(deps, state, NodeName.TRIAGE, NodeStatus.FAILED, error=type(exc).__name__)
        return {**result, "status": CaseStatus.HANDED_OFF, "handoff_reason": reason}
    return {
        **result,
        **_completed(deps, state, NodeName.TRIAGE, intent=output.intent.value),
        "triage": output,
    }


def route_after_triage(state: GraphState) -> str:
    triage_result = state.get("triage")
    if triage_result is None or triage_result.intent is not Intent.REFUND_REQUEST:
        return "resolution"
    return "refund_intake"


async def refund_intake(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    result = _started(deps, state, NodeName.REFUND_INTAKE)
    try:
        output = await deps.provider.structured(
            role=ModelRole.AGENT,
            system="Extract the refund reason, order id, and optional item ids. Never infer money.",
            schema=RefundIntakeOutput,
            untrusted_text=state["messages"][-1].content,
            task="Extract the refund request.",
        )
    except LLMError as exc:
        _audit(deps, state, NodeName.REFUND_INTAKE, NodeStatus.FAILED, error=type(exc).__name__)
        return {
            **result,
            "status": CaseStatus.HANDED_OFF,
            "handoff_reason": "The refund details need human review.",
        }
    return {**result, **_completed(deps, state, NodeName.REFUND_INTAKE), "intake": output}


def load_facts(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    result = _started(deps, state, NodeName.LOAD_FACTS)
    intake = state.get("intake")
    if intake is None:
        raise ValueError("load_facts requires intake")
    with deps.session_factory() as session:
        order = get_order_facts(session, intake.order_id, state["customer_id"])
        amount = 0
        if order is not None:
            selected = set(intake.items or ())
            items = order.items if not selected else tuple(i for i in order.items if i.item_id in selected)
            amount = sum(item.line_total_paise for item in items)
        since = deps.now() - timedelta(days=30)
        recent = (
            session.scalar(
                select(func.count())
                .select_from(Refund)
                .where(Refund.customer_id == state["customer_id"], Refund.created_at >= since)
            )
            or 0
        )
    facts = RefundFacts(
        requesting_customer_id=state["customer_id"],
        order=order,
        refund_amount_paise=amount,
        refunds_last_30d=int(recent),
        as_of=deps.now(),
    )
    return {**result, **_completed(deps, state, NodeName.LOAD_FACTS), "facts": facts}


def policy_check(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    result = _started(deps, state, NodeName.POLICY_CHECK)
    facts = state.get("facts")
    intake = state.get("intake")
    if facts is None or intake is None:
        raise ValueError("policy_check requires facts and intake")
    policy = get_policy()
    with deps.session_factory() as session:
        current_setting = session.scalar(
            select(PolicySetting).order_by(PolicySetting.version.desc(), PolicySetting.id.desc())
        )
    if current_setting is not None:
        policy = policy.model_copy(update={"auto_approve_below_paise": current_setting.threshold_paise})
    output = evaluate_refund(facts, intake.reason_category, policy)
    with deps.session_factory() as session:
        case = session.get(Case, str(state["case_id"]))
        if case is not None:
            case.threshold_paise = policy.auto_approve_below_paise
            case.policy_version = policy.version
            case.order_id = intake.order_id
            case.amount_paise = output.amount_paise
            case.reason_category = intake.reason_category.value
            case.decision_type = output.decision.value
            session.commit()
    status = {
        PolicyDecision.REJECT: CaseStatus.REJECTED,
        PolicyDecision.ESCALATE: CaseStatus.PENDING_APPROVAL,
        PolicyDecision.AUTO_APPROVE: CaseStatus.OPEN,
    }[output.decision]
    return {
        **result,
        **_completed(
            deps,
            state,
            NodeName.POLICY_CHECK,
            decision=output.decision.value,
            policy_version=output.policy_version,
        ),
        "policy_result": output,
        "policy_version": output.policy_version,
        "status": status,
    }


def route_after_policy(state: GraphState) -> str:
    result = state.get("policy_result")
    if result is None or result.decision is PolicyDecision.REJECT:
        return "responder"
    if result.decision is PolicyDecision.ESCALATE:
        return "human_review"
    return "payout"


def human_review(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    node = NodeName.HUMAN_REVIEW
    _started(deps, state, node)
    existing_review = state.get("review")
    if existing_review is None:
        policy_result = state.get("policy_result")
        _audit(deps, state, node, NodeStatus.INTERRUPTED, decision="awaiting_reviewer")
        answer = interrupt(
            {
                "reason": "needs_approval",
                "case_id": str(state["case_id"]),
                "policy_result": policy_result.model_dump(mode="json") if policy_result else None,
            }
        )
        review = ReviewDecision.model_validate(answer)
    else:
        review = existing_review
    if review.action is ReviewAction.DENY:
        return {
            **_completed(deps, state, node, action=review.action.value),
            "review": review,
            "status": CaseStatus.DENIED,
        }
    return {
        **_completed(deps, state, node, action=review.action.value),
        "review": review,
        "status": CaseStatus.OPEN,
    }


def route_after_review(state: GraphState) -> str:
    review = state.get("review")
    return "payout" if review is not None and review.action is ReviewAction.APPROVE else "responder"


def payout(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    gateway = deps.payout or MockPayoutGateway(now=deps.now)
    node = NodeName.PAYOUT
    _started(deps, state, node)
    with deps.session_factory() as session:
        result = gateway(_state(state), session)
        session.commit()
    return {
        **_completed(deps, state, node, payout_id=result.payout_id),
        "payout": result,
        "status": CaseStatus.PAID,
    }


async def resolution(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    node = NodeName.RESOLUTION
    started = _started(deps, state, node)
    try:
        if deps.tool_context is None:
            text = await deps.provider.generate_text(
                role=ModelRole.AGENT,
                system="Answer the customer's non-refund question using only verified order information.",
                untrusted_text=state["messages"][-1].content,
                task="Resolve the customer's question.",
            )
        else:
            result = await deps.provider.run_tools(
                role=ModelRole.AGENT,
                system="Answer the customer's question. Use the supplied read-only order tool when needed.",
                tools=tools_for(NodeName.RESOLUTION, deps.tool_context),
                untrusted_text=state["messages"][-1].content,
                task="Resolve the customer's question.",
            )
            text = result.text
    except LLMError as exc:
        _audit(deps, state, node, NodeStatus.FAILED, error=type(exc).__name__)
        return {
            **started,
            "status": CaseStatus.HANDED_OFF,
            "handoff_reason": "A human needs to complete this response.",
        }
    return {
        **started,
        **_completed(deps, state, node),
        "resolution_text": text,
        "status": CaseStatus.OPEN,
    }


async def responder(state: GraphState, *, deps: WorkflowDependencies) -> dict[str, Any]:
    node = NodeName.RESPONDER
    started = _started(deps, state, node)
    system = "Write one short, plain human sentence response using only the verified case result."
    review = state.get("review")
    if state.get("status") is CaseStatus.PAID and review and review.action.value == "approve":
        text = "Your refund request has been approved and the payment has been processed."
    elif state.get("status") is CaseStatus.DENIED and review and review.action.value == "deny":
        text = "Your refund request was reviewed and denied by a manager."
    elif state.get("resolution_text"):
        text = str(state["resolution_text"])
    else:
        try:
            text = await deps.provider.generate_text(
                role=ModelRole.AGENT,
                system=system,
                task=f"Respond to the customer. Case status: {state.get('status', CaseStatus.OPEN).value}.",
            )
        except LLMError as exc:
            _audit(deps, state, node, NodeStatus.FAILED, error=type(exc).__name__)
            return {
                **started,
                "status": CaseStatus.HANDED_OFF,
                "handoff_reason": "A human needs to complete this response.",
            }
    status = state.get("status", CaseStatus.OPEN)
    if status is CaseStatus.OPEN:
        status = CaseStatus.ANSWERED
    messages = list(state.get("messages", []))
    if text and not any(m.role == "assistant" and m.content == text for m in messages):
        messages.append(ChatMessage(role="assistant", content=text))
    return {
        **started,
        **_completed(deps, state, node),
        "reply": text,
        "messages": messages,
        "status": status,
        "prompt_version": deps.prompt_version,
    }


@contextmanager
def make_postgres_checkpointer(connection_string: str) -> Iterator[PostgresSaver]:
    """Open and set up a durable Postgres checkpoint store for a graph lifetime."""
    # SQLAlchemy needs the psycopg driver suffix; psycopg's conninfo parser does not.
    checkpointer_url = connection_string.replace("postgresql+psycopg://", "postgresql://", 1)
    with PostgresSaver.from_conn_string(checkpointer_url) as checkpointer:
        checkpointer.setup()
        yield checkpointer


@asynccontextmanager
async def make_async_postgres_checkpointer(
    connection_string: str,
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open the async checkpointer required by graphs with async agent nodes."""
    checkpointer_url = connection_string.replace("postgresql+psycopg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(checkpointer_url) as checkpointer:
        await checkpointer.setup()
        yield checkpointer


def build_graph(deps: WorkflowDependencies, *, checkpointer: Any | None = None) -> Any:
    builder = StateGraph(GraphState)
    builder.add_node("triage", partial(triage, deps=deps))
    builder.add_node("refund_intake", partial(refund_intake, deps=deps))
    builder.add_node("load_facts", partial(load_facts, deps=deps))
    builder.add_node("policy_check", partial(policy_check, deps=deps))
    builder.add_node("human_review", partial(human_review, deps=deps))
    builder.add_node("payout", partial(payout, deps=deps))
    builder.add_node("resolution", partial(resolution, deps=deps))
    builder.add_node("responder", partial(responder, deps=deps))
    builder.add_edge(START, "triage")

    def triage_route(state: GraphState) -> str:
        result = state.get("triage")
        if result is None or result.intent is not Intent.REFUND_REQUEST:
            return "resolution"
        if result.confidence < deps.triage_confidence_threshold:
            return "resolution"
        return "refund_intake"

    builder.add_conditional_edges(
        "triage", triage_route, {"refund_intake": "refund_intake", "resolution": "resolution"}
    )
    builder.add_edge("refund_intake", "load_facts")
    builder.add_edge("load_facts", "policy_check")
    builder.add_conditional_edges(
        "policy_check",
        route_after_policy,
        {"responder": "responder", "human_review": "human_review", "payout": "payout"},
    )
    builder.add_conditional_edges(
        "human_review", route_after_review, {"responder": "responder", "payout": "payout"}
    )
    builder.add_edge("payout", "responder")
    builder.add_edge("resolution", "responder")
    builder.add_edge("responder", END)
    return builder.compile(checkpointer=checkpointer)
