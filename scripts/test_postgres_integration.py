"""Prueba PostgreSQL 18 aislada para las funciones nuevas.

DATABASE_URL apunta únicamente al contenedor temporal de GitHub Actions.
No utilizar esta prueba contra la base del restaurante.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        for number in (2, 3):
            row = app.User(username=f"postgres_waiter_{number}", display=f"waiter_{number}",
                           role="waiter", active=True,
                           password_hash=app.ph.hash("TestingOnlyPassword2026"))
            db.add(row)
            db.flush()
            users[f"waiter_{number}"] = row.id
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

    def bearer_user(user_id):
        token = app.jwt.encode({"sub": str(user_id), "ver": 1,
            "exp": app.now() + timedelta(minutes=30)}, app.SECRET_KEY, algorithm="HS256")
        return {"Authorization": "Bearer " + token}

    def bearer(role):
        return bearer_user(users[role])

    def post(role, path, payload):
        return client.post(path, json=payload, headers=bearer(role))

    def post_user(user_id, path, payload):
        return client.post(path, json=payload, headers=bearer_user(user_id))

    # Tres transacciones de trabajo deben poder mantener FOR SHARE a la vez.
    # El cierre conserva FOR UPDATE y debe esperar a que terminen las comandas.
    barrier = threading.Barrier(4)
    release_shared = threading.Event()
    def hold_work_lock():
        with app.SessionLocal() as db:
            app.active_period_for_work(db)
            barrier.wait(timeout=10)
            if not release_shared.wait(timeout=10):
                raise AssertionError("No se liberó el bloqueo compartido del turno")
            db.rollback()

    with ThreadPoolExecutor(max_workers=4) as pool:
        holders = [pool.submit(hold_work_lock) for _ in range(3)]
        try:
            barrier.wait(timeout=10)
            exclusive_acquired = threading.Event()
            def take_exclusive_lock():
                with app.SessionLocal() as db:
                    app.active_period(db)
                    exclusive_acquired.set()
                    db.rollback()
            exclusive = pool.submit(take_exclusive_lock)
            time.sleep(0.2)
            assert not exclusive_acquired.is_set(), "El cierre no esperó a las operaciones en curso"
        finally:
            release_shared.set()
        for future in holders:
            future.result(timeout=10)
        exclusive.result(timeout=10)
        assert exclusive_acquired.is_set(), "El bloqueo exclusivo no continuó tras liberar las comandas"
    print("OK: tres envíos pueden compartir el turno; el cierre espera de forma segura.")

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

    # Tres meseros distintos envían al mismo producto a la vez, con inventario global apagado.
    waiter_ids = [users["waiter"], users["waiter_2"], users["waiter_3"]]
    account_ids = [account_id]
    for number, waiter_id in enumerate(waiter_ids[1:], start=2):
        opened = post_user(waiter_id, "/api/accounts", {"name": f"Cuenta simultánea {number}", "table_id": None})
        assert opened.status_code == 200, opened.text
        account_ids.append(opened.json()["id"])
    with app.SessionLocal() as db:
        setting = db.get(app.Setting, 1)
        setting.inventory_on = False
        setting.inventory_reviewed = False
        db.commit()
    concurrent_payloads = [
        {"client_key": uuid4().hex,
         "items": [{"product_id": product_id, "qty": 1, "prep_minutes": 15}]}
        for _ in range(3)
    ]
    def send_concurrent(index):
        return post_user(waiter_ids[index],
            f"/api/accounts/{account_ids[index]}/orders", concurrent_payloads[index])
    with ThreadPoolExecutor(max_workers=3) as pool:
        concurrent_results = list(pool.map(send_concurrent, range(3)))
    assert all(response.status_code == 200 for response in concurrent_results), [
        (response.status_code, response.text) for response in concurrent_results]
    concurrent_batch_ids = [response.json()["batch_id"] for response in concurrent_results]
    assert len(set(concurrent_batch_ids)) == 3, concurrent_batch_ids
    print("OK: tres meseros enviaron comandas simultáneas sin bloqueo global ni duplicados.")

    # Dos toques simultáneos con la misma clave deben producir una sola comanda.
    duplicate_payload = {"client_key": uuid4().hex,
        "items": [{"product_id": product_id, "qty": 1, "prep_minutes": 15}]}
    def send_duplicate(_):
        return post_user(waiter_ids[0], f"/api/accounts/{account_ids[0]}/orders", duplicate_payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate_results = list(pool.map(send_duplicate, range(2)))
    assert all(response.status_code == 200 for response in duplicate_results), [
        (response.status_code, response.text) for response in duplicate_results]
    assert len({response.json()["batch_id"] for response in duplicate_results}) == 1
    assert sorted(response.json()["duplicate"] for response in duplicate_results) == [False, True]
    print("OK: doble toque simultáneo produce una sola comanda idempotente.")

    # Con inventario activo, los bloqueos por producto siguen evitando la sobreventa.
    with app.SessionLocal() as db:
        setting = db.get(app.Setting, 1)
        setting.inventory_on = True
        setting.inventory_reviewed = True
        stocked = app.Product(name="Producto con stock concurrente", category="Pruebas",
            station="kitchen", price_cents=5000, active=True, blocked=False,
            inventory_enabled=True, stock=5, version=1)
        db.add(stocked)
        db.commit()
        stocked_product_id = stocked.id
    stock_payloads = [
        {"client_key": uuid4().hex,
         "items": [{"product_id": stocked_product_id, "qty": 2, "prep_minutes": 15}]}
        for _ in range(3)
    ]
    def send_stocked(index):
        return post_user(waiter_ids[index],
            f"/api/accounts/{account_ids[index]}/orders", stock_payloads[index])
    with ThreadPoolExecutor(max_workers=3) as pool:
        stock_results = list(pool.map(send_stocked, range(3)))
    assert sorted(response.status_code for response in stock_results) == [200, 200, 409], [
        (response.status_code, response.text) for response in stock_results]
    with app.SessionLocal() as db:
        remaining = db.get(app.Product, stocked_product_id).stock
        assert remaining == 1, remaining
    print("OK: inventario concurrente no permite sobreventa; stock final correcto.")

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
