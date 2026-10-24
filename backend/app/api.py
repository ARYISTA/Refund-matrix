"""Phase 8 FastAPI boundary with secure cookie auth and role-protected endpoints."""

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_policy, get_settings, runtime_database_url
from app.contracts.api import (
    CaseOut,
    ChatRequest,
    CompanyCaseOut,
    CompanyOverviewOut,
    CompanySettingsOut,
    CompanySettingsUpdate,
    CustomerCaseOut,
    CustomerCaseSummaryOut,
    CustomerOrderItemOut,
    CustomerOrderOut,
    LoginRequest,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewerCaseOut,
    ReviewQueueItem,
    TimelineStep,
    UserOut,
)
from app.contracts.enums import CaseStatus, UserRole
from app.contracts.state import CaseState, ChatMessage, ReviewDecision
from app.db.models import (
    AuditEvent,
    Case,
    Customer,
    Order,
    Payout,
    PolicySetting,
    PolicySettingAudit,
    User,
)
from app.graph import (
    WorkflowDependencies,
    build_graph,
    make_async_postgres_checkpointer,
    make_postgres_checkpointer,
)
from app.llm import get_provider
from app.llm.tools import ToolContext
from app.rag import HashEmbeddingProvider, upsert_current_limits
from app.security import create_access_token, current_user, require_role, verify_password

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._requests: defaultdict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def __call__(self, request: Request) -> None:
        now = time.monotonic()
        address = request.client.host if request.client else "unknown"
        with self._lock:
            recent = [stamp for stamp in self._requests[address] if now - stamp < 60]
            if len(recent) >= self.limit:
                raise HTTPException(status_code=429, detail="Too many requests. Please try again shortly.")
            recent.append(now)
            self._requests[address] = recent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Refund Matrix API", docs_url="/docs" if settings.app_env != "production" else None)
    app.state.session_factory = sessionmaker(
        create_engine(runtime_database_url(settings)), expire_on_commit=False
    )
    raw_origins = [
        origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()
    ]
    origins_set = set(raw_origins)
    for origin in raw_origins:
        if "localhost" in origin:
            origins_set.add(origin.replace("localhost", "127.0.0.1"))
        elif "127.0.0.1" in origin:
            origins_set.add(origin.replace("127.0.0.1", "localhost"))
    origins_set.update(["http://localhost:5173", "http://127.0.0.1:5173"])

    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origins_set),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    limiter = RateLimiter(settings.rate_limit_per_minute)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/login", response_model=UserOut, dependencies=[Depends(limiter)])
    def login(body: LoginRequest, response: Response) -> UserOut:
        with app.state.session_factory() as session:
            user = session.scalar(select(User).where(User.email == str(body.email).casefold()))
            if user is None or not verify_password(body.password, user.password_hash):
                raise HTTPException(status_code=401, detail="Email or password is incorrect.")
            response.set_cookie(
                "refund_session",
                create_access_token(user, settings),
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
                max_age=settings.jwt_expire_minutes * 60,
            )
            return UserOut(id=user.id, email=user.email, role=UserRole(user.role))

    @app.post("/auth/logout", status_code=204, dependencies=[Depends(limiter)])
    def logout(response: Response) -> None:
        response.delete_cookie("refund_session")
        return None

    @app.get("/auth/me", response_model=UserOut)
    @app.get("/me", response_model=UserOut, include_in_schema=False)
    def me(user: User = Depends(current_user)) -> UserOut:  # noqa: B008
        return UserOut(id=user.id, email=user.email, role=UserRole(user.role))

    @app.get("/reviews", response_model=list[ReviewQueueItem])
    @app.get("/review/queue", response_model=list[ReviewQueueItem], include_in_schema=False)
    def review_queue(
        _: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> list[ReviewQueueItem]:
        with app.state.session_factory() as session:
            cases = session.scalars(
                select(Case).where(
                    Case.status.in_([CaseStatus.PENDING_APPROVAL.value, CaseStatus.HANDED_OFF.value])
                )
            ).all()
            return [
                ReviewQueueItem(
                    case_id=UUID(case.id),
                    customer_id=case.customer_id,
                    status=CaseStatus(case.status),
                    created_at=case.created_at,
                )
                for case in cases
            ]

    @app.get("/company/overview", response_model=CompanyOverviewOut)
    def company_overview(
        _: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> CompanyOverviewOut:
        with app.state.session_factory() as session:
            rows = session.scalars(select(Case)).all()
            paid = [row for row in rows if row.status == CaseStatus.PAID.value]
            return CompanyOverviewOut(
                pending_approval=sum(row.status == CaseStatus.PENDING_APPROVAL.value for row in rows),
                auto_processed=sum(
                    row.status == CaseStatus.PAID.value and row.decision_type == "AUTO_APPROVE"
                    for row in rows
                ),
                approved=sum(
                    row.status == CaseStatus.PAID.value and row.decision_type == "ESCALATE"
                    for row in rows
                ),
                denied=sum(row.status == CaseStatus.DENIED.value for row in rows),
                rejected=sum(row.status == CaseStatus.REJECTED.value for row in rows),
                total_refunded_paise=sum(
                    value
                    for value in (
                        session.scalars(
                            select(Payout.amount_paise).where(Payout.case_id.in_([row.id for row in paid]))
                        ).all()
                    )
                ),
            )

    @app.get("/company/cases", response_model=list[CompanyCaseOut])
    def company_cases(
        search: str | None = None,
        status: CaseStatus | None = None,
        _: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> list[CompanyCaseOut]:
        with app.state.session_factory() as session:
            query = select(Case, Customer).join(Customer, Customer.id == Case.customer_id)
            if status is not None:
                query = query.where(Case.status == status.value)
            if search:
                term = f"%{search.casefold()}%"
                query = query.where(
                    or_(
                        func.lower(Customer.name).like(term),
                        func.lower(Customer.email).like(term),
                        func.lower(Case.order_id).like(term),
                    )
                )
            return [
                CompanyCaseOut(
                    case_id=UUID(case.id),
                    customer=customer.name,
                    order_id=case.order_id,
                    amount_paise=case.amount_paise,
                    reason=case.reason_category,
                    status=CaseStatus(case.status),
                    decision_type=case.decision_type,
                    created_at=case.created_at,
                )
                for case, customer in session.execute(query.order_by(Case.created_at.desc())).all()
            ]

    @app.get("/company/settings", response_model=CompanySettingsOut)
    def company_settings(
        _: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> CompanySettingsOut:
        with app.state.session_factory() as session:
            setting = session.scalar(
                select(PolicySetting).order_by(PolicySetting.version.desc(), PolicySetting.id.desc())
            )
            if setting is None:
                raise HTTPException(status_code=503, detail="Policy settings are not initialized.")
            return CompanySettingsOut(
                threshold_paise=setting.threshold_paise,
                version=setting.version,
                updated_by=setting.updated_by,
                updated_at=setting.updated_at,
            )

    @app.put("/company/settings", response_model=CompanySettingsOut)
    def update_company_settings(
        body: CompanySettingsUpdate,
        manager: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> CompanySettingsOut:
        if body.threshold_paise > settings.max_threshold_paise:
            raise HTTPException(status_code=422, detail="The threshold exceeds the configured maximum.")
        with app.state.session_factory() as session:
            previous = session.scalar(
                select(PolicySetting).order_by(PolicySetting.version.desc(), PolicySetting.id.desc())
            )
            if previous is None:
                raise HTTPException(status_code=503, detail="Policy settings are not initialized.")
            setting = PolicySetting(
                threshold_paise=body.threshold_paise,
                version=previous.version + 1,
                updated_by=manager.id,
            )
            session.add(setting)
            session.add(
                PolicySettingAudit(
                    id=str(uuid4()),
                    setting_version=setting.version,
                    old_threshold_paise=previous.threshold_paise,
                    new_threshold_paise=setting.threshold_paise,
                    updated_by=manager.id,
                )
            )
            upsert_current_limits(session, get_policy().model_copy(
                update={"auto_approve_below_paise": setting.threshold_paise}
            ), HashEmbeddingProvider(settings.embedding_dim))
            session.commit()
            session.refresh(setting)
            return CompanySettingsOut(
                threshold_paise=setting.threshold_paise,
                version=setting.version,
                updated_by=setting.updated_by,
                updated_at=setting.updated_at,
            )

    def _case_state(case: Case, graph: Any) -> CaseState:
        snapshot = graph.get_state({"configurable": {"thread_id": case.thread_id}})
        return CaseState.model_validate(snapshot.values)

    def _case_out(case: Case, state: CaseState, audit: list[AuditEvent]) -> CaseOut:
        timeline = [{"node": event.node, "status": event.status, "at": event.at} for event in audit]
        return CaseOut(
            id=UUID(case.id),
            thread_id=case.thread_id,
            status=state.status,
            created_at=case.created_at,
            updated_at=case.updated_at,
            messages=state.messages,
            timeline=timeline,
            policy_result=state.policy_result,
            amount_paise=state.policy_result.amount_paise if state.policy_result else None,
            payout=state.payout,
            awaiting_approval=state.status is CaseStatus.PENDING_APPROVAL,
        )

    @app.post("/chat", dependencies=[Depends(limiter)])
    async def chat(
        body: ChatRequest,
        user: User = Depends(require_role(UserRole.CUSTOMER)),  # noqa: B008
    ) -> StreamingResponse:
        if user.customer_id is None:
            raise HTTPException(status_code=400, detail="This account is not linked to a customer.")
        with app.state.session_factory() as session:
            if body.case_id is None:
                case_id = uuid4()
                case = Case(
                    id=str(case_id),
                    thread_id=str(case_id),
                    customer_id=user.customer_id,
                    status=CaseStatus.OPEN.value,
                )
                session.add(case)
                session.commit()
            else:
                case = session.get(Case, str(body.case_id))
                if case is None or case.customer_id != user.customer_id:
                    raise HTTPException(status_code=404, detail="The case was not found.")
                case_id = UUID(case.id)
                session.commit()
            thread_id = case.thread_id

        async def events() -> AsyncIterator[str]:
            try:
                async def run_graph() -> CaseState:
                    async with make_async_postgres_checkpointer(
                        runtime_database_url(settings)
                    ) as checkpointer:
                        deps = WorkflowDependencies(
                            provider=get_provider(settings),
                            session_factory=app.state.session_factory,
                            tool_context=ToolContext(
                                customer_id=user.customer_id or "",
                                session_factory=app.state.session_factory,
                            ),
                        )
                        graph = build_graph(deps, checkpointer=checkpointer)
                        current = await graph.aget_state(
                            {"configurable": {"thread_id": thread_id}}
                        )
                        if current.values:
                            state = dict(current.values)
                            state["messages"] = [
                                *state.get("messages", []),
                                ChatMessage(role="customer", content=body.message),
                            ]
                            result = await graph.ainvoke(
                                state,
                                {"configurable": {"thread_id": thread_id}},
                            )
                        else:
                            result = await graph.ainvoke(
                                {
                                    "case_id": case_id,
                                    "thread_id": thread_id,
                                    "customer_id": user.customer_id,
                                    "messages": [
                                        ChatMessage(role="customer", content=body.message)
                                    ],
                                    "created_at": datetime.now(UTC),
                                },
                                {"configurable": {"thread_id": thread_id}},
                            )
                        if isinstance(result, dict):
                            result = {key: value for key, value in result.items() if key != "__interrupt__"}
                        return CaseState.model_validate(result)

                def run_graph_on_selector_loop() -> CaseState:
                    return asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(run_graph())

                final = await asyncio.to_thread(run_graph_on_selector_loop)
                yield f"event: case\ndata: {json.dumps({'case_id': str(case_id)})}\n\n"
                if final.policy_result:
                    policy_data = {
                        "type": "policy",
                        "result": final.policy_result.model_dump(mode="json"),
                    }
                    yield f"event: policy\ndata: {json.dumps(policy_data)}\n\n"
                if final.reply:
                    yield f"event: token\ndata: {json.dumps({'type': 'token', 'text': final.reply})}\n\n"
                if final.status is CaseStatus.PENDING_APPROVAL:
                    interrupt_data = {"case_id": str(case_id), "reason": "needs_approval"}
                    yield f"event: interrupt\ndata: {json.dumps(interrupt_data)}\n\n"
                done_data = {"case_id": str(case_id), "status": final.status.value}
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"
            except Exception:
                log.exception("chat workflow failed", extra={"case_id": str(case_id)})
                error_data = {"type": "error", "message": "The case could not be processed."}
                yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/cases/{case_id}", response_model=CustomerCaseOut | ReviewerCaseOut)
    def case_detail(
        case_id: UUID, user: User = Depends(current_user)  # noqa: B008
    ) -> CustomerCaseOut | ReviewerCaseOut:  # noqa: B008
        with app.state.session_factory() as session:
            case = session.get(Case, str(case_id))
            if case is None or (
                user.role == UserRole.CUSTOMER.value and case.customer_id != user.customer_id
            ):
                raise HTTPException(status_code=404, detail="The case was not found.")
            audits = session.scalars(
                select(AuditEvent).where(AuditEvent.case_id == str(case_id)).order_by(AuditEvent.at)
            ).all()
        with make_postgres_checkpointer(runtime_database_url(settings)) as checkpointer:
            graph = build_graph(
                WorkflowDependencies(
                    provider=get_provider(settings), session_factory=app.state.session_factory
                ),
                checkpointer=checkpointer,
            )
            state = _case_state(case, graph)
            if user.role == UserRole.REVIEWER.value:
                base = _case_out(case, state, audits)
                return ReviewerCaseOut(
                    **base.model_dump(),
                    customer_id=case.customer_id,
                    audit_events=[
                        {
                            "id": UUID(event.id),
                            "node": event.node,
                            "status": event.status,
                            "detail": json.loads(event.detail),
                            "at": event.at,
                        }
                        for event in audits
                    ],
                    trace_id=case.trace_id,
                    trace_url=None,
                    handoff_reason=state.handoff_reason,
                    review=(
                        {
                            "action": state.review.action,
                            "reviewer_id": state.review.reviewer_id,
                            "note": state.review.note,
                            "decided_at": state.review.decided_at,
                        }
                        if state.review
                        else None
                    ),
                )
            customer_status = {
                CaseStatus.PAID: "processed",
                CaseStatus.PENDING_APPROVAL: "under_review",
                CaseStatus.REJECTED: "not_eligible",
                CaseStatus.DENIED: "rejected",
                CaseStatus.ANSWERED: "answered",
                CaseStatus.HANDED_OFF: "handed_off",
            }.get(state.status, "under_review")
            order_id = case.order_id or (state.intake.order_id if state.intake else None)
            amount_paise = case.amount_paise or (
                state.policy_result.amount_paise if state.policy_result else None
            )
            reason = (
                case.reason_category
                or (
                    state.intake.reason_category.value
                    if state.intake and state.intake.reason_category
                    else None
                )
            )
            timeline = [
                TimelineStep(node=event.node, status=event.status, at=event.at) for event in audits
            ]

            effective_reply = state.reply
            if not effective_reply:
                if state.status == CaseStatus.PENDING_APPROVAL:
                    effective_reply = (
                        f"I have processed your request for order {order_id or 'your item'}. "
                        "Because of policy criteria, this request has been forwarded to a manager "
                        "for approval. You can track status and policy checks in the audit panel."
                    )
                elif state.status in (CaseStatus.PAID, CaseStatus.OPEN) and state.policy_result:
                    effective_reply = (
                        "Your refund request has been evaluated according to company policy."
                    )

            chat_messages = list(state.messages)
            if effective_reply and not any(
                m.role == "assistant" and m.content == effective_reply for m in chat_messages
            ):
                chat_messages.append(ChatMessage(role="assistant", content=effective_reply))

            return CustomerCaseOut(
                id=UUID(case.id),
                thread_id=case.thread_id,
                status=customer_status,
                reply=state.reply,
                messages=chat_messages,
                created_at=case.created_at,
                updated_at=case.updated_at,
                order_id=order_id,
                amount_paise=amount_paise,
                policy_result=state.policy_result,
                timeline=timeline,
                reason=reason,
            )

    @app.get("/customer/orders", response_model=list[CustomerOrderOut])
    def customer_orders(
        user: User = Depends(require_role(UserRole.CUSTOMER)),  # noqa: B008
    ) -> list[CustomerOrderOut]:
        if user.customer_id is None:
            raise HTTPException(status_code=400, detail="This account is not linked to a customer.")
        with app.state.session_factory() as session:
            orders = session.scalars(
                select(Order).where(Order.customer_id == user.customer_id).order_by(Order.ordered_at.desc())
            ).all()
            result = []
            now = datetime.now(UTC)
            for o in orders:
                payment_status = o.payment.status if o.payment else "pending"
                can_refund = (
                    payment_status == "captured"
                    and o.delivered_at is not None
                    and (now - o.delivered_at).days <= 30
                )
                result.append(
                    CustomerOrderOut(
                        order_id=o.id,
                        ordered_at=o.ordered_at,
                        delivered_at=o.delivered_at,
                        total_paise=o.total_paise,
                        payment_status=payment_status,
                        items=[
                            CustomerOrderItemOut(
                                item_id=i.id,
                                name=i.name,
                                quantity=i.quantity,
                                unit_price_paise=i.unit_price_paise,
                            )
                            for i in o.items
                        ],
                        can_refund=can_refund,
                    )
                )
            return result

    @app.get("/customer/cases", response_model=list[CustomerCaseSummaryOut])
    def customer_cases(
        user: User = Depends(require_role(UserRole.CUSTOMER)),  # noqa: B008
    ) -> list[CustomerCaseSummaryOut]:
        if user.customer_id is None:
            raise HTTPException(status_code=400, detail="This account is not linked to a customer.")
        with app.state.session_factory() as session:
            cases = session.scalars(
                select(Case).where(Case.customer_id == user.customer_id).order_by(Case.created_at.desc())
            ).all()
            status_map = {
                CaseStatus.PAID.value: "processed",
                CaseStatus.PENDING_APPROVAL.value: "under_review",
                CaseStatus.REJECTED.value: "not_eligible",
                CaseStatus.DENIED.value: "rejected",
                CaseStatus.ANSWERED.value: "answered",
                CaseStatus.HANDED_OFF.value: "handed_off",
            }
            return [
                CustomerCaseSummaryOut(
                    case_id=UUID(c.id),
                    order_id=c.order_id,
                    amount_paise=c.amount_paise,
                    status=status_map.get(c.status, "under_review"),
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
                for c in cases
            ]


    @app.post("/reviews/{case_id}/decision", response_model=ReviewDecisionResponse)
    async def review_decision(
        case_id: UUID,
        body: ReviewDecisionRequest,
        reviewer: User = Depends(require_role(UserRole.REVIEWER)),  # noqa: B008
    ) -> ReviewDecisionResponse:
        with app.state.session_factory() as session:
            case = session.execute(
                select(Case).where(Case.id == str(case_id)).with_for_update()
            ).scalar_one_or_none()
            if case is None:
                raise HTTPException(status_code=404, detail="The case was not found.")
            if case.status != CaseStatus.PENDING_APPROVAL.value:
                if case.review_action == body.decision.value:
                    return ReviewDecisionResponse(
                        case_id=case_id,
                        status=CaseStatus(case.status),
                        idempotent_replay=True,
                    )
                raise HTTPException(status_code=409, detail="The case is no longer awaiting a decision.")
            case.status = (
                CaseStatus.PAID.value if body.decision.value == "approve" else CaseStatus.DENIED.value
            )
            case.review_action = body.decision.value
            case.reviewer_id = reviewer.id
            case.review_note = body.note
            case.reviewed_at = datetime.now(UTC)
            session.commit()
        decision = ReviewDecision(
            action=body.decision,
            reviewer_id=reviewer.id,
            note=body.note,
            decided_at=datetime.now(UTC),
        )
        async def resume_graph() -> CaseState:
            async with make_async_postgres_checkpointer(
                runtime_database_url(settings)
            ) as checkpointer:
                graph = build_graph(
                    WorkflowDependencies(
                        provider=get_provider(settings), session_factory=app.state.session_factory
                    ),
                    checkpointer=checkpointer,
                )
                result = await graph.ainvoke(
                    Command(resume=decision.model_dump(mode="json")),
                    {"configurable": {"thread_id": case.thread_id}},
                )
                if isinstance(result, dict):
                    result = {
                        key: value for key, value in result.items() if key != "__interrupt__"
                    }
                return CaseState.model_validate(result)

        def resume_on_selector_loop() -> CaseState:
            return asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(resume_graph())

        final = await asyncio.to_thread(resume_on_selector_loop)
        return ReviewDecisionResponse(case_id=case_id, status=final.status, idempotent_replay=False)

    return app


app = create_app()
