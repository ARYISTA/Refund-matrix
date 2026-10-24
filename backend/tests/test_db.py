"""Phase 1: migrations, models and seed data, against a real Postgres + pgvector."""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Customer, Order, OrderItem, Payment, Refund
from app.db.seed import build_seed, reset_tables, seed
from tests.conftest import alembic_config

TABLES = {"customers", "orders", "order_items", "payments", "refunds"}


# ---- migrations --------------------------------------------------------------------------------


def test_upgrade_creates_tables_and_vector_extension(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    assert TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar() == 1
    engine.dispose()


def test_models_match_migration(migrated_db_url: str):
    """No drift: autogenerate against the migrated database finds nothing to change."""
    engine = create_engine(migrated_db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_then_upgrade_again(empty_db_url: str):
    cfg = alembic_config(empty_db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(empty_db_url)
    assert not TABLES & set(inspect(engine).get_table_names())
    command.upgrade(cfg, "head")
    assert TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_pgvector_hnsw_cosine_index_works(migrated_db_url: str):
    """Confirms the image supports what Phase 5 needs: vectors with an HNSW cosine index."""
    engine = create_engine(migrated_db_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE _vec_probe (id int, v vector(3))"))
        conn.execute(text("CREATE INDEX ON _vec_probe USING hnsw (v vector_cosine_ops)"))
        conn.execute(text("INSERT INTO _vec_probe VALUES (1, '[1,0,0]'), (2, '[0,1,0]')"))
        nearest = conn.execute(
            text("SELECT id FROM _vec_probe ORDER BY v <=> '[0.9,0.1,0]' LIMIT 1")
        ).scalar()
    engine.dispose()
    assert nearest == 1


# ---- constraints ---------------------------------------------------------------------------------


def _customer(session: Session) -> None:
    session.add(Customer(id="C1", name="A", email="a@example.com", phone="9", bank_account_number="1" * 12))
    session.flush()


def _order(session: Session, order_id: str = "O1", total: int = 1000) -> None:
    now = datetime.now(UTC)
    session.add(Order(id=order_id, customer_id="C1", ordered_at=now, total_paise=total))
    session.flush()


def test_constraints_reject_bad_money_and_values(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    now = datetime.now(UTC)
    bad_rows = [
        lambda s: s.add(Order(id="O2", customer_id="C1", ordered_at=now, total_paise=0)),
        lambda s: s.add(OrderItem(id="I1", order_id="O1", name="x", quantity=0, unit_price_paise=100)),
        lambda s: s.add(OrderItem(id="I2", order_id="O1", name="x", quantity=1, unit_price_paise=-1)),
        lambda s: s.add(Payment(id="P1", order_id="O1", status="paid_in_full", amount_paise=1000)),
        lambda s: s.add(Payment(id="P2", order_id="O1", status="captured", amount_paise=0)),
        lambda s: s.add(
            Refund(
                id="R1",
                order_id="O1",
                customer_id="C1",
                amount_paise=0,
                reason_category="damaged",
                status="paid",
            )
        ),
        lambda s: s.add(
            Refund(
                id="R2",
                order_id="O1",
                customer_id="C1",
                amount_paise=5,
                reason_category="boredom",
                status="paid",
            )
        ),
        lambda s: s.add(
            Refund(
                id="R3",
                order_id="O1",
                customer_id="C1",
                amount_paise=5,
                reason_category="damaged",
                status="pending",
            )
        ),
        lambda s: s.add(Order(id="O3", customer_id="NOPE", ordered_at=now, total_paise=100)),  # FK
    ]
    for add_bad in bad_rows:
        with Session(engine) as session:
            _customer(session)
            _order(session)
            add_bad(session)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
    engine.dispose()


def test_one_payment_per_order_and_unique_email(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        _customer(session)
        _order(session)
        session.add(Payment(id="P1", order_id="O1", status="captured", amount_paise=1000))
        session.flush()
        session.add(Payment(id="P2", order_id="O1", status="captured", amount_paise=1000))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    with Session(engine) as session:
        _customer(session)
        session.add(
            Customer(id="C2", name="B", email="a@example.com", phone="9", bank_account_number="2" * 12)
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    engine.dispose()


def test_money_columns_are_64_bit(migrated_db_url: str):
    """Paise values above 2^31 must fit (Rs 2.1 crore and up)."""
    engine = create_engine(migrated_db_url)
    big = 5_000_000_000
    with Session(engine) as session:
        _customer(session)
        _order(session, total=big)
        assert session.scalar(select(Order.total_paise)) == big
        session.rollback()
    engine.dispose()


# ---- seed data --------------------------------------------------------------------------------------


def test_seed_inserts_expected_rows(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        added = seed(session)
        session.commit()
        counts = {
            m.__tablename__: session.scalar(select(func.count()).select_from(m))
            for m in (Customer, Order, OrderItem, Payment, Refund)
        }
    engine.dispose()
    assert counts == {"customers": 3, "orders": 10, "order_items": 11, "payments": 10, "refunds": 4}
    assert added == sum(counts.values())


def test_seed_is_idempotent(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        first = seed(session)
        session.commit()
        second = seed(session)
        session.commit()
    engine.dispose()
    assert first > 0 and second == 0


def test_seed_reset_reloads_with_fresh_dates(migrated_db_url: str):
    engine = create_engine(migrated_db_url)
    old = datetime.now(UTC) - timedelta(days=100)
    with Session(engine) as session:
        seed(session, now=old)
        session.commit()
        reset_tables(session)
        seed(session)
        session.commit()
        newest = session.scalar(select(func.max(Order.ordered_at)))
    engine.dispose()
    assert newest is not None and newest > old + timedelta(days=90)


def test_seed_data_is_internally_consistent():
    data = build_seed(datetime(2026, 10, 23, tzinfo=UTC))
    items_by_order: dict[str, int] = {}
    for item in data.items:
        items_by_order[item.order_id] = (
            items_by_order.get(item.order_id, 0) + item.quantity * item.unit_price_paise
        )
    refunded: dict[str, int] = {}
    for refund in data.refunds:
        refunded[refund.order_id] = refunded.get(refund.order_id, 0) + refund.amount_paise
    for order in data.orders:
        assert order.total_paise == items_by_order[order.id]  # item lines add up to the order total
        assert refunded.get(order.id, 0) <= order.total_paise  # never over-refunded
    for payment in data.payments:
        order = next(o for o in data.orders if o.id == payment.order_id)
        assert payment.amount_paise == order.total_paise
    for row in (*data.orders, *data.refunds):
        assert isinstance(row.total_paise if isinstance(row, Order) else row.amount_paise, int)
    # Demo coverage the later phases rely on:
    assert any(o.fraud_flag for o in data.orders)
    assert any(p.status == "authorized" for p in data.payments)
    assert any(o.total_paise >= 500_000 for o in data.orders)
    assert any(o.total_paise < 500_000 for o in data.orders)


def test_seed_demo_scenarios_have_expected_shape(migrated_db_url: str):
    """The facts each demo order was designed around (the engine decides in Phase 2 tests)."""
    now = datetime.now(UTC)
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        seed(session, now=now)
        session.commit()
        o1001 = session.get(Order, "ORD-1001")
        o1003 = session.get(Order, "ORD-1003")
        priya_recent = session.scalar(
            select(func.count())
            .select_from(Refund)
            .where(Refund.customer_id == "CUS-1003", Refund.created_at >= now - timedelta(days=30))
        )
        assert o1001 is not None and o1003 is not None
        assert o1001.total_paise < 500_000
        assert (now - o1003.delivered_at).days > 30  # type: ignore[operator]
        assert priya_recent == 3
    engine.dispose()
