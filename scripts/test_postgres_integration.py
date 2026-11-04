"""Prueba PostgreSQL 18 aislada para las funciones nuevas.

DATABASE_URL apunta únicamente al contenedor temporal de GitHub Actions.
No utilizar esta prueba contra la base del restaurante.
"""
import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

url = os.getenv("MITOTE_TEST_DATABASE_URL", "")
if not url.startswith("postgresql://") or "localhost" not in url:
    raise RuntimeError("Solo se permite PostgreSQL local temporal para esta prueba")
os.environ["DATABASE_URL"] = url
os.environ["ADMIN_USERNAME"] = "mitote_postgres_test"
os.environ["ADMIN_PASSWORD"] = "TestingOnlyPostgres2026!"
os.environ["SECRET_KEY"] = "CI-only-not-for-production-long-random-key"
os.environ["RESERVATIONS_INGEST_TOKEN"] = "local-postgres-integration-only"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)

import app
import mitote_extensions as ext
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    # create_all no debe modificar tablas ya existentes ni fallar al repetirse
    app.Base.metadata.create_all(app.engine)
    with app.SessionLocal() as db:
        users = {}
        for role in ("waiter", "kitchen", "admin", "cashier"):
            row = app.User(username="postgres_"+role, display=role, role=role,
                           active=True, password_hash=app.ph.hash("TestingOnlyPassword2026"))
            db.add(row)
            db.flush()
            users[role] = row.id
        users["owner"] = db.scalar(app.select(app.User.id).where(app.User.role == "owner"))
        db.add(app.CashPeriod(opening_cents=0))
        db.add(app.Product(name="Hamburguesa pruebas", category="Comida",
                           station="kitchen", price_cents=12000,
                           active=True, blocked=False, stock=50))
        db.add(app.DiningTable(name="Mesa CI", name_key="mesa ci",
                               area="Pruebas", capacity=4, active=True))
        db.commit()
        product_id = db.scalar(app.select(app.Product.id))
        table_id = db.scalar(app.select(app.DiningTable.id))

    def bearer(role):
        token = app.jwt.encode({"sub": str(users[role]), "ver": 1,
            "exp": app.now() + timedelta(minutes=30)}, app.SECRET_KEY, algorithm="HS256")
        return {"Authorization": "Bearer " + token}

    def post(role, path, payload):
        return client.post(path, json=payload, headers=bearer(role))

    r = client.put("/api/preparation/config", json={"batch_minutes":26,"warning_percent":80},headers=bearer("owner"))
    assert r.status_code == 200,r.text
    r = client.put(f"/api/preparation/products/{product_id}",json={"minutes":12},headers=bearer("owner"))
    assert r.status_code == 200,r.text
    r = post("waiter", "/api/accounts", {"name":"Cliente de prueba", "table_id":None})
    assert r.status_code == 200,r.text
    account_id = r.json()["id"]
    payload={"client_key":uuid4().hex,"items":[{"product_id":product_id,"qty":1,"prep_minutes":15}]}
    r = post("waiter",f"/api/accounts/{account_id}/orders",payload)
    assert r.status_code == 200,r.text
    batch_id=r.json()["batch_id"]
    duplicate=post("waiter",f"/api/accounts/{account_id}/orders",payload)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    with app.SessionLocal() as db:
        assert db.get(ext.BatchPrep, batch_id).minutes == 26
        item = db.scalar(app.select(app.OrderItem).where(app.OrderItem.batch_id == batch_id))
        meta = db.scalar(app.select(app.OrderItemMeta).where(app.OrderItemMeta.order_item_id == item.id))
        assert meta.prep_minutes == 12
        iid = item.id
        ticket = db.scalar(app.select(app.StationTicket).where(app.StationTicket.batch_id==batch_id))
        ticket_id = ticket.id
    assert post("kitchen", f"/api/tickets/{ticket_id}/status",{"status":"received"}).status_code == 200
    assert post("kitchen", f"/api/tickets/{ticket_id}/status",{"status":"preparing"}).status_code == 200
    result=post("kitchen",f"/api/preparation/items/{iid}/ready",{})
    assert result.status_code == 200,result.text
    result=client.get("/api/preparation/tickets", headers=bearer("kitchen"))
    assert result.status_code == 200,result.text
    assert result.json()["tickets"][0]["batch_ready"] is True

    reservation={"external_id":"ci-"+uuid4().hex,"name":"Reserva prueba",
        "phone":"","party_size":2,"scheduled_at":(app.now()+timedelta(days=1)).isoformat(),"note":""}
    hdr={"Authorization":"Bearer local-postgres-integration-only"}
    r=client.post("/api/reservations/ingest",json=reservation,headers=hdr)
    assert r.status_code==200,r.text
    rid=r.json()["id"]
    assert client.post("/api/reservations/ingest",json=reservation,headers=hdr).json()["duplicate"]
    assert post("admin",f"/api/reservations/{rid}/status",{"status":"confirmed"}).status_code==200
    payload={"table_id":table_id,"waiter_id":users["waiter"]}
    r=post("admin",f"/api/reservations/{rid}/arrive",payload)
    assert r.status_code==200,r.text
    aid=r.json()["account_id"]
    assert post("admin",f"/api/reservations/{rid}/arrive",payload).json()["account_id"]==aid
    with app.SessionLocal() as db:
        assert db.get(app.TableOccupancy, table_id).account_id==aid
        assert db.get(ext.Reservation, rid).status=="arrived"
        payment=app.Payment(account_id=aid, cashier_id=users["cashier"],
            period_id=db.scalar(app.select(app.CashPeriod.id)), client_key=uuid4().hex,
            method="cash", cents=12300,received_cents=14300)
        db.add(payment);db.flush()
        db.add(app.PaymentTip(payment_id=payment.id,account_id=aid,cashier_id=users["cashier"],
            period_id=payment.period_id,method="cash",cents=2000))
        db.commit()
    today=datetime.now(ZoneInfo("America/Mexico_City")).date().isoformat()
    res=client.get(f"/api/owner/analytics?start={today}&end={today}",headers=bearer("owner"))
    assert res.status_code==200,res.text
    assert res.json()["sales"]=="123.00",res.json()
    assert res.json()["methods"]["cash"]=="123.00"
    assert res.json()["paid_accounts"]==1
    assert client.get(f"/api/owner/analytics?start={today}&end={today}",
                      headers=bearer("admin")).status_code==403

    # Verificación PostgreSQL de archivado de productos y PDF inmutable por corte.
    with app.SessionLocal() as db:
        period_id=db.scalar(app.select(app.CashPeriod.id))
        db.get(app.Account,account_id).status="paid"
        db.get(app.Account,aid).status="paid"
        pool=app.TipDistribution(period_id=period_id,created_by=users["admin"],
            total_cents=2000,participant_count=1,base_share_cents=2000,remainder_cents=0)
        db.add(pool);db.flush()
        db.add(app.TipDistributionShare(distribution_id=pool.id,user_id=users["waiter"],
            employee_name="Mesero prueba",employee_role="waiter",share_cents=2000))
        prod=db.get(app.Product,product_id)
        previous_version=prod.version
        db.commit()
    result=post("admin",f"/api/products/{product_id}/archive",
                {"expected_version":previous_version,"reason":"Prueba de archivo segura"})
    assert result.status_code==200,result.text
    assert all(p["id"]!=product_id for p in client.get("/api/products",headers=bearer("owner")).json())
    assert client.get("/api/products/archived",headers=bearer("cashier")).status_code==403
    closure=post("admin","/api/cash/close",{"counted":"143.00","note":"Prueba PostgreSQL de corte archivado"})
    assert closure.status_code==200,closure.text
    assert closure.json()["archived"]
    with app.SessionLocal() as db:
        frozen=db.get(app.CashCutArchive,period_id)
        assert frozen and frozen.pdf_blob.startswith(b"%PDF-")
        assert db.get(app.OrderItem,iid).product_id==product_id
    pdf=client.get(f"/api/cash/periods/{period_id}/report.pdf",headers=bearer("admin"))
    assert pdf.status_code==200 and pdf.content.startswith(b"%PDF-")
    assert client.get("/api/accounts",headers=bearer("admin")).json()==[]
    assert client.get("/api/preparation/tickets",headers=bearer("kitchen")).json()["tickets"]==[]
    history=client.get("/api/cash/history",headers=bearer("admin"))
    assert history.status_code==200 and history.json()[0]["sales"]=="123.00"

print("OK PostgreSQL 18: modelos, comandos, cronómetros, reservas, permisos y ventas y PDF por corte inmutable")
