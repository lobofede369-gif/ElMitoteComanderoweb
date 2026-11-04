"""Extensiones independientes: preparación mixta, reservaciones y analítica.
Las tablas nuevas se registran antes del bootstrap de Base.metadata.
"""
import asyncio
import hmac
import json
import logging
import os
from datetime import date, datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Literal, Optional

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import (
    app, Base, Batch, Account, DiningTable, TableOccupancy, Product, OrderItem,
    OrderItemMeta, StationTicket, Payment, PaymentTip, Audit, User,
    SessionLocal, db_session, auth, allow, audit, active_period, now, notify,
    push_to, fcm_to, ticket_status, StatusIn, amount
)

LOG = logging.getLogger("mitote.extensions")
LOCAL_TZ = ZoneInfo("America/Mexico_City")
RESERVATIONS_INGEST_TOKEN = os.getenv("RESERVATIONS_INGEST_TOKEN", "").strip()


def aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def elapsed_seconds(start, end=None):
    return max(0, int(((aware(end) if end else now()) - aware(start)).total_seconds())) if start else 0


class PrepPolicy(Base):
    __tablename__ = "prep_policy"
    id = Column(Integer, primary_key=True)
    batch_minutes = Column(Integer, nullable=False, default=25)
    warning_percent = Column(Integer, nullable=False, default=80)


class ProductPrep(Base):
    __tablename__ = "product_prep"
    product_id = Column(Integer, ForeignKey("products.id"), primary_key=True)
    minutes = Column(Integer, nullable=False)


class BatchPrep(Base):
    __tablename__ = "batch_prep"
    batch_id = Column(Integer, ForeignKey("batches.id"), primary_key=True)
    minutes = Column(Integer, nullable=False)


class ItemPrep(Base):
    __tablename__ = "item_prep"
    order_item_id = Column(Integer, ForeignKey("order_items.id"), primary_key=True)
    ready_at = Column(DateTime(timezone=True), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=False)


class PrepAlert(Base):
    __tablename__ = "prep_alerts"
    id = Column(Integer, primary_key=True)
    alert_key = Column(String(90), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)


