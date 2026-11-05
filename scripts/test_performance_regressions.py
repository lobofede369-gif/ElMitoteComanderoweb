"""Performance regression checks for high-frequency POS read paths."""
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

dbfile = Path(tempfile.gettempdir()) / ("mitote-perf-" + uuid4().hex + ".sqlite")
os.environ["DATABASE_URL"] = "sqlite:///" + str(dbfile)
os.environ["ADMIN_USERNAME"] = "mitote_perf_owner"
os.environ["ADMIN_PASSWORD"] = "OnlyForTests_Perf_2026!"
os.environ["SECRET_KEY"] = "performance-test-secret-" + uuid4().hex
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON", None)
os.environ.pop("OWNER_RECOVERY_TOKEN", None)

import app
from sqlalchemy import event
from fastapi.testclient import TestClient

try:
    with TestClient(app.app) as client:
        with app.SessionLocal() as db:
            owner_id = db.scalar(app.select(app.User.id).where(app.User.role == "owner"))
            product = app.Product(
                name="Producto perf", category="Pruebas", station="kitchen",
                price_cents=12500, active=True, blocked=False,
                inventory_enabled=False, stock=25, version=1,
            )
            db.add(product)
            db.flush()
            variant = app.ProductVariant(
                product_id=product.id, name="Grande", price_cents=15000,
                active=True, sort_order=0,
            )
            group = app.ProductModifierGroup(
                product_id=product.id, name="Extras", min_select=0,
                max_select=2, active=True, sort_order=0,
            )
            db.add_all([variant, group])
            db.flush()
            db.add(app.ProductModifierOption(
                group_id=group.id, name="Queso extra",
                price_delta_cents=1500, active=True, sort_order=0,
            ))
            db.add(app.ProductPromotion(
                product_id=product.id, name="Promoción de prueba",
                price_cents=11000, discount_percent=None,
                weekdays="0,1,2,3,4,5,6", start_time="00:00",
                end_time="23:59", active=True,
            ))
            db.commit()
            product_id = product.id

        token = app.jwt.encode(
            {"sub": str(owner_id), "ver": 1,
             "exp": app.now() + timedelta(minutes=30)},
            app.SECRET_KEY, algorithm="HS256",
        )
        headers = {"Authorization": "Bearer " + token}

        statements = []
        def count_selects(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/products", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)

        assert response.status_code == 200, response.text
        rows = response.json()
        product_data = next(row for row in rows if row["id"] == product_id)
        assert product_data["variants"][0]["name"] == "Grande"
        assert product_data["modifier_groups"][0]["options"][0]["name"] == "Queso extra"
        assert product_data["promotions"][0]["name"] == "Promoción de prueba"
        assert product_data["current_price"] == "110.00"
        assert len(statements) <= 8, (
            "GET /api/products volvió a ejecutar consultas por producto: "
            + str(len(statements))
        )
        print(f"OK: catálogo completo con {len(statements)} consultas SELECT; sin N+1.")

finally:
    dbfile.unlink(missing_ok=True)
