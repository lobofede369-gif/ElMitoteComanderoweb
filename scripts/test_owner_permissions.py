"""Regresión de permisos para operaciones avanzadas de cuentas.
Prueba aislada con SQLite temporal. NUNCA consulta la base productiva.
"""
from __future__ import annotations
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

dbfile = Path(tempfile.gettempdir()) / ("mitote-role-test-" + uuid4().hex + ".sqlite")
os.environ["DATABASE_URL"] = "sqlite:///" + str(dbfile)
os.environ["ADMIN_USERNAME"] = "propietario_pruebas"
os.environ["ADMIN_PASSWORD"] = "SoloPruebas-2026!"
os.environ["SECRET_KEY"] = "testing-owner-only-not-for-deploy"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)

import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        users = {}
        for role in ("admin", "cashier", "waiter"):
            u = app.User(username=role+"_test", display=role,
                         role=role, password_hash=app.ph.hash("Demo2026_!"), active=True)
            db.add(u); db.flush(); users[role] = u.id
        owner = db.scalar(app.select(app.User).where(app.User.role == "owner"))
        users["owner"] = owner.id
        db.add(app.CashPeriod(opening_cents=0))
        db.add(app.Account(name="Mesa de pruebas A", owner_id=users["waiter"], status="open"))
        db.add(app.Account(name="Mesa de pruebas B", owner_id=users["waiter"], status="open"))
        db.add(app.Account(name="Cuenta pagada", owner_id=users["waiter"], status="paid"))
        db.commit()
        accounts = db.scalars(app.select(app.Account).order_by(app.Account.id)).all()
        a, b, paid = (row.id for row in accounts)
    def token(role):
        return app.jwt.encode(
            {"sub":str(users[role]),"ver":1,"exp":app.now()+timedelta(minutes=20)},
            app.SECRET_KEY,algorithm="HS256"
        )
    def req(role, path, payload):
        return client.post(path, headers={"Authorization":"Bearer "+token(role)}, json=payload)
    restricted = [
        (f"/api/accounts/{a}/rename", {"name":"Cambio no autorizado","reason":"Pruebas de permisos"}),
        (f"/api/accounts/{a}/transfer-table", {"table_id":None,"reason":"Cambiar mesa"}),
        (f"/api/accounts/{a}/merge", {"target_account_id":b,"reason":"Unir cuentas"}),
        (f"/api/accounts/{a}/split", {"item_id":999,"qty":1,"target_account_id":b,"reason":"Mover producto"}),
        (f"/api/accounts/{a}/reassign", {"owner_id":users["waiter"],"reason":"Cambio de mesero"}),
        (f"/api/accounts/{a}/void", {"reason":"Anular cuenta"}),
        (f"/api/accounts/{paid}/reopen-closed", {"reason":"Reabrir la cuenta"}),
    ]
    for role in ("waiter", "admin", "cashier"):
        for path, payload in restricted:
            r = req(role, path, payload)
            assert r.status_code == 403, f"{role}: {path}: {r.status_code} {r.text}"
    # El propietario sí puede modificar la cuenta.
    result = req("owner",f"/api/accounts/{a}/rename",
                 {"name":"Cuenta A actualizada","reason":"Cambio autorizado"})
    assert result.status_code == 200, result.text
    result = req("owner",f"/api/accounts/{a}/transfer-table",
                 {"table_id":None,"reason":"Cambio autorizado"})
    assert result.status_code == 200, result.text
    # El mesero conserva tareas esenciales, incluida apertura de cuentas.
    result = req("waiter", "/api/accounts", {"name":"Para llevar de prueba","table_id":None})
    assert result.status_code in (200,201), result.text
    r = client.get("/api/accounts",
                   headers={"Authorization":"Bearer "+token("waiter")})
    assert r.status_code == 200 and len(r.json()) >= 1, r.text
    with app.SessionLocal() as db:
        assert db.get(app.Account,a).name == "Cuenta A actualizada"
        assert db.get(app.Account,paid).status == "paid"
print("OK: 21 bloqueos de permisos, 2 cambios de propietario y apertura del mesero")
dbfile.unlink(missing_ok=True)
