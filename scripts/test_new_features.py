"""Pruebas de integración de las funciones 1, 2 y 4 con SQLite efímero.
Nunca conecta con Render ni con la BD de producción.
"""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

dbfile = Path(tempfile.gettempdir()) / ("mitote-features-" + uuid4().hex + ".sqlite")
os.environ["DATABASE_URL"] = "sqlite:///" + str(dbfile)
os.environ["ADMIN_USERNAME"] = "propietario_pruebas"
os.environ["ADMIN_PASSWORD"] = "SoloPruebas-2026!"
os.environ["SECRET_KEY"] = "testing-features-secret-only"
os.environ["RESERVATIONS_INGEST_TOKEN"] = "demo-internal-integration-secret"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)

import app
import mitote_extensions as ext
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        ids = {}
        for role in ("admin", "cashier", "waiter", "kitchen", "bar"):
            u = app.User(username="feature_"+role, display=role, role=role,
                         password_hash=app.ph.hash("Demo2026_!"), active=True)
            db.add(u); db.flush(); ids[role] = u.id
        ids["owner"] = db.scalar(app.select(app.User.id).where(app.User.role == "owner"))
        period = app.CashPeriod(opening_cents=0)
        product = app.Product(name="Prueba alitas",category="Comida",station="kitchen",
                              price_cents=15000,active=True,blocked=False,stock=100)
        table = app.DiningTable(name="Mesa 14",name_key="mesa 14",area="Salón",capacity=4,active=True)
        db.add_all([period, product, table]);db.commit()
        pid, tid, period_id = product.id, table.id, period.id

    def header(role):
        token = app.jwt.encode({"sub":str(ids[role]),"ver":1,
                                 "exp":app.now()+timedelta(minutes=20)},
                               app.SECRET_KEY,algorithm="HS256")
        return {"Authorization":"Bearer "+token}

    def post(role,path,data):
        return client.post(path,json=data,headers=header(role))

    assert client.get("/api/owner/analytics?start=2026-11-01&end=2026-11-04",
                      headers=header("admin")).status_code == 403
    assert client.get("/api/preparation/config",headers=header("waiter")).status_code == 403
    assert post("cashier",f"/api/preparation/products/{pid}",{"minutes":10}).status_code == 403

    result=client.put("/api/preparation/config",json={"batch_minutes":30,"warning_percent":80},
                      headers=header("owner"))
    assert result.status_code == 200,result.text
    result=client.put(f"/api/preparation/products/{pid}",json={"minutes":10},headers=header("owner"))
    assert result.status_code == 200,result.text

    result=post("waiter","/api/accounts",{"name":"Cuenta primera","table_id":None})
    assert result.status_code == 200,result.text
    account_id=result.json()["id"]
    result=post("waiter",f"/api/accounts/{account_id}/orders",
                {"client_key":uuid4().hex,"items":[{"product_id":pid,"qty":2,"prep_minutes":15}]})
    assert result.status_code == 200,result.text
    batch_id=result.json()["batch_id"]
    with app.SessionLocal() as db:
        assert db.get(ext.BatchPrep,batch_id).minutes==30
        item=db.scalar(app.select(app.OrderItem).where(app.OrderItem.batch_id==batch_id))
        iid=item.id
        meta=db.scalar(app.select(app.OrderItemMeta).where(app.OrderItemMeta.order_item_id==iid))
        assert meta.prep_minutes==10
        ticket=db.scalar(app.select(app.StationTicket).where(app.StationTicket.batch_id==batch_id))
        ticket_id=ticket.id

    response=client.get("/api/preparation/tickets",headers=header("kitchen"))
    assert response.status_code == 200,response.text
    station=response.json()["tickets"][0]
    assert station["batch_minutes"]==30
    assert station["items"][0]["prep_minutes"]==10
    assert station["items"][0]["ready"] is False
    assert post("bar",f"/api/preparation/items/{iid}/ready",{}).status_code==403
    assert post("kitchen",f"/api/preparation/items/{iid}/ready",{}).status_code==409
    assert post("kitchen",f"/api/tickets/{ticket_id}/status",{"status":"received"}).status_code==200
    assert post("kitchen",f"/api/tickets/{ticket_id}/status",{"status":"preparing"}).status_code==200
    assert post("kitchen",f"/api/preparation/items/{iid}/ready",{}).status_code==200
    assert post("kitchen",f"/api/preparation/items/{iid}/ready",{}).json()["duplicate"] is True
    with app.SessionLocal() as db:
        assert db.get(app.StationTicket,ticket_id).status=="ready"

    reservation={"name":"Cliente digital","phone":"5555550000","party_size":3,
                 "scheduled_at":(app.now()+timedelta(days=1)).isoformat(),
                 "external_id":"test-external-abc"}
    assert client.post("/api/reservations/ingest",json=reservation).status_code == 401
    params={"Authorization":"Bearer demo-internal-integration-secret"}
    first=client.post("/api/reservations/ingest",json=reservation,headers=params)
    assert first.status_code==200,first.text
    second=client.post("/api/reservations/ingest",json=reservation,headers=params)
    assert second.status_code==200 and second.json()["duplicate"],second.text
    reservation_id=first.json()["id"]
    assert post("waiter",f"/api/reservations/{reservation_id}/status",{"status":"confirmed"}).status_code==403
    assert post("admin",f"/api/reservations/{reservation_id}/status",{"status":"confirmed"}).status_code==200
    arrival={"table_id":tid,"waiter_id":ids["waiter"]}
    result=post("admin",f"/api/reservations/{reservation_id}/arrive",arrival)
    assert result.status_code==200,result.text
    linked_account=result.json()["account_id"]
    again=post("admin",f"/api/reservations/{reservation_id}/arrive",arrival)
    assert again.status_code==200 and again.json()["duplicate"] is True
    assert again.json()["account_id"]==linked_account
    with app.SessionLocal() as db:
        assert db.get(app.TableOccupancy,tid).account_id==linked_account
        assert db.get(ext.Reservation,reservation_id).status=="arrived"
        payment=app.Payment(account_id=linked_account,cashier_id=ids["cashier"],
                            period_id=period_id,client_key=uuid4().hex,method="cash",
                            cents=15000,received_cents=15000)
        db.add(payment);db.flush()
        db.add(app.PaymentTip(payment_id=payment.id,account_id=linked_account,
                              cashier_id=ids["cashier"],period_id=period_id,
                              method="cash",cents=2000))
        db.commit()

    today=datetime.now(ZoneInfo("America/Mexico_City")).date().isoformat()
    analytics=client.get(f"/api/owner/analytics?start={today}&end={today}",
                         headers=header("owner"))
    assert analytics.status_code==200,analytics.text
    assert analytics.json()["sales"]=="150.00",analytics.json()
    assert analytics.json()["average_ticket"]=="150.00"
    assert analytics.json()["methods"]["cash"]=="150.00"
    assert analytics.json()["table_rotation"][0]["table"]=="Mesa 14"
print("OK: cronómetros mixtos, permisos, idempotencia, reservaciones y métricas sin propinas")
dbfile.unlink(missing_ok=True)
