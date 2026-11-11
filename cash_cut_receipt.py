"""Generación de cortes tipo ticket; los PDFs históricos originales no se alteran."""
import io
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from fastapi import Depends, HTTPException
from fastapi.responses import Response
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import Paragraph, Table, TableStyle, Spacer, HRFlowable, SimpleDocTemplate
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import app, CashPeriod, Batch, OrderItem, Product, db_session, auth, allow, cash_period_detail

WIDTH=80*mm
MARGIN=7*mm
TEXT=WIDTH-2*MARGIN
NORMAL=ParagraphStyle("receipt",fontName="Helvetica",fontSize=8,leading=11)
SMALL=ParagraphStyle("small",parent=NORMAL,fontSize=7,leading=9)
CENTER=ParagraphStyle("center",parent=NORMAL,alignment=TA_CENTER)
TITLE=ParagraphStyle("title",parent=CENTER,fontName="Helvetica-Bold",fontSize=12,leading=16)
RIGHT=ParagraphStyle("right",parent=NORMAL,alignment=TA_RIGHT)
SECTION=ParagraphStyle("section",parent=NORMAL,fontName="Helvetica-Bold",spaceBefore=6,spaceAfter=4)


def text_node(x,style=NORMAL):
    return Paragraph(escape(str(x if x is not None else "")),style)


def currency(x):
    try:return f"$ {float(x or 0):,.2f}"
    except (TypeError,ValueError):return "$ 0.00"


def date_label(s):
    if not s:return "Sin fecha"
    try:
        value=datetime.fromisoformat(str(s).replace("Z","+00:00"))
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("America/Mexico_City")).strftime("%d/%m/%Y %I:%M %p")
    except (TypeError,ValueError):return str(s)[:22]


def section(story,label):
    story.append(Spacer(1,4))
    story.append(HRFlowable(width="100%",thickness=.5,color=colors.grey))
    story.append(text_node(label,SECTION))


def line(story,label,value):
    table=Table([[text_node(label),text_node(value,RIGHT)]],
                colWidths=[TEXT*.65,TEXT*.35],hAlign="LEFT")
    table.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),
        ("RIGHTPADDING",(0,0),(-1,-1),0),
        ("TOPPADDING",(0,0),(-1,-1),2),
        ("BOTTOMPADDING",(0,0),(-1,-1),2),
        ("VALIGN",(0,0),(-1,-1),"TOP")]))
    story.append(table)


def total_tips(d):
    return sum(float(d.get(k) or 0) for k in ("tip_cash","tip_card","tip_transfer"))


