"""Pruebas de cronómetros mixtos contra SQLite temporal, sin utilizar Render ni producción."""
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

dbfile = Path(tempfile.gettempdir()) / ("mitote-prep-" + uuid4().hex + ".sqlite")
os.environ["DATABASE_URL"] = "sqlite:///" + str(dbfile)
os.environ["ADMIN_USERNAME"] = "propietario_prep"
os.environ["ADMIN_PASSWORD"] = "ClaveDePrueba_2026!"
os.environ["SECRET_KEY"] = "testing-prep-only-do-not-deploy"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)

import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    login = client.post("/api/login", json={"username": "propietario_prep", "secret": "ClaveDePrueba_2026!"})
    assert login.status_code == 200, login.text
    owner_headers = {"Authorization": "Bearer " + login.json()["token"]}

    with app.SessionLocal() as db:
        staff = {}
        for role in ("kitchen", "bar", "waiter", "cashier", "admin"):
            u = app.User(username="prep_" + role, display=role, role=role,
                         password_hash=app.ph.hash("SoloPruebas-2026"), active=True)
            db.add(u)
            db.flush()
            staff[role] = u.id
        db.commit()

    def headers(role):
        token = app.jwt.encode(
            {"sub": str(staff[role]), "ver": 1,
             "exp": app.now() + timedelta(minutes=30)},
            app.SECRET_KEY, algorithm="HS256"
        )
        return {"Authorization": "Bearer " + token}

    def post(url, payload, role="owner"):
        return client.post(url, headers=owner_headers if role == "owner" else headers(role), json=payload)

    def put(url, payload, role="owner"):
        return client.put(url, headers=owner_headers if role == "owner" else headers(role), json=payload)

    conf = put("/api/prep/config", {"batch_limit_minutes": 25, "warning_percent": 80})
    assert conf.status_code == 200, conf.text
    assert put("/api/prep/config", {"batch_limit_minutes": 20, "warning_percent": 80}, "admin").status_code == 403
    assert put("/api/prep/config", {"batch_limit_minutes": 0, "warning_percent": 80}).status_code == 422
    assert post("/api/cash/open", {"opening": "0.00"}).status_code == 200
    products = []
    for station in ("kitchen", "bar"):
        product = post("/api/products", {"name": "Prueba " + station, "station": station,
                                          "price": "10", "stock": 100})
        assert product.status_code == 200, product.text
        products.append(product.json()["id"])
    assert put(f"/api/prep/products/{products[0]}", {"minutes": 9}).status_code == 200
    assert put(f"/api/prep/products/{products[1]}", {"minutes": 6}, "admin").status_code == 200
    assert put(f"/api/prep/products/{products[1]}", {"minutes": 10}, "waiter").status_code == 403

    acc = post("/api/accounts", {"name": "Mesa de prueba"})
    assert acc.status_code == 200, acc.text
    account_id = acc.json()["id"]

    payload = {"client_key": "preparacion-prueba-001",
               "items": [{"product_id": products[0], "qty": 1, "prep_minutes": 30},
                         {"product_id": products[1], "qty": 1, "prep_minutes": 30}]}
    sent = post(f"/api/accounts/{account_id}/orders", payload)
    assert sent.status_code == 200, sent.text
    batch_id = sent.json()["batch_id"]
    again = post(f"/api/accounts/{account_id}/orders", payload)
    assert again.status_code == 200 and again.json()["duplicate"], again.text

    ticket_response = client.get("/api/tickets", headers=owner_headers)
    assert ticket_response.status_code == 200, ticket_response.text
    tickets = [x for x in ticket_response.json() if x["batch_id"] == batch_id]
    assert len(tickets) == 2
    by_station = {x["station"]: x for x in tickets}
    for station, minutes in (("kitchen", 9), ("bar", 6)):
        t = by_station[station]
        assert t["prep_batch_minutes"] == 25
        assert not t["prep_batch_completed"]
        assert t["items"][0]["prep_minutes"] == minutes
        assert t["items"][0]["prep_elapsed_seconds"] >= 0
    kitchen = by_station["kitchen"]
    bar = by_station["bar"]
    ki = kitchen["items"][0]["id"]
    bi = bar["items"][0]["id"]
    assert post(f"/api/tickets/{kitchen['id']}/items/{ki}/ready", {}, "kitchen").status_code == 409
    assert post(f"/api/tickets/{kitchen['id']}/items/{ki}/ready", {}, "bar").status_code == 403
    for ticket, role in ((kitchen, "kitchen"), (bar, "bar")):
        assert post(f"/api/tickets/{ticket['id']}/status", {"status": "received"}, role).status_code == 200
        assert post(f"/api/tickets/{ticket['id']}/status", {"status": "preparing"}, role).status_code == 200
    marked = post(f"/api/tickets/{kitchen['id']}/items/{ki}/ready", {}, "kitchen")
    assert marked.status_code == 200 and marked.json()["station_ready"], marked.text
    duplicate = post(f"/api/tickets/{kitchen['id']}/items/{ki}/ready", {}, "kitchen")
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"], duplicate.text
    tickets = [x for x in client.get("/api/tickets", headers=owner_headers).json() if x["batch_id"] == batch_id]
    assert not any(x["prep_batch_completed"] for x in tickets)
    assert post(f"/api/tickets/{bar['id']}/status", {"status": "ready"}, "bar").status_code == 200
    tickets = [x for x in client.get("/api/tickets", headers=owner_headers).json() if x["batch_id"] == batch_id]
    assert all(x["prep_batch_completed"] for x in tickets)
    assert all(x["items"][0]["prep_ready"] for x in tickets)

    # Cambios futuros no alteran el límite congelado al enviar la comanda anterior.
    assert put("/api/prep/config", {"batch_limit_minutes": 40, "warning_percent": 80}).status_code == 200
    new = post(f"/api/accounts/{account_id}/orders",
               {"client_key": "preparacion-prueba-002",
                "items": [{"product_id": products[0], "qty": 1}]})
    assert new.status_code == 200, new.text
    ticket_response = client.get("/api/tickets", headers=owner_headers).json()
    assert next(x for x in ticket_response if x["batch_id"] == batch_id)["prep_batch_minutes"] == 25
    assert next(x for x in ticket_response if x["batch_id"] == new.json()["batch_id"])["prep_batch_minutes"] == 40

print("OK: cronómetros mixtos, roles, idempotencia, preparación por estación y snapshots")
dbfile.unlink(missing_ok=True)
