"""Seed idempotente y aislado para el entorno público de demostración."""
import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "demo-seed-test-secret-key-32-bytes-minimum"
os.environ["ADMIN_PASSWORD"] = "demo-seed-test-admin-password"
os.environ["ADMIN_USERNAME"] = "demo_propietario"
os.environ["DEMO_MODE"] = "1"

from sqlalchemy import select, func
from app import (
    bootstrap, SessionLocal, User, UserPin, Product, DiningTable,
    Account, Batch, StationTicket, Payment, CashPeriod, Audit,
    _seed_demo_data, _pin_lookup,
)

bootstrap()

with SessionLocal() as db:
    expected_pins = {
        "demo_propietario": "111111",
        "demo_mesero": "222222",
        "demo_cocina": "333333",
        "demo_barra": "444444",
        "demo_caja": "555555",
    }
    for username, pin in expected_pins.items():
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None, username
        assert user.active is True
        assert db.get(UserPin, user.id) is not None
        assert db.get(UserPin, user.id).lookup == _pin_lookup(pin)

    assert (db.scalar(select(func.count(Product.id))) or 0) == 8
    assert (db.scalar(select(func.count(DiningTable.id))) or 0) == 8
    assert (db.scalar(select(func.count(Account.id))) or 0) == 3
    assert (db.scalar(select(func.count(Batch.id))) or 0) == 3
    assert (db.scalar(select(func.count(StationTicket.id))) or 0) >= 5
    assert (db.scalar(select(func.count(Payment.id))) or 0) == 1
    assert (db.scalar(select(func.count(CashPeriod.id))) or 0) == 1
    assert (db.scalar(select(func.count(Audit.id)).where(Audit.action == "demo.seed.v1")) or 0) == 1

    before = {
        "users": db.scalar(select(func.count(User.id))),
        "products": db.scalar(select(func.count(Product.id))),
        "tables": db.scalar(select(func.count(DiningTable.id))),
        "accounts": db.scalar(select(func.count(Account.id))),
        "batches": db.scalar(select(func.count(Batch.id))),
        "payments": db.scalar(select(func.count(Payment.id))),
    }
    _seed_demo_data(db)
    db.commit()
    after = {
        "users": db.scalar(select(func.count(User.id))),
        "products": db.scalar(select(func.count(Product.id))),
        "tables": db.scalar(select(func.count(DiningTable.id))),
        "accounts": db.scalar(select(func.count(Account.id))),
        "batches": db.scalar(select(func.count(Batch.id))),
        "payments": db.scalar(select(func.count(Payment.id))),
    }
    assert before == after, (before, after)

print("OK: demo aislada, perfiles PIN, menú, mesas, cuentas y caja; seed idempotente.")
