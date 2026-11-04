"""Initializes an isolated, disposable El Mitote UI preview with synthetic data.

This file must never be run on the production service.
Run only with MITOTE_DEMO_MODE=1 and the exact local sqlite demo URL.
"""
import os
import sys
from pathlib import Path
from sqlalchemy import select
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as mitote

EXPECTED_DB = "sqlite:////tmp/mitote-preview.db"
if os.getenv("MITOTE_DEMO_MODE") != "1" or os.getenv("DATABASE_URL") != EXPECTED_DB:
    raise SystemExit("Demo seed refused: isolated SQLite preview configuration required")

mitote.bootstrap()

with mitote.SessionLocal() as db:
    if db.scalar(select(mitote.User).where(mitote.User.username == "mesero_demo")) is not None:
        print("Demo database already seeded")
        raise SystemExit(0)

    staff_password = os.environ["DEMO_STAFF_PASSWORD"]
    waiter_pin = os.environ["DEMO_WAITER_PIN"]

    staff = [
        ("mesero_demo", "Mesero de Prueba", "waiter", waiter_pin),
        ("cocina_demo", "Cocina de Prueba", "kitchen", staff_password),
        ("barra_demo", "Barra de Prueba", "bar", staff_password),
        ("caja_demo", "Caja de Prueba", "cashier", staff_password),
        ("admin_demo", "Administrador de Prueba", "admin", staff_password),
    ]
    users = []
    for username, display, role, password in staff:
        u = mitote.User(username=username, display=display, role=role,
                        password_hash=mitote.ph.hash(password), active=True)
        db.add(u)
        users.append(u)
    db.flush()
    waiter = users[0]

    products = [
        ("Tacos al pastor", "Alimentos", "kitchen", 9800, 85),
        ("Hamburguesa especial", "Alimentos", "kitchen", 16500, 42),
        ("Papas a la francesa", "Entradas", "kitchen", 6900, 65),
        ("Enchiladas", "Alimentos", "kitchen", 14500, 30),
        ("Limonada natural", "Bebidas", "bar", 4200, 45),
        ("Refresco", "Bebidas", "bar", 3500, 90),
        ("Agua mineral", "Bebidas", "bar", 3200, 70),
    ]
    product_objs = []
    for name, category, station, price, stock in products:
        p = mitote.Product(name=name, category=category, station=station,
                           price_cents=price, active=True, blocked=False,
                           inventory_enabled=True, stock=stock)
        db.add(p)
        product_objs.append(p)
    db.flush()

    tables = []
    for idx in range(1, 7):
        t = mitote.DiningTable(name=f"Mesa {idx}", name_key=f"mesa {idx}",
                               area="Salón" if idx < 5 else "Terraza",
                               capacity=4, active=True, sort_order=idx)
        db.add(t)
        tables.append(t)
    db.add(mitote.CashPeriod(opening_cents=50000))
    db.flush()

    # Visible sample orders for waiter, kitchen/bar and cashier screens.
    examples = [
        ("Mesa 2", "open", tables[1],
         [(0, 2, "Sin cebolla"), (4, 2, "Sin hielo")]),
        ("Terraza", "open", tables[4],
         [(1, 1, ""), (5, 2, "")]),
        ("Para llevar", "pending", None,
         [(2, 1, ""), (6, 1, "")]),
    ]
    for account_name, status, table, lines in examples:
        acc = mitote.Account(name=account_name, status=status, owner_id=waiter.id)
        db.add(acc)
        db.flush()
        if table is not None:
            db.add(mitote.TableOccupancy(table_id=table.id, account_id=acc.id,
                                         assigned_by=waiter.id))
        batch = mitote.Batch(account_id=acc.id, sent_by=waiter.id,
                             client_key=f"demo-seed-{acc.id}")
        db.add(batch)
        db.flush()
        stations = set()
        for product_index, qty, note in lines:
            p = product_objs[product_index]
            item = mitote.OrderItem(batch_id=batch.id, product_id=p.id,
                                    name=p.name, station=p.station, qty=qty,
                                    price_cents=p.price_cents, note=note,
                                    cancelled_qty=0)
            db.add(item)
            stations.add(p.station)
        db.flush()
        for station in stations:
            state = "new" if account_name=="Mesa 2" else (
                "preparing" if account_name=="Terraza" else "ready"
            )
            db.add(mitote.StationTicket(batch_id=batch.id,
                                        station=station,status=state))
    db.commit()
print("Isolated preview initialized with synthetic users, products, accounts and station tickets")
