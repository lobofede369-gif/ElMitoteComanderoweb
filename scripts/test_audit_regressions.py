"""Regresiones de seguridad, inventario e idempotencia usando SQLite aislado."""
import os
import tempfile
from pathlib import Path
from uuid import uuid4

dbfile = Path(tempfile.gettempdir()) / ("mitote-audit-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(dbfile)
os.environ["SECRET_KEY"]="auditoria-pruebas-2026-secreto-no-productivo"
os.environ["ADMIN_USERNAME"]="dueno_pruebas"
os.environ["ADMIN_PASSWORD"]="ClaveAnterior_Dueno_2026!"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON",None)

import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    login=client.post("/api/login",json={"username":"dueno_pruebas","secret":"ClaveAnterior_Dueno_2026!"})
    assert login.status_code==200,login.text
    token=login.json()["token"]
    headers={"Authorization":"Bearer "+token}

    # El login antiguo solo debe servir para recuperar el acceso del dueño.
    with app.SessionLocal() as db:
        db.add(app.User(username="empleado_antiguo",display="Empleado antiguo",
                        role="cashier",password_hash=app.ph.hash("ContrasenaAntigua-2026"),active=True))
        db.commit()
    old=client.post("/api/login",json={"username":"empleado_antiguo","secret":"ContrasenaAntigua-2026"})
    assert old.status_code==403,old.text

    # La ruta legacy de usuarios también debe crear un PIN de seis dígitos.
    staff=client.post("/api/users",headers=headers,json={
        "username":"mesero_pruebas","display":"Mesero de pruebas","role":"waiter","secret":"615204"})
    assert staff.status_code==200,staff.text
    uid=staff.json()["id"]
    pin=client.post("/api/login/pin",json={"pin":"615204"})
    assert pin.status_code==200 and pin.json()["user"]["role"]=="waiter",pin.text
    malformed=client.post("/api/users",headers=headers,json={
        "username":"otro_mesero","display":"Otro mesero","role":"waiter","secret":"1234"})
    assert malformed.status_code in (400,422),malformed.text
    duplicated=client.post("/api/users",headers=headers,json={
        "username":"mesero_duplicado","display":"Mesero duplicado","role":"waiter","secret":"615204"})
    assert duplicated.status_code==409,duplicated.text
    change=client.patch(f"/api/users/{uid}",headers=headers,json={"secret":"735019"})
    assert change.status_code==200,change.text
    assert client.post("/api/login/pin",json={"pin":"615204"}).status_code==401
    assert client.post("/api/login/pin",json={"pin":"735019"}).status_code==200

    opening=client.post("/api/cash/open",headers=headers,json={"opening":"100.00"})
    assert opening.status_code==200,opening.text
    product_payload={"name":"Producto auditado","category":"Pruebas","station":"kitchen",
                     "price":"10.00","stock":3,"inventory_enabled":True}
    created=client.post("/api/products",headers=headers,json=product_payload)
    assert created.status_code==200,created.text
    pid=created.json()["id"]
    edited=client.put(f"/api/products/{pid}",headers=headers,
                      json={**product_payload,"expected_version":1,"price":"10.00"})
    assert edited.status_code==200,edited.text
    stale=client.put(f"/api/products/{pid}",headers=headers,
                     json={**product_payload,"expected_version":1,"stock":99})
    assert stale.status_code==409,stale.text
    missing=client.put(f"/api/products/{pid}",headers=headers,json=product_payload)
    assert missing.status_code==409,missing.text
    assert next(x for x in client.get("/api/products",headers=headers).json() if x["id"]==pid)["stock"]==3

    activate=client.put("/api/settings",headers=headers,json={
        "inventory_on":True,"confirmed_recount":True})
    assert activate.status_code==200,activate.text

    def bill(name,order_key):
        opened=client.post("/api/accounts",headers=headers,json={"name":name})
        assert opened.status_code==200,opened.text
        aid=opened.json()["id"]
        payload={"client_key":order_key,"items":[{"product_id":pid,"qty":1}]}
        order=client.post(f"/api/accounts/{aid}/orders",headers=headers,json=payload)
        assert order.status_code==200,order.text
        again=client.post(f"/api/accounts/{aid}/orders",headers=headers,json=payload)
        assert again.status_code==200 and again.json()["duplicate"],again.text
        requested=client.post(f"/api/accounts/{aid}/request-bill",headers=headers,json={})
        assert requested.status_code==200,requested.text
        return aid

    a=bill("Cuenta A","order-a-12345678")
    b=bill("Cuenta B","order-b-12345678")
    first=client.post(f"/api/accounts/{a}/payments",headers=headers,json={
        "client_key":"12345678","lines":[{"method":"cash","amount":"10.00"}]})
    assert first.status_code==200 and not first.json()["duplicate"],first.text
    # Los guiones bajos son comodines en SQL LIKE, pero NO en claves de pago.
    second=client.post(f"/api/accounts/{b}/payments",headers=headers,json={
        "client_key":"________","lines":[{"method":"cash","amount":"10.00"}]})
    assert second.status_code==200 and not second.json()["duplicate"],second.text
    repeated=client.post(f"/api/accounts/{b}/payments",headers=headers,json={
        "client_key":"________","lines":[{"method":"cash","amount":"10.00"}]})
    assert repeated.status_code==200 and repeated.json()["duplicate"],repeated.text
    with app.SessionLocal() as db:
        assert db.scalar(app.select(app.func.count(app.Payment.id)))==2
        p=db.get(app.Product,pid)
        assert p.stock==1, f"Stock incorrecto tras dos comandas y un reintento: {p.stock}"

print("OK: PIN legacy, usuarios, stock concurrente, clave exacta de pagos y no duplicación")
dbfile.unlink(missing_ok=True)
