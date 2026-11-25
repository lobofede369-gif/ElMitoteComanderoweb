"""Performance regression checks for high-frequency POS read paths."""
import os
import re
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
from sqlalchemy import event, inspect
from fastapi.testclient import TestClient

try:
    with TestClient(app.app) as client:
        expected_indexes = {
            "accounts": {"ix_accounts_owner_created_at", "ix_accounts_created_at"},
            "batches": {"ix_batches_account_id"},
            "order_items": {"ix_order_items_batch_id"},
            "payments": {"ix_payments_account_id"},
        }
        def current_indexes():
            inspector = inspect(app.engine)
            return {
                table: {row["name"] for row in inspector.get_indexes(table)}
                for table in expected_indexes
            }
        assert all(expected <= current_indexes()[table]
                   for table, expected in expected_indexes.items()), current_indexes()
        # Simular una base ya existente sin los nuevos índices y comprobar
        # que bootstrap los restaura sin recrear tablas ni perder registros.
        with app.engine.begin() as connection:
            for names in expected_indexes.values():
                for name in names:
                    connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')
        app.bootstrap()
        restored = current_indexes()
        assert all(expected <= restored[table]
                   for table, expected in expected_indexes.items()), restored
        print("OK: índices de alto tráfico creados y migración idempotente.")

        with app.SessionLocal() as db:
            owner_id = db.scalar(app.select(app.User.id).where(app.User.role == "owner"))
            staff_ids = []
            test_pin = "641829"
            pin_user = app.User(
                username="perf_pin_login",
                display="Mesero acceso perf", role="waiter",
                active=True, password_hash="test-only",
            )
            db.add(pin_user)
            db.flush()
            db.add(app.UserPin(
                user_id=pin_user.id, lookup=app._pin_lookup(test_pin),
                pin_hash=app.ph.hash(test_pin),
            ))
            pin_login_user_id = pin_user.id
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

        # PIN correcto: una consulta SQL para PIN+usuario, sin segunda lectura.
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            login_response = client.post("/api/login/pin", json={"pin": test_pin})
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert login_response.status_code == 200, login_response.text
        login_data = login_response.json()
        assert login_data["user"]["id"] == pin_login_user_id, login_data
        assert login_data["user"]["role"] == "waiter", login_data
        assert len(statements) == 1, (
            "El login PIN debe resolver PIN y usuario en una consulta: "
            + str(len(statements))
        )
        assert login_response.headers.get("X-Request-ID"), login_response.headers
        assert login_response.headers.get("Server-Timing", "").startswith("app;dur="), login_response.headers
        print("OK: login PIN válido con una consulta SQL y perfil devuelto.")

        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/products", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)

        assert response.status_code == 200, response.text
        assert response.headers.get("X-Request-ID"), response.headers
        assert response.headers.get("Server-Timing", "").startswith("app;dur="), response.headers
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
        assert len(user_rows) == 14, len(user_rows)
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
            accounts[0].status = "pending"
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

        # Resumen administrativo: métricas agregadas sin descargar los tres listados.
        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/overview", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        overview = response.json()
        assert overview["active_accounts"] == 12, overview
        assert overview["pending_accounts"] == 1, overview
        assert overview["new_tickets"] == 12, overview
        assert overview["products"] >= 1, overview
        assert len(statements) <= 6, (
            "GET /api/overview debe usar agregados, no descargar listados: "
            + str(len(statements))
        )
        print(f"OK: resumen administrativo con {len(statements)} consultas SELECT.")

        # Caja: una sola petición entrega lista pendiente y detalle seleccionado.
        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get(
                f"/api/cashier/overview?account_id={first_account_id}", headers=headers
            )
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        cashier = response.json()
        assert len(cashier["accounts"]) == 1, cashier["accounts"]
        assert cashier["accounts"][0]["id"] == first_account_id, cashier
        assert cashier["selected"]["id"] == first_account_id, cashier
        assert cashier["selected"]["items"][0]["name"] == "Producto perf", cashier
        assert len(statements) <= 24, (
            "GET /api/cashier/overview debe evitar el viaje secuencial lista+detalle: "
            + str(len(statements))
        )
        print(f"OK: panel de cobros y detalle inicial con {len(statements)} consultas SELECT.")

        # Caja: agregados SQL para pagos/propinas y serialización en lote de cuentas bloqueantes.
        statements.clear()
        event.listen(app.engine, "before_cursor_execute", count_selects)
        try:
            response = client.get("/api/cash", headers=headers)
        finally:
            event.remove(app.engine, "before_cursor_execute", count_selects)
        assert response.status_code == 200, response.text
        cash = response.json()
        assert cash["cash"] == "10.00", cash
        assert cash["tips"] == "2.00", cash
        assert cash["blocking_count"] == 12, cash
        assert len(cash["blocking_accounts"]) == 12, cash
        assert len(statements) <= 20, (
            "GET /api/cash debe agrupar cobros y cuentas bloqueantes: "
            + str(len(statements))
        )
        print(f"OK: resumen de caja con {len(statements)} consultas SELECT.")

        # Reintentar la creación tras una respuesta perdida no debe duplicar la cuenta.
        create_payload = {"name": "Cuenta idempotente", "table_id": None}
        create_key = uuid4().hex
        first_create = client.post(
            "/api/accounts", json={**create_payload, "client_key": create_key},
            headers=headers,
        )
        assert first_create.status_code == 200, first_create.text
        first_create_data = first_create.json()
        assert first_create_data["duplicate"] is False, first_create_data
        retry_create = client.post(
            "/api/accounts", json={**create_payload, "client_key": create_key},
            headers=headers,
        )
        assert retry_create.status_code == 200, retry_create.text
        retry_data = retry_create.json()
        assert retry_data["id"] == first_create_data["id"], retry_data
        assert retry_data["duplicate"] is True, retry_data
        changed_payload = {"name": "Cuenta idempotente modificada", "table_id": None}
        changed_retry = client.post(
            "/api/accounts", json={**changed_payload, "client_key": create_key},
            headers=headers,
        )
        assert changed_retry.status_code == 409, changed_retry.text
        with app.SessionLocal() as db:
            created_count = db.scalar(app.select(app.func.count(app.Account.id)).where(
                app.Account.name == "Cuenta idempotente"))
            assert created_count == 1, created_count
        print("OK: reintentos de apertura idempotentes; no duplica cuentas ni cambia el payload.")

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
        # P14: static/sw.js ya no existe en disco; /sw.js es una ruta que
        # genera el SHELL desde index.html en cada petición.
        sw_resp = client.get("/sw.js")
        assert sw_resp.status_code == 200, sw_resp.text
        sw = sw_resp.text
        html = (root / "static/index.html").read_text(encoding="utf-8")
        account_detail = js.split("async function accountDetailHtml", 1)[1].split(
            "async function refreshAccountPanel", 1
        )[0]
        assert "apiInflight:new Map()" in js
        start_flow = js.split("async function start(authenticatedUser=null)", 1)[1].split(
            "async function go(", 1
        )[0]
        login_flow = js.split("async function loginSession", 1)[1].split(
            "$('#login-pin-form').addEventListener", 1
        )[0]
        assert "await start(d.user)" in login_flow
        assert "state.user=validUser||await api('/me')" in start_flow
        assert "const initialRender=render({preserveScroll:false})" in start_flow
        assert "await syncWaiterReadyAlerts" not in start_flow
        assert start_flow.index("const initialRender=render({preserveScroll:false})") < start_flow.index(
            "listenEvents()"
        ) < start_flow.index("await initialRender")
        assert "function loadingScreenHtml(screen)" in js
        assert "api('/cashier/overview'+query)" in js
        assert "api('/overview')" in js
        assert "function loadCashHistory(seq=state.renderSeq)" in js
        assert "function waiterReadyAlertsHtml()" in js
        app_source = (root / "app.py").read_text(encoding="utf-8")
        assert ".join(User, User.id == UserPin.user_id)" in app_source
        assert "slow_pin_login" in app_source
        assert "observe_request_performance" in app_source
        assert "slow_request" in app_source
        new_account_action = js.split("case 'new-account':", 1)[1].split("case 'reassign-form':", 1)[0]
        assert "pendingOperationKey(scope,payload)" in new_account_action
        assert "client_key" in new_account_action
        assert "state.apiInflight.clear()" in js
        assert "load-account-history" in account_detail
        assert "api(`/accounts/${id}/history`)" not in account_detail
        assert "accountHistoryMarkup(rows)" in js
        assert "prepClockTimer" in features
        assert "document.hidden" in features
        # El SHELL generado debe listar exactamente los assets versionados
        # que pide la página (antes: precache fijo con app.js?v=30, que se
        # quedaba viejo en cada despliegue) y el caché cambia de nombre
        # cuando cambia cualquier asset.
        assert re.search(r"mitote-comandas-[0-9a-f]{12}", sw), sw[:200]
        versioned = sorted(set(re.findall(r"/static/[^\"'\s]+\?v=\d+", html)))
        assert versioned, "index.html no referencia assets versionados"
        assert all(u in sw for u in versioned), [u for u in versioned if u not in sw]
        assert "function setConnectionState(connected)" in js
        print("OK: deduplicación GET, historial diferido, cronómetros activos solo en pantalla y caché versionada.")

finally:
    dbfile.unlink(missing_ok=True)
