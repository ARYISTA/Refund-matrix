"""Phase 1 tables: customers, orders, order_items, payments, refunds.

Money columns are BigInteger paise with CHECK constraints. Enum-like columns are plain
strings with a CHECK, which avoids Postgres enum types and their awkward migrations.
Cases, audit_events, KB chunks and users arrive in the phases that need them.
"""

from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from app.contracts.enums import CaseStatus, NodeName, NodeStatus, PaymentStatus, ReasonCategory, UserRole
from app.db.base import Base


class RefundStatus(StrEnum):
    APPROVED = "approved"  # approved, payout not finished yet; still counts as refunded
    PAID = "paid"


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{m.value}'" for m in enum)
    return f"{column} IN ({values})"


def _now() -> DateTime:
    return DateTime(timezone=True)


class Case(Base):
    """Durable case identity; the full workflow state lives in the checkpoint store."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(100), unique=True)
    customer_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=CaseStatus.OPEN.value)
    trace_id: Mapped[str | None] = mapped_column(String(100))
    threshold_paise: Mapped[int | None] = mapped_column(BigInteger)
    policy_version: Mapped[str | None] = mapped_column(String(50))
    order_id: Mapped[str | None] = mapped_column(String(40))
    amount_paise: Mapped[int | None] = mapped_column(BigInteger)
    reason_category: Mapped[str | None] = mapped_column(String(20))
    decision_type: Mapped[str | None] = mapped_column(String(20))
    review_action: Mapped[str | None] = mapped_column(String(20))
    reviewer_id: Mapped[str | None] = mapped_column(String(40))
    review_note: Mapped[str | None] = mapped_column(String(500))
    reviewed_at: Mapped[datetime | None] = mapped_column(_now())
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now(), onupdate=func.now())


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("status = 'processed'", name="status_valid"),
        Index("ix_payouts_case_id", "case_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    payout_id: Mapped[str] = mapped_column(String(40), unique=True)
    utr: Mapped[str] = mapped_column(String(12), unique=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="processed")
    account_masked: Mapped[str] = mapped_column(String(22))
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())


class PolicySetting(Base):
    __tablename__ = "policy_settings"
    __table_args__ = (CheckConstraint("threshold_paise >= 100", name="threshold_minimum"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    threshold_paise: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())


class PolicySettingAudit(Base):
    __tablename__ = "policy_setting_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    setting_version: Mapped[int] = mapped_column(Integer)
    old_threshold_paise: Mapped[int] = mapped_column(BigInteger)
    new_threshold_paise: Mapped[int] = mapped_column(BigInteger)
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.CUSTOMER.value)
    customer_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("customers.id"))
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())


class AuditEvent(Base):
    """PII-free node transition. Detail is JSON text to keep the audit contract explicit."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_case_id_at", "case_id", "at"),
        CheckConstraint(_in("node", NodeName), name="node_valid"),
        CheckConstraint(_in("status", NodeStatus), name="status_valid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    node: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(500), unique=True)
    source_type: Mapped[str] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_document_id", "document_id"),
        Index("ix_knowledge_chunks_content", "content"),
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    phone: Mapped[str] = mapped_column(String(20))
    # Simulation only: the mock payout needs a destination. Never logged or traced.
    bank_account_number: Mapped[str] = mapped_column(String(18))
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_paise > 0", name="total_positive"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    ordered_at: Mapped[datetime] = mapped_column(_now())
    delivered_at: Mapped[datetime | None] = mapped_column(_now())
    expected_delivery_at: Mapped[datetime | None] = mapped_column(_now())
    total_paise: Mapped[int] = mapped_column(BigInteger)
    fraud_flag: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    payment: Mapped["Payment | None"] = relationship(back_populates="order")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint("unit_price_paise >= 0", name="unit_price_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in("status", PaymentStatus), name="status_valid"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    captured_at: Mapped[datetime | None] = mapped_column(_now())

    order: Mapped[Order] = relationship(back_populates="payment")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint(_in("reason_category", ReasonCategory), name="reason_valid"),
        CheckConstraint(_in("status", RefundStatus), name="status_valid"),
        # Serves the "refunds by this customer in the last 30 days" lookup.
        Index("ix_refunds_customer_id_created_at", "customer_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    reason_category: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(_now(), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="refunds")
