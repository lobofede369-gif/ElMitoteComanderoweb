"""El Mitote Comandas - independent restaurant PWA API.

Run: ADMIN_PASSWORD='strong secret' SECRET_KEY='long random string' uvicorn app:app --reload
Do not deploy without PostgreSQL, HTTPS and changing bootstrap credentials.
"""
import os, json, asyncio, secrets, io, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
                        DateTime, Text, UniqueConstraint, select, func)
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

class Account(Base):
    __tablename__='accounts'
    id=Column(Integer,primary_key=True)
    name=Column(String(120),nullable=False)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    status=Column(String(20),default='open',nullable=False)
    created_at=Column(DateTime(timezone=True),default=now)
    closed_at=Column(DateTime(timezone=True))

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
        if not db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None))):
            db.add(CashPeriod(opening_cents=0))
        db.commit()

@asynccontextmanager
async def lifespan(app):
    bootstrap()
    yield

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

def acc_totals(db,acc):
    rows=db.execute(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(Batch.account_id==acc.id)).scalars().all()
    total=sum(x.price_cents*(x.qty-x.cancelled_qty) for x in rows)
    paid=db.scalar(select(func.coalesce(func.sum(Payment.cents),0)).where(Payment.account_id==acc.id)) or 0
    return total,paid

def account_dict(db,a):
    total,paid=acc_totals(db,a)
    u=db.get(User,a.owner_id)
    return {'id':a.id,'name':a.name,'owner_id':a.owner_id,'owner':u.display if u else '?','status':a.status,'total':amount(total),'paid':amount(paid),'due':amount(total-paid),'created_at':a.created_at.isoformat() if a.created_at else ''}

def product_dict(p,settings):
    status='No disponible' if p.blocked or not p.active else ('Sin control' if not settings.inventory_on or not p.inventory_enabled else ('Agotado' if p.stock==0 else 'Stock bajo' if p.stock<=3 else 'Disponible'))
    return {'id':p.id,'name':p.name,'category':p.category,'station':p.station,'price':amount(p.price_cents),'active':p.active,'blocked':p.blocked,'inventory_enabled':p.inventory_enabled,'stock':p.stock,'version':p.version,'status':status}

class LoginIn(BaseModel): username:str; secret:str
@app.post('/api/login')
def login(data:LoginIn,request:Request,db:Session=Depends(db_session)):
    
identifier=f"{request.client.host if request.client else 'unknown'}:{data.username.strip().lower()}"

    attempts=[t for t in login_failures.get(identifier,[]) if time.time()-t<900]
    if len(attempts)>=7:raise HTTPException(429,'Demasiados intentos. Espera 15 minutos')
    user=db.scalar(select(User).where(User.username==data.username.strip()))
    try: valid=bool(user and user.active and ph.verify(user.password_hash,data.secret))
    except (VerifyMismatchError,VerificationError): valid=False
    if not valid:
        attempts.append(time.time());login_failures[identifier]=attempts
        raise HTTPException(401,'Credenciales incorrectas')
    login_failures.pop(identifier,None)
    token=jwt.encode({'sub':str(user.id),'ver':user.session_version,'exp':now()+timedelta(hours=10)},SECRET_KEY,algorithm='HS256')
    return {'token':token,'user':{'id':user.id,'display':user.display,'username':user.username,'role':user.role}}

@app.get('/api/me')
def me(user:User=Depends(auth)):
    return {'id':user.id,'username':user.username,'display':user.display,'role':user.role}

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

