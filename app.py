"""El Mitote Comandas - independent restaurant PWA API.

Run: ADMIN_PASSWORD='strong secret' SECRET_KEY='long random string' uvicorn app:app --reload
Do not deploy without PostgreSQL, HTTPS and changing bootstrap credentials.
"""
import os, json, asyncio, secrets, io, time, logging, hashlib, hmac, unicodedata, re
from urllib.parse import urlparse
from pywebpush import webpush, WebPushException
from firebase_admin import credentials, messaging, initialize_app, get_app
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (create_engine, Column, Integer, String, Boolean, ForeignKey,
                        DateTime, Text, LargeBinary, UniqueConstraint, select, func)
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

class Audit(Base):
    __tablename__='audit'
    id=Column(Integer,primary_key=True)
    actor_id=Column(Integer,ForeignKey('users.id'))
    action=Column(String(80),nullable=False)
    detail=Column(Text,default='{}')
    created_at=Column(DateTime(timezone=True),default=now)

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
        # Solo aplica menú cuando exista la aprobación explícita en Render.
        menu_photo_import.apply_approved_photo_menu_at_startup(db)
        db.commit()

@asynccontextmanager
async def lifespan(app):
    bootstrap()
    prep_worker=asyncio.create_task(mitote_extensions.prep_alert_worker())
    try:
        yield
    finally:
        prep_worker.cancel()
        try:
            await prep_worker
        except asyncio.CancelledError:
            pass

app=FastAPI(title='El Mitote Comandas',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)
app.mount('/static',StaticFiles(directory='static'),name='static')

@app.get('/')
def root(): return FileResponse('static/index.html')
@app.get('/sw.js')
def sw(): return FileResponse('static/sw.js',media_type='application/javascript')
@app.get('/manifest.webmanifest')
def manifest(): return FileResponse('static/manifest.webmanifest',media_type='application/manifest+json')
@app.get('/api/health')
def health(): return {'ok':True}

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

def owner_account(db,user,account_id,allowed=('open','pending')):
    acc=db.execute(select(Account).where(Account.id==account_id).with_for_update()).scalar_one_or_none()
    if not acc: raise HTTPException(404,'Cuenta inexistente')
    if user.role=='waiter' and acc.owner_id!=user.id: raise HTTPException(403,'No es tu cuenta')
    if allowed and acc.status not in allowed: raise HTTPException(409,'La cuenta no permite esta operación')
    return acc

def account_gross_total(db,acc):
    rows=db.execute(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(Batch.account_id==acc.id)).scalars().all()
    return sum(x.price_cents*(x.qty-x.cancelled_qty) for x in rows)

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

def product_dict(p,settings,db=None):
    status='No disponible' if p.blocked or not p.active else ('Sin control' if not settings.inventory_on or not p.inventory_enabled else ('Agotado' if p.stock==0 else 'Stock bajo' if p.stock<=3 else 'Disponible'))
    variants=[];groups=[];promotions=[];active_promo=None
    if db is not None:
        variants=[{'id':v.id,'name':v.name,'price':amount(v.price_cents),'active':v.active,'sort_order':v.sort_order}
                  for v in db.scalars(select(ProductVariant).where(ProductVariant.product_id==p.id).order_by(ProductVariant.sort_order,ProductVariant.id))]
        for g in db.scalars(select(ProductModifierGroup).where(ProductModifierGroup.product_id==p.id).order_by(ProductModifierGroup.sort_order,ProductModifierGroup.id)):
            options=[{'id':o.id,'name':o.name,'price_delta':amount(o.price_delta_cents),'active':o.active,'sort_order':o.sort_order}
                     for o in db.scalars(select(ProductModifierOption).where(ProductModifierOption.group_id==g.id).order_by(ProductModifierOption.sort_order,ProductModifierOption.id))]
            groups.append({'id':g.id,'name':g.name,'min_select':g.min_select,'max_select':g.max_select,'active':g.active,'options':options})
        promo_rows=db.scalars(select(ProductPromotion).where(ProductPromotion.product_id==p.id).order_by(ProductPromotion.id.desc())).all()
        promotions=[{'id':x.id,'name':x.name,'price':amount(x.price_cents) if x.price_cents is not None else None,'discount_percent':x.discount_percent,'weekdays':x.weekdays,'start_time':x.start_time,'end_time':x.end_time,'start_date':x.start_date,'end_date':x.end_date,'active':x.active}
                    for x in promo_rows]
        active_promo=next((x for x in promo_rows if promotion_is_active(x)),None)
    current_cents=apply_promotion(p.price_cents,active_promo)
    return {'id':p.id,'name':p.name,'category':p.category,'station':p.station,'price':amount(p.price_cents),
            'current_price':amount(current_cents),'promotion':({'id':active_promo.id,'name':active_promo.name,'price':amount(current_cents),'fixed_price':amount(active_promo.price_cents) if active_promo.price_cents is not None else None,'discount_percent':active_promo.discount_percent} if active_promo else None),
            'variants':variants,'modifier_groups':groups,'promotions':promotions,
            'active':p.active,'blocked':p.blocked,'inventory_enabled':p.inventory_enabled,'stock':p.stock,'version':p.version,'status':status}

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
    rec = db.scalar(select(UserPin).where(UserPin.lookup == _pin_lookup(data.pin)))
    user = db.get(User, rec.user_id) if rec else None
    try:
        valid = bool(user and user.active and ph.verify(rec.pin_hash, data.pin))
    except (VerifyMismatchError, VerificationError):
        valid = False
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
                vapid_claims={'sub':VAPID_SUBJECT},ttl=600,timeout=5)
        return None
    except WebPushException as exc:
        if exc.status_code in (404,410):return row['id']
        log.warning('Fallo push id=%s codigo=%s',row['id'],exc.status_code)
    except Exception:
        log.exception('Error de notificación push id=%s',row['id'])
    return None

