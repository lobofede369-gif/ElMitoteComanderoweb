"""No borra movimientos: archiva productos y congela el corte con PDF/JSON.
Prueba autónoma sobre SQLite efímera; nunca utiliza el servidor real.
"""
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

db_file=Path(tempfile.gettempdir())/("mitote-archive-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(db_file)
os.environ["ADMIN_USERNAME"]="owner_archive_tests"
os.environ["ADMIN_PASSWORD"]="ArchiveTestsOnly2026!"
os.environ["SECRET_KEY"]="local-archive-tests-secret-not-production"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON",None)

import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        ids={}
        for role in ("admin","cashier","waiter","kitchen"):
            u=app.User(username="archive_"+role,display=role,role=role,
                       password_hash=app.ph.hash("ArchiveTestsOnly2026!"),active=True)
            db.add(u);db.flush();ids[role]=u.id
        ids["owner"]=db.scalar(app.select(app.User.id).where(app.User.role=="owner"))
        period=app.CashPeriod(opening_cents=0,opened_at=app.now()-timedelta(minutes=10))
        db.add(period);db.flush();period_id=period.id
        product=app.Product(name="Producto para archivar",station="kitchen",price_cents=25000,
                            category="Pruebas",stock=12,active=True,blocked=False)
        db.add(product);db.flush();product_id=product.id
        account=app.Account(name="Mesa pruebas corte 1",status="paid",owner_id=ids["waiter"])
        db.add(account);db.flush();account_id=account.id
        batch=app.Batch(account_id=account.id,sent_by=ids["waiter"],client_key=uuid4().hex)
        db.add(batch);db.flush()
        order=app.OrderItem(batch_id=batch.id,product_id=product.id,name=product.name,
                            station="kitchen",price_cents=25000,qty=1)
        db.add(order)
        db.add(app.StationTicket(batch_id=batch.id,station="kitchen",status="ready",ready_at=app.now()))
        payment=app.Payment(account_id=account_id,cashier_id=ids["cashier"],period_id=period_id,
                            client_key=uuid4().hex,method="cash",cents=25000,
                            received_cents=27000)
        db.add(payment);db.flush()
        db.add(app.PaymentTip(payment_id=payment.id,account_id=account_id,cashier_id=ids["cashier"],
                              period_id=period_id,method="cash",cents=2000))
        distribution=app.TipDistribution(period_id=period_id,created_by=ids["admin"],
                                         total_cents=2000,participant_count=1,
                                         base_share_cents=2000,remainder_cents=0)
        db.add(distribution);db.flush()
        db.add(app.TipDistributionShare(distribution_id=distribution.id,user_id=ids["waiter"],
                   employee_name="Mesero pruebas",employee_role="waiter",share_cents=2000))
        db.commit()

    def headers(role):
        token=app.jwt.encode({"sub":str(ids[role]),"ver":1,
            "exp":app.now()+timedelta(minutes=30)},app.SECRET_KEY,algorithm="HS256")
        return {"Authorization":"Bearer "+token}

    def get(role,path):
        return client.get(path,headers=headers(role))

    def post(role,path,data):
        return client.post(path,headers=headers(role),json=data)

    assert len(get("admin","/api/accounts").json())==1
    assert len(get("kitchen","/api/tickets").json())==1
    assert len(get("kitchen","/api/preparation/tickets").json()["tickets"])==1

    # El usuario de caja no puede eliminar productos.
    payload={"reason":"Retirar del menú de pruebas","expected_version":1}
    assert post("cashier",f"/api/products/{product_id}/archive",payload).status_code==403
    archived=post("admin",f"/api/products/{product_id}/archive",payload)
    assert archived.status_code==200,archived.text
    assert all(p["id"]!=product_id for p in get("admin","/api/products").json())
    assert all(p["id"]!=product_id for p in get("admin","/api/preparation/config").json()["products"])
    assert post("admin",f"/api/products/{product_id}/archive",payload).status_code==409
    records=get("admin","/api/products/archived").json()
    assert len(records)==1 and records[0]["id"]==product_id
    assert get("cashier","/api/products/archived").status_code==403
    with app.SessionLocal() as db:
        assert db.get(app.OrderItem,1).product_id==product_id

    restored=post("admin",f"/api/products/{product_id}/restore",
                  {"expected_version":records[0]["version"]})
    assert restored.status_code==200,restored.text
    assert any(p["id"]==product_id for p in get("admin","/api/products").json())

    close=post("admin","/api/cash/close",{"counted":"270.00","note":"Corte completo pruebas"})
    assert close.status_code==200,close.text
    assert close.json()["archived"] is True
    assert get("admin","/api/accounts").json()==[]
    assert get("kitchen","/api/tickets").json()==[]
    assert get("kitchen","/api/preparation/tickets").json()["tickets"]==[]
    assert get("cashier","/api/cash").json()["open"] is False

    detail=get("admin",f"/api/cash/periods/{period_id}").json()
    assert detail["archived"] is True
    assert detail["sales"]=="250.00" and detail["tip_cash"]=="20.00"
    assert detail["paid_accounts"]==1 and len(detail["payments"])==1
    pdf=get("admin",f"/api/cash/periods/{period_id}/report.pdf")
    assert pdf.status_code==200 and pdf.content.startswith(b"%PDF-")
    saved=pdf.content

    # Alterar registros históricos de prueba NO modifica ni el PDF ni el resumen congelados.
    with app.SessionLocal() as db:
        db.get(app.Account,account_id).name="Nombre cambiado después del corte"
        payment=db.scalar(app.select(app.Payment).where(app.Payment.period_id==period_id))
        payment.cents=123  # Esta modificación ocurre solo en la DB temporal de pruebas.
        db.commit()
    assert get("admin",f"/api/cash/periods/{period_id}").json()==detail
    assert get("admin",f"/api/cash/periods/{period_id}/report.pdf").content==saved
    history=get("admin","/api/cash/history").json()
    assert history[0]["id"]==period_id and history[0]["sales"]=="250.00"

    opened=post("admin","/api/cash/open",{"opening":"50.00"})
    assert opened.status_code==200,opened.text
    assert opened.json()["id"]!=period_id
    assert get("admin","/api/accounts").json()==[]
    assert get("kitchen","/api/tickets").json()==[]
    assert get("admin","/api/cash").json()["cash"]=="0.00"

print("OK: producto seguro, permisos, corte PDF/JSON inmutable y pantallas aisladas por turno")
db_file.unlink(missing_ok=True)