class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    source = Column(String(32), default="manual", nullable=False)
    external_id = Column(String(120), unique=True, nullable=True)
    name = Column(String(120), nullable=False)
    phone = Column(String(40), default="", nullable=False)
    party_size = Column(Integer, nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), default="pending", nullable=False)
    note = Column(String(400), default="", nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    arrived_at = Column(DateTime(timezone=True))


def policy(db):
    return db.get(PrepPolicy, 1)


def product_prep_minutes(db, product_id, requested=15):
    configured = db.get(ProductPrep, product_id)
    return configured.minutes if configured else max(1, min(240, int(requested or 15)))


def snapshot_batch_policy(db, batch_id):
    config = policy(db)
    db.add(BatchPrep(batch_id=batch_id, minutes=config.batch_minutes if config else 25))


def batch_limit(db, batch_id):
    snapshot = db.get(BatchPrep, batch_id)
    if snapshot:
        return snapshot.minutes
    config = policy(db)
    return config.batch_minutes if config else 25


def get_item_completion(db, item_id, ticket):
    row = db.get(ItemPrep, item_id)
    return (row.ready_at if row else ticket.ready_at if ticket.status == "ready" else None)


class PrepPolicyIn(BaseModel):
    batch_minutes: int = Field(ge=1, le=240)
    warning_percent: int = Field(ge=50, le=95)


class ProductPrepIn(BaseModel):
    minutes: int = Field(ge=1, le=240)


@app.get("/api/preparation/config")
def prep_config(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    p = policy(db)
    configs = {row.product_id: row.minutes for row in db.scalars(select(ProductPrep))}
    products = db.scalars(select(Product).order_by(Product.name, Product.id)).all()
    return {"batch_minutes": p.batch_minutes if p else 25,
            "warning_percent": p.warning_percent if p else 80,
            "products": [{"id": pr.id, "name": pr.name, "station": pr.station,
                          "minutes": configs.get(pr.id, 15)} for pr in products]}


@app.put("/api/preparation/config")
async def put_prep_config(data: PrepPolicyIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    p = policy(db)
    if not p:
        p = PrepPolicy(id=1)
        db.add(p)
    p.batch_minutes = data.batch_minutes
    p.warning_percent = data.warning_percent
    audit(db, user, "prep.policy", batch_minutes=p.batch_minutes, warning_percent=p.warning_percent)
    db.commit()
    await notify()
    return {"ok": True}


@app.put("/api/preparation/products/{product_id}")
async def put_product_prep(product_id: int, data: ProductPrepIn, db: Session = Depends(db_session),
                           user: User = Depends(auth)):
    allow(user, "owner", "admin")
    if not db.get(Product, product_id):
        raise HTTPException(404, "Producto no encontrado")
    p = db.get(ProductPrep, product_id)
    if not p:
        p = ProductPrep(product_id=product_id)
        db.add(p)
    p.minutes = data.minutes
    audit(db, user, "prep.product", product_id=product_id, minutes=p.minutes)
    db.commit()
    await notify()
    return {"ok": True}


@app.get("/api/preparation/tickets")
def preparation_tickets(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin", "kitchen", "bar")
    query = select(StationTicket).order_by(StationTicket.id.desc()).limit(150)
    if user.role in ("kitchen", "bar"):
        query = query.where(StationTicket.station == user.role)
    tickets = db.scalars(query).all()
    output = []
    config = policy(db)
    warning_pct = config.warning_percent if config else 80
    stamp = now()
    for ticket in tickets:
        batch = db.get(Batch, ticket.batch_id)
        account = db.get(Account, batch.account_id)
        waiter = db.get(User, batch.sent_by)
        rows = db.scalars(select(OrderItem).where(OrderItem.batch_id == batch.id,
                                                   OrderItem.station == ticket.station)).all()
        items = []
        for item in rows:
            qty = item.qty - item.cancelled_qty
            if qty <= 0:
                continue
            meta = db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id == item.id))
            limit = meta.prep_minutes if meta else 15
            completion = get_item_completion(db, item.id, ticket)
            elapsed = elapsed_seconds(batch.created_at, completion or stamp)
            items.append({
                "id": item.id, "name": item.name, "qty": qty, "note": item.note,
                "variant_name": meta.variant_name if meta else "",
                "modifiers": json.loads(meta.modifiers_json or "[]") if meta else [],
                "prep_minutes": max(1, limit), "ready": bool(completion),
                "ready_at": aware(completion).isoformat() if completion else None,
                "elapsed_seconds": elapsed,
                "timing": "ready" if completion else "late" if elapsed > limit * 60
                           else "warning" if elapsed >= limit * 60 * warning_pct / 100 else "ok",
            })
        other_tickets = db.scalars(select(StationTicket).where(StationTicket.batch_id == batch.id)).all()
        batch_ready = all(t.status == "ready" for t in other_tickets)
        finished = max((aware(t.ready_at) for t in other_tickets if t.ready_at), default=None) if batch_ready else None
        batch_seconds = elapsed_seconds(batch.created_at, finished or stamp)
        limit = batch_limit(db, batch.id)
        output.append({
            "id": ticket.id, "batch_id": batch.id, "account_id": account.id, "account": account.name,
            "station": ticket.station, "status": ticket.status,
            "created_at": aware(batch.created_at).isoformat(), "waiter": waiter.display if waiter else "?",
            "items": items, "batch_minutes": limit, "batch_ready": batch_ready,
            "batch_elapsed_seconds": batch_seconds,
            "batch_timing": "ready" if batch_ready else "late" if batch_seconds > limit * 60
                            else "warning" if batch_seconds >= limit * 60 * warning_pct / 100 else "ok",
        })
    return {"server_time": stamp.isoformat(), "tickets": output}


@app.post("/api/preparation/items/{item_id}/ready")
async def mark_item_ready(item_id: int, db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin", "kitchen", "bar")
    item = db.get(OrderItem, item_id)
    if not item:
        raise HTTPException(404, "Producto no encontrado")
    ticket = db.execute(select(StationTicket).where(StationTicket.batch_id == item.batch_id,
                    StationTicket.station == item.station).with_for_update()).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Estación no encontrada")
    if user.role in ("kitchen", "bar") and item.station != user.role:
        raise HTTPException(403, "No corresponde a tu estación")
    prior = db.get(ItemPrep, item.id)
    if prior or ticket.status == "ready":
        return {"ok": True, "duplicate": True}
    if ticket.status != "preparing":
        raise HTTPException(409, "Primero confirma recepción e inicia la preparación")
    db.add(ItemPrep(order_item_id=item.id, ready_at=now(), actor_id=user.id))
    audit(db, user, "prep.item_ready", item_id=item.id, batch_id=item.batch_id, station=item.station)
    db.flush()
    remaining = db.scalars(select(OrderItem).where(OrderItem.batch_id == item.batch_id,
                       OrderItem.station == item.station)).all()
    all_ready = all(row.qty <= row.cancelled_qty or db.get(ItemPrep, row.id) is not None for row in remaining)
    if all_ready:
        # Reutiliza el flujo ya existente (auditoría, notificaciones al mesero y FCM).
        return await ticket_status(ticket.id, StatusIn(status="ready"), db, user)
    db.commit()
    await notify()
    return {"ok": True, "duplicate": False}


class ReservationIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(default="", max_length=40)
    party_size: int = Field(ge=1, le=30)
    scheduled_at: datetime
    note: str = Field(default="", max_length=400)
    external_id: Optional[str] = Field(default=None, min_length=3, max_length=120)


def reservation_dict(row):
    return {
        "id": row.id, "external_id": row.external_id, "source": row.source,
        "name": row.name, "phone": row.phone, "party_size": row.party_size,
        "scheduled_at": aware(row.scheduled_at).isoformat(), "status": row.status,
        "note": row.note, "account_id": row.account_id,
        "arrived_at": aware(row.arrived_at).isoformat() if row.arrived_at else None,
    }


def create_reservation(db, data, source, actor=None):
    existing = db.scalar(select(Reservation).where(Reservation.external_id == data.external_id)) if data.external_id else None
    if existing:
        return existing, True
    row = Reservation(name=data.name.strip(), phone=data.phone.strip(), party_size=data.party_size,
                      scheduled_at=aware(data.scheduled_at), note=data.note.strip(),
                      external_id=data.external_id, source=source)
    if not row.name:
        raise HTTPException(400, "Nombre obligatorio")
    db.add(row)
    db.flush()
    audit(db, actor, "reservation.create", reservation_id=row.id, source=source)
    return row, False


@app.post("/api/reservations/ingest")
async def ingest_reservation(data: ReservationIn, authorization: Optional[str] = Header(None),
                             db: Session = Depends(db_session)):
    # Este endpoint es únicamente de servidor a servidor; nunca incrustar el secreto en JS público.
    if not RESERVATIONS_INGEST_TOKEN:
        raise HTTPException(503, "Integración externa sin configurar")
    candidate = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(candidate, RESERVATIONS_INGEST_TOKEN):
        raise HTTPException(401, "Integración no autorizada")
    if not data.external_id:
        raise HTTPException(422, "external_id obligatorio para solicitudes externas")
    try:
        row, duplicate = create_reservation(db, data, "website")
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(select(Reservation).where(Reservation.external_id == data.external_id))
        if row is None:
            raise
        duplicate = True
    if not duplicate:
        await notify()
    return {"id": row.id, "duplicate": duplicate}


@app.post("/api/reservations")
async def add_reservation(data: ReservationIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    row, duplicate = create_reservation(db, data, "manual", user)
    db.commit()
    await notify()
    return {"id": row.id, "duplicate": duplicate}


@app.get("/api/reservations")
def reservations(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    rows = db.scalars(select(Reservation).order_by(Reservation.scheduled_at.desc()).limit(200)).all()
    return [reservation_dict(x) for x in rows]


class ReservationStatusIn(BaseModel):
    status: Literal["confirmed", "cancelled", "no_show"]


@app.post("/api/reservations/{reservation_id}/status")
async def change_reservation_status(reservation_id: int, data: ReservationStatusIn,
                                    db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    row = db.execute(select(Reservation).where(Reservation.id == reservation_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Reservación no encontrada")
    if row.account_id:
        raise HTTPException(409, "Ya existe una cuenta para esta reservación")
    if data.status == "confirmed" and row.status != "pending":
        raise HTTPException(409, "Solo se pueden confirmar reservaciones pendientes")
    if data.status in ("cancelled", "no_show") and row.status not in ("pending", "confirmed"):
        raise HTTPException(409, "Estado incompatible")
    row.status = data.status
    audit(db, user, "reservation.status", reservation_id=row.id, status=row.status)
    db.commit()
    await notify()
    return {"ok": True}


class ReservationArrivalIn(BaseModel):
    table_id: int
    waiter_id: int


@app.post("/api/reservations/{reservation_id}/arrive")
async def reservation_arrival(reservation_id: int, data: ReservationArrivalIn,
                              db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, "owner", "admin")
    row = db.execute(select(Reservation).where(Reservation.id == reservation_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Reservación no encontrada")
    if row.account_id:
        return {"ok": True, "duplicate": True, "account_id": row.account_id}
    if row.status not in ("pending", "confirmed"):
        raise HTTPException(409, "La reservación no está activa")
    active_period(db)
    waiter = db.get(User, data.waiter_id)
    if not waiter or not waiter.active or waiter.role != "waiter":
        raise HTTPException(400, "Selecciona un mesero activo")
    table = db.execute(select(DiningTable).where(DiningTable.id == data.table_id).with_for_update()).scalar_one_or_none()
    if not table or not table.active:
        raise HTTPException(404, "Mesa no disponible")
    if table.capacity < row.party_size:
        raise HTTPException(409, "La capacidad de la mesa es insuficiente")
    if db.get(TableOccupancy, table.id):
        raise HTTPException(409, "La mesa está ocupada")
    acc = Account(name=row.name, owner_id=waiter.id)
    db.add(acc)
    db.flush()
    db.add(TableOccupancy(table_id=table.id, account_id=acc.id, assigned_by=user.id))
    row.status = "arrived"
    row.arrived_at = now()
    row.account_id = acc.id
    audit(db, user, "reservation.arrive", reservation_id=row.id, account_id=acc.id,
          table_id=table.id, waiter_id=waiter.id)
    audit(db, user, "table.assign", table_id=table.id, table_name=table.name,
          account_id=acc.id, waiter=waiter.display)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "La mesa acaba de ocuparse; actualiza la pantalla")
    await notify()
    return {"ok": True, "account_id": acc.id, "duplicate": False}


def close_reservation_for_account(db, account_id, actor):
    row = db.scalar(select(Reservation).where(Reservation.account_id == account_id))
    if row and row.status == "arrived":
        row.status = "served"
        audit(db, actor, "reservation.served", reservation_id=row.id, account_id=account_id)


class AnalyticsQuery(BaseModel):
    start: date
    end: date


@app.get("/api/owner/analytics")
def owner_analytics(start: date, end: date, db: Session = Depends(db_session),
                    user: User = Depends(auth)):
    allow(user, "owner")
    if end < start or (end - start).days > 93:
        raise HTTPException(400, "El periodo debe ser de 1 a 94 días")
    begin = datetime.combine(start, day_time.min, LOCAL_TZ).astimezone(timezone.utc)
    finish = datetime.combine(end + timedelta(days=1), day_time.min, LOCAL_TZ).astimezone(timezone.utc)
    payments = db.scalars(select(Payment).where(Payment.created_at >= begin, Payment.created_at < finish)).all()
    sales_cents = sum(p.cents for p in payments)
    accounts_paid = len({p.account_id for p in payments})
    methods = {method: sum(p.cents for p in payments if p.method == method)
               for method in ("cash", "card", "transfer")}
    by_day = {str(start + timedelta(days=d)): 0 for d in range((end - start).days + 1)}
    by_hour = {f"{h:02d}:00": 0 for h in range(24)}
    for p in payments:
        local = aware(p.created_at).astimezone(LOCAL_TZ)
        by_day[local.date().isoformat()] += p.cents
        by_hour[f"{local.hour:02d}:00"] += p.cents
    batches = db.scalars(select(Batch).where(Batch.created_at >= begin, Batch.created_at < finish)).all()
    batch_ids = [b.id for b in batches]
    products = {}
    if batch_ids:
        for item in db.scalars(select(OrderItem).where(OrderItem.batch_id.in_(batch_ids))):
            qty = max(0, item.qty - item.cancelled_qty)
            if qty:
                row = products.setdefault(item.product_id, {"product_id": item.product_id, "name": item.name,
                    "quantity": 0, "estimated_gross_cents": 0})
                row["quantity"] += qty
                row["estimated_gross_cents"] += qty * item.price_cents
    product_rank = sorted(products.values(), key=lambda x: (-x["quantity"], x["name"]))[:15]
    for product in product_rank:
        product["estimated_gross"] = amount(product.pop("estimated_gross_cents"))
    tickets = db.scalars(select(StationTicket).where(
        StationTicket.ready_at >= begin, StationTicket.ready_at < finish,
        StationTicket.status == "ready")).all()
    prep_samples = [elapsed_seconds(t.created_at, t.ready_at) for t in tickets]
    prep_average = round(sum(prep_samples) / len(prep_samples) / 60, 1) if prep_samples else None
    assignments = db.scalars(select(Audit).where(
        Audit.action == "table.assign", Audit.created_at >= begin, Audit.created_at < finish)).all()
    rotation = {}
    for entry in assignments:
        try:
            detail = json.loads(entry.detail or "{}")
            tid = int(detail["table_id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        table = db.get(DiningTable, tid)
        label = table.name if table else str(tid)
        rotation[label] = rotation.get(label, 0) + 1
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": amount(sales_cents), "paid_accounts": accounts_paid,
        "average_ticket": amount(round(sales_cents / accounts_paid)) if accounts_paid else "0.00",
        "methods": {k: amount(v) for k, v in methods.items()},
        "sales_by_day": [{"label": k, "amount": amount(v)} for k, v in by_day.items()],
        "sales_by_hour": [{"label": k, "amount": amount(v)} for k, v in by_hour.items()],
        "products": product_rank, "prep_avg_minutes": prep_average,
        "completed_station_tickets": len(prep_samples),
        "table_rotation": [{"table": k, "accounts_opened": v} for k, v in sorted(rotation.items())],
        "costs_available": False,
        "note": "Propinas excluidas. Productos por fecha de comanda y cobros por fecha de pago; no son importes equivalentes.",
    }


async def prep_alert_worker():
    """Aviso único por producto/comanda, registro persistente evita spam tras reinicios."""
    while True:
        try:
            await asyncio.sleep(45)
            outgoing = []
            with SessionLocal() as db:
                stamp = now()
                min_date = stamp - timedelta(hours=6)
                tickets = db.scalars(select(StationTicket).where(StationTicket.status != "ready",
                                      StationTicket.created_at >= min_date).limit(250)).all()
                for ticket in tickets:
                    batch = db.get(Batch, ticket.batch_id)
                    acc = db.get(Account, batch.account_id)
                    if not acc or acc.status not in ("open", "pending"):
                        continue
                    seconds = elapsed_seconds(batch.created_at, stamp)
                    limit = batch_limit(db, batch.id)
                    if seconds > limit * 60:
                        key = f"batch:{batch.id}:{ticket.station}"
                        if not db.scalar(select(PrepAlert.id).where(PrepAlert.alert_key == key)):
                            db.add(PrepAlert(alert_key=key))
                            try:
                                db.commit()
                                outgoing.append((ticket.station, f"Comanda #{batch.id} · {acc.name} excedió {limit} min", key))
                            except IntegrityError:
                                db.rollback()
                    items = db.scalars(select(OrderItem).where(OrderItem.batch_id == batch.id,
                                        OrderItem.station == ticket.station)).all()
                    for item in items:
                        if item.qty <= item.cancelled_qty or get_item_completion(db, item.id, ticket):
                            continue
                        meta = db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id == item.id))
                        product_limit = meta.prep_minutes if meta else 15
                        if seconds <= max(1, product_limit) * 60:
                            continue
                        key = f"item:{item.id}"
                        if db.scalar(select(PrepAlert.id).where(PrepAlert.alert_key == key)):
                            continue
                        db.add(PrepAlert(alert_key=key))
                        try:
                            db.commit()
                            outgoing.append((ticket.station, f"{item.name} · comanda #{batch.id} retrasada", key))
                        except IntegrityError:
                            db.rollback()
            for station, body, key in outgoing:
                payload = {"title": "El Mitote · Retraso de preparación",
                           "body": body, "url": "/?push=station", "tag": f"mitote-delay-{key}"}
                try:
                    await push_to(payload, role=station)
                    await fcm_to(payload, role=station)
                except Exception:
                    LOG.exception("No se pudo enviar aviso de demora %s", key)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Error en vigilancia de tiempos de preparación")
