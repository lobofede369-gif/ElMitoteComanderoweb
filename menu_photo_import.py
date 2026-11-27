"""Importación supervisada del menú fotografiado, sin duplicar productos existentes.

No ejecuta escrituras al iniciar. El propietario/administrador primero revisa
los cambios de precio y confirma expresamente el botón de importación.
"""
import hashlib
import json
import unicodedata
import re
from pathlib import Path
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import (
    app, Base, Product, ArchivedProduct, User, db_session, auth, allow, audit, notify, now,
    Column, String, Integer, DateTime,
)

SOURCE = Path(__file__).resolve().parent / "data" / "menu_fotografias_2026_11.json"

class MenuPhotoApplied(Base):
    """Marca duradera para que un despliegue o reinicio no repita la importación."""
    __tablename__ = "menu_photo_applied"
    manifest_sha256 = Column(String(64), primary_key=True)
    manifest_version = Column(String(100), nullable=False)
    created_count = Column(Integer, nullable=False)
    updated_count = Column(Integer, nullable=False)
    applied_at = Column(DateTime(timezone=True), default=now, nullable=False)



def norm(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def photo_manifest():
    raw = SOURCE.read_bytes()
    payload = json.loads(raw)
    entries = payload["entries"]
    if len(entries) != 70:
        raise RuntimeError("El catálogo de fotografías no contiene 70 productos")
    return payload, hashlib.sha256(raw).hexdigest()


def find_products(db, name, aliases):
    options = {norm(value) for value in [name] + aliases}
    return [p for p in db.scalars(select(Product)) if norm(p.name) in options]


def preview(db):
    catalog, checksum = photo_manifest()
    rows = []
    mapped_ids = set()
    archived_ids = set(db.scalars(select(ArchivedProduct.product_id)))
    for item in catalog["entries"]:
        found = find_products(db, item["name"], item.get("aliases", []))
        if len(found) > 1:
            status = "ambiguous"
            prior = None
        elif found and found[0].id in archived_ids:
            status = "archived_conflict"
            prior = found[0]
        elif found and found[0].id in mapped_ids:
            status = "ambiguous"
            prior = found[0]
        else:
            prior = found[0] if found else None
            if prior:
                mapped_ids.add(prior.id)
            status = "create" if not prior else (
                "update" if (
                    prior.name != item["name"] or prior.category != item["category"] or
                    prior.station != item["station"] or prior.price_cents != int(item["price"])*100
                ) else "unchanged"
            )
        rows.append({
            "name": item["name"], "category": item["category"], "station": item["station"],
            "price": item["price"], "status": status,
            "existing_id": prior.id if prior else None,
            "existing_name": prior.name if prior else None,
            "previous_price": prior.price_cents / 100 if prior else None,
            "price_changes": bool(prior and prior.price_cents != int(item["price"])*100),
            "current_version": prior.version if prior else None
        })
    payload = {
        "manifest_sha256": checksum,
        "source": catalog["source"],
        "total": len(rows), "rows": rows,
        "counts": {s: sum(r["status"] == s for r in rows)
                   for s in ("create", "update", "unchanged", "ambiguous", "archived_conflict")},
    }
    payload["preview_token"] = hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return payload


def apply_approved_photo_menu_at_startup(db):
    """Importación transaccional y única, solo por aprobación explícita de Render.

    Se ejecuta antes de confirmar el arranque, en la misma transacción de bootstrap.
    No se dispara por reiniciar el servidor sin la variable de aprobación.
    """
    import os
    version = os.getenv("MENU_PHOTO_IMPORT_APPROVED_VERSION", "").strip()
    if not version:
        return None
    manifest, sha256 = photo_manifest()
    if version != manifest["version"]:
        raise RuntimeError("La versión de menú aprobada no coincide con el catálogo desplegado")
    from app import Setting
    db.flush()
    db.execute(select(Setting).where(Setting.id == 1).with_for_update()).scalar_one()
    if db.get(MenuPhotoApplied, sha256):
        return {"already_applied": True, "sha256": sha256}

    data = preview(db)
    if data["counts"]["ambiguous"] or data["counts"]["archived_conflict"]:
        raise RuntimeError("Importación cancelada: hay coincidencias ambiguas o productos archivados")
    expected_create = int(os.getenv("MENU_PHOTO_IMPORT_EXPECT_NEW", "-1"))
    expected_existing = int(os.getenv("MENU_PHOTO_IMPORT_EXPECT_EXISTING", "-1"))
    existing = data["counts"]["update"] + data["counts"]["unchanged"]
    if data["counts"]["create"] != expected_create or existing != expected_existing:
        raise RuntimeError(
            f"Importación cancelada: se esperaban {expected_create} nuevos y "
            f"{expected_existing} existentes; hay {data['counts']['create']} nuevos y {existing} existentes"
        )

    created = updated = unchanged = 0
    for item, row in zip(manifest["entries"], data["rows"]):
        if row["status"] == "unchanged":
            unchanged += 1
            continue
        if row["status"] == "create":
            db.add(Product(name=item["name"], category=item["category"], station=item["station"],
                           price_cents=int(item["price"])*100, active=True, blocked=False,
                           inventory_enabled=False, stock=0))
            created += 1
        elif row["status"] == "update":
            product = db.get(Product, row["existing_id"])
            if product is None or product.version != row["current_version"]:
                raise RuntimeError("El producto cambió durante la importación; se canceló sin aplicar")
            product.name = item["name"]
            product.category = item["category"]
            product.station = item["station"]
            product.price_cents = int(item["price"])*100
            product.version = (product.version or 1) + 1
            updated += 1
        else:
            raise RuntimeError("Importación cancelada por conflicto inesperado")
    db.add(MenuPhotoApplied(manifest_sha256=sha256, manifest_version=manifest["version"],
                            created_count=created, updated_count=updated))
    audit(db, None, "menu.photo_approved_import", version=manifest["version"],
          created=created, updated=updated, unchanged=unchanged, manifest_sha256=sha256)
    return {"already_applied": False, "created": created, "updated": updated, "unchanged": unchanged}


@app.get("/api/menu/photo-preview")
def menu_preview(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    return preview(db)


class ApplyPhotoMenu(BaseModel):
    preview_token: str
    confirm: bool


@app.post("/api/menu/photo-apply")
async def menu_apply(data: ApplyPhotoMenu, db: Session = Depends(db_session),
                     user: User = Depends(auth)):
    allow(user, "owner", "admin")
    if not data.confirm:
        raise HTTPException(400, "Revisa y confirma los precios antes de importar")
    # Serializa importaciones concurrentes en PostgreSQL.
    from app import Setting
    db.execute(select(Setting).where(Setting.id == 1).with_for_update()).scalar_one()
    current = preview(db)
    if current["preview_token"] != data.preview_token:
        raise HTTPException(409, "El menú cambió desde la vista previa. Vuelve a revisarlo.")
    if current["counts"]["ambiguous"] or current["counts"]["archived_conflict"]:
        raise HTTPException(409, "Resuelve productos duplicados o eliminados antes de importar")
    catalog, _ = photo_manifest()
    created = changed = unchanged = 0
    for item, row in zip(catalog["entries"], current["rows"]):
        if row["status"] == "unchanged":
            unchanged += 1
            continue
        if row["status"] == "create":
            p = Product(name=item["name"], category=item["category"],
                        station=item["station"],price_cents=int(item["price"])*100,
                        active=True,blocked=False, inventory_enabled=False,stock=0)
            db.add(p);created += 1
        else:
            p = db.get(Product, row["existing_id"])
            if not p or p.version != row["current_version"]:
                raise HTTPException(409, "El catálogo cambió. Revisa nuevamente los precios.")
            p.name = item["name"]
            p.category = item["category"]
            p.station = item["station"]
            p.price_cents = int(item["price"])*100
            p.version = (p.version or 1)+1
            changed += 1
    audit(db,user,"menu.photo_import",version=catalog["version"],
          created=created,updated=changed,unchanged=unchanged,
          manifest_sha256=current["manifest_sha256"])
    db.commit()
    await notify()
    return {"ok":True, "created":created, "updated":changed, "unchanged":unchanged}
