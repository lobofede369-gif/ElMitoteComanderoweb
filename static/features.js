/* El Mitote · módulos de preparación, reservaciones y analítica.
   Reutiliza la sesión y el sistema de navegación del cliente existente. */
function prepDuration(seconds){const value=Math.max(0,Math.floor(Number(seconds)||0));return Math.floor(value/60)+'m '+String(value%60).padStart(2,'0')+'s'}
function prepBadge(status){return '<span class="prep-flag prep-'+esc(status)+'">'+({ready:'Listo',warning:'Por vencer',late:'Retrasado',ok:'En tiempo'}[status]||'En tiempo')+'</span>'}
function clockNode(start,ready,limit){return '<span class="prep-clock" data-prep-start="'+esc(start||'')+'" data-prep-stop="'+esc(ready||'')+'" data-prep-limit="'+Number(limit||15)+'">—</span>'}
let prepOffset=0;
function tickPrepClocks(){
  const clocks=document.querySelectorAll('.prep-clock');
  if(!clocks.length)return;
  const instant=Date.now()+prepOffset;
  clocks.forEach(el=>{
    const start=Date.parse(el.dataset.prepStart||''),stop=Date.parse(el.dataset.prepStop||'');
    if(!Number.isFinite(start))return;
    const seconds=Math.max(0,Math.floor(((Number.isFinite(stop)?stop:instant)-start)/1000));
    el.textContent=prepDuration(seconds);
    const limit=Math.max(1,Number(el.dataset.prepLimit)||15);
    el.classList.toggle('prep-clock-late',!Number.isFinite(stop)&&seconds>limit*60);
  });
}
let prepClockTimer=null;
function startPrepClock(){
  if(document.hidden)return;
  if(prepClockTimer===null)prepClockTimer=setInterval(tickPrepClocks,1000);
  tickPrepClocks();
}
function stopPrepClock(){
  if(prepClockTimer!==null){clearInterval(prepClockTimer);prepClockTimer=null;}
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden)stopPrepClock();
  else if(['station','history','tickets'].includes(state.screen))startPrepClock();
});

async function showPrepStation(history=false,all=false){
  const response=await api('/preparation/tickets');
  const tickets=response.tickets||[];
  prepOffset=Date.parse(response.server_time)-Date.now();
  state.data.tickets=tickets;
  const shown=tickets.filter(t=>history?t.status==='ready':t.status!=='ready');
  const newCount=tickets.filter(t=>t.status==='new'&&(all||t.station===state.user.role)).length;
  const cards=shown.map(t=>{
    const products=t.items.map(i=>'<div class="prep-product"><div class="row"><b>'+i.qty+' × '+esc(i.name)+(i.variant_name?' · '+esc(i.variant_name):'')+'</b>'+prepBadge(i.timing)+'</div>'+
      (i.note?'<div class="mini">Nota: '+esc(i.note)+'</div>':'')+
      (i.modifiers?.length?'<div class="mini">Extras: '+i.modifiers.map(m=>esc(m.name)).join(', ')+'</div>':'')+
      '<div class="row mini"><span>Producto: '+clockNode(t.created_at,i.ready_at,i.prep_minutes)+'</span><span>Límite: '+Number(i.prep_minutes)+' min</span></div>'+
      (!history&&t.status==='preparing'&&!i.ready?'<button class="outline small" data-action="prep-item-ready" data-id="'+Number(i.id)+'">✓ Producto listo</button>':'')+'</div>').join('');
    return '<div class="card prep-ticket"><div class="row"><h3>'+esc(t.account)+' · #'+t.account_id+'</h3>'+pill(t.status)+'</div>'+
      '<div class="mini">'+(t.station==='kitchen'?'🍳 Cocina':'🥤 Barra')+' · '+esc(t.waiter)+' · Envío #'+t.batch_id+'</div>'+
      '<div class="prep-total"><div class="row"><b>Tiempo de la comanda</b>'+prepBadge(t.batch_timing)+'</div>'+
      '<div class="row"><span>'+clockNode(t.created_at,t.batch_ready?new Date(Date.parse(t.created_at)+t.batch_elapsed_seconds*1000).toISOString():null,t.batch_minutes)+'</span><span>Límite '+t.batch_minutes+' min</span></div></div>'+
      products+(!history?'<div class="prep-actions"><button class="primary" data-action="ticket-status" data-id="'+t.id+'" data-status="'+(t.status==='new'?'received':t.status==='received'?'preparing':'ready')+'">'+
      (t.status==='new'?'Confirmar recepción':t.status==='received'?'Iniciar preparación':'Marcar toda la estación como lista')+'</button></div>':'')+'</div>';
  }).join('');
  const html='<div class="statgrid">'+stat('Comandas nuevas',newCount)+stat('En proceso',shown.filter(t=>t.status==='received'||t.status==='preparing').length)+
    stat('Con demora',shown.filter(t=>t.batch_timing==='late').length)+'</div>'+
    (!history?'<div class="note">Cronómetros simultáneos por producto y por envío. Las nuevas adiciones a la cuenta comienzan un nuevo envío.</div><p><button class="outline" data-action="enable-sound">🔔 Activar / probar sonido</button></p>':'')+
    '<div class="cards">'+(cards||'<div class="empty">No hay comandas en esta sección.</div>')+'</div>';
  requestAnimationFrame(startPrepClock);
  return html;
}

