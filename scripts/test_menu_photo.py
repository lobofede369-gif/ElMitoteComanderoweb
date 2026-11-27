"""Regresiones del importador en base SQLite temporal, sin datos de producción."""
import os, tempfile, secrets
from pathlib import Path
from uuid import uuid4
from datetime import timedelta
p=Path(tempfile.gettempdir()) / ("mitote-photo-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(p)
os.environ["ADMIN_USERNAME"]="photo-ci"
os.environ["ADMIN_PASSWORD"]=secrets.token_urlsafe(22)
os.environ["SECRET_KEY"]=secrets.token_urlsafe(42)
os.environ.pop("OWNER_RECOVERY_TOKEN",None)
import app
from fastapi.testclient import TestClient
with TestClient(app.app) as client:
    with app.SessionLocal() as db:
        owner=db.scalar(app.select(app.User).where(app.User.role=="owner"))
        token=app.jwt.encode({"sub":str(owner.id),"ver":owner.session_version,
            "exp":app.now()+timedelta(minutes=15)},app.SECRET_KEY,algorithm="HS256")
        db.add(app.Product(name="Birriaquiles",category="ESPECIALIDADES",
            station="kitchen",price_cents=4000,active=True,blocked=False,stock=0))
        db.commit()
    headers={"Authorization":"Bearer "+token}
    preview=client.get("/api/menu/photo-preview",headers=headers)
    assert preview.status_code==200,preview.text
    data=preview.json()
    assert data["total"]==70 and data["counts"]["update"]==1
    assert data["counts"]["create"]==69
    result=client.post("/api/menu/photo-apply",headers=headers,
      json={"preview_token":data["preview_token"],"confirm":True})
    assert result.status_code==200,result.text
    all_products=client.get("/api/products",headers=headers).json()
    assert len(all_products)==70
    assert len({x["category"] for x in all_products})==13
    assert next(x for x in all_products if x["name"]=="Birriaquiles")["price"]=="110.00"
    refreshed=client.get("/api/menu/photo-preview",headers=headers).json()
    assert refreshed["counts"]["unchanged"]==70
    invalid=client.post("/api/categories/remove",headers=headers,
      json={"name":"SODAS ITALIANAS","destination":"Refrescos preparados",
            "expected_visible_count":2})
    assert invalid.status_code==409
    changed=client.post("/api/categories/remove",headers=headers,
      json={"name":"SODAS ITALIANAS","destination":"Refrescos preparados",
            "expected_visible_count":3})
    assert changed.status_code==200,changed.text
    after=client.get("/api/products",headers=headers).json()
    assert not any(x["category"]=="SODAS ITALIANAS" for x in after)
    assert len([x for x in after if x["category"]=="Refrescos preparados"])==3
print("OK importador: 70 productos, 13 categorias, sin duplicados, cambios de precio y borrar categoria")
p.unlink(missing_ok=True)
