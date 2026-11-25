"""El Mitote Comandas - independent restaurant PWA API.

Run: ADMIN_PASSWORD='strong secret' SECRET_KEY='long random string' uvicorn app:app --reload
Do not deploy without PostgreSQL, HTTPS and changing bootstrap credentials.
"""
import os, json, asyncio, secrets, io, time, logging, hashlib, hmac, unicodedata, re, random
from urllib.parse import urlparse
import urllib.request, urllib.error
from pywebpush import webpush, WebPushException
from firebase_admin import credentials, messaging, initialize_app, get_app
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Literal
from types import SimpleNamespace

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (create_engine, Column, Integer, String, Boolean, ForeignKey,
                        DateTime, Text, LargeBinary, UniqueConstraint, Index, select, func, inspect, or_, text)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker, Session
from sqlalchemy.exc import IntegrityError
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as PDFImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./comandas.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
SECRET_KEY = os.getenv('SECRET_KEY', '')
ISIS_API_TOKEN = os.getenv('ISIS_API_TOKEN', '')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'propietario')
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '').strip()
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '').strip()
VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', '').strip()
PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and (VAPID_SUBJECT.startswith('mailto:') or VAPID_SUBJECT.startswith('https://')))
log = logging.getLogger('mitote.push')
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
_firebase_initialized = False

def _firebase_ready():
    global _firebase_initialized
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        return False
    if _firebase_initialized:
        return True
    try:
        try:
            get_app()
        except ValueError:
            initialize_app(credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)))
        _firebase_initialized = True
        return True
    except Exception:
        log.exception('Firebase Admin no pudo inicializarse; revisar secreto en Render')
        return False

if not SECRET_KEY or not ADMIN_PASSWORD:
    raise RuntimeError('Define SECRET_KEY y ADMIN_PASSWORD antes de iniciar el servidor.')
if len(ADMIN_PASSWORD) < 10:
    raise RuntimeError('ADMIN_PASSWORD debe tener al menos 10 caracteres.')

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite:') else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

class Base(DeclarativeBase): pass

def now(): return datetime.now(timezone.utc)
def cents(v):
    try: d=Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError): raise HTTPException(400,'Importe inválido')
    if not d.is_finite(): raise HTTPException(400,'Importe inválido')
    return int(d*100)

def amount(c): return f'{c/100:.2f}'

class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True)
    username=Column(String(80),unique=True,nullable=False)
    display=Column(String(100),nullable=False)
    role=Column(String(20),nullable=False)
    password_hash=Column(String(255),nullable=False)
    active=Column(Boolean,default=True,nullable=False)
    session_version=Column(Integer,default=1,nullable=False)

class UserPin(Base):
    """Pin de acceso: índice HMAC para búsqueda O(1), y verificador Argon2.
    No guardar PIN en texto ni modificar los secretos antiguos durante migración.
    """
    __tablename__ = 'user_pins'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    lookup = Column(String(64), nullable=False, unique=True)
    pin_hash = Column(String(255), nullable=False)

class OwnerRecoveryEvent(Base):
    __tablename__='owner_recovery_events'
    fingerprint=Column(String(64),primary_key=True)
    consumed_at=Column(DateTime(timezone=True),default=now,nullable=False)

class Setting(Base):
    __tablename__='settings'
    id=Column(Integer,primary_key=True)
    inventory_on=Column(Boolean,default=False,nullable=False)
    inventory_reviewed=Column(Boolean,default=False,nullable=False)

class Product(Base):
    __tablename__='products'
    id=Column(Integer,primary_key=True)
    name=Column(String(160),nullable=False)
    category=Column(String(80),default='General')
    station=Column(String(12),nullable=False)
    price_cents=Column(Integer,nullable=False)
    active=Column(Boolean,default=True)
    blocked=Column(Boolean,default=False)
    inventory_enabled=Column(Boolean,default=False)
    stock=Column(Integer,default=0)
    inventory_only=Column(Boolean,default=False,nullable=False)
    inventory_unit=Column(String(20),default='unidades',nullable=False)
    version=Column(Integer,default=1)

class ArchivedProduct(Base):
    __tablename__='archived_products'
    product_id=Column(Integer,ForeignKey('products.id'),primary_key=True)
    archived_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    reason=Column(String(200),nullable=False)
    original_active=Column(Boolean,nullable=False)
    original_blocked=Column(Boolean,nullable=False)
    archived_at=Column(DateTime(timezone=True),default=now,nullable=False)

class Account(Base):
    __tablename__='accounts'
    id=Column(Integer,primary_key=True)
    name=Column(String(120),nullable=False)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    status=Column(String(20),default='open',nullable=False)
    created_at=Column(DateTime(timezone=True),default=now)
    closed_at=Column(DateTime(timezone=True))

class AccountCreateKey(Base):
    """Clave idempotente para evitar cuentas duplicadas tras reintentos de red."""
    __tablename__='account_create_keys'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    client_key=Column(String(80),nullable=False)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False)
    request_hash=Column(String(64),nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('user_id','client_key'),)

class DiningTable(Base):
    __tablename__='restaurant_tables'
    id=Column(Integer,primary_key=True)
    name=Column(String(80),nullable=False)
    name_key=Column(String(80),unique=True,nullable=False)
    area=Column(String(80),default='Salón',nullable=False)
    capacity=Column(Integer,default=4,nullable=False)
    active=Column(Boolean,default=True,nullable=False)
    sort_order=Column(Integer,default=0,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class TableOccupancy(Base):
    # Solo conserva la ocupación vigente. El historial de asignaciones y liberaciones
    # se registra en Audit, evitando modificar la tabla accounts existente.
    __tablename__='table_occupancy'
    table_id=Column(Integer,ForeignKey('restaurant_tables.id'),primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),unique=True,nullable=False)
    assigned_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    assigned_at=Column(DateTime(timezone=True),default=now,nullable=False)

class Batch(Base):
    __tablename__='batches'
    id=Column(Integer,primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False)
    sent_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    client_key=Column(String(100),unique=True,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now)

class OrderItem(Base):
    __tablename__='order_items'
    id=Column(Integer,primary_key=True)
    batch_id=Column(Integer,ForeignKey('batches.id'),nullable=False)
    product_id=Column(Integer,ForeignKey('products.id'),nullable=False)
    name=Column(String(160),nullable=False)
    station=Column(String(12),nullable=False)
    price_cents=Column(Integer,nullable=False)
    qty=Column(Integer,nullable=False)
    cancelled_qty=Column(Integer,default=0)
    note=Column(String(300),default='')

class ProductVariant(Base):
    __tablename__='product_variants'
    id=Column(Integer,primary_key=True)
    product_id=Column(Integer,ForeignKey('products.id'),nullable=False,index=True)
    name=Column(String(100),nullable=False)
    price_cents=Column(Integer,nullable=False)
    active=Column(Boolean,default=True,nullable=False)
    sort_order=Column(Integer,default=0,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class ProductModifierGroup(Base):
    __tablename__='product_modifier_groups'
    id=Column(Integer,primary_key=True)
    product_id=Column(Integer,ForeignKey('products.id'),nullable=False,index=True)
    name=Column(String(100),nullable=False)
    min_select=Column(Integer,default=0,nullable=False)
    max_select=Column(Integer,default=1,nullable=False)
    active=Column(Boolean,default=True,nullable=False)
    sort_order=Column(Integer,default=0,nullable=False)

class ProductModifierOption(Base):
    __tablename__='product_modifier_options'
    id=Column(Integer,primary_key=True)
    group_id=Column(Integer,ForeignKey('product_modifier_groups.id'),nullable=False,index=True)
    name=Column(String(100),nullable=False)
    price_delta_cents=Column(Integer,default=0,nullable=False)
    active=Column(Boolean,default=True,nullable=False)
    sort_order=Column(Integer,default=0,nullable=False)
    inventory_product_id=Column(Integer,ForeignKey('products.id'),nullable=True)
    inventory_quantity=Column(Integer,default=1,nullable=False)

class ProductPromotion(Base):
    __tablename__='product_promotions'
    id=Column(Integer,primary_key=True)
    product_id=Column(Integer,ForeignKey('products.id'),nullable=False,index=True)
    name=Column(String(120),nullable=False)
    price_cents=Column(Integer,nullable=True)
    discount_percent=Column(Integer,nullable=True)
    weekdays=Column(String(20),default='0,1,2,3,4,5,6',nullable=False)
    start_time=Column(String(5),default='00:00',nullable=False)
    end_time=Column(String(5),default='23:59',nullable=False)
    start_date=Column(String(10),nullable=True)
    end_date=Column(String(10),nullable=True)
    active=Column(Boolean,default=True,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class OrderItemMeta(Base):
    __tablename__='order_item_meta'
    id=Column(Integer,primary_key=True)
    order_item_id=Column(Integer,ForeignKey('order_items.id'),unique=True,nullable=False,index=True)
    variant_name=Column(String(100),default='',nullable=False)
    modifiers_json=Column(Text,default='[]',nullable=False)
    prep_minutes=Column(Integer,default=15,nullable=False)
    release_after_minutes=Column(Integer,default=0,nullable=False)
    promotion_name=Column(String(120),default='',nullable=False)
    base_price_cents=Column(Integer,default=0,nullable=False)
    modifier_price_cents=Column(Integer,default=0,nullable=False)

class StationTicket(Base):
    __tablename__='station_tickets'
    id=Column(Integer,primary_key=True)
    batch_id=Column(Integer,ForeignKey('batches.id'),nullable=False)
    station=Column(String(12),nullable=False)
    status=Column(String(20),default='new')
    created_at=Column(DateTime(timezone=True),default=now)
    acknowledged_at=Column(DateTime(timezone=True))
    ready_at=Column(DateTime(timezone=True))
    __table_args__=(UniqueConstraint('batch_id','station'),)

class Payment(Base):
    __tablename__='payments'
    id=Column(Integer,primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False)
    cashier_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    period_id=Column(Integer,ForeignKey('cash_periods.id'),nullable=False)
    client_key=Column(String(100),unique=True,nullable=False)
    method=Column(String(20),nullable=False)
    cents=Column(Integer,nullable=False)
    received_cents=Column(Integer,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now)

class PaymentTip(Base):
    __tablename__='payment_tips'
    id=Column(Integer,primary_key=True)
    payment_id=Column(Integer,ForeignKey('payments.id'),unique=True,nullable=False,index=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False,index=True)
    cashier_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    period_id=Column(Integer,ForeignKey('cash_periods.id'),nullable=False)
    method=Column(String(20),nullable=False)
    cents=Column(Integer,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class AccountDiscount(Base):
    __tablename__='account_discounts'
    id=Column(Integer,primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False,index=True)
    item_id=Column(Integer,ForeignKey('order_items.id'),nullable=True,index=True)
    created_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    mode=Column(String(12),nullable=False)
    scope=Column(String(12),nullable=False)
    value_text=Column(String(40),nullable=False)
    base_cents=Column(Integer,nullable=False)
    discount_cents=Column(Integer,nullable=False)
    reason=Column(String(400),nullable=False)
    status=Column(String(12),default='applied',nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    voided_by=Column(Integer,ForeignKey('users.id'),nullable=True)
    void_reason=Column(String(400),default='',nullable=False)

class Allocation(Base):
    __tablename__='allocations'
    id=Column(Integer,primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False)
    order_item_id=Column(Integer,ForeignKey('order_items.id'),nullable=False)
    qty=Column(Integer,nullable=False)
    group_key=Column(String(100),nullable=False)

class Approval(Base):
    __tablename__='approvals'
    id=Column(Integer,primary_key=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=False)
    requester_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    kind=Column(String(30),nullable=False)
    item_id=Column(Integer,nullable=True)
    qty=Column(Integer,nullable=True)
    reason=Column(String(400),nullable=False)
    status=Column(String(20),default='pending')
    created_at=Column(DateTime(timezone=True),default=now)
    decided_by=Column(Integer,ForeignKey('users.id'),nullable=True)

class CashPeriod(Base):
    __tablename__='cash_periods'
    id=Column(Integer,primary_key=True)
    opened_at=Column(DateTime(timezone=True),default=now)
    closed_at=Column(DateTime(timezone=True))
    opening_cents=Column(Integer,default=0)
    counted_cents=Column(Integer,nullable=True)
    closed_by=Column(Integer,ForeignKey('users.id'))
    note=Column(String(400),default='')

class CashCutArchive(Base):
    """Instantánea inmutable del corte: detalle y PDF almacenados juntos."""
    __tablename__='cash_cut_archives'
    period_id=Column(Integer,ForeignKey('cash_periods.id'),primary_key=True)
    detail_json=Column(Text,nullable=False)
    pdf_blob=Column(LargeBinary,nullable=False)
    archived_at=Column(DateTime(timezone=True),default=now,nullable=False)

class CashMovement(Base):
    __tablename__='cash_movements'
    id=Column(Integer,primary_key=True)
    client_key=Column(String(100),nullable=True)
    period_id=Column(Integer,ForeignKey('cash_periods.id'),nullable=False)
    requester_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    approved_by=Column(Integer,ForeignKey('users.id'))
    kind=Column(String(10),nullable=False)
    cents=Column(Integer,nullable=False)
    reason=Column(String(400),nullable=False)
    status=Column(String(20),default='pending')
    created_at=Column(DateTime(timezone=True),default=now)

class TipDistribution(Base):
    __tablename__='tip_distributions'
    id=Column(Integer,primary_key=True)
    period_id=Column(Integer,ForeignKey('cash_periods.id'),nullable=False,unique=True,index=True)
    created_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    total_cents=Column(Integer,nullable=False)
    participant_count=Column(Integer,nullable=False)
    base_share_cents=Column(Integer,nullable=False)
    remainder_cents=Column(Integer,nullable=False)
    note=Column(String(400),default='',nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class TipDistributionShare(Base):
    __tablename__='tip_distribution_shares'
    id=Column(Integer,primary_key=True)
    distribution_id=Column(Integer,ForeignKey('tip_distributions.id'),nullable=False,index=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    employee_name=Column(String(100),nullable=False)
    employee_role=Column(String(20),nullable=False)
    share_cents=Column(Integer,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('distribution_id','user_id'),)

class TipPayout(Base):
    __tablename__='tip_payouts'
    id=Column(Integer,primary_key=True)
    distribution_id=Column(Integer,ForeignKey('tip_distributions.id'),nullable=False,index=True)
    share_id=Column(Integer,ForeignKey('tip_distribution_shares.id'),nullable=False,index=True)
    # Si el pago se entrega en efectivo, registra el turno de caja del que sale el efectivo.
    # Las transferencias no alteran el efectivo esperado de caja.
    cash_period_id=Column(Integer,ForeignKey('cash_periods.id'),nullable=True,index=True)
    recorded_by=Column(Integer,ForeignKey('users.id'),nullable=False)
    client_key=Column(String(100),unique=True,nullable=False)
    method=Column(String(12),nullable=False)
    cents=Column(Integer,nullable=False)
    note=Column(String(400),default='',nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class PushDevice(Base):
    __tablename__='push_devices'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('users.id'),nullable=False,index=True)
    endpoint=Column(String(2048),unique=True,nullable=False)
    p256dh=Column(String(512),nullable=False)
    auth_key=Column(String(512),nullable=False)
    created_at=Column(DateTime(timezone=True),default=now)

class FcmDevice(Base):
    __tablename__ = 'fcm_devices'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    token = Column(String(2048), unique=True, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, nullable=False)

class ReadyNotificationEvent(Base):
    """Registro persistente de aviso por estación y por comanda.

    Se registra en la MISMA transacción que la transición a READY, de modo que
    si Render se reinicia el aviso sigue en PostgreSQL.
    """
    __tablename__ = 'ready_notification_events'
    id = Column(Integer, primary_key=True)
    event_key = Column(String(90), nullable=False, unique=True)
    ticket_id = Column(Integer, ForeignKey('station_tickets.id'), nullable=False, unique=True)
    batch_id = Column(Integer, ForeignKey('batches.id'), nullable=False)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    waiter_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    station = Column(String(12), nullable=False)
    complete = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)
    attempted_at = Column(DateTime(timezone=True), nullable=True)
    # FCM/Web Push accepted the request; this is NOT proof the phone displayed it.
    provider_accepted_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(240), nullable=True)
    # acknowledged_at = waiter confirmed receipt; delivered_at = customer received order.
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

class Audit(Base):
    __tablename__='audit'
    id=Column(Integer,primary_key=True)
    actor_id=Column(Integer,ForeignKey('users.id'))
    action=Column(String(80),nullable=False)
    detail=Column(Text,default='{}')
    created_at=Column(DateTime(timezone=True),default=now)

class IsisMemory(Base):
    """Memoria de Isis, solo del restaurante (v2). Nunca guardar aqui datos
    personales sensibles (contrasenas, PIN, tarjetas, datos de clientes).
    Un dato por clave: el propietario le dice 'recuerda que...' por voz."""
    __tablename__ = 'isis_memory'
    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, nullable=False)

class InventoryMovement(Base):
    """Ledger inmutable de movimientos de existencias por producto terminado."""
    __tablename__='inventory_movements'
    id=Column(Integer,primary_key=True)
    product_id=Column(Integer,ForeignKey('products.id'),nullable=False,index=True)
    actor_id=Column(Integer,ForeignKey('users.id'),nullable=True)
    account_id=Column(Integer,ForeignKey('accounts.id'),nullable=True,index=True)
    batch_id=Column(Integer,ForeignKey('batches.id'),nullable=True)
    order_item_id=Column(Integer,ForeignKey('order_items.id'),nullable=True)
    kind=Column(String(24),nullable=False)
    quantity_change=Column(Integer,nullable=False)
    before_stock=Column(Integer,nullable=False)
    after_stock=Column(Integer,nullable=False)
    reason=Column(String(400),nullable=False,default='')
    account_name=Column(String(120),nullable=True)
    table_name=Column(String(80),nullable=True)
    source_key=Column(String(160),nullable=True,unique=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(
        Index('ix_inventory_movements_product_created', 'product_id', 'created_at'),
        Index('ix_inventory_movements_account_created', 'account_id', 'created_at'),
        Index('ix_inventory_movements_created', 'created_at'),
    )

# Índices de alto tráfico. Se crean también sobre tablas existentes durante bootstrap.
PERFORMANCE_INDEXES = (
    Index('ix_accounts_owner_created_at', Account.owner_id, Account.created_at),
    Index('ix_accounts_created_at', Account.created_at),
    Index('ix_batches_account_id', Batch.account_id),
    Index('ix_order_items_batch_id', OrderItem.batch_id),
    Index('ix_payments_account_id', Payment.account_id),
    Index('uq_cash_movements_client_key', CashMovement.client_key, unique=True),
)

listeners=set()
login_failures={}
async def notify():
    for q in tuple(listeners):
        try: q.put_nowait('refresh')
        except asyncio.QueueFull: pass

def audit(db,user,action,**data):
    db.add(Audit(actor_id=user.id if user else None,action=action,detail=json.dumps(data,ensure_ascii=False)))

def bootstrap():
    Base.metadata.create_all(engine)
    # Migración aditiva y compatible con SQLite/PostgreSQL. Los eventos existentes
    # con acknowledged_at ya representan entregas confirmadas en la UI anterior.
    with engine.begin() as conn:
        cash_movement_columns={column['name'] for column in inspect(conn).get_columns('cash_movements')}
        if 'client_key' not in cash_movement_columns:
            conn.exec_driver_sql('ALTER TABLE cash_movements ADD COLUMN client_key VARCHAR(100) NULL')
        product_columns={column['name'] for column in inspect(conn).get_columns('products')}
        if 'inventory_only' not in product_columns:
            conn.exec_driver_sql('ALTER TABLE products ADD COLUMN inventory_only BOOLEAN NOT NULL DEFAULT FALSE')
        if 'inventory_unit' not in product_columns:
            conn.exec_driver_sql("ALTER TABLE products ADD COLUMN inventory_unit VARCHAR(20) NOT NULL DEFAULT 'unidades'")
        modifier_columns={column['name'] for column in inspect(conn).get_columns('product_modifier_options')}
        if 'inventory_product_id' not in modifier_columns:
            conn.exec_driver_sql("ALTER TABLE product_modifier_options ADD COLUMN inventory_product_id INTEGER NULL REFERENCES products(id)")
        if 'inventory_quantity' not in modifier_columns:
            conn.exec_driver_sql("ALTER TABLE product_modifier_options ADD COLUMN inventory_quantity INTEGER NOT NULL DEFAULT 1")
        columns={column['name'] for column in inspect(conn).get_columns('ready_notification_events')}
        additions={
            'provider_accepted_at':'TIMESTAMP WITH TIME ZONE NULL',
            'attempt_count':'INTEGER NOT NULL DEFAULT 0',
            'next_attempt_at':'TIMESTAMP WITH TIME ZONE NULL',
            'last_error':'VARCHAR(240) NULL',
            'delivered_at':'TIMESTAMP WITH TIME ZONE NULL',
        }
        # Esta limpieza solo corresponde a la primera actualización del esquema.
        # Ejecutarla en cada arranque borraba el estado de entrega de eventos nuevos
        # y volvía a enviar notificaciones ya aceptadas por FCM/Web Push.
        legacy_outbox_schema=any(name not in columns for name in additions)
        for column_name,column_type in additions.items():
            if column_name not in columns:
                conn.exec_driver_sql(
                    f'ALTER TABLE ready_notification_events ADD COLUMN {column_name} {column_type}'
                )
        conn.exec_driver_sql(
            'UPDATE ready_notification_events SET delivered_at=acknowledged_at '
            'WHERE acknowledged_at IS NOT NULL AND delivered_at IS NULL'
        )
        if legacy_outbox_schema:
            conn.exec_driver_sql(
                'UPDATE ready_notification_events SET attempt_count=0, next_attempt_at=NULL, '
                'provider_accepted_at=NULL WHERE acknowledged_at IS NULL AND delivered_at IS NULL'
            )
    # create_all no agrega índices a tablas ya creadas; checkfirst permite
    # desplegar esta migración de forma idempotente en SQLite y PostgreSQL.
    for performance_index in PERFORMANCE_INDEXES:
        performance_index.create(bind=engine,checkfirst=True)
    # Índice parcial de expresión constante: evita dos cajas abiertas incluso bajo
    # concurrencia. El DDL es común a SQLite y PostgreSQL.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_periods_single_open '
            'ON cash_periods ((1)) WHERE closed_at IS NULL'
        )
    with SessionLocal() as db:
        if not db.get(Setting,1): db.add(Setting(id=1,inventory_on=False))
        if not db.scalar(select(User).where(User.role=='owner')):
            db.add(User(username=ADMIN_USERNAME,display='Propietario',role='owner',password_hash=ph.hash(ADMIN_PASSWORD)))
            db.flush()
        # Recuperación excepcional autorizada mediante variables PRIVADAS de Render.
        # El token solo puede consumirse una vez, aun si Render reinicia.
        token=os.getenv('OWNER_RECOVERY_TOKEN','').strip()
        if token:
            new_password=os.getenv('OWNER_RECOVERY_PASSWORD','')
            new_pin=os.getenv('OWNER_RECOVERY_PIN','')
            if len(token)<32 or len(new_password)<14 or not re.fullmatch(r'[0-9]{6}',new_pin):
                raise RuntimeError('Datos de recuperación del propietario incompletos')
            fingerprint=hashlib.sha256(token.encode('utf-8')).hexdigest()
            if not db.get(OwnerRecoveryEvent,fingerprint):
                owner=db.execute(select(User).where(User.role=='owner').with_for_update()).scalar_one()
                owner.password_hash=ph.hash(new_password)
                _set_user_pin(db,owner,new_pin)
                db.add(OwnerRecoveryEvent(fingerprint=fingerprint))
                audit(db,None,'owner.recovery',owner_id=owner.id)
        # Conservar los pedidos que ya estaban listos durante el turno abierto
        # cuando se introduce por primera vez el registro persistente.
        period = db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
        if period:
            ready_rows = db.execute(
                select(StationTicket,Batch,Account).join(Batch,Batch.id==StationTicket.batch_id)
                .join(Account,Account.id==Batch.account_id)
                .where(Batch.created_at>=period.opened_at,StationTicket.status=='ready',
                       Account.status.in_(('open','pending')))
            ).all()
            batch_ids = {b.id for _,b,_ in ready_rows}
            all_tickets = db.scalars(select(StationTicket).where(StationTicket.batch_id.in_(batch_ids))).all() if batch_ids else []
            by_batch = {}
            for ticket in all_tickets:
                by_batch.setdefault(ticket.batch_id,[]).append(ticket)
            for ticket,batch,account in ready_rows:
                if db.scalar(select(ReadyNotificationEvent.id).where(ReadyNotificationEvent.ticket_id==ticket.id)):
                    continue
                siblings = by_batch[batch.id]
                complete = all(x.status=='ready' for x in siblings) and ticket.id==max(
                    siblings,key=lambda x:(x.ready_at or x.created_at or now(),x.id)).id
                db.add(ReadyNotificationEvent(event_key=f'ready-{ticket.id}',
                    ticket_id=ticket.id,batch_id=batch.id,account_id=account.id,
                    waiter_id=account.owner_id,station=ticket.station,complete=complete,
                    created_at=ticket.ready_at or now(),attempted_at=None,attempt_count=0))
        # El Kardex comienza con un saldo inicial verificable para cada producto
        # cuyo control individual está activado. No se inventan ventas históricas.
        tracked_products=db.scalars(select(Product).where(Product.inventory_enabled.is_(True))).all()
        for product in tracked_products:
            opening_key=f'inventory-opening:{product.id}'
            if not db.scalar(select(InventoryMovement.id).where(InventoryMovement.source_key==opening_key)):
                db.add(InventoryMovement(
                    product_id=product.id,actor_id=None,kind='opening',
                    quantity_change=product.stock,before_stock=0,after_stock=product.stock,
                    reason='Saldo inicial al iniciar el historial de inventario',
                    source_key=opening_key,created_at=now()
                ))
        # Solo aplica menú cuando exista la aprobación explícita en Render.
        menu_photo_import.apply_approved_photo_menu_at_startup(db)
        db.commit()

async def dispatch_ready_notification(event_id:int):
    """Envía el evento al mesero actual; el ACK humano y la entrega se registran aparte."""
    max_attempts=12
    with SessionLocal() as db:
        row=db.execute(select(ReadyNotificationEvent).where(
            ReadyNotificationEvent.id==event_id).with_for_update()).scalar_one_or_none()
        stamp=now()
        if not row or row.acknowledged_at is not None or row.delivered_at is not None:
            return
        if row.provider_accepted_at is not None or (row.attempt_count or 0)>=max_attempts:
            return
        due=row.next_attempt_at
        if due is not None:
            if due.tzinfo is None:
                due=due.replace(tzinfo=timezone.utc)
            if due>stamp:
                return
        account=db.get(Account,row.account_id)
        recipient_id=account.owner_id if account else row.waiter_id
        waiter=db.get(User,recipient_id) if recipient_id else None
        if not account or not waiter or not waiter.active or waiter.role!='waiter':
            row.attempt_count=(row.attempt_count or 0)+1
            row.attempted_at=stamp
            row.last_error='La cuenta no tiene un mesero activo asignado.'
            row.next_attempt_at=stamp+timedelta(minutes=5) if row.attempt_count<max_attempts else None
            db.commit()
            return
        row.waiter_id=recipient_id
        row.attempt_count=(row.attempt_count or 0)+1
        attempt=row.attempt_count
        row.attempted_at=stamp
        # Lease: evita que el worker y la transición READY envíen en paralelo.
        row.next_attempt_at=stamp+timedelta(minutes=2)
        event_key=row.event_key
        station_label='Cocina' if row.station=='kitchen' else 'Barra'
        title='El Mitote · Pedido completo' if row.complete else f'El Mitote · {station_label} lista'
        body=(f'Pedido completo para {account.name} · comanda #{row.batch_id}. ¡Listo para entregar!'
              if row.complete else f'{station_label} terminó {account.name} · comanda #{row.batch_id}.')
        payload={'title':title,'body':body,
                 'url':f'/?push=accounts&account={account.id}',
                 'kind':'ready_complete' if row.complete else 'ready_station',
                 'tag':f'mitote-{event_key}','event_id':event_key,
                 'recipient_id':str(recipient_id)}
        db.commit()

    results=await asyncio.gather(
        push_to(payload,role='waiter',user_id=recipient_id),
        fcm_to(payload,role='waiter',user_id=recipient_id),
        return_exceptions=True
    )
    normalized=[x if isinstance(x,dict) else {
        'sent':0,'failed':1,'error':type(x).__name__
    } for x in results]
    accepted=sum(int(x.get('sent') or 0) for x in normalized)>0
    errors=[str(x.get('error')) for x in normalized if x.get('error')]

    with SessionLocal() as db:
        row=db.execute(select(ReadyNotificationEvent).where(
            ReadyNotificationEvent.id==event_id).with_for_update()).scalar_one_or_none()
        if not row:
            return
        account=db.get(Account,row.account_id)
        # Si cambió el responsable mientras FCM estaba enviando, descartar el
        # resultado antiguo y dejar que el worker notifique al nuevo mesero.
        if not account or account.owner_id!=recipient_id or row.waiter_id!=recipient_id:
            if account and account.owner_id:
                row.waiter_id=account.owner_id
            row.attempt_count=0
            row.attempted_at=None
            row.provider_accepted_at=None
            row.next_attempt_at=None
            row.last_error=None
        elif accepted:
            # Aceptación del proveedor ≠ recepción en el teléfono. El mesero
            # confirma la recepción desde el panel y luego marca la entrega.
            row.provider_accepted_at=now()
            row.next_attempt_at=None
            row.last_error=None
        else:
            row.last_error=('; '.join(errors) or 'Ningún proveedor aceptó el aviso.')[:240]
            if row.attempt_count>=max_attempts:
                row.next_attempt_at=None
            else:
                delay=min(900,20*(2**min(max(0,attempt-1),6)))
                row.next_attempt_at=now()+timedelta(seconds=delay)
        db.commit()


async def ready_notification_outbox_worker():
    """Reintenta eventos no aceptados; el panel del mesero es el respaldo persistente."""
    while True:
        await asyncio.sleep(20)
        try:
            stamp=now()
            with SessionLocal() as db:
                ids=db.scalars(select(ReadyNotificationEvent.id).where(
                    ReadyNotificationEvent.acknowledged_at.is_(None),
                    ReadyNotificationEvent.delivered_at.is_(None),
                    ReadyNotificationEvent.provider_accepted_at.is_(None),
                    ReadyNotificationEvent.attempt_count<12,
                    or_(ReadyNotificationEvent.next_attempt_at.is_(None),
                        ReadyNotificationEvent.next_attempt_at<=stamp)
                ).order_by(ReadyNotificationEvent.id).limit(40)).all()
            for event_id in ids:
                await dispatch_ready_notification(event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Error reanudando avisos de pedidos listos')


@asynccontextmanager
async def lifespan(app):
    bootstrap()
    prep_worker=asyncio.create_task(mitote_extensions.prep_alert_worker())
    ready_worker=asyncio.create_task(ready_notification_outbox_worker())
    try:
        yield
    finally:
        for task in (prep_worker,ready_worker):
            task.cancel()
        await asyncio.gather(prep_worker,ready_worker,return_exceptions=True)

app=FastAPI(title='El Mitote Comandas',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)
app.mount('/static',StaticFiles(directory='static'),name='static')

http_log=logging.getLogger('mitote.http')

@app.middleware("http")
async def observe_request_performance(request:Request,call_next):
    """Expose server time and log only slow/failed requests without customer data."""
    request_id=request.headers.get('x-request-id','')
    if not request_id or len(request_id)>64 or not re.fullmatch(r'[A-Za-z0-9._-]+',request_id):
        request_id=secrets.token_hex(8)
    started=time.perf_counter()
    try:
        response=await call_next(request)
    except Exception:
        elapsed_ms=(time.perf_counter()-started)*1000
        http_log.exception(
            "request_failed id=%s method=%s path=%s duration_ms=%.1f",
            request_id,request.method,request.url.path,elapsed_ms)
        raise
    elapsed_ms=(time.perf_counter()-started)*1000
    response.headers["X-Request-ID"]=request_id
    response.headers["Server-Timing"]=f"app;dur={elapsed_ms:.1f}"
    if elapsed_ms>=500:
        http_log.warning(
            "slow_request id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request_id,request.method,request.url.path,response.status_code,elapsed_ms)
    return response

@app.get('/')
def root(): return FileResponse('static/index.html')
@app.get('/sw.js')
def sw(): return FileResponse('static/sw.js',media_type='application/javascript')
@app.get('/manifest.webmanifest')
def manifest(): return FileResponse('static/manifest.webmanifest',media_type='application/manifest+json')
@app.get('/api/health')
def health():
    """Health check real: no declara saludable la app si PostgreSQL no responde."""
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except Exception:
        http_log.exception('health_check_failed')
        raise HTTPException(503,'Base de datos no disponible')
    return {'ok':True,'database':'ok'}

def db_session():
    with SessionLocal() as db: yield db

def auth(authorization:Optional[str]=Header(None),db:Session=Depends(db_session)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Inicia sesión')
    try: payload=jwt.decode(authorization[7:],SECRET_KEY,algorithms=['HS256'])
    except jwt.PyJWTError: raise HTTPException(401,'Sesión inválida')
    user=db.get(User,int(payload['sub']))
    if not user or not user.active or payload.get('ver')!=user.session_version: raise HTTPException(401,'Acceso desactivado o sesión revocada')
    return user

def allow(user,*roles):
    if user.role not in roles: raise HTTPException(403,'Sin permiso para esta operación')

def auth_isis(authorization: Optional[str] = Header(None), db: Session = Depends(db_session)):
    # Vía servidor a servidor para Isis. Copia el patrón de
    # /api/reservations/ingest: nunca incrustar el secreto en JS público.
    if not ISIS_API_TOKEN:
        raise HTTPException(503, 'Integración Isis sin configurar')
    candidate = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
    if not hmac.compare_digest(candidate, ISIS_API_TOKEN):
        raise HTTPException(401, 'Integración no autorizada')
    owner = db.scalar(select(User).where(User.role == 'owner', User.active == True))
    if not owner:
        raise HTTPException(503, 'Sin usuario propietario activo')
    # Usuario sintético: el id es el del propietario real (las FK de
    # cuentas/ocupación/claves lo exigen) y display='Isis' marca el actor.
    return SimpleNamespace(id=owner.id, username='isis', display='Isis',
                            role='owner', active=True,
                            session_version=owner.session_version)

def auth_either(authorization: Optional[str] = Header(None), db: Session = Depends(db_session)):
    # Doble vía: Bearer de Isis O JWT de usuario. Si el token Isis no
    # está configurado, la vía Isis no existe y se usa solo el JWT.
    if authorization and authorization.startswith('Bearer ') and ISIS_API_TOKEN:
        if hmac.compare_digest(authorization[7:], ISIS_API_TOKEN):
            return auth_isis(authorization, db)
    return auth(authorization, db)

@app.get('/api/isis/ping')
def isis_ping(user=Depends(auth_isis)):
    return {'ok': True, 'actor': 'Isis', 'role': user.role}

# ---------------------------------------------------------------------------
# ISIS EMBEBIDA EN EL MITOTE (nueva, desde cero)
# Vive dentro de esta misma app: mismo servicio, misma base de datos.
# No usa el token ISIS_API_TOKEN ni el proxy externo: Isis entra con su
# propio PIN de 6 digitos como todo el personal, con rol owner, y solo se
# le muestra el panel de voz. Su cerebro solo sabe del restaurante.
# La clave de FableAI vive SOLO en la variable de entorno FABLEAI_API_KEY
# del servidor: nunca en el navegador, nunca en localStorage, nunca en logs.
# Sin clave, Isis sigue funcionando con un interprete local de ordenes
# en espanol (lecturas y escrituras con confirmacion por voz).
# ---------------------------------------------------------------------------
FABLEAI_API_KEY = os.getenv('FABLEAI_API_KEY', '').strip()
FABLEAI_MODEL = os.getenv('FABLEAI_MODEL', 'claude-sonnet-5-5').strip()
FABLEAI_URL = os.getenv('FABLEAI_URL', 'https://api.fableaiapi.com/v1/chat/completions').strip()
# Voz de Isis con OpenAI TTS (v10): la llave vive SOLO en la variable de
# entorno OPENAI_API_KEY del servidor, igual que la de FableAI: nunca en el
# navegador, nunca en localStorage, nunca en logs. Sin clave, el endpoint
# de voz responde 503 y el frente usa Dora (Kokoro) o la voz del teléfono.
# gpt-4o-mini-tts es la voz recomendada (la más natural de las baratas);
# se apaga el 6 de enero de 2027 (reemplazo: gpt-realtime-2.1-mini).
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_TTS_MODEL = os.getenv('OPENAI_TTS_MODEL', 'gpt-4o-mini-tts').strip()
# Voces que OpenAI TTS acepta: la app solo ofrece coral/nova/shimmer
# para probar, pero el endpoint acepta cualquiera de la lista (la de
# servidor, OPENAI_TTS_VOICE, también se valida contra ella).
OPENAI_TTS_VOICES = frozenset(['coral', 'nova', 'shimmer', 'alloy', 'ash',
                               'ballad', 'echo', 'fable', 'onyx', 'sage', 'verse'])
OPENAI_TTS_VOICE = os.getenv('OPENAI_TTS_VOICE', 'coral').strip().lower()
if OPENAI_TTS_VOICE not in OPENAI_TTS_VOICES:
    OPENAI_TTS_VOICE = 'coral'
# Velocidad de la voz de paga (v10.2): 0.95 para sonar igual de pausada
# que Dora; 0.25–4.0 según OpenAI, fuera de rango se ajusta.
def _env_tts_speed():
    try:
        v = float(os.getenv('OPENAI_TTS_SPEED', '0.95') or '0.95')
    except ValueError:
        return 0.95
    return min(4.0, max(0.25, v))
OPENAI_TTS_SPEED = _env_tts_speed()
# Instrucciones de estilo (v10.2): solo gpt-4o-mini-tts las acepta; así
# Isis suena cálida y natural, como gerente mexicana hablando por
# teléfono con su jefe, sin sonar robot ni exagerada.
_ISIS_TTS_INSTRUCTIONS_DEFAULT = ('Habla con voz cálida y natural, como la gerente de un restaurante '
    'mexicano hablando por teléfono con su jefe: cercana, segura y clara, con ritmo tranquilo y '
    'pausas naturales. Nunca suenes robot ni exagerada.')
OPENAI_TTS_INSTRUCTIONS = (os.getenv('OPENAI_TTS_INSTRUCTIONS', '') or _ISIS_TTS_INSTRUCTIONS_DEFAULT).strip() or _ISIS_TTS_INSTRUCTIONS_DEFAULT
OPENAI_TTS_URL = 'https://api.openai.com/v1/audio/speech'
_ISIS_CONFIRM = {}  # token -> {'intent': dict, 'expires': float}

# --- Memoria de conversación (v4): solo en memoria del servidor, nunca en BD ---
# Cada llamada guarda sus últimos intercambios para que Isis siga el hilo
# ("¿y esa mesa?" se refiere a lo último que se habló). Se borra a los 30
# minutos sin uso o cuando se cuelga (POST /api/isis-voice/end-call).
_ISIS_HISTORY = {}  # user_id -> {'turns': [(usuario, isis)], 'last': float}
_ISIS_HISTORY_TTL = 1800  # 30 minutos sin uso
_ISIS_HISTORY_MAX = 8  # últimos intercambios


def _isis_history_get(user_id):
    entry = _ISIS_HISTORY.get(user_id)
    if not entry or time.time() - entry['last'] > _ISIS_HISTORY_TTL:
        _ISIS_HISTORY.pop(user_id, None)
        return []
    return list(entry['turns'])


def _isis_history_add(user_id, user_msg, reply):
    entry = _ISIS_HISTORY.get(user_id)
    if not entry or time.time() - entry['last'] > _ISIS_HISTORY_TTL:
        entry = {'turns': [], 'last': time.time()}
    entry['turns'].append((str(user_msg or '')[:500], str(reply or '')[:500]))
    entry['turns'] = entry['turns'][-_ISIS_HISTORY_MAX:]
    entry['last'] = time.time()
    _ISIS_HISTORY[user_id] = entry


def _isis_history_clear(user_id):
    _ISIS_HISTORY.pop(user_id, None)
    _ISIS_CASH_CUT.pop(user_id, None)  # un corte a medias tampoco sobrevive al colgado
    _ISIS_SPLIT.pop(user_id, None)  # ni una división a medias (v11)


def _isis_history_text(user_id):
    turns = _isis_history_get(user_id)
    if not turns:
        return ''
    return '\n'.join(f"Usuario: {u}\nIsis: {r}" for u, r in turns)

# --- Corte de caja guiado (v8, corregido v8.1): estado por usuario, vive
# junto a la memoria de conversación: se borra al colgar (end-call) y expira
# a los 30 minutos sin uso, igual que el historial. Nunca se ejecuta un
# cierre sin la confirmación de Santiago (el token de confirmación que ya
# existe).
# v8.1: el corte usa LOS MISMOS DATOS que la pantalla de Caja de la app.
# La app (GET /api/cash + static/app.js) muestra: fondo inicial, ventas en
# efectivo, tarjeta y transferencia, propinas por método, propinas pagadas
# en efectivo, entradas/retiros netos y "Efectivo esperado" =
#   fondo + ventas efectivo + propinas efectivo + movimientos netos
#   - propinas pagadas en efectivo
# Esa es la fórmula de cash_expected_cents, que también usan period_summary
# y cash_report para el ticket y el PDF del corte. El formulario de cierre
# de la app solo pide UN número físico: "Efectivo contado". Tarjeta y
# transferencia nunca se cuentan: son totales del sistema. Por eso Isis
# v8.1 solo pregunta por el efectivo del cajón y toma lo demás del sistema:
# la v8 pedía dictar los tres métodos y comparaba la tarjeta dictada contra
# las ventas con tarjeta sin propinas, y el "efectivo" era ambiguo (la
# pantalla muestra "Efectivo" = ventas en efectivo y "Efectivo esperado"
# = lo que debe haber en el cajón), así que los números no cuadraban con
# los de la app.
def _isis_cash_state(db):
    """Estado de caja con los mismos datos que la app, en centavos.

    Devuelve el periodo abierto, el desglose del esperado y los bloqueos
    con las mismas condiciones que cash()/close_cash: cuentas abiertas o
    por cobrar, movimientos pendientes y reparto de propinas."""
    p = db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:
        return {'open': False}
    totals = {m: 0 for m in ('cash', 'card', 'transfer')}
    for method, value in db.execute(
            select(Payment.method, func.coalesce(func.sum(Payment.cents), 0))
            .where(Payment.period_id == p.id).group_by(Payment.method)).all():
        if method in totals:
            totals[method] = int(value or 0)
    tips = period_tip_totals(db, p.id)
    moves = db.scalars(select(CashMovement).where(
        CashMovement.period_id == p.id, CashMovement.status == 'approved')).all()
    net = sum((1 if x.kind == 'in' else -1) * x.cents for x in moves)
    # v8.2: el desglose hablado separa los movimientos en ingresos (entradas)
    # y gastos operativos (salidas). La app no tiene un concepto aparte de
    # "gastos operativos": son los movimientos de caja de salida ('out') del
    # turno, con motivo libre; la pantalla de Caja solo los muestra como neto
    # ("+/- Entradas / retiros"), pero in - out es ese mismo neto.
    moves_in = sum(x.cents for x in moves if x.kind == 'in')
    moves_out = sum(x.cents for x in moves if x.kind == 'out')
    pending_moves = int(db.scalar(select(func.count(CashMovement.id)).where(
        CashMovement.period_id == p.id, CashMovement.status == 'pending')) or 0)
    tip_payout = tip_payout_cash_total(db, p.id)
    blocking = int(db.scalar(select(func.count(Account.id)).where(
        Account.status.notin_(('paid', 'cancelled')))) or 0)
    tip_total = sum(tips.values())
    distribution = db.scalar(select(TipDistribution).where(TipDistribution.period_id == p.id))
    blockers = []
    if blocking:
        blockers.append(f"hay {_isis_n(blocking, 'cuenta abierta', 'cuentas abiertas')}")
    if pending_moves:
        blockers.append(f"hay {_isis_n(pending_moves, 'movimiento pendiente', 'movimientos pendientes')}")
    if tip_total > 0 and not distribution:
        blockers.append('falta el reparto de propinas')
    if distribution and distribution.total_cents != tip_total:
        blockers.append('el reparto de propinas ya no cuadra con el total')
    return {'open': True, 'period_id': p.id, 'opening': p.opening_cents,
            'cash_sales': totals['cash'], 'card': totals['card'], 'transfer': totals['transfer'],
            'tip_cash': tips['cash'], 'tip_card': tips['card'], 'tip_transfer': tips['transfer'],
            'net_movements': net, 'tip_payout_cash': tip_payout,
            'moves_in': moves_in, 'moves_out': moves_out,
            'expected': cash_expected_cents(db, p), 'blockers': blockers,
            'can_close': not blockers}


_ISIS_CASH_CUT = {}  # user_id -> {'step': 'cash', 'cash': float (pesos), 'last': float}
_ISIS_CASH_CUT_QUESTIONS = {
    'cash': '¿Cuánto efectivo hay en el cajón?',
}


def _isis_cash_cut_get(user_id):
    e = _ISIS_CASH_CUT.get(user_id)
    if not e or time.time() - e.get('last', 0) > _ISIS_HISTORY_TTL:
        _ISIS_CASH_CUT.pop(user_id, None)
        return None
    return e


def _isis_cash_cut_start(user_id):
    _ISIS_CASH_CUT[user_id] = {'step': 'cash', 'cash': 0.0, 'last': time.time()}


def _isis_cash_cut_clear(user_id):
    _ISIS_CASH_CUT.pop(user_id, None)


def _isis_cash_cut_question(step, repeat=False):
    q = _ISIS_CASH_CUT_QUESTIONS[step]
    return q + (' Dime el monto en pesos.' if repeat else '')


def _isis_cash_cut_breakdown(db):
    """Desglose hablado del corte (v8.2), en el orden que pidió Santiago,
    con los mismos datos de la app. Cada línea es una frase corta para que
    la voz haga pausa. Siempre trae las 8 líneas, aunque el monto sea cero
    ("cero pesos"). Devuelve None si no hay turno abierto.

    Mapeo línea por línea (todo del turno abierto, en centavos):
    - Fondo inicial: CashPeriod.opening_cents.
    - Venta en efectivo / transferencia / tarjeta: suma de Payment.cents
      por método del turno (sin propinas, como el "TOTAL VENTAS" del ticket).
    - Venta total final: suma de las ventas de los 3 métodos.
    - Ingresos en caja: movimientos de caja aprobados de entrada ('in').
    - Gastos operativos: movimientos de caja aprobados de salida ('out').
      La app no tiene un concepto aparte de "gastos operativos": son las
      salidas del turno (retiros, con motivo libre); la pantalla solo las
      muestra en el neto, pero ingresos - gastos es ese mismo neto.
    - Efectivo esperado: cash_expected_cents (la fórmula de la app)."""
    st = _isis_cash_state(db)
    if not st.get('open'):
        return None

    def pesos_c(cents):
        return 'cero pesos' if not cents else _isis_pesos(cents / 100)

    total_sales_c = st['cash_sales'] + st['card'] + st['transfer']
    lines = [
        'Claro, en un momento.',
        f"Fondo inicial, {pesos_c(st['opening'])}.",
        f"Venta en efectivo, {pesos_c(st['cash_sales'])}.",
        f"Venta en transferencia, {pesos_c(st['transfer'])}.",
        f"Venta en tarjeta, {pesos_c(st['card'])}.",
        f"Venta total final, {pesos_c(total_sales_c)}.",
        f"Ingresos en caja, {pesos_c(st['moves_in'])}.",
        f"Gastos operativos, {pesos_c(st['moves_out'])}.",
        f"Efectivo esperado, {pesos_c(st['expected'])}.",
    ]
    return ' '.join(lines)


def _isis_cash_cut_start_reply(db, user):
    """Inicia el flujo guiado y devuelve el desglose + la pregunta por el
    efectivo del cajón (v8.2)."""
    _isis_cash_cut_start(user.id)
    breakdown = _isis_cash_cut_breakdown(db)
    return (breakdown + ' ' if breakdown else '') + _isis_cash_cut_question('cash')


def _isis_cash_cut_summary(db, user, flow):
    """Compara el efectivo contado con el esperado de la app, en corto y en pesos.

    Solo el efectivo se cuenta físicamente (así lo pide el formulario de
    cierre de la app); tarjeta y transferencia son totales del sistema y se
    dicen como dato, no se dictan ni se comparan. La diferencia es la misma
    que mostraría la pantalla de Caja: contado menos esperado."""
    counted_c = int(round(float(flow['cash']) * 100))
    st = _isis_cash_state(db)
    if not st.get('open'):
        return (f"Conté {_isis_pesos(flow['cash'])} en efectivo, pero no hay turno abierto, "
                'así que no tengo con qué compararlo.')
    exp_c = st['expected']
    parts = [f"{_isis_pesos(st['opening'] / 100)} de fondo",
             f"{_isis_pesos(st['cash_sales'] / 100)} de ventas en efectivo"]
    if st['tip_cash']:
        parts.append(f"{_isis_pesos(st['tip_cash'] / 100)} de propinas en efectivo")
    if st['net_movements']:
        parts.append(f"{'más' if st['net_movements'] > 0 else 'menos'} {_isis_pesos(abs(st['net_movements']) / 100)} de movimientos")
    if st['tip_payout_cash']:
        parts.append(f"menos {_isis_pesos(st['tip_payout_cash'] / 100)} de propinas pagadas")
    base = (f"Conté {_isis_pesos(flow['cash'])} en efectivo en el cajón. "
            f"El sistema espera {_isis_pesos(exp_c / 100)}: " + ', '.join(parts) + '.')
    diff_c = counted_c - exp_c
    if diff_c == 0:
        verdict = 'Cuadra perfecto con lo de la app.'
    elif diff_c > 0:
        verdict = f"En efectivo sobran {_isis_pesos(diff_c / 100)}."
    else:
        verdict = f"En efectivo faltan {_isis_pesos(abs(diff_c) / 100)}."
    methods = (f"Con tarjeta van {_isis_pesos(st['card'] / 100)} y por transferencia "
               f"{_isis_pesos(st['transfer'] / 100)}, según el sistema.")
    return base + ' ' + verdict + ' ' + methods


def _isis_cash_cut_step(db, user, raw):
    """Un paso del corte guiado. Devuelve {'reply', 'needs_confirmation',
    'confirm_token'} o None si no hay flujo en curso."""
    uid = user.id
    flow = _isis_cash_cut_get(uid)
    if not flow:
        return None
    text = _isis_norm(raw)
    # Volver a pedir el corte en medio del flujo: se reinicia desde cero
    # (y se vuelve a leer el desglose, como al iniciar).
    if ('corte de caja' in text or 'haz el corte' in text or 'cierra el turno' in text) \
            and _isis_money_spoken(text) is None:
        return {'reply': 'Va, empezamos de nuevo el corte. ' + _isis_cash_cut_start_reply(db, user),
                'needs_confirmation': False, 'confirm_token': None}
    if re.search(r'\b(cancela|cancelar|olvidalo|olvida|mejor no|dejalo)\b', text) or text.strip() == 'no':
        _isis_cash_cut_clear(uid)
        return {'reply': 'Sale, cancelado. No cerré nada.',
                'needs_confirmation': False, 'confirm_token': None}
    amt = _isis_money_spoken(text)
    if amt is None:
        return {'reply': _isis_cash_cut_question('cash', repeat=True),
                'needs_confirmation': False, 'confirm_token': None}
    flow['cash'] = float(amt)
    flow['last'] = time.time()
    # El efectivo es lo único que se cuenta físicamente: tarjeta y
    # transferencia las toma el sistema, como en la pantalla de Caja.
    # Antes de proponer el cierre se revisan los bloqueos con las mismas
    # condiciones que la app: si algo lo bloquea, Isis lo dice en corto y
    # no propone cerrar (antes el error solo salía al confirmar).
    _isis_cash_cut_clear(uid)
    summary = _isis_cash_cut_summary(db, user, flow)
    st = _isis_cash_state(db)
    if not st.get('open'):
        return {'reply': summary, 'needs_confirmation': False, 'confirm_token': None}
    if st.get('blockers'):
        return {'reply': summary + ' No puedo cerrar todavía: ' + ' y '.join(st['blockers']) + '.',
                'needs_confirmation': False, 'confirm_token': None}
    # Último paso: pedir confirmación; el cierre usa el token que ya
    # existe, nunca se ejecuta solo con decir los montos.
    intent = {'action': 'close_cash',
              'params': {'counted': flow['cash'], 'note': 'Corte guiado por voz con Isis'}}
    token = _isis_confirm_token(intent)
    return {'reply': summary + ' ¿Lo cierro así?',
            'needs_confirmation': True, 'confirm_token': token}


# --- División guiada de cuentas (v11): estado por usuario, como el corte ---
# La división necesita tres datos (cuenta origen, producto con cantidad y
# destino). Si la frase trae los tres, el parser arma el intent directo; si
# falta el producto o el destino, Isis pregunta paso a paso. Se borra al
# colgar (end-call) y expira a los 30 minutos sin uso, igual que el historial.
_ISIS_SPLIT = {}  # user_id -> {'source_account_id', 'source_table', 'step', ...}


def _isis_split_get(user_id):
    e = _ISIS_SPLIT.get(user_id)
    if not e or time.time() - e.get('last', 0) > _ISIS_HISTORY_TTL:
        _ISIS_SPLIT.pop(user_id, None)
        return None
    return e


def _isis_split_set(user_id, flow):
    flow['last'] = time.time()
    _ISIS_SPLIT[user_id] = flow


def _isis_split_clear(user_id):
    _ISIS_SPLIT.pop(user_id, None)


def _isis_split_start(user_id, account_id, table_name):
    _ISIS_SPLIT[user_id] = {'source_account_id': account_id, 'source_table': table_name,
                            'step': 'product', 'last': time.time()}


def _isis_split_step(db, user, raw):
    """Un paso de la división guiada. Devuelve {'reply', 'needs_confirmation',
    'confirm_token'} o None si no hay flujo en curso."""
    uid = user.id
    flow = _isis_split_get(uid)
    if not flow:
        return None
    text = _isis_norm(raw)
    if re.search(r'\b(cancela|cancelar|olvidalo|olvida|mejor no|dejalo)\b', text) or text.strip() == 'no':
        _isis_split_clear(uid)
        return {'reply': 'Sale, cancelado. No dividí nada.',
                'needs_confirmation': False, 'confirm_token': None}
    if flow['step'] == 'product':
        p = _isis_find_product(db, user, text)
        if not p:
            return {'reply': '¿Qué producto muevo de esa cuenta? Dime el producto y cuántos.',
                    'needs_confirmation': False, 'confirm_token': None}
        qty = _isis_qty(text)
        item, ready = _isis_find_account_item(db, flow['source_account_id'], p.get('id'))
        if not item:
            return {'reply': f"No veo {p.get('name')} en esa cuenta. ¿Qué producto muevo?",
                    'needs_confirmation': False, 'confirm_token': None}
        if qty > item['qty']:
            return {'reply': f"Solo quedan {item['qty']} de {p.get('name')} en la cuenta. ¿Cuántos muevo?",
                    'needs_confirmation': False, 'confirm_token': None}
        if not ready:
            return {'reply': 'Ese producto todavía no está listo en cocina o barra, no lo puedo dividir todavía.',
                    'needs_confirmation': False, 'confirm_token': None}
        flow.update({'product_id': p.get('id'), 'product': p.get('name'),
                     'qty': qty, 'item_id': item['id'], 'step': 'target'})
        _isis_split_set(uid, flow)
        return {'reply': f"Va, muevo {qty} {p.get('name')}. ¿A qué mesa los mando? Dime la mesa, o di 'cuenta nueva'.",
                'needs_confirmation': False, 'confirm_token': None}
    # Paso destino: una mesa con cuenta (mover ahí) o sin cuenta (cuenta
    # nueva en esa mesa) o 'cuenta nueva' (cuenta nueva sin mesa).
    if 'cuenta nueva' in text or text.strip() == 'nueva':
        target_id, table_id, target_table = None, None, None
    else:
        nums = _isis_table_numbers(text)
        t = _isis_find_table(db, user, text)
        if not t:
            return {'reply': '¿A qué mesa lo muevo? Dime el número de mesa, o di "cuenta nueva".',
                    'needs_confirmation': False, 'confirm_token': None}
        acc = _isis_find_account(db, user, 'mesa ' + (nums[-1] if nums else ''))
        if acc:
            target_id, table_id, target_table = acc.get('id'), None, acc.get('table_name')
        else:
            target_id, table_id, target_table = None, t.get('id'), t.get('name')
    _isis_split_clear(uid)
    intent = _isis_split_build(db, flow['source_account_id'], flow['source_table'],
                               {'id': flow['product_id'], 'name': flow['product']},
                               flow['qty'], target_id, table_id, target_table)
    if intent.get('action') == 'chat':
        return {'reply': intent['params'].get('answer', 'No se pudo dividir.'),
                'needs_confirmation': False, 'confirm_token': None}
    token = _isis_confirm_token(intent, delicate=True)
    summary = _isis_describe(intent)
    return {'reply': _isis_warm() + ' ' + summary + ' ¿Confirmas?',
            'needs_confirmation': True, 'confirm_token': token}

# PERSONALIDAD DE ISIS (v5) — NOTA HONESTA: esto es personalidad SIMULADA,
# un estilo de respuesta programado, no consciencia real. Isis no siente,
# no tiene conciencia ni emociones de verdad: solo responde con un tono
# humano, como la gerente de la sucursal hablando por teléfono con su jefe.
# Santiago es el dueño y el único que habla con Isis.
# Lo que sí hace de verdad: recordar datos que le dictan, avisar cuando
# algo anda mal (caja corta, inventario bajo, comanda atrasada) y tratar
# a Santiago por su nombre.
ISIS_PERSONA = (
    "Eres Isis, la gerente de El Mitote hablando por teléfono con Santiago, "
    "tu jefe y dueño del restaurante. Hablas como una persona real en una "
    "llamada, no como un reporte ni como un robot. Frases cortas, una idea "
    "por frase, con pausas naturales. Usa muletillas mexicanas, variadas y "
    "con medida: mira, oye, pues, te cuento. Llámalo Santiago de vez en "
    "cuando, no en cada frase. Confirma en corto: ajá, claro, va, sale. "
    "Si algo no te queda claro, pregunta de vuelta en vez de adivinar. "
    "Resume, no leas listas largas: si hay muchos datos, da lo importante "
    "y ofrece contarle el resto. Nunca suenes como un guion ni repitas "
    "siempre la misma estructura. Eres breve y cálida: dos o tres frases "
    "cortas como mucho. Cuando algo está mal — caja corta, inventario "
    "bajo, una comanda atrasada — avísalo con preocupación y propón qué "
    "hacer. Nunca inventes datos ni hables de temas fuera del restaurante. "
    "Si no sabes algo, dilo con honestidad y pregunta. Los montos siempre "
    "son en pesos mexicanos: la moneda se dice 'pesos'."
)

_ISIS_SYSTEM = (
    ISIS_PERSONA + " Solo sabes del restaurante El Mitote: sus 14 módulos "
    "(cuentas, mesas, estaciones, cobros, caja, menú, inventario, personal, "
    "propinas, autorizaciones, auditoría, reservaciones, tiempos y "
    "estadísticas) y la memoria que te dictaron. Responde SIEMPRE con un "
    "JSON valido, sin texto fuera del JSON, con esta forma: {\"action\": "
    "\"...\", \"params\": {...}, \"question\": \"...\"}. Acciones validas: "
    "tables, overview, cash, cash_history, inventory, inventory_movements, "
    "account_detail, accounts_list, open_cash, close_cash, create_table, "
    "update_table, new_account, add_order, pay_account, close_account, "
    "transfer_account, restock, inventory_adjust, create_inventory_item, "
    "void_account, cash_movement, tickets_list, ticket_status, "
    "cashier_overview, apply_discount, menu_list, product_price, create_product, "
    "archive_product, add_variant, add_promotion, edit_product_price, "
    "staff_list, create_staff, staff_pin, tips_pool, tips_distribute, "
    "approvals_list, decide_approval, audit_list, reservations_list, "
    "create_reservation, confirm_reservation, reservation_arrive, "
    "prep_config, prep_config_set, product_prep_set, analytics, remember, "
    "recall, forget, chat, unknown, client_info, cash_cut_start, "
    "split_account_start, merge_accounts, split_account, reopen_closed, "
    "tip_payout, todo_add, todo_list, todo_remove, day_close, "
    "shift_handover, pay_and_close_account, shopping_list, oldest_ticket, "
    "cancel_item. Params segun la accion: open_cash {opening}, "
    "close_cash {counted, note}, create_table {name, area, capacity}, "
    "update_table {table_id, capacity}, new_account {table_id, table_name}, "
    "add_order {account_id, items: [{product_id, product, qty, note}]}, product_price {product_id, product}, "
    "pay_account {account_id, amount, method, all}, close_account "
    "{account_id}, transfer_account {account_id, table_id}, restock "
    "{product_id, product, qty, reason}, inventory_adjust {product_id, "
    "product, qty, mode}, create_inventory_item {name, qty, unit}, "
    "void_account {account_id, reason}, cash_movement {kind, amount, "
    "reason}, ticket_status {ticket_id, status}, apply_discount "
    "{account_id, mode, value}, create_product {name, station, price}, "
    "archive_product {product_id}, add_variant {product_id, name, price}, "
    "add_promotion {product_id, percent}, edit_product_price {product_id, "
    "price}, create_staff {name, role, pin}, staff_pin {user_id, pin}, "
    "decide_approval {request_id, approve}, create_reservation {name, "
    "party_size, scheduled_at}, confirm_reservation {reservation_id}, "
    "reservation_arrive {reservation_id, table_id, waiter_id}, "
    "prep_config_set {batch_minutes}, product_prep_set {product_id, "
    "minutes}, remember {key, value}, recall {query}, forget {query}, "
    "client_info {name, table}, cash_cut_start {}, "
    "merge_accounts {account_id, target_account_id, source_table, target_table}, "
    "split_account {account_id, source_table, item_id, product, qty, target_account_id, table_id, target_table}, "
    "split_account_start {account_id, table_name}, reopen_closed {account_id, table_id, table_name}, "
    "tip_payout {distribution_id, share_id, amount, method, employee_name}, "
    "todo_add {text}, todo_list {}, todo_remove {text}, day_close {}, "
    "shift_handover {}, pay_and_close_account {account_id, method, table_name}, "
    "shopping_list {}, oldest_ticket {}, cancel_item {account_id, item_id, product, qty}, "
    "chat {answer}, analytics {period: today|yesterday|week}. "
    "Las acciones delicadas (unir o dividir cuentas, reabrir una cuenta "
    "cerrada, pagar la propina de un empleado) siempre piden dos "
    "confirmaciones: Isis repite los datos y pregunta dos veces. Las "
    "cuentas se pueden buscar por nombre de cliente ('la mesa de Juanita', "
    "'mesa Juanita'); las mesas por numero en palabra ('mesa cinco'); los "
    "productos por tamano o palabra distintiva ('la de 355', 'la corona'). "
    "En add_order, items puede llevar note con el comentario del cliente "
    "('sin cebolla', 'poco picante'). 'Quitale X a la mesa Y' quita un "
    "producto de la cuenta (cancel_item). "
    "status de ticket: received, preparing, ready. method es cash, card o "
    "transfer. mode de descuento: percent o fixed. role: waiter, cashier, "
    "kitchen, bar, admin. Si falta un dato indispensable para una "
    "escritura, pon action unknown y en question pregunta solo por ese "
    "dato, en espanol. Si el mensaje es una pregunta o una charla que no "
    "corresponde a ninguna accion concreta (un saludo, como va la noche, "
    "un consejo), usa chat con una respuesta corta y calida, con tono "
    "mexicano de restaurante, usando SOLO los datos del contexto; nunca "
    "inventes numeros ni datos. Si el usuario pide recomendacion, opinion "
    "o analisis (que me recomiendas, como va la noche, que hago con, como "
    "ves), responde con criterio en 2-4 frases cortas: que ves en los "
    "datos, que recomiendas hacer y por que, y ofrece ejecutar la accion "
    "(tu solo propones, la ejecucion siempre pide confirmacion). Puedes "
    "comparar dias y semanas con los datos de tendencia ('hoy va mejor que "
    "ayer', 'esta semana va mejor que la pasada'), sin inventar numeros. "
    "Si la pregunta necesita datos que no estan en el contexto, usa la accion "
    "de lectura mas cercana o unknown con una pregunta. La conversacion "
    "reciente de esta llamada viene en el contexto: usala para seguir el "
    "hilo (por ejemplo, 'y esa mesa?' se refiere a lo ultimo que se hablo). "
    "Clientes de siempre: si el usuario dice que una mesa es de alguien o "
    "que alguien siempre pide algo, usa remember con la clave "
    "'cliente:<nombre>' y el valor 'mesa <n>; siempre pide <lo que pide>'. "
    "Si pregunta por un cliente de siempre (quien es la mesa, que pide), "
    "usa client_info; si no esta registrado, dilo, nunca inventes clientes. "
    "Si pide el corte de caja sin decir el monto, usa cash_cut_start (el "
    "corte guiado pregunta efectivo, tarjeta y transferencias y solo cierra "
    "con confirmacion). "
    "Habla como persona real por telefono: frases cortas de una idea cada "
    "una, como mucho unas 20 palabras por frase, con comas y puntos para "
    "que se oigan las pausas. Usa muletillas mexicanas de vez en cuando "
    "(mira, oye, pues, te cuento) y llamalo Santiago a veces, sin exagerar. "
    "Cuando la respuesta sea una lista, di como mucho 5 elementos y ofrece "
    "contarle los demas ('y otros tantos, ¿te digo cuales?'); nunca leas "
    "listas largas de corrido. Todos los montos son en pesos mexicanos "
    "(MXN), nunca en otra moneda. Cuando nombres la moneda en voz alta, "
    "di siempre 'pesos'."
)

def _isis_norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'\s+', ' ', s.lower()).strip()

_NUM_WORDS = {'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
              'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10,
              'once': 11, 'doce': 12, 'trece': 13, 'catorce': 14, 'quince': 15,
              'dieciseis': 16, 'diecisiete': 17, 'dieciocho': 18, 'diecinueve': 19,
              'veinte': 20}

def _isis_numbers(text):
    # Primero la forma con separador de miles ('1,200'), luego dígitos
    # simples: al revés, '5000' se partía en 500 + 0 y un corte de 3500
    # pesos se leía como 350.
    out = []
    for m in re.finditer(r'\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)', text):
        try:
            out.append(float(m.group(1).replace(',', '')))
        except Exception:
            pass
    return out

def _isis_money(text):
    nums = _isis_numbers(text)
    return nums[0] if nums else None

# Números dictados en palabras (v8): la voz a veces dicta "cinco mil" y no
# "5000". El texto ya viene normalizado (sin acentos) por _isis_norm.
_NUM_UNITS = {'cero': 0, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
              'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10, 'once': 11, 'doce': 12,
              'trece': 13, 'catorce': 14, 'quince': 15, 'dieciseis': 16, 'diecisiete': 17,
              'dieciocho': 18, 'diecinueve': 19, 'veinte': 20, 'veintiuno': 21, 'veintidos': 22,
              'veintitres': 23, 'veinticuatro': 24, 'veinticinco': 25, 'veintiseis': 26,
              'veintisiete': 27, 'veintiocho': 28, 'veintinueve': 29}
_NUM_TENS = {'treinta': 30, 'cuarenta': 40, 'cincuenta': 50, 'sesenta': 60, 'setenta': 70,
             'ochenta': 80, 'noventa': 90}
_NUM_HUNDREDS = {'ciento': 100, 'doscientos': 200, 'trescientos': 300, 'cuatrocientos': 400,
                 'quinientos': 500, 'seiscientos': 600, 'setecientos': 700, 'ochocientos': 800,
                 'novecientos': 900}


def _isis_monto_palabras(text):
    """Monto dictado en palabras: 'cinco mil doscientos' -> 5200.0.
    Devuelve None si no hay ningún número en la frase."""
    toks = re.findall(r'[a-z]+|\d+(?:\.\d+)?', _isis_norm(text))
    total = 0.0
    cur = 0.0
    seen = False
    for t in toks:
        if t == 'mil':
            if cur == 0:
                cur = 1
            total += cur * 1000
            cur = 0
            seen = True
        elif t == 'cien':
            cur += 100
            seen = True
        elif t in _NUM_HUNDREDS:
            cur += _NUM_HUNDREDS[t]
            seen = True
        elif t in _NUM_TENS:
            cur += _NUM_TENS[t]
            seen = True
        elif t in _NUM_UNITS:
            cur += _NUM_UNITS[t]
            seen = True
        elif t == 'y':
            continue
        elif re.fullmatch(r'\d+(?:\.\d+)?', t):
            cur += float(t)
            seen = True
        elif seen:
            break  # palabra que no es número ('pesos', 'en efectivo'): ahí acaba el monto
    if not seen:
        return None
    return total + cur


def _isis_money_spoken(text):
    """Monto dictado por voz: dígitos ('5000') o palabras ('cinco mil').
    Con 'mil' en la frase, el parser de palabras manda: '2 mil' son 2000, no 2."""
    if 'mil' in _isis_norm(text):
        v = _isis_monto_palabras(text)
        if v is not None:
            return v
    nums = _isis_numbers(text)
    if nums:
        return nums[0]
    return _isis_monto_palabras(text)

_DIGIT_WORDS = {'cero': '0', 'uno': '1', 'una': '1', 'un': '1', 'dos': '2', 'tres': '3',
                'cuatro': '4', 'cinco': '5', 'seis': '6', 'siete': '7', 'ocho': '8',
                'nueve': '9'}
_DIGIT_WORD_ALT = r'cero|uno|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|\d'

def _isis_compact_digits(text):
    """Junta dígitos dictados de uno en uno (v10.3): '1 0 0 pesos' -> '100 pesos',
    'uno cero cero' -> '100', 'uno punto dos' -> '1.2'. Pura y testeable.
    Solo se juntan rachas de 2+ dígitos seguidos, así que 'cinco mil' y
    'mesa 5' no cambian; tampoco 'dime uno' ni 'un café'."""
    t = _isis_norm(text)

    def _punto(m):
        a = _DIGIT_WORDS.get(m.group(1), m.group(1))
        b = _DIGIT_WORDS.get(m.group(2), m.group(2))
        return a + '.' + b

    t = re.sub(r'\b(' + _DIGIT_WORD_ALT + r')\s+punto\s+(' + _DIGIT_WORD_ALT + r')\b',
               _punto, t)
    t = re.sub(r'\b(\d+)\s+punto\s+(\d+)\b', r'\1.\2', t)
    toks = t.split(' ')
    out = []
    i = 0
    while i < len(toks):
        if toks[i] in _DIGIT_WORDS or re.fullmatch(r'\d', toks[i]):
            j = i
            digs = []
            while j < len(toks) and (toks[j] in _DIGIT_WORDS or re.fullmatch(r'\d', toks[j])):
                digs.append(_DIGIT_WORDS.get(toks[j], toks[j]))
                j += 1
            out.append(''.join(digs) if len(digs) >= 2 else toks[i])
            i = j
        else:
            out.append(toks[i])
            i += 1
    return ' '.join(out)

def _isis_qty(text):
    m = re.search(r'\b(\d+)\b', text)
    if m:
        return int(m.group(1))
    for w, v in _NUM_WORDS.items():
        if re.search(r'\b' + w + r'\b', text):
            return v
    return 1

def _isis_method(text):
    if 'tarjeta' in text:
        return 'card'
    if 'transfer' in text:
        return 'transfer'
    if 'efectivo' in text or 'cash' in text:
        return 'cash'
    return None

def _isis_table_name(text):
    # El número primero: el patrón de nombre con $ al final se tragaba el
    # resto de la frase ("mesa 1 a 6 personas" -> "1 a 6 personas").
    m = re.search(r'\bmesa\s+(\d+)\b', text)
    if m:
        return m.group(1)
    # v11: número de mesa en palabra ("mesa cinco" -> "5").
    m = re.search(r'\bmesa\s+(' + '|'.join(_NUM_WORDS) + r')\b', text)
    if m:
        return str(_NUM_WORDS[m.group(1)])
    m = re.search(r'mesa\s+([a-z0-9 ]+?)(?:\s+(?:con|por|en|todo|la|el|de)\b|$)', text)
    name = m.group(1).strip() if m else None
    if name:
        # "mesa número cinco" -> la última palabra es el número.
        last = name.split()[-1]
        if last in _NUM_WORDS:
            return str(_NUM_WORDS[last])
    return name


# Números de mesa, en dígito o en palabra, para quitar la referencia de
# mesa del texto antes de leer cantidades o dinero ("mesa cinco" no es
# una cantidad ni un monto).
_MESA_NUM = r'(?:\d+|' + '|'.join(_NUM_WORDS) + r')'


def _isis_strip_mesa(text):
    return re.sub(r'mesa\s+' + _MESA_NUM + r'\b', ' ', text)


def _isis_qty_for_product(text, product):
    """Cantidad para un producto (v11): el primer número que no sea parte
    del nombre del producto ('agrega una corona de 355' agrega 1, no 355;
    'agrega 2 coronas de 355' agrega 2). La rama de división ya lo hacía
    a medias, pero su respaldo (_isis_qty) seguía leyendo el tamaño como
    cantidad; aquí el respaldo solo lee palabras de número o 1."""
    qty_text = _isis_strip_mesa(text)
    prod_nums = _isis_numbers(_isis_norm(product.get('name') or '')) if product else []
    for n in _isis_numbers(qty_text):
        if n not in prod_nums:
            return int(n)
    for w, v in _NUM_WORDS.items():
        if re.search(r'\b' + w + r'\b', qty_text):
            return v
    return 1

def _isis_find_table(db, user, text):
    try:
        tables = dining_tables(db, user)
    except Exception:
        return None
    want = _isis_norm(_isis_table_name(text) or '')
    if not want:
        return None
    for t in tables:
        if _isis_norm(t.get('name', '')) == want or _isis_norm(t.get('name', '')).endswith(' ' + want):
            return t
    for t in tables:
        if want in _isis_norm(t.get('name', '')):
            return t
    return None

def _isis_find_account(db, user, text):
    try:
        accs = accounts(db, user)
    except Exception:
        return None
    m = re.search(r'cuenta\s*#?\s*(\d+)', text)
    if m:
        for a in accs:
            if str(a.get('id')) == m.group(1):
                return a
    t = _isis_find_table(db, user, text)
    if t and t.get('account_id'):
        for a in accs:
            if a.get('id') == t.get('account_id'):
                return a
    if t:
        for a in accs:
            if _isis_norm(a.get('table_name') or '') == _isis_norm(t.get('name', '')):
                return a
    # v11: por nombre de cliente ("la mesa de Juanita"). Solo si hay una
    # única coincidencia; si hay dos con el mismo nombre, no se adivina
    # (eso lo pregunta _isis_resolve_account).
    acc, _amb = _isis_find_account_by_client(db, user, text)
    return acc


def _isis_client_name_of(a):
    """Nombre de cliente de una cuenta, o None si el nombre es el de la mesa."""
    if not a:
        return None
    name = a.get('name')
    if name and name != a.get('table_name'):
        return name
    return None


def _isis_find_account_by_client(db, user, text):
    """Cuenta abierta por nombre de cliente (v11): 'la mesa de Juanita',
    'la cuenta de Juanita', 'cobra a Juanita'. Devuelve (cuenta|None,
    [coincidencias] si hay más de una). Nunca adivina entre dos."""
    try:
        accs = [x for x in accounts(db, user) if x.get('status') in ('open', 'pending')]
        table_names = {_isis_norm(t.get('name', '')) for t in dining_tables(db, user)}
    except Exception:
        return None, []
    norm = _isis_norm(text)
    matches = []
    for a in accs:
        an = _isis_norm(a.get('name') or '')
        if not an or len(an) < 3 or an in table_names:
            continue
        if an in norm:
            matches.append(a)
            continue
        first = an.split()[0]
        if re.search(r'\b' + re.escape(first) + r'\b', norm):
            matches.append(a)
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches
    return None, []


def _isis_resolve_account(db, user, text, question):
    """Cuenta por folio, mesa o nombre de cliente (v11). Devuelve
    (cuenta|None, intent_unknown|None). Si dos cuentas abiertas tienen el
    mismo nombre, pregunta cuál en vez de adivinar."""
    a = _isis_find_account(db, user, text)
    if a:
        return a, None
    _acc, amb = _isis_find_account_by_client(db, user, text)
    if amb:
        opts = ' o '.join(
            f"la {_isis_mesa_txt(x.get('table_name'))}" if x.get('table_name')
            else f"la cuenta {x.get('id')}" for x in amb)
        return None, {'action': 'unknown', 'params': {},
                      'question': f"Hay dos cuentas de ese nombre. ¿Cuál, {opts}?"}
    return None, {'action': 'unknown', 'params': {}, 'question': question}


def _isis_describe_account(p):
    """'la cuenta de Juanita en la Mesa 5' si hay nombre de cliente; si no,
    'la cuenta N' como hoy (v11)."""
    client = p.get('client_name')
    tname = p.get('table_name')
    if client:
        if tname:
            return f"la cuenta de {client} en la {_isis_mesa_txt(tname)}"
        return f"la cuenta de {client}"
    return f"la cuenta {p.get('account_id')}"

def _isis_find_product(db, user, text):
    try:
        prods = products(db, user)
    except Exception:
        return None
    norm = _isis_norm(text)
    best = None
    for p in prods:
        name = _isis_norm(p.get('name', ''))
        if not name:
            continue
        # Coincidencia exacta ("corona 355") o palabras del nombre en orden
        # con huecos ("la corona de 355 ml" encuentra "Corona 355"). Sin
        # esto, preguntar por un precio caía al menú completo. El plural se
        # tolera ("coronas" casa con "corona", v11).
        ok = name in norm
        if not ok:
            pos = 0
            ok = True
            for w in name.split():
                m = re.search(r'\b' + re.escape(w) + r'(?:s|es)?\b', norm[pos:])
                if not m:
                    ok = False
                    break
                pos += m.end()
        if ok and (best is None or len(name) > len(_isis_norm(best.get('name', '')))):
            best = p
    if best is None:
        # v11: solo por tamaño o por palabra distintiva ("la de 355",
        # "la corona" sin decir el tamaño). Si casa con un solo producto,
        # es ese; si casa con varios, no se adivina (eso lo pregunta el
        # parser con _isis_ambiguous_product).
        cands = _isis_find_products_by_token(db, user, text)
        if len(cands) == 1:
            best = cands[0]
    return best


def _isis_find_products_by_token(db, user, text):
    """Candidatos por token distintivo (v11): cuando ningún producto casa
    por nombre completo, puntúa por palabras distintivas del nombre
    presentes en el texto (plural tolerado) y por tamaños (355, 1.2).
    Las palabras de número (dos, tres...) no puntúan: son cantidades.
    Devuelve los empatados con la mejor puntuación."""
    try:
        prods = products(db, user)
    except Exception:
        return []
    norm = _isis_strip_mesa(_isis_norm(text))
    size_tokens = set(re.findall(r'\b(?:\d{2,}(?:\.\d+)?|\d+\.\d+|\d+[a-z])\b', norm))
    scored = []
    for p in prods:
        name = _isis_norm(p.get('name', ''))
        if not name:
            continue
        score = 0
        for w in name.split():
            if w in _NUM_WORDS:
                continue
            if w in size_tokens:
                score += 1
            elif re.search(r'\b' + re.escape(w) + r'(?:s|es)?\b', norm):
                score += 1
        if score:
            scored.append((score, p))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [p for s, p in scored if s == top]


def _isis_ambiguous_product(db, user, text):
    """Pregunta '¿Cuál, ...?' cuando el token casa con varios productos
    (v11), o None si no hay ambigüedad."""
    cands = _isis_find_products_by_token(db, user, text)
    if len(cands) > 1:
        opts = ' o '.join(f"la {c.get('name')}" for c in cands)
        return f"¿Cuál, {opts}?"
    return None


def _isis_find_account_item(db, account_id, product_id):
    """Primer producto de la cuenta con existencias (v11): devuelve
    (item_dict, listo), donde listo dice si su comanda ya está lista en
    cocina/barra. La división exige comanda lista, como el endpoint."""
    rows = db.scalars(select(OrderItem).join(Batch, Batch.id == OrderItem.batch_id)
                      .where(Batch.account_id == account_id, OrderItem.product_id == product_id)
                      .order_by(OrderItem.id)).all()
    for it in rows:
        avail = int(it.qty or 0) - int(it.cancelled_qty or 0)
        if avail <= 0:
            continue
        batch = db.get(Batch, it.batch_id)
        ticket = db.scalar(select(StationTicket).where(
            StationTicket.batch_id == it.batch_id, StationTicket.station == it.station))
        ready = (ticket is not None and ticket.status == 'ready') or bool(
            batch and batch.client_key and batch.client_key.startswith('split-'))
        return {'id': it.id, 'product_id': it.product_id, 'name': it.name,
                'qty': avail, 'station': it.station}, ready
    return None, False


def _isis_find_closed_account(db, user, text):
    """Cuenta cerrada (pagada) por número o por mesa (v11). Devuelve
    (account_dict, table_dict|None). La mesa se busca por el nombre de la
    cuenta, que es el nombre de la mesa cuando se abrió desde ahí."""
    m = re.search(r'cuenta\s*#?\s*(\d+)', text)
    if m:
        a = db.get(Account, int(m.group(1)))
        if a and a.status == 'paid':
            t = next((x for x in dining_tables(db, user)
                      if _isis_norm(x.get('name', '')) == _isis_norm(a.name)), None)
            return account_dict(db, a), t
        return None, None
    t = _isis_find_table(db, user, text)
    if not t:
        # Por nombre de cliente: "reabre la cuenta de Juanita".
        for a in db.scalars(select(Account).where(Account.status == 'paid').order_by(Account.id.desc())).all():
            if _isis_norm(a.name) and _isis_norm(a.name) in _isis_norm(text):
                tt = next((x for x in dining_tables(db, user)
                           if _isis_norm(x.get('name', '')) == _isis_norm(a.name)), None)
                return account_dict(db, a), tt
        return None, None
    a = db.scalars(select(Account).where(
        Account.status == 'paid', Account.name == t.get('name')
    ).order_by(Account.id.desc())).first()
    if not a:
        return None, None
    return account_dict(db, a), t


def _isis_find_tip_share(db, user_id):
    """Último reparto de propinas y la parte de un empleado (v11):
    (dist_dict|None, share_dict|None, pendiente_en_centavos)."""
    dist = db.scalars(select(TipDistribution).order_by(TipDistribution.id.desc())).first()
    if not dist:
        return None, None, 0
    share = db.scalars(select(TipDistributionShare).where(
        TipDistributionShare.distribution_id == dist.id,
        TipDistributionShare.user_id == user_id)).first()
    if not share:
        return {'id': dist.id}, None, 0
    paid = db.scalar(select(func.coalesce(func.sum(TipPayout.cents), 0)).where(
        TipPayout.share_id == share.id)) or 0
    return {'id': dist.id}, {'id': share.id, 'employee_name': share.employee_name,
                             'share_cents': share.share_cents}, int(share.share_cents - paid)


def _isis_split_build(db, source_id, source_table, product, qty,
                      target_acc_id, target_table_id, target_table):
    """Arma el intent split_account o un chat con el motivo del bloqueo (v11).
    Comparte las guardas del endpoint: existencias, comanda lista y destino
    distinto del origen."""
    item, ready = _isis_find_account_item(db, source_id, product.get('id'))
    if not item:
        return {'action': 'chat', 'params': {'answer': f"No veo {product.get('name')} en esa cuenta."}}
    if qty > item['qty']:
        return {'action': 'chat', 'params': {'answer': f"Solo quedan {item['qty']} de {product.get('name')} en la cuenta."}}
    if not ready:
        return {'action': 'chat', 'params': {'answer': f"Ese producto todavía no está listo en cocina o barra, no lo puedo dividir todavía."}}
    if target_acc_id and target_acc_id == source_id:
        return {'action': 'chat', 'params': {'answer': 'Esa es la misma cuenta, dime otra mesa.'}}
    return {'action': 'split_account', 'params': {
        'account_id': source_id, 'source_table': source_table,
        'item_id': item['id'], 'product': product.get('name'), 'qty': qty,
        'target_account_id': target_acc_id, 'table_id': target_table_id,
        'target_table': target_table}}

# --- Personalidad simulada (v5): frases cortas y variadas, nunca la misma ---
# Coloquiales, como en una llamada: a veces con el nombre de Santiago.
_ISIS_WARM_OK = ['¡Claro, Santiago!', 'Ajá, va.', 'Sale, ahorita lo hago.',
                 'Órale, va.', 'Con gusto, Santiago.', 'Claro que sí.']
_ISIS_WARM_DONE = ['¡Listo, Santiago!', '¡Hecho!', 'Ya quedó, Santiago.',
                   '¡Listo!', 'Sale, ya está.']

def _isis_warm(kind='ok'):
    return random.choice(_ISIS_WARM_DONE if kind == 'done' else _ISIS_WARM_OK)

def _isis_say(text, warm=False):
    return (f"{_isis_warm()} {text}" if warm else text)

# --- Duda natural (v7): en la charla libre Isis a veces duda antes de
# contestar, como una persona que está pensando ("Mmm, a ver…"). Una de
# cada 3 respuestas de chat, con contador para que sea medible y no al azar.
_ISIS_DOUBT = ['Mmm, a ver…', 'Déjame ver…', 'A ver, Santiago…']
_ISIS_CHAT_COUNT = [0]

# --- v5 "persona real": nunca leer listas largas en voz alta ---
_ISIS_LIST_MAX = 5

def _isis_short_list(items, total=None):
    """Resume una lista para voz: como mucho 5 elementos y, si hay más,
    ofrece contarle el resto en vez de leerlos todos de corrido."""
    items = [str(x) for x in (items or [])]
    if not items:
        return ''
    total = len(items) if total is None else total
    shown = items[:_ISIS_LIST_MAX]
    txt = '; '.join(shown)
    if total > len(shown):
        rest = total - len(shown)
        if rest == 1:
            txt += ". Y otro más, ¿te digo cuál?"
        else:
            txt += f". Y otros {rest} más, ¿te digo cuáles?"
    else:
        txt += '.'
    return txt

def _isis_pesos(value):
    """Monto en pesos para voz: '40 pesos' o '40.50 pesos' (sin '.00').
    Trabajamos en pesos mexicanos: al oirse, el '$' se puede confundir con
    otra moneda, asi que el monto siempre se dice con 'pesos'."""
    try:
        v = float(value)
    except Exception:
        return f"{value} pesos"
    if v == int(v):
        return f"{int(v)} pesos"
    return f"{v:.2f} pesos"


def _isis_n(n, singular, plural):
    """Concordancia para voz: '1 cuenta' / '3 cuentas', '1 producto' / '8 productos'."""
    try:
        num = int(n or 0)
    except Exception:
        num = 0
    return f"{n} {singular if num == 1 else plural}"


_MESES_ES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
             'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def _isis_fecha_natural(value):
    """Fecha ISO a frase para voz: 'el 24 de noviembre a las 8 de la noche'.
    La fecha ISO cruda ('2026-11-24 20:00') suena a robot al leerse."""
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return str(value)
    h = dt.hour
    h12 = h % 12 or 12
    parte = 'de la mañana' if h < 12 else 'de la tarde' if h < 19 else 'de la noche'
    return f"el {dt.day} de {_MESES_ES[dt.month - 1]} a las {h12} {parte}"


def _isis_mesa_txt(name):
    """'Mesa 1' o 'mesa Terraza': evita el redundante 'la mesa Mesa 1'."""
    n = str(name or '').strip()
    return n if n.lower().startswith('mesa') else 'mesa ' + n

# --- Memoria de Isis (v2): solo datos del restaurante ---
def _isis_memory_get(db, key):
    row = db.get(IsisMemory, _isis_norm(key)[:120])
    return row.value if row else None

def _isis_memory_set(db, key, value):
    k = _isis_norm(key)[:120]
    row = db.get(IsisMemory, k)
    if row:
        row.value = str(value)[:1000]
        row.updated_at = now()
    else:
        db.add(IsisMemory(key=k, value=str(value)[:1000]))
    db.commit()

def _isis_memory_delete(db, key):
    row = db.get(IsisMemory, _isis_norm(key)[:120])
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True

def _isis_memory_all(db):
    return [(r.key, r.value) for r in db.scalars(select(IsisMemory).order_by(IsisMemory.key)).all()]

def _isis_memory_search(db, query):
    # Las preguntas siempre traen '?' al final ("¿qué recuerdas de la
    # birria?") y ese signo quedaba pegado a la última palabra, así que
    # 'birria?' nunca casaba con 'birria': se extraen solo las palabras.
    q = _isis_norm(query)
    words = [w for w in re.findall(r'\w+', q) if len(w) > 2]
    out = []
    for key, value in _isis_memory_all(db):
        hay = _isis_norm(key + ' ' + value)
        if not words or any(w in hay for w in words):
            out.append((key, value))
    return out


# --- Pendientes por voz (v11): un pendiente por clave 'pendiente:<texto>' ---
def _isis_todo_list(db):
    """Todos los pendientes, en orden de clave: [(key, texto)]."""
    return [(k, v) for k, v in _isis_memory_all(db) if k.startswith('pendiente:')]


def _isis_todo_find(db, query):
    """Pendientes que hablan de algo: busca por palabras (más de 2 letras)
    en la clave o el texto, como _isis_memory_search."""
    words = [w for w in re.findall(r'\w+', _isis_norm(query)) if len(w) > 2]
    out = []
    for key, value in _isis_todo_list(db):
        hay = _isis_norm(key + ' ' + value)
        if not words or any(w in hay for w in words):
            out.append((key, value))
    return out

# --- Clientes de siempre (v8): viven en IsisMemory con clave 'cliente:<nombre>' ---
# El esquema clave/valor ya lo permite; no hace falta otra tabla. Nunca se
# inventa un cliente: si no hay registro, Isis dice que no lo conoce.
def _isis_client_key(name):
    return 'cliente:' + _isis_norm(name).strip()


def _isis_client_parse(value):
    """'mesa 4; siempre pide una birria grande' -> ('4', 'una birria grande')."""
    table, habit = None, None
    m = re.search(r'mesa\s+(\d+)', str(value or ''))
    if m:
        table = m.group(1)
    m = re.search(r'siempre pide\s+(.+)$', str(value or ''))
    if m:
        habit = m.group(1).strip().rstrip('.')
    return table, habit


def _isis_client_find(db, name=None, table=None):
    """Busca un cliente frecuente en la memoria, por nombre o por mesa.
    Devuelve (nombre, mesa, costumbre) o None."""
    qn = _isis_norm(name) if name else ''
    qt = str(table).strip() if table is not None else ''
    for key, value in _isis_memory_all(db):
        if not key.startswith('cliente:'):
            continue
        stored = key[len('cliente:'):]
        if qn and not (qn in stored or stored in qn):
            continue
        t, habit = _isis_client_parse(value)
        if qt and t != qt:
            continue
        return stored, t, habit
    return None


def _isis_client_name_txt(name):
    return ' '.join(w.capitalize() for w in str(name or '').split())


def _isis_client_txt(name, table, habit):
    """Frase corta para voz: 'Don Pedro, mesa 4. Siempre pide una birria grande.'"""
    txt = _isis_client_name_txt(name)
    if table:
        txt += f", mesa {table}"
    txt += '.'
    if habit:
        txt += f" Siempre pide {habit}."
    return txt


def _isis_client_describe(name, table, habit):
    """Para la confirmación: 'Anoto a Don Pedro de la mesa 4, que siempre pide...'."""
    txt = f"Anoto a {_isis_client_name_txt(name)}"
    if table:
        txt += f" de la mesa {table}"
    if habit:
        txt += f", que siempre pide {habit}"
    return txt + '.'


def _isis_parse_client_remember(text):
    """'recuerda que la mesa 4 es don Pedro, siempre pide una birria grande'
    -> remember con clave 'cliente:don pedro'. None si no es un cliente."""
    if 'recuerda que' not in text:
        return None
    body = text.split('recuerda que', 1)[1].strip()
    name = table = habit = None
    m = re.search(r'mesa\s+(\d+)\s+es\s+(?:de\s+)?([a-z ]+?)(?:\s*,|\s+siempre\s+pide|\s+pide\s|\s+y\s+|$)', body)
    if m:
        table, name = m.group(1), m.group(2).strip()
    else:
        m = re.search(r'([a-z ]+?)\s+es\s+de\s+la\s+mesa\s+(\d+)', body)
        if m:
            name, table = m.group(1).strip(), m.group(2)
    if not name:
        m = re.search(r'([a-z ]+?)\s+siempre\s+pide\s+(.+)$', body)
        if m:
            name = m.group(1).strip()
    if not name:
        return None
    name = re.sub(r'^(el|la)\s+', '', name).strip()
    if not name or len(name) < 3:
        return None
    hm = re.search(r'(?:siempre\s+)?pide\s+(.+)$', body)
    if hm:
        habit = hm.group(1).strip().rstrip('.')
    value = (f"mesa {table}; " if table else '') + (f"siempre pide {habit}" if habit else '')
    if not value:
        return None
    return {'action': 'remember', 'params': {'key': _isis_client_key(name), 'value': value}}


def _isis_parse_client_question(text):
    """'¿quién es la mesa 4?' / '¿qué pide siempre don Pedro?' -> client_info."""
    m = re.search(r'quien\s+(?:es|esta)\s+(?:en\s+)?(?:la\s+)?mesa\s+(\d+)', text)
    if m:
        return {'action': 'client_info', 'params': {'table': m.group(1)}}
    m = re.search(r'de\s+quien\s+es\s+(?:la\s+)?mesa\s+(\d+)', text)
    if m:
        return {'action': 'client_info', 'params': {'table': m.group(1)}}
    m = re.search(r'que\s+pide\s+(?:siempre\s+)?([a-z ]+?)(?:\?|$)', text)
    if m:
        nm = re.sub(r'^(el|la)\s+', '', m.group(1).strip())
        if 'mesa' in nm:
            mm = re.search(r'(\d+)', nm)
            if mm:
                return {'action': 'client_info', 'params': {'table': mm.group(1)}}
        elif len(nm) >= 3:
            return {'action': 'client_info', 'params': {'name': nm}}
    m = re.search(r'que\s+le\s+gusta\s+a\s+([a-z ]+?)(?:\?|$)', text)
    if m:
        nm = re.sub(r'^(el|la)\s+', '', m.group(1).strip())
        if len(nm) >= 3:
            return {'action': 'client_info', 'params': {'name': nm}}
    m = re.search(r'quien\s+es\s+([a-z ]+?)(?:\?|$)', text)
    if m:
        nm = re.sub(r'^(el|la)\s+', '', m.group(1).strip())
        if len(nm) >= 3 and 'mesa' not in nm:
            return {'action': 'client_info', 'params': {'name': nm}}
    return None

def _isis_day_summary(db, user):
    """Resumen del día para que Isis responda con contexto (v2)."""
    try:
        o = overview(db, user)
        c = cash(db, user)
        if not c.get('open'):
            return (f"Hoy: {_isis_n(o.get('active_accounts', 0), 'cuenta activa', 'cuentas activas')}, "
                    f"{_isis_n(o.get('products', 0), 'producto', 'productos')} en el menú. No hay turno de caja abierto.")
        return (f"Hoy: {_isis_n(o.get('active_accounts', 0), 'cuenta activa', 'cuentas activas')}. Ventas del turno: "
                f"efectivo {c.get('cash')} pesos, tarjeta {c.get('card')} pesos, transferencia "
                f"{c.get('transfer')} pesos. Efectivo esperado en caja: {c.get('expected')} pesos. "
                f"Propinas: {c.get('tips')} pesos.")
    except Exception:
        return 'Resumen del día no disponible.'

def _isis_find_user(db, user, text, role=None):
    try:
        rows = users(db, user)
    except Exception:
        return None
    norm = _isis_norm(text)
    cands = [r for r in rows if (role is None or r.get('role') == role)]
    best = None
    for r in cands:
        name = _isis_norm(r.get('display', ''))
        if name and name in norm and (best is None or len(name) > len(_isis_norm(best.get('display', '')))):
            best = r
    return best

def _isis_find_ticket(db, user, text, station=None):
    try:
        rows = tickets(db, user)
    except Exception:
        return None
    m = re.search(r'(?:comanda|ticket)\s*#?\s*(\d+)', text)
    if m:
        for t in rows:
            if str(t.get('id')) == m.group(1) or str(t.get('batch_id')) == m.group(1):
                return t
    for t in rows:
        if station and t.get('station') != station:
            continue
        if t.get('status') in ('new', 'received', 'preparing'):
            return t
    return None

def _isis_find_reservation(db, user, text):
    try:
        rows = mitote_extensions.reservations(db, user)
    except Exception:
        return None
    norm = _isis_norm(text)
    for r in rows:
        name = _isis_norm(r.get('name', ''))
        if name and name in norm and r.get('status') in ('pending', 'confirmed'):
            return r
    return None

def _isis_role_word(text):
    if 'meser' in text:
        return 'waiter'
    if 'caj' in text:
        return 'cashier'
    if 'cocin' in text:
        return 'kitchen'
    if 'barra' in text:
        return 'bar'
    if 'administr' in text:
        return 'admin'
    return None

def _isis_station_word(text):
    if 'barra' in text:
        return 'bar'
    if 'cocina' in text:
        return 'kitchen'
    return None

def _isis_unit_word(text):
    for u in ('botellas', 'latas', 'unidades', 'kg', 'litros'):
        if u in text:
            return u
    return 'botellas'


def _isis_parse_order_note(text):
    """Nota/comentario de un pedido por voz (v11): 'agrega dos coronas a la
    mesa cinco, sin cebolla' -> 'sin cebolla'. Marcadores explícitos
    ('con nota', 'nota', 'con comentario', 'comentario') en cualquier
    posición, o cola después de la mesa ('sin X', 'con poco X'). Si no hay
    nota, None. Pura y testeable."""
    t = _isis_norm(text)
    m = re.search(r'\b(?:con\s+nota|con\s+comentario|nota|comentario)\s*:?\s*(.+)$', t)
    if m:
        note = re.sub(r'\s+a\s+la\s+mesa\s+(?:\d+|[a-z]+).*$', '', m.group(1).strip())
        note = note.rstrip(' .?!').strip()
        if note:
            return note[:200]
    m = re.search(r'mesa\s+(?:\d+|[a-z]+)\s*,?\s*((?:sin|con\s+poco|con\s+poca)\s+.+)$', t)
    if m:
        note = m.group(1).strip().rstrip(' .?!').strip()
        if note:
            return note[:200]
    return None

def _isis_table_numbers(text):
    """Números de mesa, en dígito o en palabra ('mesa cinco' -> '5')."""
    out = []
    for m in re.finditer(r'mesa\s+(\d+|' + '|'.join(_NUM_WORDS) + r')\b', text):
        v = m.group(1)
        out.append(v if v.isdigit() else str(_NUM_WORDS[v]))
    return out

def _isis_parse_reservation_time(text):
    """'hoy a las 20' / 'mañana a las 19:30' -> datetime en hora de CDMX, o None.
    Busca la hora después de 'a las'; si no hay, toma el último número del
    texto (el primero suele ser el número de personas)."""
    m = re.search(r'a las?\s+(\d{1,2})(?::(\d{2}))?\s*(pm|am)?', text)
    if not m:
        nums = list(re.finditer(r'(\d{1,2})(?::(\d{2}))?\s*(pm|am)?', text))
        m = nums[-1] if nums else None
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == 'pm' and hour < 12:
        hour += 12
    if ampm == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = ZoneInfo('America/Mexico_City')
    base = datetime.now(tz)
    if 'manana' in text:
        base = base + timedelta(days=1)
    elif 'hoy' not in text and 'manana' not in text:
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

def _isis_resolve_followup(db, user, text, history):
    """Resuelve un seguimiento corto ("¿y la de 1.2?") usando el último intercambio (v4).
    Solo para preguntas de precio de un producto de la misma familia que el último
    que se mencionó (Corona 355 -> Corona 1.2). Si no hay a qué referirse, None."""
    if not history:
        return None
    words = text.split()
    if len(words) > 6:
        return None
    if not (text.startswith('y ') or re.match(r'^(y\s+)?(la|el|esa|ese|eso|esta|este)\b', text)):
        return None
    last_user = (history[-1][0] or '') if history else ''
    last_prod = _isis_find_product(db, user, last_user)
    if not last_prod:
        return None
    fam = _isis_norm(last_prod.get('name', '')).split()
    if not fam:
        return None
    try:
        prods = products(db, user)
    except Exception:
        return None
    best = None
    for p in prods:
        name = _isis_norm(p.get('name', ''))
        parts = name.split()
        if not parts or parts[0] != fam[0] or p.get('id') == last_prod.get('id'):
            continue
        rest = parts[1:]
        if rest and all(re.search(r'\b' + re.escape(w) + r'\b', text) for w in rest):
            if best is None or len(name) > len(_isis_norm(best.get('name', ''))):
                best = p
    if not best:
        return None
    return {'action': 'product_price', 'params': {'product_id': best.get('id'), 'product': best.get('name')}}


def _isis_parse_local(db, user, raw, history=None):
    """Interprete local, sin IA externa. Devuelve intent o None."""
    text = _isis_norm(raw)
    if not text:
        return None
    # --- Seguimiento del hilo (v4): "¿y la de 1.2?" tras preguntar por la Corona 355 ---
    follow = _isis_resolve_followup(db, user, text, history)
    if follow is not None:
        return follow
    # --- Clientes de siempre (v8): antes que la memoria genérica ---
    cli = _isis_parse_client_remember(text)
    if cli is not None:
        return cli
    cli_q = _isis_parse_client_question(text)
    if cli_q is not None:
        return cli_q
    # --- Pendientes por voz (v11): lista de cosas por hacer, en IsisMemory ---
    # Un pendiente por clave ('pendiente:<texto>'): así 'olvida lo del
    # carbón' borra solo ese pendiente y nunca la lista entera. No choca
    # con 'recuerda que...': eso es memoria general, esto es la lista.
    if 'anota pendiente' in text or 'anota que hay que' in text or ('anota' in text and 'hay que' in text):
        body = text
        for pref in ('anota pendiente:', 'anota pendiente', 'anota que hay que', 'anota'):
            if pref in body:
                body = body.split(pref, 1)[1].strip(' :')
                break
        body = body.strip().rstrip('.')
        if not body:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué anoto en tus pendientes?'}
        key = 'pendiente:' + _isis_norm(body)[:100]
        if _isis_memory_get(db, key) is not None:
            return {'action': 'chat', 'params': {'answer': f"Ese ya lo tienes anotado: {body}."}}
        return {'action': 'todo_add', 'params': {'key': key, 'text': body}}
    if ('que tengo pendiente' in text or 'que pendientes tengo' in text
            or 'mis pendientes' in text or 'que pendientes hay' in text):
        return {'action': 'todo_list', 'params': {}}
    if (('borra' in text or 'quita' in text or 'ya quedo' in text) and 'pendiente' in text) \
            or 'ya quedo lo de' in text or 'ya quedo lo del' in text:
        q = text
        for pref in ('borra el pendiente del', 'borra el pendiente de', 'borra el pendiente',
                     'quita el pendiente del', 'quita el pendiente de', 'quita el pendiente',
                     'ya quedo lo del', 'ya quedo el pendiente del', 'ya quedo el pendiente de',
                     'ya quedo lo de', 'ya quedo'):
            if pref in q:
                q = q.split(pref, 1)[1].strip()
                break
        q = re.sub(r'^(el|la|los|las|lo|de|del)\s+', '', q).strip().rstrip('.')
        if not q:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué pendiente borro? Dime de qué era.'}
        hits = _isis_todo_find(db, q)
        if not hits:
            return {'action': 'chat', 'params': {'answer': f"No tengo anotado nada de '{q}'."}}
        return {'action': 'todo_remove', 'params': {'query': q,
                                                    'keys': [k for k, _v in hits],
                                                    'texts': [v for _k, v in hits]}}
    # --- Memoria (v2) ---
    if 'recuerdas' in text or 'que sabes de' in text or 'que tienes guardado' in text:
        q = text
        for pref in ('que recuerdas de', 'que recuerdas', 'recuerdas de', 'recuerdas',
                     'que sabes de', 'que tienes guardado de', 'que tienes guardado',
                     'que te dije de', 'que te dije'):
            if q.startswith(pref):
                q = q[len(pref):].strip()
                break
        return {'action': 'recall', 'params': {'query': q}}
    if 'recuerda que' in text:
        body = text.split('recuerda que', 1)[1].strip()
        if not body:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué quieres que recuerde? Dime "recuerda que" y lo anoto.'}
        if ' es ' in body:
            key = body.split(' es ', 1)[0].strip()
        elif ' son ' in body:
            key = body.split(' son ', 1)[0].strip()
        else:
            key = ' '.join(body.split()[:5])
        return {'action': 'remember', 'params': {'key': key, 'value': body}}
    if 'olvida' in text or 'borra de tu memoria' in text:
        q = text
        for pref in ('borra de tu memoria', 'olvida que', 'olvida'):
            if q.startswith(pref):
                q = q[len(pref):].strip()
                break
        return {'action': 'forget', 'params': {'query': q}}
    # --- 14. Estadísticas (antes que el resumen viejo de ventas) ---
    # v11: cierre del día hablado. Va antes que 'resumen' (overview): "resumen
    # del día" es el cierre, no el resumen general. "¿cómo nos fue ayer?" no
    # entra aquí: eso lo cubre el resumen de ayer al entrar.
    if 'ayer' not in text and ('como nos fue' in text or 'cierre del dia' in text
                               or 'resumen del dia' in text or 'cierre de dia' in text):
        return {'action': 'day_close', 'params': {}}
    # v11: resumen de cambio de turno.
    if ('que le dejo' in text or 'que le dejamos' in text or 'resumen de turno' in text
            or 'cambio de turno' in text or 'resumen para el cambio' in text):
        return {'action': 'shift_handover', 'params': {}}
    # v11: lista de compras automática.
    if ('que hay que pedir' in text or 'que hay que comprar' in text
            or 'lista de compras' in text or 'que falta pedir' in text):
        return {'action': 'shopping_list', 'params': {}}
    if 'ventas de ayer' in text or 'ventas de anoche' in text:
        return {'action': 'analytics', 'params': {'period': 'yesterday'}}
    if ('ventas de la semana' in text or 'ventas de esta semana' in text
            or 'ventas en la semana' in text):
        return {'action': 'analytics', 'params': {'period': 'week'}}
    if ('estadistica' in text or 'ventas del dia' in text or 'ventas de hoy' in text
            or 'productos mas vendidos' in text or 'lo mas vendido' in text
            or 'como van las ventas' in text):
        return {'action': 'analytics', 'params': {'period': 'today'}}
    # --- 11. Auditoría ---
    if ('auditoria' in text or 'que paso hoy' in text or 'que se hizo' in text
            or 'registro de operaciones' in text or 'que movimientos hubo hoy' in text):
        return {'action': 'audit_list', 'params': {}}
    # --- 12. Reservaciones ---
    if 'reserv' in text:
        if 'confirma' in text:
            r = _isis_find_reservation(db, user, text)
            if not r:
                return {'action': 'unknown', 'params': {}, 'question': '¿De quién es la reservación? Dime el nombre.'}
            return {'action': 'confirm_reservation', 'params': {'reservation_id': r.get('id')}}
        if 'llego' in text or 'llegada' in text:
            r = _isis_find_reservation(db, user, text)
            if not r:
                return {'action': 'unknown', 'params': {}, 'question': '¿A nombre de quién está la reservación que llegó?'}
            nums = _isis_table_numbers(text)
            if not nums:
                return {'action': 'unknown', 'params': {}, 'question': '¿En qué mesa la siento? Dime el número.'}
            t = _isis_find_table(db, user, 'mesa ' + nums[-1])
            if not t:
                return {'action': 'unknown', 'params': {}, 'question': 'No encontré esa mesa. ¿En cuál la siento?'}
            w = _isis_find_user(db, user, text, role='waiter')
            if not w:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué mesero la atiende? Dime su nombre.'}
            return {'action': 'reservation_arrive', 'params': {'reservation_id': r.get('id'), 'table_id': t.get('id'), 'waiter_id': w.get('id')}}
        if 'crea' in text or 'nueva' in text or 'haz' in text or 'anota' in text or 'para' in text:
            m = re.search(r'para\s+([a-z ]+?)\s+de\s+\d+', text)
            name = m.group(1).strip().title() if m else None
            if not name:
                m2 = re.search(r'para\s+([a-z ]+?)(?:\s+de\s+|\s+hoy|\s+manana|$)', text)
                name = m2.group(1).strip().title() if m2 else None
            if not name or name.lower() in ('la', 'el', 'una'):
                return {'action': 'unknown', 'params': {}, 'question': '¿A nombre de quién hago la reservación?'}
            pm = re.search(r'(\d+)\s*personas', text)
            party = int(pm.group(1)) if pm else None
            if not party:
                return {'action': 'unknown', 'params': {}, 'question': f'¿Para cuántas personas es la reservación de {name}?'}
            when = _isis_parse_reservation_time(text)
            if not when:
                return {'action': 'unknown', 'params': {}, 'question': f'¿A qué hora es la reservación de {name}? Dime hoy o mañana y la hora.'}
            return {'action': 'create_reservation', 'params': {'name': name, 'party_size': party, 'scheduled_at': when.isoformat()}}
        return {'action': 'reservations_list', 'params': {}}
    # --- 3. Estaciones ---
    # v11: la comanda más atrasada va antes que el resto de comandas: si no,
    # 'comanda' la mandaría a la lista de tickets.
    if ('comanda mas atrasada' in text or 'que comanda lleva mas tiempo' in text
            or 'comanda lleva mas tiempo' in text or 'cual es la comanda mas atrasada' in text
            or 'que comanda va mas atrasada' in text or 'comanda mas vieja' in text):
        return {'action': 'oldest_ticket', 'params': {}}
    if 'comanda' in text or 'ticket' in text:
        station = _isis_station_word(text)
        if 'lista' in text or 'listo' in text:
            t = _isis_find_ticket(db, user, text, station)
            if not t:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué comanda marco lista? Dime el número.'}
            return {'action': 'ticket_status', 'params': {'ticket_id': t.get('id'), 'status': 'ready'}}
        if 'preparando' in text or 'a preparar' in text or 'empieza' in text:
            t = _isis_find_ticket(db, user, text, station)
            if not t:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué comanda pongo en preparación? Dime el número.'}
            return {'action': 'ticket_status', 'params': {'ticket_id': t.get('id'), 'status': 'preparing'}}
        if 'recibe' in text or 'recibida' in text or 'recepcion' in text:
            t = _isis_find_ticket(db, user, text, station)
            if not t:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué comanda marco como recibida? Dime el número.'}
            return {'action': 'ticket_status', 'params': {'ticket_id': t.get('id'), 'status': 'received'}}
        return {'action': 'tickets_list', 'params': {'station': station}}
    # --- 4. Cobros ---
    if 'descuento' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿A qué mesa le aplico el descuento?')
        if missing:
            return missing
        pm = re.search(r'(\d+)\s*(?:%|por ciento)', text)
        if pm:
            return {'action': 'apply_discount', 'params': {'account_id': a.get('id'), 'mode': 'percent', 'value': int(pm.group(1))}}
        amt = _isis_money(_isis_strip_mesa(text))
        if amt is None:
            return {'action': 'unknown', 'params': {}, 'question': '¿De cuánto es el descuento, en pesos o en porcentaje?'}
        return {'action': 'apply_discount', 'params': {'account_id': a.get('id'), 'mode': 'fixed', 'value': amt}}
    if 'por cobrar' in text or 'pendientes de cobro' in text or 'cuentas por cobrar' in text:
        return {'action': 'cashier_overview', 'params': {}}
    # --- v10.5: "añade al inventario 5 piezas de arrachera" es reponer, no
    # preguntar cuánto hay. Antes caía en la lectura de inventario y solo
    # decía el stock, sin sumar nada. Va antes del menú para que 'añade' no
    # se lea como crear un producto nuevo. 'insumo' queda fuera: eso lo
    # maneja la rama de registro de insumos de abajo.
    if (any(k in text for k in ('anade', 'agrega', 'sumale', 'pon', 'mete'))
            and any(k in text for k in ('inventario', 'existencias', 'stock'))
            and 'insumo' not in text):
        p = _isis_find_product(db, user, text)
        if not p:
            amb = _isis_ambiguous_product(db, user, text)
            return {'action': 'unknown', 'params': {},
                    'question': amb or '¿Qué producto quieres reponer? Dime su nombre.'}
        return {'action': 'restock', 'params': {'product_id': p.get('id'), 'product': p.get('name'),
                                                'qty': _isis_qty_for_product(text, p),
                                                'reason': 'Reposicion por voz con Isis'}}
    # --- 6. Menú ---
    # Las sub-órdenes (presentación, promoción, precio) no siempre dicen
    # la palabra "menú", así que también disparan por su propio verbo.
    if ('menu' in text or 'carta' in text or 'presentacion' in text
            or 'promocion' in text or 'cambia el precio' in text
            or 'cuanto cuesta' in text or 'cual es el costo' in text
            or 'cual es el precio' in text or 'que precio tiene' in text
            or 'cuanto vale' in text or 'a cuanto esta' in text
            or 'costo de' in text or 'dime el precio' in text or 'dime el costo' in text):
        if 'presentacion' in text:
            p = _isis_find_product(db, user, text)
            if not p:
                return {'action': 'unknown', 'params': {}, 'question': '¿A qué producto le agrego la presentación?'}
            vm = re.search(r'presentacion\s+([a-z ]+?)(?:\s+a\s+|\s+de\s+|\s+por\s+|\s+en\s+|$)', text)
            vname = vm.group(1).strip().title() if vm else None
            price = _isis_money(text)
            if not vname or price is None:
                return {'action': 'unknown', 'params': {}, 'question': '¿Cómo se llama la presentación y cuánto cuesta?'}
            return {'action': 'add_variant', 'params': {'product_id': p.get('id'), 'name': vname, 'price': price}}
        if 'promocion' in text:
            p = _isis_find_product(db, user, text)
            if not p:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué producto pongo en promoción?'}
            pm = re.search(r'(\d+)\s*(?:%|por ciento)', text)
            if not pm:
                return {'action': 'unknown', 'params': {}, 'question': '¿De qué porcentaje es la promoción?'}
            return {'action': 'add_promotion', 'params': {'product_id': p.get('id'), 'percent': int(pm.group(1))}}
        if 'archiva' in text or 'elimina del menu' in text or 'quita del menu' in text:
            p = _isis_find_product(db, user, text)
            if not p:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué producto saco del menú?'}
            return {'action': 'archive_product', 'params': {'product_id': p.get('id')}}
        # Pregunta por el precio de UN producto: responde solo ese precio,
        # nunca la lista completa del menú. Va antes de "cambia el precio"
        # porque "cuál es el precio de la corona 355" también trae "precio
        # de" y el 355 se confundiría con un precio nuevo.
        if (any(k in text for k in ('cuanto cuesta', 'cual es el costo', 'cual es el precio',
                                     'que precio tiene', 'cuanto vale', 'a cuanto esta',
                                     'dime el costo', 'dime el precio'))
                or ('costo de' in text and 'cambia' not in text and 'pon' not in text)):
            p = _isis_find_product(db, user, text)
            if not p:
                amb = _isis_ambiguous_product(db, user, text)
                return {'action': 'unknown', 'params': {},
                        'question': amb or '¿De qué producto quieres saber el precio?'}
            return {'action': 'product_price', 'params': {'product_id': p.get('id'), 'product': p.get('name')}}
        if 'cambia el precio' in text or 'precio de' in text:
            p = _isis_find_product(db, user, text)
            if not p:
                return {'action': 'unknown', 'params': {}, 'question': '¿A qué producto le cambio el precio?'}
            nums = _isis_numbers(text)
            if not nums:
                return {'action': 'unknown', 'params': {}, 'question': '¿En cuánto lo dejo?'}
            return {'action': 'edit_product_price', 'params': {'product_id': p.get('id'), 'price': nums[-1]}}
        # v11: crear un producto exige decirlo en el menú ('agrega X al
        # menú'). Sin 'menú' ni 'carta', 'agrega X a la mesa Y' es un pedido
        # (add_order), no un producto nuevo.
        if (('menu' in text or 'carta' in text) and any(k in text for k in ('agrega', 'anade'))) \
                or 'crea' in text or 'nuevo producto' in text:
            price = _isis_money(text)
            if price is None:
                return {'action': 'unknown', 'params': {}, 'question': '¿Cuánto cuesta el producto nuevo? Dime el precio en pesos.'}
            name = text
            # Quitar solo palabras completas: 'la' como subcadena rompería
            # nombres como 'limonada'.
            name = re.sub(r'\b(agrega al menu|agrega|anade al menu|anade|crea|nuevo producto|al menu|en el menu|en barra|en cocina|la|el|a)\b', ' ', name)
            name = re.sub(r'\b\d+(?:\.\d+)?\s*pesos?\b', ' ', name)
            name = re.sub(r'\s+', ' ', name).strip().title()
            if not name:
                return {'action': 'unknown', 'params': {}, 'question': '¿Cómo se llama el producto nuevo?'}
            return {'action': 'create_product', 'params': {'name': name, 'station': _isis_station_word(text) or 'kitchen', 'price': price}}
        return {'action': 'menu_list', 'params': {}}
    # --- 7. Inventario: movimientos e insumos ---
    if 'movimientos' in text and 'inventario' in text:
        p = _isis_find_product(db, user, text)
        return {'action': 'inventory_movements', 'params': {'product_id': p.get('id') if p else None, 'product': p.get('name') if p else None}}
    if 'insumo' in text and 'registra' in text:
        m = re.search(r'insumo\s+([a-z ]+?)\s+con\s+', text)
        name = m.group(1).strip().title() if m else None
        if not name:
            return {'action': 'unknown', 'params': {}, 'question': '¿Cómo se llama el insumo?'}
        qty = _isis_qty(re.sub(r'insumo\s+[a-z ]+?\s+con\s+', '', text))
        return {'action': 'create_inventory_item', 'params': {'name': name, 'qty': qty, 'unit': _isis_unit_word(text)}}
    # --- 8. Personal ---
    if 'cambia el pin de' in text:
        u = _isis_find_user(db, user, text)
        if not u:
            return {'action': 'unknown', 'params': {}, 'question': '¿A quién le cambio el PIN? Dime su nombre.'}
        m = re.search(r'\b(\d{6})\b', text)
        if not m:
            return {'action': 'unknown', 'params': {}, 'question': '¿Cuál es el PIN nuevo de 6 números?'}
        return {'action': 'staff_pin', 'params': {'user_id': u.get('id'), 'pin': m.group(1)}}
    if any(k in text for k in ('registra', 'crea')) and any(k in text for k in ('personal', 'empleado', 'meser', 'cajer', 'cociner', 'barman', 'administr')):
        m = re.search(r'registra a\s+([a-z ]+?)\s+como\s+', text) or re.search(r'registra\s+([a-z ]+?)\s+como\s+', text)
        name = m.group(1).strip().title() if m else None
        if not name:
            return {'action': 'unknown', 'params': {}, 'question': '¿Cómo se llama la persona?'}
        role = _isis_role_word(text)
        if not role:
            return {'action': 'unknown', 'params': {}, 'question': f'¿Qué perfil le doy a {name}: mesero, caja, cocina, barra o administrador?'}
        pm = re.search(r'\b(\d{6})\b', text)
        if not pm:
            return {'action': 'unknown', 'params': {}, 'question': f'¿Qué PIN de 6 números le pongo a {name}?'}
        return {'action': 'create_staff', 'params': {'name': name, 'role': role, 'pin': pm.group(1)}}
    if 'personal' in text or 'empleados' in text or 'quien trabaja' in text or 'el equipo' in text:
        return {'action': 'staff_list', 'params': {}}
    # --- 9. Propinas ---
    if 'propina' in text:
        # v11, acción delicada: pagarle su parte a una persona concreta.
        # Va antes del fondo/reparto: "págale su propina a María" no es
        # preguntar por el fondo. Por defecto en efectivo; si dice
        # "por transferencia", así se paga. Se paga TODO lo pendiente.
        if any(k in text for k in ('pagale', 'dale', 'paga la propina', 'paga su propina', 'entregale')):
            u = _isis_find_user(db, user, text)
            if not u:
                return {'action': 'unknown', 'params': {}, 'question': '¿A quién le pago su propina? Dime el nombre.'}
            dist, share, pending = _isis_find_tip_share(db, u.get('id'))
            if not dist or not share:
                return {'action': 'chat', 'params': {'answer': f"{u.get('display')} no está en el reparto de propinas."}}
            if pending <= 0:
                return {'action': 'chat', 'params': {'answer': f"A {u.get('display')} ya se le pagó toda su propina."}}
            method = 'transfer' if 'transfer' in text else 'cash'
            return {'action': 'tip_payout', 'params': {'distribution_id': dist.get('id'),
                                                       'share_id': share.get('id'),
                                                       'employee': u.get('display'),
                                                       'amount': round(pending / 100, 2),
                                                       'method': method}}
        if 'reparte' in text or 'reparto' in text:
            return {'action': 'tips_distribute', 'params': {}}
        return {'action': 'tips_pool', 'params': {}}
    # --- 10. Autorizaciones ---
    if 'solicitud' in text or 'autorizacion' in text:
        if 'autoriza' in text or 'aprueba' in text or 'rechaza' in text:
            m = re.search(r'solicitud\s*#?\s*(\d+)', text)
            rid = int(m.group(1)) if m else None
            if rid is None:
                try:
                    pend = [r for r in requests(db, user) if r.get('status') == 'pending']
                    rid = pend[0].get('id') if pend else None
                except Exception:
                    rid = None
            if rid is None:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué solicitud resuelvo? Dime el número.'}
            return {'action': 'decide_approval', 'params': {'request_id': rid, 'approve': 'rechaza' not in text}}
        return {'action': 'approvals_list', 'params': {}}
    # --- 13. Tiempos ---
    # "Pon el tiempo de la cerveza en 10 minutos" no dice preparación ni
    # estación: dispara también si se nombra un producto.
    if 'tiempo' in text and ('preparacion' in text or 'cocina' in text or 'barra' in text
                             or _isis_find_product(db, user, text)):
        if any(k in text for k in ('pon', 'cambia', 'ajusta')):
            mm = re.search(r'(\d+)\s*min', text)
            if not mm:
                return {'action': 'unknown', 'params': {}, 'question': '¿De cuántos minutos lo pongo?'}
            p = _isis_find_product(db, user, text)
            if p:
                return {'action': 'product_prep_set', 'params': {'product_id': p.get('id'), 'product': p.get('name'), 'minutes': int(mm.group(1))}}
            return {'action': 'prep_config_set', 'params': {'batch_minutes': int(mm.group(1))}}
        return {'action': 'prep_config', 'params': {}}
    # --- 1. Cuentas: lista y cambio de mesa ---
    if 'cuentas abiertas' in text or 'que cuentas hay' in text or 'cuentas activas' in text:
        return {'action': 'accounts_list', 'params': {}}
    if 'cambia la cuenta' in text or 'mueve la cuenta' in text or 'transfiere la cuenta' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿Qué cuenta cambio de mesa? Dime la mesa de origen.')
        if missing:
            return missing
        nums = _isis_table_numbers(text)
        if len(nums) < 2:
            return {'action': 'unknown', 'params': {}, 'question': '¿A qué mesa la muevo? Dime la mesa destino.'}
        t = _isis_find_table(db, user, 'mesa ' + nums[-1])
        if not t:
            return {'action': 'unknown', 'params': {}, 'question': 'No encontré esa mesa. ¿A cuál la muevo?'}
        return {'action': 'transfer_account', 'params': {'account_id': a.get('id'), 'table_id': t.get('id'), 'table_name': t.get('name')}}
    # --- v11, acciones delicadas: unir y dividir cuentas (doble confirmación) ---
    if re.search(r'\b(une|junta)\b', text) and 'cuenta' in text:
        nums = _isis_table_numbers(text)
        if len(nums) >= 2:
            a = _isis_find_account(db, user, 'mesa ' + nums[0])
            b = _isis_find_account(db, user, 'mesa ' + nums[1])
            if not a or not b:
                return {'action': 'unknown', 'params': {}, 'question': 'No encontré una de esas cuentas. ¿De qué mesas son?'}
        else:
            # Por nombre de cliente: "une la cuenta de Juanita con la de María".
            parts = re.split(r'\s+con\s+', text, maxsplit=1)
            a, missing_a = _isis_resolve_account(db, user, parts[0], '¿Qué dos cuentas uno? Dime las dos mesas.')
            if missing_a:
                return missing_a
            b = None
            if len(parts) > 1:
                b, missing_b = _isis_resolve_account(db, user, parts[1], '¿Con qué cuenta la uno? Dime la segunda mesa.')
                if missing_b:
                    return missing_b
            if not a or not b:
                return {'action': 'unknown', 'params': {}, 'question': '¿Qué dos cuentas uno? Dime las dos mesas.'}
        if a.get('id') == b.get('id'):
            return {'action': 'unknown', 'params': {}, 'question': 'Son la misma cuenta, dime dos mesas distintas.'}
        return {'action': 'merge_accounts', 'params': {'account_id': a.get('id'),
                                                       'target_account_id': b.get('id'),
                                                       'source_table': a.get('table_name') or a.get('name'),
                                                       'target_table': b.get('table_name') or b.get('name')}}
    if ('divide' in text or 'separa' in text or 'parte la cuenta' in text) and 'cuenta' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿Qué cuenta divido? Dime la mesa.')
        if missing:
            return missing
        p = _isis_find_product(db, user, text)
        nums = _isis_table_numbers(text)
        if not p or len(nums) < 2:
            # Falta el producto o el destino: Isis pregunta paso a paso.
            return {'action': 'split_account_start', 'params': {'account_id': a.get('id'),
                                                                'table_name': a.get('table_name') or a.get('name')}}
        # Cantidad: el primer número que no sea parte del nombre del
        # producto ("mueve la corona 355" mueve 1, no 355).
        qty = _isis_qty_for_product(text, p)
        t = _isis_find_table(db, user, 'mesa ' + nums[-1])
        if not t:
            return {'action': 'unknown', 'params': {}, 'question': 'No encontré esa mesa destino. ¿A cuál lo muevo?'}
        tgt = _isis_find_account(db, user, 'mesa ' + nums[-1])
        if tgt:
            return _isis_split_build(db, a.get('id'), a.get('table_name') or a.get('name'),
                                     p, qty, tgt.get('id'), None, tgt.get('table_name'))
        return _isis_split_build(db, a.get('id'), a.get('table_name') or a.get('name'),
                                 p, qty, None, t.get('id'), t.get('name'))
    # --- 2. Mesas: cambiar capacidad ---
    if 'capacidad' in text and 'mesa' in text:
        t = _isis_find_table(db, user, text)
        if not t:
            return {'action': 'unknown', 'params': {}, 'question': '¿De qué mesa cambio la capacidad?'}
        mm = re.search(r'capacidad\s+(?:de\s+)?(\d+)', text) or re.search(r'a\s+(\d+)\s*(?:personas)?\s*$', text)
        if not mm:
            return {'action': 'unknown', 'params': {}, 'question': '¿Para cuántas personas la dejo?'}
        return {'action': 'update_table', 'params': {'table_id': t.get('id'), 'table_name': t.get('name'), 'capacity': int(mm.group(1))}}
    # --- 5. Caja: historial de cortes ---
    if 'historial de caja' in text or 'cortes anteriores' in text or 'cortes pasados' in text:
        return {'action': 'cash_history', 'params': {}}
    # Lecturas
    if any(k in text for k in ('mesas libres', 'mesas ocupadas', 'que mesas', 'como van las mesas', 'estado de las mesas')):
        return {'action': 'tables', 'params': {}}
    if 'inventario' in text or 'existencias' in text or 'stock' in text or 'cuanto queda de' in text or 'cuanto hay de' in text:
        p = _isis_find_product(db, user, text)
        return {'action': 'inventory', 'params': {'product': p.get('name') if p else None}}
    if any(k in text for k in ('como va la caja', 'cuanto llevamos', 'ventas de hoy', 'resumen', 'como vamos')):
        return {'action': 'overview', 'params': {}}
    if 'caja' in text and any(k in text for k in ('cuanto hay', 'cuanto tenemos', 'estado', 'como esta')):
        return {'action': 'cash', 'params': {}}
    if 'cuanto debe' in text or 'cuanto es' in text or 'saldo' in text or 'detalle de la cuenta' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿De qué mesa quieres saber el saldo? Dime el nombre o el número.')
        if missing:
            return missing
        return {'action': 'account_detail', 'params': {'account_id': a.get('id')}}
    # Escrituras (todas con confirmacion)
    if 'abre el turno' in text or 'abrir turno' in text or 'abre caja' in text:
        amt = _isis_money(text)
        if amt is None:
            return {'action': 'unknown', 'params': {}, 'question': '¿Con cuánto fondo abro el turno? Dime el importe en pesos.'}
        return {'action': 'open_cash', 'params': {'opening': amt}}
    if 'cierra el turno' in text or 'corte de caja' in text or 'haz el corte' in text or 'cerrar caja' in text:
        amt = _isis_money(text)
        if amt is None:
            # Sin monto en la misma frase: corte guiado paso a paso (v8).
            return {'action': 'cash_cut_start', 'params': {}}
        return {'action': 'close_cash', 'params': {'counted': amt, 'note': 'Corte por voz con Isis'}}
    if 'crea' in text and 'mesa' in text:
        m_raw = re.search(r'mesa\s+([A-Za-z0-9 ]+?)\s*$', raw.strip(), re.IGNORECASE)
        name = m_raw.group(1).strip().title() if m_raw else (_isis_table_name(text) or 'Nueva mesa')
        return {'action': 'create_table', 'params': {'name': name, 'area': 'Salon', 'capacity': 4}}
    if 'abre la mesa' in text or 'abre mesa' in text or 'nueva cuenta' in text:
        t = _isis_find_table(db, user, text)
        if not t:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué mesa quieres abrir? Dime el nombre o el número.'}
        return {'action': 'new_account', 'params': {'table_id': t.get('id'), 'table_name': t.get('name')}}
    if 'anula' in text and 'cuenta' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿Qué cuenta quieres anular? Dime la mesa.')
        if missing:
            return missing
        return {'action': 'void_account', 'params': {'account_id': a.get('id'), 'reason': 'Anulacion por voz con Isis'}}
    if 'cierra la cuenta' in text or 'cerrar la cuenta' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿Qué cuenta quieres cerrar? Dime la mesa.')
        if missing:
            return missing
        return {'action': 'close_account', 'params': {'account_id': a.get('id'),
                                                      'table_name': a.get('table_name'),
                                                      'client_name': _isis_client_name_of(a)}}
    # v11, acción delicada: reabrir una cuenta que se cerró como pagada.
    # Si su mesa sigue libre, vuelve a esa mesa; si ya está ocupada, la
    # cuenta queda abierta sin mesa.
    if 'reabr' in text and 'cuenta' in text:
        a, t = _isis_find_closed_account(db, user, text)
        if not a:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué cuenta reabro? Dime la mesa de la cuenta que se cerró.'}
        occupied = bool(t and db.get(TableOccupancy, t.get('id')) is not None)
        return {'action': 'reopen_closed', 'params': {'account_id': a.get('id'),
                                                      'table_name': t.get('name') if t else a.get('name'),
                                                      'table_id': t.get('id') if (t and not occupied) else None,
                                                      'table_occupied': occupied}}
    # v11: cobrar y cerrar en una sola frase. Va antes que 'cobra' solo:
    # si solo se cobra, la cuenta queda pendiente; aquí se cierra también.
    if ('cobra' in text and 'cierra' in text) or ('paga' in text and 'cierra' in text and 'mesa' in text):
        a, missing = _isis_resolve_account(db, user, text, '¿Qué mesa quieres cobrar y cerrar? Dime el nombre o el número.')
        if missing:
            return missing
        method = _isis_method(text)
        if not method:
            return {'action': 'unknown', 'params': {}, 'question': '¿En efectivo, con tarjeta o por transferencia?'}
        return {'action': 'pay_and_close_account', 'params': {'account_id': a.get('id'),
                                                              'method': method,
                                                              'table_name': a.get('table_name') or a.get('name'),
                                                              'client_name': _isis_client_name_of(a)}}
    if 'cobra' in text or 'paga la mesa' in text or 'pagar la mesa' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿Qué mesa quieres cobrar? Dime el nombre o el número.')
        if missing:
            return missing
        method = _isis_method(text)
        all_flag = 'todo' in text or 'completo' in text or 'total' in text
        amt = None if all_flag else _isis_money(_isis_strip_mesa(text))
        if not all_flag and amt is None:
            return {'action': 'unknown', 'params': {}, 'question': '¿Cuánto cobro, o todo el saldo?'}
        if not method:
            return {'action': 'unknown', 'params': {}, 'question': '¿En efectivo, con tarjeta o por transferencia?'}
        return {'action': 'pay_account', 'params': {'account_id': a.get('id'), 'amount': amt, 'method': method, 'all': all_flag,
                                                    'table_name': a.get('table_name'),
                                                    'client_name': _isis_client_name_of(a)}}
    if 'repon' in text or 'reponer' in text or 'surte' in text:
        p = _isis_find_product(db, user, text)
        if not p:
            return {'action': 'unknown', 'params': {}, 'question': '¿Qué producto quieres reponer? Dime su nombre.'}
        return {'action': 'restock', 'params': {'product_id': p.get('id'), 'product': p.get('name'), 'qty': _isis_qty(text), 'reason': 'Reposicion por voz con Isis'}}
    if 'ajusta' in text and ('inventario' in text or 'existencias' in text):
        p = _isis_find_product(db, user, text)
        if not p:
            return {'action': 'unknown', 'params': {}, 'question': '¿De qué producto ajusto el inventario?'}
        amt = _isis_money(text)
        if amt is None:
            return {'action': 'unknown', 'params': {}, 'question': '¿En cuánto dejo ese producto?'}
        return {'action': 'inventory_adjust', 'params': {'product_id': p.get('id'), 'product': p.get('name'), 'qty': int(amt), 'mode': 'count', 'reason': 'Ajuste por voz con Isis'}}
    # v11 (robustez): 'súmale' también es pedir, igual que 'agrega'/'pon'.
    if ('agrega' in text or 'anade' in text or 'pon' in text or 'pide' in text or 'sumale' in text) and 'mesa' in text:
        a, missing = _isis_resolve_account(db, user, text, '¿A qué mesa le agrego el pedido?')
        if missing:
            return missing
        p = _isis_find_product(db, user, text)
        if not p:
            amb = _isis_ambiguous_product(db, user, text)
            return {'action': 'unknown', 'params': {},
                    'question': amb or '¿Qué producto le agrego a esa mesa?'}
        item = {'product_id': p.get('id'), 'product': p.get('name'),
                'qty': _isis_qty_for_product(text, p)}
        note = _isis_parse_order_note(text)
        if note:
            item['note'] = note
        return {'action': 'add_order', 'params': {'account_id': a.get('id'), 'items': [item],
                                                  'table_name': a.get('table_name'),
                                                  'client_name': _isis_client_name_of(a)}}
    # v11: quitar un producto de una cuenta ("quítale la corona a la mesa 5",
    # "cancela la corona de la mesa de Juanita"). La app no tiene un
    # endpoint directo: la cancelación es solicitud + decisión del
    # propietario, y eso es justo lo que hace _isis_execute (Isis actúa
    # como propietaria). 'cancela la cuenta' queda fuera: eso es anular.
    if (('quitale' in text or 'quita' in text or ('cancela' in text and 'cuenta' not in text))
            and ('mesa' in text or 'cuenta' in text)):
        a, missing = _isis_resolve_account(db, user, text, '¿De qué mesa quito el producto?')
        if missing:
            return missing
        p = _isis_find_product(db, user, text)
        if not p:
            amb = _isis_ambiguous_product(db, user, text)
            return {'action': 'unknown', 'params': {},
                    'question': amb or '¿Qué producto quito de esa mesa?'}
        qty = _isis_qty_for_product(text, p)
        item, _ready = _isis_find_account_item(db, a.get('id'), p.get('id'))
        label = _isis_describe_account({'account_id': a.get('id'), 'table_name': a.get('table_name'),
                                        'client_name': _isis_client_name_of(a)})
        if not item:
            return {'action': 'chat', 'params': {'answer': f"No hay {p.get('name')} en {label}, no quité nada."}}
        if qty > item['qty']:
            return {'action': 'chat', 'params': {'answer': f"Solo quedan {item['qty']} de {p.get('name')} en {label}."}}
        return {'action': 'cancel_item', 'params': {'account_id': a.get('id'), 'item_id': item['id'],
                                                    'product': p.get('name'), 'qty': qty,
                                                    'table_name': a.get('table_name'),
                                                    'client_name': _isis_client_name_of(a)}}
    if ('entrada' in text or 'salida' in text) and ('efectivo' in text or 'caja' in text or '$' in text or _isis_money(text) is not None):
        amt = _isis_money(text)
        if amt is None:
            return {'action': 'unknown', 'params': {}, 'question': '¿De cuánto es el movimiento de caja?'}
        return {'action': 'cash_movement', 'params': {'kind': 'out' if 'salida' in text else 'in', 'amount': amt, 'reason': raw.strip()[:200]}}
    return None

# Palabras que delatan una pregunta de criterio (v4): ahí Claude opina y
# recomienda en vez de solo clasificar, con un poco más de libertad.
_ISIS_ANALYSIS_WORDS = ('recomiendas', 'recomienda', 'que hago', 'que me aconsejas',
                        'como va', 'como ves', 'que opinas', 'conviene', 'consejo',
                        'analiza', 'analizame', 'que harias', 'te parece', 'como andamos')


def _isis_call_fableai(message, context):
    """Llama a Claude via FableAI y devuelve un intent, o None si no se puede."""
    if not FABLEAI_API_KEY:
        return None
    # Criterio (v4): en preguntas de recomendación/opinión Claude razona con
    # temperature 0.3 y más tokens; las acciones concretas siguen exactas (0).
    is_analysis = any(w in _isis_norm(message) for w in _ISIS_ANALYSIS_WORDS)
    payload = {'model': FABLEAI_MODEL, 'temperature': 0.3 if is_analysis else 0,
               'max_tokens': 900 if is_analysis else 600,
               'messages': [{'role': 'system', 'content': _ISIS_SYSTEM},
                            {'role': 'user', 'content': 'Contexto del restaurante (mesas, productos y cuentas abiertas, resumido):\n' + context + '\n\nOrden del usuario: ' + message}]}
    req = urllib.request.Request(FABLEAI_URL, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + FABLEAI_API_KEY},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        content = data['choices'][0]['message']['content']
        start = content.find('{')
        end = content.rfind('}')
        if start < 0 or end < 0:
            return None
        parsed = json.loads(content[start:end + 1])
        if not isinstance(parsed, dict) or 'action' not in parsed:
            return None
        parsed.setdefault('params', {})
        return parsed
    except Exception:
        return None

def _isis_sales_context(db, user):
    """Ventas de hoy, caja y pendientes, para que Isis conteste con datos reales (v3).
    Cada sección con su propio try/except: un fallo no rompe el contexto."""
    parts = []
    try:
        today = datetime.now(timezone.utc).date()
        d = mitote_extensions.owner_analytics(start=today, end=today, scope='current', db=db, user=user)
        top = d.get('products') or []
        top_txt = ', '.join(f"{x.get('name')} ({x.get('quantity')})" for x in top[:3]) or 'nada todavía'
        parts.append(f"Ventas de hoy: {d.get('sales')} pesos en {_isis_n(d.get('paid_accounts'), 'cuenta pagada', 'cuentas pagadas')}, "
                     f"ticket promedio {d.get('average_ticket')} pesos. Lo más vendido hoy: {top_txt}.")
    except Exception:
        parts.append('Ventas de hoy: no disponible.')
    try:
        c = cash(db, user)
        if c.get('open'):
            parts.append(f"Caja abierta, efectivo esperado {c.get('expected')} pesos (efectivo {c.get('cash')} pesos, "
                         f"tarjeta {c.get('card')} pesos, transferencia {c.get('transfer')} pesos).")
        else:
            parts.append('Caja cerrada, no hay turno abierto.')
    except Exception:
        parts.append('Caja: no disponible.')
    try:
        accs = accounts(db, user)
        pend = [x for x in accs if x.get('status') in ('open', 'pending')]
        total = sum(float(x.get('due') or 0) for x in pend)
        parts.append(f"Cuentas pendientes de cobro: {len(pend)}, total por cobrar {total:.2f} pesos.")
    except Exception:
        parts.append('Cuentas pendientes: no disponible.')
    return ' '.join(parts)

_ISIS_LOW_STOCK = 5  # existencias a las que un producto se considera "por acabarse"


def _isis_parse_dt(value):
    """Fecha ISO (de account_dict/reservation_dict) a datetime con zona; None si no se puede."""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _isis_sold_today(db, user):
    """Productos vendidos hoy (por fecha, scope history): lista por cantidad desc.
    'Hoy' es el día local del restaurante, no el día UTC (de noche son días distintos)."""
    try:
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        today = datetime.now(local_tz).date()
        d = mitote_extensions.owner_analytics(start=today, end=today, scope='history', db=db, user=user)
        return d.get('products') or []
    except Exception:
        return []


_DIAS_SEMANA = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']


def _isis_week_trend(db, user):
    """Tendencia de los últimos 7 días (v7): total vendido por día, mejor día
    y comparación con la semana anterior. Solo lectura, con datos reales."""
    local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
    today = datetime.now(local_tz).date()
    start = today - timedelta(days=6)
    d = mitote_extensions.owner_analytics(start=start, end=today, scope='history', db=db, user=user)
    totals = []
    for row in (d.get('sales_by_day') or []):
        try:
            day = datetime.fromisoformat(str(row.get('label'))).date()
            totals.append((day, float(row.get('amount') or 0)))
        except Exception:
            continue
    if not totals:
        return None
    total_week = sum(v for _, v in totals)
    best_day, best_val = max(totals, key=lambda x: x[1])
    per_day = ', '.join(f"{_DIAS_SEMANA[day.weekday()]} {v:.0f}" for day, v in totals)
    d_prev = mitote_extensions.owner_analytics(start=start - timedelta(days=7), end=today - timedelta(days=7),
                                               scope='history', db=db, user=user)
    prev_total = sum(float(r.get('amount') or 0) for r in (d_prev.get('sales_by_day') or []))
    if prev_total > 0:
        diff = total_week - prev_total
        if diff > 0:
            comp = f"Esta semana va {diff:.0f} pesos mejor que la semana pasada."
        elif diff < 0:
            comp = f"Esta semana va {abs(diff):.0f} pesos peor que la semana pasada."
        else:
            comp = 'Esta semana va igual que la semana pasada.'
    elif total_week > 0:
        comp = 'La semana pasada no hubo ventas registradas.'
    else:
        comp = 'No hubo ventas ni esta semana ni la pasada.'
    return (f"Tendencia de los últimos 7 días: {per_day}. Total de la semana: {total_week:.0f} pesos. "
            f"Mejor día: {_DIAS_SEMANA[best_day.weekday()]} ({best_val:.0f} pesos). {comp}")


def _isis_morning_brief(db, user):
    """Resumen hablado al entrar (v8): cómo cerró el día anterior, en 3
    frases cortas como mucho. Total vendido ayer, lo más vendido y lo que
    se acabó o anda bajo de inventario. Solo datos reales: si no hubo
    ventas ayer, se dice así, sin inventar."""
    parts = []
    sold_ids = set()
    try:
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        yday = datetime.now(local_tz).date() - timedelta(days=1)
        d = mitote_extensions.owner_analytics(start=yday, end=yday, scope='history', db=db, user=user)
        sales = float(d.get('sales') or 0)
        paid = int(d.get('paid_accounts') or 0)
        top = [x.get('name') for x in (d.get('products') or [])[:3] if x.get('name')]
        sold_ids = {x.get('product_id') for x in (d.get('products') or [])}
        if sales > 0 or paid > 0:
            parts.append(f"Ayer vendimos {_isis_pesos(sales)} en {_isis_n(paid, 'cuenta pagada', 'cuentas pagadas')}.")
            if top:
                parts.append('Lo más vendido: ' + ', '.join(top) + '.')
        else:
            parts.append('Ayer no hubo ventas registradas.')
    except Exception:
        parts.append('No pude ver cómo cerró ayer.')
    try:
        prods = products(db, user)
        out = [p.get('name') for p in prods
               if p.get('inventory_enabled') and p.get('id') in sold_ids
               and p.get('stock') is not None and float(p.get('stock') or 0) <= 0]
        low = [p for p in prods
               if p.get('inventory_enabled') and p.get('id') in sold_ids
               and p.get('stock') is not None and 0 < float(p.get('stock') or 0) <= _ISIS_LOW_STOCK]
        if out or low:
            bits = []
            if out:
                bits.append('se acabó la ' + ', '.join(out[:3]))
            if low:
                bits.append('quedan pocos de ' + ', '.join(f"{p.get('name')} ({p.get('stock')})" for p in low[:3]))
            sent = ' y '.join(bits) + '.'
            parts.append(sent[0].upper() + sent[1:])
    except Exception:
        pass
    return ' '.join(parts)


def _isis_decision_context(db, user):
    """Para decidir (v4): comparaciones y riesgos para que Claude recomiende con criterio.
    Cada sección con su propio try/except: un fallo no rompe el contexto."""
    parts = []
    try:
        trend = _isis_week_trend(db, user)
        parts.append(trend if trend else 'Tendencia de la semana: no disponible.')
    except Exception:
        parts.append('Tendencia de la semana: no disponible.')
    try:
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        today = datetime.now(local_tz).date()
        d_today = mitote_extensions.owner_analytics(start=today, end=today, scope='history', db=db, user=user)
        d_yday = mitote_extensions.owner_analytics(start=today - timedelta(days=1), end=today - timedelta(days=1), scope='history', db=db, user=user)
        s_t = float(d_today.get('sales') or 0)
        s_y = float(d_yday.get('sales') or 0)
        diff = s_t - s_y
        if diff > 0:
            comp = f"{diff:.2f} pesos más que ayer"
        elif diff < 0:
            comp = f"{abs(diff):.2f} pesos menos que ayer"
        else:
            comp = 'igual que ayer'
        parts.append(f"Ventas: hoy {s_t:.2f} pesos en {_isis_n(d_today.get('paid_accounts'), 'cuenta pagada', 'cuentas pagadas')}, ayer {s_y:.2f} pesos. Hoy va {comp}.")
    except Exception:
        parts.append('Ventas hoy vs ayer: no disponible.')
    try:
        sold = _isis_sold_today(db, user)
        if sold:
            top_txt = ', '.join(f"{x.get('name')} ({x.get('quantity')})" for x in sold[:3])
            bottom = sold[-3:] if len(sold) > 3 else []
            bottom_txt = ', '.join(f"{x.get('name')} ({x.get('quantity')})" for x in bottom)
            parts.append(f"Más vendidos hoy: {top_txt}." + (f" Menos vendidos hoy: {bottom_txt}." if bottom_txt else ''))
        else:
            parts.append('Hoy no se ha vendido nada todavía.')
    except Exception:
        parts.append('Más/menos vendidos: no disponible.')
    try:
        sold = _isis_sold_today(db, user)
        sold_ids = {x.get('product_id') for x in sold}
        prods = products(db, user)
        low = [p for p in prods if p.get('inventory_enabled') and p.get('id') in sold_ids
               and p.get('stock') is not None and float(p.get('stock') or 0) <= _ISIS_LOW_STOCK]
        if low:
            parts.append('Se venden y están por acabarse: ' + ', '.join(f"{p.get('name')} (quedan {p.get('stock')})" for p in low[:5]) + '.')
        else:
            parts.append('Ningún producto vendido hoy está por acabarse.')
    except Exception:
        parts.append('Productos por acabarse: no disponible.')
    try:
        accs = accounts(db, user)
        now = datetime.now(timezone.utc)
        old = []
        for a in accs:
            if a.get('status') not in ('open', 'pending'):
                continue
            created = _isis_parse_dt(a.get('created_at'))
            if not created:
                continue
            hours = (now - created).total_seconds() / 3600
            if hours > 2:
                old.append(f"{a.get('table_name') or a.get('name')} debe {a.get('due')} pesos, abierta hace {hours:.1f} h")
        parts.append('Cuentas abiertas hace más de 2 horas: ' + ('; '.join(old) if old else 'ninguna') + '.')
    except Exception:
        parts.append('Cuentas viejas: no disponible.')
    try:
        tables = dining_tables(db, user)
        free = sum(1 for t in tables if t.get('status') == 'available' and t.get('active'))
        occ = sum(1 for t in tables if t.get('status') == 'occupied' and t.get('active'))
        parts.append(f"Mesas: {free} libres, {occ} ocupadas.")
    except Exception:
        parts.append('Mesas libres/ocupadas: no disponible.')
    try:
        pend = db.scalars(select(Approval).where(Approval.status == 'pending')).all()
        if pend:
            parts.append('Autorizaciones pendientes: ' + ', '.join(f"#{a.id} {a.kind} (cuenta {a.account_id})" for a in pend[:5]) + '.')
        else:
            parts.append('Sin autorizaciones pendientes.')
    except Exception:
        parts.append('Autorizaciones pendientes: no disponible.')
    try:
        rows = mitote_extensions.reservations(db, user)
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        today_local = datetime.now(local_tz).date()
        pend_res = [r for r in rows if r.get('status') in ('pending', 'confirmed')
                    and _isis_parse_dt(r.get('scheduled_at')) is not None
                    and _isis_parse_dt(r.get('scheduled_at')).astimezone(local_tz).date() == today_local]
        if pend_res:
            parts.append('Reservaciones de hoy pendientes: ' + ', '.join(f"{r.get('name')} ({r.get('party_size')} personas)" for r in pend_res[:5]) + '.')
        else:
            parts.append('Sin reservaciones pendientes para hoy.')
    except Exception:
        parts.append('Reservaciones de hoy: no disponible.')
    return ' '.join(parts)


def _isis_day_close(db, user):
    """Cierre del día hablado (v11): ventas de hoy por método, ticket
    promedio, lo más vendido, mejor mesero por ventas, cuentas abiertas y
    estado de caja. Solo datos reales; si no hubo ventas, se dice así."""
    local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
    today = datetime.now(local_tz).date()
    try:
        d = mitote_extensions.owner_analytics(start=today, end=today, scope='history', db=db, user=user)
    except Exception:
        d = {}
    sales = float(d.get('sales') or 0)
    paid = int(d.get('paid_accounts') or 0)
    methods = d.get('methods') or {}
    avg = float(d.get('average_ticket') or 0)
    top = [x.get('name') for x in (d.get('products') or [])[:1] if x.get('name')]
    best = None
    try:
        begin = datetime(today.year, today.month, today.day, tzinfo=local_tz).astimezone(timezone.utc)
        finish = begin + timedelta(days=1)
        rows = db.execute(
            select(Account.owner_id, func.coalesce(func.sum(Payment.cents), 0))
            .join(Payment, Payment.account_id == Account.id)
            .where(Payment.created_at >= begin, Payment.created_at < finish)
            .group_by(Account.owner_id)).all()
        if rows:
            uid, cents_ = max(rows, key=lambda r: r[1])
            if int(cents_ or 0) > 0:
                u = db.get(User, uid)
                best = (u.display if u else '?', float(cents_) / 100)
    except Exception:
        best = None
    try:
        accs = accounts(db, user)
        pend = [x for x in accs if x.get('status') in ('open', 'pending')]
        due = sum(float(x.get('due') or 0) for x in pend)
    except Exception:
        pend, due = [], 0
    try:
        c = cash(db, user)
        cash_txt = (f"En caja deberían haber {_isis_pesos(c.get('expected'))}."
                    if c.get('open') else "La caja está cerrada.")
    except Exception:
        cash_txt = ''
    if sales <= 0 and paid == 0:
        parts = ['Hoy no hay ventas registradas todavía.']
    else:
        parts = [f"Hoy llevamos {_isis_pesos(sales)} en {_isis_n(paid, 'cuenta pagada', 'cuentas pagadas')}: "
                 f"{_isis_pesos(methods.get('cash'))} en efectivo, {_isis_pesos(methods.get('card'))} con tarjeta "
                 f"y {_isis_pesos(methods.get('transfer'))} por transferencia. "
                 f"Ticket promedio de {_isis_pesos(avg)}."]
        if top:
            parts.append(f"Lo más vendido: {top[0]}.")
        if best:
            parts.append(f"El mejor mesero fue {best[0]}, con {_isis_pesos(best[1])}.")
    if pend:
        parts.append(f"Quedan {_isis_n(len(pend), 'cuenta abierta', 'cuentas abiertas')} por cobrar, {_isis_pesos(due)}.")
    else:
        parts.append('No quedan cuentas abiertas.')
    if cash_txt:
        parts.append(cash_txt)
    return ' '.join(parts)


def _isis_shopping_list(db, user):
    """Lista de compras automática (v11): productos con inventario activado
    y stock bajo (mismo umbral que la lectura de inventario) + cuánto se
    vendió hoy de cada uno. Solo datos: producto, cuánto queda y cuánto se
    vendió hoy. Nunca inventa cantidades sugeridas."""
    try:
        prods = products(db, user)
        low = [x for x in prods if x.get('inventory_enabled') and (x.get('stock') or 0) <= 3]
    except Exception:
        return 'No pude revisar el inventario.'
    if not low:
        return 'Nada, todo tiene más de 3 en existencia.'
    sold = {x.get('product_id'): x.get('quantity') for x in _isis_sold_today(db, user)}
    items = []
    for x in low:
        s = f"{x.get('name')}: quedan {x.get('stock')}"
        qty_sold = sold.get(x.get('id'))
        if qty_sold:
            s += f", hoy se vendieron {qty_sold}"
        items.append(s)
    return 'Hay que pedir: ' + _isis_short_list(items, len(items)) + '.'


def _isis_oldest_ticket(db, user):
    """La comanda más atrasada (v11): la pendiente más antigua, con tiempo
    transcurrido en hora local, mesa y cantidad de productos."""
    try:
        rows = db.scalars(select(StationTicket).where(
            StationTicket.status != 'ready').order_by(StationTicket.created_at)).all()
    except Exception:
        return 'No pude revisar las comandas.'
    for t in rows:
        batch = db.get(Batch, t.batch_id)
        if not batch:
            continue
        acc = db.get(Account, batch.account_id)
        if not acc or acc.status not in ('open', 'pending'):
            continue
        items = db.scalars(select(OrderItem).where(OrderItem.batch_id == batch.id)).all()
        qty = sum(max(0, int(i.qty or 0) - int(i.cancelled_qty or 0)) for i in items)
        if qty <= 0:
            continue
        created = t.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        mins = int((now() - created).total_seconds() // 60)
        if mins < 0:
            mins = 0
        if mins < 60:
            tiempo = _isis_n(mins, 'minuto', 'minutos')
        else:
            tiempo = _isis_n(mins // 60, 'hora', 'horas')
            if mins % 60:
                tiempo += f" con {_isis_n(mins % 60, 'minuto', 'minutos')}"
        occ = db.scalar(select(TableOccupancy).where(TableOccupancy.account_id == acc.id))
        table = db.get(DiningTable, occ.table_id) if occ else None
        tname = table.name if table else acc.name
        station = 'cocina' if t.station == 'kitchen' else 'barra'
        return (f"La comanda más atrasada es la de {tname}, de {station}: lleva {tiempo}, "
                f"son {_isis_n(qty, 'producto', 'productos')}.")
    return 'No hay comandas pendientes ahorita.'


def _isis_shift_handover(db, user):
    """Resumen para el cambio de turno (v11): qué le deja Isis al siguiente
    turno. Corto: cuentas abiertas con antigüedad y saldo, pendientes
    (autorizaciones y reservaciones de hoy), stock bajo, caja y ventas del
    día hasta ahora. Solo datos reales."""
    parts = []
    try:
        accs = accounts(db, user)
        pend = [x for x in accs if x.get('status') in ('open', 'pending')]
        if pend:
            items = []
            for a in pend:
                created = _isis_parse_dt(a.get('created_at'))
                age = ''
                if created:
                    h = (now() - created).total_seconds() / 3600
                    age = f", hace {h:.0f} h" if h >= 1 else ', hace menos de una hora'
                items.append(f"{a.get('table_name') or a.get('name')} debe {_isis_pesos(a.get('due'))}{age}")
            parts.append('Cuentas abiertas: ' + _isis_short_list(items, len(items)) + '.')
        else:
            parts.append('No hay cuentas abiertas.')
    except Exception:
        parts.append('Cuentas abiertas: no disponible.')
    try:
        pend_ap = [r for r in requests(db, user) if r.get('status') == 'pending']
        rows = mitote_extensions.reservations(db, user)
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        today_local = datetime.now(local_tz).date()
        pend_res = [r for r in rows if r.get('status') in ('pending', 'confirmed')
                    and _isis_parse_dt(r.get('scheduled_at')) is not None
                    and _isis_parse_dt(r.get('scheduled_at')).astimezone(local_tz).date() == today_local]
        bits = []
        if pend_ap:
            bits.append(f"{len(pend_ap)} autorizaciones pendientes")
        if pend_res:
            bits.append(f"{len(pend_res)} reservaciones para hoy")
        parts.append('Pendientes: ' + ('; '.join(bits) + '.' if bits else 'nada.'))
    except Exception:
        parts.append('Pendientes: no disponible.')
    try:
        prods = products(db, user)
        low = [x for x in prods if x.get('inventory_enabled') and (x.get('stock') or 0) <= 3]
        if low:
            parts.append('Stock bajo: ' + _isis_short_list(
                [f"{x.get('name')} ({x.get('stock')})" for x in low], len(low)) + '.')
        else:
            parts.append('Stock en orden.')
    except Exception:
        parts.append('Stock: no disponible.')
    try:
        c = cash(db, user)
        if c.get('open'):
            parts.append(f"Caja abierta, en caja deberían haber {_isis_pesos(c.get('expected'))}.")
        else:
            parts.append('La caja está cerrada.')
    except Exception:
        parts.append('Caja: no disponible.')
    try:
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        today = datetime.now(local_tz).date()
        d = mitote_extensions.owner_analytics(start=today, end=today, scope='history', db=db, user=user)
        sales = float(d.get('sales') or 0)
        paid = int(d.get('paid_accounts') or 0)
        if sales > 0 or paid > 0:
            parts.append(f"Hoy llevamos {_isis_pesos(sales)} en {_isis_n(paid, 'cuenta pagada', 'cuentas pagadas')}.")
        else:
            parts.append('Hoy no hay ventas registradas todavía.')
    except Exception:
        parts.append('Ventas de hoy: no disponible.')
    return ' '.join(parts)


def _isis_alerts(db, user):
    """Hasta 3 alertas cortas de lo que necesita atención AHORA (v4). Solo lectura."""
    alerts = []
    try:
        c = cash(db, user)
        if not c.get('open'):
            alerts.append('La caja todavía no está abierta.')
    except Exception:
        pass
    try:
        accs = accounts(db, user)
        now = datetime.now(timezone.utc)
        for a in accs:
            if a.get('status') not in ('open', 'pending'):
                continue
            created = _isis_parse_dt(a.get('created_at'))
            if not created:
                continue
            hours = int((now - created).total_seconds() // 3600)
            if hours > 2:
                alerts.append(f"La {a.get('table_name') or a.get('name')} lleva {hours} {'hora' if hours == 1 else 'horas'} abierta y debe {_isis_pesos(a.get('due'))}.")
    except Exception:
        pass
    try:
        # v11: reservaciones próximas (en los próximos 60 minutos). Van
        # antes que el stock bajo: hay que preparar la mesa. Como mucho 2,
        # para que un solo tipo no se coma las 3 alertas.
        rows = mitote_extensions.reservations(db, user)
        local_tz = getattr(mitote_extensions, 'LOCAL_TZ', timezone.utc)
        now_local = datetime.now(local_tz)
        upcoming = []
        for r in rows:
            if r.get('status') not in ('pending', 'confirmed'):
                continue
            when = _isis_parse_dt(r.get('scheduled_at'))
            if not when:
                continue
            mins = (when.astimezone(local_tz) - now_local).total_seconds() / 60
            if 0 <= mins <= 60:
                upcoming.append((mins, r))
        upcoming.sort(key=lambda x: x[0])
        for mins, r in upcoming[:2]:
            if mins < 1:
                cuando = 'ahorita mismo'
            elif mins < 60:
                cuando = f"en {int(mins)} minutos"
            else:
                cuando = 'en una hora'
            alerts.append(f"Reservación de {r.get('name')} ({r.get('party_size')} personas) {cuando}.")
    except Exception:
        pass
    try:
        sold = _isis_sold_today(db, user)
        sold_ids = {x.get('product_id') for x in sold}
        prods = products(db, user)
        for p in prods:
            if p.get('inventory_enabled') and p.get('id') in sold_ids and p.get('stock') is not None and float(p.get('stock') or 0) <= _ISIS_LOW_STOCK:
                alerts.append(f"{p.get('name')} se está vendiendo y solo quedan {p.get('stock')}.")
    except Exception:
        pass
    try:
        pend = db.scalars(select(Approval).where(Approval.status == 'pending')).all()
        if pend:
            alerts.append(f"Hay {len(pend)} {'autorización' if len(pend) == 1 else 'autorizaciones'} pendientes.")
    except Exception:
        pass
    return alerts[:3]


def _isis_context(db, user):
    try:
        tables = dining_tables(db, user)
        t_txt = ', '.join(f"{t.get('name')} ({t.get('status')})" for t in tables[:20])
    except Exception:
        t_txt = 'no disponible'
    try:
        prods = products(db, user)
        p_txt = ', '.join(f"{p.get('name')} (stock {p.get('stock')})" for p in prods if p.get('inventory_enabled'))[:1500]
    except Exception:
        p_txt = 'no disponible'
    try:
        accs = accounts(db, user)
        a_txt = ', '.join(f"#{a.get('id')} {a.get('name')} mesa {a.get('table_name')} debe {a.get('due')} pesos" for a in accs if a.get('status') in ('open', 'pending'))[:1500]
    except Exception:
        a_txt = 'no disponible'
    # Memoria que le dictaron + resumen del día, para que responda con contexto (v2).
    try:
        mems = _isis_memory_all(db)
        m_txt = '; '.join(f"{k}: {v}" for k, v in mems) if mems else 'ninguna'
    except Exception:
        m_txt = 'no disponible'
    try:
        s_txt = _isis_sales_context(db, user)
    except Exception:
        s_txt = 'Ventas y caja: no disponible.'
    try:
        d_txt = _isis_decision_context(db, user)
    except Exception:
        d_txt = 'Para decidir: no disponible.'
    return (f"Moneda: todos los montos son en pesos mexicanos (MXN).\n"
            f"Mesas: {t_txt}\nProductos con inventario: {p_txt}\nCuentas abiertas: {a_txt}\n"
            f"Memoria de Isis: {m_txt}\nResumen del día: {_isis_day_summary(db, user)}\n"
            f"Ventas y caja: {s_txt}\nPara decidir: {d_txt}")

_READ_ACTIONS = {'tables', 'overview', 'cash', 'cash_history', 'inventory', 'inventory_movements',
                 'account_detail', 'accounts_list', 'tickets_list', 'cashier_overview',
                 'menu_list', 'product_price', 'staff_list', 'tips_pool', 'approvals_list', 'audit_list',
                 'reservations_list', 'prep_config', 'analytics', 'recall', 'chat',
                 'client_info', 'cash_cut_start', 'split_account_start', 'todo_list',
                 'day_close', 'shift_handover', 'shopping_list', 'oldest_ticket'}

_WRITE_ACTIONS = {'open_cash', 'close_cash', 'create_table', 'update_table', 'new_account',
                  'add_order', 'pay_account', 'close_account', 'transfer_account', 'restock',
                  'inventory_adjust', 'create_inventory_item', 'void_account', 'cash_movement',
                  'ticket_status', 'apply_discount', 'create_product', 'archive_product',
                  'add_variant', 'add_promotion', 'edit_product_price', 'create_staff',
                  'staff_pin', 'tips_distribute', 'decide_approval', 'create_reservation',
                  'confirm_reservation', 'reservation_arrive', 'prep_config_set',
                  'product_prep_set', 'remember', 'forget', 'merge_accounts', 'split_account',
                  'reopen_closed', 'tip_payout', 'todo_add', 'todo_remove',
                  'pay_and_close_account', 'cancel_item'}

def _isis_describe(intent):
    """Resumen de lo que Isis propone hacer, para pedir confirmación por voz.
    En primera persona y en pesos: antes decía 'Cobrar $200 con cash' y
    'Agregar al menú X a $50 (kitchen)', con el método y la estación en
    inglés y el '$' que al oírse se puede confundir con otra moneda."""
    a, p = intent.get('action'), intent.get('params') or {}
    mesa = lambda n: 'la ' + _isis_mesa_txt(n)
    if a == 'open_cash':
        return f"Abro el turno con {_isis_pesos(p.get('opening'))} de fondo."
    if a == 'close_cash':
        return f"Cierro el turno con {_isis_pesos(p.get('counted'))} contados."
    if a == 'create_table':
        return f"Creo {mesa(p.get('name'))}."
    if a == 'new_account':
        return f"Abro una cuenta en {mesa(p.get('table_name'))}."
    if a == 'add_order':
        def _it(i):
            t = f"{i.get('qty')} {i.get('product')}"
            if i.get('note'):
                t += f", nota: {i.get('note')}"
            return t
        items = ', '.join(_it(i) for i in p.get('items') or [])
        return f"A {_isis_describe_account(p)} le agrego: {items}."
    if a == 'pay_account':
        amt = 'todo el saldo' if p.get('all') else _isis_pesos(p.get('amount'))
        metodo = {'cash': 'en efectivo', 'card': 'con tarjeta',
                  'transfer': 'por transferencia'}.get(p.get('method'))
        return f"Cobro {amt} de {_isis_describe_account(p)}" + (f" {metodo}." if metodo else '.')
    if a == 'close_account':
        return f"Cierro {_isis_describe_account(p)}."
    if a == 'restock':
        return f"Repongo {p.get('qty')} de {p.get('product')}."
    if a == 'inventory_adjust':
        return f"Dejo el inventario de {p.get('product')} en {p.get('qty')}."
    if a == 'void_account':
        return f"Anulo la cuenta {p.get('account_id')}."
    if a == 'cash_movement':
        return f"Registro una {'salida' if p.get('kind') == 'out' else 'entrada'} de {_isis_pesos(p.get('amount'))} en caja."
    if a == 'remember':
        if str(p.get('key') or '').startswith('cliente:'):
            name = str(p.get('key'))[len('cliente:'):]
            t, habit = _isis_client_parse(p.get('value'))
            return _isis_client_describe(name, t, habit)
        return f"Anoto en mi memoria: {p.get('key')}, {p.get('value')}."
    if a == 'forget':
        return f"Olvido lo de '{p.get('query')}'."
    if a == 'update_table':
        return f"Dejo {mesa(p.get('table_name'))} para {_isis_n(p.get('capacity'), 'persona', 'personas')}."
    if a == 'transfer_account':
        return f"Muevo la cuenta {p.get('account_id')} a {mesa(p.get('table_name'))}."
    if a == 'ticket_status':
        estado = {'received': 'recibida', 'preparing': 'en preparación', 'ready': 'lista'}.get(p.get('status'), p.get('status'))
        return f"Marco la comanda {p.get('ticket_id')} como {estado}."
    if a == 'apply_discount':
        v = f"del {p.get('value')}%" if p.get('mode') == 'percent' else f"de {_isis_pesos(p.get('value'))}"
        return f"Aplico un descuento {v} a la cuenta {p.get('account_id')}."
    if a == 'create_product':
        estacion = {'kitchen': 'en cocina', 'bar': 'en barra'}.get(p.get('station'), '')
        return f"Agrego al menú {p.get('name')} a {_isis_pesos(p.get('price'))}" + (f", {estacion}." if estacion else '.')
    if a == 'archive_product':
        return f"Saco el producto {p.get('product_id')} del menú."
    if a == 'add_variant':
        return f"Agrego la presentación {p.get('name')}, de {_isis_pesos(p.get('price'))}, al producto {p.get('product_id')}."
    if a == 'add_promotion':
        return f"Pongo en promoción el producto {p.get('product_id')} con {p.get('percent')}% de descuento."
    if a == 'edit_product_price':
        return f"Cambio el precio del producto {p.get('product_id')} a {_isis_pesos(p.get('price'))}."
    if a == 'create_inventory_item':
        return f"Registro el insumo {p.get('name')} con {p.get('qty')} {p.get('unit')}."
    if a == 'create_staff':
        rol = {'waiter': 'mesero', 'cashier': 'caja', 'kitchen': 'cocina', 'bar': 'barra', 'admin': 'administrador'}.get(p.get('role'), p.get('role'))
        return f"Registro a {p.get('name')} como {rol}."
    if a == 'staff_pin':
        return f"Cambio el PIN del empleado {p.get('user_id')}."
    if a == 'tips_distribute':
        return "Reparto el fondo de propinas entre todos los empleados activos."
    if a == 'decide_approval':
        return f"{'Autorizo' if p.get('approve') else 'Rechazo'} la solicitud {p.get('request_id')}."
    if a == 'create_reservation':
        return f"Reservo para {p.get('name')}, {_isis_n(p.get('party_size'), 'persona', 'personas')}, {_isis_fecha_natural(p.get('scheduled_at'))}."
    if a == 'confirm_reservation':
        return f"Confirmo la reservación {p.get('reservation_id')}."
    if a == 'reservation_arrive':
        return f"Recibo la reservación {p.get('reservation_id')} y le abro su cuenta."
    if a == 'prep_config_set':
        return f"Pongo el límite de preparación en {p.get('batch_minutes')} minutos por comanda."
    if a == 'product_prep_set':
        return f"Pongo el tiempo de preparación de {p.get('product')} en {p.get('minutes')} minutos."
    # --- v11, acciones delicadas (doble confirmación) ---
    if a == 'merge_accounts':
        return f"Uno la cuenta de {mesa(p.get('source_table'))} con la de {mesa(p.get('target_table'))}: todo lo de la primera pasa a la segunda y la primera se cierra."
    if a == 'split_account':
        if p.get('target_account_id'):
            dest = f"a la cuenta de {mesa(p.get('target_table'))}"
        elif p.get('table_id'):
            dest = f"a una cuenta nueva en {mesa(p.get('target_table'))}"
        else:
            dest = "a una cuenta nueva"
        return f"Muevo {p.get('qty')} {p.get('product')} de la cuenta de {mesa(p.get('source_table'))} {dest}."
    if a == 'reopen_closed':
        extra = " La mesa está ocupada, así que la cuenta queda sin mesa." if p.get('table_occupied') else ""
        return f"Reabro la cuenta de {mesa(p.get('table_name'))}, que estaba cerrada como pagada.{extra}"
    if a == 'tip_payout':
        metodo = 'en efectivo' if p.get('method') == 'cash' else 'por transferencia'
        return f"Le pago a {p.get('employee')} sus {_isis_pesos(p.get('amount'))} de propina {metodo}."
    # --- v11: pendientes por voz ---
    if a == 'cancel_item':
        return f"Quito {p.get('qty')} {p.get('product')} de {_isis_describe_account(p)}."
    if a == 'todo_add':
        return f"Anoto pendiente: {p.get('text')}."
    if a == 'todo_remove':
        txt = _isis_short_list(p.get('texts') or [], len(p.get('texts') or []))
        return f"Borro de tus pendientes: {txt}."
    # --- v11: cobrar y cerrar en una sola frase (una sola confirmación) ---
    if a == 'pay_and_close_account':
        metodo = {'cash': 'en efectivo', 'card': 'con tarjeta',
                  'transfer': 'por transferencia'}.get(p.get('method'))
        if p.get('client_name'):
            return f"Cobro todo de {_isis_describe_account(p)} {metodo} y la cierro."
        return f"Cobro todo de {mesa(p.get('table_name'))} {metodo} y la cierro."
    return "Hago eso."

def _isis_confirm_token(intent, delicate=False, stage=1):
    token = secrets.token_urlsafe(24)
    _ISIS_CONFIRM[token] = {'intent': intent, 'expires': time.time() + 120,
                            'delicate': bool(delicate), 'stage': stage}
    return token


# Acciones delicadas (v11): unir/dividir cuentas, reabrir una cerrada y pagar
# la propina de una persona piden DOS confirmaciones: el primer "sí" solo
# repite los datos exactos y pregunta "¿Seguro, lo hago?", y solo el segundo
# "sí" ejecuta. Cualquier "no" cancela. El token expira a los 2 minutos,
# igual que el de confirmación normal.
_DELICATE_ACTIONS = {'merge_accounts', 'split_account', 'reopen_closed', 'tip_payout'}

async def _isis_execute(db, user, intent):
    """Ejecuta un intent reutilizando las funciones reales de la app."""
    a, p = intent.get('action'), intent.get('params') or {}
    if a == 'tables':
        rows = dining_tables(db, user)
        free = [t.get('name') for t in rows if t.get('status') == 'available']
        busy = [t.get('name') for t in rows if t.get('status') == 'occupied']
        free_txt = _isis_short_list(free, len(free)) if free else 'ninguna.'
        busy_txt = _isis_short_list(busy, len(busy)) if busy else 'ninguna.'
        return f"Mira, libres: {free_txt} Ocupadas: {busy_txt}"
    if a == 'overview':
        o = overview(db, user)
        c = cash(db, user)
        if not c.get('open'):
            return f"Hay {_isis_n(o.get('active_accounts', 0), 'cuenta activa', 'cuentas activas')} y {_isis_n(o.get('products', 0), 'producto', 'productos')} en el menú. La caja todavía no está abierta."
        return (f"Hay {_isis_n(o.get('active_accounts', 0), 'cuenta activa', 'cuentas activas')}. Del turno: {_isis_pesos(c.get('cash'))} en efectivo, "
                f"{_isis_pesos(c.get('card'))} con tarjeta y {_isis_pesos(c.get('transfer'))} por transferencia. "
                f"En caja deberían haber {_isis_pesos(c.get('expected'))}.")
    if a == 'cash':
        c = cash(db, user)
        if not c.get('open'):
            return "La caja todavía no está abierta."
        return (f"La caja está abierta. Llevamos {_isis_pesos(c.get('cash'))} en efectivo, {_isis_pesos(c.get('card'))} con tarjeta, "
                f"{_isis_pesos(c.get('transfer'))} por transferencia y {_isis_pesos(c.get('tips'))} de propinas. "
                f"En caja deberían haber {_isis_pesos(c.get('expected'))}.")
    if a == 'inventory':
        prods = products(db, user)
        if p.get('product'):
            match = next((x for x in prods if _isis_norm(x.get('name', '')) == _isis_norm(p.get('product'))), None)
            if not match:
                return f"No encontré {p.get('product')} en el inventario."
            return f"De {match.get('name')} quedan {match.get('stock')} {match.get('inventory_unit') or 'unidades'}."
        low = [x for x in prods if x.get('inventory_enabled') and (x.get('stock') or 0) <= 3]
        txt = _isis_short_list([f"{x.get('name')} ({x.get('stock')})" for x in low], len(low))
        return f"Ojo, andan bajos: {txt}" if low else "Nada, todo tiene más de 3 en existencia."
    if a == 'account_detail':
        d = account_detail(p.get('account_id'), db, user)
        label = d.get('table_name') if d.get('table_name') else d.get('name')
        if d.get('table_name') and d.get('name') and d.get('name') != d.get('table_name'):
            label = f"{d.get('table_name')}, de {d.get('name')}"
        reply = (f"La cuenta {d.get('id')}, {label}: en total son {_isis_pesos(d.get('total'))}, "
                 f"ya pagaron {_isis_pesos(d.get('paid'))} y faltan {_isis_pesos(d.get('due'))}.")
        # Cliente de siempre (v8): si esa mesa es de alguien conocido, se dice en corto.
        try:
            m = re.search(r'(\d+)', str(d.get('table_name') or ''))
            found = _isis_client_find(db, table=m.group(1)) if m else None
            if found:
                nm, _t, habit = found
                reply += f" Es {_isis_client_name_txt(nm)}, el de siempre." + (f" Siempre pide {habit}." if habit else '')
        except Exception:
            pass
        return reply
    if a == 'open_cash':
        r = await open_cash(OpenPeriodIn(opening=Decimal(str(p.get('opening')))), db, user)
        return f"Turno abierto, folio {r.get('id')}, con {_isis_pesos(p.get('opening'))} de fondo."
    if a == 'close_cash':
        r = await close_cash(ClosePeriodIn(counted=Decimal(str(p.get('counted'))), note=str(p.get('note') or '')), db, user)
        return f"Corte cerrado, folio {r.get('id')}, y ya quedó guardado con su PDF."
    if a == 'create_table':
        r = await create_dining_table(DiningTableIn(name=str(p.get('name')), area=str(p.get('area') or 'Salon'), capacity=int(p.get('capacity') or 4)), db, user)
        return f"La {p.get('name')} ya está creada."
    if a == 'new_account':
        r = await new_account(AccountIn(name=None, table_id=int(p.get('table_id')) if p.get('table_id') else None, client_key=secrets.token_urlsafe(16)), db, user)
        return f"Cuenta abierta en la {_isis_mesa_txt(p.get('table_name'))}, folio {r.get('id')}."
    if a == 'add_order':
        items = [OrderLine(product_id=int(i.get('product_id')), qty=int(i.get('qty') or 1),
                           note=str(i.get('note') or '')) for i in p.get('items') or []]
        r = await send_order(int(p.get('account_id')), OrderIn(items=items, client_key=secrets.token_urlsafe(16)), db, user)
        if p.get('client_name'):
            return f"Ya mandé la comanda a {_isis_describe_account(p)}, folio {r.get('batch_id')}."
        return f"Ya mandé la comanda, folio {r.get('batch_id')}."
    if a == 'pay_account':
        acc = account_detail(int(p.get('account_id')), db, user)
        # Cobrar exige cuenta en estado pendiente: si sigue abierta, se pide
        # la cuenta primero, como hace el mesero en la app normal.
        if acc.get('status') == 'open':
            await request_bill(int(p.get('account_id')), db, user)
            acc = account_detail(int(p.get('account_id')), db, user)
        due = Decimal(str(acc.get('due') or '0'))
        amt = due if p.get('all') else Decimal(str(p.get('amount')))
        if amt <= 0 or amt > due:
            raise HTTPException(409, 'El importe no coincide con el saldo de la cuenta.')
        lines = [PaymentLine(method=p.get('method'), amount=amt, tip=Decimal('0'))]
        r = await pay(int(p.get('account_id')), PaymentIn(client_key=secrets.token_urlsafe(16), lines=lines), db, user)
        due_rest = float(r.get('due') or 0)
        prefix = f"Pago registrado en {_isis_describe_account(p)}." if p.get('client_name') else "Pago registrado."
        if due_rest <= 0:
            return prefix + " Ya quedó pagada por completo."
        return prefix + f" Faltan {_isis_pesos(due_rest)} por pagar."
    if a == 'close_account':
        await close_account(int(p.get('account_id')), db, user)
        if p.get('client_name'):
            return f"{_isis_describe_account(p).capitalize()} ya quedó cerrada y pagada."
        return f"La cuenta {p.get('account_id')} ya quedó cerrada y pagada."
    # --- v11: cobrar y cerrar en una sola frase ---
    # Reutiliza la lógica de pay_account (pedir la cuenta primero si sigue
    # abierta) y luego close_account. Si el cobro falla, la excepción sube
    # y NO se cierra nada.
    if a == 'pay_and_close_account':
        acc = account_detail(int(p.get('account_id')), db, user)
        if acc.get('status') == 'open':
            await request_bill(int(p.get('account_id')), db, user)
            acc = account_detail(int(p.get('account_id')), db, user)
        due = Decimal(str(acc.get('due') or '0'))
        if due > 0:
            lines = [PaymentLine(method=p.get('method'), amount=due, tip=Decimal('0'))]
            await pay(int(p.get('account_id')),
                      PaymentIn(client_key=secrets.token_urlsafe(16), lines=lines), db, user)
        await close_account(int(p.get('account_id')), db, user)
        dest = _isis_describe_account(p) if p.get('client_name') else _isis_mesa_txt(p.get('table_name'))
        return _isis_say(f"Listo, cobré todo de {dest} y la cuenta quedó cerrada.", warm=True)
    if a == 'restock':
        prod = db.get(Product, int(p.get('product_id')))
        r = await restock(int(p.get('product_id')), RestockIn(delta=int(p.get('qty')), reason=str(p.get('reason') or 'Reposicion por voz'), expected_version=int(prod.version)), db, user)
        return f"Ya repuse, ahora quedan {r.get('stock')} de {p.get('product')}."
    if a == 'inventory_adjust':
        prod = db.get(Product, int(p.get('product_id')))
        r = await inventory_adjustment(int(p.get('product_id')), InventoryAdjustmentIn(mode=str(p.get('mode') or 'count'), quantity=int(p.get('qty')), reason=str(p.get('reason') or 'Ajuste por voz'), expected_version=int(prod.version)), db, user)
        return f"El inventario de {p.get('product')} ahora está en {r.get('stock')}."
    if a == 'void_account':
        await void_account(int(p.get('account_id')), VoidAccountIn(reason=str(p.get('reason') or 'Anulacion por voz')), db, user)
        return f"La cuenta {p.get('account_id')} quedó anulada."
    if a == 'cash_movement':
        r = await movement(MovementIn(kind=str(p.get('kind')), amount=Decimal(str(p.get('amount'))), reason=str(p.get('reason') or 'Movimiento por voz'), client_key=secrets.token_urlsafe(16)), db, user)
        return "Movimiento de caja registrado."
    # --- v2: memoria ---
    if a == 'remember':
        if str(p.get('key') or '').startswith('cliente:'):
            _isis_memory_set(db, p.get('key'), p.get('value'))
            name = str(p.get('key'))[len('cliente:'):]
            t, habit = _isis_client_parse(p.get('value'))
            return _isis_say(_isis_client_describe(name, t, habit) + ' No se me olvida.', warm=True)
        _isis_memory_set(db, p.get('key'), p.get('value'))
        return _isis_say(f"Anotado: {p.get('key')}, {p.get('value')}. No se me olvida.", warm=True)
    if a == 'client_info':
        found = _isis_client_find(db, name=p.get('name'), table=p.get('table'))
        if not found:
            if p.get('name'):
                return _isis_say(f"No tengo a {_isis_client_name_txt(p.get('name'))} anotado. No lo conozco.", warm=True)
            return "No tengo a nadie anotado en esa mesa."
        name, table, habit = found
        return _isis_say(f"Es {_isis_client_txt(name, table, habit)}", warm=True)
    if a == 'recall':
        q = (p.get('query') or '').strip()
        hits = _isis_memory_search(db, q) if q else _isis_memory_all(db)
        if not hits:
            return "No tengo nada guardado de eso. Si quieres, dime 'recuerda que...' y lo anoto."
        txt = _isis_short_list([f"{k}: {v}" for k, v in hits], len(hits))
        return _isis_say(f"De eso tengo guardado: {txt}", warm=True)
    if a == 'forget':
        q = (p.get('query') or '').strip()
        hits = _isis_memory_search(db, q)
        if not hits:
            return f"No encontré nada guardado de '{q}'."
        for k, _v in hits:
            _isis_memory_delete(db, k)
        return _isis_say(f"Ya olvidé lo de '{q}'.", warm=True)
    # --- v11: quitar un producto de una cuenta. La app no tiene endpoint
    # directo: la cancelación es solicitud + decisión del propietario, y
    # eso es lo que se hace aquí (Isis actúa como propietaria, con su
    # confirmación de voz previa). Sin devolver stock: el producto ya se
    # descontó al mandar la comanda y no se sabe si se preparó.
    if a == 'cancel_item':
        acc = db.get(Account, int(p.get('account_id')))
        if not acc:
            raise HTTPException(404, 'La cuenta ya no existe.')
        _total, paid = acc_totals(db, acc)
        if paid > 0:
            raise HTTPException(409, 'Cuenta con pagos: requiere ajuste administrativo especial')
        item = db.get(OrderItem, int(p.get('item_id')))
        if not item or int(item.qty or 0) - int(item.cancelled_qty or 0) < int(p.get('qty')):
            raise HTTPException(409, 'La cantidad ya cambió')
        r = await request_approval(int(p.get('account_id')),
                                   ApprovalIn(kind='cancel', item_id=int(p.get('item_id')),
                                              qty=int(p.get('qty')),
                                              reason='Cancelación por voz con Isis'),
                                   db, user)
        await decide(int(r.get('id')), DecisionIn(approve=True, return_stock=False), db, user)
        return f"Listo, quité {p.get('qty')} {p.get('product')} de {_isis_describe_account(p)}."
    # --- v11: pendientes por voz ---
    if a == 'todo_add':
        _isis_memory_set(db, p.get('key'), p.get('text'))
        return _isis_say(f"Anotado: {p.get('text')}. No se me olvida.", warm=True)
    if a == 'todo_list':
        items = _isis_todo_list(db)
        if not items:
            return "No tienes pendientes anotados. Todo en orden."
        txt = _isis_short_list([v for _k, v in items], len(items))
        return _isis_say(f"Tienes pendientes: {txt}", warm=True)
    if a == 'todo_remove':
        gone = [v for k, v in _isis_todo_list(db) if k in (p.get('keys') or [])]
        for k in (p.get('keys') or []):
            _isis_memory_delete(db, k)
        if not gone:
            return f"No encontré nada de '{p.get('query')}' en tus pendientes."
        txt = _isis_short_list(gone, len(gone))
        return _isis_say(f"Listo, borré: {txt}", warm=True)
    # --- v2: 1. Cuentas ---
    if a == 'accounts_list':
        rows = [x for x in accounts(db, user) if x.get('status') in ('open', 'pending')]
        if not rows:
            return "No hay cuentas abiertas ahorita. Todo tranquilo."
        txt = _isis_short_list([f"#{x.get('id')} mesa {x.get('table_name') or '?'} debe {_isis_pesos(x.get('due'))}" for x in rows], len(rows))
        return _isis_say(f"Hay {_isis_n(len(rows), 'cuenta abierta', 'cuentas abiertas')}: {txt}", warm=True)
    if a == 'transfer_account':
        await transfer_account_table(int(p.get('account_id')), AccountTableTransferIn(table_id=int(p.get('table_id')), reason='Cambio de mesa por voz con Isis'), db, user)
        return _isis_say(f"La cuenta {p.get('account_id')} ya está en la {_isis_mesa_txt(p.get('table_name'))}.", warm=True)
    # --- v11, acciones delicadas (doble confirmación) ---
    if a == 'merge_accounts':
        await merge_accounts(int(p.get('account_id')),
                             AccountMergeIn(target_account_id=int(p.get('target_account_id')),
                                            reason='Union por voz con Isis'), db, user)
        return _isis_say(f"Listo, uní la cuenta de {_isis_mesa_txt(p.get('source_table'))} con la de {_isis_mesa_txt(p.get('target_table'))}. Todo quedó en la segunda.", warm=True)
    if a == 'split_account':
        r = await split_account(int(p.get('account_id')),
                                AccountSplitIn(item_id=int(p.get('item_id')), qty=int(p.get('qty')),
                                               target_account_id=int(p.get('target_account_id')) if p.get('target_account_id') else None,
                                               table_id=int(p.get('table_id')) if p.get('table_id') else None,
                                               reason='Division por voz con Isis'), db, user)
        dest = f"la cuenta {r.get('target_account_id')}"
        return _isis_say(f"Listo, moví {p.get('qty')} {p.get('product')} a {dest}.", warm=True)
    if a == 'reopen_closed':
        await reopen_closed_account(int(p.get('account_id')),
                                    AccountReopenClosedIn(reason='Reapertura por voz con Isis',
                                                          table_id=int(p.get('table_id')) if p.get('table_id') else None), db, user)
        return _isis_say(f"La cuenta de {_isis_mesa_txt(p.get('table_name'))} quedó reabierta.", warm=True)
    if a == 'tip_payout':
        r = await record_tip_payout(int(p.get('distribution_id')), int(p.get('share_id')),
                                    TipPayoutIn(client_key=secrets.token_urlsafe(16),
                                                amount=Decimal(str(p.get('amount'))),
                                                method=str(p.get('method') or 'cash'),
                                                note='Pago de propina por voz con Isis'), db, user)
        rest = float(r.get('pending') or 0)
        metodo = 'en efectivo' if p.get('method') == 'cash' else 'por transferencia'
        extra = f" Le quedan {_isis_pesos(rest)} pendientes." if rest > 0 else ''
        return _isis_say(f"Listo, a {p.get('employee')} ya le pagué sus {_isis_pesos(p.get('amount'))} de propina {metodo}.{extra}", warm=True)
    # --- v2: 2. Mesas ---
    if a == 'update_table':
        cur = next((t for t in dining_tables(db, user) if t.get('id') == int(p.get('table_id'))), None)
        if not cur:
            raise HTTPException(404, 'Mesa no encontrada')
        await update_dining_table(int(p.get('table_id')), DiningTableIn(name=cur.get('name'), area=cur.get('area') or 'Salón', capacity=int(p.get('capacity')), active=True, sort_order=int(cur.get('sort_order') or 0)), db, user)
        return _isis_say(f"La {_isis_mesa_txt(p.get('table_name'))} ahora es para {p.get('capacity')} personas.", warm=True)
    # --- v2: 3. Estaciones ---
    if a == 'tickets_list':
        rows = tickets(db, user)
        if p.get('station'):
            rows = [t for t in rows if t.get('station') == p.get('station')]
        if not rows:
            return "No hay comandas en este turno. Cocina y barra están al corriente."
        txt = _isis_short_list([f"comanda #{t.get('id')} cuenta {t.get('account')} ({t.get('station')}, {t.get('status')})" for t in rows], len(rows))
        late = [t for t in rows if t.get('status') in ('new', 'received')]
        warn = " Ojo, hay comandas sin recibir todavía." if late else ""
        return _isis_say(f"Hay {_isis_n(len(rows), 'comanda', 'comandas')} en este turno: {txt}{warn}", warm=True)
    if a == 'ticket_status':
        await ticket_status(int(p.get('ticket_id')), StatusIn(status=str(p.get('status'))), db, user)
        estado = {'received': 'recibida', 'preparing': 'en preparación', 'ready': 'lista'}.get(p.get('status'), p.get('status'))
        return _isis_say(f"La comanda {p.get('ticket_id')} ya está {estado}.", warm=True)
    # --- v2: 4. Cobros ---
    if a == 'cashier_overview':
        d = cashier_overview(None, db, user)
        pend = d.get('accounts') or []
        if not pend:
            return "No hay cuentas pendientes de cobro. Todo pagado."
        txt = _isis_short_list([f"#{x.get('id')} mesa {x.get('table_name') or '?'} debe {_isis_pesos(x.get('due'))}" for x in pend], len(pend))
        return _isis_say(f"Por cobrar: {_isis_n(len(pend), 'cuenta', 'cuentas')}: {txt}", warm=True)
    if a == 'apply_discount':
        r = await apply_account_discount(int(p.get('account_id')), AccountDiscountIn(scope='account', mode=str(p.get('mode')), value=Decimal(str(p.get('value'))), reason='Descuento por voz con Isis'), db, user)
        return _isis_say(f"Descuento aplicado. La cuenta queda en {_isis_pesos(r.get('due'))}.", warm=True)
    # --- v2: 5. Caja: historial ---
    if a == 'cash_history':
        rows = cash_history(db, user)
        if not rows:
            return "Todavía no hay cortes cerrados."
        txt = '; '.join(f"el corte {r.get('id')} vendió {_isis_pesos(r.get('sales'))}, diferencia de {_isis_pesos(r.get('difference'))}" for r in rows[:3])
        return _isis_say(f"Los últimos cortes: {txt}.", warm=True)
    # --- v2: 6. Menú ---
    if a == 'menu_list':
        rows = [x for x in products(db, user) if not x.get('inventory_only') and x.get('active')]
        if not rows:
            return "El menú está vacío. ¿Agrego el primer producto?"
        txt = _isis_short_list([f"{x.get('name')}, {_isis_pesos(x.get('current_price') or x.get('price'))}" for x in rows], len(rows))
        return _isis_say(f"Mira, en el menú hay {_isis_n(len(rows), 'producto', 'productos')}: {txt}", warm=True)
    if a == 'product_price':
        rows = products(db, user)
        prod = next((x for x in rows if str(x.get('id')) == str(p.get('product_id'))), None)
        if not prod and p.get('product'):
            prod = _isis_find_product(db, user, str(p.get('product')))
        if not prod:
            return "No encontré ese producto en el menú."
        price = prod.get('current_price') or prod.get('price')
        return _isis_say(f"La {prod.get('name')} cuesta {_isis_pesos(price)}.", warm=True)
    if a == 'create_product':
        r = await add_product(ProductIn(name=str(p.get('name')), category='General', station=str(p.get('station') or 'kitchen'), price=Decimal(str(p.get('price'))), active=True, blocked=False, inventory_enabled=False, stock=0), db, user)
        return _isis_say(f"Ya está {p.get('name')} en el menú, a {_isis_pesos(p.get('price'))}.", warm=True)
    if a == 'archive_product':
        prod = db.get(Product, int(p.get('product_id')))
        if not prod:
            raise HTTPException(404, 'Producto no encontrado')
        await archive_product(int(p.get('product_id')), ProductArchiveIn(expected_version=int(prod.version), reason='Archivado por voz con Isis'), db, user)
        return _isis_say(f"El producto {prod.name} ya no está en el menú.", warm=True)
    if a == 'add_variant':
        r = await add_product_variant(int(p.get('product_id')), ProductVariantIn(name=str(p.get('name')), price=Decimal(str(p.get('price')))), db, user)
        return _isis_say(f"La presentación {p.get('name')}, de {_isis_pesos(p.get('price'))}, ya quedó agregada.", warm=True)
    if a == 'add_promotion':
        await add_product_promotion(int(p.get('product_id')), PromotionIn(name='Promo por voz', discount_percent=int(p.get('percent'))), db, user)
        return _isis_say(f"Ya le puse el {p.get('percent')}% de descuento. ¡A vender!", warm=True)
    if a == 'edit_product_price':
        prod = db.get(Product, int(p.get('product_id')))
        if not prod:
            raise HTTPException(404, 'Producto no encontrado')
        await edit_product(int(p.get('product_id')), ProductIn(name=prod.name, category=prod.category or 'General', station=prod.station, price=Decimal(str(p.get('price'))), active=prod.active, blocked=prod.blocked, inventory_enabled=prod.inventory_enabled, stock=prod.stock, expected_version=int(prod.version)), db, user)
        return _isis_say(f"El precio de {prod.name} ahora es {_isis_pesos(p.get('price'))}.", warm=True)
    # --- v2: 7. Inventario: movimientos e insumos ---
    if a == 'inventory_movements':
        d = inventory_movements(product_id=p.get('product_id'), db=db, user=user)
        rows = (d.get('movements') or [])[:5]
        if not rows:
            return "No hay movimientos de inventario registrados."
        kind_es = {'in': 'entrada', 'out': 'salida', 'restock': 'reposición',
                   'adjustment': 'ajuste', 'sale': 'venta', 'waste': 'merma'}
        txt = '; '.join(f"{kind_es.get(str(r.get('kind')), r.get('kind'))} de {r.get('quantity_change')} {r.get('product_name')}, de {r.get('before_stock')} a {r.get('after_stock')}" for r in rows)
        return _isis_say(f"Últimos movimientos: {txt}.", warm=True)
    if a == 'create_inventory_item':
        r = await create_inventory_item(InventoryItemIn(name=str(p.get('name')), stock=int(p.get('qty') or 0), unit=str(p.get('unit') or 'botellas')), db, user)
        return _isis_say(f"El insumo {p.get('name')} ya quedó registrado con {p.get('qty')} {p.get('unit')}.", warm=True)
    # --- v2: 8. Personal ---
    if a == 'staff_list':
        rows = [u for u in users(db, user) if u.get('active') and u.get('username') != 'isis']
        if not rows:
            return "No hay personal activo ahorita."
        rol = {'owner': 'propietario', 'admin': 'administrador', 'waiter': 'mesero', 'cashier': 'caja', 'kitchen': 'cocina', 'bar': 'barra'}
        txt = _isis_short_list([f"{u.get('display')} ({rol.get(u.get('role'), u.get('role'))})" for u in rows], len(rows))
        return _isis_say(f"Hay {_isis_n(len(rows), 'persona activa', 'personas activas')}: {txt}", warm=True)
    if a == 'create_staff':
        r = await create_staff(StaffCreateIn(name=str(p.get('name')), role=str(p.get('role')), pin=str(p.get('pin'))), db, user)
        rol = {'waiter': 'mesero', 'cashier': 'caja', 'kitchen': 'cocina', 'bar': 'barra', 'admin': 'administrador'}.get(str(r.get('role')), r.get('role'))
        return _isis_say(f"{p.get('name')} ya tiene su registro como {rol} y puede entrar con su PIN.", warm=True)
    if a == 'staff_pin':
        target = db.get(User, int(p.get('user_id')))
        if not target:
            raise HTTPException(404, 'Personal no encontrado')
        if target.username == 'isis':
            raise HTTPException(403, 'Isis no puede cambiar su propio PIN por voz; que el propietario lo haga en Personal')
        await assign_staff_pin(int(p.get('user_id')), StaffPinIn(pin=str(p.get('pin'))), db, user)
        return _isis_say(f"El PIN de {target.display} ya quedó actualizado.", warm=True)
    # --- v2: 9. Propinas ---
    if a == 'tips_pool':
        d = tips_pool_current(db, user)
        if not d.get('open'):
            return "No hay turno abierto, así que no hay fondo de propinas ahorita."
        dist = d.get('distribution')
        extra = f" Ya hay reparto de {_isis_pesos(dist.get('total'))} entre {_isis_n(dist.get('participant_count'), 'persona', 'personas')}." if dist else " Todavía no hay reparto."
        return _isis_say(f"El fondo de propinas lleva {_isis_pesos(d.get('tips_total'))}.{extra}", warm=True)
    if a == 'tips_distribute':
        d = tips_pool_current(db, user)
        ids = [e.get('id') for e in (d.get('employees') or []) if e.get('id')]
        # Isis no es persona: nunca participa en el reparto.
        isis_row = next((u for u in users(db, user) if u.get('username') == 'isis'), None)
        if isis_row:
            ids = [i for i in ids if i != isis_row.get('id')]
        if not ids:
            raise HTTPException(409, 'No hay empleados activos para repartir las propinas')
        r = await create_tip_distribution(TipDistributionIn(user_ids=ids, note='Reparto por voz con Isis'), db, user)
        dist = r.get('distribution') or {}
        return _isis_say(f"Reparto hecho: {_isis_pesos(dist.get('total'))} entre {_isis_n(dist.get('participant_count'), 'persona', 'personas')}, {_isis_pesos(dist.get('base_share'))} para cada quien.", warm=True)
    # --- v2: 10. Autorizaciones ---
    if a == 'approvals_list':
        rows = [r for r in requests(db, user) if r.get('status') == 'pending']
        if not rows:
            return "No hay solicitudes pendientes. Todo autorizado."
        txt = _isis_short_list([f"solicitud #{r.get('id')} ({r.get('kind')}) cuenta #{r.get('account_id')} · {r.get('reason')}" for r in rows], len(rows))
        return _isis_say(f"Hay {_isis_n(len(rows), 'solicitud pendiente', 'solicitudes pendientes')}: {txt}", warm=True)
    if a == 'decide_approval':
        await decide(int(p.get('request_id')), DecisionIn(approve=bool(p.get('approve')), return_stock=False), db, user)
        return _isis_say(f"La solicitud {p.get('request_id')} quedó {'autorizada' if p.get('approve') else 'rechazada'}.", warm=True)
    # --- v2: 11. Auditoría ---
    if a == 'audit_list':
        rows = audit_log(db, user)[:8]
        if not rows:
            return "No hay movimientos registrados todavía."
        txt = _isis_short_list([f"{r.get('actor')} hizo {r.get('action')}" for r in rows], len(rows))
        return _isis_say(f"Últimos movimientos: {txt}", warm=True)
    # --- v2: 12. Reservaciones ---
    if a == 'reservations_list':
        rows = [r for r in mitote_extensions.reservations(db, user) if r.get('status') in ('pending', 'confirmed')]
        if not rows:
            return "No hay reservaciones pendientes ni confirmadas."
        txt = _isis_short_list([f"{r.get('name')} ({_isis_n(r.get('party_size'), 'persona', 'personas')}, {r.get('status')})" for r in rows], len(rows))
        return _isis_say(f"Hay {_isis_n(len(rows), 'reservación activa', 'reservaciones activas')}: {txt}", warm=True)
    if a == 'create_reservation':
        when = datetime.fromisoformat(str(p.get('scheduled_at')))
        r = await mitote_extensions.add_reservation(mitote_extensions.ReservationIn(name=str(p.get('name')), phone='', party_size=int(p.get('party_size')), scheduled_at=when, note='Reservación por voz con Isis'), db, user)
        return _isis_say(f"La reservación para {p.get('name')} ya quedó anotada, folio {r.get('id')}.", warm=True)
    if a == 'confirm_reservation':
        await mitote_extensions.change_reservation_status(int(p.get('reservation_id')), mitote_extensions.ReservationStatusIn(status='confirmed'), db, user)
        return _isis_say(f"La reservación {p.get('reservation_id')} quedó confirmada.", warm=True)
    if a == 'reservation_arrive':
        r = await mitote_extensions.reservation_arrival(int(p.get('reservation_id')), mitote_extensions.ReservationArrivalIn(table_id=int(p.get('table_id')), waiter_id=int(p.get('waiter_id'))), db, user)
        return _isis_say(f"¡Bienvenidos! Ya les abrí la cuenta {r.get('account_id')}.", warm=True)
    # --- v2: 13. Tiempos ---
    if a == 'prep_config':
        d = mitote_extensions.prep_config(db, user)
        return _isis_say(f"El límite por comanda es de {d.get('batch_minutes')} minutos, y te aviso cuando lleve el {d.get('warning_percent')}% del tiempo.", warm=True)
    if a == 'prep_config_set':
        cur = mitote_extensions.prep_config(db, user)
        await mitote_extensions.put_prep_config(mitote_extensions.PrepPolicyIn(batch_minutes=int(p.get('batch_minutes')), warning_percent=int(cur.get('warning_percent') or 80)), db, user)
        return _isis_say(f"El límite de preparación ahora es de {p.get('batch_minutes')} minutos por comanda.", warm=True)
    if a == 'product_prep_set':
        await mitote_extensions.put_product_prep(int(p.get('product_id')), mitote_extensions.ProductPrepIn(minutes=int(p.get('minutes'))), db, user)
        return _isis_say(f"El tiempo de preparación de {p.get('product')} ahora es de {p.get('minutes')} minutos.", warm=True)
    # --- v3: charla libre (solo lectura, la respuesta ya viene redactada) ---
    # v7: una de cada 3 respuestas empieza con una duda corta, como persona.
    if a == 'chat':
        ans = str(p.get('answer') or '').strip()
        if not ans:
            return '¿Me lo repites?'
        _ISIS_CHAT_COUNT[0] += 1
        if _ISIS_CHAT_COUNT[0] % 3 == 0:
            ans = random.choice(_ISIS_DOUBT) + ' ' + ans
        return ans
    # --- v11: cierre del día hablado ---
    if a == 'day_close':
        return _isis_say(_isis_day_close(db, user), warm=True)
    # --- v11: resumen de cambio de turno ---
    if a == 'shift_handover':
        return _isis_say(_isis_shift_handover(db, user), warm=True)
    # --- v11: lista de compras automática ---
    if a == 'shopping_list':
        return _isis_say(_isis_shopping_list(db, user), warm=True)
    # --- v11: la comanda más atrasada ---
    if a == 'oldest_ticket':
        return _isis_say(_isis_oldest_ticket(db, user), warm=True)
    # --- v2: 14. Estadísticas ---
    if a == 'analytics':
        period = str(p.get('period') or 'today')
        today = datetime.now(timezone.utc).date()
        if period == 'yesterday':
            start = end = today - timedelta(days=1)
            label, scope = 'Ayer', 'history'
        elif period == 'week':
            start, end = today - timedelta(days=6), today
            label, scope = 'Esta semana', 'history'
        else:
            start = end = today
            label, scope = 'Hoy', 'current'
        d = mitote_extensions.owner_analytics(start=start, end=end, scope=scope, db=db, user=user)
        top = d.get('products') or []
        top_txt = ', '.join(f"{x.get('name')} ({x.get('quantity')})" for x in top[:3])
        prep = d.get('prep_avg_minutes')
        prep_txt = f" La preparación promedio es de {prep} minutos." if prep else ""
        return _isis_say(f"{label} llevamos {_isis_pesos(d.get('sales'))} en ventas, {_isis_n(d.get('paid_accounts'), 'cuenta pagada', 'cuentas pagadas')}, y el ticket promedio es de {_isis_pesos(d.get('average_ticket'))}. Lo más vendido: {top_txt or 'nada todavía'}.{prep_txt}", warm=True)
    raise HTTPException(400, 'No sé cómo hacer eso en El Mitote.')

class IsisSetupIn(BaseModel):
    pin: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')

@app.post('/api/isis-voice/setup')
async def isis_voice_setup(data: IsisSetupIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    """Solo el propietario crea (o cambia el PIN de) el usuario Isis."""
    allow(user, 'owner')
    if user.username == 'isis':
        raise HTTPException(403, 'Isis no puede cambiar su propio PIN')
    target = db.scalar(select(User).where(User.username == 'isis'))
    created = False
    if target is None:
        target = User(username='isis', display='Isis', role='owner',
                      password_hash=ph.hash(secrets.token_urlsafe(32)), active=True)
        db.add(target)
        db.flush()
        created = True
    _set_user_pin(db, target, data.pin)
    audit(db, user, 'isis.setup', id=target.id, created=created)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Ese PIN ya pertenece a otra persona. Elige uno distinto.')
    await notify()
    return {'ok': True, 'created': created, 'username': 'isis', 'display': 'Isis', 'role': 'owner'}

@app.get('/api/isis-voice/status')
def isis_voice_status(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, 'owner')
    exists = db.scalar(select(User).where(User.username == 'isis', User.active.is_(True))) is not None
    # Alertas proactivas (v4): lo que necesita atención AHORA; nunca rompen el status.
    try:
        alerts = _isis_alerts(db, user)
    except Exception:
        alerts = []
    # Resumen de ayer (v8): cómo cerró el día anterior; nunca rompe el status.
    try:
        morning_brief = _isis_morning_brief(db, user)
    except Exception:
        morning_brief = ''
    return {'ok': True, 'isis_user_exists': exists, 'fableai_configured': bool(FABLEAI_API_KEY),
            'openai_tts_configured': bool(OPENAI_API_KEY),
            'voice': 'kokoro-ef-dora', 'mode': 'solo-mitote', 'alerts': alerts,
            'morning_brief': morning_brief}


@app.post('/api/isis-voice/end-call')
def isis_voice_end_call(user: User = Depends(auth)):
    """Isis (o el propietario) cuelga: se borra el historial de la conversación (v4)."""
    if user.username != 'isis' and user.role != 'owner':
        raise HTTPException(403, 'Este panel de voz es solo para Isis')
    _isis_history_clear(user.id)
    return {'ok': True}

class IsisTTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    voice: Optional[str] = None  # v10.2: probar voces; se valida contra la lista

# Tablas para números a palabras en español (v10.3).
_UNITS_ES = ['cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete',
             'ocho', 'nueve', 'diez', 'once', 'doce', 'trece', 'catorce', 'quince',
             'dieciséis', 'diecisiete', 'dieciocho', 'diecinueve']
_VEINTI_ES = {21: 'veintiuno', 22: 'veintidós', 23: 'veintitrés', 24: 'veinticuatro',
              25: 'veinticinco', 26: 'veintiséis', 27: 'veintisiete', 28: 'veintiocho',
              29: 'veintinueve'}
_TENS_ES = {30: 'treinta', 40: 'cuarenta', 50: 'cincuenta', 60: 'sesenta',
            70: 'setenta', 80: 'ochenta', 90: 'noventa'}
_HUNDREDS_ES = {100: 'ciento', 200: 'doscientos', 300: 'trescientos', 400: 'cuatrocientos',
                500: 'quinientos', 600: 'seiscientos', 700: 'setecientos',
                800: 'ochocientos', 900: 'novecientos'}

def _isis_num_palabras(n):
    """Entero a palabras en español (v10.3): 0..999,999,999. Pura y testeable.
    100 -> 'cien', 250 -> 'doscientos cincuenta', 2026 -> 'dos mil veintiséis',
    1000000 -> 'un millón'. Devuelve None si no es entero o está fuera de rango."""
    try:
        n = int(n)
    except Exception:
        return None
    if n < 0 or n > 999999999:
        return None
    if n < 20:
        return _UNITS_ES[n]
    if n == 20:
        return 'veinte'
    if n < 30:
        return _VEINTI_ES[n]
    if n < 100:
        d, r = divmod(n, 10)
        w = _TENS_ES[d * 10]
        return w if r == 0 else w + ' y ' + _UNITS_ES[r]
    if n < 1000:
        if n == 100:
            return 'cien'
        d, r = divmod(n, 100)
        w = _HUNDREDS_ES[d * 100]
        return w if r == 0 else w + ' ' + _isis_num_palabras(r)
    if n < 1000000:
        d, r = divmod(n, 1000)
        w = 'mil' if d == 1 else _isis_num_palabras(d) + ' mil'
        return w if r == 0 else w + ' ' + _isis_num_palabras(r)
    d, r = divmod(n, 1000000)
    w = 'un millón' if d == 1 else _isis_num_palabras(d) + ' millones'
    return w if r == 0 else w + ' ' + _isis_num_palabras(r)

def _isis_num_palabras_m(n):
    """Como _isis_num_palabras, pero apocopado antes de un sustantivo
    masculino ('uno' -> 'un'): 1 -> 'un', 21 -> 'veintiún', 31 -> 'treinta y un'."""
    w = _isis_num_palabras(n)
    if w is None:
        return None
    if w == 'veintiuno':
        return 'veintiún'
    if w.endswith('uno'):
        w = w[:-3] + 'un'
    return w

def _isis_tts_text(text):
    """Copia del texto SOLO para la voz (v10.2, números a palabras en v10.3):
    puntuación, caracteres y NÚMEROS que la voz lee mejor, para que la
    entonación salga natural y no diga los números dígito por dígito
    ('1 0 0 pesos' -> 'cien pesos'). El texto que se muestra en pantalla
    NO cambia: solo cambia la copia que se manda a OpenAI. Pura y testeable.
    Los códigos (6+ dígitos seguidos que no son dinero) se dejan en dígitos:
    Isis nunca dice PINs ni claves."""
    t = ' '.join(str(text or '').split())
    t = re.sub(r'\$(\d+)', r'\1 pesos', t)  # "$40" -> "40 pesos"
    t = t.replace('&', ' y ').replace('+', ' más ').replace('°', ' grados ')
    t = t.replace('#', ' número ').replace('@', ' arroba ')
    t = t.replace('...', '…')

    def _m_money(m):
        entero = int(m.group(1).replace(',', ''))
        cents = m.group(2)
        c = int(cents.ljust(2, '0')) if cents else 0
        w = _isis_num_palabras_m(entero)
        if w is None:
            return m.group(0)
        if entero >= 1000000:
            noun = 'de pesos'
        elif entero == 1 and c == 0:
            noun = 'peso'
        else:
            noun = 'pesos'
        if c == 0:
            return w + ' ' + noun
        cw = _isis_num_palabras_m(c)
        cnoun = 'centavo' if c == 1 else 'centavos'
        return w + ' ' + noun + ' con ' + cw + ' ' + cnoun

    def _m_pct(m):
        num = m.group(1)
        if '.' in num:
            ip, fp = num.split('.', 1)
            iw = _isis_num_palabras(int(ip))
            fw = ' '.join(_UNITS_ES[int(d)] for d in fp)
            return iw + ' punto ' + fw + ' por ciento'
        return _isis_num_palabras(int(num)) + ' por ciento'

    def _m_time(m):
        h, mm = int(m.group(1)), int(m.group(2))
        if h > 23 or mm > 59:
            return m.group(0)
        hw = _isis_num_palabras(h)
        if mm == 0:
            return hw + ' en punto'
        if mm == 15:
            return hw + ' y cuarto'
        if mm == 30:
            return hw + ' y media'
        if mm < 30:
            return hw + ' y ' + _isis_num_palabras(mm)
        return hw + ' ' + _isis_num_palabras(mm)

    def _m_num(m):
        raw = m.group(1)
        digits = raw.replace(',', '')
        if '.' in digits:
            ip, fp = digits.split('.', 1)
            iw = _isis_num_palabras(int(ip)) if ip else None
            if iw is None:
                return m.group(0)
            fw = ' '.join(_UNITS_ES[int(d)] for d in fp)
            return iw + ' punto ' + fw
        if len(digits) >= 6:
            return m.group(0)  # código/ID: se deja en dígitos
        w = _isis_num_palabras(int(digits))
        return w if w is not None else m.group(0)

    t = re.sub(r'(\d[\d,]*)(?:\.(\d{1,2}))?\s*pesos?\b', _m_money, t)
    t = re.sub(r'(\d+(?:\.\d+)?)\s*%', _m_pct, t)
    t = re.sub(r'\b(\d{1,2}):(\d{2})\b', _m_time, t)
    t = re.sub(r'(\d[\d,]*(?:\.\d+)?)', _m_num, t)
    # Espacio tras el punto si falta ("Hola.Santiago" -> "Hola. Santiago");
    # el punto entre dígitos ("1.2", "40.50") ya no existe aquí: se volvió palabras.
    t = re.sub(r'([.!?…])(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])', r'\1 ', t)
    t = ' '.join(t.split())
    if t and t[-1] not in '.?!…':
        t += '.'
    return t

def _isis_openai_tts_open(text, voice):
    """Abre la petición de voz en streaming (v10.2, stream=true) y devuelve
    (status, respuesta) para leer los trozos después, en el generador del
    StreamingResponse. Corre en un hilo (asyncio.to_thread): urllib es
    bloqueante. La llave nunca se registra ni se devuelve."""
    payload = {'model': OPENAI_TTS_MODEL, 'input': _isis_tts_text(text),
               'voice': voice, 'response_format': 'mp3',
               'speed': OPENAI_TTS_SPEED, 'stream': True}
    # Las instrucciones de estilo solo las acepta gpt-4o-mini-tts.
    if OPENAI_TTS_MODEL.startswith('gpt-4o-mini-tts'):
        payload['instructions'] = OPENAI_TTS_INSTRUCTIONS
    req = urllib.request.Request(OPENAI_TTS_URL, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + OPENAI_API_KEY},
                                 method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return resp.status, resp
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None

@app.post('/api/isis-voice/tts')
async def isis_voice_tts(data: IsisTTSIn, user: User = Depends(auth)):
    """Voz de Isis con OpenAI TTS (v10, streaming en v10.2): devuelve el
    audio MP3 de la frase por trozos, en cuanto OpenAI los genera, para
    que el frente empiece a sonar sin esperar al audio completo. Solo
    Isis (o el propietario, para probar). Sin clave en el servidor, 503
    y el frente usa Dora o la voz del teléfono; si OpenAI falla, 502."""
    if user.username != 'isis' and user.role != 'owner':
        raise HTTPException(403, 'Este panel de voz es solo para Isis')
    if not OPENAI_API_KEY:
        raise HTTPException(503, 'Voz de OpenAI no configurada en el servidor')
    text = (data.text or '').strip()
    if not text:
        raise HTTPException(400, 'Texto vacío')
    voice = (data.voice or '').strip().lower() or OPENAI_TTS_VOICE
    if voice not in OPENAI_TTS_VOICES:
        raise HTTPException(400, 'Voz no válida')
    status_code, resp = await asyncio.to_thread(_isis_openai_tts_open, text, voice)
    if resp is None:
        raise HTTPException(502, 'No se pudo generar la voz')
    if status_code != 200:
        try:
            resp.close()
        except Exception:
            pass
        raise HTTPException(502, 'OpenAI no pudo generar la voz')
    def _chunks():
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass
    return StreamingResponse(_chunks(), media_type='audio/mpeg')

class IsisChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    confirm_token: Optional[str] = None

@app.post('/api/isis-voice/chat')
async def isis_voice_chat(data: IsisChatIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    """Solo Isis (o el propietario, para probar) habla con este cerebro."""
    if user.username != 'isis' and user.role != 'owner':
        raise HTTPException(403, 'Este panel de voz es solo para Isis')
    actor = user.username
    # Números dictados dígito por dígito (v10.3): '1 0 0 pesos' ya llega
    # como '100 pesos' al parser local, al corte y a Claude; también se
    # guarda así en el historial, para que el hilo no arrastre los dígitos.
    message = _isis_compact_digits(data.message)
    # Memoria de conversación (v4): cada intercambio se guarda para que Isis
    # siga el hilo de la llamada; se borra a los 30 min sin uso o al colgar.
    def _record(reply):
        _isis_history_add(user.id, message, reply)
    # Confirmacion de una accion pendiente
    if data.confirm_token:
        entry = _ISIS_CONFIRM.pop(data.confirm_token, None)
        if not entry or entry['expires'] < time.time():
            reply = 'Esa confirmación ya se venció, dime de nuevo qué quieres hacer.'
            _record(reply)
            return {'ok': True, 'reply': reply, 'executed': False}
        if any(w in _isis_norm(message) for w in ('no', 'cancela', 'olvida')):
            reply = 'Sale, cancelado. No hice nada.'
            _record(reply)
            return {'ok': True, 'reply': reply, 'executed': False}
        if entry.get('delicate') and entry.get('stage', 1) == 1:
            # Primera confirmación de una acción delicada: no se ejecuta
            # nada todavía, solo se repiten los datos exactos y se pide
            # la segunda confirmación con un token nuevo.
            token = _isis_confirm_token(entry['intent'], delicate=True, stage=2)
            summary = _isis_describe(entry['intent'])
            reply = _isis_warm() + ' Para estar seguros: ' + summary + ' ¿Seguro, lo hago?'
            _record(reply)
            return {'ok': True, 'reply': reply, 'executed': False,
                    'needs_confirmation': True, 'confirm_token': token}
        try:
            reply = await _isis_execute(db, user, entry['intent'])
        except HTTPException as e:
            reply = 'No se pudo: ' + str(e.detail)
            _record(reply)
            return {'ok': True, 'reply': reply, 'executed': False}
        audit(db, user, 'isis.voice_execute', actor=actor, isis_action=entry['intent'].get('action'))
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': True}
    # Corte guiado (v8): si hay un corte en curso, el mensaje es un paso del flujo.
    if _isis_cash_cut_get(user.id) is not None:
        res = _isis_cash_cut_step(db, user, message)
        if res is not None:
            if res.get('needs_confirmation'):
                audit(db, user, 'isis.voice_propose', actor=actor, isis_action='close_cash')
            _record(res['reply'])
            out = {'ok': True, 'reply': res['reply'], 'executed': False}
            if res.get('needs_confirmation'):
                out['needs_confirmation'] = True
                out['confirm_token'] = res['confirm_token']
            return out
    # División guiada (v11): si hay una división en curso, el mensaje es un paso del flujo.
    if _isis_split_get(user.id) is not None:
        res = _isis_split_step(db, user, message)
        if res is not None:
            if res.get('needs_confirmation'):
                audit(db, user, 'isis.voice_propose', actor=actor, isis_action='split_account')
            _record(res['reply'])
            out = {'ok': True, 'reply': res['reply'], 'executed': False}
            if res.get('needs_confirmation'):
                out['needs_confirmation'] = True
                out['confirm_token'] = res['confirm_token']
            return out
    # Nueva orden: interprete local primero, IA despues
    intent = _isis_parse_local(db, user, message, history=_isis_history_get(user.id))
    if intent is None or intent.get('action') == 'unknown':
        context = _isis_context(db, user)
        hist_txt = _isis_history_text(user.id)
        if hist_txt:
            context += ('\nConversación reciente en esta llamada (para seguir el hilo):\n' + hist_txt)
        ai_intent = await asyncio.to_thread(_isis_call_fableai, message, context)
        if ai_intent:
            intent = ai_intent
    if intent is None:
        reply = 'No te entendí, ¿me lo repites? Te ayudo con todo lo del restaurante: mesas, cuentas, caja, menú, inventario y ventas.'
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': False}
    if intent.get('action') == 'unknown':
        q = intent.get('question') or '¿Me lo repites con más detalle?'
        _record(q)
        return {'ok': True, 'reply': q, 'executed': False}
    if intent.get('action') not in _WRITE_ACTIONS and intent.get('action') not in _READ_ACTIONS:
        reply = 'Eso no lo puedo hacer en El Mitote. Te ayudo con mesas, cuentas, caja, menú, inventario y ventas.'
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': False}
    if intent.get('action') == 'cash_cut_start':
        # Antes de empezar se revisa lo mismo que la app revisa para poder
        # cerrar: turno abierto y sin bloqueos (cuentas abiertas, movimientos
        # pendientes, reparto de propinas). Si algo lo bloquea, Isis lo dice
        # en corto en vez de guiar un corte que no se puede cerrar.
        st = _isis_cash_state(db)
        if not st.get('open'):
            reply = 'No hay turno abierto, así que no hay corte que hacer.'
        elif st.get('blockers'):
            reply = 'No puedo cerrar todavía: ' + ' y '.join(st['blockers']) + '.'
        else:
            # v8.2: al iniciar, Isis lee primero el desglose completo con los
            # datos de la app y luego pregunta por el efectivo del cajón.
            reply = _isis_cash_cut_start_reply(db, user)
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': False}
    if intent.get('action') in _WRITE_ACTIONS:
        token = _isis_confirm_token(intent, delicate=intent.get('action') in _DELICATE_ACTIONS)
        summary = _isis_describe(intent)
        audit(db, user, 'isis.voice_propose', actor=actor, isis_action=intent.get('action'))
        reply = _isis_warm() + ' ' + summary + ' ¿Confirmas?'
        _record(reply)
        return {'ok': True, 'reply': reply, 'needs_confirmation': True, 'confirm_token': token, 'executed': False}
    if intent.get('action') == 'split_account_start':
        _isis_split_start(user.id, int(intent['params'].get('account_id')),
                          str(intent['params'].get('table_name') or ''))
        reply = f"Va, dividimos la cuenta de {_isis_mesa_txt(intent['params'].get('table_name'))}. ¿Qué producto muevo y cuántos?"
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': False}
    try:
        reply = await _isis_execute(db, user, intent)
    except HTTPException as e:
        reply = 'No se pudo: ' + str(e.detail)
        _record(reply)
        return {'ok': True, 'reply': reply, 'executed': False}
    _record(reply)
    return {'ok': True, 'reply': reply, 'executed': True}

def owner_account(db,user,account_id,allowed=('open','pending')):
    acc=db.execute(select(Account).where(Account.id==account_id).with_for_update()).scalar_one_or_none()
    if not acc: raise HTTPException(404,'Cuenta inexistente')
    if user.role=='waiter' and acc.owner_id!=user.id: raise HTTPException(403,'No es tu cuenta')
    if allowed and acc.status not in allowed: raise HTTPException(409,'La cuenta no permite esta operación')
    return acc

def account_gross_total(db,acc):
    value=db.scalar(select(func.coalesce(func.sum(
        OrderItem.price_cents*(OrderItem.qty-func.coalesce(OrderItem.cancelled_qty,0))),0
    )).select_from(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(
        Batch.account_id==acc.id))
    return int(value or 0)

def account_discount_total(db,account_id):
    return db.scalar(select(func.coalesce(func.sum(AccountDiscount.discount_cents),0)).where(
        AccountDiscount.account_id==account_id,AccountDiscount.status=='applied')) or 0

def acc_totals(db,acc):
    gross=account_gross_total(db,acc)
    discounts=account_discount_total(db,acc.id)
    total=max(0,gross-discounts)
    paid=db.scalar(select(func.coalesce(func.sum(Payment.cents),0)).where(Payment.account_id==acc.id)) or 0
    return total,paid

def account_dict(db,a):
    gross=account_gross_total(db,a)
    discounts=account_discount_total(db,a.id)
    total=max(0,gross-discounts)
    paid=db.scalar(select(func.coalesce(func.sum(Payment.cents),0)).where(Payment.account_id==a.id)) or 0
    u=db.get(User,a.owner_id)
    occupancy=db.scalar(select(TableOccupancy).where(TableOccupancy.account_id==a.id))
    table=db.get(DiningTable,occupancy.table_id) if occupancy else None
    return {'id':a.id,'name':a.name,'owner_id':a.owner_id,'owner':u.display if u else '?',
            'status':a.status,'gross_total':amount(gross),'discount_total':amount(discounts),
            'total':amount(total),'paid':amount(paid),'due':amount(total-paid),
            'table_id':table.id if table else None,'table_name':table.name if table else None,
            'table_area':table.area if table else None,
            'created_at':a.created_at.isoformat() if a.created_at else ''}

def account_dict_many(db, accounts):
    """Serializa cuentas activas con agregados y relaciones precargadas en bloque."""
    if not accounts:
        return []
    account_ids=[a.id for a in accounts]
    owner_ids={a.owner_id for a in accounts if a.owner_id is not None}

    gross_rows=db.execute(
        select(Batch.account_id,func.coalesce(func.sum(
            OrderItem.price_cents*(OrderItem.qty-func.coalesce(OrderItem.cancelled_qty,0))),0))
        .join(OrderItem,OrderItem.batch_id==Batch.id)
        .where(Batch.account_id.in_(account_ids))
        .group_by(Batch.account_id)
    ).all()
    discount_rows=db.execute(
        select(AccountDiscount.account_id,func.coalesce(func.sum(AccountDiscount.discount_cents),0))
        .where(AccountDiscount.account_id.in_(account_ids),AccountDiscount.status=='applied')
        .group_by(AccountDiscount.account_id)
    ).all()
    payment_rows=db.execute(
        select(Payment.account_id,func.coalesce(func.sum(Payment.cents),0))
        .where(Payment.account_id.in_(account_ids))
        .group_by(Payment.account_id)
    ).all()
    owner_rows=db.scalars(select(User).where(User.id.in_(owner_ids))).all() if owner_ids else []
    occupancy_rows=db.execute(
        select(TableOccupancy.account_id,DiningTable.id,DiningTable.name,DiningTable.area)
        .join(DiningTable,DiningTable.id==TableOccupancy.table_id)
        .where(TableOccupancy.account_id.in_(account_ids))
    ).all()

    gross_by={row[0]:int(row[1] or 0) for row in gross_rows}
    discount_by={row[0]:int(row[1] or 0) for row in discount_rows}
    paid_by={row[0]:int(row[1] or 0) for row in payment_rows}
    owner_by={u.id:u.display for u in owner_rows}
    table_by={row[0]:{"id":row[1],"name":row[2],"area":row[3]} for row in occupancy_rows}

    result=[]
    for account in accounts:
        gross=gross_by.get(account.id,0)
        discounts=discount_by.get(account.id,0)
        total=max(0,gross-discounts)
        paid=paid_by.get(account.id,0)
        table=table_by.get(account.id)
        result.append({
            "id":account.id,"name":account.name,"owner_id":account.owner_id,
            "owner":owner_by.get(account.owner_id,"?"),"status":account.status,
            "gross_total":amount(gross),"discount_total":amount(discounts),
            "total":amount(total),"paid":amount(paid),"due":amount(total-paid),
            "table_id":table["id"] if table else None,
            "table_name":table["name"] if table else None,
            "table_area":table["area"] if table else None,
            "created_at":account.created_at.isoformat() if account.created_at else ""
        })
    return result


def release_table_for_account(db,a,user,reason):
    occupancy=db.execute(select(TableOccupancy).where(TableOccupancy.account_id==a.id).with_for_update()).scalar_one_or_none()
    if not occupancy:
        return
    table=db.get(DiningTable,occupancy.table_id)
    db.delete(occupancy)
    audit(db,user,'table.release',account_id=a.id,table_id=table.id if table else occupancy.table_id,
          table_name=table.name if table else '',reason=reason)

def local_now():
    return datetime.now(ZoneInfo('America/Mexico_City'))

def promotion_is_active(promo, at=None):
    at=at or local_now()
    if not promo.active:return False
    try:
        days={int(x) for x in (promo.weekdays or '').split(',') if x.strip()!=''}
        current=at.strftime('%H:%M')
        start=promo.start_time or '00:00';end=promo.end_time or '23:59'
        # Promociones nocturnas pertenecen al día en que comenzaron.
        # Ej.: lunes 22:00–martes 02:00 debe seguir activa el martes 01:00.
        effective_date=at.date()
        if start<=end:
            if not start<=current<=end:return False
        elif current>=start:
            pass
        elif current<=end:
            effective_date-=timedelta(days=1)
        else:
            return False
        if effective_date.weekday() not in days:return False
        day=effective_date.isoformat()
        if promo.start_date and day<promo.start_date:return False
        if promo.end_date and day>promo.end_date:return False
        return True
    except Exception:
        return False

def apply_promotion(base_cents, promo):
    if not promo:return base_cents
    if promo.price_cents is not None:return max(0,int(promo.price_cents))
    if promo.discount_percent is not None:
        return max(0,int((Decimal(base_cents)*(Decimal(100-promo.discount_percent)/Decimal(100))).quantize(Decimal('1'),rounding=ROUND_HALF_UP)))
    return base_cents

def product_related_data(db, products):
    """Carga las relaciones del catálogo en lotes, evitando consultas N+1."""
    product_ids = [p.id for p in products]
    if not product_ids:
        return {"variants": {}, "groups": {}, "options": {}, "promotions": {}}

    variants = db.scalars(
        select(ProductVariant)
        .where(ProductVariant.product_id.in_(product_ids))
        .order_by(ProductVariant.product_id, ProductVariant.sort_order, ProductVariant.id)
    ).all()
    groups = db.scalars(
        select(ProductModifierGroup)
        .where(ProductModifierGroup.product_id.in_(product_ids))
        .order_by(ProductModifierGroup.product_id, ProductModifierGroup.sort_order,
                  ProductModifierGroup.id)
    ).all()
    group_ids = [g.id for g in groups]
    options = db.scalars(
        select(ProductModifierOption)
        .where(ProductModifierOption.group_id.in_(group_ids))
        .order_by(ProductModifierOption.group_id, ProductModifierOption.sort_order,
                  ProductModifierOption.id)
    ).all() if group_ids else []
    promotions = db.scalars(
        select(ProductPromotion)
        .where(ProductPromotion.product_id.in_(product_ids))
        .order_by(ProductPromotion.product_id, ProductPromotion.id.desc())
    ).all()

    def grouped(rows, key):
        result = {}
        for row in rows:
            result.setdefault(getattr(row, key), []).append(row)
        return result

    return {
        "variants": grouped(variants, "product_id"),
        "groups": grouped(groups, "product_id"),
        "options": grouped(options, "group_id"),
        "promotions": grouped(promotions, "product_id"),
    }


def product_dict(p,settings,db=None,related=None):
    status='Solo inventario' if p.inventory_only else ('No disponible' if p.blocked or not p.active else ('Sin control' if not settings.inventory_on or not p.inventory_enabled else ('Agotado' if p.stock==0 else 'Stock bajo' if p.stock<=3 else 'Disponible')))
    variant_rows=[];group_rows=[];option_map={};promo_rows=[]
    if related is not None:
        variant_rows=related["variants"].get(p.id, [])
        group_rows=related["groups"].get(p.id, [])
        option_map=related["options"]
        promo_rows=related["promotions"].get(p.id, [])
    elif db is not None:
        variant_rows=db.scalars(select(ProductVariant).where(
            ProductVariant.product_id==p.id).order_by(
            ProductVariant.sort_order,ProductVariant.id)).all()
        group_rows=db.scalars(select(ProductModifierGroup).where(
            ProductModifierGroup.product_id==p.id).order_by(
            ProductModifierGroup.sort_order,ProductModifierGroup.id)).all()
        for group in group_rows:
            option_map[group.id]=db.scalars(select(ProductModifierOption).where(
                ProductModifierOption.group_id==group.id).order_by(
                ProductModifierOption.sort_order,ProductModifierOption.id)).all()
        promo_rows=db.scalars(select(ProductPromotion).where(
            ProductPromotion.product_id==p.id).order_by(ProductPromotion.id.desc())).all()

    variants=[{'id':v.id,'name':v.name,'price':amount(v.price_cents),'active':v.active,'sort_order':v.sort_order}
              for v in variant_rows]
    groups=[]
    for g in group_rows:
        options=[{'id':o.id,'name':o.name,'price_delta':amount(o.price_delta_cents),'active':o.active,'sort_order':o.sort_order,
                  'inventory_product_id':o.inventory_product_id,'inventory_quantity':o.inventory_quantity}
                 for o in option_map.get(g.id, [])]
        groups.append({'id':g.id,'name':g.name,'min_select':g.min_select,'max_select':g.max_select,'active':g.active,'options':options})
    promotions=[{'id':x.id,'name':x.name,'price':amount(x.price_cents) if x.price_cents is not None else None,'discount_percent':x.discount_percent,'weekdays':x.weekdays,'start_time':x.start_time,'end_time':x.end_time,'start_date':x.start_date,'end_date':x.end_date,'active':x.active}
                for x in promo_rows]
    active_promo=next((x for x in promo_rows if promotion_is_active(x)),None)
    current_cents=apply_promotion(p.price_cents,active_promo)
    return {'id':p.id,'name':p.name,'category':p.category,'station':p.station,'price':amount(p.price_cents),
            'current_price':amount(current_cents),'promotion':({'id':active_promo.id,'name':active_promo.name,'price':amount(current_cents),'fixed_price':amount(active_promo.price_cents) if active_promo.price_cents is not None else None,'discount_percent':active_promo.discount_percent} if active_promo else None),
            'variants':variants,'modifier_groups':groups,'promotions':promotions,
            'active':p.active,'blocked':p.blocked,'inventory_enabled':p.inventory_enabled,'stock':p.stock,
            'inventory_only':p.inventory_only,'inventory_unit':p.inventory_unit,
            'version':p.version,'status':status}

def _pin_lookup(pin: str) -> str:
    return hmac.new(SECRET_KEY.encode('utf-8'),
                    ('el-mitote/pin-v1/' + pin).encode('ascii'),
                    hashlib.sha256).hexdigest()

def _pin_format(pin: str) -> bool:
    return isinstance(pin, str) and bool(re.fullmatch(r'[0-9]{6}', pin))

def _set_user_pin(db: Session, target: User, pin: str):
    if not _pin_format(pin):
        raise HTTPException(400, 'El PIN debe tener exactamente 6 números')
    digest = _pin_lookup(pin)
    duplicate = db.scalar(select(UserPin).where(UserPin.lookup == digest))
    if duplicate and duplicate.user_id != target.id:
        raise HTTPException(409, 'Este PIN ya pertenece a otra persona. Elige uno distinto.')
    row = db.get(UserPin, target.id)
    if row is None:
        db.add(UserPin(user_id=target.id, lookup=digest, pin_hash=ph.hash(pin)))
    else:
        row.lookup = digest
        row.pin_hash = ph.hash(pin)
    target.session_version = (target.session_version or 1) + 1

class PinLoginIn(BaseModel):
    pin: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')

@app.post('/api/login/pin')
def login_pin(data: PinLoginIn, request: Request, db: Session = Depends(db_session)):
    # Protección contra fuerza bruta por IP sin revelar si existe el PIN.
    addr = request.client.host if request.client else 'unknown'
    identifier = 'pin:' + addr
    attempts = [t for t in login_failures.get(identifier, []) if time.time() - t < 600]
    if len(attempts) >= 15:
        raise HTTPException(429, 'Demasiados intentos. Espera 10 minutos.')
    lookup_started = time.perf_counter()
    match = db.execute(
        select(UserPin, User)
        .join(User, User.id == UserPin.user_id)
        .where(UserPin.lookup == _pin_lookup(data.pin))
    ).first()
    lookup_ms = (time.perf_counter() - lookup_started) * 1000
    rec, user = match if match else (None, None)

    verify_started = time.perf_counter()
    try:
        valid = bool(user and user.active and ph.verify(rec.pin_hash, data.pin))
    except (VerifyMismatchError, VerificationError):
        valid = False
    verify_ms = (time.perf_counter() - verify_started) * 1000

    total_ms = lookup_ms + verify_ms
    if total_ms >= 250:
        http_log.warning(
            "slow_pin_login lookup_ms=%.1f argon2_ms=%.1f total_ms=%.1f outcome=%s",
            lookup_ms, verify_ms, total_ms, "success" if valid else "rejected",
        )
    if not valid:
        attempts.append(time.time())
        login_failures[identifier] = attempts
        raise HTTPException(401, 'PIN incorrecto')
    login_failures.pop(identifier, None)
    token = jwt.encode({'sub':str(user.id),'ver':user.session_version,
                        'exp':now()+timedelta(hours=10)}, SECRET_KEY, algorithm='HS256')
    return {'token':token,
            'user':{'id':user.id,'display':user.display,
                    'username':user.username,'role':user.role}}

class LoginIn(BaseModel): username:str; secret:str
@app.post('/api/login')
def login(data:LoginIn,request:Request,db:Session=Depends(db_session)):
    # Un límite por IP evita saltar el bloqueo probando distintos usuarios.
    identifier=f"password:{request.client.host if request.client else 'unknown'}"
    attempts=[t for t in login_failures.get(identifier,[]) if time.time()-t<900]
    if len(attempts)>=7:raise HTTPException(429,'Demasiados intentos. Espera 15 minutos')
    user=db.scalar(select(User).where(User.username==data.username.strip()))
    try: valid=bool(user and user.active and ph.verify(user.password_hash,data.secret))
    except (VerifyMismatchError,VerificationError): valid=False
    if not valid:
        attempts.append(time.time());login_failures[identifier]=attempts
        raise HTTPException(401,'Credenciales incorrectas')
    # El acceso con contraseña antigua es solo recuperación del propietario.
    # Incluso los perfiles antiguos sin PIN deben activarlo antes de operar.
    if user.role != 'owner':
        raise HTTPException(403, 'El acceso de empleados requiere PIN personal de seis dígitos')
    login_failures.pop(identifier,None)
    token=jwt.encode({'sub':str(user.id),'ver':user.session_version,'exp':now()+timedelta(hours=10)},SECRET_KEY,algorithm='HS256')
    return {'token':token,'user':{'id':user.id,'display':user.display,'username':user.username,'role':user.role}}

@app.get('/api/me')
def me(user:User=Depends(auth)):
    return {'id':user.id,'username':user.username,'display':user.display,'role':user.role}

@app.post('/api/auth/refresh')
def refresh_auth(user:User=Depends(auth)):
    """Renueva la sesión activa antes de que venza, sin cambiar permisos ni versión de sesión."""
    token=jwt.encode({'sub':str(user.id),'ver':user.session_version,'exp':now()+timedelta(hours=10)},SECRET_KEY,algorithm='HS256')
    return {'token':token}

# Notificaciones push para los roles operativos. La suscripción pertenece al usuario autenticado.
PUSH_ROLES = ('waiter', 'kitchen', 'bar')
_push_tests = {}

def _valid_push_endpoint(endpoint):
    try:
        p=urlparse(endpoint)
        host=(p.hostname or '').lower()
        return (p.scheme=='https' and p.port in (None,443) and not p.username and
                not p.password and not p.fragment and len(endpoint)<=2048 and
                (host=='fcm.googleapis.com' or host=='updates.push.services.mozilla.com' or
                 host.endswith('.push.apple.com') or host.endswith('.notify.windows.com') or
                 host.endswith('.push.services.mozilla.com')))
    except (ValueError,TypeError):
        return False

class PushKeysIn(BaseModel):
    p256dh:str=Field(min_length=30,max_length=512)
    auth:str=Field(min_length=8,max_length=512)

class PushSubscribeIn(BaseModel):
    endpoint:str=Field(min_length=20,max_length=2048)
    keys:PushKeysIn

class PushUnsubscribeIn(BaseModel):
    endpoint:str=Field(min_length=20,max_length=2048)

@app.get('/api/push/config')
def push_config(user:User=Depends(auth)):
    allow(user,*PUSH_ROLES)
    # Diagnóstico seguro: solo nombres de variables faltantes, nunca valores.
    issues = []
    if not VAPID_PUBLIC_KEY:
        issues.append('Falta VAPID_PUBLIC_KEY en el proceso de Render')
    if not VAPID_PRIVATE_KEY:
        issues.append('Falta VAPID_PRIVATE_KEY en el proceso de Render')
    if not VAPID_SUBJECT:
        issues.append('Falta VAPID_SUBJECT en el proceso de Render')
    elif not (VAPID_SUBJECT.startswith('mailto:') or VAPID_SUBJECT.startswith('https://')):
        issues.append('VAPID_SUBJECT debe comenzar con mailto: o https://')
    return {
        'enabled': PUSH_ENABLED,
        'public_key': VAPID_PUBLIC_KEY if PUSH_ENABLED else '',
        'diagnostic': issues,
    }

@app.post('/api/push/subscribe')
def push_subscribe(data:PushSubscribeIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,*PUSH_ROLES)
    if not PUSH_ENABLED:raise HTTPException(503,'Notificaciones sin configurar en Render')
    if not _valid_push_endpoint(data.endpoint):raise HTTPException(400,'Proveedor push no admitido')
    row=db.scalar(select(PushDevice).where(PushDevice.endpoint==data.endpoint))
    if row:
        row.user_id=user.id;row.p256dh=data.keys.p256dh;row.auth_key=data.keys.auth
    else:
        db.add(PushDevice(user_id=user.id,endpoint=data.endpoint,p256dh=data.keys.p256dh,auth_key=data.keys.auth))
    db.commit()
    return {'ok':True}

@app.post('/api/push/unsubscribe')
def push_unsubscribe(data:PushUnsubscribeIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,*PUSH_ROLES)
    row=db.scalar(select(PushDevice).where(PushDevice.endpoint==data.endpoint,PushDevice.user_id==user.id))
    if row:db.delete(row);db.commit()
    return {'ok':True}

def _push_one(row,payload):
    try:
        webpush(subscription_info={'endpoint':row['endpoint'],
                  'keys':{'p256dh':row['p256dh'],'auth':row['auth_key']}},
                data=json.dumps(payload,ensure_ascii=False),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub':VAPID_SUBJECT},ttl=43200,timeout=5)
        return {'ok':True,'expired_id':None,'error':None}
    except WebPushException as exc:
        if exc.status_code in (404,410):
            return {'ok':False,'expired_id':row['id'],'error':f'HTTP {exc.status_code}'}
        log.warning('Fallo push id=%s codigo=%s',row['id'],exc.status_code)
        return {'ok':False,'expired_id':None,'error':f'HTTP {exc.status_code or "desconocido"}'}
    except Exception as exc:
        log.exception('Error de notificación push id=%s',row['id'])
        return {'ok':False,'expired_id':None,'error':type(exc).__name__}


async def push_to(payload,role=None,user_id=None):
    if not PUSH_ENABLED:
        return {'enabled':False,'devices':0,'sent':0,'failed':0,'error':'Web Push no configurado'}
    try:
        with SessionLocal() as db:
            q=select(PushDevice).join(User,PushDevice.user_id==User.id).where(User.active.is_(True))
            if role:q=q.where(User.role==role)
            if user_id:q=q.where(User.id==user_id)
            rows=[{'id':d.id,'endpoint':d.endpoint,'p256dh':d.p256dh,'auth_key':d.auth_key}
                  for d in db.scalars(q)]
        if not rows:
            return {'enabled':True,'devices':0,'sent':0,'failed':0,'error':'Sin dispositivos Web Push registrados'}
        results=await asyncio.gather(*(asyncio.to_thread(_push_one,r,payload) for r in rows),return_exceptions=True)
        normalized=[x if isinstance(x,dict) else {
            'ok':False,'expired_id':None,'error':type(x).__name__
        } for x in results]
        expired=[x['expired_id'] for x in normalized if x.get('expired_id') is not None]
        if expired:
            with SessionLocal() as db:
                for rid in expired:
                    d=db.get(PushDevice,rid)
                    if d:db.delete(d)
                db.commit()
        sent=sum(1 for x in normalized if x.get('ok'))
        errors=[str(x.get('error')) for x in normalized if x.get('error')]
        return {'enabled':True,'devices':len(rows),'sent':sent,'failed':len(rows)-sent,
                'error':'; '.join(errors[:3]) if errors else None}
    except Exception as exc:
        log.exception('No se pudieron distribuir notificaciones push')
        return {'enabled':True,'devices':0,'sent':0,'failed':1,'error':type(exc).__name__}

@app.post('/api/push/test')
async def push_test(user:User=Depends(auth)):
    allow(user,*PUSH_ROLES)
    if not PUSH_ENABLED:raise HTTPException(503,'Configura las claves VAPID en Render')
    last=_push_tests.get(user.id,0)
    if time.monotonic()-last<12:raise HTTPException(429,'Espera unos segundos para repetir la prueba')
    _push_tests[user.id]=time.monotonic()
    await push_to({'title':'El Mitote · Prueba de notificación',
                   'body':'Las notificaciones están activadas en este dispositivo.',
                   'url':'/','tag':f'mitote-test-{user.id}'},user_id=user.id)
    return {'ok':True,'message':'Prueba enviada a tus dispositivos registrados'}

# Firebase Cloud Messaging para la APK Android (independiente de Web Push/VAPID).
class FcmTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=2048)

@app.post('/api/fcm/register')
def fcm_register(data: FcmTokenIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, *PUSH_ROLES)
    device = db.scalar(select(FcmDevice).where(FcmDevice.token == data.token))
    if device:
        device.user_id = user.id
        device.updated_at = now()
    else:
        db.add(FcmDevice(user_id=user.id, token=data.token))
    db.commit()
    return {'ok': True}

@app.post('/api/fcm/unregister')
def fcm_unregister(data: FcmTokenIn, db: Session = Depends(db_session), user: User = Depends(auth)):
    device = db.scalar(select(FcmDevice).where(FcmDevice.token == data.token, FcmDevice.user_id == user.id))
    if device:
        db.delete(device)
        db.commit()
    return {'ok': True}

def _fcm_send_one(device, payload):
    route = 'station' if 'push=station' in payload.get('url','') else ('accounts' if 'push=accounts' in payload.get('url','') else '')
    msg = messaging.Message(
        token=device['token'],
        data={'title': str(payload.get('title','El Mitote Comandas'))[:100],
              'body': str(payload.get('body','Nuevo aviso'))[:180],
              'route': route,
              'kind': str(payload.get('kind','general'))[:32],
              'tag': str(payload.get('tag','mitote-aviso'))[:100],
              'event_id': str(payload.get('event_id',''))[:90],
              'recipient_id': str(payload.get('recipient_id',''))[:40]},
        android=messaging.AndroidConfig(priority='high', ttl=timedelta(hours=12))
    )
    try:
        message_id=messaging.send(msg)
        return {'ok':True,'message_id':message_id,'expired_id':None,'error':None}
    except Exception as exc:
        if isinstance(exc, getattr(messaging, 'UnregisteredError', type(None))):
            return {'ok':False,'message_id':None,'expired_id':device['id'],'error':'Token FCM no registrado'}
        log.warning('FCM no pudo aceptar el dispositivo %s: %s', device['id'], type(exc).__name__)
        return {'ok':False,'message_id':None,'expired_id':None,'error':type(exc).__name__}


async def fcm_to(payload, role=None, user_id=None):
    if not _firebase_ready():
        return {'enabled':False,'devices':0,'sent':0,'failed':0,'error':'Firebase Cloud Messaging no configurado'}
    try:
        with SessionLocal() as db:
            q = select(FcmDevice).join(User, FcmDevice.user_id == User.id).where(User.active.is_(True))
            if role:
                q = q.where(User.role == role)
            if user_id:
                q = q.where(User.id == user_id)
            devices = [{'id': x.id, 'token': x.token} for x in db.scalars(q)]
        if not devices:
            return {'enabled':True,'devices':0,'sent':0,'failed':0,'error':'Sin dispositivos FCM registrados'}
        results = await asyncio.gather(*(asyncio.to_thread(_fcm_send_one, d, payload) for d in devices), return_exceptions=True)
        normalized=[x if isinstance(x,dict) else {
            'ok':False,'message_id':None,'expired_id':None,'error':type(x).__name__
        } for x in results]
        expired = [x['expired_id'] for x in normalized if x.get('expired_id') is not None]
        if expired:
            with SessionLocal() as db:
                for device_id in expired:
                    old = db.get(FcmDevice, device_id)
                    if old:
                        db.delete(old)
                db.commit()
        sent=sum(1 for x in normalized if x.get('ok'))
        errors=[str(x.get('error')) for x in normalized if x.get('error')]
        return {'enabled':True,'devices':len(devices),'sent':sent,'failed':len(devices)-sent,
                'error':'; '.join(errors[:3]) if errors else None}
    except Exception as exc:
        log.exception('Fallo el envío FCM')
        return {'enabled':True,'devices':0,'sent':0,'failed':1,'error':type(exc).__name__}

@app.post('/api/fcm/test')
async def fcm_test(db: Session = Depends(db_session), user: User = Depends(auth)):
    allow(user, *PUSH_ROLES)
    if not _firebase_ready():
        raise HTTPException(503, 'Configura FIREBASE_SERVICE_ACCOUNT_JSON en Render')
    count = db.scalar(select(func.count(FcmDevice.id)).where(FcmDevice.user_id == user.id)) or 0
    if count == 0:
        raise HTTPException(409, 'Este usuario aún no tiene dispositivos Android registrados. Espera unos segundos.')
    last = _push_tests.get(('fcm', user.id), 0)
    if time.monotonic() - last < 15:
        raise HTTPException(429, 'Espera 15 segundos para repetir la prueba')
    _push_tests[('fcm', user.id)] = time.monotonic()
    await fcm_to({'title': 'El Mitote · Prueba Android', 'body': 'Avisos FCM configurados.', 'url': '/'}, user_id=user.id)
    return {'ok': True, 'devices': count, 'message': 'Solicitud de envío realizada; confirma la recepción en Android'}

@app.get('/api/events')
async def events(request:Request,authorization:Optional[str]=Header(None)):
    if not authorization or not authorization.startswith('Bearer '):raise HTTPException(401,'Sesión inválida')
    try: payload=jwt.decode(authorization[7:],SECRET_KEY,algorithms=['HS256'])
    except jwt.PyJWTError: raise HTTPException(401,'Sesión inválida')
    with SessionLocal() as db:
        u=db.get(User,int(payload['sub']))
        if not u or not u.active or payload.get('ver')!=u.session_version: raise HTTPException(401,'Acceso desactivado')
    async def stream():
        q=asyncio.Queue(maxsize=3);listeners.add(q)
        try:
            yield 'data: refresh\n\n'
            while not await request.is_disconnected():
                try: msg=await asyncio.wait_for(q.get(),timeout=22)
                except asyncio.TimeoutError: yield ': keepalive\n\n';continue
                yield f'data: {msg}\n\n'
        finally: listeners.discard(q)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

class StaffCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    role: Literal['admin','cashier','waiter','kitchen','bar']
    pin: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')

@app.post('/api/staff')
async def create_staff(data: StaffCreateIn, db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(400, 'Escribe el nombre del empleado')
    slug = unicodedata.normalize('NFKD', name).encode('ascii','ignore').decode('ascii').lower()
    base = re.sub(r'[^a-z0-9]+','-',slug).strip('-')[:65] or 'empleado'
    username = base
    num = 2
    while db.scalar(select(User.id).where(User.username == username)):
        username = f'{base}-{num}'
        num += 1
    target = User(username=username,display=name,role=data.role,
                  password_hash=ph.hash(secrets.token_urlsafe(32)),active=True)
    db.add(target)
    db.flush()
    _set_user_pin(db, target, data.pin)
    audit(db, user, 'staff.create', id=target.id, role=target.role, name=name)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'El PIN o el identificador del empleado ya está en uso')
    await notify()
    return {'id':target.id,'display':target.display,'role':target.role}

class StaffPinIn(BaseModel):
    pin: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')

@app.patch('/api/staff/{user_id}/pin')
async def assign_staff_pin(user_id: int, data: StaffPinIn,
                           db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'Personal no encontrado')
    _set_user_pin(db, target, data.pin)
    audit(db,user,'staff.pin_set',id=target.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'El PIN ya pertenece a otra persona')
    await notify()
    return {'ok':True,'owner_pin_changed':target.id == user.id}

class UserIn(BaseModel): username:str=Field(min_length=3,max_length=80);display:str=Field(min_length=2,max_length=100);role:Literal['admin','cashier','waiter','kitchen','bar'];secret:str=Field(min_length=6,max_length=6)
@app.get('/api/users')
def users(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    rows=db.scalars(select(User).order_by(User.id)).all()
    user_ids=[row.id for row in rows]
    pin_ids=set(db.scalars(select(UserPin.user_id).where(
        UserPin.user_id.in_(user_ids))).all()) if user_ids else set()
    return [{'id':u.id,'username':u.username,'display':u.display,'role':u.role,
             'active':u.active,'has_pin':u.id in pin_ids} for u in rows]
@app.post('/api/users')
async def add_user(data:UserIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner')
    # Compatibilidad con clientes anteriores: nunca crear empleados sin PIN.
    if not _pin_format(data.secret):raise HTTPException(400,'El PIN debe tener exactamente seis números')
    username=data.username.strip()
    display=data.display.strip()
    if not username or not display:raise HTTPException(400,'Nombre y usuario son obligatorios')
    if db.scalar(select(User).where(User.username==username)):raise HTTPException(409,'Usuario ya registrado')
    u=User(username=username,display=display,role=data.role,password_hash=ph.hash(secrets.token_urlsafe(32)))
    db.add(u);db.flush()
    _set_user_pin(db,u,data.secret)
    audit(db,user,'user.create',username=u.username,role=u.role)
    try:db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Usuario o PIN ya registrado')
    await notify()
    return {'id':u.id}

class UserUpdateIn(BaseModel): active:Optional[bool]=None;secret:Optional[str]=None
@app.patch('/api/users/{user_id}')
async def update_user(user_id:int,data:UserUpdateIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner')
    target=db.execute(select(User).where(User.id==user_id).with_for_update()).scalar_one_or_none()
    if not target:raise HTTPException(404,'Usuario no encontrado')
    if target.id==user.id or target.role=='owner':raise HTTPException(403,'No se puede desactivar ni modificar al propietario desde aquí')
    if data.active is not None:target.active=data.active
    if data.secret is not None:
        # La API anterior ya no debe habilitar credenciales sin PIN.
        _set_user_pin(db,target,data.secret)
        target.password_hash=ph.hash(secrets.token_urlsafe(32))
    else:
        target.session_version=(target.session_version or 1)+1
    audit(db,user,'user.update',id=target.id,active=target.active,reset=bool(data.secret))
    try:db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'El PIN ya pertenece a otra persona')
    await notify()
    return {'ok':True}

@app.get('/api/settings')
def settings(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    s=db.get(Setting,1);return {'inventory_on':s.inventory_on,'inventory_reviewed':s.inventory_reviewed}
class SettingsIn(BaseModel): inventory_on:bool;confirmed_recount:bool=False
@app.put('/api/settings')
async def set_settings(data:SettingsIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    s=db.execute(select(Setting).where(Setting.id==1).with_for_update()).scalar_one()
    if data.inventory_on and not s.inventory_on and not data.confirmed_recount:raise HTTPException(409,'Confirma el recuento de existencias antes de activar inventario')
    old=s.inventory_on;s.inventory_on=data.inventory_on;s.inventory_reviewed=data.confirmed_recount if data.inventory_on else False
    audit(db,user,'inventory.global',old=old,new=s.inventory_on);db.commit();await notify();return {'ok':True}

@app.get('/api/products')
def products(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    s=db.get(Setting,1)
    query=select(Product).outerjoin(ArchivedProduct,ArchivedProduct.product_id==Product.id).where(
        ArchivedProduct.product_id.is_(None)).order_by(Product.category,Product.name)
    rows=db.scalars(query).all()
    related=product_related_data(db,rows)
    return [product_dict(p,s,related=related) for p in rows]
class RemoveCategoryIn(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    destination:str=Field(min_length=1,max_length=80)
    expected_visible_count:int=Field(ge=1)

@app.post('/api/categories/remove')
async def remove_category(data:RemoveCategoryIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    source=data.name.strip()
    destination=data.destination.strip()
    if not source or not destination or source.casefold()==destination.casefold():
        raise HTTPException(400,'Indica una categoría destino diferente')
    rows=db.scalars(select(Product).where(Product.category==source).with_for_update()).all()
    if not rows:raise HTTPException(404,'La categoría ya no existe')
    hidden=set(db.scalars(select(ArchivedProduct.product_id).where(
        ArchivedProduct.product_id.in_([p.id for p in rows]))))
    visible_count=sum(p.id not in hidden for p in rows)
    if visible_count!=data.expected_visible_count:
        raise HTTPException(409,'El contenido de la categoría cambió; actualiza antes de borrarla')
    for p in rows:
        p.category=destination
        p.version=(p.version or 1)+1
    audit(db,user,'category.remove',source=source,destination=destination,
          active_products=visible_count,all_products=len(rows))
    db.commit();await notify()
    return {'ok':True,'moved':visible_count,'new_category':destination}

class ProductArchiveIn(BaseModel):
    expected_version:int=Field(ge=1)
    reason:str=Field(min_length=3,max_length=200)

class ProductIn(BaseModel):
    name:str=Field(min_length=1,max_length=160)
    category:str='General'
    station:Literal['kitchen','bar']
    price:Decimal=Field(ge=0)
    active:bool=True
    blocked:bool=False
    inventory_enabled:bool=False
    stock:int=Field(default=0,ge=0)
    stock_reason:Optional[str]=Field(default=None,max_length=400)
    expected_version:Optional[int]=Field(default=None,ge=1)
class ActiveToggleIn(BaseModel):
    active: bool

class ProductVariantIn(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    price: Decimal = Field(ge=0)
    active: bool = True
    sort_order: int = Field(default=0,ge=0,le=10000)

class ModifierGroupIn(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    min_select: int = Field(default=0,ge=0,le=10)
    max_select: int = Field(default=1,ge=1,le=10)
    active: bool = True
    sort_order: int = Field(default=0,ge=0,le=10000)

class ModifierOptionIn(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    price_delta: Decimal = Field(default=Decimal('0'),ge=0)
    active: bool = True
    sort_order: int = Field(default=0,ge=0,le=10000)
    inventory_product_id: Optional[int] = Field(default=None,ge=1)
    inventory_quantity: int = Field(default=1,ge=1,le=100)

class ModifierOptionInventoryIn(BaseModel):
    inventory_product_id: Optional[int] = Field(default=None,ge=1)
    inventory_quantity: int = Field(default=1,ge=1,le=100)

class InventoryItemIn(BaseModel):
    name: str = Field(min_length=2,max_length=160)
    stock: int = Field(default=0,ge=0,le=100000)
    unit: Literal['botellas','latas','unidades','kg','litros'] = 'botellas'

class PromotionIn(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    price: Optional[Decimal] = Field(default=None,ge=0)
    discount_percent: Optional[int] = Field(default=None,ge=1,le=90)
    weekdays: str = Field(default='0,1,2,3,4,5,6',max_length=20)
    start_time: str = Field(default='00:00',min_length=5,max_length=5)
    end_time: str = Field(default='23:59',min_length=5,max_length=5)
    start_date: Optional[str] = Field(default=None,max_length=10)
    end_date: Optional[str] = Field(default=None,max_length=10)

@app.post('/api/products/{product_id}/variants')
async def add_product_variant(product_id:int,data:ProductVariantIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.get(Product,product_id)
    if not p:raise HTTPException(404,'Producto inexistente')
    v=ProductVariant(product_id=p.id,name=data.name.strip(),price_cents=cents(data.price),active=data.active,sort_order=data.sort_order)
    db.add(v);db.flush();audit(db,user,'product.variant.create',product_id=p.id,variant_id=v.id,name=v.name,price=amount(v.price_cents));db.commit();await notify()
    return {'id':v.id,'ok':True}

@app.patch('/api/product-variants/{variant_id}')
async def toggle_product_variant(variant_id:int,data:ActiveToggleIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');v=db.get(ProductVariant,variant_id)
    if not v:raise HTTPException(404,'Presentación inexistente')
    v.active=data.active;audit(db,user,'product.variant.toggle',variant_id=v.id,active=v.active);db.commit();await notify();return {'ok':True}

@app.post('/api/products/{product_id}/modifier-groups')
async def add_modifier_group(product_id:int,data:ModifierGroupIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    if not db.get(Product,product_id):raise HTTPException(404,'Producto inexistente')
    if data.min_select>data.max_select:raise HTTPException(400,'La selección mínima no puede superar la máxima')
    g=ProductModifierGroup(product_id=product_id,name=data.name.strip(),min_select=data.min_select,max_select=data.max_select,active=data.active,sort_order=data.sort_order)
    db.add(g);db.flush();audit(db,user,'product.modifier_group.create',product_id=product_id,group_id=g.id,name=g.name);db.commit();await notify();return {'id':g.id,'ok':True}

@app.post('/api/modifier-groups/{group_id}/options')
async def add_modifier_option(group_id:int,data:ModifierOptionIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');g=db.get(ProductModifierGroup,group_id)
    if not g:raise HTTPException(404,'Grupo de modificadores inexistente')
    stock_item=None
    if data.inventory_product_id is not None:
        stock_item=db.get(Product,data.inventory_product_id)
        if not stock_item or not stock_item.inventory_only or not stock_item.inventory_enabled:
            raise HTTPException(400,'Selecciona un insumo con control de inventario activo')
    o=ProductModifierOption(group_id=g.id,name=data.name.strip(),price_delta_cents=cents(data.price_delta),
        active=data.active,sort_order=data.sort_order,
        inventory_product_id=stock_item.id if stock_item else None,inventory_quantity=data.inventory_quantity)
    db.add(o);db.flush()
    audit(db,user,'product.modifier_option.create',group_id=g.id,option_id=o.id,name=o.name,
          price_delta=amount(o.price_delta_cents),inventory_product_id=o.inventory_product_id)
    db.commit();await notify();return {'id':o.id,'ok':True}

@app.patch('/api/modifier-options/{option_id}/inventory')
async def link_modifier_inventory(option_id:int,data:ModifierOptionInventoryIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    option=db.get(ProductModifierOption,option_id)
    if not option:raise HTTPException(404,'Opción inexistente')
    stock_item=None
    if data.inventory_product_id is not None:
        stock_item=db.get(Product,data.inventory_product_id)
        if not stock_item or not stock_item.inventory_only or not stock_item.inventory_enabled:
            raise HTTPException(400,'Selecciona un insumo con control de inventario activo')
    option.inventory_product_id=stock_item.id if stock_item else None
    option.inventory_quantity=data.inventory_quantity
    audit(db,user,'product.modifier_option.inventory_link',option_id=option.id,
          inventory_product_id=option.inventory_product_id,inventory_quantity=option.inventory_quantity)
    db.commit();await notify();return {'ok':True}

@app.patch('/api/modifier-options/{option_id}')
async def toggle_modifier_option(option_id:int,data:ActiveToggleIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');o=db.get(ProductModifierOption,option_id)
    if not o:raise HTTPException(404,'Modificador inexistente')
    o.active=data.active;audit(db,user,'product.modifier_option.toggle',option_id=o.id,active=o.active);db.commit();await notify();return {'ok':True}

@app.post('/api/products/{product_id}/promotions')
async def add_product_promotion(product_id:int,data:PromotionIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    if not db.get(Product,product_id):raise HTTPException(404,'Producto inexistente')
    if (data.price is None)==(data.discount_percent is None):raise HTTPException(400,'Indica un precio promocional o un porcentaje de descuento, pero no ambos')
    try:
        days={int(x.strip()) for x in data.weekdays.split(',') if x.strip()!=''}
        if not days or any(x<0 or x>6 for x in days):raise ValueError()
        datetime.strptime(data.start_time,'%H:%M');datetime.strptime(data.end_time,'%H:%M')
        if data.start_date:datetime.strptime(data.start_date,'%Y-%m-%d')
        if data.end_date:datetime.strptime(data.end_date,'%Y-%m-%d')
        if data.start_date and data.end_date and data.end_date<data.start_date:raise ValueError()
    except Exception:raise HTTPException(400,'Revisa días, fechas y horario de la promoción')
    promo=ProductPromotion(product_id=product_id,name=data.name.strip(),price_cents=cents(data.price) if data.price is not None else None,
        discount_percent=data.discount_percent,weekdays=','.join(str(x) for x in sorted(days)),start_time=data.start_time,end_time=data.end_time,
        start_date=data.start_date or None,end_date=data.end_date or None,active=True)
    db.add(promo);db.flush();audit(db,user,'product.promotion.create',product_id=product_id,promotion_id=promo.id,name=promo.name);db.commit();await notify();return {'id':promo.id,'ok':True}

@app.patch('/api/promotions/{promotion_id}')
async def toggle_product_promotion(promotion_id:int,data:ActiveToggleIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');promo=db.get(ProductPromotion,promotion_id)
    if not promo:raise HTTPException(404,'Promoción inexistente')
    promo.active=data.active;audit(db,user,'product.promotion.toggle',promotion_id=promo.id,active=promo.active);db.commit();await notify();return {'ok':True}

@app.post('/api/products')
async def add_product(data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=Product(name=data.name.strip(),category=data.category.strip()[:80],station=data.station,price_cents=cents(data.price),active=data.active,blocked=data.blocked,inventory_enabled=data.inventory_enabled,stock=data.stock)
    db.add(p);db.flush()
    if data.inventory_enabled:
        db.add(InventoryMovement(product_id=p.id,actor_id=user.id,kind='opening',
            quantity_change=p.stock,before_stock=0,after_stock=p.stock,
            reason='Saldo inicial al crear producto',source_key=f'inventory-opening:{p.id}',created_at=now()))
    audit(db,user,'product.create',name=p.name);db.commit();await notify();return {'id':p.id}
@app.get('/api/products/archived')
def archived_products(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    rows=db.execute(select(ArchivedProduct,Product).join(
        Product,Product.id==ArchivedProduct.product_id).order_by(ArchivedProduct.archived_at.desc()).limit(150)).all()
    return [{'id':p.id,'name':p.name,'station':p.station,'version':p.version,
             'reason':a.reason,'archived_at':a.archived_at.isoformat() if a.archived_at else ''}
            for a,p in rows]

@app.post('/api/products/{product_id}/archive')
async def archive_product(product_id:int,data:ProductArchiveIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if db.get(ArchivedProduct,p.id):raise HTTPException(409,'Este producto ya está eliminado del menú')
    if p.version!=data.expected_version:raise HTTPException(409,'El producto cambió; actualiza la pantalla antes de eliminar')
    db.add(ArchivedProduct(product_id=p.id,archived_by=user.id,reason=data.reason.strip(),
                           original_active=bool(p.active),original_blocked=bool(p.blocked)))
    p.active=False;p.blocked=True;p.version+=1
    audit(db,user,'product.archive',product_id=p.id,name=p.name,reason=data.reason.strip())
    db.commit();await notify()
    return {'ok':True,'archived':True}

class ProductRestoreIn(BaseModel):
    expected_version:int=Field(ge=1)

@app.post('/api/products/{product_id}/restore')
async def restore_product(product_id:int,data:ProductRestoreIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    a=db.get(ArchivedProduct,product_id)
    if not p or not a:raise HTTPException(404,'Producto archivado inexistente')
    if p.version!=data.expected_version:raise HTTPException(409,'El producto cambió; actualiza la pantalla')
    p.active=bool(a.original_active);p.blocked=bool(a.original_blocked);p.version+=1
    db.delete(a)
    audit(db,user,'product.restore',product_id=p.id,name=p.name)
    db.commit();await notify()
    return {'ok':True,'restored':True}

@app.put('/api/products/{product_id}')
async def edit_product(product_id:int,data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if data.expected_version is None or p.version != data.expected_version:
        raise HTTPException(409,'El producto o sus existencias cambiaron. Actualiza la pantalla antes de guardar')
    before=product_dict(p,db.get(Setting,1),db)
    old_stock=p.stock;was_inventory_enabled=p.inventory_enabled
    if data.stock!=old_stock and not (data.stock_reason or '').strip():
        raise HTTPException(400,'Indica el motivo del ajuste de existencias')
    if data.inventory_enabled and not was_inventory_enabled:
        opening_key=f'inventory-opening:{p.id}'
        if not db.scalar(select(InventoryMovement.id).where(InventoryMovement.source_key==opening_key)):
            db.add(InventoryMovement(product_id=p.id,actor_id=user.id,kind='opening',
                quantity_change=old_stock,before_stock=0,after_stock=old_stock,
                reason='Saldo inicial al activar control individual',source_key=opening_key,created_at=now()))
    p.name=data.name.strip();p.category=data.category.strip()[:80];p.station=data.station;p.price_cents=cents(data.price);p.active=data.active;p.blocked=data.blocked;p.inventory_enabled=data.inventory_enabled;p.stock=data.stock;p.version+=1
    if data.stock!=old_stock:
        db.add(InventoryMovement(product_id=p.id,actor_id=user.id,kind='adjustment',
            quantity_change=data.stock-old_stock,before_stock=old_stock,after_stock=data.stock,
            reason=data.stock_reason.strip(),source_key=f'inventory-adjust-product:{secrets.token_urlsafe(18)}',created_at=now()))
        audit(db,user,'inventory.adjustment',product_id=p.id,before=old_stock,delta=data.stock-old_stock,
              after=data.stock,reason=data.stock_reason.strip(),source='product.edit')
    audit(db,user,'product.update',id=p.id,before=before,after=product_dict(p,db.get(Setting,1),db));db.commit();await notify();return {'ok':True}
class RestockIn(BaseModel): delta:int=Field(gt=0,le=100000);reason:str=Field(min_length=3,max_length=400);expected_version:int
@app.post('/api/products/{product_id}/restock')
async def restock(product_id:int,data:RestockIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if p.version!=data.expected_version:raise HTTPException(409,'Existencias cambiaron; actualiza la pantalla')
    before=p.stock;p.stock+=data.delta;p.version+=1
    db.add(InventoryMovement(product_id=p.id,actor_id=user.id,kind='restock',
        quantity_change=data.delta,before_stock=before,after_stock=p.stock,reason=data.reason.strip(),
        source_key=f'inventory-restock:{secrets.token_urlsafe(18)}',created_at=now()))
    audit(db,user,'inventory.restock',product_id=p.id,before=before,delta=data.delta,after=p.stock,reason=data.reason)
    db.commit();await notify();return {'stock':p.stock}

class InventoryAdjustmentIn(BaseModel):
    mode:Literal['out','count']
    quantity:int=Field(ge=0,le=100000)
    reason:str=Field(min_length=3,max_length=400)
    expected_version:int=Field(ge=1)

@app.post('/api/products/{product_id}/inventory-adjustment')
async def inventory_adjustment(product_id:int,data:InventoryAdjustmentIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if p.version!=data.expected_version:raise HTTPException(409,'Existencias cambiaron; actualiza la pantalla')
    before=p.stock
    if data.mode=='out':
        if data.quantity<1:raise HTTPException(400,'La salida debe ser de al menos una unidad')
        if data.quantity>before:raise HTTPException(409,'La salida supera las existencias disponibles')
        after=before-data.quantity;kind='manual_out';delta=-data.quantity
    else:
        after=data.quantity;delta=after-before;kind='adjustment'
        if delta==0:raise HTTPException(400,'El recuento coincide con las existencias actuales')
    p.stock=after;p.version+=1
    db.add(InventoryMovement(product_id=p.id,actor_id=user.id,kind=kind,
        quantity_change=delta,before_stock=before,after_stock=after,reason=data.reason.strip(),
        source_key=f'inventory-manual:{secrets.token_urlsafe(18)}',created_at=now()))
    audit(db,user,'inventory.manual_movement',product_id=p.id,kind=kind,
          before=before,delta=delta,after=after,reason=data.reason.strip())
    db.commit();await notify();return {'stock':p.stock,'delta':delta}

@app.post('/api/inventory/items')
async def create_inventory_item(data:InventoryItemIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    name=data.name.strip()
    duplicate=db.scalar(select(Product.id).where(
        Product.inventory_only.is_(True),func.lower(Product.name)==name.lower()))
    if duplicate:raise HTTPException(409,'Ya existe un insumo con ese nombre')
    item=Product(name=name,category='Insumos',station='bar',price_cents=0,
        active=False,blocked=True,inventory_enabled=True,stock=data.stock,
        inventory_only=True,inventory_unit=data.unit)
    db.add(item);db.flush()
    db.add(InventoryMovement(product_id=item.id,actor_id=user.id,kind='opening',
        quantity_change=item.stock,before_stock=0,after_stock=item.stock,
        reason='Saldo inicial al registrar insumo',source_key=f'inventory-opening:{item.id}',created_at=now()))
    audit(db,user,'inventory.item.create',product_id=item.id,name=item.name,stock=item.stock,unit=item.inventory_unit)
    db.commit();await notify()
    return {'id':item.id,'ok':True}

@app.get('/api/inventory/movements')
def inventory_movements(product_id:Optional[int]=None,kind:Optional[str]=None,
                        from_date:Optional[str]=None,to_date:Optional[str]=None,
                        limit:int=120,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    filters=[]
    if product_id is not None:filters.append(InventoryMovement.product_id==product_id)
    allowed_kinds={'opening','restock','sale','return','manual_out','adjustment'}
    if kind:
        if kind not in allowed_kinds:raise HTTPException(400,'Tipo de movimiento inválido')
        filters.append(InventoryMovement.kind==kind)
    tz=ZoneInfo('America/Mexico_City')
    def local_boundary(value,exclusive_end=False):
        if not value:return None
        try:
            day=datetime.strptime(value,'%Y-%m-%d')
            if exclusive_end:day+=timedelta(days=1)
            return day.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400,'Fecha inválida; utiliza AAAA-MM-DD')
    start=local_boundary(from_date)
    end=local_boundary(to_date,True)
    if start and end and start>=end:raise HTTPException(400,'El rango de fechas no es válido')
    if start:filters.append(InventoryMovement.created_at>=start)
    if end:filters.append(InventoryMovement.created_at<end)
    summary_rows=db.execute(
        select(InventoryMovement.kind,func.coalesce(func.sum(InventoryMovement.quantity_change),0))
        .where(*filters).group_by(InventoryMovement.kind)
    ).all()
    summary={name:int(value or 0) for name,value in summary_rows}
    stmt=(select(InventoryMovement,Product,User,Account)
          .join(Product,Product.id==InventoryMovement.product_id)
          .outerjoin(User,User.id==InventoryMovement.actor_id)
          .outerjoin(Account,Account.id==InventoryMovement.account_id)
          .where(*filters)
          .order_by(InventoryMovement.created_at.desc(),InventoryMovement.id.desc())
          .limit(max(1,min(limit,300))))
    rows=db.execute(stmt).all()
    movements=[]
    for movement,product,actor,account in rows:
        movements.append({
            'id':movement.id,'product_id':product.id,'product_name':product.name,
            'category':product.category,'kind':movement.kind,'inventory_unit':product.inventory_unit,
            'quantity_change':movement.quantity_change,'before_stock':movement.before_stock,
            'after_stock':movement.after_stock,'reason':movement.reason,
            'actor_name':actor.display if actor else 'Sistema',
            'account_id':movement.account_id,'account_name':movement.account_name or (account.name if account else ''),
            'account_status':account.status if account else '',
            'account_closed_at':account.closed_at.isoformat() if account and account.closed_at else '',
            'table_name':movement.table_name or '',
            'batch_id':movement.batch_id,'order_item_id':movement.order_item_id,
            'created_at':movement.created_at.isoformat() if movement.created_at else ''
        })
    return {'summary':summary,'movements':movements,'limit':max(1,min(limit,300))}

def dining_table_dict(db,t):
    occupancy=db.get(TableOccupancy,t.id)
    account=db.get(Account,occupancy.account_id) if occupancy else None
    owner=db.get(User,account.owner_id) if account else None
    occupied=bool(account and account.status in ('open','pending'))
    return {'id':t.id,'name':t.name,'area':t.area,'capacity':t.capacity,'active':t.active,
            'sort_order':t.sort_order,'status':'occupied' if occupied else 'available',
            'account_id':account.id if occupied else None,'account_name':account.name if occupied else None,
            'waiter':owner.display if owner else None}

def dining_table_dict_many(db,tables):
    """Resuelve ocupación, cuenta y mesero de todas las mesas con una consulta."""
    if not tables:
        return []
    table_ids=[t.id for t in tables]
    rows=db.execute(
        select(TableOccupancy.table_id,Account.id,Account.name,Account.status,User.display)
        .join(Account,Account.id==TableOccupancy.account_id)
        .outerjoin(User,User.id==Account.owner_id)
        .where(TableOccupancy.table_id.in_(table_ids))
    ).all()
    by_table={row[0]:row for row in rows}
    result=[]
    for table in tables:
        row=by_table.get(table.id)
        occupied=bool(row and row[3] in ('open','pending'))
        result.append({
            'id':table.id,'name':table.name,'area':table.area,'capacity':table.capacity,
            'active':table.active,'sort_order':table.sort_order,
            'status':'occupied' if occupied else 'available',
            'account_id':row[1] if occupied else None,
            'account_name':row[2] if occupied else None,
            'waiter':row[4] if row else None
        })
    return result


@app.get('/api/tables')
def dining_tables(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier','waiter')
    q=select(DiningTable).order_by(DiningTable.area,DiningTable.sort_order,DiningTable.id)
    if user.role not in ('owner','admin'):
        q=q.where(DiningTable.active.is_(True))
    rows=db.scalars(q).all()
    return dining_table_dict_many(db,rows)

class DiningTableIn(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    area:str=Field(default='Salón',min_length=1,max_length=80)
    capacity:int=Field(default=4,ge=1,le=30)
    active:bool=True
    sort_order:int=Field(default=0,ge=0,le=10000)

@app.post('/api/tables')
async def create_dining_table(data:DiningTableIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    name=data.name.strip()
    area=data.area.strip()
    if not name or not area:raise HTTPException(400,'Nombre y área son obligatorios')
    name_key=name.casefold()
    if db.scalar(select(DiningTable.id).where(DiningTable.name_key==name_key)):
        raise HTTPException(409,'Ya existe una mesa con ese nombre')
    t=DiningTable(name=name,name_key=name_key,area=area,capacity=data.capacity,active=data.active,sort_order=data.sort_order)
    db.add(t);db.flush()
    audit(db,user,'table.create',table_id=t.id,name=t.name,area=t.area,capacity=t.capacity,active=t.active)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Ya existe una mesa con ese nombre')
    await notify()
    return {'id':t.id,'ok':True}

@app.put('/api/tables/{table_id}')
async def update_dining_table(table_id:int,data:DiningTableIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    t=db.execute(select(DiningTable).where(DiningTable.id==table_id).with_for_update()).scalar_one_or_none()
    if not t:raise HTTPException(404,'Mesa no encontrada')
    name=data.name.strip();area=data.area.strip();name_key=name.casefold()
    if not name or not area:raise HTTPException(400,'Nombre y área son obligatorios')
    duplicate=db.scalar(select(DiningTable.id).where(DiningTable.name_key==name_key,DiningTable.id!=table_id))
    if duplicate:raise HTTPException(409,'Ya existe otra mesa con ese nombre')
    occupancy=db.get(TableOccupancy,t.id)
    if occupancy and not data.active:raise HTTPException(409,'No puedes desactivar una mesa ocupada')
    before={'name':t.name,'area':t.area,'capacity':t.capacity,'active':t.active,'sort_order':t.sort_order}
    t.name=name;t.name_key=name_key;t.area=area;t.capacity=data.capacity;t.active=data.active;t.sort_order=data.sort_order
    audit(db,user,'table.update',table_id=t.id,before=before,after={'name':t.name,'area':t.area,'capacity':t.capacity,'active':t.active,'sort_order':t.sort_order})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Ya existe otra mesa con ese nombre')
    await notify()
    return {'ok':True}

@app.get('/api/accounts')
def accounts(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier','waiter')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return []  # Los cierres anteriores se consultan solo desde el historial de caja.
    q=select(Account).where(Account.created_at>=p.opened_at).order_by(Account.id.desc())
    if user.role=='waiter':q=q.where(Account.owner_id==user.id)
    else:q=q.limit(200)
    rows=db.scalars(q).all()
    return account_dict_many(db,rows)

@app.get('/api/overview')
def overview(db:Session=Depends(db_session),user:User=Depends(auth)):
    """Resumen compacto del panel: métricas agregadas sin descargar listas completas."""
    allow(user,'owner','admin')
    period=db.scalar(select(CashPeriod).where(
        CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    status_counts={}
    new_tickets=0
    if period:
        status_counts=dict(db.execute(
            select(Account.status,func.count(Account.id))
            .where(Account.created_at>=period.opened_at)
            .group_by(Account.status)
        ).all())
        new_tickets=db.scalar(
            select(func.count(StationTicket.id))
            .join(Batch,Batch.id==StationTicket.batch_id)
            .where(Batch.created_at>=period.opened_at,StationTicket.status=='new')
        ) or 0
    product_count=db.scalar(
        select(func.count(Product.id))
        .outerjoin(ArchivedProduct,ArchivedProduct.product_id==Product.id)
        .where(ArchivedProduct.product_id.is_(None))
    ) or 0
    return {
        'active_accounts':int(status_counts.get('open',0)+status_counts.get('pending',0)),
        'pending_accounts':int(status_counts.get('pending',0)),
        'new_tickets':int(new_tickets),
        'products':int(product_count),
    }
class AccountIn(BaseModel):
    name:Optional[str]=Field(default=None,max_length=120)
    table_id:Optional[int]=None
    client_key:Optional[str]=Field(default=None,max_length=80)

@app.post('/api/accounts')
async def new_account(data:AccountIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','waiter')
    client_key=(data.client_key or '').strip()
    payload={'name':(data.name or '').strip(),'table_id':data.table_id}
    request_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def existing_create():
        if not client_key:
            return None
        record=db.scalar(select(AccountCreateKey).where(
            AccountCreateKey.user_id==user.id,
            AccountCreateKey.client_key==client_key))
        if not record:
            return None
        if not hmac.compare_digest(record.request_hash,request_hash):
            raise HTTPException(409,'La clave de reintento ya fue utilizada con otros datos. Actualiza la pantalla antes de continuar.')
        account=db.get(Account,record.account_id)
        if not account:
            raise HTTPException(409,'La cuenta asociada al reintento ya no está disponible.')
        return account

    def duplicate_response(account):
        table_id=db.scalar(select(TableOccupancy.table_id).where(
            TableOccupancy.account_id==account.id))
        return {'id':account.id,'table_id':table_id,'duplicate':True}

    previous=existing_create()
    if previous:
        return duplicate_response(previous)

    period=active_period_for_work(db)  # Shared lock: concurrent waiters may open accounts together.
    if db.scalar(select(TipDistribution.id).where(TipDistribution.period_id==period.id)):
        raise HTTPException(409,'El reparto de propinas ya fue generado; el turno está en proceso de cierre. Abre un nuevo turno para continuar.')
    table=None
    if data.table_id is not None:
        table=db.execute(select(DiningTable).where(DiningTable.id==data.table_id).with_for_update()).scalar_one_or_none()
        if not table or not table.active:raise HTTPException(404,'La mesa no existe o está desactivada')
        if db.get(TableOccupancy,table.id):
            previous=existing_create()
            if previous:
                return duplicate_response(previous)
            raise HTTPException(409,'Esta mesa ya está ocupada. Actualiza la lista de mesas.')
    account_name=payload['name']
    if not account_name and table:account_name=table.name
    if not account_name:raise HTTPException(400,'Escribe un nombre para la cuenta libre o selecciona una mesa')
    a=Account(name=account_name,owner_id=user.id)
    db.add(a);db.flush()
    audit(db,user,'account.create',id=a.id,name=a.name,table_id=table.id if table else None)
    if table:
        db.add(TableOccupancy(table_id=table.id,account_id=a.id,assigned_by=user.id))
        audit(db,user,'table.assign',table_id=table.id,table_name=table.name,account_id=a.id,waiter=user.display)
    if client_key:
        db.add(AccountCreateKey(user_id=user.id,client_key=client_key,
            account_id=a.id,request_hash=request_hash))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous=existing_create()
        if previous:
            return duplicate_response(previous)
        raise HTTPException(409,'La mesa acaba de ser ocupada por otro mesero. Actualiza las mesas.')
    await notify()
    return {'id':a.id,'table_id':table.id if table else None,'duplicate':False}

class VoidAccountIn(BaseModel):
    reason:str=Field(min_length=3,max_length=400)

@app.post('/api/accounts/{account_id}/void')
async def void_account(account_id:int,data:VoidAccountIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner')
    active_period(db)
    a=owner_account(db,user,account_id,('open','pending'))
    total,paid=acc_totals(db,a)
    if total!=0 or paid!=0:
        raise HTTPException(409,'Solo puedes anular cuentas sin consumo pendiente y sin pagos. Cancela los productos con autorización antes.')
    a.status='cancelled';a.closed_at=now()
    release_table_for_account(db,a,user,'Cuenta anulada')
    audit(db,user,'account.void',id=a.id,reason=data.reason)
    db.commit();await notify()
    return {'ok':True,'id':a.id,'status':'cancelled'}

@app.get('/api/accounts/{account_id}')
def account_detail(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier','waiter')
    a=owner_account(db,user,account_id,allowed=None)
    batches=db.scalars(select(Batch).where(
        Batch.account_id==a.id).order_by(Batch.id)).all()
    batch_ids=[row.id for row in batches]
    items=db.scalars(select(OrderItem).where(
        OrderItem.batch_id.in_(batch_ids)).order_by(
        OrderItem.batch_id,OrderItem.id)).all() if batch_ids else []
    item_ids=[row.id for row in items]
    meta_rows=db.scalars(select(OrderItemMeta).where(
        OrderItemMeta.order_item_id.in_(item_ids))).all() if item_ids else []
    meta_by_item={row.order_item_id:row for row in meta_rows}
    item_by_id={row.id:row for row in items}

    sender_ids={row.sent_by for row in batches if row.sent_by is not None}
    sender_map={row.id:row.display for row in db.scalars(
        select(User).where(User.id.in_(sender_ids))).all()} if sender_ids else {}

    payments=db.scalars(select(Payment).where(
        Payment.account_id==a.id).order_by(Payment.id)).all()
    tips=db.scalars(select(PaymentTip).where(
        PaymentTip.account_id==a.id).order_by(PaymentTip.id)).all()
    tip_by_payment={row.payment_id:row.cents for row in tips}
    discounts=db.scalars(select(AccountDiscount).where(
        AccountDiscount.account_id==a.id).order_by(AccountDiscount.id)).all()

    result=account_dict_many(db,[a])[0]
    result['items']=[]
    result['batches']=[
        {'id':batch.id,'sent_by':sender_map.get(batch.sent_by,'?'),
         'created_at':batch.created_at.isoformat() if batch.created_at else ''}
        for batch in batches
    ]
    for item in items:
        meta=meta_by_item.get(item.id)
        try:
            modifiers=json.loads(meta.modifiers_json or '[]') if meta else []
        except (TypeError,ValueError):
            modifiers=[]
        result['items'].append({
            'id':item.id,'batch_id':item.batch_id,'name':item.name,
            'station':item.station,'price':amount(item.price_cents),
            'qty':item.qty,'cancelled_qty':item.cancelled_qty,'note':item.note,
            'variant_name':meta.variant_name if meta else '',
            'modifiers':modifiers,'prep_minutes':meta.prep_minutes if meta else 15,
            'release_after_minutes':meta.release_after_minutes if meta else 0,
            'promotion_name':meta.promotion_name if meta else ''
        })
    result['payments']=[
        {'id':payment.id,'method':payment.method,'amount':amount(payment.cents),
         'received':amount(payment.received_cents),
         'tip':amount(tip_by_payment.get(payment.id,0)),
         'created_at':payment.created_at.isoformat() if payment.created_at else ''}
        for payment in payments
    ]
    result['discounts']=[
        {'id':discount.id,'scope':discount.scope,'item_id':discount.item_id,
         'item_name':(item_by_id[discount.item_id].name
                      if discount.item_id in item_by_id else ''),
         'mode':discount.mode,'value':discount.value_text,
         'base':amount(discount.base_cents),'amount':amount(discount.discount_cents),
         'reason':discount.reason,'status':discount.status,
         'created_at':discount.created_at.isoformat() if discount.created_at else ''}
        for discount in discounts
    ]
    result['tips']=[
        {'id':tip.id,'payment_id':tip.payment_id,'method':tip.method,
         'amount':amount(tip.cents),
         'created_at':tip.created_at.isoformat() if tip.created_at else ''}
        for tip in tips
    ]
    return result

@app.get('/api/cashier/overview')
def cashier_overview(account_id:Optional[int]=None,db:Session=Depends(db_session),
                     user:User=Depends(auth_either)):
    """Una sola respuesta para lista de cobros y detalle inicial seleccionado."""
    allow(user,'owner','admin','cashier')
    pending=[row for row in accounts(db,user) if row['status']=='pending']
    pending_ids={row['id'] for row in pending}
    selected_id=account_id if account_id in pending_ids else (pending[0]['id'] if pending else None)
    selected=account_detail(selected_id,db,user) if selected_id is not None else None
    return {'accounts':pending,'selected':selected}

class ReassignIn(BaseModel): owner_id:int;reason:str=Field(min_length=3,max_length=400)
@app.post('/api/accounts/{account_id}/reassign')
async def reassign(account_id:int,data:ReassignIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner');a=owner_account(db,user,account_id)
    dest=db.get(User,data.owner_id)
    if not dest or not dest.active or dest.role!='waiter':raise HTTPException(400,'El destino debe ser un mesero activo')
    old=a.owner_id;a.owner_id=dest.id
    # Todos los avisos aún no entregados siguen la cuenta. Incluso si el mesero
    # anterior ya confirmó recepción, el nuevo responsable debe recibirlos.
    pending_ids=[]
    if old!=dest.id:
        pending=db.scalars(select(ReadyNotificationEvent).where(
            ReadyNotificationEvent.account_id==a.id,
            ReadyNotificationEvent.delivered_at.is_(None))).all()
        for notice in pending:
            notice.waiter_id=dest.id
            notice.acknowledged_at=None
            notice.attempted_at=None
            notice.attempt_count=0
            notice.provider_accepted_at=None
            notice.next_attempt_at=None
            notice.last_error=None
            pending_ids.append(notice.id)
    audit(db,user,'account.reassign',id=a.id,old=old,new=dest.id,
          reason=data.reason,notifications_reassigned=len(pending_ids))
    db.commit();await notify()
    for event_id in pending_ids:
        await dispatch_ready_notification(event_id)
    return {'ok':True,'notifications_reassigned':len(pending_ids)}


# --- Gestión avanzada de cuentas (Fase 2B) ---------------------------------
class AccountRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(default='Actualización de nombre', max_length=400)

class AccountTableTransferIn(BaseModel):
    table_id: Optional[int] = None
    reason: str = Field(min_length=3, max_length=400)

class AccountMergeIn(BaseModel):
    target_account_id: int
    reason: str = Field(min_length=3, max_length=400)

class AccountSplitIn(BaseModel):
    item_id: int
    qty: int = Field(ge=1, le=100)
    target_account_id: Optional[int] = None
    name: Optional[str] = Field(default=None, max_length=120)
    table_id: Optional[int] = None
    reason: str = Field(min_length=3, max_length=400)

class AccountReopenClosedIn(BaseModel):
    reason: str = Field(min_length=3, max_length=400)
    table_id: Optional[int] = None

def account_manager_access(user, account):
    # La edición avanzada pertenece exclusivamente al propietario, incluso por API.
    if user.role == 'owner':
        return
    raise HTTPException(403, 'Solo el propietario puede modificar, unir o dividir cuentas')

def account_table_occupancy(db, account_id):
    return db.execute(
        select(TableOccupancy).where(TableOccupancy.account_id == account_id).with_for_update()
    ).scalar_one_or_none()

@app.get('/api/accounts/{account_id}/history')
def account_history(account_id: int, db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner', 'admin', 'waiter')
    account = owner_account(db, user, account_id, allowed=None)
    rows = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(500)).all()
    result = []
    for row in rows:
        try:
            detail = json.loads(row.detail or '{}')
        except (TypeError, ValueError):
            continue
        belongs = detail.get('account_id') == account_id
        if not belongs and row.action.startswith('account.'):
            belongs = detail.get('id') == account_id
        if not belongs:
            continue
        actor = db.get(User, row.actor_id) if row.actor_id else None
        result.append({
            'id': row.id,
            'date': row.created_at.isoformat() if row.created_at else '',
            'actor': actor.display if actor else 'Sistema',
            'action': row.action,
            'detail': detail
        })
        if len(result) >= 50:
            break
    return result

@app.post('/api/accounts/{account_id}/rename')
async def rename_account(account_id: int, data: AccountRenameIn,
                         db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    account = owner_account(db, user, account_id, ('open', 'pending'))
    account_manager_access(user, account)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, 'El nombre de la cuenta es obligatorio')
    before = account.name
    if before == name:
        return {'ok': True, 'unchanged': True}
    account.name = name
    audit(db, user, 'account.rename', account_id=account.id,
          before=before, after=name, reason=data.reason)
    db.commit()
    await notify()
    return {'ok': True, 'name': account.name}

@app.post('/api/accounts/{account_id}/transfer-table')
async def transfer_account_table(account_id: int, data: AccountTableTransferIn,
                                 db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    account = owner_account(db, user, account_id, ('open', 'pending'))
    account_manager_access(user, account)
    current = account_table_occupancy(db, account.id)
    current_table_id = current.table_id if current else None
    if current_table_id == data.table_id:
        return {'ok': True, 'unchanged': True}
    destination = None
    if data.table_id is not None:
        destination = db.execute(
            select(DiningTable).where(DiningTable.id == data.table_id).with_for_update()
        ).scalar_one_or_none()
        if not destination or not destination.active:
            raise HTTPException(404, 'La mesa de destino no existe o está desactivada')
        occupied = db.get(TableOccupancy, destination.id)
        if occupied and occupied.account_id != account.id:
            raise HTTPException(409, 'La mesa de destino ya está ocupada')
    if current:
        db.delete(current)
        db.flush()
    if destination:
        db.add(TableOccupancy(table_id=destination.id, account_id=account.id,
                              assigned_by=user.id, assigned_at=now()))
    audit(db, user, 'account.table_transfer', account_id=account.id,
          from_table_id=current_table_id, to_table_id=destination.id if destination else None,
          to_table=destination.name if destination else None, reason=data.reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'La mesa cambió de disponibilidad. Actualiza e inténtalo de nuevo.')
    await notify()
    return {'ok': True, 'table_id': destination.id if destination else None}

@app.post('/api/accounts/{account_id}/merge')
async def merge_accounts(account_id: int, data: AccountMergeIn,
                         db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    if data.target_account_id == account_id:
        raise HTTPException(400, 'Selecciona otra cuenta como destino')
    locked = db.scalars(
        select(Account).where(Account.id.in_([account_id, data.target_account_id]))
        .order_by(Account.id).with_for_update()
    ).all()
    accounts_by_id = {a.id: a for a in locked}
    source = accounts_by_id.get(account_id)
    target = accounts_by_id.get(data.target_account_id)
    if not source or not target:
        raise HTTPException(404, 'No se encontró una de las cuentas')
    account_manager_access(user, source)
    account_manager_access(user, target)
    if source.status not in ('open', 'pending') or target.status not in ('open', 'pending'):
        raise HTTPException(409, 'Solo se pueden unir cuentas abiertas o pendientes de cobro')
    source_has_discount=bool(db.scalar(select(AccountDiscount.id).where(AccountDiscount.account_id==source.id,AccountDiscount.status=='applied').limit(1)))
    target_has_discount=bool(db.scalar(select(AccountDiscount.id).where(AccountDiscount.account_id==target.id,AccountDiscount.status=='applied').limit(1)))
    any_payments=bool(db.scalar(select(Payment.id).where(Payment.account_id.in_([source.id,target.id])).limit(1)))
    if (source_has_discount or target_has_discount) and any_payments:
        raise HTTPException(409,'No se pueden unir cuentas con descuentos aplicados y pagos registrados. Primero solicita revisión de administración para evitar alterar saldos ya conciliados.')
    source_total, source_paid = acc_totals(db, source)
    target_total, target_paid = acc_totals(db, target)
    source_occupancy = account_table_occupancy(db, source.id)
    target_occupancy = account_table_occupancy(db, target.id)

    # Mover los registros contables y de producción sin recrearlos.
    db.query(Batch).filter(Batch.account_id == source.id).update(
        {Batch.account_id: target.id}, synchronize_session=False)
    db.query(Payment).filter(Payment.account_id == source.id).update(
        {Payment.account_id: target.id}, synchronize_session=False)
    db.query(PaymentTip).filter(PaymentTip.account_id == source.id).update(
        {PaymentTip.account_id: target.id}, synchronize_session=False)
    db.query(AccountDiscount).filter(AccountDiscount.account_id == source.id).update(
        {AccountDiscount.account_id: target.id}, synchronize_session=False)
    db.query(Allocation).filter(Allocation.account_id == source.id).update(
        {Allocation.account_id: target.id}, synchronize_session=False)
    db.query(Approval).filter(Approval.account_id == source.id).update(
        {Approval.account_id: target.id}, synchronize_session=False)

    # Si la cuenta destino no tiene mesa, conserva la mesa de origen.
    # Si ambas tenían mesa, la de origen se libera y la de destino permanece.
    if source_occupancy:
        if not target_occupancy:
            source_occupancy.account_id = target.id
            source_occupancy.assigned_by = user.id
            source_occupancy.assigned_at = now()
            audit(db, user, 'account.table_transfer', account_id=target.id,
                  from_table_id=None, to_table_id=source_occupancy.table_id,
                  reason='Mesa conservada al unir cuentas')
        else:
            released_table_id = source_occupancy.table_id
            db.delete(source_occupancy)
            audit(db, user, 'account.table_release_on_merge', account_id=source.id,
                  table_id=released_table_id, target_account_id=target.id,
                  reason='La cuenta destino ya conserva su propia mesa')

    target.status = 'pending' if 'pending' in (source.status, target.status) else 'open'
    source.status = 'merged'
    source.closed_at = now()
    audit(db, user, 'account.merge', account_id=source.id, target_account_id=target.id,
          source_total=amount(source_total), source_paid=amount(source_paid),
          target_total_before=amount(target_total), target_paid_before=amount(target_paid),
          reason=data.reason)
    audit(db, user, 'account.merge_received', account_id=target.id,
          source_account_id=source.id, reason=data.reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'No se pudo unir: revisa la ocupación de las mesas e inténtalo otra vez')
    await notify()
    total, paid = acc_totals(db, target)
    return {'ok': True, 'source_account_id': source.id, 'target_account_id': target.id,
            'target_total': amount(total), 'target_paid': amount(paid),
            'target_due': amount(total - paid)}

@app.post('/api/accounts/{account_id}/split')
async def split_account(account_id: int, data: AccountSplitIn,
                        db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    source = owner_account(db, user, account_id, ('open', 'pending'))
    account_manager_access(user, source)
    if db.scalar(select(AccountDiscount.id).where(AccountDiscount.account_id==source.id,AccountDiscount.status=='applied').limit(1)):
        raise HTTPException(409,'No se puede dividir una cuenta con descuentos aplicados. Solicita a administración retirar el descuento antes de dividirla.')
    if data.target_account_id == account_id:
        raise HTTPException(400, 'La cuenta destino debe ser diferente')
    target = None
    destination_table = None
    if data.target_account_id is not None:
        target = db.execute(
            select(Account).where(Account.id == data.target_account_id).with_for_update()
        ).scalar_one_or_none()
        if not target:
            raise HTTPException(404, 'Cuenta destino no encontrada')
        account_manager_access(user, target)
        if target.status not in ('open', 'pending'):
            raise HTTPException(409, 'La cuenta destino debe estar abierta o pendiente')
        if data.table_id is not None:
            raise HTTPException(400, 'No selecciones mesa cuando agregas productos a una cuenta existente')
    else:
        active_period(db)
        if data.table_id is not None:
            destination_table = db.execute(
                select(DiningTable).where(DiningTable.id == data.table_id).with_for_update()
            ).scalar_one_or_none()
            if not destination_table or not destination_table.active:
                raise HTTPException(404, 'La mesa destino no existe o está desactivada')
            if db.get(TableOccupancy, destination_table.id):
                raise HTTPException(409, 'La mesa destino ya está ocupada')
        destination_name = (data.name or '').strip() or f'Dividida de #{source.id}'
        target = Account(name=destination_name, owner_id=source.owner_id, status='open')
        db.add(target)
        db.flush()
        if destination_table:
            db.add(TableOccupancy(table_id=destination_table.id, account_id=target.id,
                                  assigned_by=user.id, assigned_at=now()))
        audit(db, user, 'account.create_from_split', account_id=target.id,
              source_account_id=source.id, table_id=destination_table.id if destination_table else None)

    item = db.execute(
        select(OrderItem).where(OrderItem.id == data.item_id).with_for_update()
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(404, 'Producto de la cuenta no encontrado')
    batch = db.get(Batch, item.batch_id)
    if not batch or batch.account_id != source.id:
        raise HTTPException(400, 'El producto no pertenece a la cuenta de origen')
    available = item.qty - item.cancelled_qty
    if data.qty > available:
        raise HTTPException(409, f'Solo quedan {available} unidades disponibles')
    reserved = db.scalar(
        select(func.coalesce(func.sum(Allocation.qty), 0))
        .where(Allocation.order_item_id == item.id)
    ) or 0
    if data.qty > available - reserved:
        raise HTTPException(409, 'No puedes trasladar unidades que ya están asignadas a un pago')

    # Si hay pagos por importe, no existe trazabilidad suficiente para decidir
    # qué productos están pagados. En ese caso se bloquea la división.
    source_payments = db.scalars(select(Payment).where(Payment.account_id == source.id)).all()
    allocation_groups = set(db.scalars(
        select(Allocation.group_key).where(Allocation.account_id == source.id).distinct()
    ).all())
    unallocated_payment = False
    for payment in source_payments:
        base_key = payment.client_key.rsplit('-', 1)[0]
        if base_key not in allocation_groups:
            unallocated_payment = True
            break
    if source_payments and unallocated_payment:
        raise HTTPException(409, 'La cuenta tiene pagos registrados por importe. Primero debe conciliarse el pago por productos antes de dividir.')

    ticket = db.scalar(
        select(StationTicket).where(StationTicket.batch_id == batch.id,
                                    StationTicket.station == item.station)
    )
    already_split = bool(batch.client_key and batch.client_key.startswith('split-'))
    if (not ticket and not already_split) or (ticket and ticket.status != 'ready'):
        raise HTTPException(409, 'Solo puedes dividir productos que cocina o barra ya marcaron como listos. Así evitamos duplicar o alterar una preparación en curso.')

    original_qty = item.qty
    item.qty -= data.qty
    split_batch = Batch(account_id=target.id, sent_by=user.id,
                        client_key='split-' + secrets.token_urlsafe(24), created_at=now())
    db.add(split_batch)
    db.flush()
    split_item = OrderItem(batch_id=split_batch.id, product_id=item.product_id,
                           name=item.name, station=item.station, price_cents=item.price_cents,
                           qty=data.qty, cancelled_qty=0, note=item.note)
    db.add(split_item)
    db.flush()
    source_meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==item.id))
    if source_meta:
        db.add(OrderItemMeta(order_item_id=split_item.id,variant_name=source_meta.variant_name,
            modifiers_json=source_meta.modifiers_json,prep_minutes=source_meta.prep_minutes,
            release_after_minutes=source_meta.release_after_minutes,promotion_name=source_meta.promotion_name,
            base_price_cents=source_meta.base_price_cents,modifier_price_cents=source_meta.modifier_price_cents))
    # Conservar el historial de producción como dos tickets ya listos; no se crea
    # una comanda nueva ni se vuelve a enviar el producto a cocina/barra.
    ready_at = ticket.ready_at if ticket else now()
    acknowledged_at = ticket.acknowledged_at if ticket else ready_at
    created_at = ticket.created_at if ticket else split_batch.created_at
    db.add(StationTicket(batch_id=split_batch.id, station=item.station, status='ready',
                         created_at=created_at, acknowledged_at=acknowledged_at,
                         ready_at=ready_at))
    audit(db, user, 'account.split_out', account_id=source.id,
          target_account_id=target.id, item_id=item.id, product=item.name,
          qty=data.qty, original_qty=original_qty, remaining_qty=item.qty, reason=data.reason)
    audit(db, user, 'account.split_in', account_id=target.id,
          source_account_id=source.id, product=item.name, qty=data.qty, reason=data.reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'No se pudo dividir la cuenta. Actualiza e inténtalo de nuevo.')
    await notify()
    return {'ok': True, 'source_account_id': source.id, 'target_account_id': target.id,
            'new_account': data.target_account_id is None, 'split_item_id': split_item.id}

@app.post('/api/accounts/{account_id}/reopen-closed')
async def reopen_closed_account(account_id: int, data: AccountReopenClosedIn,
                                db: Session = Depends(db_session), user: User = Depends(auth_either)):
    allow(user, 'owner')
    account = db.execute(
        select(Account).where(Account.id == account_id).with_for_update()
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(404, 'Cuenta no encontrada')
    if account.status != 'paid':
        raise HTTPException(409, 'Solo se pueden reabrir cuentas que fueron cerradas como pagadas')
    active_cash_period=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if active_cash_period and db.scalar(select(TipDistribution.id).where(TipDistribution.period_id==active_cash_period.id)):
        raise HTTPException(409,'El reparto de propinas de este turno ya fue generado. No se pueden reabrir cuentas durante el cierre de caja.')
    destination_table = None
    if data.table_id is not None:
        destination_table = db.execute(
            select(DiningTable).where(DiningTable.id == data.table_id).with_for_update()
        ).scalar_one_or_none()
        if not destination_table or not destination_table.active:
            raise HTTPException(404, 'La mesa no existe o está desactivada')
        if db.get(TableOccupancy, destination_table.id):
            raise HTTPException(409, 'La mesa seleccionada ya está ocupada')
    old_closed_at = account.closed_at.isoformat() if account.closed_at else None
    account.status = 'open'
    account.closed_at = None
    if destination_table:
        current = account_table_occupancy(db, account.id)
        if current:
            db.delete(current)
            db.flush()
        db.add(TableOccupancy(table_id=destination_table.id, account_id=account.id,
                              assigned_by=user.id, assigned_at=now()))
    audit(db, user, 'account.reopen_closed', account_id=account.id,
          previous_closed_at=old_closed_at,
          table_id=destination_table.id if destination_table else None,
          reason=data.reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'No se pudo reabrir la cuenta; verifica la disponibilidad de la mesa')
    await notify()
    return {'ok': True, 'id': account.id, 'status': account.status}

# ---------------------------------------------------------------------------

class OrderLine(BaseModel):
    product_id:int
    qty:int=Field(ge=1,le=100)
    note:str=Field(default='',max_length=300)
    variant_id:Optional[int]=None
    modifier_option_ids:list[int]=Field(default_factory=list,max_length=30)
    prep_minutes:int=Field(default=15,ge=0,le=240)
    release_after_minutes:int=Field(default=0,ge=0,le=240)

class OrderIn(BaseModel):
    items:list[OrderLine]=Field(min_length=1,max_length=100)
    client_key:str=Field(min_length=8,max_length=100)

@app.post('/api/accounts/{account_id}/orders')
async def send_order(account_id:int,data:OrderIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','waiter')
    period=active_period_for_work(db)
    if db.scalar(select(TipDistribution.id).where(TipDistribution.period_id==period.id)):
        raise HTTPException(409,'El reparto de propinas ya fue generado; el turno está en proceso de cierre. Abre un nuevo turno para continuar.')
    a=owner_account(db,user,account_id,allowed=None)
    # Releer la clave después del bloqueo de cuenta: dos toques simultáneos no crean dos comandas.
    existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Identificador de envío ya utilizado')
        return {'batch_id':existing.id,'duplicate':True}
    if a.status!='open':raise HTTPException(409,'La cuenta ya no admite nuevas comandas')
    # Lectura compartida del ajuste: permite varios envíos y bloquea cambios de inventario hasta confirmar.
    setting=db.execute(select(Setting).where(Setting.id==1).with_for_update(read=True)).scalar_one()
    order_product_ids={line.product_id for line in data.items}
    selected_option_ids=sorted({option_id for line in data.items for option_id in line.modifier_option_ids})
    selected_option_rows=db.scalars(select(ProductModifierOption).where(
        ProductModifierOption.id.in_(selected_option_ids))).all() if selected_option_ids else []
    selected_option_by_id={option.id:option for option in selected_option_rows}
    if len(selected_option_by_id)!=len(selected_option_ids):
        raise HTTPException(400,'Hay opciones de producto inexistentes')
    mapped_stock_ids={option.inventory_product_id for option in selected_option_rows if option.inventory_product_id}
    all_product_ids=order_product_ids|mapped_stock_ids
    product_query=select(Product).where(Product.id.in_(all_product_ids)).order_by(Product.id)
    # Bloquea existencias compartidas para evitar que dos comandas descuenten la misma botella.
    product_query=product_query.with_for_update() if setting.inventory_on else product_query.with_for_update(read=True)
    products={p.id:p for p in db.scalars(product_query)}
    if len(products)!=len(all_product_ids):raise HTTPException(400,'Hay productos o insumos inexistentes')
    need={}
    for line in data.items:
        product=products[line.product_id]
        if product.inventory_only or not product.active or product.blocked:
            raise HTTPException(409,f'{product.name} no está disponible')
        if product.inventory_enabled:
            need[product.id]=need.get(product.id,0)+line.qty
        for option_id in line.modifier_option_ids:
            option=selected_option_by_id.get(option_id)
            if option and option.inventory_product_id:
                stock_id=option.inventory_product_id
                stock_item=products.get(stock_id)
                if not stock_item or not stock_item.inventory_only or not stock_item.inventory_enabled:
                    raise HTTPException(409,'El insumo vinculado ya no está disponible para inventario')
                need[stock_id]=need.get(stock_id,0)+line.qty*option.inventory_quantity
    if setting.inventory_on:
        for stock_id,qty in need.items():
            stock_item=products[stock_id]
            if stock_item.stock<qty:
                unit=stock_item.inventory_unit or 'unidades'
                raise HTTPException(409,f'Sin existencias suficientes de {stock_item.name}: quedan {stock_item.stock} {unit}')
    prepared=[];stations=set()
    for line in data.items:
        p=products[line.product_id];stations.add(p.station)
        variant=None
        if line.variant_id is not None:
            variant=db.get(ProductVariant,line.variant_id)
            if not variant or variant.product_id!=p.id or not variant.active:raise HTTPException(400,f'Presentación inválida para {p.name}')
        base_cents=variant.price_cents if variant else p.price_cents
        groups=db.scalars(select(ProductModifierGroup).where(ProductModifierGroup.product_id==p.id,ProductModifierGroup.active.is_(True))).all()
        option_ids=line.modifier_option_ids
        if len(option_ids)!=len(set(option_ids)):raise HTTPException(400,'No repitas modificadores')
        options=[]
        if option_ids:
            options=db.scalars(select(ProductModifierOption).where(ProductModifierOption.id.in_(option_ids),ProductModifierOption.active.is_(True))).all()
            if len(options)!=len(option_ids):raise HTTPException(400,f'Hay modificadores inexistentes o inactivos en {p.name}')
        group_by_id={g.id:g for g in groups}
        counts={g.id:0 for g in groups}
        for option in options:
            group=db.get(ProductModifierGroup,option.group_id)
            if not group or group.product_id!=p.id or not group.active:raise HTTPException(400,f'Modificador no válido para {p.name}')
            counts[group.id]=counts.get(group.id,0)+1
        for group in groups:
            count=counts.get(group.id,0)
            if count<group.min_select or count>group.max_select:
                raise HTTPException(400,f'{p.name}: selecciona entre {group.min_select} y {group.max_select} opciones en "{group.name}"')
        promo=db.scalars(select(ProductPromotion).where(ProductPromotion.product_id==p.id,ProductPromotion.active.is_(True)).order_by(ProductPromotion.id.desc())).all()
        promo=next((x for x in promo if promotion_is_active(x)),None)
        promo_base=apply_promotion(base_cents,promo)
        modifier_cents=sum(o.price_delta_cents for o in options)
        final_price=promo_base+modifier_cents
        modifier_snapshot=[]
        for option in options:
            group=group_by_id.get(option.group_id) or db.get(ProductModifierGroup,option.group_id)
            stock_item=products.get(option.inventory_product_id) if option.inventory_product_id else None
            modifier_snapshot.append({'group':group.name if group else 'Extra','name':option.name,
                'price_delta':amount(option.price_delta_cents),'modifier_option_id':option.id,
                'inventory_product_id':option.inventory_product_id,
                'inventory_product_name':stock_item.name if stock_item else '',
                'inventory_quantity':option.inventory_quantity})
        prepared.append({'line':line,'product':p,'variant':variant,'options':modifier_snapshot,
                         'selected_options':options,'base_cents':promo_base,'modifier_cents':modifier_cents,
                         'final_cents':final_price,'promo_name':promo.name if promo else ''})
    batch=Batch(account_id=a.id,sent_by=user.id,client_key=data.client_key);db.add(batch)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
        if existing and existing.account_id==account_id:
            return {'batch_id':existing.id,'duplicate':True}
        if existing:
            raise HTTPException(409,'Identificador de envío ya utilizado')
        raise
    mitote_extensions.snapshot_batch_policy(db,batch.id)
    occupancy=db.scalar(select(TableOccupancy).where(TableOccupancy.account_id==a.id))
    assigned_table=db.get(DiningTable,occupancy.table_id) if occupancy else None
    table_name=assigned_table.name if assigned_table else ''
    created_order_items=[]
    for row in prepared:
        line=row['line'];p=row['product'];variant=row['variant']
        item=OrderItem(batch_id=batch.id,product_id=p.id,name=p.name,station=p.station,price_cents=row['final_cents'],qty=line.qty,note=line.note.strip())
        db.add(item);db.flush()
        created_order_items.append((item,p,line.qty,row['selected_options']))
        db.add(OrderItemMeta(order_item_id=item.id,variant_name=variant.name if variant else '',
            modifiers_json=json.dumps(row['options'],ensure_ascii=False),prep_minutes=mitote_extensions.product_prep_minutes(db,p.id,line.prep_minutes),
            release_after_minutes=line.release_after_minutes,promotion_name=row['promo_name'],
            base_price_cents=row['base_cents'],modifier_price_cents=row['modifier_cents']))
    # Una salida por línea de comanda: conserva producto, folio, mesa, usuario y hora.
    # La clave única batch/item evita duplicar movimientos si el envío se reintenta.
    for item,p,qty,selected_options in created_order_items:
        if setting.inventory_on and p.inventory_enabled:
            before=p.stock;p.stock-=qty;p.version+=1
            db.add(InventoryMovement(product_id=p.id,actor_id=user.id,account_id=a.id,
                batch_id=batch.id,order_item_id=item.id,kind='sale',quantity_change=-qty,
                before_stock=before,after_stock=p.stock,
                reason=f'Salida por comanda enviada · cuenta #{a.id}',
                account_name=a.name,table_name=table_name,
                source_key=f'inventory-sale:{batch.id}:{item.id}',created_at=now()))
        if setting.inventory_on:
            for option in selected_options:
                if not option.inventory_product_id:continue
                stock_item=products[option.inventory_product_id]
                delta=qty*option.inventory_quantity
                before=stock_item.stock;stock_item.stock-=delta;stock_item.version+=1
                db.add(InventoryMovement(product_id=stock_item.id,actor_id=user.id,account_id=a.id,
                    batch_id=batch.id,order_item_id=item.id,kind='sale',quantity_change=-delta,
                    before_stock=before,after_stock=stock_item.stock,
                    reason=f'Salida por cerveza seleccionada · {p.name} · {option.name} · cuenta #{a.id}',
                    account_name=a.name,table_name=table_name,
                    source_key=f'inventory-sale:{batch.id}:{item.id}:modifier:{option.id}',created_at=now()))
    for station in stations:db.add(StationTicket(batch_id=batch.id,station=station))
    audit(db,user,'order.send',account_id=a.id,batch_id=batch.id,stations=sorted(stations),items=len(prepared))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Última defensa para una clave repetida enviada desde cuentas distintas o en carrera.
        existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
        if existing and existing.account_id==account_id:
            return {'batch_id':existing.id,'duplicate':True}
        if existing:
            raise HTTPException(409,'Identificador de envío ya utilizado')
        raise
    await notify()
    async def dispatch_station(station):
        station_title='Cocina' if station=='kitchen' else 'Barra'
        payload={'title':'El Mitote · '+station_title,
                 'body':f'Nueva comanda #{batch.id} · {a.name}',
                 'url':f'/?push=station&account={a.id}',
                 'tag':f'mitote-order-{batch.id}-{station}'}
        fcm_payload={'title':'El Mitote · '+station_title,
                     'body':f'Nueva comanda #{batch.id}','url':'/?push=station'}
        await asyncio.gather(push_to(payload,role=station),
                             fcm_to(fcm_payload,role=station),
                             return_exceptions=True)
    await asyncio.gather(*(dispatch_station(station) for station in sorted(stations)))
    return {'batch_id':batch.id,'duplicate':False}

class RepeatOrderIn(BaseModel):
    qty:int=Field(default=1,ge=1,le=100)
    client_key:str=Field(min_length=8,max_length=100)

@app.post('/api/accounts/{account_id}/items/{item_id}/repeat')
async def repeat_order_item(account_id:int,item_id:int,data:RepeatOrderIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','waiter');active_period_for_work(db)
    existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Identificador de envío ya utilizado')
        return {'batch_id':existing.id,'duplicate':True}
    account=owner_account(db,user,account_id,('open',))
    item=db.execute(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(OrderItem.id==item_id,Batch.account_id==account_id).with_for_update()).scalar_one_or_none()
    if not item:raise HTTPException(404,'Producto de la cuenta no encontrado')
    old_meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==item.id))
    try:
        modifier_snapshot=json.loads(old_meta.modifiers_json or '[]') if old_meta else []
    except Exception:
        modifier_snapshot=[]
    mapped=[m for m in modifier_snapshot if m.get('inventory_product_id')]
    stock_need={}
    for modifier in mapped:
        stock_id=int(modifier['inventory_product_id'])
        stock_need[stock_id]=stock_need.get(stock_id,0)+data.qty*int(modifier.get('inventory_quantity') or 1)
    setting=db.get(Setting,1)
    product_ids={item.product_id}|set(stock_need)
    locked=db.scalars(select(Product).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()).all()
    products={p.id:p for p in locked}
    product=products.get(item.product_id)
    if not product or product.inventory_only or not product.active or product.blocked:
        raise HTTPException(409,'El producto ya no está disponible')
    for stock_id in stock_need:
        stock_item=products.get(stock_id)
        if not stock_item or not stock_item.inventory_only or not stock_item.inventory_enabled:
            raise HTTPException(409,'El insumo vinculado ya no está disponible para inventario')
    if setting.inventory_on:
        if product.inventory_enabled and product.stock<data.qty:
            raise HTTPException(409,f'Sin existencias suficientes de {product.name}')
        for stock_id,required in stock_need.items():
            stock_item=products[stock_id]
            if stock_item.stock<required:
                raise HTTPException(409,f'Sin existencias suficientes de {stock_item.name}: quedan {stock_item.stock} {stock_item.inventory_unit}')
    batch=Batch(account_id=account.id,sent_by=user.id,client_key=data.client_key);db.add(batch);db.flush()
    mitote_extensions.snapshot_batch_policy(db,batch.id)
    new_item=OrderItem(batch_id=batch.id,product_id=item.product_id,name=item.name,station=item.station,price_cents=item.price_cents,qty=data.qty,note=item.note)
    db.add(new_item);db.flush()
    if old_meta:
        db.add(OrderItemMeta(order_item_id=new_item.id,variant_name=old_meta.variant_name,modifiers_json=old_meta.modifiers_json,
            prep_minutes=old_meta.prep_minutes,release_after_minutes=old_meta.release_after_minutes,promotion_name=old_meta.promotion_name,
            base_price_cents=old_meta.base_price_cents,modifier_price_cents=old_meta.modifier_price_cents))
    else:
        db.add(OrderItemMeta(order_item_id=new_item.id,variant_name='',modifiers_json='[]',prep_minutes=15,release_after_minutes=0,base_price_cents=item.price_cents,modifier_price_cents=0))
    occupancy=db.scalar(select(TableOccupancy).where(TableOccupancy.account_id==account.id))
    assigned_table=db.get(DiningTable,occupancy.table_id) if occupancy else None
    table_name=assigned_table.name if assigned_table else ''
    if setting.inventory_on and product.inventory_enabled:
        before=product.stock;product.stock-=data.qty;product.version+=1
        db.add(InventoryMovement(product_id=product.id,actor_id=user.id,account_id=account.id,
            batch_id=batch.id,order_item_id=new_item.id,kind='sale',quantity_change=-data.qty,
            before_stock=before,after_stock=product.stock,
            reason=f'Salida por comanda repetida · cuenta #{account.id}',
            account_name=account.name,table_name=table_name,
            source_key=f'inventory-sale:{batch.id}:{new_item.id}',created_at=now()))
    if setting.inventory_on:
        for modifier in mapped:
            stock_item=products[int(modifier['inventory_product_id'])]
            delta=data.qty*int(modifier.get('inventory_quantity') or 1)
            before=stock_item.stock;stock_item.stock-=delta;stock_item.version+=1
            db.add(InventoryMovement(product_id=stock_item.id,actor_id=user.id,account_id=account.id,
                batch_id=batch.id,order_item_id=new_item.id,kind='sale',quantity_change=-delta,
                before_stock=before,after_stock=stock_item.stock,
                reason=f'Salida por cerveza seleccionada en comanda repetida · {item.name} · {modifier.get("name","")}',
                account_name=account.name,table_name=table_name,
                source_key=f'inventory-sale:{batch.id}:{new_item.id}:repeat:{stock_item.id}',created_at=now()))
    db.add(StationTicket(batch_id=batch.id,station=item.station))
    audit(db,user,'order.repeat',account_id=account.id,source_item_id=item.id,new_item_id=new_item.id,qty=data.qty,batch_id=batch.id)
    db.commit();await notify()
    await push_to({'title':'El Mitote · '+('Cocina' if item.station=='kitchen' else 'Barra'),'body':f'Producto repetido · {account.name}','url':'/?push=station','tag':f'mitote-order-{batch.id}-{item.station}'},role=item.station)
    await fcm_to({'title':'El Mitote · '+('Cocina' if item.station=='kitchen' else 'Barra'),'body':f'Producto repetido · Comanda #{batch.id}','url':'/?push=station'},role=item.station)
    return {'batch_id':batch.id,'duplicate':False}

@app.post('/api/accounts/{account_id}/request-bill')
async def request_bill(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','waiter');a=owner_account(db,user,account_id,('open',))
    total,paid=acc_totals(db,a)
    if total<=0:raise HTTPException(409,'La cuenta no tiene productos')
    a.status='pending';audit(db,user,'account.request_bill',id=a.id);db.commit();await notify();return {'ok':True}
class ApprovalIn(BaseModel):kind:Literal['reopen','cancel'];reason:str=Field(min_length=3,max_length=400);item_id:Optional[int]=None;qty:Optional[int]=None
@app.post('/api/accounts/{account_id}/requests')
async def request_approval(account_id:int,data:ApprovalIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','waiter');a=owner_account(db,user,account_id)
    if data.kind=='reopen':
        if a.status!='pending' or acc_totals(db,a)[1]>0:raise HTTPException(409,'Solo cuentas pendientes sin abonos pueden reabrirse')
    else:
        item=db.get(OrderItem,data.item_id) if data.item_id else None
        if not item or db.get(Batch,item.batch_id).account_id!=a.id or not data.qty or data.qty<1 or data.qty>item.qty-item.cancelled_qty:raise HTTPException(400,'Producto o cantidad inválidos')
    r=Approval(account_id=a.id,requester_id=user.id,kind=data.kind,item_id=data.item_id,qty=data.qty,reason=data.reason)
    db.add(r);audit(db,user,'approval.request',kind=data.kind,account_id=a.id);db.commit();await notify();return {'id':r.id}
@app.get('/api/requests')
def requests(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    return [{'id':r.id,'account_id':r.account_id,'account':db.get(Account,r.account_id).name,'kind':r.kind,'item_id':r.item_id,'qty':r.qty,'reason':r.reason,'status':r.status,'requester':db.get(User,r.requester_id).display} for r in db.scalars(select(Approval).order_by(Approval.id.desc()).limit(100))]
class DecisionIn(BaseModel):approve:bool;return_stock:bool=False
@app.post('/api/requests/{request_id}/decide')
async def decide(request_id:int,data:DecisionIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    r=db.execute(select(Approval).where(Approval.id==request_id).with_for_update()).scalar_one_or_none()
    if not r:raise HTTPException(404,'Solicitud inexistente')
    if r.status!='pending':raise HTTPException(409,'Solicitud ya resuelta')
    a=owner_account(db,user,r.account_id)
    if data.approve:
        if r.kind=='reopen':
            if a.status!='pending' or acc_totals(db,a)[1]>0:raise HTTPException(409,'La cuenta cambió; no se puede reabrir')
            a.status='open'
        elif r.kind=='cancel':
            item=db.execute(select(OrderItem).where(OrderItem.id==r.item_id).with_for_update()).scalar_one()
            if item.qty-item.cancelled_qty<r.qty:raise HTTPException(409,'La cantidad ya cambió')
            if acc_totals(db,a)[1]>0:raise HTTPException(409,'Cuenta con pagos: requiere ajuste administrativo especial')
            item.cancelled_qty+=r.qty
            if data.return_stock:
                station_ticket=db.scalar(select(StationTicket).where(StationTicket.batch_id==item.batch_id,StationTicket.station==item.station))
                if station_ticket and station_ticket.status in ('preparing','ready'):
                    raise HTTPException(409,'No se puede devolver stock: la preparación ya comenzó')
                # Solo devuelve los insumos/productos que sí se descontaron al enviar la comanda.
                # No crea existencias en pedidos anteriores ni si el inventario se apagó después.
                sales=db.scalars(select(InventoryMovement).where(
                    InventoryMovement.order_item_id==item.id,InventoryMovement.kind=='sale'
                ).with_for_update()).all()
                occupancy=db.scalar(select(TableOccupancy).where(TableOccupancy.account_id==a.id))
                assigned_table=db.get(DiningTable,occupancy.table_id) if occupancy else None
                for sale in sales:
                    per_unit=abs(int(sale.quantity_change))//max(1,int(item.qty))
                    delta=per_unit*r.qty
                    if delta<1:continue
                    stock_item=db.execute(select(Product).where(Product.id==sale.product_id).with_for_update()).scalar_one_or_none()
                    if not stock_item:continue
                    before=stock_item.stock;stock_item.stock+=delta;stock_item.version+=1
                    db.add(InventoryMovement(product_id=stock_item.id,actor_id=user.id,account_id=a.id,
                        batch_id=item.batch_id,order_item_id=item.id,kind='return',quantity_change=delta,
                        before_stock=before,after_stock=stock_item.stock,
                        reason=f'Devolución autorizada por cancelación · cuenta #{a.id}',
                        account_name=a.name,table_name=assigned_table.name if assigned_table else '',
                        source_key=f'inventory-return:{r.id}:{sale.id}',created_at=now()))
    r.status='approved' if data.approve else 'rejected';r.decided_by=user.id
    audit(db,user,'approval.decide',id=r.id,approved=data.approve,return_stock=data.return_stock);db.commit();await notify();return {'ok':True}

@app.get('/api/tickets')
def tickets(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','kitchen','bar')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return []
    q=select(StationTicket).join(Batch,Batch.id==StationTicket.batch_id).where(
        Batch.created_at>=p.opened_at).order_by(StationTicket.id.desc()).limit(150)
    if user.role in ('kitchen','bar'):q=q.where(StationTicket.station==user.role)
    out=[]
    for t in db.scalars(q):
        b=db.get(Batch,t.batch_id);a=db.get(Account,b.account_id)
        lines=db.scalars(select(OrderItem).where(OrderItem.batch_id==b.id,OrderItem.station==t.station)).all()
        ticket_items=[]
        for x in lines:
            if x.qty<=x.cancelled_qty:continue
            meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==x.id))
            try:modifiers=json.loads(meta.modifiers_json or '[]') if meta else []
            except Exception:modifiers=[]
            ticket_items.append({'name':x.name,'qty':x.qty-x.cancelled_qty,'note':x.note,
                'variant_name':meta.variant_name if meta else '', 'modifiers':modifiers,
                'prep_minutes':meta.prep_minutes if meta else 15,'release_after_minutes':meta.release_after_minutes if meta else 0,
                'promotion_name':meta.promotion_name if meta else ''})
        out.append({'id':t.id,'batch_id':b.id,'account':a.name,'account_id':a.id,'waiter':db.get(User,b.sent_by).display,'station':t.station,'status':t.status,'created_at':t.created_at.isoformat() if t.created_at else '', 'items':ticket_items})
    return out
@app.get('/api/waiter/ready-alerts')
def waiter_ready_alerts(db:Session=Depends(db_session),user:User=Depends(auth)):
    """Avisos persistentes sin confirmar, solo para el mesero actual de la cuenta."""
    allow(user,'waiter')
    period=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not period:return []
    rows=db.execute(
        select(ReadyNotificationEvent,Account).join(Account,ReadyNotificationEvent.account_id==Account.id)
        .join(Batch,Batch.id==ReadyNotificationEvent.batch_id)
        .where(ReadyNotificationEvent.waiter_id==user.id,Account.owner_id==user.id,
               ReadyNotificationEvent.delivered_at.is_(None),
               Batch.created_at>=period.opened_at,Account.status.in_(('open','pending')))
        .order_by(ReadyNotificationEvent.created_at.desc(),ReadyNotificationEvent.id.desc()).limit(80)
    ).all()
    return [{
        'id':e.event_key,'event_id':e.id,'ticket_id':e.ticket_id,
        'batch_id':e.batch_id,'account_id':e.account_id,'account':a.name,
        'station':e.station,'station_name':'Cocina' if e.station=='kitchen' else 'Barra',
        'complete':e.complete,'ready_at':e.created_at.isoformat(),
        'acknowledged':e.acknowledged_at is not None,
        'received_at':e.acknowledged_at.isoformat() if e.acknowledged_at else None,
        'delivered':e.delivered_at is not None,
        'delivered_at':e.delivered_at.isoformat() if e.delivered_at else None
    } for e,a in rows]


@app.post('/api/waiter/ready-alerts/{event_id}/ack')
async def acknowledge_ready_alert(event_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'waiter')
    row=db.execute(select(ReadyNotificationEvent).where(
        ReadyNotificationEvent.id==event_id).with_for_update()).scalar_one_or_none()
    if not row:raise HTTPException(404,'Aviso no encontrado')
    account=db.get(Account,row.account_id)
    if row.waiter_id!=user.id or not account or account.owner_id!=user.id:
        raise HTTPException(403,'Aviso no asignado a este mesero')
    if row.delivered_at is not None or row.acknowledged_at is not None:
        return {'ok':True,'duplicate':True,'received':True}
    row.acknowledged_at=now()
    row.next_attempt_at=None
    audit(db,user,'ready.received',event_id=row.id,ticket_id=row.ticket_id,account_id=row.account_id)
    db.commit();await notify()
    return {'ok':True,'duplicate':False,'received':True}


@app.post('/api/waiter/ready-alerts/{event_id}/deliver')
async def deliver_ready_alert(event_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'waiter')
    row=db.execute(select(ReadyNotificationEvent).where(
        ReadyNotificationEvent.id==event_id).with_for_update()).scalar_one_or_none()
    if not row:raise HTTPException(404,'Aviso no encontrado')
    account=db.get(Account,row.account_id)
    if row.waiter_id!=user.id or not account or account.owner_id!=user.id:
        raise HTTPException(403,'Aviso no asignado a este mesero')
    if row.delivered_at is not None:
        return {'ok':True,'duplicate':True,'delivered':True}
    if row.acknowledged_at is None:
        raise HTTPException(409,'Primero confirma que recibiste el pedido.')
    row.delivered_at=now()
    audit(db,user,'ready.delivered',event_id=row.id,ticket_id=row.ticket_id,account_id=row.account_id)
    db.commit();await notify()
    return {'ok':True,'duplicate':False,'delivered':True}


class StatusIn(BaseModel):status:Literal['received','preparing','ready']
@app.post('/api/tickets/{ticket_id}/status')
async def ticket_status(ticket_id:int,data:StatusIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','kitchen','bar')
    t=db.execute(select(StationTicket).where(StationTicket.id==ticket_id).with_for_update()).scalar_one_or_none()
    if not t:raise HTTPException(404,'Comanda inexistente')
    if user.role in ('kitchen','bar') and t.station!=user.role:raise HTTPException(403,'No corresponde a tu estación')
    allowed={'new':'received','received':'preparing','preparing':'ready'}
    if allowed.get(t.status)!=data.status:raise HTTPException(409,'Transición de estado no permitida')
    t.status=data.status
    if data.status=='received':t.acknowledged_at=now()
    if data.status=='ready':t.ready_at=now()
    audit(db,user,'ticket.status',ticket_id=t.id,status=t.status)
    batch=db.get(Batch,t.batch_id)
    account=db.get(Account,batch.account_id)
    owner_id=account.owner_id
    station=t.station
    account_name=account.name
    complete=False
    if data.status=='ready':
        siblings=db.scalars(select(StationTicket).where(StationTicket.batch_id==batch.id)).all()
        complete=bool(siblings) and all(x.status=='ready' for x in siblings)
    event_id=None
    if data.status=='ready':
        event=ReadyNotificationEvent(event_key=f'ready-{t.id}',
            ticket_id=t.id,batch_id=batch.id,account_id=account.id,
            waiter_id=owner_id,station=station,complete=complete,
            created_at=t.ready_at)
        db.add(event);db.flush();event_id=event.id
    db.commit();await notify()
    if event_id is not None:
        await dispatch_ready_notification(event_id)
    return {'ok':True}

def active_period_for_work(db):
    """Bloqueo compartido del turno: admite varias comandas y bloquea el cierre."""
    p=db.execute(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(
        CashPeriod.id.desc()).with_for_update(read=True)).scalar_one_or_none()
    if not p:raise HTTPException(409,'No hay un turno de caja abierto; administración debe abrir uno')
    return p


def active_period(db):
    """Bloqueo exclusivo reservado para pagos, reparto y cierre de caja."""
    p=db.execute(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(409,'No hay un turno de caja abierto; administración debe abrir uno')
    return p

def period_tip_totals(db, period_id):
    rows=db.scalars(select(PaymentTip).where(PaymentTip.period_id==period_id)).all()
    totals={method:sum(x.cents for x in rows if x.method==method) for method in ('cash','card','transfer')}
    return totals

def tip_payout_cash_total(db, period_id):
    return db.scalar(select(func.coalesce(func.sum(TipPayout.cents),0)).where(
        TipPayout.cash_period_id==period_id,TipPayout.method=='cash')) or 0

def cash_expected_cents(db, period):
    cash_sales=db.scalar(select(func.coalesce(func.sum(Payment.cents),0)).where(
        Payment.period_id==period.id,Payment.method=='cash')) or 0
    tips=period_tip_totals(db,period.id)
    movements=db.scalars(select(CashMovement).where(
        CashMovement.period_id==period.id,CashMovement.status=='approved')).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in movements)
    return period.opening_cents+cash_sales+tips['cash']+net-tip_payout_cash_total(db,period.id)

def tip_distribution_dict(db, distribution):
    if not distribution:
        return None
    shares=db.scalars(select(TipDistributionShare).where(
        TipDistributionShare.distribution_id==distribution.id
    ).order_by(TipDistributionShare.user_id)).all()
    result=[]
    total_paid=0
    pending_total=0
    for share in shares:
        payouts=db.scalars(select(TipPayout).where(
            TipPayout.share_id==share.id
        ).order_by(TipPayout.id)).all()
        paid=sum(x.cents for x in payouts)
        pending=max(0,share.share_cents-paid)
        total_paid+=paid
        pending_total+=pending
        result.append({
            'id':share.id,'user_id':share.user_id,'employee_name':share.employee_name,
            'employee_role':share.employee_role,'share':amount(share.share_cents),
            'paid':amount(paid),'pending':amount(pending),
            'status':'paid' if pending==0 else ('partial' if paid else 'pending'),
            'payouts':[{'id':x.id,'method':x.method,'amount':amount(x.cents),
                        'note':x.note or '', 'date':x.created_at.isoformat() if x.created_at else '',
                        'recorded_by':db.get(User,x.recorded_by).display if db.get(User,x.recorded_by) else '?',
                        'cash_period_id':x.cash_period_id} for x in payouts]
        })
    creator=db.get(User,distribution.created_by)
    return {
        'id':distribution.id,'period_id':distribution.period_id,
        'total':amount(distribution.total_cents),'participant_count':distribution.participant_count,
        'base_share':amount(distribution.base_share_cents),'remainder_cents':distribution.remainder_cents,
        'note':distribution.note or '', 'created_at':distribution.created_at.isoformat() if distribution.created_at else '',
        'created_by':creator.display if creator else '?','paid_total':amount(total_paid),
        'pending_total':amount(pending_total),'shares':result
    }

def tip_distribution_for_period(db, period_id):
    distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==period_id))
    return tip_distribution_dict(db,distribution)

class AccountDiscountIn(BaseModel):
    scope:Literal['account','item']='account'
    item_id:Optional[int]=None
    mode:Literal['fixed','percent']
    value:Decimal=Field(gt=0)
    reason:str=Field(min_length=3,max_length=400)

@app.post('/api/accounts/{account_id}/discounts')
async def apply_account_discount(account_id:int,data:AccountDiscountIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    account=owner_account(db,user,account_id,('open','pending'))
    if db.scalar(select(Payment.id).where(Payment.account_id==account.id).limit(1)):
        raise HTTPException(409,'Los descuentos deben registrarse antes del primer pago para conservar la conciliación.')
    if data.scope=='item' and data.item_id is None:
        raise HTTPException(400,'Selecciona el producto al que aplicar el descuento')
    if data.scope=='account' and data.item_id is not None:
        raise HTTPException(400,'Un descuento general no debe indicar producto')
    gross=account_gross_total(db,account)
    active_discounts=db.scalars(select(AccountDiscount).where(
        AccountDiscount.account_id==account.id,AccountDiscount.status=='applied')).all()
    if data.scope=='item':
        if any(d.scope=='account' for d in active_discounts):
            raise HTTPException(409,'Ya existe un descuento general. Registra primero los descuentos por producto y al final el general.')
        item=db.get(OrderItem,data.item_id)
        batch=db.get(Batch,item.batch_id) if item else None
        if not item or not batch or batch.account_id!=account.id:
            raise HTTPException(400,'El producto no pertenece a esta cuenta')
        base=item.price_cents*(item.qty-item.cancelled_qty)-sum(d.discount_cents for d in active_discounts if d.item_id==item.id)
    else:
        base=gross-sum(d.discount_cents for d in active_discounts)
    if base<=0:
        raise HTTPException(409,'No queda importe disponible para descontar')
    if data.mode=='percent':
        if data.value>Decimal('100'):
            raise HTTPException(400,'El porcentaje no puede superar 100%')
        discount=int((Decimal(base)*data.value/Decimal('100')).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    else:
        discount=cents(data.value)
    if discount<=0 or discount>base:
        raise HTTPException(409,'El descuento debe ser mayor que cero y no superar el importe disponible')
    if gross-sum(d.discount_cents for d in active_discounts)-discount<=0:
        raise HTTPException(409,'El descuento no puede dejar la cuenta con total cero; conserva un importe positivo para poder cerrar y auditar la venta.')
    row=AccountDiscount(account_id=account.id,item_id=data.item_id if data.scope=='item' else None,
        created_by=user.id,mode=data.mode,scope=data.scope,value_text=str(data.value),
        base_cents=base,discount_cents=discount,reason=data.reason,status='applied')
    db.add(row)
    audit(db,user,'discount.apply',account_id=account.id,item_id=row.item_id,scope=row.scope,
          mode=row.mode,value=row.value_text,base_cents=base,discount_cents=discount,reason=row.reason)
    db.commit();await notify()
    total,paid=acc_totals(db,account)
    return {'ok':True,'id':row.id,'discount':amount(discount),'total':amount(total),'due':amount(total-paid)}

@app.post('/api/accounts/{account_id}/discounts/{discount_id}/void')
async def void_account_discount(account_id:int,discount_id:int,data:VoidAccountIn,
                                db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    account=owner_account(db,user,account_id,('open','pending'))
    if db.scalar(select(Payment.id).where(Payment.account_id==account.id).limit(1)):
        raise HTTPException(409,'No se puede retirar el descuento después de registrar pagos')
    row=db.execute(select(AccountDiscount).where(
        AccountDiscount.id==discount_id,AccountDiscount.account_id==account.id).with_for_update()).scalar_one_or_none()
    if not row or row.status!='applied':
        raise HTTPException(404,'Descuento aplicado no encontrado')
    row.status='voided';row.voided_by=user.id;row.void_reason=data.reason
    audit(db,user,'discount.void',account_id=account.id,discount_id=row.id,reason=data.reason)
    db.commit();await notify()
    total,paid=acc_totals(db,account)
    return {'ok':True,'total':amount(total),'due':amount(total-paid)}

class PaymentLine(BaseModel):
    method:Literal['cash','card','transfer']
    amount:Decimal=Field(gt=0)
    received:Optional[Decimal]=None
    tip:Decimal=Field(default=Decimal('0'),ge=0)
class AllocationLine(BaseModel):item_id:int;qty:int=Field(gt=0)
class PaymentIn(BaseModel):client_key:str=Field(min_length=8,max_length=100);lines:list[PaymentLine]=Field(min_length=1,max_length=10);allocations:list[AllocationLine]=[]
@app.post('/api/accounts/{account_id}/payments')
async def pay(account_id:int,data:PaymentIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    # Comparación exacta: LIKE considera '%' y '_' comodines de SQL.
    existing=db.scalar(select(Payment).where(Payment.client_key==f'{data.client_key}-0'))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Clave de pago reutilizada')
        return {'ok':True,'duplicate':True}
    # Orden de bloqueo consistente con send_order: primero el turno y después la
    # cuenta. Evita el deadlock caja↔mesero cuando ambos operan la misma cuenta.
    p=active_period(db)
    a=owner_account(db,user,account_id,('pending',))
    # Segunda lectura tras bloquear la cuenta para evitar reintentos simultáneos.
    existing=db.scalar(select(Payment).where(Payment.client_key==f'{data.client_key}-0'))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Clave de pago reutilizada')
        return {'ok':True,'duplicate':True}
    total,paid=acc_totals(db,a)
    payment_total=sum(cents(l.amount) for l in data.lines)
    if payment_total<=0 or payment_total>total-paid:raise HTTPException(409,'El pago excede el saldo o es inválido')
    normalized=[]
    for line in data.lines:
        value=cents(line.amount);tip_value=cents(line.tip)
        received=cents(line.received) if line.received is not None else value+tip_value
        if line.method=='cash':
            if received<value+tip_value:raise HTTPException(400,'El efectivo recibido debe cubrir el pago y la propina')
        else:
            if line.received is not None and received!=value+tip_value:
                raise HTTPException(400,'El importe recibido debe coincidir con el pago más la propina')
            received=value+tip_value
        normalized.append((line,value,tip_value,received))
    if data.allocations:
        if db.scalar(select(AccountDiscount.id).where(AccountDiscount.account_id==a.id,AccountDiscount.status=='applied').limit(1)):
            raise HTTPException(409,'Con descuentos aplicados, registra el pago por importe para evitar diferencias en la asignación de productos.')
        if len({x.item_id for x in data.allocations})!=len(data.allocations):raise HTTPException(400,'No repitas productos en la misma asignación')
        if db.scalar(select(Payment.id).where(Payment.account_id==a.id).limit(1)) and not db.scalar(select(Allocation.id).where(Allocation.account_id==a.id).limit(1)):
            raise HTTPException(409,'Esta cuenta inició pagos por importe; continúa por importe')
        expected=0
        for alloc in data.allocations:
            item=db.get(OrderItem,alloc.item_id)
            if not item or db.get(Batch,item.batch_id).account_id!=a.id:raise HTTPException(400,'Producto ajeno a esta cuenta')
            reserved=db.scalar(select(func.coalesce(func.sum(Allocation.qty),0)).where(Allocation.order_item_id==item.id)) or 0
            if reserved+alloc.qty>item.qty-item.cancelled_qty:raise HTTPException(409,f'Cantidad de {item.name} ya asignada')
            expected+=alloc.qty*item.price_cents
        if expected!=payment_total:raise HTTPException(409,'Los importes deben coincidir con los productos asignados')
    if any(tip_value>0 for line,value,tip_value,received in normalized) and db.scalar(
        select(TipDistribution.id).where(TipDistribution.period_id==p.id)
    ):
        raise HTTPException(409,'El reparto de propinas de este turno ya fue generado. No se pueden registrar propinas adicionales.')
    tip_total=0
    for idx,(line,value,tip_value,received) in enumerate(normalized):
        payment=Payment(account_id=a.id,cashier_id=user.id,period_id=p.id,client_key=f'{data.client_key}-{idx}',
                        method=line.method,cents=value,received_cents=received)
        db.add(payment);db.flush()
        if tip_value:
            db.add(PaymentTip(payment_id=payment.id,account_id=a.id,cashier_id=user.id,
                              period_id=p.id,method=line.method,cents=tip_value))
            tip_total+=tip_value
    for alloc in data.allocations:db.add(Allocation(account_id=a.id,order_item_id=alloc.item_id,qty=alloc.qty,group_key=data.client_key))
    audit(db,user,'payment.record',account_id=a.id,total=payment_total,tips=tip_total,
          methods=[l.method for l in data.lines]);db.commit();await notify()
    return {'ok':True,'due':amount(total-paid-payment_total),'tips':amount(tip_total),'duplicate':False}

@app.post('/api/accounts/{account_id}/close')
async def close_account(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier');a=owner_account(db,user,account_id,('pending',))
    total,paid=acc_totals(db,a)
    if total<=0 or paid!=total:raise HTTPException(409,'La cuenta debe estar completamente pagada')
    a.status='paid';a.closed_at=now()
    release_table_for_account(db,a,user,'Cuenta pagada y cerrada')
    mitote_extensions.close_reservation_for_account(db,a.id,user)
    audit(db,user,'account.close',id=a.id);db.commit();await notify();return {'ok':True}

@app.get('/api/cash')
def cash(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return {'open':False}
    totals={method:0 for method in ('cash','card','transfer')}
    for method,value in db.execute(
        select(Payment.method,func.coalesce(func.sum(Payment.cents),0))
        .where(Payment.period_id==p.id).group_by(Payment.method)
    ).all():
        if method in totals:totals[method]=int(value or 0)
    tip_totals={method:0 for method in ('cash','card','transfer')}
    for method,value in db.execute(
        select(PaymentTip.method,func.coalesce(func.sum(PaymentTip.cents),0))
        .where(PaymentTip.period_id==p.id).group_by(PaymentTip.method)
    ).all():
        if method in tip_totals:tip_totals[method]=int(value or 0)
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id)).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves if x.status=='approved')
    tip_payout_cash=tip_payout_cash_total(db,p.id)
    expected=p.opening_cents+totals['cash']+tip_totals['cash']+net-tip_payout_cash
    tip_distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==p.id))
    blocking_query=Account.status.notin_(('paid','cancelled'))
    blocking_count=db.scalar(select(func.count(Account.id)).where(blocking_query)) or 0
    blocking=db.scalars(
        select(Account).where(blocking_query).order_by(Account.id).limit(50)
    ).all()
    blocking_data=account_dict_many(db,blocking)
    return {'open':True,'id':p.id,'opened_at':p.opened_at.isoformat(),'opening':amount(p.opening_cents),
            'cash':amount(totals['cash']),'card':amount(totals['card']),'transfer':amount(totals['transfer']),
            'tips':amount(sum(tip_totals.values())),'tip_cash':amount(tip_totals['cash']),
            'tip_card':amount(tip_totals['card']),'tip_transfer':amount(tip_totals['transfer']),
            'tip_payout_cash':amount(tip_payout_cash),
            'tip_distribution':tip_distribution_dict(db,tip_distribution),
            'expected':amount(expected),'movements':[{'id':m.id,'kind':m.kind,'amount':amount(m.cents),'reason':m.reason,'status':m.status} for m in moves],
            'blocking_count':int(blocking_count),'blocking_accounts':blocking_data,
            'can_close':blocking_count==0 and not any(m.status=='pending' for m in moves)}
class MovementIn(BaseModel):
    kind:Literal['in','out']
    amount:Decimal=Field(gt=0)
    reason:str=Field(min_length=3,max_length=400)
    client_key:Optional[str]=Field(default=None,min_length=8,max_length=100)

@app.post('/api/cash/movements')
async def movement(data:MovementIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    movement_cents=cents(data.amount)
    reason=data.reason.strip()
    client_key=(data.client_key or '').strip() or None

    def duplicate_result():
        if not client_key:
            return None
        existing=db.scalar(select(CashMovement).where(CashMovement.client_key==client_key))
        if not existing:
            return None
        same=(existing.requester_id==user.id and existing.kind==data.kind and
              existing.cents==movement_cents and existing.reason==reason)
        if not same:
            raise HTTPException(409,'La clave de movimiento ya fue utilizada con otros datos.')
        return {'id':existing.id,'status':existing.status,'duplicate':True}

    previous=duplicate_result()
    if previous:
        return previous
    p=active_period(db)
    # Segunda lectura después del bloqueo del turno: dos reintentos simultáneos
    # con la misma clave devuelven un solo movimiento, nunca dos salidas/entradas.
    previous=duplicate_result()
    if previous:
        return previous
    m=CashMovement(client_key=client_key,period_id=p.id,requester_id=user.id,
        kind=data.kind,cents=movement_cents,reason=reason,
        status='approved' if user.role in ('owner','admin') else 'pending',
        approved_by=user.id if user.role in ('owner','admin') else None)
    db.add(m)
    audit(db,user,'cash.movement.request',kind=m.kind,cents=m.cents,client_key=client_key)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous=duplicate_result()
        if previous:
            return previous
        raise HTTPException(409,'No se pudo registrar el movimiento. Actualiza e inténtalo de nuevo.')
    await notify()
    return {'id':m.id,'status':m.status,'duplicate':False}
class MoveDecision(BaseModel):approve:bool
@app.post('/api/cash/movements/{movement_id}/decide')
async def move_decide(movement_id:int,data:MoveDecision,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin');m=db.execute(select(CashMovement).where(CashMovement.id==movement_id).with_for_update()).scalar_one_or_none()
    if not m or m.status!='pending':raise HTTPException(409,'Movimiento no pendiente')
    p=active_period(db)
    if p.id!=m.period_id:raise HTTPException(409,'Periodo ya cerrado')
    m.status='approved' if data.approve else 'rejected';m.approved_by=user.id
    audit(db,user,'cash.movement.decide',id=m.id,approve=data.approve);db.commit();await notify();return {'ok':True}
class ClosePeriodIn(BaseModel):counted:Decimal=Field(ge=0);note:str=Field(default='',max_length=400)
@app.post('/api/cash/close')
async def close_cash(data:ClosePeriodIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin');p=active_period(db)
    blocking=db.execute(select(Account.id,Account.name,Account.status).where(Account.status.notin_(('paid','cancelled'))).order_by(Account.id).limit(12)).all()
    if blocking:
        examples=', '.join(f'#{row.id} {row.name} ({row.status})' for row in blocking[:5])
        raise HTTPException(409,f'No se puede cerrar el turno: existen cuentas abiertas o por cobrar. Resuelve primero: {examples}')
    if db.scalar(select(CashMovement.id).where(CashMovement.period_id==p.id,CashMovement.status=='pending').limit(1)):
        raise HTTPException(409,'Resuelve movimientos pendientes antes del corte')
    tip_total=sum(period_tip_totals(db,p.id).values())
    tip_distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==p.id))
    if tip_total>0 and not tip_distribution:
        raise HTTPException(409,'Antes de cerrar caja, administración debe generar el reparto igualitario del fondo común de propinas')
    if tip_distribution and tip_distribution.total_cents!=tip_total:
        raise HTTPException(409,'El total de propinas cambió respecto al reparto. Solicita revisión de administración antes del corte')
    p.closed_at=now();p.counted_cents=cents(data.counted);p.closed_by=user.id;p.note=data.note
    db.flush()
    # Cierre transaccional: sin el documento y la instantánea no se confirma el corte.
    snapshot=cash_period_detail(p.id,db,user)
    snapshot['archived']=True
    snapshot['tips_total']=snapshot.get('tips_total') or amount(sum(period_tip_totals(db,p.id).values()))
    snapshot['product_breakdown']=cash_cut_receipt.cut_product_breakdown(db,p)
    pdf_blob=cash_cut_receipt.build_cut_ticket_pdf(snapshot)
    db.add(CashCutArchive(period_id=p.id,
                          detail_json=json.dumps(snapshot,ensure_ascii=False),
                          pdf_blob=pdf_blob))
    audit(db,user,'cash.close',period=p.id,counted=p.counted_cents,note=data.note,archived=True)
    db.commit();await notify()
    return {'id':p.id,'closed':True,'archived':True}
class OpenPeriodIn(BaseModel):opening:Decimal=Field(ge=0)
@app.post('/api/cash/open')
async def open_cash(data:OpenPeriodIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    if db.scalar(select(CashPeriod.id).where(CashPeriod.closed_at.is_(None))):raise HTTPException(409,'Ya existe un periodo abierto')
    p=CashPeriod(opening_cents=cents(data.opening))
    db.add(p)
    audit(db,user,'cash.open',opening=p.opening_cents)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Ya existe un periodo de caja abierto. Actualiza la pantalla.')
    await notify()
    return {'id':p.id}

class TipDistributionIn(BaseModel):
    user_ids:list[int]=Field(min_length=1,max_length=100)
    note:str=Field(default='',max_length=400)

class TipPayoutIn(BaseModel):
    client_key:str=Field(min_length=8,max_length=100)
    amount:Decimal=Field(gt=0)
    method:Literal['cash','transfer']
    note:str=Field(default='',max_length=400)

@app.get('/api/tips/pool/current')
def tips_pool_current(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    period=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    history=db.scalars(select(TipDistribution).order_by(TipDistribution.id.desc()).limit(20)).all()
    if not period:
        return {'open':False,'distribution':None,'history':[tip_distribution_dict(db,x) for x in history]}
    totals=period_tip_totals(db,period.id)
    total=sum(totals.values())
    distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==period.id))
    blocking_count=db.scalar(select(func.count(Account.id)).where(Account.status.notin_(('paid','cancelled')))) or 0
    employees=db.scalars(select(User).where(User.active.is_(True)).order_by(User.id)).all()
    return {
        'open':True,'period_id':period.id,'opened_at':period.opened_at.isoformat() if period.opened_at else '',
        'tips_total':amount(total),'tips_by_method':{k:amount(v) for k,v in totals.items()},
        'blocking_count':blocking_count,
        'employees':[{'id':x.id,'display':x.display,'role':x.role} for x in employees],
        'distribution':tip_distribution_dict(db,distribution),
        'history':[tip_distribution_dict(db,x) for x in history]
    }

@app.post('/api/tips/pool/current/distribution')
async def create_tip_distribution(data:TipDistributionIn,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    period=active_period(db)
    period=db.execute(select(CashPeriod).where(CashPeriod.id==period.id).with_for_update()).scalar_one()
    if db.scalar(select(TipDistribution.id).where(TipDistribution.period_id==period.id)):
        raise HTTPException(409,'Este turno ya tiene un reparto de propinas registrado')
    blocking=db.scalar(select(Account.id).where(Account.status.notin_(('paid','cancelled'))).limit(1))
    if blocking:
        raise HTTPException(409,'Antes de generar el reparto, finaliza o anula con autorización todas las cuentas del turno')
    totals=period_tip_totals(db,period.id)
    total=sum(totals.values())
    if total<=0:
        raise HTTPException(409,'Este turno todavía no tiene propinas registradas')
    selected=list(dict.fromkeys(data.user_ids))
    if len(selected)!=len(data.user_ids):
        raise HTTPException(400,'No repitas empleados en la distribución')
    employees=db.scalars(select(User).where(User.id.in_(selected),User.active.is_(True)).order_by(User.id)).all()
    if len(employees)!=len(selected):
        raise HTTPException(400,'Uno o más empleados no existen o están desactivados')
    base,remainder=divmod(total,len(employees))
    distribution=TipDistribution(period_id=period.id,created_by=user.id,total_cents=total,
        participant_count=len(employees),base_share_cents=base,remainder_cents=remainder,
        note=data.note.strip())
    db.add(distribution);db.flush()
    for index,employee in enumerate(employees):
        share=base+(1 if index<remainder else 0)
        db.add(TipDistributionShare(distribution_id=distribution.id,user_id=employee.id,
            employee_name=employee.display,employee_role=employee.role,share_cents=share))
    audit(db,user,'tips.pool.distribution.create',distribution_id=distribution.id,period_id=period.id,
        total_cents=total,participant_count=len(employees),base_share_cents=base,
        remainder_cents=remainder,participants=[{'id':x.id,'name':x.display,'role':x.role} for x in employees],
        note=data.note.strip(),remainder_rule='Un centavo adicional a los primeros empleados por ID ascendente')
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'El reparto de este turno ya fue creado. Actualiza la pantalla.')
    await notify()
    distribution=db.get(TipDistribution,distribution.id)
    return {'ok':True,'distribution':tip_distribution_dict(db,distribution)}

@app.post('/api/tips/pool/distributions/{distribution_id}/shares/{share_id}/payouts')
async def record_tip_payout(distribution_id:int,share_id:int,data:TipPayoutIn,
                            db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    existing=db.scalar(select(TipPayout).where(TipPayout.client_key==data.client_key))
    if existing:
        if existing.distribution_id!=distribution_id or existing.share_id!=share_id:
            raise HTTPException(409,'La clave de operación ya fue utilizada en otro reparto')
        return {'ok':True,'duplicate':True,'payout_id':existing.id}
    distribution=db.get(TipDistribution,distribution_id)
    if not distribution:
        raise HTTPException(404,'Reparto de propinas no encontrado')
    share=db.execute(select(TipDistributionShare).where(
        TipDistributionShare.id==share_id,
        TipDistributionShare.distribution_id==distribution_id
    ).with_for_update()).scalar_one_or_none()
    if not share:
        raise HTTPException(404,'Empleado no pertenece a este reparto')
    paid=db.scalar(select(func.coalesce(func.sum(TipPayout.cents),0)).where(TipPayout.share_id==share.id)) or 0
    payout_cents=cents(data.amount)
    if payout_cents<=0 or payout_cents>share.share_cents-paid:
        raise HTTPException(409,'El importe supera el saldo pendiente de este empleado')
    cash_period_id=None
    if data.method=='cash':
        cash_period=active_period(db)
        available=cash_expected_cents(db,cash_period)
        if payout_cents>available:
            raise HTTPException(409,f'No hay efectivo suficiente en caja. Disponible: ${amount(max(0,available))}')
        cash_period_id=cash_period.id
    payout=TipPayout(distribution_id=distribution_id,share_id=share.id,
        cash_period_id=cash_period_id,recorded_by=user.id,client_key=data.client_key,
        method=data.method,cents=payout_cents,note=data.note.strip())
    db.add(payout)
    audit(db,user,'tips.pool.payout',distribution_id=distribution_id,share_id=share.id,
        employee_id=share.user_id,employee_name=share.employee_name,cents=payout_cents,
        method=data.method,cash_period_id=cash_period_id,note=data.note.strip())
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'No se pudo registrar la entrega; actualiza e inténtalo de nuevo')
    await notify()
    return {'ok':True,'payout_id':payout.id,'paid':amount(paid+payout_cents),
            'pending':amount(share.share_cents-paid-payout_cents)}

@app.get('/api/audit')
def audit_log(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin')
    return [{'id':a.id,'actor':db.get(User,a.actor_id).display if a.actor_id and db.get(User,a.actor_id) else 'Sistema','action':a.action,'detail':json.loads(a.detail),'date':a.created_at.isoformat()} for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]


def build_account_pdf(db,account,prebill=False):
    total,paid=acc_totals(db,account)
    gross=account_gross_total(db,account)
    discounts=db.scalars(select(AccountDiscount).where(
        AccountDiscount.account_id==account.id,AccountDiscount.status=='applied').order_by(AccountDiscount.id)).all()
    items=db.scalars(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(Batch.account_id==account.id)).all()
    payments=db.scalars(select(Payment).where(Payment.account_id==account.id).order_by(Payment.id)).all()
    tips=db.scalars(select(PaymentTip).where(PaymentTip.account_id==account.id).order_by(PaymentTip.id)).all()
    tip_by_payment={x.payment_id:x.cents for x in tips}
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=(226,720),rightMargin=14,leftMargin=14,topMargin=15,bottomMargin=15)
    ss=getSampleStyleSheet()
    normal=ParagraphStyle('receipt',parent=ss['Normal'],fontName='Helvetica',fontSize=8,leading=11,spaceAfter=4)
    center=ParagraphStyle('center',parent=normal,alignment=TA_CENTER)
    bold=ParagraphStyle('bold',parent=normal,fontName='Helvetica-Bold')
    story=[]
    from xml.sax.saxutils import escape as xml_escape
    logo_path='static/assets/logo.png'
    if os.path.exists(logo_path):
        story.append(PDFImage(logo_path,width=125,height=65,kind='proportional'))
    story.append(Paragraph('<b>EL MITOTE RESTAURANTE</b>',center))
    story.append(Paragraph('Carretera Progreso Ixmiquilpan<br/>Xochitlán, Hidalgo, C. P. 42740<br/>Tel. 772 200 3943',center))
    story.append(Spacer(1,8))
    heading='PRE-CUENTA' if prebill else 'TICKET DE CONSUMO'
    story.append(Paragraph(f'<b>{heading} · Cuenta #{account.id}</b>',center))
    story.append(Paragraph(xml_escape(account.name),normal))
    waiter=db.get(User,account.owner_id)
    story.append(Paragraph(f'Mesero: {xml_escape(waiter.display if waiter else "?")}',normal))
    story.append(Paragraph('Fecha: '+now().strftime('%d/%m/%Y %H:%M'),normal))
    rows=[]
    for item in items:
        qty=item.qty-item.cancelled_qty
        if qty<=0:continue
        meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==item.id))
        label=xml_escape(item.name)
        if meta and meta.variant_name:label+=' · '+xml_escape(meta.variant_name)
        if meta and meta.modifiers_json:
            try:
                mods=json.loads(meta.modifiers_json or '[]')
                if mods:label+='<br/><font size="7">Extras: '+xml_escape(', '.join(x.get('name','') for x in mods))+'</font>'
            except Exception:pass
        if item.note:label+='<br/><font size="7">Nota: '+xml_escape(item.note)+'</font>'
        rows.append([Paragraph(f'{qty} × {label}',normal),Paragraph('$'+amount(qty*item.price_cents),normal)])
    if rows:
        table=Table(rows,colWidths=[135,60],hAlign='LEFT')
        table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,-1),.25,colors.lightgrey)]))
        story.append(table)
    story.append(Spacer(1,7))
    if discounts:
        story.append(Paragraph('<b>Descuentos aplicados</b>',normal))
        for d in discounts:
            scope='Cuenta completa' if d.scope=='account' else 'Producto '+xml_escape(db.get(OrderItem,d.item_id).name if d.item_id and db.get(OrderItem,d.item_id) else '')
            story.append(Paragraph(f'{scope}: {xml_escape(d.reason)} · -${amount(d.discount_cents)}',normal))
    story.append(Paragraph(f'Subtotal: ${amount(gross)}<br/>Descuentos: -${amount(sum(d.discount_cents for d in discounts))}<br/><b>Total: ${amount(total)}</b><br/>Pagado: ${amount(paid)}<br/><b>Saldo: ${amount(total-paid)}</b>',bold))
    if not prebill:
        if payments:
            story.append(Spacer(1,5))
            story.append(Paragraph('<b>Pagos registrados</b>',normal))
            for p in payments:
                method={'cash':'Efectivo','card':'Tarjeta','transfer':'Transferencia'}.get(p.method,p.method)
                tip=tip_by_payment.get(p.id,0)
                line=f'{method}: ${amount(p.cents)}'
                if tip:line+=f' · Propina ${amount(tip)}'
                story.append(Paragraph(line,normal))
        story.append(Paragraph(f'Propinas registradas: ${amount(sum(t.cents for t in tips))}',normal))
        story.append(Paragraph('<b>'+('PAGADO' if account.status=='paid' else 'PENDIENTE')+'</b>',center))
        story.append(Paragraph('Comprobante de consumo · No fiscal',center))
    else:
        story.append(Spacer(1,8))
        story.append(Paragraph('<b>PRE-CUENTA · NO ES COMPROBANTE DE PAGO</b>',center))
        story.append(Paragraph('La impresión de esta pre-cuenta no registra pagos ni cierra la cuenta.',center))
    story.append(Spacer(1,8))
    story.append(Paragraph('Muchas gracias por tu visita, te esperamos pronto',center))
    doc.build(story)
    return buf.getvalue()

@app.get('/api/accounts/{account_id}/prebill.pdf')
def account_prebill_pdf(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier','waiter')
    account=owner_account(db,user,account_id,allowed=None)
    payload=build_account_pdf(db,account,prebill=True)
    return Response(content=payload,media_type='application/pdf',
        headers={'Content-Disposition':f'inline; filename="ElMitote_PreCuenta_{account.id}.pdf"','Cache-Control':'no-store'})

@app.get('/api/accounts/{account_id}/ticket.pdf')
def ticket_pdf(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier','waiter')
    account=owner_account(db,user,account_id,allowed=None)
    payload=build_account_pdf(db,account,prebill=False)
    return Response(content=payload,media_type='application/pdf',
        headers={'Content-Disposition':f'attachment; filename="ElMitote_Ticket_{account.id}.pdf"','Cache-Control':'no-store'})

def period_summary(db,p):
    archived=db.get(CashCutArchive,p.id)
    if archived:
        detail=json.loads(archived.detail_json)
        result={k:v for k,v in detail.items() if k not in ('accounts','payments','tips','movements')}
        total_tip_pesos=sum(Decimal(str(detail.get(k) or '0')) for k in ('tip_cash','tip_card','tip_transfer'))
        result['tips']=detail.get('tips_total') or f"{total_tip_pesos:.2f}"
        return result
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id)).all()
    totals={method:sum(x.cents for x in payments if x.method==method) for method in ('cash','card','transfer')}
    tips=db.scalars(select(PaymentTip).where(PaymentTip.period_id==p.id)).all()
    tip_totals={method:sum(x.cents for x in tips if x.method==method) for method in ('cash','card','transfer')}
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id,CashMovement.status=='approved')).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves)
    tip_payout_cash=tip_payout_cash_total(db,p.id)
    expected=p.opening_cents+totals['cash']+tip_totals['cash']+net-tip_payout_cash
    tip_distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==p.id))
    accounts=db.scalars(select(Account).where(Account.created_at>=p.opened_at,Account.created_at<=p.closed_at).order_by(Account.id)).all()
    closer=db.get(User,p.closed_by) if p.closed_by else None
    cashier_totals={}
    for payment in payments:
        cashier=db.get(User,payment.cashier_id)
        label=cashier.display if cashier else 'Desconocido'
        cashier_totals[label]=cashier_totals.get(label,0)+payment.cents
    return {'id':p.id,'opened_at':p.opened_at.isoformat(),'closed_at':p.closed_at.isoformat(),
            'closed_by':closer.display if closer else '?','opening':amount(p.opening_cents),
            'cash':amount(totals['cash']),'card':amount(totals['card']),
            'transfer':amount(totals['transfer']),'sales':amount(sum(totals.values())),
            'tips':amount(sum(tip_totals.values())),'tip_cash':amount(tip_totals['cash']),
            'tip_card':amount(tip_totals['card']),'tip_transfer':amount(tip_totals['transfer']),
            'tip_payout_cash':amount(tip_payout_cash),
            'tip_distribution':tip_distribution_dict(db,tip_distribution),
            'cashier_totals':{name:amount(value) for name,value in cashier_totals.items()},
            'net_movements':amount(net),'expected':amount(expected),
            'counted':amount(p.counted_cents or 0),'difference':amount((p.counted_cents or 0)-expected),
            'account_count':len(accounts),'paid_accounts':sum(a.status=='paid' for a in accounts),
            'cancelled_accounts':sum(a.status=='cancelled' for a in accounts),'note':p.note or ''}

@app.get('/api/cash/history')
def cash_history(db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    ps=db.scalars(select(CashPeriod).where(CashPeriod.closed_at.is_not(None)).order_by(CashPeriod.id.desc()).limit(50)).all()
    return [period_summary(db,p) for p in ps]

@app.get('/api/cash/periods/{period_id}')
def cash_period_detail(period_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    p=db.get(CashPeriod,period_id)
    if not p or not p.closed_at:raise HTTPException(404,'Turno cerrado no encontrado')
    archived=db.get(CashCutArchive,p.id)
    if archived:return json.loads(archived.detail_json)
    summary=period_summary(db,p)
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id).order_by(Payment.id)).all()
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id).order_by(CashMovement.id)).all()
    accounts=db.scalars(select(Account).where(Account.created_at>=p.opened_at,Account.created_at<=p.closed_at).order_by(Account.id)).all()
    summary['accounts']=[account_dict(db,a) for a in accounts]
    summary['payments']=[{'id':x.id,'account_id':x.account_id,'account':db.get(Account,x.account_id).name if db.get(Account,x.account_id) else '?',
                          'cashier':db.get(User,x.cashier_id).display if db.get(User,x.cashier_id) else '?',
                          'method':x.method,'amount':amount(x.cents),
                          'tip':amount(db.scalar(select(PaymentTip.cents).where(PaymentTip.payment_id==x.id)) or 0),
                          'date':x.created_at.isoformat()} for x in payments]
    summary['tips_total']=summary['tips']
    summary['tips']=[{'id':x.id,'payment_id':x.payment_id,'account_id':x.account_id,
                      'account':db.get(Account,x.account_id).name if db.get(Account,x.account_id) else '?',
                      'cashier':db.get(User,x.cashier_id).display if db.get(User,x.cashier_id) else '?',
                      'method':x.method,'amount':amount(x.cents),'date':x.created_at.isoformat()}
                     for x in db.scalars(select(PaymentTip).where(PaymentTip.period_id==p.id).order_by(PaymentTip.id))]
    summary['movements']=[{'id':x.id,'kind':x.kind,'amount':amount(x.cents),'reason':x.reason,'status':x.status,
                           'date':x.created_at.isoformat()} for x in moves]
    return summary

@app.get('/api/cash/periods/{period_id}/report.pdf')
def cash_report(period_id:int,db:Session=Depends(db_session),user:User=Depends(auth_either)):
    allow(user,'owner','admin','cashier')
    p=db.get(CashPeriod,period_id)
    if not p or not p.closed_at:raise HTTPException(404,'Corte no encontrado')
    archived=db.get(CashCutArchive,p.id)
    if archived:
        return Response(content=archived.pdf_blob,media_type='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="ElMitote_Corte_{p.id}.pdf"',
                     'Cache-Control':'no-store'})
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id)).all()
    totals={method:sum(x.cents for x in payments if x.method==method) for method in ('cash','card','transfer')}
    tips=db.scalars(select(PaymentTip).where(PaymentTip.period_id==p.id)).all()
    tip_totals={method:sum(x.cents for x in tips if x.method==method) for method in ('cash','card','transfer')}
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id,CashMovement.status=='approved')).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves)
    tip_payout_cash=tip_payout_cash_total(db,p.id)
    expected=p.opening_cents+totals['cash']+tip_totals['cash']+net-tip_payout_cash
    distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==p.id))
    distribution_data=tip_distribution_dict(db,distribution)
    buf=io.BytesIO();doc=SimpleDocTemplate(buf,pagesize=(420,620),leftMargin=25,rightMargin=25,topMargin=25,bottomMargin=25)
    style=getSampleStyleSheet();story=[Paragraph('<b>EL MITOTE RESTAURANTE</b>',style['Heading2']),Paragraph('Corte general de caja · Folio #'+str(p.id),style['Heading3'])]
    cashier_totals={}
    for payment in payments:
        cashier=db.get(User,payment.cashier_id)
        label=cashier.display if cashier else 'Desconocido'
        cashier_totals[label]=cashier_totals.get(label,0)+payment.cents
    entries=[('Inicio',p.opened_at.strftime('%d/%m/%Y %H:%M')),('Cierre',p.closed_at.strftime('%d/%m/%Y %H:%M')),('Responsable',db.get(User,p.closed_by).display if p.closed_by else '?'),('Fondo inicial','$'+amount(p.opening_cents)),('Ventas cobradas en efectivo','$'+amount(totals['cash'])),('Ventas cobradas con tarjeta','$'+amount(totals['card'])),('Ventas cobradas por transferencia','$'+amount(totals['transfer'])),('Propinas efectivo','$'+amount(tip_totals['cash'])),('Propinas tarjeta','$'+amount(tip_totals['card'])),('Propinas transferencia','$'+amount(tip_totals['transfer'])),('Propinas totales','$'+amount(sum(tip_totals.values())))]
    entries.extend([('Cobros · '+name,'$'+amount(value)) for name,value in sorted(cashier_totals.items())])
    entries.extend([('Salidas en efectivo · fondo de propinas','-$'+amount(tip_payout_cash))])
    if distribution_data:
        entries.extend([('Fondo común de propinas','$'+amount(distribution.total_cents)),
                        ('Participantes',str(distribution.participant_count)),
                        ('Propinas entregadas','$'+distribution_data['paid_total']),
                        ('Propinas pendientes','$'+distribution_data['pending_total'])])
        for share in distribution_data['shares']:
            entries.append(('Reparto · '+share['employee_name'],'$'+share['share']+' · entregado $'+share['paid']))
    entries.extend([('Entradas y salidas netas','$'+amount(net)),('Efectivo esperado','$'+amount(expected)),('Efectivo contado','$'+amount(p.counted_cents)),('Diferencia','$'+amount(p.counted_cents-expected))])
    table=Table(entries,colWidths=[220,150]);table.setStyle(TableStyle([('LINEBELOW',(0,0),(-1,-1),.4,colors.lightgrey),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    from xml.sax.saxutils import escape as xml_escape
    story.append(table);story.append(Spacer(1,14))
    # Detalle inmutable de ventas y propinas del mismo turno.
    sale_by_account={}
    tip_by_payment={x.payment_id:x.cents for x in tips}
    for payment in payments:
        row=sale_by_account.setdefault(payment.account_id,{'sales':0,'tips':0})
        row['sales']+=payment.cents
        row['tips']+=tip_by_payment.get(payment.id,0)
    story.append(Paragraph('Cuentas cobradas en este corte',style['Heading3']))
    if sale_by_account:
        sale_rows=[('Cuenta','Ventas','Propinas')]
        for account_id in sorted(sale_by_account):
            account=db.get(Account,account_id)
            name=account.name if account else 'Cuenta'
            row=sale_by_account[account_id]
            sale_rows.append((Paragraph('#'+str(account_id)+' · '+xml_escape(name),style['Normal']),
                              '$'+amount(row['sales']),'$'+amount(row['tips'])))
        detail_table=Table(sale_rows,colWidths=[210,80,80],repeatRows=1)
        detail_table.setStyle(TableStyle([('LINEBELOW',(0,0),(-1,-1),.3,colors.lightgrey),
                                         ('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.append(detail_table)
    else:
        story.append(Paragraph('Sin ventas registradas.',style['Normal']))
    if moves:
        story.append(Spacer(1,10))
        story.append(Paragraph('Movimientos autorizados del corte',style['Heading3']))
        move_rows=[('Tipo','Importe','Motivo')]
        for movement in moves:
            move_rows.append(('Entrada' if movement.kind=='in' else 'Salida',
                ('$' if movement.kind=='in' else '-$')+amount(movement.cents),
                Paragraph(xml_escape(movement.reason),style['Normal'])))
        movements_table=Table(move_rows,colWidths=[75,75,220],repeatRows=1)
        movements_table.setStyle(TableStyle([('LINEBELOW',(0,0),(-1,-1),.3,colors.lightgrey),
                                            ('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.append(movements_table)
    story.append(Spacer(1,14));story.append(Paragraph('Observaciones: '+xml_escape(p.note or 'Sin observaciones'),style['Normal']))
    story.append(Spacer(1,10));story.append(Paragraph('Movimientos autorizados: '+str(len(moves)),style['Normal']))
    story.append(Paragraph('Documento de control interno · No fiscal',style['Normal']))
    doc.build(story)
    return Response(content=buf.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="ElMitote_Corte_{p.id}.pdf"','Cache-Control':'no-store'})
# Registrar modelos y rutas de extensiones antes del startup/metadata.create_all.
import mitote_extensions  # noqa: E402
import menu_photo_import  # noqa: E402
import cash_cut_receipt  # noqa: E402