class UserIn(BaseModel): username:str=Field(min_length=3,max_length=80);display:str=Field(min_length=2,max_length=100);role:Literal['admin','cashier','waiter','kitchen','bar'];secret:str=Field(min_length=4,max_length=120)
@app.get('/api/users')
def users(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    return [{'id':u.id,'username':u.username,'display':u.display,'role':u.role,'active':u.active} for u in db.scalars(select(User).order_by(User.id))]
@app.post('/api/users')
async def add_user(data:UserIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner')
    if data.role=='waiter' and (len(data.secret)!=4 or not data.secret.isdigit()):raise HTTPException(400,'El PIN del mesero debe tener cuatro dígitos')
    if data.role!='waiter' and len(data.secret)<8:raise HTTPException(400,'La contraseña debe tener al menos 8 caracteres')
    if db.scalar(select(User).where(User.username==data.username.strip())):raise HTTPException(409,'Usuario ya registrado')
    u=User(username=data.username.strip(),display=data.display.strip(),role=data.role,password_hash=ph.hash(data.secret))
    db.add(u);audit(db,user,'user.create',username=u.username,role=u.role);db.commit();await notify()
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
        if target.role=='waiter' and (len(data.secret)!=4 or not data.secret.isdigit()):raise HTTPException(400,'PIN de 4 dígitos requerido')
        if target.role!='waiter' and len(data.secret)<8:raise HTTPException(400,'Contraseña de 8 caracteres requerida')
        target.password_hash=ph.hash(data.secret)
    target.session_version+=1
    audit(db,user,'user.update',id=target.id,active=target.active,reset=bool(data.secret));db.commit();await notify();return {'ok':True}

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
    return [product_dict(p,s) for p in db.scalars(select(Product).order_by(Product.category,Product.name))]
class ProductIn(BaseModel):
    name:str=Field(min_length=1,max_length=160)
    category:str='General'
    station:Literal['kitchen','bar']
    price:Decimal=Field(ge=0)
    active:bool=True
    blocked:bool=False
    inventory_enabled:bool=False
    stock:int=Field(default=0,ge=0)
@app.post('/api/products')
async def add_product(data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=Product(name=data.name.strip(),category=data.category.strip()[:80],station=data.station,price_cents=cents(data.price),active=data.active,blocked=data.blocked,inventory_enabled=data.inventory_enabled,stock=data.stock)
    db.add(p);audit(db,user,'product.create',name=p.name);db.commit();await notify();return {'id':p.id}
@app.put('/api/products/{product_id}')
async def edit_product(product_id:int,data:ProductIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    before=product_dict(p,db.get(Setting,1))
    p.name=data.name.strip();p.category=data.category.strip()[:80];p.station=data.station;p.price_cents=cents(data.price);p.active=data.active;p.blocked=data.blocked;p.inventory_enabled=data.inventory_enabled;p.stock=data.stock;p.version+=1
    audit(db,user,'product.update',id=p.id,before=before,after=product_dict(p,db.get(Setting,1)));db.commit();await notify();return {'ok':True}
class RestockIn(BaseModel): delta:int=Field(gt=0,le=100000);reason:str=Field(min_length=3,max_length=400);expected_version:int
@app.post('/api/products/{product_id}/restock')
async def restock(product_id:int,data:RestockIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    p=db.execute(select(Product).where(Product.id==product_id).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(404,'Producto inexistente')
    if p.version!=data.expected_version:raise HTTPException(409,'Existencias cambiaron; actualiza la pantalla')
    before=p.stock;p.stock+=data.delta;p.version+=1
    audit(db,user,'inventory.restock',product_id=p.id,before=before,delta=data.delta,after=p.stock,reason=data.reason);db.commit();await notify();return {'stock':p.stock}

@app.get('/api/accounts')
def accounts(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    q=select(Account).order_by(Account.id.desc())
    if user.role=='waiter':q=q.where(Account.owner_id==user.id)
    else:q=q.limit(200)
    return [account_dict(db,a) for a in db.scalars(q)]
class AccountIn(BaseModel): name:str=Field(min_length=1,max_length=120)
@app.post('/api/accounts')
async def new_account(data:AccountIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter')
    a=Account(name=data.name.strip(),owner_id=user.id)
    db.add(a);db.flush();audit(db,user,'account.create',id=a.id,name=a.name);db.commit();await notify();return {'id':a.id}
@app.get('/api/accounts/{account_id}')
def account_detail(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    a=owner_account(db,user,account_id,allowed=None)
    batches=db.scalars(select(Batch).where(Batch.account_id==a.id).order_by(Batch.id)).all()
    result=account_dict(db,a);result['items']=[];result['batches']=[]
    for b in batches:
        items=db.scalars(select(OrderItem).where(OrderItem.batch_id==b.id)).all()
        result['batches'].append({'id':b.id,'sent_by':db.get(User,b.sent_by).display,'created_at':b.created_at.isoformat() if b.created_at else ''})
        for i in items:result['items'].append({'id':i.id,'batch_id':b.id,'name':i.name,'station':i.station,'price':amount(i.price_cents),'qty':i.qty,'cancelled_qty':i.cancelled_qty,'note':i.note})
    result['payments']=[{'id':p.id,'method':p.method,'amount':amount(p.cents),'received':amount(p.received_cents),'created_at':p.created_at.isoformat() if p.created_at else ''} for p in db.scalars(select(Payment).where(Payment.account_id==a.id).order_by(Payment.id))]
    return result
class ReassignIn(BaseModel): owner_id:int;reason:str=Field(min_length=3,max_length=400)
@app.post('/api/accounts/{account_id}/reassign')
async def reassign(account_id:int,data:ReassignIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin');a=owner_account(db,user,account_id)
    dest=db.get(User,data.owner_id)
    if not dest or not dest.active or dest.role!='waiter':raise HTTPException(400,'El destino debe ser un mesero activo')
    old=a.owner_id;a.owner_id=dest.id;audit(db,user,'account.reassign',id=a.id,old=old,new=dest.id,reason=data.reason);db.commit();await notify();return {'ok':True}
class OrderLine(BaseModel): product_id:int;qty:int=Field(ge=1,le=100);note:str=Field(default='',max_length=300)
class OrderIn(BaseModel): items:list[OrderLine]=Field(min_length=1,max_length=100);client_key:str=Field(min_length=8,max_length=100)
@app.post('/api/accounts/{account_id}/orders')
async def send_order(account_id:int,data:OrderIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','waiter')
    existing=db.scalar(select(Batch).where(Batch.client_key==data.client_key))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Identificador de envío ya utilizado')
        return {'batch_id':existing.id,'duplicate':True}
    a=owner_account(db,user,account_id,('open',))
    s=db.get(Setting,1);need={}
    for line in data.items:need[line.product_id]=need.get(line.product_id,0)+line.qty
    ps={p.id:p for p in db.scalars(select(Product).where(Product.id.in_(need)).order_by(Product.id).with_for_update())}
    if len(ps)!=len(need):raise HTTPException(400,'Hay productos inexistentes')
    for pid,qty in need.items():
        p=ps[pid]
        if not p.active or p.blocked:raise HTTPException(409,f'{p.name} no está disponible')
        if s.inventory_on and p.inventory_enabled and p.stock<qty:raise HTTPException(409,f'Sin existencias suficientes de {p.name}')
    b=Batch(account_id=a.id,sent_by=user.id,client_key=data.client_key);db.add(b);db.flush()
    stations=set()
    for line in data.items:
        p=ps[line.product_id];stations.add(p.station)
        db.add(OrderItem(batch_id=b.id,product_id=p.id,name=p.name,station=p.station,price_cents=p.price_cents,qty=line.qty,note=line.note))
    for pid,qty in need.items():
        p=ps[pid]
        if s.inventory_on and p.inventory_enabled:p.stock-=qty;p.version+=1
    for station in stations:db.add(StationTicket(batch_id=b.id,station=station))
    audit(db,user,'order.send',account_id=a.id,batch_id=b.id,stations=sorted(stations));db.commit();await notify();return {'batch_id':b.id,'duplicate':False}

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
    q=select(StationTicket).order_by(StationTicket.id.desc()).limit(150)
    if user.role in ('kitchen','bar'):q=q.where(StationTicket.station==user.role)
    out=[]
    for t in db.scalars(q):
        b=db.get(Batch,t.batch_id);a=db.get(Account,b.account_id)
        lines=db.scalars(select(OrderItem).where(OrderItem.batch_id==b.id,OrderItem.station==t.station)).all()
        out.append({'id':t.id,'batch_id':b.id,'account':a.name,'account_id':a.id,'waiter':db.get(User,b.sent_by).display,'station':t.station,'status':t.status,'created_at':t.created_at.isoformat() if t.created_at else '', 'items':[{'name':x.name,'qty':x.qty-x.cancelled_qty,'note':x.note} for x in lines if x.qty>x.cancelled_qty]})
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
    audit(db,user,'ticket.status',ticket_id=t.id,status=t.status);db.commit();await notify();return {'ok':True}

def active_period(db):
    p=db.execute(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()).with_for_update()).scalar_one_or_none()
    if not p:raise HTTPException(409,'No hay un turno de caja abierto; administración debe abrir uno')
    return p

class PaymentLine(BaseModel):method:Literal['cash','card','transfer'];amount:Decimal=Field(gt=0);received:Optional[Decimal]=None
class AllocationLine(BaseModel):item_id:int;qty:int=Field(gt=0)
class PaymentIn(BaseModel):client_key:str=Field(min_length=8,max_length=100);lines:list[PaymentLine]=Field(min_length=1,max_length=10);allocations:list[AllocationLine]=[]
@app.post('/api/accounts/{account_id}/payments')
async def pay(account_id:int,data:PaymentIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    existing=db.scalar(select(Payment).where(Payment.client_key.like(data.client_key+'-%')))
    if existing:
        if existing.account_id!=account_id:raise HTTPException(409,'Clave de pago reutilizada')
        return {'ok':True,'duplicate':True}
    a=owner_account(db,user,account_id,('pending',));total,paid=acc_totals(db,a)
    payment_total=sum(cents(l.amount) for l in data.lines)
    if payment_total<=0 or payment_total>total-paid:raise HTTPException(409,'El pago excede el saldo o es inválido')
    for line in data.lines:
        value=cents(line.amount);received=cents(line.received) if line.received is not None else value
        if line.method!='cash' and received!=value:raise HTTPException(400,'Solo efectivo admite cambio')
        if received<value:raise HTTPException(400,'Efectivo recibido insuficiente')
    if data.allocations:
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
    for idx,line in enumerate(data.lines):
        value=cents(line.amount);received=cents(line.received) if line.received is not None else value
        db.add(Payment(account_id=a.id,cashier_id=user.id,period_id=p.id,client_key=f'{data.client_key}-{idx}',method=line.method,cents=value,received_cents=received))
    for alloc in data.allocations:db.add(Allocation(account_id=a.id,order_item_id=alloc.item_id,qty=alloc.qty,group_key=data.client_key))
    audit(db,user,'payment.record',account_id=a.id,total=payment_total,methods=[l.method for l in data.lines]);db.commit();await notify()
    return {'ok':True,'due':amount(total-paid-payment_total),'duplicate':False}

@app.post('/api/accounts/{account_id}/close')
async def close_account(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier');a=owner_account(db,user,account_id,('pending',))
    total,paid=acc_totals(db,a)
    if total<=0 or paid!=total:raise HTTPException(409,'La cuenta debe estar completamente pagada')
    a.status='paid';a.closed_at=now();audit(db,user,'account.close',id=a.id);db.commit();await notify();return {'ok':True}

@app.get('/api/cash')
def cash(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    p=db.scalar(select(CashPeriod).where(CashPeriod.closed_at.is_(None)).order_by(CashPeriod.id.desc()))
    if not p:return {'open':False}
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id)).all()
    totals={method:sum(x.cents for x in payments if x.method==method) for method in ('cash','card','transfer')}
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id)).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves if x.status=='approved')
    expected=p.opening_cents+totals['cash']+net
    return {'open':True,'id':p.id,'opened_at':p.opened_at.isoformat(),'opening':amount(p.opening_cents),'cash':amount(totals['cash']),'card':amount(totals['card']),'transfer':amount(totals['transfer']),'expected':amount(expected),'movements':[{'id':m.id,'kind':m.kind,'amount':amount(m.cents),'reason':m.reason,'status':m.status} for m in moves]}
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
    if db.scalar(select(CashMovement.id).where(CashMovement.period_id==p.id,CashMovement.status=='pending').limit(1)):raise HTTPException(409,'Resuelve movimientos pendientes antes del corte')
    p.closed_at=now();p.counted_cents=cents(data.counted);p.closed_by=user.id;p.note=data.note
    audit(db,user,'cash.close',period=p.id,counted=p.counted_cents,note=data.note);db.commit();await notify();return {'id':p.id,'closed':True}
class OpenPeriodIn(BaseModel):opening:Decimal=Field(ge=0)
@app.post('/api/cash/open')
async def open_cash(data:OpenPeriodIn,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    if db.scalar(select(CashPeriod.id).where(CashPeriod.closed_at.is_(None))):raise HTTPException(409,'Ya existe un periodo abierto')
    p=CashPeriod(opening_cents=cents(data.opening));db.add(p);audit(db,user,'cash.open',opening=p.opening_cents);db.commit();await notify();return {'id':p.id}

@app.get('/api/audit')
def audit_log(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin')
    return [{'id':a.id,'actor':db.get(User,a.actor_id).display if a.actor_id and db.get(User,a.actor_id) else 'Sistema','action':a.action,'detail':json.loads(a.detail),'date':a.created_at.isoformat()} for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]


@app.get('/api/accounts/{account_id}/ticket.pdf')
def ticket_pdf(account_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier','waiter')
    a=owner_account(db,user,account_id,allowed=None)
    total,paid=acc_totals(db,a)
    items=db.scalars(select(OrderItem).join(Batch,Batch.id==OrderItem.batch_id).where(Batch.account_id==a.id)).all()
    payments=db.scalars(select(Payment).where(Payment.account_id==a.id).order_by(Payment.id)).all()
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=(226,650),rightMargin=14,leftMargin=14,topMargin=15,bottomMargin=15)
    ss=getSampleStyleSheet();normal=ParagraphStyle('receipt',parent=ss['Normal'],fontName='Helvetica',fontSize=8,leading=11,spaceAfter=4)
    center=ParagraphStyle('center',parent=normal,alignment=TA_CENTER)
    bold=ParagraphStyle('bold',parent=normal,fontName='Helvetica-Bold')
    story=[]
    from xml.sax.saxutils import escape as xml_escape
    story.append(PDFImage('static/assets/logo.png',width=125,height=65,kind='proportional'))
    story.append(Paragraph('<b>EL MITOTE RESTAURANTE</b>',center))
    story.append(Paragraph('Carretera Progreso Ixmiquilpan<br/>Xochitlán, Hidalgo, C. P. 42740<br/>Tel. 772 200 3943',center))
    story.append(Spacer(1,8))
    story.append(Paragraph(f'<b>Cuenta #{a.id}</b> · {xml_escape(a.name)}',normal))
    story.append(Paragraph(f'Mesero: {xml_escape(db.get(User,a.owner_id).display)}',normal))
    story.append(Paragraph('Fecha: '+(a.closed_at or now()).strftime('%d/%m/%Y %H:%M'),normal))
    rows=[]
    for i in items:
        qty=i.qty-i.cancelled_qty
        if qty:rows.append([Paragraph(f'{qty} × {xml_escape(i.name)}',normal),Paragraph('$'+amount(qty*i.price_cents),normal)])
    if rows:
        t=Table(rows,colWidths=[135,60],hAlign='LEFT')
        t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,-1),.25,colors.lightgrey)]))
        story.append(t)
    story.append(Spacer(1,8))
    story.append(Paragraph(f'<b>Total: ${amount(total)}</b><br/>Pagado: ${amount(paid)}<br/>Saldo: ${amount(total-paid)}',bold))
    for p in payments:
        method={'cash':'Efectivo','card':'Tarjeta','transfer':'Transferencia'}.get(p.method,p.method)
        story.append(Paragraph(f'{method}: ${amount(p.cents)}',normal))
    story.append(Spacer(1,8))
    story.append(Paragraph('<b>'+('PAGADO' if a.status=='paid' else 'PENDIENTE')+'</b>',center))
    story.append(Paragraph('Muchas gracias por tu visita, te esperamos pronto',center))
    story.append(Paragraph('Comprobante de consumo · No fiscal',center))
    doc.build(story)
    return Response(content=buf.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="ElMitote_Ticket_{a.id}.pdf"','Cache-Control':'no-store'})

@app.get('/api/cash/history')
def cash_history(db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    ps=db.scalars(select(CashPeriod).where(CashPeriod.closed_at.is_not(None)).order_by(CashPeriod.id.desc()).limit(50)).all()
    return [{'id':p.id,'opened_at':p.opened_at.isoformat(),'closed_at':p.closed_at.isoformat(),'counted':amount(p.counted_cents or 0),'closed_by':db.get(User,p.closed_by).display if p.closed_by else '?'} for p in ps]

@app.get('/api/cash/periods/{period_id}/report.pdf')
def cash_report(period_id:int,db:Session=Depends(db_session),user:User=Depends(auth)):
    allow(user,'owner','admin','cashier')
    p=db.get(CashPeriod,period_id)
    if not p or not p.closed_at:raise HTTPException(404,'Corte no encontrado')
    payments=db.scalars(select(Payment).where(Payment.period_id==p.id)).all()
    totals={method:sum(x.cents for x in payments if x.method==method) for method in ('cash','card','transfer')}
    moves=db.scalars(select(CashMovement).where(CashMovement.period_id==p.id,CashMovement.status=='approved')).all()
    net=sum((1 if x.kind=='in' else -1)*x.cents for x in moves)
    expected=p.opening_cents+totals['cash']+net
    buf=io.BytesIO();doc=SimpleDocTemplate(buf,pagesize=(420,620),leftMargin=25,rightMargin=25,topMargin=25,bottomMargin=25)
    style=getSampleStyleSheet();story=[Paragraph('<b>EL MITOTE RESTAURANTE</b>',style['Heading2']),Paragraph('Corte general de caja · Folio #'+str(p.id),style['Heading3'])]
    entries=[('Inicio',p.opened_at.strftime('%d/%m/%Y %H:%M')),('Cierre',p.closed_at.strftime('%d/%m/%Y %H:%M')),('Responsable',db.get(User,p.closed_by).display if p.closed_by else '?'),('Fondo inicial','$'+amount(p.opening_cents)),('Ventas cobradas en efectivo','$'+amount(totals['cash'])),('Ventas cobradas con tarjeta','$'+amount(totals['card'])),('Ventas cobradas por transferencia','$'+amount(totals['transfer'])),('Entradas y salidas netas','$'+amount(net)),('Efectivo esperado','$'+amount(expected)),('Efectivo contado','$'+amount(p.counted_cents)),('Diferencia','$'+amount(p.counted_cents-expected))]
    table=Table(entries,colWidths=[220,150]);table.setStyle(TableStyle([('LINEBELOW',(0,0),(-1,-1),.4,colors.lightgrey),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story.append(table);story.append(Spacer(1,14));story.append(Paragraph('Observaciones: '+(p.note or 'Sin observaciones'),style['Normal']))
    story.append(Spacer(1,10));story.append(Paragraph('Movimientos autorizados: '+str(len(moves)),style['Normal']))
    story.append(Paragraph('Documento de control interno · No fiscal',style['Normal']))
    doc.build(story)
    return Response(content=buf.getvalue(),media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="ElMitote_Corte_{p.id}.pdf"','Cache-Control':'no-store'})
