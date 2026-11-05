"""Regresión de operación integral: inventario, costos históricos y cronómetros programados.

Por defecto usa SQLite temporal. En CI PostgreSQL, MITOTE_TEST_DATABASE_URL
apunta exclusivamente al servicio efímero del workflow; nunca a producción.
"""
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

test_database=os.getenv("MITOTE_TEST_DATABASE_URL","").strip()
dbfile=None
if test_database:
    os.environ["DATABASE_URL"]=test_database
else:
    dbfile=Path(tempfile.gettempdir())/("mitote-operacion-"+uuid4().hex+".sqlite")
    os.environ["DATABASE_URL"]="sqlite:///"+str(dbfile)

os.environ["ADMIN_USERNAME"]="operacion_owner_test"
os.environ["ADMIN_PASSWORD"]="OnlyForTests_2026!"
os.environ["SECRET_KEY"]="operacion-test-secret-"+uuid4().hex
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON",None)
os.environ.pop("OWNER_RECOVERY_TOKEN",None)

import app
import mitote_extensions as ext
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        ids={}
        for role in ("admin","cashier","waiter","kitchen","bar"):
            user=app.User(username="operacion_"+role,display="Test "+role,role=role,
                          password_hash=app.ph.hash("DemoOnly_2026!"),active=True)
            db.add(user);db.flush();ids[role]=user.id
        ids["owner"]=db.scalar(app.select(app.User.id).where(app.User.role=="owner"))
        period=app.CashPeriod(opening_cents=0)
        product=app.Product(name="Producto de prueba",category="Pruebas",station="kitchen",
            price_cents=15000,active=True,blocked=False,inventory_enabled=True,stock=20,version=1)
        db.add_all([period,product]);db.flush()
        setting=db.get(app.Setting,1)
        setting.inventory_on=True
        setting.inventory_reviewed=True
        ids["product"]=product.id
        period_id=period.id
        db.add(app.InventoryMovement(movement_key="initial:operacion-test",product_id=product.id,
            period_id=period.id,actor_id=ids["owner"],kind="initial",delta=20,
            before_stock=0,after_stock=20,reason="Existencia inicial de prueba"))
        db.commit()

    def headers(role):
        token=app.jwt.encode({"sub":str(ids[role]),"ver":1,
            "exp":app.now()+timedelta(minutes=30)},app.SECRET_KEY,algorithm="HS256")
        return {"Authorization":"Bearer "+token}

    def get(role,path):
        return client.get("/api"+path,headers=headers(role))

    def post(role,path,data):
        return client.post("/api"+path,json=data,headers=headers(role))

    def put(role,path,data):
        return client.put("/api"+path,json=data,headers=headers(role))

    pid=ids["product"]

    # Seguridad de endpoints administrativos de inventario y costos.
    assert get("cashier","/inventory/costs").status_code==403
    assert get("cashier","/inventory/movements").status_code==403
    assert post("cashier","/inventory/waste",{"product_id":pid,"quantity":1,
        "reason":"Prueba no autorizada","expected_version":1}).status_code==403

    # Costo unitario real: el registro actual se congela al enviar la comanda.
    saved=put("owner",f"/products/{pid}/cost",{"unit_cost":"40.00"})
    assert saved.status_code==200,saved.text
    assert saved.json()["unit_cost"]=="40.00"

    opened=post("waiter","/accounts",{"name":"Mesa de prueba","table_id":None})
    assert opened.status_code==200,opened.text
    account_id=opened.json()["id"]

    sent=post("waiter",f"/accounts/{account_id}/orders",{
        "client_key":uuid4().hex,
        "items":[{"product_id":pid,"qty":2,"prep_minutes":15,"release_after_minutes":30}]
    })
    assert sent.status_code==200,sent.text
    batch_id=sent.json()["batch_id"]

    with app.SessionLocal() as db:
        product=db.get(app.Product,pid)
        assert product.stock==18,product.stock
        item=db.scalar(app.select(app.OrderItem).where(app.OrderItem.batch_id==batch_id))
        assert item is not None
        snapshot=db.get(app.OrderItemCost,item.id)
        assert snapshot and snapshot.unit_cost_cents==4000
        sale=db.scalar(app.select(app.InventoryMovement).where(
            app.InventoryMovement.movement_key==f"sale:{batch_id}:{pid}"))
        assert sale and sale.delta==-2 and sale.before_stock==20 and sale.after_stock==18
        meta=db.scalar(app.select(app.OrderItemMeta).where(app.OrderItemMeta.order_item_id==item.id))
        assert meta.release_after_minutes==30

    # El cronómetro de producto inicia cuando se libera, no al enviar el lote.
    prep=get("kitchen","/preparation/tickets")
    assert prep.status_code==200,prep.text
    ticket=next(x for x in prep.json()["tickets"] if x["batch_id"]==batch_id)
    item_view=ticket["items"][0]
    assert item_view["release_after_minutes"]==30
    assert item_view["timing"]=="scheduled",item_view
    assert item_view["elapsed_seconds"]==0
    assert item_view["started_at"]>ticket["created_at"]

    # La merma exige inventario activo, control de versión y existencias suficientes.
    with app.SessionLocal() as db:
        version=db.get(app.Product,pid).version
    waste=post("owner","/inventory/waste",{"product_id":pid,"quantity":1,
        "reason":"Producto dañado durante preparación","expected_version":version})
    assert waste.status_code==200,waste.text
    assert waste.json()["stock"]==17
    assert post("owner","/inventory/waste",{"product_id":pid,"quantity":99,
        "reason":"No debe permitir stock negativo","expected_version":version+1}).status_code==409

    # Reposición y trazabilidad de todas las variaciones de stock.
    with app.SessionLocal() as db:
        version=db.get(app.Product,pid).version
    restock=post("owner",f"/products/{pid}/restock",{
        "delta":5,"reason":"Reposición de prueba","expected_version":version})
    assert restock.status_code==200,restock.text
    assert restock.json()["stock"]==22

    # Un cambio de costo afecta pedidos nuevos, no snapshots históricos.
    updated=put("owner",f"/products/{pid}/cost",{"unit_cost":"50.00"})
    assert updated.status_code==200,updated.text
    sent2=post("waiter",f"/accounts/{account_id}/orders",{
        "client_key":uuid4().hex,
        "items":[{"product_id":pid,"qty":1,"prep_minutes":15,"release_after_minutes":0}]
    })
    assert sent2.status_code==200,sent2.text
    batch2=sent2.json()["batch_id"]
    with app.SessionLocal() as db:
        items=list(db.scalars(app.select(app.OrderItem).where(
            app.OrderItem.batch_id.in_([batch_id,batch2])).order_by(app.OrderItem.id)))
        costs={x.order_item_id:x.unit_cost_cents for x in db.scalars(
            app.select(app.OrderItemCost).where(app.OrderItemCost.order_item_id.in_([x.id for x in items])))}
        assert [costs[x.id] for x in items]==[4000,5000],costs
        kinds=[x.kind for x in db.scalars(app.select(app.InventoryMovement).where(
            app.InventoryMovement.product_id==pid))]
        assert "initial" in kinds and "sale" in kinds and "waste" in kinds and "restock" in kinds

    movement_response=get("owner","/inventory/movements?limit=100")
    assert movement_response.status_code==200,movement_response.text
    movement_data=movement_response.json()
    assert any(x["kind"]=="waste" and x["delta"]==-1 for x in movement_data)
    assert any(x["kind"]=="restock" and x["delta"]==5 for x in movement_data)
    assert any(x["kind"]=="sale" and x["delta"]==-2 for x in movement_data)

    # Los costos históricos sobreviven al retirar el costo vigente.
    cleared=put("owner",f"/products/{pid}/cost",{"unit_cost":None})
    assert cleared.status_code==200,cleared.text
    today=app.datetime.now(ZoneInfo("America/Mexico_City")).date().isoformat()
    analytics=get("owner",f"/owner/analytics?start={today}&end={today}&scope=current")
    assert analytics.status_code==200,analytics.text
    report=analytics.json()
    assert report["costs_available"] is True,report
    assert report["costed_quantity"]==3,report
    assert report["total_quantity"]==3,report
    assert report["estimated_cost"]=="130.00",report
    assert report["estimated_gross_profit"]=="320.00",report
    assert report["cost_coverage_percent"]==100.0,report
    assert report["products"][0]["estimated_cost"]=="130.00"

    # El costo eliminado no borra las instantáneas de pedidos ya enviados.
    with app.SessionLocal() as db:
        assert db.get(app.ProductCost,pid) is None
        assert db.scalar(app.select(app.OrderItemCost).where(
            app.OrderItemCost.order_item_id==items[0].id)).unit_cost_cents==4000

    # Los cambios de inventario no deben producir cantidades negativas.
    with app.SessionLocal() as db:
        assert db.get(app.Product,pid).stock==21

# Comprobaciones estructurales del cliente web/PWA.
from pathlib import Path
root=Path(__file__).resolve().parents[1]
js=(root/"static/app.js").read_text(encoding="utf-8")
features=(root/"static/features.js").read_text(encoding="utf-8")
sw=(root/"static/sw.js").read_text(encoding="utf-8")
html=(root/"static/index.html").read_text(encoding="utf-8")
assert "owner-live-panel" in js and "SEGUIMIENTO EN TIEMPO REAL" in js
assert "/inventory/waste" in features and "inventory-cost-" in features
assert "prep-clock-queued" in features and "Inicia en " in features
assert "mitote-comandas-v28" in sw
assert "/static/app.js?v=24" in sw and "/static/features.js?v=3" in sw
assert "/static/app.js?v=24" in html and "/static/features.js?v=3" in html
assert "Sin conexión" in js and "pendingOperationKey" in js
print("OK: kardex, merma, costos históricos, rentabilidad con cobertura, cronómetro programado, permisos y modo sin conexión")

if dbfile:
    dbfile.unlink(missing_ok=True)
