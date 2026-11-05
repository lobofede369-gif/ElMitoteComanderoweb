"""Regresión: avisos por cocina/barra dirigidos al mesero correcto.

Prueba offline con SQLite efímero. No toca Render ni envía push reales.
"""
import asyncio
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

dbfile=Path(tempfile.gettempdir())/("mitote-ready-alerts-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(dbfile)
os.environ["ADMIN_USERNAME"]="owner_ready_test"
os.environ["ADMIN_PASSWORD"]="OnlyForTests_2026"
os.environ["SECRET_KEY"]="test-ready-alerts-"+uuid4().hex
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON",None)
os.environ.pop("MENU_PHOTO_IMPORT_APPROVED_VERSION",None)
import app
from fastapi.testclient import TestClient

web_calls=[]
fcm_calls=[]
async def mock_web(payload,role=None,user_id=None):
    web_calls.append((payload,role,user_id))
async def mock_fcm(payload,role=None,user_id=None):
    fcm_calls.append((payload,role,user_id))
app.push_to=mock_web
app.fcm_to=mock_fcm

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        users={}
        for role in ("waiter","kitchen","bar","admin"):
            user=app.User(username="ready_"+role,display="Test "+role,
                          role=role,password_hash=app.ph.hash("DemoOnly_2026"),
                          active=True)
            db.add(user);db.flush();users[role]=user.id
        other=app.User(username="other_waiter",display="Otro mesero",role="waiter",
                       password_hash=app.ph.hash("DemoOnly_2026"),active=True)
        db.add(other);db.flush();users["other_waiter"]=other.id
        db.add(app.CashPeriod(opening_cents=0))
        for name,station in (("Quesabirria","kitchen"),("Agua fresca","bar"),("Consomé","kitchen")):
            db.add(app.Product(name=name,category="Pruebas",station=station,price_cents=10000,
                   active=True,blocked=False,stock=20))
        db.commit()
        products={p.name:p.id for p in db.scalars(app.select(app.Product))}
        users["owner"]=db.scalar(app.select(app.User.id).where(app.User.role=="owner"))

    def headers(role):
        token=app.jwt.encode({"sub":str(users[role]),"ver":1,
             "exp":app.now()+timedelta(minutes=30)},app.SECRET_KEY,algorithm="HS256")
        return {"Authorization":"Bearer "+token}
    def post(role,path,data):
        return client.post(path,json=data,headers=headers(role))
    def get(role,path):
        return client.get(path,headers=headers(role))

    assert get("owner","/api/waiter/ready-alerts").status_code==403
    assert get("other_waiter","/api/waiter/ready-alerts").json()==[]
    assert get("waiter","/api/waiter/ready-alerts").json()==[]
    r=post("waiter","/api/accounts",{"name":"Mesa 14","table_id":None})
    assert r.status_code==200,r.text
    aid=r.json()["id"]
    order={"client_key":uuid4().hex,"items":[{"product_id":products["Quesabirria"],"qty":2},
                                            {"product_id":products["Agua fresca"],"qty":1}]}
    r=post("waiter",f"/api/accounts/{aid}/orders",order)
    assert r.status_code==200,r.text
    bid=r.json()["batch_id"]
    with app.SessionLocal() as db:
        tickets={t.station:t.id for t in db.scalars(app.select(app.StationTicket).where(app.StationTicket.batch_id==bid))}
    for station in ["kitchen","bar"]:
        for status in ("received","preparing"):
            assert post(station,f"/api/tickets/{tickets[station]}/status",{"status":status}).status_code==200
    assert post("kitchen",f"/api/tickets/{tickets['kitchen']}/status",{"status":"ready"}).status_code==200
    event=get("waiter","/api/waiter/ready-alerts")
    assert event.status_code==200,event.text
    assert len(event.json())==1 and event.json()[0]["station"]=="kitchen"
    assert event.json()[0]["complete"] is False
    assert get("other_waiter","/api/waiter/ready-alerts").json()==[]
    assert len([x for x in web_calls if x[1]=="waiter"])==1
    assert web_calls[-1][0]["kind"]=="ready_station"
    assert "Mesa 14" in web_calls[-1][0]["body"]
    assert web_calls[-1][2]==users["waiter"]

    assert post("bar",f"/api/tickets/{tickets['bar']}/status",{"status":"ready"}).status_code==200
    events=get("waiter","/api/waiter/ready-alerts").json()
    assert len(events)==2,events
    assert sum(1 for x in events if x["complete"])==1,events
    assert events[0]["station"]=="bar" and events[0]["complete"]
    ready_web=[x for x in web_calls if x[1]=="waiter"]
    ready_fcm=[x for x in fcm_calls if x[1]=="waiter"]
    assert len(ready_web)==len(ready_fcm)==2
    assert ready_web[-1][0]["kind"]=="ready_complete"
    assert ready_fcm[-1][0]["kind"]=="ready_complete"
    assert "Pedido completo" in ready_web[-1][0]["title"]
    assert "listo para entregar" in ready_web[-1][0]["body"].lower()
    assert ready_web[-1][0]["tag"]!=ready_web[-2][0]["tag"]
    assert post("bar",f"/api/tickets/{tickets['bar']}/status",{"status":"ready"}).status_code==409
    assert len([x for x in web_calls if x[1]=="waiter"])==2
    assert len([x for x in fcm_calls if x[1]=="waiter"])==2

    # El marcado automático al terminar todos los productos usa un solo aviso.
    order2={"client_key":uuid4().hex,"items":[
        {"product_id":products["Quesabirria"],"qty":1},
        {"product_id":products["Consomé"],"qty":1}]}
    r=post("waiter",f"/api/accounts/{aid}/orders",order2)
    assert r.status_code==200,r.text
    second_bid=r.json()["batch_id"]
    with app.SessionLocal() as db:
        ticket=db.scalar(app.select(app.StationTicket).where(app.StationTicket.batch_id==second_bid))
        items=list(db.scalars(app.select(app.OrderItem).where(app.OrderItem.batch_id==second_bid)))
        id1,id2=items[0].id,items[1].id
    for status in ("received","preparing"):
        assert post("kitchen",f"/api/tickets/{ticket.id}/status",{"status":status}).status_code==200
    assert post("kitchen",f"/api/preparation/items/{id1}/ready",{}).status_code==200
    assert len([x for x in web_calls if x[1]=="waiter"])==2
    assert post("kitchen",f"/api/preparation/items/{id2}/ready",{}).status_code==200
    assert len([x for x in web_calls if x[1]=="waiter"])==3
    assert web_calls[-1][0]["kind"]=="ready_complete"
    assert post("kitchen",f"/api/preparation/items/{id2}/ready",{}).json()["duplicate"] is True
    assert len([x for x in web_calls if x[1]=="waiter"])==3
    assert web_calls[-1][0]["event_id"].startswith("ready-")
    assert fcm_calls[-1][0]["event_id"]==web_calls[-1][0]["event_id"]

    # El registro persistente sobrevive el reinicio sin duplicar avisos.
    with app.SessionLocal() as db:
        rows=list(db.scalars(app.select(app.ReadyNotificationEvent).order_by(app.ReadyNotificationEvent.id)))
        assert len(rows)==3,rows
        assert len({e.event_key for e in rows})==3
        assert all(e.attempted_at is not None for e in rows)
        first_event_id=rows[0].id
        remaining_ids=[e.id for e in rows[1:]]
    app.bootstrap()
    with app.SessionLocal() as db:
        assert len(list(db.scalars(app.select(app.ReadyNotificationEvent))))==3

    assert post("other_waiter",f"/api/waiter/ready-alerts/{first_event_id}/ack",{}).status_code==403
    ack=post("waiter",f"/api/waiter/ready-alerts/{first_event_id}/ack",{})
    assert ack.status_code==200,ack.text
    again=post("waiter",f"/api/waiter/ready-alerts/{first_event_id}/ack",{})
    assert again.status_code==200 and again.json()["duplicate"]
    assert len(get("waiter","/api/waiter/ready-alerts").json())==2

    # Cambio de mesero: las pendientes pasan al nuevo responsable.
    transfer=post("owner",f"/api/accounts/{aid}/reassign",{
        "owner_id":users["other_waiter"],"reason":"Cambio de responsable de mesa"})
    assert transfer.status_code==200,transfer.text
    assert get("waiter","/api/waiter/ready-alerts").json()==[]
    pending=get("other_waiter","/api/waiter/ready-alerts")
    assert pending.status_code==200 and len(pending.json())==2,pending.text
    assert post("waiter",f"/api/waiter/ready-alerts/{remaining_ids[0]}/ack",{}).status_code==403
    assert post("other_waiter",f"/api/waiter/ready-alerts/{remaining_ids[0]}/ack",{}).status_code==200
    assert len(get("other_waiter","/api/waiter/ready-alerts").json())==1
    # La reasignación vuelve a notificar al nuevo responsable, no solo cambia la lista.
    assert len([x for x in web_calls if x[1]=="waiter"])==5
    assert len([x for x in fcm_calls if x[1]=="waiter"])==5
    assert all(x[2]==users["other_waiter"] for x in [z for z in web_calls if z[1]=="waiter"][-2:])
    assert all(x[2]==users["other_waiter"] for x in [z for z in fcm_calls if z[1]=="waiter"][-2:])

    # Si el proveedor falla, el outbox conserva el aviso y lo reintenta tras el backoff.
    retry_account=post("waiter","/api/accounts",{"name":"Mesa reintento","table_id":None})
    assert retry_account.status_code==200,retry_account.text
    retry_aid=retry_account.json()["id"]
    with app.SessionLocal() as db:
        retry_batch=app.Batch(account_id=retry_aid,sent_by=users["waiter"],client_key=uuid4().hex)
        db.add(retry_batch);db.flush()
        retry_ticket=app.StationTicket(batch_id=retry_batch.id,station="kitchen",
                                       status="preparing",created_at=app.now())
        db.add(retry_ticket);db.commit()
        retry_ticket_id=retry_ticket.id

    async def fail_transport(payload,role=None,user_id=None):
        return {"enabled":True,"registered":1,"accepted":0,"failed":1}
    app.push_to=fail_transport
    app.fcm_to=fail_transport
    failed=post("kitchen",f"/api/tickets/{retry_ticket_id}/status",{"status":"ready"})
    assert failed.status_code==200,failed.text
    with app.SessionLocal() as db:
        retry_event=db.scalar(app.select(app.ReadyNotificationEvent).where(
            app.ReadyNotificationEvent.ticket_id==retry_ticket_id))
        assert retry_event and retry_event.attempted_at is not None
        retry_event_id=retry_event.id
        deliveries=list(db.scalars(app.select(app.ReadyNotificationDelivery).where(
            app.ReadyNotificationDelivery.event_id==retry_event_id)))
        assert {x.channel for x in deliveries}=={"webpush","fcm"}
        assert all(x.attempts==1 and x.accepted_at is None and x.last_error for x in deliveries)
        for delivery in deliveries:
            delivery.next_attempt_at=app.now()-timedelta(seconds=1)
        db.commit()

    app.push_to=mock_web
    app.fcm_to=mock_fcm
    asyncio.run(app.dispatch_ready_notification(retry_event_id))
    with app.SessionLocal() as db:
        retried=list(db.scalars(app.select(app.ReadyNotificationDelivery).where(
            app.ReadyNotificationDelivery.event_id==retry_event_id)))
        assert all(x.attempts==2 and x.accepted_at is not None for x in retried)
    web_before=len(web_calls);fcm_before=len(fcm_calls)
    asyncio.run(app.dispatch_ready_notification(retry_event_id))
    assert len(web_calls)==web_before and len(fcm_calls)==fcm_before
    retry_ack=post("waiter",f"/api/waiter/ready-alerts/{retry_event_id}/ack",{})
    assert retry_ack.status_code==200,retry_ack.text
    with app.SessionLocal() as db:
        retried=list(db.scalars(app.select(app.ReadyNotificationDelivery).where(
            app.ReadyNotificationDelivery.event_id==retry_event_id)))
        assert all(x.resolved_at is not None for x in retried)

    with app.SessionLocal() as db:
        period=db.scalar(app.select(app.CashPeriod).where(app.CashPeriod.closed_at.is_(None)))
        period.closed_at=app.now()
        db.commit()
    assert get("waiter","/api/waiter/ready-alerts").json()==[]
    assert get("other_waiter","/api/waiter/ready-alerts").json()==[]

js=Path("static/app.js").read_text()
assert "syncWaiterReadyAlerts" in js
assert "Pedidos pendientes de entregar" in js
assert "beep('ready')" in js
assert "window.MitoteAndroid" in js
assert "case 'ack-ready':" in js
print("OK: mesero correcto, cocina, barra, pedido completo, idempotencia, pantalla, sonido y turno activo")
dbfile.unlink(missing_ok=True)