def build_cut_ticket_pdf(d):
    story=[text_node("EL MITOTE",TITLE),text_node("RESTAURANTE · CORTE DE CAJA",CENTER),
           text_node("CONTROL INTERNO · NO FISCAL",SMALL),
           Spacer(1,5),text_node("CORTE #"+str(d.get("id","?"))),
           text_node("CAJA: GENERAL"),
           text_node("DESDE: "+date_label(d.get("opened_at"))),
           text_node("HASTA: "+date_label(d.get("closed_at"))),
           text_node("RESPONSABLE: "+str(d.get("closed_by") or "?"))]
    section(story,"MOVIMIENTO DE CAJA")
    for label,key in [("+ Fondo inicial","opening"),("+ Ventas efectivo","cash"),
                      ("+ Propinas efectivo","tip_cash"),
                      ("+/- Entradas / retiros","net_movements"),
                      ("- Propinas pagadas","tip_payout_cash"),
                      ("= Efectivo esperado","expected"),
                      ("Efectivo contado","counted"),
                      ("Diferencia","difference")]:
        line(story,label,currency(d.get(key)))
    section(story,"FORMA DE PAGO · VENTAS")
    for label,key in [("Efectivo","cash"),("Tarjeta","card"),
                      ("Transferencia","transfer"),("TOTAL VENTAS","sales")]:
        line(story,label,currency(d.get(key)))
    section(story,"PROPINA · SEPARADA DE VENTAS")
    for label,key in [("Efectivo","tip_cash"),("Tarjeta","tip_card"),
                      ("Transferencia","tip_transfer")]:
        line(story,label,currency(d.get(key)))
    line(story,"TOTAL PROPINA",currency(total_tips(d)))
    distribution=d.get("tip_distribution")
    if isinstance(distribution,dict):
        line(story,"Propinas entregadas",currency(distribution.get("paid_total")))
        line(story,"Propinas pendientes",currency(distribution.get("pending_total")))
    section(story,"OPERACIÓN DEL CORTE")
    line(story,"Cuentas pagadas",str(d.get("paid_accounts",0)))
    line(story,"Cuentas canceladas",str(d.get("cancelled_accounts",0)))
    if d.get("cashier_totals"):
        section(story,"POR CAJERO")
        for label,value in sorted(d["cashier_totals"].items()):
            line(story,label,currency(value))
    section(story,"POR TIPO DE PRODUCTO")
    breakdown=d.get("product_breakdown") or []
    if breakdown:
        for row in breakdown:
            line(story,str(row["name"])+" ("+str(row["units"])+" pz)",currency(row["gross_amount"]))
        story.append(text_node("Importes brutos de comandas; pueden diferir de los cobros por descuentos.",SMALL))
    else:
        story.append(text_node("Sin desglose histórico archivado para este corte.",SMALL))
    story.append(text_node("Tipo de servicio: no registrado por el sistema.",SMALL))
    if d.get("movements"):
        section(story,"MOVIMIENTOS AUTORIZADOS")
        for row in d["movements"]:
            if row.get("status")=="approved":
                line(story,("Entrada" if row.get("kind")=="in" else "Retiro")+
                     " · "+str(row.get("reason") or "")[:30],currency(row.get("amount")))
    if d.get("note"):
        section(story,"OBSERVACIONES")
        story.append(text_node(d["note"],SMALL))
    story.append(Spacer(1,7))
    story.append(text_node("Corte #"+str(d.get("id","?"))+" · EL MITOTE",CENTER))
    story.append(text_node("MXN · Propinas fuera de ingresos",SMALL))
    height=max(180*mm,min(900*mm,(len(story)*4+120)*mm))
    buffer=io.BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=(WIDTH,height),topMargin=MARGIN,bottomMargin=MARGIN,
        leftMargin=MARGIN,rightMargin=MARGIN,title="Corte de caja El Mitote")
    doc.build(story)
    data=buffer.getvalue()
    if not data.startswith(b"%PDF-"):raise RuntimeError("PDF inválido")
    return data


def cut_product_breakdown(db,period):
    """Desglose bruto capturado al cierre, por productos enviados en ese turno."""
    items=db.execute(select(OrderItem,Product.category).join(
        Batch,Batch.id==OrderItem.batch_id).outerjoin(
        Product,Product.id==OrderItem.product_id).where(
        Batch.created_at>=period.opened_at,Batch.created_at<=period.closed_at)).all()
    totals=defaultdict(lambda:[0,0])
    for item,category in items:
        qty=max(0,(item.qty or 0)-(item.cancelled_qty or 0))
        if qty:
            totals[category or "Sin categoría"][0]+=qty
            totals[category or "Sin categoría"][1]+=qty*item.price_cents
    return [{"name":k,"units":v[0],"gross_amount":f"{v[1]/100:.2f}"}
            for k,v in sorted(totals.items())]


@app.get("/api/cash/periods/{period_id}/ticket.pdf")
def cash_ticket_pdf(period_id:int,db:Session=Depends(db_session),user=Depends(auth)):
    allow(user,"owner","admin","cashier")
    period=db.get(CashPeriod,period_id)
    if not period or period.closed_at is None:raise HTTPException(404,"Corte no encontrado")
    snapshot=cash_period_detail(period_id,db,user)
    pdf=build_cut_ticket_pdf(snapshot)
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":f'attachment; filename="ElMitote_CorteTicket_{period_id}.pdf"',
                 "Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
