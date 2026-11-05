"""Seed idempotente y aislado para el entorno público de demostración."""
import os
import tempfile

_db_file = tempfile.NamedTemporaryFile(prefix="mitote-demo-test-", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _db_file.name
os.environ["SECRET_KEY"] = "demo-seed-test-secret-key-32-bytes-minimum"
os.environ["ADMIN_PASSWORD"] = "demo-seed-test-admin-password"
os.environ["ADMIN_USERNAME"] = "demo_propietario"
os.environ["DEMO_MODE"] = "1"

from sqlalchemy import select, func
from app import (
    bootstrap, SessionLocal, User, UserPin, Product, DiningTable,
    Account, Batch, StationTicket, Payment, CashPeriod, Audit,
    _seed_demo_data, _pin_lookup, app as api_app, engine,
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

from fastapi.testclient import TestClient

with TestClient(api_app) as client:
    info = client.get("/api/demo/info")
    assert info.status_code == 200, info.text
    assert info.json()["demo"] is True
    assert info.json()["shared"] is True

    for pin, expected_role in (
        ("111111", "owner"),
        ("222222", "waiter"),
        ("333333", "kitchen"),
        ("444444", "bar"),
        ("555555", "cashier"),
    ):
        login = client.post("/api/login/pin", json={"pin": pin})
        assert login.status_code == 200, (expected_role, login.text)
        token = login.json()["token"]
        profile = client.get("/api/me", headers={"Authorization": "Bearer " + token})
        assert profile.status_code == 200, profile.text
        assert profile.json()["role"] == expected_role, profile.json()

engine.dispose()
try:
    os.unlink(_db_file.name)
except OSError:
    pass

print("OK: demo aislada, perfiles PIN, menú, mesas, cuentas, caja, API pública y login de los cinco perfiles.")