async function showPrepConfig(){
  const data=await api('/preparation/config');
  return '<div class="card"><h3>Tiempo general de cada envío</h3><form id="prep-policy" class="formrow"><div class="formgrid">'+
    '<label>Límite de comanda (minutos)<input name="batch_minutes" type="number" min="1" max="240" value="'+data.batch_minutes+'" required></label>'+
    '<label>Advertencia al (%)<input name="warning_percent" type="number" min="50" max="95" value="'+data.warning_percent+'" required></label></div>'+
    '<p class="mini">Los nuevos envíos guardan el límite al enviarse; los anteriores conservan el suyo.</p><button class="primary">Guardar límites</button></form></div>'+
    '<h2 class="section">Tiempos por producto</h2><div class="cards">'+data.products.map(p=>
    '<div class="card"><form class="formrow" id="prep-product-'+p.id+'"><div class="row"><b>'+esc(p.name)+'</b><span class="mini">'+
    (p.station==='kitchen'?'Cocina':'Barra')+'</span></div><div class="formgrid"><label>Tiempo esperado (min)<input type="number" min="1" max="240" name="minutes" value="'+p.minutes+'" required></label>'+
    '<button class="outline" type="submit">Guardar</button></div></form></div>').join('')+'</div>';
}

function reservaEstado(s){return ({pending:'Pendiente',confirmed:'Confirmada',arrived:'Cliente presente',served:'Atendida',cancelled:'Cancelada',no_show:'No presentada'})[s]||s}
async function showReservations(){
  const [rs,ts,us]=await Promise.all([api('/reservations'),api('/tables'),api('/users')]);
  const waiters=us.filter(u=>u.role==='waiter'&&u.active);
  const available=ts.filter(t=>t.active&&t.status==='available');
  return '<div class="card"><h3>Crear reservación manual</h3><form id="reservation-add" class="formrow">'+
    '<div class="formgrid"><label>Nombre del cliente<input name="name" maxlength="120" required></label><label>Teléfono (opcional)<input name="phone" maxlength="40"></label>'+
    '<label>Personas<input name="party_size" type="number" min="1" max="30" value="2" required></label><label>Fecha y hora<input name="scheduled_at" type="datetime-local" required></label></div>'+
    '<label>Notas<input name="note" maxlength="400"></label><button class="primary">Registrar reservación</button></form></div>'+
    '<h2 class="section">Reservaciones recientes</h2><div class="cards">'+rs.map(r=>'<div class="card"><div class="row"><h3>'+esc(r.name)+'</h3><span class="prep-flag">'+reservaEstado(r.status)+'</span></div>'+
    '<div class="mini">'+new Date(r.scheduled_at).toLocaleString('es-MX',{timeZone:'America/Mexico_City',dateStyle:'medium',timeStyle:'short'})+
    ' · '+r.party_size+' personas · '+esc(r.source==='website'?'Página web':'Manual')+'</div>'+
    (r.phone?'<p class="mini">'+esc(r.phone)+'</p>':'')+(r.note?'<p>'+esc(r.note)+'</p>':'')+
    (r.account_id?'<p class="mini">Cuenta abierta: #'+r.account_id+'</p>':'')+
    (['pending','confirmed'].includes(r.status)?'<div class="prep-actions">'+
       (r.status==='pending'?'<button class="outline small" data-action="reservation-confirm" data-id="'+r.id+'">Confirmar</button>':'')+
       '<button class="outline small" data-action="reservation-cancel" data-id="'+r.id+'">Cancelar</button>'+
       '<button class="outline small" data-action="reservation-no-show" data-id="'+r.id+'">No asistió</button></div>'+
       '<div class="formgrid"><label>Mesa disponible<select class="reservation-table"><option value="">Seleccionar</option>'+available.filter(t=>t.capacity>=r.party_size).map(t=>'<option value="'+t.id+'">'+esc(t.name)+' · '+t.capacity+' lugares</option>').join('')+'</select></label>'+
       '<label>Mesero<select class="reservation-waiter"><option value="">Seleccionar</option>'+waiters.map(u=>'<option value="'+u.id+'">'+esc(u.display)+'</option>').join('')+'</select></label></div>'+
       '<button class="primary" data-action="reservation-arrive" data-id="'+r.id+'">Registrar llegada y abrir cuenta</button>':'')+
       '</div>').join('')+'</div>';
}

