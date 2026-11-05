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
import mitote_extensions as ext
from sqlalchemy import event
from fastapi.testclient import TestClient

try:
    with TestClient(app.app) as client:
        with app.SessionLocal() as db:
            owner_id = db.scalar(app.select(app.User.id).where(app.User.role == "owner"))
            staff_ids = []
            for number in range(12):
                staff = app.User(
                    username=f"perf_waiter_{number}",
                    display=f"Mesero perf {number}", role="waiter",
                    active=True, password_hash="test-only",
                )
                db.add(staff)
                db.flush()
                db.add(app.UserPin(
                    user_id=staff.id, lookup=f"perf-pin-{uuid4().hex}",
                    pin_hash="test-only",
                ))
                staff_ids.append(staff.id)
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

        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/users", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        user_rows = response.json()
        assert len(user_rows) == 13, len(user_rows)
        assert all(next(row for row in user_rows if row["id"] == staff_id)["has_pin"]
                   for staff_id in staff_ids)
        assert len(statements) <= 4, (
            "GET /api/users volvió a consultar el PIN por empleado: "
            + str(len(statements))
        )
        print(f"OK: {len(user_rows)} usuarios con {len(statements)} consultas SELECT; PIN correctos.")

        # La lista de cuentas también debe usar agregados en bloque, no consultar
        # productos, descuentos, pagos y mesas una vez por cada cuenta.
        with app.SessionLocal() as db:
            period = app.CashPeriod(opening_cents=0)
            db.add(period)
            db.flush()
            accounts = []
            items = []
            for number in range(12):
                account = app.Account(name=f"Cuenta perf {number}", owner_id=owner_id)
                db.add(account)
                db.flush()
                batch = app.Batch(
                    account_id=account.id, sent_by=owner_id,
                    client_key=f"perf-{uuid4().hex}",
                )
                db.add(batch)
                db.flush()
                item = app.OrderItem(
                    batch_id=batch.id, product_id=product_id,
                    name="Producto perf", station="kitchen",
                    price_cents=12500, qty=number + 1, cancelled_qty=0,
                )
                db.add(item)
                db.flush()
                db.add(app.StationTicket(batch_id=batch.id, station="kitchen", status="new"))
                db.add(app.OrderItemMeta(order_item_id=item.id))
                db.add(ext.BatchPrep(batch_id=batch.id, minutes=25))
                accounts.append(account)
                items.append(item)
            db.flush()
            db.add(app.AccountDiscount(
                account_id=accounts[0].id, item_id=items[0].id,
                created_by=owner_id, mode="fixed", scope="account",
                value_text="10.00", base_cents=12500, discount_cents=1000,
                reason="Prueba de agregados", status="applied",
            ))
            payment = app.Payment(
                account_id=accounts[0].id, cashier_id=owner_id,
                period_id=period.id, client_key=f"perf-pay-{uuid4().hex}",
                method="cash", cents=1000, received_cents=1000,
            )
            db.add(payment)
            db.flush()
            db.add(app.PaymentTip(
                payment_id=payment.id, account_id=accounts[0].id,
                cashier_id=owner_id, period_id=period.id,
                method="cash", cents=200,
            ))
            table = app.DiningTable(
                name="Mesa perf", name_key="mesa perf", area="Pruebas",
                capacity=4, active=True,
            )
            db.add(table)
            for number in range(1, 12):
                db.add(app.DiningTable(
                    name=f"Mesa perf {number}", name_key=f"mesa perf {number}",
                    area="Pruebas", capacity=4, active=True,
                    sort_order=number,
                ))
            db.flush()
            db.add(app.TableOccupancy(
                table_id=table.id, account_id=accounts[1].id,
                assigned_by=owner_id, assigned_at=app.now(),
            ))
            db.commit()
            first_account_id = accounts[0].id
            second_account_id = accounts[1].id
            expected_table_id = table.id

        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/accounts", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)

        assert response.status_code == 200, response.text
        account_rows = response.json()
        assert len(account_rows) == 12, len(account_rows)
        first = next(row for row in account_rows if row["id"] == first_account_id)
        assert first["gross_total"] == "125.00", first
        assert first["discount_total"] == "10.00", first
        assert first["total"] == "115.00", first
        assert first["paid"] == "10.00", first
        assert first["due"] == "105.00", first
        second = next(row for row in account_rows if row["id"] == second_account_id)
        assert second["table_id"] == expected_table_id, second
        assert second["table_name"] == "Mesa perf", second
        assert len(statements) <= 9, (
            "GET /api/accounts volvió a ejecutar consultas por cuenta: "
            + str(len(statements))
        )
        print(f"OK: 12 cuentas con {len(statements)} consultas SELECT; totales y mesa correctos.")

        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get(f"/api/accounts/{first_account_id}", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        detail = response.json()
        assert len(detail["items"]) == 1, detail["items"]
        assert detail["payments"][0]["tip"] == "2.00", detail["payments"]
        assert detail["tips"][0]["amount"] == "2.00", detail["tips"]
        assert detail["discounts"][0]["item_name"] == "Producto perf", detail["discounts"]
        assert len(statements) <= 15, (
            "GET /api/accounts/{id} volvió a ejecutar consultas por producto: "
            + str(len(statements))
        )
        print(f"OK: detalle de cuenta con {len(statements)} consultas SELECT; historial contable íntegro.")

        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/tables", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        table_rows = response.json()
        assert len(table_rows) == 12, len(table_rows)
        occupied = next(row for row in table_rows if row["id"] == expected_table_id)
        assert occupied["status"] == "occupied", occupied
        assert occupied["account_id"] == second_account_id, occupied
        assert len(statements) <= 4, (
            "GET /api/tables volvió a consultar ocupación por mesa: "
            + str(len(statements))
        )
        print(f"OK: 12 mesas con {len(statements)} consultas SELECT; ocupación correcta.")

        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/preparation/tickets", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        prep = response.json()
        assert len(prep["tickets"]) == 12, len(prep["tickets"])
        newest = prep["tickets"][0]
        assert newest["items"][0]["name"] == "Producto perf", newest
        assert newest["batch_minutes"] == 25, newest
        assert len(statements) <= 13, (
            "GET /api/preparation/tickets volvió a ejecutar consultas por comanda: "
            + str(len(statements))
        )
        print(f"OK: 12 comandas de cocina con {len(statements)} consultas SELECT; cronómetros correctos.")

        root = Path(__file__).resolve().parents[1]
        js = (root / "static/app.js").read_text(encoding="utf-8")
        features = (root / "static/features.js").read_text(encoding="utf-8")
        sw = (root / "static/sw.js").read_text(encoding="utf-8")
        html = (root / "static/index.html").read_text(encoding="utf-8")
        account_detail = js.split("async function accountDetailHtml", 1)[1].split(
            "async function refreshAccountPanel", 1
        )[0]
        assert "apiInflight:new Map()" in js
        assert "state.apiInflight.clear()" in js
        assert "load-account-history" in account_detail
        assert "api(`/accounts/${id}/history`)" not in account_detail
        assert "accountHistoryMarkup(rows)" in js
        assert "prepClockTimer" in features
        assert "document.hidden" in features
        assert "mitote-comandas-v28" in sw
        assert "/static/app.js?v=24" in sw and "/static/app.js?v=24" in html
        assert "/static/features.js?v=3" in sw and "/static/features.js?v=3" in html
        print("OK: deduplicación GET, historial diferido, cronómetros activos solo en pantalla y caché versionada.")

finally:
    dbfile.unlink(missing_ok=True)
