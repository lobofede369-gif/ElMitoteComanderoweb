"""Prueba de importación aprobada por propietario, con base temporal sin producción."""
import os
import secrets
import tempfile
from pathlib import Path
from uuid import uuid4

file=Path(tempfile.gettempdir())/("mitote-photo-approved-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(file)
os.environ["ADMIN_USERNAME"]="owner-import-ci"
os.environ["ADMIN_PASSWORD"]=secrets.token_urlsafe(24)
os.environ["SECRET_KEY"]=secrets.token_urlsafe(32)
os.environ.pop("MENU_PHOTO_IMPORT_APPROVED_VERSION",None)
os.environ.pop("MENU_PHOTO_IMPORT_EXPECT_NEW",None)
os.environ.pop("MENU_PHOTO_IMPORT_EXPECT_EXISTING",None)

import app
import menu_photo_import as photo

# Prepara un catálogo con 38 artículos existentes y seis precios antiguos.
app.bootstrap()
manifest,sha=photo.photo_manifest()
assert len(manifest["entries"])==61
with app.SessionLocal() as db:
    for i,item in enumerate(manifest["entries"][:38]):
        db.add(app.Product(name=item["aliases"][0] if item["aliases"] else item["name"],
                           category="Temporal de pruebas",station=item["station"],
                           price_cents=item["price"]*100+(100 if i<6 else 0),
                           stock=8,active=True,blocked=False))
    db.commit()

os.environ["MENU_PHOTO_IMPORT_APPROVED_VERSION"]=manifest["version"]
os.environ["MENU_PHOTO_IMPORT_EXPECT_NEW"]="23"
os.environ["MENU_PHOTO_IMPORT_EXPECT_EXISTING"]="38"
app.bootstrap()
with app.SessionLocal() as db:
    r=db.scalars(app.select(app.Product)).all()
    assert len(r)==61
    assert len({p.category for p in r})==12
    assert len({photo.norm(p.name) for p in r})==61
    approved=db.get(photo.MenuPhotoApplied,sha)
    assert approved and approved.created_count==23
    assert approved.updated_count==38
    assert [x.price_cents for x in r if x.name=="Costillas BBQ"]==[17000]
    assert len([p for p in r if p.stock==8])==38
    audit_count=db.scalar(app.select(app.func.count()).select_from(app.Audit).where(
        app.Audit.action=="menu.photo_approved_import"))
    assert audit_count==1

app.bootstrap()
with app.SessionLocal() as db:
    assert db.scalar(app.select(app.func.count()).select_from(app.Product))==61
    assert db.scalar(app.select(app.func.count()).select_from(app.Audit).where(
        app.Audit.action=="menu.photo_approved_import"))==1
    assert db.scalar(app.select(app.func.count()).select_from(photo.MenuPhotoApplied))==1

os.environ.pop("MENU_PHOTO_IMPORT_APPROVED_VERSION",None)
os.environ.pop("MENU_PHOTO_IMPORT_EXPECT_NEW",None)
os.environ.pop("MENU_PHOTO_IMPORT_EXPECT_EXISTING",None)
print("OK: 61 productos, 12 categorías, 23 nuevos, 38 actualizados, seis cambios de precio, reinicio idempotente")
file.unlink(missing_ok=True)