let analyticsDates=null;
let analyticsScope='current';
async function showAnalytics(){
  const today=new Date();
  const localDate=d=>new Intl.DateTimeFormat('en-CA',{timeZone:'America/Mexico_City',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
  if(!analyticsDates){const last=new Date(today.getTime()-6*86400000);analyticsDates={start:localDate(last),end:localDate(today)};}
  const r=await api('/owner/analytics?start='+encodeURIComponent(analyticsDates.start)+'&end='+encodeURIComponent(analyticsDates.end)+'&scope='+analyticsScope);
  const bars=(rows,key)=>{const max=Math.max(1,...rows.map(x=>Number(x.amount)));return '<div class="prep-bars">'+rows.map(x=>
    '<div class="prep-bar-row"><span>'+esc(x[key])+'</span><div class="prep-bar-track"><div style="width:'+
    Math.round(Number(x.amount)/max*100)+'%"></div></div><b>'+money(x.amount)+'</b></div>').join('')+'</div>'};
  const hours=r.sales_by_hour.filter(x=>Number(x.amount)>0);
  return '<div class="card"><div class="prep-actions"><button class="'+(analyticsScope==='current'?'primary':'outline')+' small" data-action="analytics-current">Turno activo</button>'+
    '<button class="'+(analyticsScope==='history'?'primary':'outline')+' small" data-action="analytics-history">Consultar otros cortes</button></div>'+
    (analyticsScope==='history'?'<form id="analytics-period" class="formrow"><div class="formgrid">'+
    '<label>Desde<input type="date" name="start" value="'+analyticsDates.start+'" required></label>'+
    '<label>Hasta<input type="date" name="end" value="'+analyticsDates.end+'" required></label></div><button class="outline">Consultar historial</button></form>':
    '<p class="mini">'+(r.active_period_id?'Solo ventas y comandas del corte activo #'+r.active_period_id:
      'No hay turno de caja abierto. Las ventas anteriores están en el historial de cortes.')+'</p>')+'</div>'+
    '<div class="statgrid">'+stat('Ventas cobradas',money(r.sales))+stat('Cuentas con cobros',r.paid_accounts)+
    stat('Ticket promedio',money(r.average_ticket))+stat('Preparación media',r.prep_avg_minutes===null?'Sin datos':r.prep_avg_minutes+' min')+'</div>'+
    '<h2 class="section">Ventas por día</h2><div class="card">'+bars(r.sales_by_day,'label')+'</div>'+
    '<h2 class="section">Horarios de mayor venta</h2><div class="card">'+(hours.length?bars(hours,'label'):'<div class="empty">Sin ventas en el periodo</div>')+'</div>'+
    '<h2 class="section">Métodos de pago</h2><div class="card">'+
    ['cash','card','transfer'].map(k=>'<div class="row"><span>'+({cash:'Efectivo',card:'Tarjeta',transfer:'Transferencia'})[k]+'</span><b>'+money(r.methods[k])+'</b></div>').join('')+'</div>'+
    '<h2 class="section">Productos más vendidos</h2><div class="card">'+
    (r.products.map(p=>'<div class="row"><span>'+esc(p.name)+'</span><b>'+p.quantity+' uds.</b></div>').join('')||'<p>Sin ventas de productos registradas.</p>')+'</div>'+
    '<h2 class="section">Rotación de mesas</h2><div class="card">'+
    (r.table_rotation.map(x=>'<div class="row"><span>'+esc(x.table)+'</span><b>'+x.accounts_opened+' asignaciones</b></div>').join('')||'<p>Sin asignaciones registradas.</p>')+'</div>'+
    '<div class="note">Las propinas no forman parte de las ventas. Los costos y márgenes se mostrarán cuando existan costos reales registrados. Los productos se agrupan por fecha de comanda y los ingresos por fecha de cobro.</div>';
}

async function mitoteFeatureAction(action,btn){
  const id=Number(btn.dataset.id);
  if(action==='analytics-current'||action==='analytics-history'){
    analyticsScope=action==='analytics-current'?'current':'history';
    await render();return true;
  }
  if(action==='prep-item-ready'){
    await api('/preparation/items/'+id+'/ready','POST',{});toast('Producto marcado listo');await render();return true;
  }
  if(action.startsWith('reservation-')){
    const kind=action.slice('reservation-'.length);
    if(kind==='arrive'){
      const card=btn.closest('.card');
      const table_id=Number(card.querySelector('.reservation-table')?.value);
      const waiter_id=Number(card.querySelector('.reservation-waiter')?.value);
      if(!table_id||!waiter_id)throw Error('Selecciona mesa y mesero');
      if(!confirm('¿Confirmar llegada y abrir la cuenta?'))return true;
      await api('/reservations/'+id+'/arrive','POST',{table_id,waiter_id});
      toast('Cliente recibido y cuenta abierta');
    }else{
      const status={confirm:'confirmed',cancel:'cancelled','no-show':'no_show'}[kind];
      if(!status)throw Error('Acción no disponible');
      if(status!=='confirmed'&&!confirm('¿Confirmar el cambio de estado?'))return true;
      await api('/reservations/'+id+'/status','POST',{status});toast('Reservación actualizada');
    }
    await render();return true;
  }
  return false;
}
async function mitoteFeatureForm(form){
  const d=Object.fromEntries(new FormData(form).entries());
  if(form.id==='prep-policy'){
    await api('/preparation/config','PUT',{batch_minutes:Number(d.batch_minutes),warning_percent:Number(d.warning_percent)});
    toast('Tiempos de preparación actualizados');await render();return true;
  }
  if(form.id.startsWith('prep-product-')){
    const id=Number(form.id.slice('prep-product-'.length));
    await api('/preparation/products/'+id,'PUT',{minutes:Number(d.minutes)});
    toast('Tiempo por producto guardado');await render();return true;
  }
  if(form.id==='reservation-add'){
    const iso=new Date(d.scheduled_at).toISOString();
    await api('/reservations','POST',{name:d.name,phone:d.phone||'',party_size:Number(d.party_size),scheduled_at:iso,note:d.note||''});
    toast('Reservación registrada');await render();return true;
  }
  if(form.id==='analytics-period'){
    analyticsDates={start:d.start,end:d.end};
    analyticsScope='history';
    await render();return true;
  }
  return false;
}