async def push_to(payload,role=None,user_id=None):
    if not PUSH_ENABLED:return
    try:
        with SessionLocal() as db:
            q=select(PushDevice).join(User,PushDevice.user_id==User.id).where(User.active.is_(True))
            if role:q=q.where(User.role==role)
            if user_id:q=q.where(User.id==user_id)
            rows=[{'id':d.id,'endpoint':d.endpoint,'p256dh':d.p256dh,'auth_key':d.auth_key}
                  for d in db.scalars(q)]
        if not rows:return
        results=await asyncio.gather(*(asyncio.to_thread(_push_one,r,payload) for r in rows),return_exceptions=True)
        expired=[x for x in results if isinstance(x,int)]
        if expired:
            with SessionLocal() as db:
                for rid in expired:
                    d=db.get(PushDevice,rid)
                    if d:db.delete(d)
                db.commit()
    except Exception:
        log.exception('No se pudieron distribuir notificaciones push')

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
              'route': route},
        android=messaging.AndroidConfig(priority='high', ttl=timedelta(minutes=10))
    )
    try:
        messaging.send(msg)
        return None
    except Exception as exc:
        if isinstance(exc, getattr(messaging, 'UnregisteredError', type(None))):
            return device['id']
        log.warning('FCM no pudo entregar al dispositivo %s: %s', device['id'], type(exc).__name__)
        return None

