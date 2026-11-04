"""Cronómetros mixtos por producto y por envío; registro histórico sin alterar tablas existentes."""
from datetime import timezone
from typing import Optional
from types import SimpleNamespace

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, select
from sqlalchemy.orm import Session


def register_prep(app, Base, db_session, auth, allow, audit, notify, now,
                  Product, Batch, OrderItem, StationTicket, Account, push_to, fcm_to):
    class PrepConfig(Base):
        __tablename__ = "prep_config"
        id = Column(Integer, primary_key=True)
        batch_limit_minutes = Column(Integer, nullable=False, default=25)
        warning_percent = Column(Integer, nullable=False, default=80)

    class ProductPrepLimit(Base):
        __tablename__ = "prep_product_limits"
        product_id = Column(Integer, ForeignKey("products.id"), primary_key=True)
        minutes = Column(Integer, nullable=False)

    class BatchPrepLimit(Base):
        __tablename__ = "prep_batch_limits"
        batch_id = Column(Integer, ForeignKey("batches.id"), primary_key=True)
        minutes = Column(Integer, nullable=False)

    class ItemPrepProgress(Base):
        __tablename__ = "prep_item_progress"
        order_item_id = Column(Integer, ForeignKey("order_items.id"), primary_key=True)
        ready_at = Column(DateTime(timezone=True), nullable=False)
        ready_by = Column(Integer, ForeignKey("users.id"), nullable=False)

    def as_utc(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def seconds_between(start, end):
        if not start:
            return 0
        return max(0, int((as_utc(end or now()) - as_utc(start)).total_seconds()))

    def get_conf(db):
        conf = db.get(PrepConfig, 1)
        return {
            "batch_limit_minutes": conf.batch_limit_minutes if conf else 25,
            "warning_percent": conf.warning_percent if conf else 80,
        }

    def product_minutes(db, product_id, fallback=15):
        row = db.get(ProductPrepLimit, product_id)
        return row.minutes if row else max(1, int(fallback))

    def snapshot_batch(db, batch):
        if db.get(BatchPrepLimit, batch.id) is None:
            db.add(BatchPrepLimit(batch_id=batch.id, minutes=get_conf(db)["batch_limit_minutes"]))

    def mark_station_ready(db, ticket, user):
        """Compatibilidad con el botón antiguo: todo el ticket se considera listo."""
        items = db.scalars(select(OrderItem).where(
            OrderItem.batch_id == ticket.batch_id, OrderItem.station == ticket.station
        )).all()
        ended = ticket.ready_at or now()
        for item in items:
            if item.qty <= item.cancelled_qty:
                continue
            if db.get(ItemPrepProgress, item.id) is None:
                db.add(ItemPrepProgress(order_item_id=item.id, ready_at=ended, ready_by=user.id))

    def enrich_tickets(db, result):
        """Un único conjunto de consultas para los estados; tiempos calculados por el servidor."""
        if not result:
            return result
        batch_ids = list({ticket["batch_id"] for ticket in result})
        order_item_ids = [item["id"] for ticket in result for item in ticket["items"]]
        snapshots = {x.batch_id: x.minutes for x in db.scalars(
            select(BatchPrepLimit).where(BatchPrepLimit.batch_id.in_(batch_ids))
        )}
        progress = {x.order_item_id: x for x in db.scalars(
            select(ItemPrepProgress).where(ItemPrepProgress.order_item_id.in_(order_item_ids))
        )} if order_item_ids else {}
        batches = {x.id: x for x in db.scalars(select(Batch).where(Batch.id.in_(batch_ids)))}
        station_rows = db.scalars(select(StationTicket).where(StationTicket.batch_id.in_(batch_ids))).all()
        by_batch = {}
        for row in station_rows:
            by_batch.setdefault(row.batch_id, []).append(row)
        conf = get_conf(db)
        for ticket in result:
            batch_id = ticket["batch_id"]
            batch = batches[batch_id]
            stations = by_batch.get(batch_id, [])
            completed = bool(stations) and all(t.status == "ready" for t in stations)
            end = max((as_utc(t.ready_at) for t in stations if t.ready_at), default=None) if completed else None
            ticket["prep_batch_minutes"] = snapshots.get(batch_id, 25)
            ticket["prep_batch_elapsed_seconds"] = seconds_between(batch.created_at, end)
            ticket["prep_batch_completed"] = completed
            ticket["prep_warning_percent"] = conf["warning_percent"]
            for item in ticket["items"]:
                state = progress.get(item["id"])
                ready_at = state.ready_at if state else None
                # Tickets antiguos que ya estaban listos antes de activar el módulo.
                if ticket["status"] == "ready" and ready_at is None:
                    ready_at = next((t.ready_at for t in stations if t.id == ticket["id"]), None)
                item["prep_ready"] = bool(ready_at)
                item["prep_elapsed_seconds"] = seconds_between(batch.created_at, ready_at)
        return result

    class ConfigIn(BaseModel):
        batch_limit_minutes: int = Field(ge=1, le=240)
        warning_percent: int = Field(ge=50, le=95)

    class ProductLimitIn(BaseModel):
        minutes: int = Field(ge=1, le=240)

    @app.get("/api/prep/config")
    def read_config(db: Session = Depends(db_session), user=Depends(auth)):
        allow(user, "owner", "admin")
        return get_conf(db)

    @app.put("/api/prep/config")
    async def set_config(data: ConfigIn, db: Session = Depends(db_session), user=Depends(auth)):
        allow(user, "owner")
        row = db.get(PrepConfig, 1)
        if row is None:
            row = PrepConfig(id=1)
            db.add(row)
        row.batch_limit_minutes = data.batch_limit_minutes
        row.warning_percent = data.warning_percent
        audit(db, user, "prep.config.update", **data.model_dump())
        db.commit()
        await notify()
        return get_conf(db)

    @app.put("/api/prep/products/{product_id}")
    async def set_product_limit(product_id: int, data: ProductLimitIn,
                                db: Session = Depends(db_session), user=Depends(auth)):
        allow(user, "owner", "admin")
        if db.get(Product, product_id) is None:
            raise HTTPException(404, "Producto inexistente")
        row = db.get(ProductPrepLimit, product_id)
        if row is None:
            row = ProductPrepLimit(product_id=product_id, minutes=data.minutes)
            db.add(row)
        else:
            row.minutes = data.minutes
        audit(db, user, "prep.product_limit.update", product_id=product_id, minutes=data.minutes)
        db.commit()
        await notify()
        return {"ok": True, "minutes": data.minutes}

    @app.post("/api/tickets/{ticket_id}/items/{item_id}/ready")
    async def mark_item_ready(ticket_id: int, item_id: int,
                              db: Session = Depends(db_session), user=Depends(auth)):
        allow(user, "owner", "admin", "kitchen", "bar")
        ticket = db.execute(select(StationTicket).where(
            StationTicket.id == ticket_id).with_for_update()).scalar_one_or_none()
        if ticket is None:
            raise HTTPException(404, "Comanda inexistente")
        if user.role in ("kitchen", "bar") and user.role != ticket.station:
            raise HTTPException(403, "No corresponde a tu estación")
        item = db.get(OrderItem, item_id)
        if (not item or item.batch_id != ticket.batch_id or item.station != ticket.station
                or item.qty <= item.cancelled_qty):
            raise HTTPException(404, "Producto inexistente en esta comanda")
        previous = db.get(ItemPrepProgress, item.id)
        if previous:
            return {"ok": True, "duplicate": True, "station_ready": ticket.status == "ready"}
        if ticket.status != "preparing":
            raise HTTPException(409, "Primero inicia la preparación de esta comanda")
        db.add(ItemPrepProgress(order_item_id=item.id, ready_at=now(), ready_by=user.id))
        db.flush()
        remaining = db.scalars(select(OrderItem).where(
            OrderItem.batch_id == ticket.batch_id, OrderItem.station == ticket.station
        )).all()
        station_ready = all(
            x.qty <= x.cancelled_qty or db.get(ItemPrepProgress, x.id) is not None
            for x in remaining
        )
        batch = db.get(Batch, ticket.batch_id)
        account = db.get(Account, batch.account_id)
        if station_ready:
            ticket.status = "ready"
            ticket.ready_at = now()
        audit(db, user, "prep.item.ready", ticket_id=ticket.id, item_id=item.id,
              station=ticket.station, station_ready=station_ready)
        db.commit()
        await notify()
        if station_ready:
            await push_to({"title": "El Mitote · Pedido listo",
                           "body": f'{"Cocina" if ticket.station == "kitchen" else "Barra"} terminó el pedido de {account.name}',
                           "url": f"/?push=accounts&account={account.id}",
                           "tag": f"mitote-ready-{ticket.id}"}, role="waiter", user_id=account.owner_id)
            await fcm_to({"title": "El Mitote · Pedido listo",
                          "body": f"Pedido listo · comanda #{batch.id}",
                          "url": "/?push=accounts"}, role="waiter", user_id=account.owner_id)
        return {"ok": True, "duplicate": False, "station_ready": station_ready}

    return SimpleNamespace(
        product_minutes=product_minutes,
        snapshot_batch=snapshot_batch,
        mark_station_ready=mark_station_ready,
        enrich_tickets=enrich_tickets,
    )
