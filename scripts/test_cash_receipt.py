"""Prueba de descarga en formato ticket y preservación del PDF original."""
import os
import json
import secrets
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
os.environ["DATABASE_URL"]="sqlite:///"+str(Path(tempfile.gettempdir())/("mitote-ticket-"+uuid4().hex+".sqlite"))
os.environ["ADMIN_USERNAME"]="owner-ticket-ci"
os.environ["ADMIN_PASSWORD"]=secrets.token_urlsafe(24)
os.environ["SECRET_KEY"]=secrets.token_urlsafe(30)
os.environ.pop("OWNER_RECOVERY_TOKEN",None)
os.environ.pop("MENU_PHOTO_IMPORT_APPROVED_VERSION",None)

import app
import cash_cut_receipt
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        owner=db.scalar(app.select(app.User).where(app.User.role=="owner"))
        admin=app.User(username="admin_pdf_ci",display="Admin PDF",role="admin",
            password_hash=app.ph.hash("only-for-tests"),active=True)
        waiter=app.User(username="waiter_pdf_ci",display="Mesero PDF",role="waiter",
            password_hash=app.ph.hash("only-for-tests"),active=True)
        db.add_all([admin,waiter]);db.flush()
        ids={"owner":owner.id,"admin":admin.id,"waiter":waiter.id}
        cut1=app.CashPeriod(opening_cents=50000,opened_at=app.now()-timedelta(hours=6),
            closed_at=app.now()-timedelta(hours=4),counted_cents=77500,closed_by=owner.id,
            note="Corte antiguo")
        cut2=app.CashPeriod(opening_cents=0,opened_at=app.now()-timedelta(hours=3),
            closed_at=app.now()-timedelta(hours=1),counted_cents=15000,closed_by=owner.id,
            note="Corte archivado")
        db.add_all([cut1,cut2]);db.flush()
        old_pdf=b"%PDF-1.4 archived-original-do-not-change"
        detail={"id":cut2.id,"opened_at":cut2.opened_at.isoformat(),
            "closed_at":cut2.closed_at.isoformat(),"closed_by":"Propietario",
            "opening":"0.00","cash":"150.00","card":"0.00","transfer":"0.00",
            "sales":"150.00","tips":[{"amount":"20.00"}],
            "tip_cash":"20.00","tip_card":"0.00","tip_transfer":"0.00",
            "tip_payout_cash":"0.00","net_movements":"0.00",
            "expected":"170.00","counted":"150.00","difference":"-20.00",
            "paid_accounts":1,"cancelled_accounts":0,"cashier_totals":{"Caja":"150.00"},
            "movements":[],"note":"Cierre de prueba",
            "product_breakdown":[{"name":"SNACKS","units":1,"gross_amount":"150.00"}]}
        db.add(app.CashCutArchive(period_id=cut2.id,detail_json=json.dumps(detail),
            pdf_blob=old_pdf))
        db.commit()
        id1,id2=cut1.id,cut2.id

    def headers(role):
        u=ids[role]
        token=app.jwt.encode({"sub":str(u),"ver":1,"exp":app.now()+timedelta(minutes=15)},
            app.SECRET_KEY,algorithm="HS256")
        return {"Authorization":"Bearer "+token}

    assert client.get(f"/api/cash/periods/{id2}/ticket.pdf").status_code==401
    assert client.get(f"/api/cash/periods/{id2}/ticket.pdf",headers=headers("waiter")).status_code==403
    old=client.get(f"/api/cash/periods/{id1}/ticket.pdf",headers=headers("admin"))
    new=client.get(f"/api/cash/periods/{id2}/ticket.pdf",headers=headers("owner"))
    assert old.status_code==200,old.text
    assert new.status_code==200,new.text
    for response in [old,new]:
        assert response.headers["content-type"]=="application/pdf"
        assert response.content.startswith(b"%PDF-")
        assert len(response.content)>1300
        assert b"/MediaBox" in response.content
    assert "CorteTicket_2.pdf" in new.headers["content-disposition"]
    immutable=client.get(f"/api/cash/periods/{id2}/report.pdf",headers=headers("owner"))
    assert immutable.status_code==200
    assert immutable.content==old_pdf
    historical=client.get("/api/cash/history",headers=headers("owner")).json()
    item=next(x for x in historical if x["id"]==id2)
    assert item["tips"]=="20.00",item

print("OK: PDF ticket para cortes antiguos y archivados; 401/403; original conservado; propinas correctas")