async def fcm_to(payload, role=None, user_id=None):
    if not _firebase_ready():
        return
    try:
        with SessionLocal() as db:
            q = select(FcmDevice).join(User, FcmDevice.user_id == User.id).where(User.active.is_(True))
            if role:
                q = q.where(User.role == role)
            if user_id:
                q = q.where(User.id == user_id)
            devices = [{'id': x.id, 'token': x.token} for x in db.scalars(q)]
        if not devices:
            return
        results = await asyncio.gather(*(asyncio.to_thread(_fcm_send_one, d, payload) for d in devices), return_exceptions=True)
        expired = [x for x in results if isinstance(x, int)]
        if expired:
            with SessionLocal() as db:
                for device_id in expired:
                    old = db.get(FcmDevice, device_id)
                    if old:
                        db.delete(old)
                db.commit()
    except Exception:
        log.exception('Fallo el envío FCM')

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
async def create_staff(data: StaffCreateIn, db: Session = Depends(db_session), user: User = Depends(auth)):
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
                           db: Session = Depends(db_session), user: User = Depends(auth)):
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
def users(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    return [{'id':u.id,'username':u.username,'display':u.display,'role':u.role,'active':u.active,'has_pin':db.get(UserPin,u.id) is not None} for u in db.scalars(select(User).order_by(User.id))]
@app.post('/api/users')
async def add_user(data:UserIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def update_user(user_id:int,data:UserUpdateIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
def settings(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    s=db.get(Setting,1);return {'inventory_on':s.inventory_on,'inventory_reviewed':s.inventory_reviewed}
class SettingsIn(BaseModel): inventory_on:bool;confirmed_recount:bool=False
@app.put('/api/settings')
async def set_settings(data:SettingsIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    s=db.get(Setting,1)
    if data.inventory_on and not s.inventory_on and not data.confirmed_recount:raise HTTPException(409,'Confirma el recuento de existencias antes de activar inventario')
    old=s.inventory_on;s.inventory_on=data.inventory_on;s.inventory_reviewed=data.confirmed_recount if data.inventory_on else False
    audit(db,user,'inventory.global',old=old,new=s.inventory_on);db.commit();await notify();return {'ok':True}

@app.get('/api/products')
def products(db:Session=Depends(db_session),user:User=Depends(auth)):
    s=db.get(Setting,1)
    query=select(Product).outerjoin(ArchivedProduct,ArchivedProduct.product_id==Product.id).where(
        ArchivedProduct.product_id.is_(None)).order_by(Product.category,Product.name)
    return [product_dict(p,s,db) for p in db.scalars(query)]
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
async def add_product_variant(product_id:int,data:ProductVariantIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def add_modifier_group(product_id:int,data:ModifierGroupIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    if not db.get(Product,product_id):raise HTTPException(404,'Producto inexistente')
    if data.min_select>data.max_select:raise HTTPException(400,'La selección mínima no puede superar la máxima')
    g=ProductModifierGroup(product_id=product_id,name=data.name.strip(),min_select=data.min_select,max_select=data.max_select,active=data.active,sort_order=data.sort_order)
    db.add(g);db.flush();audit(db,user,'product.modifier_group.create',product_id=product_id,group_id=g.id,name=g.name);db.commit();await notify();return {'id':g.id,'ok':True}

@app.post('/api/modifier-groups/{group_id}/options')
async def add_modifier_option(group_id:int,data:ModifierOptionIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');g=db.get(ProductModifierGroup,group_id)
    if not g:raise HTTPException(404,'Grupo de modificadores inexistente')
    o=ProductModifierOption(group_id=g.id,name=data.name.strip(),price_delta_cents=cents(data.price_delta),active=data.active,sort_order=data.sort_order)
    db.add(o);db.flush();audit(db,user,'product.modifier_option.create',group_id=g.id,option_id=o.id,name=o.name,price_delta=amount(o.price_delta_cents));db.commit();await notify();return {'id':o.id,'ok':True}

@app.patch('/api/modifier-options/{option_id}')
async def toggle_modifier_option(option_id:int,data:ActiveToggleIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');o=db.get(ProductModifierOption,option_id)
    if not o:raise HTTPException(404,'Modificador inexistente')
    o.active=data.active;audit(db,user,'product.modifier_option.toggle',option_id=o.id,active=o.active);db.commit();await notify();return {'ok':True}

@app.post('/api/products/{product_id}/promotions')
async def add_product_promotion(product_id:int,data:PromotionIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def add_product(data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=Product(name=data.name.strip(),category=data.category.strip()[:80],station=data.station,price_cents=cents(data.price),active=data.active,blocked=data.blocked,inventory_enabled=data.inventory_enabled,stock=data.stock)
    db.add(p);audit(db,user,'product.create',name=p.name);db.commit();await notify();return {'id':p.id}
@app.get('/api/products/archived')
def archived_products(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    rows=db.execute(select(ArchivedProduct,Product).join(
        Product,Product.id==ArchivedProduct.product_id).order_by(ArchivedProduct.archived_at.desc()).limit(150)).all()
    return [{'id':p.id,'name':p.name,'station':p.station,'version':p.version,
             'reason':a.reason,'archived_at':a.archived_at.isoformat() if a.archived_at else ''}
            for a,p in rows]

@app.post('/api/products/{product_id}/archive')
async def archive_product(product_id:int,data:ProductArchiveIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def restore_product(product_id:int,data:ProductRestoreIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def edit_product(product_id:int,data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if data.expected_version is None or p.version != data.expected_version:
        raise HTTPException(409,'El producto o sus existencias cambiaron. Actualiza la pantalla antes de guardar')
    before=product_dict(p,db.get(Setting,1),db)
    p.name=data.name.strip();p.category=data.category.strip()[:80];p.station=data.station;p.price_cents=cents(data.price);p.active=data.active;p.blocked=data.blocked;p.inventory_enabled=data.inventory_enabled;p.stock=data.stock;p.version+=1
    audit(db,user,'product.update',id=p.id,before=before,after=product_dict(p,db.get(Setting,1),db));db.commit();await notify();return {'ok':True}
class RestockIn(BaseModel): delta:int=Field(gt=0,le=100000);reason:str=Field(min_length=3,max_length=400);expected_version:int
@app.post('/api/products/{product_id}/restock')
async def restock(product_id:int,data:RestockIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if p.version!=data.expected_version:raise HTTPException(409,'Existencias cambiaron; actualiza la pantalla')
    before=p.stock;p.stock+=data.delta;p.version+=1
    audit(db,user,'inventory.restock',product_id=p.id,before=before,delta=data.delta,after=p.stock,reason=data.reason);db.commit();await notify();return {'stock':p.stock}

def dining_table_dict(db,t):
    occupancy=db.get(TableOccupancy,t.id)
    account=db.get(Account,occupancy.account_id) if occupancy else None
    owner=db.get(User,account.owner_id) if account else None
    occupied=bool(account and account.status in ('open','pending'))
    return {'id':t.id,'name':t.name,'area':t.area,'capacity':t.capacity,'active':t.active,
            'sort_order':t.sort_order,'status':'occupied' if occupied else 'available',
            'account_id':account.id if occupied else None,'account_name':account.name if occupied else None,
            'waiter':owner.display if owner else None}

@app.get('/api/tables')
def dining_tables(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    q=select(DiningTable).order_by(DiningTable.area,DiningTable.sort_order,DiningTable.id)
    if user.role not in ('owner','admin'):
        q=q.where(DiningTable.active.is_(True))
    return [dining_table_dict(db,t) for t in db.scalars(q)]

class DiningTableIn(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    area:str=Field(default='Salón',min_length=1,max_length=80)
    capacity:int=Field(default=4,ge=1,le=30)
    active:bool=True
    sort_order:int=Field(default=0,ge=0,le=10000)

@app.post('/api/tables')
async def create_dining_table(data:DiningTableIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def update_dining_table(table_id:int,data:DiningTableIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
def accounts(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return []  # Los cierres anteriores se consultan solo desde el historial de caja.
    q=select(Account).where(Account.created_at>=p.opened_at).order_by(Account.id.desc())
    if user.role=='waiter':q=q.where(Account.owner_id==user.id)
    else:q=q.limit(200)
    return [account_dict(db,a) for a in db.scalars(q)]
class AccountIn(BaseModel):
    name:Optional[str]=Field(default=None,max_length=120)
    table_id:Optional[int]=None

@app.post('/api/accounts')
async def new_account(data:AccountIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter')
    period=active_period(db)  # Locks the current shift against a concurrent close.
    if db.scalar(select(TipDistribution.id).where(TipDistribution.period_id==period.id)):
        raise HTTPException(409,'El reparto de propinas ya fue generado; el turno está en proceso de cierre. Abre un nuevo turno para continuar.')
    table=None
    if data.table_id is not None:
        table=db.execute(select(DiningTable).where(DiningTable.id==data.table_id).with_for_update()).scalar_one_or_none()
        if not table or not table.active:raise HTTPException(404,'La mesa no existe o está desactivada')
        if db.get(TableOccupancy,table.id):raise HTTPException(409,'Esta mesa ya está ocupada. Actualiza la lista de mesas.')
    account_name=(data.name or '').strip()
    if not account_name and table:account_name=table.name
    if not account_name:raise HTTPException(400,'Escribe un nombre para la cuenta libre o selecciona una mesa')
    a=Account(name=account_name,owner_id=user.id)
    db.add(a);db.flush()
    audit(db,user,'account.create',id=a.id,name=a.name,table_id=table.id if table else None)
    if table:
        db.add(TableOccupancy(table_id=table.id,account_id=a.id,assigned_by=user.id))
        audit(db,user,'table.assign',table_id=table.id,table_name=table.name,account_id=a.id,waiter=user.display)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'La mesa acaba de ser ocupada por otro mesero. Actualiza las mesas.')
    await notify()
    return {'id':a.id,'table_id':table.id if table else None}
class VoidAccountIn(BaseModel):
    reason:str=Field(min_length=3,max_length=400)

@app.post('/api/accounts/{account_id}/void')
async def void_account(account_id:int,data:VoidAccountIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
def account_detail(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    a=owner_account(db,user,account_id,allowed=None)
    batches=db.scalars(select(Batch).where(Batch.account_id==a.id).order_by(Batch.id)).all()
    result=account_dict(db,a);result['items']=[];result['batches']=[]
    for b in batches:
        items=db.scalars(select(OrderItem).where(OrderItem.batch_id==b.id)).all()
        result['batches'].append({'id':b.id,'sent_by':db.get(User,b.sent_by).display,'created_at':b.created_at.isoformat() if b.created_at else ''})
        for i in items:
            meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==i.id))
            try:modifiers=json.loads(meta.modifiers_json or '[]') if meta else []
            except Exception:modifiers=[]
            result['items'].append({'id':i.id,'batch_id':b.id,'name':i.name,'station':i.station,'price':amount(i.price_cents),
                'qty':i.qty,'cancelled_qty':i.cancelled_qty,'note':i.note,'variant_name':meta.variant_name if meta else '',
                'modifiers':modifiers,'prep_minutes':meta.prep_minutes if meta else 15,
                'release_after_minutes':meta.release_after_minutes if meta else 0,'promotion_name':meta.promotion_name if meta else ''})
    result['payments']=[{'id':p.id,'method':p.method,'amount':amount(p.cents),'received':amount(p.received_cents),
                         'tip':amount(db.scalar(select(PaymentTip.cents).where(PaymentTip.payment_id==p.id)) or 0),
                         'created_at':p.created_at.isoformat() if p.created_at else ''} for p in db.scalars(select(Payment).where(Payment.account_id==a.id).order_by(Payment.id))]
    result['discounts']=[{'id':d.id,'scope':d.scope,'item_id':d.item_id,
                          'item_name':(db.get(OrderItem,d.item_id).name if d.item_id and db.get(OrderItem,d.item_id) else ''),
                          'mode':d.mode,'value':d.value_text,'base':amount(d.base_cents),
                          'amount':amount(d.discount_cents),'reason':d.reason,'status':d.status,
                          'created_at':d.created_at.isoformat() if d.created_at else ''}
                         for d in db.scalars(select(AccountDiscount).where(AccountDiscount.account_id==a.id).order_by(AccountDiscount.id))]
    result['tips']=[{'id':t.id,'payment_id':t.payment_id,'method':t.method,'amount':amount(t.cents),
                     'created_at':t.created_at.isoformat() if t.created_at else ''}
                    for t in db.scalars(select(PaymentTip).where(PaymentTip.account_id==a.id).order_by(PaymentTip.id))]
    return result
class ReassignIn(BaseModel): owner_id:int;reason:str=Field(min_length=3,max_length=400)
@app.post('/api/accounts/{account_id}/reassign')
async def reassign(account_id:int,data:ReassignIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner');a=owner_account(db,user,account_id)
    dest=db.get(User,data.owner_id)
    if not dest or not dest.active or dest.role!='waiter':raise HTTPException(400,'El destino debe ser un mesero activo')
    old=a.owner_id;a.owner_id=dest.id;audit(db,user,'account.reassign',id=a.id,old=old,new=dest.id,reason=data.reason);db.commit();await notify();return {'ok':True}


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
def account_history(account_id: int, db: Session = Depends(db_session), user: User = Depends(auth)):
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
                         db: Session = Depends(db_session), user: User = Depends(auth)):
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
                                 db: Session = Depends(db_session), user: User = Depends(auth)):
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
                         db: Session = Depends(db_session), user: User = Depends(auth)):
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
                        db: Session = Depends(db_session), user: User = Depends(auth)):
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
                                db: Session = Depends(db_session), user: User = Depends(auth)):
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
async def send_order(account_id:int,data:OrderIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter')
    active_period(db)
    existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Identificador de envío ya utilizado')
        return {'batch_id':existing.id,'duplicate':True}
    a=owner_account(db,user,account_id,('open',))
    setting=db.get(Setting,1);need={}
    for line in data.items:need[line.product_id]=need.get(line.product_id,0)+line.qty
    products={p.id:p for p in db.scalars(select(Product).where(Product.id.in_(need)).order_by(Product.id).with_for_update())}
    if len(products)!=len(need):raise HTTPException(400,'Hay productos inexistentes')
    for pid,qty in need.items():
        p=products[pid]
        if not p.active or p.blocked:raise HTTPException(409,f'{p.name} no está disponible')
        if setting.inventory_on and p.inventory_enabled and p.stock<qty:raise HTTPException(409,f'Sin existencias suficientes de {p.name}')
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
            modifier_snapshot.append({'group':group.name if group else 'Extra','name':option.name,'price_delta':amount(option.price_delta_cents)})
        prepared.append({'line':line,'product':p,'variant':variant,'options':modifier_snapshot,
                         'base_cents':promo_base,'modifier_cents':modifier_cents,'final_cents':final_price,
                         'promo_name':promo.name if promo else ''})
    batch=Batch(account_id=a.id,sent_by=user.id,client_key=data.client_key);db.add(batch);db.flush()
    mitote_extensions.snapshot_batch_policy(db,batch.id)
    for row in prepared:
        line=row['line'];p=row['product'];variant=row['variant']
        item=OrderItem(batch_id=batch.id,product_id=p.id,name=p.name,station=p.station,price_cents=row['final_cents'],qty=line.qty,note=line.note.strip())
        db.add(item);db.flush()
        db.add(OrderItemMeta(order_item_id=item.id,variant_name=variant.name if variant else '',
            modifiers_json=json.dumps(row['options'],ensure_ascii=False),prep_minutes=mitote_extensions.product_prep_minutes(db,p.id,line.prep_minutes),
            release_after_minutes=line.release_after_minutes,promotion_name=row['promo_name'],
            base_price_cents=row['base_cents'],modifier_price_cents=row['modifier_cents']))
    for pid,qty in need.items():
        p=products[pid]
        if setting.inventory_on and p.inventory_enabled:p.stock-=qty;p.version+=1
    for station in stations:db.add(StationTicket(batch_id=batch.id,station=station))
    audit(db,user,'order.send',account_id=a.id,batch_id=batch.id,stations=sorted(stations),items=len(prepared))
    db.commit();await notify()
    for station in sorted(stations):
        await push_to({'title':'El Mitote · '+('Cocina' if station=='kitchen' else 'Barra'),
                       'body':f'Nueva comanda #{batch.id} · {a.name}',
                       'url':f'/?push=station&account={a.id}',
                       'tag':f'mitote-order-{batch.id}-{station}'},role=station)
        await fcm_to({'title':'El Mitote · '+('Cocina' if station=='kitchen' else 'Barra'),
                      'body':f'Nueva comanda #{batch.id}','url':'/?push=station'},role=station)
    return {'batch_id':batch.id,'duplicate':False}

class RepeatOrderIn(BaseModel):
    qty:int=Field(default=1,ge=1,le=100)
    client_key:str=Field(min_length=8,max_length=100)

@app.post('/api/accounts/{account_id}/items/{item_id}/repeat')
async def repeat_order_item(account_id:int,item_id:int,data:RepeatOrderIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter');active_period(db)
    existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Identificador de envío ya utilizado')
        return {'batch_id':existing.id,'duplicate':True}
    account=owner_account(db,user,account_id,('open',))
    item=db.execute(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(OrderItem.id==item_id,Batch.account_id==account_id).with_for_update()).scalar_one_or_none()
    if not item:raise HTTPException(404,'Producto de la cuenta no encontrado')
    product=db.execute(select(Product).where(Product.id==item.product_id).with_for_update()).scalar_one_or_none()
    if not product or not product.active or product.blocked:raise HTTPException(409,'El producto ya no está disponible')
    setting=db.get(Setting,1)
    if setting.inventory_on and product.inventory_enabled and product.stock<data.qty:raise HTTPException(409,'No hay existencias suficientes para repetir el producto')
    old_meta=db.scalar(select(OrderItemMeta).where(OrderItemMeta.order_item_id==item.id))
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
    if setting.inventory_on and product.inventory_enabled:product.stock-=data.qty;product.version+=1
    db.add(StationTicket(batch_id=batch.id,station=item.station))
    audit(db,user,'order.repeat',account_id=account.id,source_item_id=item.id,new_item_id=new_item.id,qty=data.qty,batch_id=batch.id)
    db.commit();await notify()
    await push_to({'title':'El Mitote · '+('Cocina' if item.station=='kitchen' else 'Barra'),'body':f'Producto repetido · {account.name}','url':'/?push=station','tag':f'mitote-order-{batch.id}-{item.station}'},role=item.station)
    await fcm_to({'title':'El Mitote · '+('Cocina' if item.station=='kitchen' else 'Barra'),'body':f'Producto repetido · Comanda #{batch.id}','url':'/?push=station'},role=item.station)
    return {'batch_id':batch.id,'duplicate':False}

@app.post('/api/accounts/{account_id}/request-bill')
async def request_bill(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter');a=owner_account(db,user,account_id,('open',))
    total,paid=acc_totals(db,a)
    if total<=0:raise HTTPException(409,'La cuenta no tiene productos')
    a.status='pending';audit(db,user,'account.request_bill',id=a.id);db.commit();await notify();return {'ok':True}
class ApprovalIn(BaseModel):kind:Literal['reopen','cancel'];reason:str=Field(min_length=3,max_length=400);item_id:Optional[int]=None;qty:Optional[int]=None
@app.post('/api/accounts/{account_id}/requests')
async def request_approval(account_id:int,data:ApprovalIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter');a=owner_account(db,user,account_id)
    if data.kind=='reopen':
        if a.status!='pending' or acc_totals(db,a)[1]>0:raise HTTPException(409,'Solo cuentas pendientes sin abonos pueden reabrirse')
    else:
        item=db.get(OrderItem,data.item_id) if data.item_id else None
        if not item or db.get(Batch,item.batch_id).account_id!=a.id or not data.qty or data.qty<1 or data.qty>item.qty-item.cancelled_qty:raise HTTPException(400,'Producto o cantidad inválidos')
    r=Approval(account_id=a.id,requester_id=user.id,kind=data.kind,item_id=data.item_id,qty=data.qty,reason=data.reason)
    db.add(r);audit(db,user,'approval.request',kind=data.kind,account_id=a.id);db.commit();await notify();return {'id':r.id}
@app.get('/api/requests')
def requests(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    return [{'id':r.id,'account_id':r.account_id,'account':db.get(Account,r.account_id).name,'kind':r.kind,'item_id':r.item_id,'qty':r.qty,'reason':r.reason,'status':r.status,'requester':db.get(User,r.requester_id).display} for r in db.scalars(select(Approval).order_by(Approval.id.desc()).limit(100))]
class DecisionIn(BaseModel):approve:bool;return_stock:bool=False
@app.post('/api/requests/{request_id}/decide')
async def decide(request_id:int,data:DecisionIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
                p=db.execute(select(Product).where(Product.id==item.product_id).with_for_update()).scalar_one()
                s=db.get(Setting,1)
                if s.inventory_on and p.inventory_enabled:p.stock+=r.qty;p.version+=1
    r.status='approved' if data.approve else 'rejected';r.decided_by=user.id
    audit(db,user,'approval.decide',id=r.id,approved=data.approve,return_stock=data.return_stock);db.commit();await notify();return {'ok':True}

@app.get('/api/tickets')
def tickets(db:Session=Depends(db_session),user:User=Depends(auth)):
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
class StatusIn(BaseModel):status:Literal['received','preparing','ready']
@app.post('/api/tickets/{ticket_id}/status')
async def ticket_status(ticket_id:int,data:StatusIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
    db.commit();await notify()
    if data.status=='ready':
        await push_to({'title':'El Mitote · Pedido listo',
                       'body':f'{"Cocina" if station=="kitchen" else "Barra"} terminó el pedido de {account_name}',
                       'url':f'/?push=accounts&account={account.id}',
                       'tag':f'mitote-ready-{t.id}'},role='waiter',user_id=owner_id)
        await fcm_to({'title': 'El Mitote · Pedido listo',
                      'body': f'Pedido listo · comanda #{batch.id}',
                      'url': '/?push=accounts'}, role='waiter', user_id=owner_id)
    return {'ok':True}

def active_period(db):
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
async def apply_account_discount(account_id:int,data:AccountDiscountIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
                                db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def pay(account_id:int,data:PaymentIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    # Comparación exacta: LIKE considera '%' y '_' comodines de SQL.
    existing=db.scalar(select(Payment).where(Payment.client_key==f'{data.client_key}-0'))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Clave de pago reutilizada')
        return {'ok':True,'duplicate':True}
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
    p=active_period(db)
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
async def close_account(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier');a=owner_account(db,user,account_id,('pending',))
    total,paid=acc_totals(db,a)
    if total<=0 or paid!=total:raise HTTPException(409,'La cuenta debe estar completamente pagada')
    a.status='paid';a.closed_at=now()
    release_table_for_account(db,a,user,'Cuenta pagada y cerrada')
    mitote_extensions.close_reservation_for_account(db,a.id,user)
    audit(db,user,'account.close',id=a.id);db.commit();await notify();return {'ok':True}

@app.get('/api/cash')
def cash(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return {'open':False}
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id)).all()
    totals={method:sum(x.cents for x in payments if x.method==method) for method in ('cash','card','transfer')}
    tips=db.scalars(select(PaymentTip).where(PaymentTip.period_id==p.id)).all()
    tip_totals={method:sum(x.cents for x in tips if x.method==method) for method in ('cash','card','transfer')}
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id)).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves if x.status=='approved')
    tip_payout_cash=tip_payout_cash_total(db,p.id)
    expected=p.opening_cents+totals['cash']+tip_totals['cash']+net-tip_payout_cash
    tip_distribution=db.scalar(select(TipDistribution).where(TipDistribution.period_id==p.id))
    blocking=db.scalars(select(Account).where(Account.status.notin_(('paid','cancelled'))).order_by(Account.id)).all()
    return {'open':True,'id':p.id,'opened_at':p.opened_at.isoformat(),'opening':amount(p.opening_cents),
            'cash':amount(totals['cash']),'card':amount(totals['card']),'transfer':amount(totals['transfer']),
            'tips':amount(sum(tip_totals.values())),'tip_cash':amount(tip_totals['cash']),
            'tip_card':amount(tip_totals['card']),'tip_transfer':amount(tip_totals['transfer']),
            'tip_payout_cash':amount(tip_payout_cash),
            'tip_distribution':tip_distribution_dict(db,tip_distribution),
            'expected':amount(expected),'movements':[{'id':m.id,'kind':m.kind,'amount':amount(m.cents),'reason':m.reason,'status':m.status} for m in moves],
            'blocking_count':len(blocking),'blocking_accounts':[account_dict(db,a) for a in blocking[:50]],
            'can_close':not blocking and not any(m.status=='pending' for m in moves)}
class MovementIn(BaseModel):kind:Literal['in','out'];amount:Decimal=Field(gt=0);reason:str=Field(min_length=3,max_length=400)
@app.post('/api/cash/movements')
async def movement(data:MovementIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier');p=active_period(db)
    m=CashMovement(period_id=p.id,requester_id=user.id,kind=data.kind,cents=cents(data.amount),reason=data.reason,status='approved' if user.role in ('owner','admin') else 'pending',approved_by=user.id if user.role in ('owner','admin') else None)
    db.add(m);audit(db,user,'cash.movement.request',kind=m.kind,cents=m.cents);db.commit();await notify();return {'id':m.id,'status':m.status}
class MoveDecision(BaseModel):approve:bool
@app.post('/api/cash/movements/{movement_id}/decide')
async def move_decide(movement_id:int,data:MoveDecision,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');m=db.execute(select(CashMovement).where(CashMovement.id==movement_id).with_for_update()).scalar_one_or_none()
    if not m or m.status!='pending':raise HTTPException(409,'Movimiento no pendiente')
    p=active_period(db)
    if p.id!=m.period_id:raise HTTPException(409,'Periodo ya cerrado')
    m.status='approved' if data.approve else 'rejected';m.approved_by=user.id
    audit(db,user,'cash.movement.decide',id=m.id,approve=data.approve);db.commit();await notify();return {'ok':True}
class ClosePeriodIn(BaseModel):counted:Decimal=Field(ge=0);note:str=Field(default='',max_length=400)
@app.post('/api/cash/close')
async def close_cash(data:ClosePeriodIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def open_cash(data:OpenPeriodIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    if db.scalar(select(CashPeriod.id).where(CashPeriod.closed_at.is_(None))):raise HTTPException(409,'Ya existe un periodo abierto')
    p=CashPeriod(opening_cents=cents(data.opening));db.add(p);audit(db,user,'cash.open',opening=p.opening_cents);db.commit();await notify();return {'id':p.id}

class TipDistributionIn(BaseModel):
    user_ids:list[int]=Field(min_length=1,max_length=100)
    note:str=Field(default='',max_length=400)

class TipPayoutIn(BaseModel):
    client_key:str=Field(min_length=8,max_length=100)
    amount:Decimal=Field(gt=0)
    method:Literal['cash','transfer']
    note:str=Field(default='',max_length=400)

@app.get('/api/tips/pool/current')
def tips_pool_current(db:Session=Depends(db_session),user:User=Depends(auth)):
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
async def create_tip_distribution(data:TipDistributionIn,db:Session=Depends(db_session),user:User=Depends(auth)):
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
                            db:Session=Depends(db_session),user:User=Depends(auth)):
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
def audit_log(db:Session=Depends(db_session),user:User=Depends(auth)):
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
def account_prebill_pdf(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    account=owner_account(db,user,account_id,allowed=None)
    payload=build_account_pdf(db,account,prebill=True)
    return Response(content=payload,media_type='application/pdf',
        headers={'Content-Disposition':f'inline; filename="ElMitote_PreCuenta_{account.id}.pdf"','Cache-Control':'no-store'})

@app.get('/api/accounts/{account_id}/ticket.pdf')
def ticket_pdf(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
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
        result['tips']=detail.get('tips_total') or amount(sum(
            float(detail.get(k) or 0) for k in ('tip_cash','tip_card','tip_transfer')))
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
def cash_history(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    ps=db.scalars(select(CashPeriod).where(CashPeriod.closed_at.is_not(None)).order_by(CashPeriod.id.desc()).limit(50)).all()
    return [period_summary(db,p) for p in ps]

@app.get('/api/cash/periods/{period_id}')
def cash_period_detail(period_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
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
def cash_report(period_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
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
