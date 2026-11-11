'use strict';
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const savedToken=localStorage.getItem('mitote-token')||sessionStorage.getItem('mitote-token')||'';
if(savedToken)localStorage.setItem('mitote-token',savedToken);
const state={token:savedToken,user:null,screen:'',account:null,draft:[],products:[],data:{},apiInflight:new Map(),lastRefresh:0,alarmTimer:null,audio:null,alarmArmed:false,navStack:[],renderSeq:0,pendingRefresh:false,waiterReadyInitialized:false,waiterReadySeen:new Set(),waiterReadyBusy:false,waiterReadyPending:false};
const workKey=()=>state.user?`mitote-work-${state.user.id}`:'';
function persistWorkState(){if(!state.user)return;try{localStorage.setItem(workKey(),JSON.stringify({account:state.account,draft:state.draft}));}catch{}}
function restoreWorkState(){if(!state.user)return;try{const saved=JSON.parse(localStorage.getItem(workKey())||'{}');state.account=Number(saved.account)||null;state.draft=Array.isArray(saved.draft)?saved.draft.filter(x=>x&&Number.isInteger(Number(x.product_id))&&Number(x.qty)>0).map(x=>({product_id:Number(x.product_id),name:String(x.name||''),station:x.station==='bar'?'bar':'kitchen',qty:Math.min(100,Number(x.qty)),note:String(x.note||'').slice(0,300),variant_id:x.variant_id?Number(x.variant_id):null,variant_name:String(x.variant_name||''),modifier_option_ids:Array.isArray(x.modifier_option_ids)?x.modifier_option_ids.map(Number):[],modifier_names:Array.isArray(x.modifier_names)?x.modifier_names.map(String):[],prep_minutes:Math.max(0,Math.min(240,Number(x.prep_minutes??15))),release_after_minutes:Math.max(0,Math.min(240,Number(x.release_after_minutes??0))),unit_price:Number(x.unit_price)||0,promotion_name:String(x.promotion_name||'')})):[];}catch{state.account=null;state.draft=[];}}
const sections={waiter:[['accounts','▤','Mis cuentas'],['new','＋','Nueva cuenta'],['orders','♨','Comandas'],['notifications','🔔','Avisos']],kitchen:[['station','♨','Cocina'],['history','▤','Historial'],['notifications','🔔','Avisos']],bar:[['station','◉','Barra'],['history','▤','Historial'],['notifications','🔔','Avisos']],cashier:[['cashier','＄','Cobros'],['cash','▣','Caja']],admin:[['overview','▦','Resumen'],['all','▤','Cuentas'],['tables','▦','Mesas'],['approvals','✓','Autorizaciones'],['tickets','♨','Estaciones'],['cashier','＄','Cobros'],['cash','▣','Caja'],['tips','♧','Propinas'],['products','☷','Menú'],['inventory','▣','Inventario'],['users','♙','Personal'],['audit','≡','Auditoría'],['reservations','▣','Reservaciones'],['prepconfig','◷','Tiempos']],owner:[['overview','▦','Resumen'],['all','▤','Cuentas'],['tables','▦','Mesas'],['approvals','✓','Autorizaciones'],['tickets','♨','Estaciones'],['cashier','＄','Cobros'],['cash','▣','Caja'],['tips','♧','Propinas'],['products','☷','Menú'],['inventory','▣','Inventario'],['users','♙','Personal'],['audit','≡','Auditoría'],['reservations','▣','Reservaciones'],['prepconfig','◷','Tiempos'],['analytics','▥','Estadísticas']]};
const labels={owner:'Propietario',admin:'Administrador',cashier:'Caja',waiter:'Mesero',kitchen:'Cocina',bar:'Barra'};
const statuses={open:'Abierta',pending:'Pendiente de cobro',paid:'Pagada',new:'Nueva',cancelled:'Anulada',merged:'Unida a otra cuenta',received:'Recibida',preparing:'Preparando',ready:'Lista',approved:'Aprobada',rejected:'Rechazada',occupied:'Ocupada',available:'Disponible',inactive:'Inactiva'};
const money=n=>Number(n||0).toLocaleString('es-MX',{style:'currency',currency:'MXN'});
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pill=s=>`<span class="pill ${esc(s)}">${esc(statuses[s]||s)}</span>`;
const key=()=>crypto.randomUUID();
// Reutilizar clave al reintentar una operación cuya respuesta pudo perderse.
function pendingOperationKey(scope,payload){
  const name='mitote-pending-'+state.user.id+'-'+scope;
  const signature=JSON.stringify(payload);
  try{
    const saved=JSON.parse(localStorage.getItem(name)||'null');
    if(saved?.signature===signature&&typeof saved.key==='string')return saved.key;
  }catch{}
  const id=key();
  try{localStorage.setItem(name,JSON.stringify({key:id,signature}));}catch{}
  return id;
}
function clearPendingOperation(scope){
  if(!state.user)return;
  try{localStorage.removeItem('mitote-pending-'+state.user.id+'-'+scope);}catch{}
}
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.style.borderColor=error?'#ff5777':'#ffd400';el.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),4500)}
function setConnectionState(connected){
  const el=$('#connection');
  if(!el)return;
  el.textContent=connected?'● Conectado':'● Sin conexión';
  el.classList.toggle('off',!connected);
}
let refreshPromise=null;
function tokenExpiresSoon(token){try{const raw=token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/');const part=raw+'='.repeat((4-raw.length%4)%4);const payload=JSON.parse(atob(part));return Number(payload.exp)*1000-Date.now()<45*60*1000}catch{return false}}
async function refreshSessionIfNeeded(){
  if(!state.token||!tokenExpiresSoon(state.token))return;
  if(refreshPromise)return refreshPromise;
  const current=state.token;
  refreshPromise=(async()=>{
    const r=await fetch('/api/auth/refresh',{method:'POST',headers:{Authorization:'Bearer '+current},cache:'no-store'});
    let data={};try{data=await r.json()}catch{}
    if(r.status===401){logout('expired');const error=new Error('Tu sesión venció o fue revocada. Vuelve a iniciar sesión.');error.authExpired=true;throw error}
    if(!r.ok)return;
    if(data.token&&state.token===current){state.token=data.token;localStorage.setItem('mitote-token',data.token);if(window.MitoteAndroid){try{window.MitoteAndroid.syncSession(data.token,state.user?.role||'')}catch{}}}
  })();
  try{await refreshPromise}catch(e){if(e?.authExpired)throw e;/* Si el servidor falla temporalmente, intenta la operación con el token actual. */}finally{refreshPromise=null}
}
async function api(path,method='GET',body){
  const verb=String(method||'GET').toUpperCase();
  if(path!=='/auth/refresh')await refreshSessionIfNeeded();
  if(!navigator.onLine&&verb!=='GET')throw Error('Sin conexión: operación bloqueada para evitar duplicados');

  const requestKey=String(state.user?.id||'session')+':'+path;
  if(verb==='GET'){
    const pending=state.apiInflight.get(requestKey);
    if(pending)return pending;
  }

  const request=(async()=>{
    let response;
    try{
      response=await fetch('/api'+path,{
        method:verb,
        headers:{'Content-Type':'application/json',Authorization:'Bearer '+state.token},
        body:body===undefined?undefined:JSON.stringify(body),
        cache:'no-store'
      });
    }catch(error){
      setConnectionState(false);
      throw Error(verb==='GET'
        ?'No se pudo conectar con el servidor. Revisa Internet e inténtalo de nuevo.'
        :'No se confirmó la operación. Verifica la cuenta antes de repetir para evitar duplicados.');
    }
    let data={};try{data=await response.json()}catch{}
    if(response.status===401){
      logout('expired');
      throw Error('Tu sesión venció o fue revocada. Vuelve a iniciar sesión.');
    }
    setConnectionState(true);
    if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Error al procesar la operación');
    return data;
  })();

  if(verb!=='GET')return request;
  state.apiInflight.set(requestKey,request);
  try{
    return await request;
  }finally{
    if(state.apiInflight.get(requestKey)===request)state.apiInflight.delete(requestKey);
  }
}
async function fetchProtectedPdf(url){
  await refreshSessionIfNeeded();
  if(!navigator.onLine)throw Error('Se necesita internet para descargar cortes.');
  const response=await fetch(url,{headers:{Authorization:'Bearer '+state.token,Accept:'application/pdf'},cache:'no-store'});
  if(response.status===401){logout('expired');throw Error('La sesión expiró. Inicia sesión para descargar el corte.')}
  if(!response.ok){
    let error='Error HTTP '+response.status;
    try{const json=await response.json();if(typeof json.detail==='string')error=json.detail}catch{}
    throw Error('No se pudo obtener el PDF: '+error);
  }
  const blob=await response.blob();
  const type=response.headers.get('content-type')||'';
  const prefix=await blob.slice(0,5).text();
  if(!type.includes('pdf')||prefix!=='%PDF-'||blob.size<100)throw Error('El servidor no devolvió un PDF válido.');
  return blob;
}
function savePdfToDevice(blob,filename){
  // En Chrome Android y PWA el enlace de descarga debe estar unido al DOM.
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;link.download=filename;link.style.display='none';
  document.body.appendChild(link);
  link.click();
  setTimeout(()=>{link.remove();URL.revokeObjectURL(url)},120000);
}
function cutMoney(d,key){return money(Number(d[key]||0))}
async function showCashTicket(id){
  const p=await api('/cash/periods/'+Number(id));
  const target=$('#period-detail');
  if(!target)return;
  const row=(name,value)=>'<div class="cash-receipt-row"><span>'+esc(name)+'</span><b>'+esc(value)+'</b></div>';
  const title=name=>'<h4>'+esc(name)+'</h4>';
  const time=s=>s?new Date(s).toLocaleString('es-MX',{timeZone:'America/Mexico_City'}):'No registrado';
  const tips=['tip_cash','tip_card','tip_transfer'].reduce((sum,k)=>sum+Number(p[k]||0),0);
  const groups=p.product_breakdown||[];
  let html='<section class="cash-receipt"><h3>EL MITOTE</h3><p class="cash-receipt-center">RESTAURANTE · CORTE DE CAJA</p>'+
    '<p class="cash-receipt-center">CORTE #'+Number(p.id)+' · CAJA GENERAL</p>'+
    '<p>Inicio: '+esc(time(p.opened_at))+'</p><p>Cierre: '+esc(time(p.closed_at))+'</p>'+
    '<p>Responsable: '+esc(p.closed_by||'No registrado')+'</p>'+
    title('MOVIMIENTO DE CAJA')+
    row('+ Fondo inicial',cutMoney(p,'opening'))+row('+ Ventas efectivo',cutMoney(p,'cash'))+
    row('+ Propinas efectivo',cutMoney(p,'tip_cash'))+row('+/- Entradas / retiros',cutMoney(p,'net_movements'))+
    row('- Propinas pagadas',cutMoney(p,'tip_payout_cash'))+
    row('= Efectivo esperado',cutMoney(p,'expected'))+row('Efectivo contado',cutMoney(p,'counted'))+
    row('DIFERENCIA',cutMoney(p,'difference'))+title('FORMA DE PAGO · VENTAS')+
    row('Efectivo',cutMoney(p,'cash'))+row('Tarjeta',cutMoney(p,'card'))+
    row('Transferencia',cutMoney(p,'transfer'))+row('TOTAL VENTAS',cutMoney(p,'sales'))+
    title('PROPINA · SEPARADA DE VENTAS')+
    row('Efectivo',cutMoney(p,'tip_cash'))+row('Tarjeta',cutMoney(p,'tip_card'))+
    row('Transferencia',cutMoney(p,'tip_transfer'))+row('TOTAL PROPINA',money(tips))+
    title('OPERACIÓN DEL TURNO')+row('Cuentas pagadas',String(p.paid_accounts||0))+
    row('Cuentas canceladas',String(p.cancelled_accounts||0));
  if(p.cashier_totals&&Object.keys(p.cashier_totals).length){
    html+=title('POR CAJERO')+Object.entries(p.cashier_totals).map(([k,v])=>row(k,money(v))).join('');
  }
  html+=title('POR TIPO DE PRODUCTO')+
    (groups.length?groups.map(g=>row(g.name+' ('+g.units+' pz)',money(g.gross_amount))).join('')+
      '<p class="cash-receipt-note">Importes brutos de comandas: pueden diferir de los cobros por descuentos.</p>':
      '<p class="cash-receipt-note">Este corte no conserva desglose histórico por categoría.</p>')+
    '<p class="cash-receipt-note">Tipo de servicio: no registrado históricamente.</p>';
  if(p.note)html+=title('OBSERVACIONES')+'<p>'+esc(p.note)+'</p>';
  html+='</section><div class="cash-receipt-actions">'+
    '<button class="primary" data-action="download-report" data-id="'+Number(id)+'">Descargar ticket PDF</button>'+
    '<button class="outline" data-action="download-report-original" data-id="'+Number(id)+'">PDF original</button>'+
    '<button class="outline" data-action="hide-period-detail">Cerrar vista</button></div>';
  target.innerHTML=html;
  target.scrollIntoView({behavior:'smooth',block:'start'});
}
function logout(reason='manual'){
  const oldToken=state.token;
  // Solo se elimina la suscripción push cuando el usuario cierra sesión voluntariamente.
  // Una expiración de token no debe desactivar las notificaciones del dispositivo.
  if(reason==='manual'&&oldToken&&'serviceWorker' in navigator){navigator.serviceWorker.ready.then(async reg=>{
    const sub=await reg.pushManager.getSubscription();
    if(sub){try{await fetch('/api/push/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+oldToken},body:JSON.stringify({endpoint:sub.endpoint}),keepalive:true})}catch{}
      try{await sub.unsubscribe()}catch{}}
  }).catch(()=>{});}
  if(reason==='manual'&&window.MitoteAndroid){try{window.MitoteAndroid.clearSession()}catch(e){}}
  if(reason==='manual'&&state.user){try{localStorage.removeItem(workKey())}catch{}}
  if(typeof stopPrepClock==='function')stopPrepClock();
  state.apiInflight.clear();
  state.token='';state.user=null;state.screen='';state.navStack=[];state.waiterReadyInitialized=false;state.waiterReadySeen=new Set();
  localStorage.removeItem('mitote-token');sessionStorage.removeItem('mitote-token');localStorage.removeItem('mitote-screen');
  $('#app').hidden=true;$('#login').hidden=false;stopAlarm();
}
function refreshNav(){const list=sections[state.user.role]||[];$('#nav').innerHTML=list.map(([id,icon,title])=>`<button class="navbtn ${state.screen===id?'active':''}" data-nav="${id}"><span style="font-size:1.15rem">${icon}</span>${title}</button>`).join('');$('#role-name').textContent=`${state.user.display} · ${labels[state.user.role]}`;$('#page-title').textContent=(list.find(x=>x[0]===state.screen)||list[0]||[])[2]||'Inicio';const back=$('#back');if(back)back.hidden=state.navStack.length===0;}
async function start(authenticatedUser=null){try{
    const validUser=authenticatedUser&&Number.isSafeInteger(Number(authenticatedUser.id))&&
      typeof authenticatedUser.display==='string'&&sections[authenticatedUser.role]
      ?authenticatedUser:null;
    state.user=validUser||await api('/me');
    if(window.MitoteAndroid){try{window.MitoteAndroid.syncSession(state.token,state.user.role)}catch(e){}}
    restoreWorkState();
    $('#login').hidden=true;$('#app').hidden=false;
    const pushScreen=new URLSearchParams(location.search).get('push');
    const allowed=sections[state.user.role]||[];
    const savedScreen=localStorage.getItem('mitote-screen');
    state.screen=(pushScreen==='station'&&['kitchen','bar'].includes(state.user.role))?'station':
      (pushScreen==='accounts'&&state.user.role==='waiter')?'accounts':
      (allowed.some(x=>x[0]===savedScreen)?savedScreen:allowed[0][0]);
    state.navStack=[];
    history.replaceState({mitoteScreen:state.screen,mitoteStack:[]},'',location.pathname+location.search+'#'+state.screen);
    localStorage.setItem('mitote-screen',state.screen);
    refreshNav();
    // Limpiar cualquier contenido de la sesión anterior y pintar el esqueleto sin bloquear.
    $('#content').innerHTML='';
    const initialRender=render({preserveScroll:false});
    // La conexión de eventos se inicia mientras se consultan los primeros datos.
    listenEvents();
    await initialRender;
    if(state.user){
      if(state.user.role==='waiter')syncWaiterReadyAlerts(true).catch(()=>{});
      syncPushOnLogin().catch(()=>{});
    }
  }catch(e){logout('expired');toast(e.message,true)}}
async function go(screen,options={}){
  if(!state.user)return;
  const allowed=(sections[state.user.role]||[]).some(x=>x[0]===screen);
  if(!allowed||screen===state.screen)return;
  const stack=options.replace?[...state.navStack]:[...state.navStack,state.screen];
  if(options.replace&&stack[stack.length-1]===screen)stack.pop();
  state.navStack=stack;state.screen=screen;localStorage.setItem('mitote-screen',screen);persistWorkState();
  const payload={mitoteScreen:screen,mitoteStack:stack};
  const url=location.pathname+location.search+'#'+screen;
  if(options.replace)history.replaceState(payload,'',url);else history.pushState(payload,'',url);
  refreshNav();await render({preserveScroll:false});
}
function goBack(){if(state.navStack.length)history.back();}
window.addEventListener('popstate',event=>{
  if(!state.user)return;
  const screen=event.state?.mitoteScreen;
  const allowed=(sections[state.user.role]||[]).some(x=>x[0]===screen);
  if(allowed){state.screen=screen;state.navStack=Array.isArray(event.state.mitoteStack)?event.state.mitoteStack:[];localStorage.setItem('mitote-screen',screen);refreshNav();void render({preserveScroll:false});}
});
function stat(label,val){return `<div class="stat"><b>${esc(val)}</b><span>${esc(label)}</span></div>`}
function accountCard(a,action='open-account'){return `<article class="card account-card account-card-${esc(a.status)}"><div class="account-card-head"><div class="account-card-title"><h3>${esc(a.name)}</h3><div class="account-card-meta">Folio #${a.id} · ${esc(a.owner)}${a.table_name?` · ${esc(a.table_name)}`:''}</div></div>${pill(a.status)}</div><div class="account-card-values"><div><span>Total</span><strong>${money(a.total)}</strong></div><div class="account-card-balance"><span>Saldo pendiente</span><strong>${money(a.due)}</strong></div></div><div class="account-card-footer"><span class="account-card-reference">${a.table_name?'Mesa asignada':'Cuenta libre'}</span><button class="outline small" data-action="${action}" data-id="${a.id}">Abrir cuenta</button></div></article>`}
function loadingScreenHtml(screen){
  const labels={accounts:'Mis cuentas',all:'Cuentas',tables:'Mesas',new:'Nueva cuenta',
    orders:'Comandas',station:'Cocina / barra',history:'Historial de preparación',
    notifications:'Avisos',tickets:'Estaciones',cashier:'Cobros',cash:'Caja',
    tips:'Propinas',products:'Menú',inventory:'Inventario',users:'Personal',
    approvals:'Autorizaciones',audit:'Auditoría',reservations:'Reservaciones',
    prepconfig:'Tiempos de preparación',analytics:'Estadísticas',overview:'Resumen'};
  const label=labels[screen]||'la aplicación';
  return `<div class="loading-screen" role="status" aria-live="polite">
    <div class="loading-screen-heading"><span class="loading-screen-indicator"></span>
      <div><strong>Abriendo ${esc(label)}</strong><p>Conectando y preparando tus datos…</p></div>
    </div>
    <div class="loading-screen-grid"><div class="loading-skeleton"></div>
      <div class="loading-skeleton"></div><div class="loading-skeleton"></div></div>
    <div class="loading-skeleton loading-screen-wide"></div>
  </div>`;
}
async function render(options={}){
  if(!state.user)return;
  const area=$('#content');
  const screen=state.screen;
  if(!['station','history','tickets'].includes(screen)&&typeof stopPrepClock==='function')stopPrepClock();
  const seq=++state.renderSeq;
  // Las lecturas de una pantalla anterior no deben bloquear ni contaminar la nueva.
  state.apiInflight.clear();
  const preserveScroll=options.preserveScroll!==false;
  const scrollY=window.scrollY||0;
  persistWorkState();
  // En el primer acceso se pinta una estructura ligera antes de esperar a la API.
  if(!area.childElementCount)area.innerHTML=loadingScreenHtml(screen);
  area.setAttribute('aria-busy','true');
  try{
    let html='';
    switch(screen){case 'accounts':html=await showAccounts(false);break;case 'all':html=await showAccounts(true);break;case 'tables':html=await showTables();break;case 'new':html=await showNew();break;case 'orders':html=await showOrders();break;case 'station':html=await showPrepStation(false);break;case 'history':html=await showPrepStation(true);break;case 'notifications':html=await showNotifications();break;case 'tickets':html=await showPrepStation(false,true);break;case 'cashier':html=await showCashier();break;case 'cash':html=await showCash();break;case 'tips':html=await showTips();break;case 'products':html=await showProducts();break;case 'inventory':html=await showInventory();break;case 'users':html=await showUsers();break;case 'approvals':html=await showApprovals();break;case 'audit':html=await showAudit();break;case 'reservations':html=await showReservations();break;case 'prepconfig':html=await showPrepConfig();break;case 'analytics':html=await showAnalytics();break;case 'overview':html=await showOverview();break;default:html='<div class="empty">Sin pantalla</div>'}
    if(seq!==state.renderSeq||screen!==state.screen)return;
    area.innerHTML=html;area.removeAttribute('aria-busy');
    if(preserveScroll)window.scrollTo(0,scrollY);else window.scrollTo(0,0);
    $('#refresh')?.classList.remove('has-update');
    state.pendingRefresh=false;
    checkAlarm();
    // El historial es secundario: se consulta después de mostrar el panel de caja.
    if(screen==='cash')loadCashHistory(seq).catch(()=>{});
  }catch(e){
    if(seq!==state.renderSeq||screen!==state.screen)return;
    area.removeAttribute('aria-busy');
    area.innerHTML=`<div class="note danger-note">${esc(e.message)}</div>`;toast(e.message,true);
  }
}
function waiterReadyAlertsHtml(){
  const ready=state.data.readyAlerts||[];
  return '<div class="card waiter-ready-card"><div class="row"><h3>🔔 Pedidos pendientes de entregar</h3><button class="outline small" data-action="enable-sound">♪ Probar sonido</button></div>'+
    (ready.length?ready.slice(0,14).map(x=>{
      const status=x.acknowledged?'Recibido · pendiente de entrega':'Pendiente de confirmar recepción';
      const action=x.acknowledged?'deliver-ready':'ack-ready';
      const label=x.acknowledged?'✓ Entregado':'✓ Recibido';
      return '<div class="lineitem row"><div><b>'+
        esc(x.complete?'✅ Pedido completo':'✓ '+x.station_name+' lista')+
        '</b><div class="mini">'+esc(x.account)+' · Comanda #'+Number(x.batch_id)+
        (x.complete?' · Todas las estaciones listas':'')+
        '</div><div class="mini">'+status+'</div></div><div class="row"><button class="outline small" data-action="open-account" data-id="'+
        Number(x.account_id)+'">Ver cuenta</button><button class="primary small" data-action="'+action+'" data-id="'+
        Number(x.event_id)+'">'+label+'</button></div></div>';
    }).join(''):
      '<p class="mini">No tienes avisos pendientes de entregar en este turno.</p>')+'</div>';
}
async function showAccounts(all){
  const arr=await api('/accounts');
  state.data.accounts=arr;
  const active=arr.filter(x=>x.status==='open'||x.status==='pending');
  const readyHtml=!all?waiterReadyAlertsHtml():'';
  return `<div class="accounts-screen"><div class="statgrid account-stats">${stat('Cuentas activas',active.length)}${stat('Por cobrar',active.filter(x=>x.status==='pending').length)}</div>${readyHtml}${!all?'<button class="primary accounts-new-button" data-nav="new">＋ Abrir nueva cuenta</button>':''}<h2 class="section accounts-section-title">${all?'Todas las cuentas':'Mis cuentas'}</h2><div class="cards accounts-list">${active.map(a=>accountCard(a,all?'admin-account':'open-account')).join('')||'<div class="empty">Todavía no hay cuentas activas.</div>'}</div>${all?'<h2 class="section accounts-section-title">Cuentas pagadas de este turno</h2><div class="cards accounts-list">'+arr.filter(a=>a.status==='paid').slice(0,15).map(a=>accountCard(a,'admin-account')).join('')+'</div><h2 class="section accounts-section-title">Cuentas unidas recientemente</h2><div class="cards accounts-list">'+arr.filter(a=>a.status==='merged').slice(0,15).map(a=>accountCard(a,'admin-account')).join('')+'</div>':''}</div>`;
}

function ticketLookupHtml(){return `<div class="card" style="margin-top:15px"><h3>Consultar ticket histórico</h3><p>Busca por folio, aunque la cuenta ya esté pagada.</p><form id="ticket-lookup" class="formrow"><label>Folio de cuenta<input type="number" min="1" name="id" required></label><button class="outline">Buscar ticket</button></form></div>`}
async function showNew(){
  const tables=await api('/tables');state.data.tables=tables;
  const available=tables.filter(t=>t.active&&t.status==='available');
  const occupied=tables.filter(t=>t.status==='occupied');
  return `<div class="card"><h3>Abrir una cuenta</h3><p>Selecciona una mesa disponible o abre una cuenta libre para llevar.</p><form id="new-account" class="formrow"><label>Nombre o referencia <span class="mini">(opcional si eliges una mesa)</span><input name="name" maxlength="120" placeholder="Familia López, Terraza, Para llevar…"></label><h3 class="section">Tipo de cuenta</h3><div class="table-choice-grid"><label class="table-choice selected"><input type="radio" name="table_id" value="" checked><span><b>Cuenta libre</b><small>Sin mesa asignada</small></span><em>＋</em></label>${available.map(t=>`<label class="table-choice"><input type="radio" name="table_id" value="${t.id}"><span><b>${esc(t.name)}</b><small>${esc(t.area)} · ${t.capacity} personas</small></span><em>Disponible</em></label>`).join('')}${occupied.map(t=>`<div class="table-choice occupied" aria-disabled="true"><span><b>${esc(t.name)}</b><small>${esc(t.area)} · ${esc(t.account_name||'Cuenta activa')} · ${esc(t.waiter||'')}</small></span><em>Ocupada</em></div>`).join('')}</div>${!tables.length?'<div class="note">Todavía no hay mesas configuradas. Puedes abrir una cuenta libre; solicita al administrador que registre las mesas.</div>':''}<button class="primary">Crear cuenta</button></form></div>`;
}
async function showTables(){
  const tables=await api('/tables');state.data.tables=tables;
  const active=tables.filter(t=>t.active).length;
  const occupied=tables.filter(t=>t.status==='occupied').length;
  const available=tables.filter(t=>t.active&&t.status==='available').length;
  const inactive=tables.filter(t=>!t.active).length;
  const liveCards=tables.map(t=>{
    const status=t.status==='occupied'?'occupied':t.active?'available':'inactive';
    return `<article class="table-live-card table-live-${status}">
      <div class="table-live-head"><span class="table-live-icon" aria-hidden="true">▦</span>${pill(status)}</div>
      <h3>${esc(t.name)}</h3>
      <p class="table-live-meta">${esc(t.area)} · Capacidad ${Number(t.capacity)||0}</p>
      ${status==='occupied'?'<div class="table-live-assignment"><span>Cuenta</span><strong>'+esc(t.account_name||'Cuenta activa')+'</strong><small>Mesero: '+esc(t.waiter||'Sin asignar')+'</small></div>':'<div class="table-live-assignment"><span>Estado</span><strong>'+(status==='available'?'Lista para asignar':'Deshabilitada')+'</strong></div>'}
    </article>`;
  }).join('');
  return `<div class="table-screen">
    <div class="statgrid table-stats">${stat('Mesas registradas',tables.length)}${stat('Disponibles',available)}${stat('Ocupadas',occupied)}${stat('Inactivas',inactive)}</div>
    <section class="table-live-section">
      <div class="table-live-heading"><div><p class="premium-eyebrow">ESTADO DEL SALÓN</p><h2>Mesas en tiempo real</h2></div><span class="table-live-count">${active} activas</span></div>
      <div class="table-live-grid">${liveCards||'<div class="empty">Todavía no hay mesas registradas.</div>'}</div>
    </section>
    <section class="card table-create-card"><div class="table-admin-heading"><div><p class="premium-eyebrow">CONFIGURACIÓN</p><h3>Registrar nueva mesa</h3></div></div>
      <p>Configura aquí las mesas del restaurante. Los meseros solo podrán seleccionarlas; no podrán crear ni modificar mesas.</p>
      <form id="table-add" class="formrow"><div class="formgrid">
        <label>Nombre o número<input name="name" required maxlength="80" placeholder="Mesa 1"></label>
        <label>Área<input name="area" required maxlength="80" value="Salón" placeholder="Salón, terraza…"></label>
        <label>Capacidad de personas<input name="capacity" type="number" min="1" max="30" value="4" required></label>
        <label>Orden de visualización<input name="sort_order" type="number" min="0" value="${tables.length+1}"></label>
      </div><button class="primary">＋ Registrar mesa</button></form>
    </section>
    <h2 class="section table-admin-title">Administrar mesas registradas</h2>
    <div class="table-admin-grid">${tables.map(t=>`<div class="card table-admin-card"><div class="row"><h3>${esc(t.name)}</h3>${pill(t.status==='occupied'?'occupied':t.active?'available':'inactive')}</div>
      <p class="mini">${esc(t.area)} · Capacidad ${t.capacity}${t.account_name?`<br>Cuenta: ${esc(t.account_name)} · Mesero: ${esc(t.waiter||'')}`:''}</p>
      <form id="table-edit-${t.id}" class="formrow"><label>Nombre<input name="name" value="${esc(t.name)}" maxlength="80" required></label>
        <div class="formgrid"><label>Área<input name="area" value="${esc(t.area)}" maxlength="80" required></label>
        <label>Capacidad<input name="capacity" type="number" min="1" max="30" value="${t.capacity}" required></label>
        <label>Orden<input name="sort_order" type="number" min="0" value="${t.sort_order}"></label>
        <label>Estado<select name="active"><option value="true" ${t.active?'selected':''}>Activa</option><option value="false" ${!t.active?'selected':''} ${t.status==='occupied'?'disabled':''}>Inactiva</option></select></label></div>
        <button class="outline small">Guardar cambios</button></form></div>`).join('')||'<div class="empty">Todavía no hay mesas. Registra la primera arriba.</div>'}</div>
  </div>`;
}
function orderProductConfig(p){
  if(!p)return '<p class="muted">No hay productos disponibles.</p>';
  const variants=(p.variants||[]).filter(v=>v.active);
  const groups=(p.modifier_groups||[]).filter(g=>g.active&&g.options.some(o=>o.active));
  const promo=p.promotion?`<span class="pill available">${esc(p.promotion.name)} · ${money(p.promotion.price)}</span>`:'';
  return `<div class="order-config-block"><div class="row"><b>Precio base</b><span class="price">${money(p.current_price??p.price)}</span></div>${promo}
    ${variants.length?`<label>Presentación<select name="variant_id"><option value="">Estándar · ${money(p.current_price??p.price)}</option>${variants.map(v=>`<option value="${v.id}">${esc(v.name)} · ${money(v.price)}</option>`).join('')}</select></label>`:''}
    ${groups.map(g=>`<fieldset class="modifier-group" data-group-id="${g.id}" data-min="${g.min_select}" data-max="${g.max_select}"><legend>${esc(g.name)} <small>${g.min_select?'Obligatorio':'Opcional'} · máximo ${g.max_select}</small></legend><div class="modifier-options">${g.options.filter(o=>o.active).map(o=>`<label class="modifier-option"><input type="checkbox" name="modifier_option_ids" value="${o.id}"><span>${esc(o.name)}</span><b>${Number(o.price_delta)>0?'+'+money(o.price_delta):'Sin costo'}</b></label>`).join('')}</div></fieldset>`).join('')}
    <div class="formgrid"><label>Preparación estimada (min)<input name="prep_minutes" type="number" min="0" max="240" value="15"></label><label>Salida después de (min)<input name="release_after_minutes" type="number" min="0" max="240" value="0"></label></div>
    <label>Nota para cocina/barra<textarea name="note" maxlength="300" rows="2" placeholder="Ej. sin cebolla, salsa aparte, término medio…"></textarea></label>
  </div>`;
}
function estimateDraftPrice(p,variantId,optionIds){
  const variant=(p.variants||[]).find(v=>v.id===Number(variantId));
  let base=Number(variant?variant.price:(p.current_price??p.price));
  if(variant&&p.promotion){
    if(p.promotion.fixed_price!==null&&p.promotion.fixed_price!==undefined)base=Number(p.promotion.fixed_price);
    else if(p.promotion.discount_percent)base=base*(100-Number(p.promotion.discount_percent))/100;
  }
  const extras=(p.modifier_groups||[]).flatMap(g=>g.options||[]).filter(o=>optionIds.includes(o.id)).reduce((n,o)=>n+Number(o.price_delta||0),0);
  return Math.max(0,base+extras);
}
function draftMarkup(){
  return `${state.draft.map((x,i)=>`<div class="lineitem row"><div><b>${x.qty} × ${esc(x.name)}${x.variant_name?' · '+esc(x.variant_name):''}</b><div class="mini">${x.station==='kitchen'?'Cocina':'Barra'} · ${esc((Array.isArray(x.modifier_names)?x.modifier_names:[]).join(', '))}${x.note?' · '+esc(x.note):''}</div><div class="mini">Preparación: ${x.prep_minutes} min${x.release_after_minutes?' · Salida +'+x.release_after_minutes+' min':''}</div><b>${money(x.unit_price*x.qty)}</b></div><button class="ghost small" data-action="remove-line" data-id="${i}">Quitar</button></div>`).join('')||'<p class="muted">Agrega productos para formar una comanda.</p>'}
    <button id="send-order-btn" class="primary" data-action="send-order" ${!state.draft.length?'disabled':''}>Enviar comanda a cocina y barra</button>`;
}
function updateDraftUI(){
  const panel=$('#draft-panel');
  if(panel)panel.innerHTML=draftMarkup();
  const heading=$('#draft-heading');
  if(heading)heading.textContent=`Comanda por enviar (${state.draft.length})`;
  const bill=$('#bill-btn');
  if(bill)bill.disabled=state.draft.length>0;
}
async function showOrders(){
  // Cargar las dependencias de la pantalla en paralelo y reutilizar el contexto
  // de cuentas/mesas al construir el detalle, sin repetir las mismas peticiones.
  const requests=[api('/accounts'),api('/products')];
  if(state.user?.role==='owner')requests.push(api('/tables'));
  const [arr,productResponse,tables]=await Promise.all(requests);
  state.data.accounts=arr;
  if(tables)state.data.tables=tables;
  const open=arr.filter(x=>x.status==='open');
  if(!open.length)return '<div class="empty">No tienes cuentas abiertas. Crea una para comenzar.</div><p><button class="primary" data-nav="new">Nueva cuenta</button></p>';
  if(!open.some(a=>a.id===state.account)){state.account=open[0].id;state.draft=[];persistWorkState()}
  const a=open.find(x=>x.id===state.account);
  const prods=productResponse.filter(x=>x.active&&!x.blocked&&x.status!=='Agotado');state.products=prods;
  state.draft=(Array.isArray(state.draft)?state.draft:[]).filter(Boolean).map(x=>({...x,modifier_names:Array.isArray(x.modifier_names)?x.modifier_names:[]}));
  const first=prods[0];
  return `<div class="card"><label>Cuenta<select id="order-account">${open.map(x=>`<option value="${x.id}" ${x.id===state.account?'selected':''}>#${x.id} · ${esc(x.name)}</option>`).join('')}</select></label><div class="row" style="margin-top:12px"><b>${esc(a.name)}</b>${pill(a.status)}</div></div>
  <h2 class="section">Personalizar pedido</h2><div class="card"><form id="add-line" class="formrow">
    <label>Producto<select name="product_id" id="order-product" required>${prods.map(p=>`<option value="${p.id}">${esc(p.name)} · ${money(p.current_price??p.price)} · ${p.station==='kitchen'?'Cocina':'Barra'}</option>`).join('')}</select></label>
    <div id="product-config">${orderProductConfig(first)}</div>
    <label>Cantidad<input name="qty" type="number" min="1" max="100" value="1" required></label>
    <button class="outline" ${!prods.length?'disabled':''}>＋ Agregar a la comanda</button>
  </form></div>
  <h2 class="section" id="draft-heading">Comanda por enviar (${state.draft.length})</h2><div class="card" id="draft-panel">${draftMarkup()}</div>
  <div class="card" style="margin-top:15px"><button id="bill-btn" class="outline" data-action="bill" data-id="${a.id}" ${state.draft.length?'disabled':''}>Solicitar cuenta a caja</button><p class="mini">Envía o quita los productos pendientes antes de solicitar el cobro.</p></div>
  <h2 class="section">Detalle y solicitudes</h2>${await accountDetailHtml(a.id,false)}`;
}
function accountHistoryMarkup(rows){
  const labels={
    'account.rename':'Cambio de nombre',
    'account.table_transfer':'Cambio de mesa',
    'account.reassign':'Cambio de mesero',
    'account.merge':'Unión de cuenta',
    'account.merge_received':'Cuenta recibida',
    'account.split_out':'Producto trasladado',
    'account.split_in':'Producto recibido',
    'account.create_from_split':'Cuenta creada por división',
    'account.reopen_closed':'Cuenta reabierta',
    'account.close':'Cuenta cerrada'
  };
  if(!rows.length)return '<p class="mini">No hay movimientos registrados para esta cuenta.</p>';
  return rows.slice(0,12).map(h=>'<div class="lineitem"><div class="row"><b>'+
    esc(labels[h.action]||h.action)+'</b><span class="mini">'+
    esc(h.date?new Date(h.date).toLocaleString('es-MX'):'')+
    '</span></div><div class="mini">'+esc(h.actor||'Sistema')+' · '+
    esc(h.detail?.reason||h.detail?.product||'Movimiento registrado')+
    (h.detail?.target_account_id?' · Cuenta destino #'+Number(h.detail.target_account_id):'')+
    (h.detail?.to_table?' · '+esc(h.detail.to_table):'')+
    '</div></div>').join('');
}
async function accountDetailHtml(id,admin=false){
  const a=await api('/accounts/'+id);
  const canManage=['owner','admin','waiter'].includes(state.user?.role);
  // Conservar acciones operativas de meseros; reservar la edición avanzada al Propietario.
  const canEditAccount=state.user?.role==='owner';
  const isAdmin=['owner','admin'].includes(state.user?.role);
  let tables=[],accounts=[];
  if(canEditAccount&&['open','pending'].includes(a.status)){
    tables=state.data.tables||await api('/tables');
    accounts=state.data.accounts||await api('/accounts');
  }
  if(canEditAccount&&!state.data.users)state.data.users=await api('/users');
  const availableTables=tables.filter(t=>t.active&&(t.status==='available'||t.id===a.table_id));
  const otherAccounts=accounts.filter(x=>x.id!==a.id&&['open','pending'].includes(x.status));
  const activeItems=a.items.filter(i=>i.qty>i.cancelled_qty);
  // El historial se consulta solo cuando alguien lo solicita, no en cada apertura.
  const historyHtml=canManage?`<div class="card"><div class="row"><h3>Historial de movimientos</h3><button class="outline small" data-action="load-account-history" data-id="${id}">Consultar historial</button></div><div id="account-history-${id}" class="mini">El historial se cargará cuando lo necesites.</div></div>`:'';
  const actionsHtml=canEditAccount&&['open','pending'].includes(a.status)?`
    <div class="card"><h3>Modificar cuenta</h3>
      <form id="account-rename-form" class="formrow">
        <input type="hidden" name="account_id" value="${id}">
        <label>Nombre de la cuenta<input name="name" maxlength="120" required value="${esc(a.name)}"></label>
        <label>Motivo del cambio<input name="reason" maxlength="400" minlength="3" required placeholder="Corrección del nombre"></label>
        <button class="outline small">Guardar nombre</button>
      </form>
      <div class="divider"></div>
      <form id="account-table-form" class="formrow">
        <input type="hidden" name="account_id" value="${id}">
        <label>Asignar o cambiar mesa<select name="table_id"><option value="">Sin mesa · Cuenta libre</option>${availableTables.map(t=>`<option value="${t.id}" ${t.id===a.table_id?'selected':''}>${esc(t.name)} · ${esc(t.area)}${t.id===a.table_id?' · Actual':''}</option>`).join('')}</select></label>
        <label>Motivo<input name="reason" minlength="3" maxlength="400" required placeholder="Cambio de ubicación"></label>
        <button class="outline small">Actualizar mesa</button>
      </form>
      ${canEditAccount?`<div class="divider"></div><form id="reassign-form" class="formrow"><input type="hidden" name="account_id" value="${id}"><label>Nuevo mesero<select name="owner_id">${(state.data.users||[]).filter(u=>u.role==='waiter'&&u.active).map(u=>`<option value="${u.id}" ${u.id===a.owner_id?'selected':''}>${esc(u.display)}</option>`).join('')}</select></label><label>Motivo<input name="reason" required minlength="3" placeholder="Cambio de turno"></label><button class="outline small">Reasignar mesero</button></form>`:''}
    </div>
    ${otherAccounts.length?`<div class="card"><h3>Unir esta cuenta con otra</h3><p class="mini">Esta cuenta se integrará en la cuenta destino. Sus comandas, pagos y asignaciones de productos se conservarán.</p><form id="account-merge-form" class="formrow"><input type="hidden" name="account_id" value="${id}"><label>Cuenta destino<select name="target_account_id" required>${otherAccounts.map(x=>`<option value="${x.id}">#${x.id} · ${esc(x.name)} · ${esc(x.owner)} · Saldo ${money(x.due)}</option>`).join('')}</select></label><label>Motivo de unión<input name="reason" minlength="3" maxlength="400" required placeholder="Misma mesa / solicitud del cliente"></label><button class="red small">Unir cuentas</button></form></div>`:''}
    ${activeItems.length?`<div class="card"><h3>Dividir productos de esta cuenta</h3><p class="mini">Solo se pueden trasladar unidades no asignadas a pagos y que cocina o barra ya hayan marcado como listas. La división no genera una nueva comanda.</p><form id="account-split-form" class="formrow"><input type="hidden" name="account_id" value="${id}"><label>Producto<select name="item_id" required>${activeItems.map(i=>`<option value="${i.id}">${i.qty-i.cancelled_qty} × ${esc(i.name)} · ${money(i.price)} · #${i.id}</option>`).join('')}</select></label><label>Cantidad a trasladar<input name="qty" type="number" min="1" max="${Math.max(...activeItems.map(i=>i.qty-i.cancelled_qty))}" value="1" required></label><label>Destino<select name="target_account_id"><option value="">Crear una cuenta nueva</option>${otherAccounts.map(x=>`<option value="${x.id}">Agregar a #${x.id} · ${esc(x.name)}</option>`).join('')}</select></label><label>Nombre de la nueva cuenta<input name="name" maxlength="120" placeholder="Dividida de #${id}"></label><label>Mesa para la nueva cuenta<select name="table_id"><option value="">Cuenta libre</option>${tables.filter(t=>t.active&&t.status==='available').map(t=>`<option value="${t.id}">${esc(t.name)} · ${esc(t.area)}</option>`).join('')}</select></label><label>Motivo<input name="reason" minlength="3" maxlength="400" required placeholder="División solicitada por el cliente"></label><button class="outline small">Dividir / trasladar producto</button></form></div>`:''}
  `:'';
  const reopenHtml=canEditAccount&&a.status==='paid'?`<div class="card"><h3>Reabrir cuenta cerrada</h3><p class="mini">Se conservarán los pagos anteriores. La cuenta volverá a estar abierta para registrar nuevos consumos.</p><button class="outline" data-action="reopen-closed" data-id="${id}">Reabrir cuenta #${id}</button></div>`:'';
  return `<div class="card"><div class="row"><h3>${esc(a.name)} · #${a.id}</h3>${pill(a.status)}</div>${a.table_name?`<p class="mini">Mesa: ${esc(a.table_name)} · ${esc(a.table_area||'')}</p>`:''}<p>Total ${money(a.total)} · Pagado ${money(a.paid)} · <b>Saldo ${money(a.due)}</b></p>${a.items.map(i=>`<div class="lineitem"><div class="row"><b>${i.qty-i.cancelled_qty} × ${esc(i.name)}${i.variant_name?' · '+esc(i.variant_name):''}</b><span>${money(Number(i.price)*(i.qty-i.cancelled_qty))}</span></div><div class="mini">${i.station==='kitchen'?'Cocina':'Barra'}${i.promotion_name?' · '+esc(i.promotion_name):''}</div>${i.modifiers?.length?`<div class="mini">Extras: ${i.modifiers.map(m=>esc(m.name)+(Number(m.price_delta)>0?' (+'+money(m.price_delta)+')':'')).join(', ')}</div>`:''}${i.note?`<div class="note">${esc(i.note)}</div>`:''}<div class="mini">Preparación: ${Number(i.prep_minutes||15)} min${i.release_after_minutes?' · Salida +'+Number(i.release_after_minutes)+' min':''}</div>${canManage&&a.status==='open'&&i.qty>i.cancelled_qty?`<div class="inline"><button class="ghost small" data-action="repeat-item" data-account="${id}" data-id="${i.id}">Repetir producto</button>${!admin?`<button class="ghost small" data-action="cancel-request" data-account="${id}" data-id="${i.id}" data-qty="${i.qty-i.cancelled_qty}">Solicitar cancelación</button>`:''}</div>`:(!admin&&['open','pending'].includes(a.status)&&i.qty>i.cancelled_qty?`<button class="ghost small" data-action="cancel-request" data-account="${id}" data-id="${i.id}" data-qty="${i.qty-i.cancelled_qty}">Solicitar cancelación</button>`:'')}</div>`).join('')||'<p class="muted">Sin productos.</p>'}${!admin&&a.status==='pending'&&Number(a.paid)===0?`<button class="outline small" data-action="reopen-request" data-id="${id}">Solicitar reapertura</button>`:''}${admin&&canEditAccount&&['open','pending'].includes(a.status)&&Number(a.total)===0&&Number(a.paid)===0?`<div class="divider"></div><button class="red" data-action="void-account" data-id="${id}">Anular cuenta vacía (con motivo)</button>`:''}</div>${actionsHtml}${reopenHtml}${historyHtml}`;
}
async function refreshAccountPanel(id,admin=false){
  if(admin){
    if(state.user?.role==='owner'){
      const [users,accounts,tables]=await Promise.all([
        api('/users'),api('/accounts'),api('/tables')
      ]);
      state.data.users=users;state.data.accounts=accounts;state.data.tables=tables;
    }else{
      state.data.accounts=await api('/accounts');
    }
    $('#content').innerHTML=`<button class="outline small" data-nav="all">← Volver a todas las cuentas</button><h2 class="section">Cuenta #${id}</h2>${await accountDetailHtml(id,true)}`;
  }else{
    await render();
  }
}
async function showStation(history=false,all=false){const tickets=await api('/tickets');state.data.tickets=tickets;const shown=tickets.filter(t=>history?t.status==='ready':t.status!=='ready');const newCount=tickets.filter(t=>t.status==='new'&&(all?true:t.station===state.user.role)).length;return `<div class="statgrid">${stat('Comandas nuevas',newCount)}${stat('En proceso',shown.filter(t=>t.status==='received'||t.status==='preparing').length)}</div>${!history?'<div class="note">Las comandas nuevas requieren confirmar recepción. Los tiempos de preparación y salida se muestran por producto.</div><p><button class="outline" data-action="enable-sound">🔔 Activar / probar sonido</button></p>':''}<div class="cards" style="margin-top:15px">${shown.map(t=>`<div class="card ticket ${t.status}"><div class="row"><h3>${esc(t.account)} · #${t.account_id}</h3>${pill(t.status)}</div><div class="mini">${t.station==='kitchen'?'🍳 Cocina':'🥤 Barra'} · Mesero: ${esc(t.waiter)} · Comanda #${t.batch_id} · ${t.created_at?new Date(t.created_at).toLocaleTimeString('es-MX',{hour:'2-digit',minute:'2-digit'}):''}</div><div class="divider"></div>${t.items.map(i=>`<div class="lineitem"><b>${i.qty} × ${esc(i.name)}${i.variant_name?' · '+esc(i.variant_name):''}</b>${i.modifiers?.length?`<div class="mini">Extras: ${i.modifiers.map(m=>esc(m.name)).join(', ')}</div>`:''}${i.note?`<div class="note">${esc(i.note)}</div>`:''}<div class="mini">Preparación estimada: ${Number(i.prep_minutes||15)} min${i.release_after_minutes?' · Salida programada +'+Number(i.release_after_minutes)+' min desde el envío':''}</div></div>`).join('')}${!history?`<button class="primary" data-action="ticket-status" data-id="${t.id}" data-status="${t.status==='new'?'received':t.status==='received'?'preparing':'ready'}">${t.status==='new'?'Confirmar recepción':t.status==='received'?'Iniciar preparación':'Marcar como listo'}</button>`:''}</div>`).join('')||'<div class="empty">No hay comandas en esta sección.</div>'}</div>`}

async function showCashier(){
  const query=state.account?'?account_id='+encodeURIComponent(state.account):'';
  const data=await api('/cashier/overview'+query);
  const accounts=data.accounts||[];
  state.data.accounts=accounts;
  if(!accounts.length){
    state.account=null;state.data.currentAccount=null;
    return '<div class="cashier-screen"><div class="empty cashier-empty">No hay cuentas pendientes de cobro.</div>'+ticketLookupHtml()+'</div>';
  }
  const a=data.selected;
  if(!a)throw Error('No se pudo cargar el detalle de la cuenta seleccionada. Actualiza e inténtalo de nuevo.');
  state.account=a.id;state.data.currentAccount=a;
  const admin=['owner','admin'].includes(state.user.role);
  const activeDiscounts=(a.discounts||[]).filter(d=>d.status==='applied');
  const hasPayments=(a.payments||[]).length>0;
  const discountHtml=`<section class="cashier-discount-section"><h2 class="section">Descuentos</h2><div class="card cashier-discounts">
    ${activeDiscounts.map(d=>`<div class="lineitem row"><div><b>${d.scope==='account'?'Descuento general':'Descuento por producto'+(d.item_name?' · '+esc(d.item_name):'')}</b> · -${money(d.amount)}<div class="mini">${esc(d.reason)} · ${esc(d.mode==='percent'?d.value+'%':'Importe fijo')}</div></div>${admin&&!hasPayments?`<button class="ghost small" data-action="void-discount" data-id="${d.id}" data-account="${a.id}">Retirar</button>`:''}</div>`).join('')||'<p class="muted">No hay descuentos aplicados.</p>'}
    ${admin&&!hasPayments?`<form id="discount-form" class="formrow"><h3>Aplicar descuento autorizado</h3><div class="formgrid"><label>Aplicar a<select name="scope"><option value="account">Cuenta completa</option><option value="item">Un producto</option></select></label><label>Tipo<select name="mode"><option value="fixed">Importe fijo ($)</option><option value="percent">Porcentaje (%)</option></select></label></div><label>Producto (solo para descuento por producto)<select name="item_id"><option value="">Seleccionar producto</option>${a.items.filter(i=>i.qty>i.cancelled_qty).map(i=>`<option value="${i.id}">${esc(i.name)} · ${i.qty-i.cancelled_qty} ud. · ${money(i.price)}</option>`).join('')}</select></label><div class="formgrid"><label>Valor<input name="value" type="number" min="0.01" step="0.01" required></label><label>Motivo obligatorio<input name="reason" minlength="3" maxlength="400" required placeholder="Autorización, promoción…"></label></div><p class="mini">Los descuentos se registran antes del primer pago. Primero aplica los descuentos por producto y al final, si corresponde, el descuento general.</p><button class="outline">Aplicar descuento</button></form>`:hasPayments?'<div class="note">Los descuentos quedan bloqueados después del primer pago para proteger la conciliación.</div>':''}
  </div></section>`;
  return `<div class="cashier-screen"><div class="card cashier-summary"><label>Cuenta por cobrar<select id="cashier-account">${accounts.map(x=>`<option value="${x.id}" ${x.id===a.id?'selected':''}>#${x.id} · ${esc(x.name)} · Saldo ${money(x.due)}</option>`).join('')}</select></label><div class="divider"></div><div class="row"><span>Subtotal antes de descuentos</span><b>${money(a.gross_total??a.total)}</b></div><div class="row"><span>Descuentos</span><b>-${money(a.discount_total||0)}</b></div><div class="row"><b>Total a cobrar: ${money(a.total)}</b>${pill(a.status)}</div><div class="row"><span>Pagado: ${money(a.paid)}</span><strong class="total">Resta ${money(a.due)}</strong></div></div>
  <h2 class="section cashier-section-title">Productos de la cuenta</h2><div class="card cashier-products">${a.items.map(i=>`<div class="lineitem row"><div><b>${i.qty-i.cancelled_qty} × ${esc(i.name)}</b><div class="mini">#${i.id} · ${money(i.price)} por unidad</div></div><span>${money(Number(i.price)*(i.qty-i.cancelled_qty))}</span></div>`).join('')||'<p class="muted">Sin productos.</p>'}</div>
  ${discountHtml}
  <h2 class="section cashier-section-title">Registrar pago parcial o mixto</h2><div class="card cashier-payment"><form id="pay-form" class="formrow"><label>Forma de división<select id="split-mode" name="split_mode"><option value="amount">Por importe</option><option value="products" ${activeDiscounts.length?'disabled':''}>Por productos completos ${activeDiscounts.length?'(deshabilitado con descuentos)':''}</option></select></label><div id="split-products" hidden><p class="mini">Indica cuántas unidades paga esta persona. La suma se calculará automáticamente.</p>${a.items.filter(i=>i.qty>i.cancelled_qty).map(i=>`<label>${esc(i.name)} · ${money(i.price)} por unidad<input class="allocation-qty" type="number" min="0" max="${i.qty-i.cancelled_qty}" value="0" data-item="${i.id}" data-price="${i.price}"></label>`).join('')}</div><div class="formgrid"><label>Consumo en efectivo ($)<input type="number" step="0.01" min="0" name="cash" value="0"></label><label>Consumo con tarjeta ($)<input type="number" step="0.01" min="0" name="card" value="0"></label><label>Consumo por transferencia ($)<input type="number" step="0.01" min="0" name="transfer" value="0"></label><label>Efectivo recibido (incluye propina) ($)<input type="number" step="0.01" min="0" name="received" value="0"></label></div><h3>Propinas (separadas del consumo)</h3><div class="formgrid"><label>Propina en efectivo ($)<input type="number" step="0.01" min="0" name="cash_tip" value="0"></label><label>Propina con tarjeta ($)<input type="number" step="0.01" min="0" name="card_tip" value="0"></label><label>Propina por transferencia ($)<input type="number" step="0.01" min="0" name="transfer_tip" value="0"></label></div><div class="note">Las propinas no reducen el saldo de consumo; se registran por separado y se incluyen en el corte de caja. Si se recibe efectivo, indica el total entregado incluyendo la propina.</div><button class="primary" ${Number(a.due)<=0?'disabled':''}>Confirmar pago</button></form></div>
  <h2 class="section cashier-section-title">Pagos registrados</h2><div class="card cashier-history">${(a.payments||[]).map(p=>`<div class="lineitem row"><span>${esc(p.method)} · ${money(p.amount)}${Number(p.tip)>0?` · Propina ${money(p.tip)}`:''}</span><span class="mini">#${p.id}</span></div>`).join('')||'<p class="muted">Sin pagos.</p>'}<div class="row"><span>Propinas acumuladas</span><b>${money((a.tips||[]).reduce((s,t)=>s+Number(t.amount||0),0))}</b></div>${Number(a.due)===0?`<button class="primary" data-action="close-account" data-id="${a.id}">Cerrar cuenta pagada</button>`:''}<div class="inline"><button class="outline" data-action="print-prebill" data-id="${a.id}">Pre-cuenta</button><button class="outline" data-action="download-prebill" data-id="${a.id}">Descargar pre-cuenta PDF</button><button class="outline" data-action="print-ticket" data-id="${a.id}">Ver ticket</button><button class="outline" data-action="download-ticket" data-id="${a.id}">Descargar PDF</button><button class="outline" data-action="share-ticket" data-id="${a.id}">Compartir por WhatsApp</button></div></div>${ticketLookupHtml()}</div>`;
}
function cashHistoryMarkup(history){
  return `<h2 class="section">Historial de turnos cerrados</h2><div class="cards">${history.map(p=>`<div class="card"><div class="row"><h3>Turno #${p.id}</h3>${pill('paid')}</div><p>${esc(new Date(p.closed_at).toLocaleString('es-MX'))} · ${esc(p.closed_by)}</p><p><b>Ventas ${money(p.sales)}</b> · Propinas ${money(p.tips||0)} · ${p.account_count} cuentas · Diferencia ${money(p.difference)} · ${p.tip_distribution?'Reparto de propinas generado':'Sin reparto de propinas'}</p><div class="inline"><button class="primary small" data-action="view-report" data-id="${p.id}">Ver corte tipo ticket</button><button class="outline small" data-action="download-report" data-id="${p.id}">Descargar PDF</button><button class="outline small" data-action="period-detail" data-id="${p.id}">Detalles</button></div></div>`).join('')||'<div class="empty">Aún no hay turnos cerrados.</div>'}</div><div id="period-detail" style="margin-top:14px"></div>`;
}
async function loadCashHistory(seq=state.renderSeq){
  const target=$('#cash-history');
  if(!target||state.screen!=='cash')return;
  target.innerHTML='<div class="empty">Cargando historial de turnos…</div>';
  try{
    const history=await api('/cash/history');
    if(seq!==state.renderSeq||state.screen!=='cash')return;
    const current=$('#cash-history');
    if(current)current.innerHTML=cashHistoryMarkup(history);
  }catch(error){
    if(seq!==state.renderSeq||state.screen!=='cash')return;
    const current=$('#cash-history');
    if(current)current.innerHTML='<div class="note danger-note">No se pudo cargar el historial. Usa Actualizar para volver a intentarlo.</div>';
  }
}
async function showCash(){
  const c=await api('/cash');
  state.data.cash=c;
  const admin=['owner','admin'].includes(state.user.role);
  const historyHtml='<div id="cash-history"><div class="empty">Preparando historial de turnos…</div></div>';
  if(!c.open)return `<div class="note danger-note">Turno cerrado y archivado. La actividad del turno anterior quedó en Historial con su PDF. Abre un turno nuevo para empezar con las vistas activas limpias.</div>${admin?'<form id="cash-open" class="card formrow"><label>Fondo inicial ($)<input name="opening" type="number" min="0" step="0.01" required value="0"></label><button class="primary">Abrir turno nuevo</button></form>':''}${historyHtml}`;
  const blockers=c.blocking_accounts||[];
  const blockersHtml=`<div class="card" style="margin-top:14px"><h3>Control de cierre de turno</h3>${c.blocking_count>0?`<div class="note danger-note"><b>Cierre bloqueado:</b> hay ${c.blocking_count} cuenta(s) sin finalizar. Deben quedar pagadas y cerradas o anuladas con autorización.</div><div class="stack" style="margin-top:12px">${blockers.map(a=>`<div class="row lineitem"><div><b>#${a.id} · ${esc(a.name)}</b><div class="mini">${esc(a.status==='pending'?'Por cobrar':'Abierta')} · Saldo ${money(a.due)}</div></div>${admin?`<button class="outline small" data-action="admin-account" data-id="${a.id}">Revisar</button>`:''}</div>`).join('')}</div>`:'<div class="note">Todas las cuentas están finalizadas. El turno puede cerrarse cuando no existan movimientos pendientes.</div>'}</div>`;
  const pendingMoves=c.movements.some(m=>m.status==='pending');
  const canClose=Boolean(c.can_close);
  const movementHtml=`<div class="card"><h3>Entradas y salidas de efectivo</h3><form id="cash-movement" class="formrow"><div class="formgrid"><label>Tipo<select name="kind"><option value="in">Entrada</option><option value="out">Salida</option></select></label><label>Importe ($)<input name="amount" type="number" min="0.01" step="0.01" required></label></div><label>Motivo<input name="reason" minlength="3" required></label><button class="outline">${state.user.role==='cashier'?'Solicitar autorización':'Registrar movimiento'}</button></form>${c.movements.map(m=>`<div class="lineitem row"><div><b>${m.kind==='in'?'Entrada':'Salida'} ${money(m.amount)}</b><div class="mini">${esc(m.reason)} · ${esc(m.status)}</div></div>${m.status==='pending'&&admin?`<div class="inline"><button class="primary small" data-action="move-decision" data-id="${m.id}" data-approve="true">Aprobar</button><button class="ghost small" data-action="move-decision" data-id="${m.id}" data-approve="false">Rechazar</button></div>`:''}</div>`).join('')}</div>`;
  const closeHtml=admin?`<div class="card" style="margin-top:14px"><h3>Corte general de caja</h3><p>Solo administración puede cerrar el turno. Se guardará el PDF y el detalle del corte; las vistas de ventas y comandas se reinician para el siguiente turno.</p>${pendingMoves?'<div class="note danger-note">Resuelve los movimientos de caja pendientes antes de cerrar.</div>':''}<form id="cash-close" class="formrow"><label>Efectivo contado ($)<input name="counted" type="number" step="0.01" min="0" required></label><label>Observaciones<input name="note" maxlength="400"></label><button class="red" ${canClose?'':'disabled title="Hay cuentas o movimientos pendientes"'}>${canClose?'Confirmar cierre de turno':'Cierre bloqueado'}</button></form></div>`:'';
  return `<div class="statgrid">${stat('Turno',`#${c.id}`)}${stat('Efectivo',money(c.cash))}${stat('Tarjeta',money(c.card))}${stat('Transferencias',money(c.transfer))}${stat('Propinas',money(c.tips||0))}${stat('Propinas entregadas en efectivo',money(c.tip_payout_cash||0))}${stat('Efectivo esperado',money(c.expected))}</div>${blockersHtml}${movementHtml}${closeHtml}${historyHtml}`;
}

async function showTips(){
  const data=await api('/tips/pool/current');
  const roleName={owner:'Propietario',admin:'Administrador',cashier:'Caja',waiter:'Mesero',kitchen:'Cocina',bar:'Barra'};
  const statusName={paid:'Entregada',partial:'Parcial',pending:'Pendiente'};
  const payoutForm=(d,s,allowCash)=>{
    const pending=Number(s.pending||0);
    if(pending<=0)return '<span class="pill paid">Completada</span>';
    return `<form id="tip-payout-${d.id}-${s.id}" class="formrow" style="margin-top:8px">
      <div class="formgrid"><label>Importe a entregar ($)<input name="amount" type="number" min="0.01" max="${pending.toFixed(2)}" step="0.01" required value="${pending.toFixed(2)}"></label>
      <label>Forma de entrega<select name="method"><option value="cash" ${allowCash?"":"disabled"}>Efectivo</option><option value="transfer">Transferencia</option></select></label></div>
      <label>Nota (opcional)<input name="note" maxlength="400" placeholder="Recibido por el empleado"></label>
      <button class="outline small">Registrar entrega</button>
      ${!allowCash?'<p class="mini">No hay turno de caja abierto: solo se puede registrar transferencia.</p>':''}
    </form>`;
  };
  const distributionCard=(d,allowCash)=>`<div class="card">
    <div class="row"><h3>Reparto · Turno #${d.period_id}</h3><span class="pill paid">Registrado</span></div>
    <p><b>Fondo común: ${money(d.total)}</b> · ${d.participant_count} participantes</p>
    <p class="mini">Cuota base: ${money(d.base_share)} · Centavos sobrantes: ${d.remainder_cents} · Creado por ${esc(d.created_by||'?')} · ${d.created_at?esc(new Date(d.created_at).toLocaleString('es-MX')):''}</p>
    ${d.note?`<p class="mini">Nota: ${esc(d.note)}</p>`:''}
    <div class="statgrid">${stat('Entregado',money(d.paid_total))}${stat('Pendiente',money(d.pending_total))}</div>
    ${d.shares.map(s=>`<div class="lineitem">
      <div class="row"><div><b>${esc(s.employee_name)}</b><div class="mini">${esc(roleName[s.employee_role]||s.employee_role)} · Cuota ${money(s.share)}</div></div><span class="pill ${s.status==='paid'?'paid':s.status==='partial'?'pending':'open'}">${statusName[s.status]||s.status}</span></div>
      <div class="mini">Entregado: ${money(s.paid)} · Pendiente: ${money(s.pending)}</div>
      ${(s.payouts||[]).map(x=>`<div class="mini">↳ ${money(x.amount)} por ${x.method==='cash'?'efectivo':'transferencia'} · ${esc(x.recorded_by)} · ${x.date?esc(new Date(x.date).toLocaleString('es-MX')):''}${x.note?' · '+esc(x.note):''}</div>`).join('')}
      ${payoutForm(d,s,allowCash)}
    </div>`).join('')}
  </div>`;
  const history=(data.history||[]).filter(x=>!data.distribution||x.id!==data.distribution.id);
  const historyHtml=`<h2 class="section">Historial de fondos comunes</h2><div class="cards">${history.map(d=>distributionCard(d,data.open)).join('')||'<div class="empty">Todavía no hay repartos anteriores.</div>'}</div>`;
  if(!data.open)return `<div class="note">No hay un turno de caja abierto. Puedes consultar y completar entregas pendientes por transferencia.</div>${historyHtml}`;
  const methods=data.tips_by_method||{};
  let currentHtml='';
  if(data.distribution){
    currentHtml=distributionCard(data.distribution,true);
  }else if(Number(data.tips_total)>0){
    const blocked=Number(data.blocking_count||0)>0;
    currentHtml=`<div class="card">
      <h3>Crear fondo común · Turno #${data.period_id}</h3>
      <div class="statgrid">${stat('Propinas en efectivo',money(methods.cash||0))}${stat('Propinas con tarjeta',money(methods.card||0))}${stat('Propinas por transferencia',money(methods.transfer||0))}${stat('Total del fondo',money(data.tips_total))}</div>
      ${blocked?`<div class="note danger-note">Antes de repartir, finaliza o anula con autorización las ${data.blocking_count} cuenta(s) que siguen abiertas o pendientes de cobro.</div>`:`<div class="note">Selecciona únicamente al personal que participó en este turno. El reparto será igualitario. Si sobran centavos, se asignará un centavo adicional por empleado en orden de ID ascendente, hasta agotar el sobrante.</div>`}
      <form id="tip-distribution-form" class="formrow">
        <h3>Personal participante</h3>
        <div class="stack">${(data.employees||[]).map(e=>`<label class="row"><span><b>${esc(e.display)}</b><span class="mini"> · ${esc(roleName[e.role]||e.role)}</span></span><input type="checkbox" name="user_ids" value="${e.id}"></label>`).join('')||'<p class="muted">No hay empleados activos.</p>'}</div>
        <label>Observaciones (opcional)<input name="note" maxlength="400" placeholder="Turno, incidencias o aclaraciones"></label>
        <button class="primary" ${blocked||!(data.employees||[]).length?'disabled':''}>Generar reparto igualitario</button>
      </form>
    </div>`;
  }else{
    currentHtml=`<div class="card"><h3>Fondo común · Turno #${data.period_id}</h3><p>Propinas registradas: <b>${money(0)}</b></p><div class="note">Cuando se registren propinas en los cobros, aparecerán aquí. El reparto se habilitará después de finalizar todas las cuentas del turno.</div></div>`;
  }
  return `<div class="statgrid">${stat('Turno activo',`#${data.period_id}`)}${stat('Fondo común',money(data.tips_total||0))}</div>${currentHtml}${historyHtml}`;
}

async function showPeriodDetail(id){
  const p=await api(`/cash/periods/${id}`);
  const target=$('#period-detail');
  if(!target)return;
  target.innerHTML=`<div class="card"><div class="row"><h3>Detalle del turno #${p.id}</h3><button class="ghost small" data-action="hide-period-detail">Cerrar</button></div><p>Abierto: ${esc(new Date(p.opened_at).toLocaleString('es-MX'))}<br>Cerrado: ${esc(new Date(p.closed_at).toLocaleString('es-MX'))}<br>Responsable: ${esc(p.closed_by)}</p><div class="statgrid">${stat('Total cobrado',money(p.sales))}${stat('Efectivo',money(p.cash))}${stat('Tarjeta',money(p.card))}${stat('Transferencia',money(p.transfer))}${stat('Propinas',money(p.tips||0))}${stat('Diferencia',money(p.difference))}</div><p>Fondo inicial ${money(p.opening)} · Esperado ${money(p.expected)} · Contado ${money(p.counted)}</p><h3>Cobros por cajero</h3>${Object.entries(p.cashier_totals||{}).map(([name,total])=>`<div class="lineitem">${esc(name)} · ${money(total)}</div>`).join('')||'<p class="muted">Sin cobros.</p>'}<p class="muted">Observaciones: ${esc(p.note||'Sin observaciones')}</p><h3>Cuentas del turno (${p.accounts.length})</h3>${p.accounts.map(a=>`<div class="lineitem"><b>#${a.id} ${esc(a.name)}</b> · ${esc(statuses[a.status]||a.status)} · ${money(a.total)} · Mesero ${esc(a.owner)}</div>`).join('')||'<p class="muted">Sin cuentas asociadas por fecha.</p>'}<h3 style="margin-top:18px">Pagos (${p.payments.length})</h3>${p.payments.map(x=>`<div class="lineitem">Cuenta #${x.account_id} · ${esc(x.account)} · ${esc(x.method)} · <b>${money(x.amount)}</b>${Number(x.tip)>0?` · Propina ${money(x.tip)}`:''}<div class="mini">Cajero: ${esc(x.cashier)} · ${esc(new Date(x.date).toLocaleString('es-MX'))}</div></div>`).join('')||'<p class="muted">Sin pagos.</p>'}<h3 style="margin-top:18px">Propinas (${(p.tips||[]).length})</h3>${(p.tips||[]).map(x=>`<div class="lineitem">Cuenta #${x.account_id} · ${esc(x.account)} · ${esc(x.method)} · <b>${money(x.amount)}</b><div class="mini">Cajero: ${esc(x.cashier)} · ${esc(new Date(x.date).toLocaleString('es-MX'))}</div></div>`).join('')||'<p class="muted">Sin propinas registradas.</p>'}
  ${p.tip_distribution?`<h3 style="margin-top:18px">Fondo común y reparto igualitario</h3><p>Total ${money(p.tip_distribution.total)} · Participantes ${p.tip_distribution.participant_count} · Entregado ${money(p.tip_distribution.paid_total)} · Pendiente ${money(p.tip_distribution.pending_total)}</p>${p.tip_distribution.shares.map(s=>`<div class="lineitem">${esc(s.employee_name)} · Cuota ${money(s.share)} · Entregado ${money(s.paid)} · Pendiente ${money(s.pending)}</div>`).join('')}`:'<p class="mini">No se generó un reparto igualitario en este turno.</p>'}
  <h3 style="margin-top:18px">Movimientos (${p.movements.length})</h3>${p.movements.map(x=>`<div class="lineitem">${esc(x.kind==='in'?'Entrada':'Salida')} · ${money(x.amount)} · ${esc(x.status)}<div class="mini">${esc(x.reason)}</div></div>`).join('')||'<p class="muted">Sin movimientos.</p>'}<button class="outline" data-action="view-report" data-id="${p.id}">Ver corte tipo ticket</button><button class="outline" data-action="download-report" data-id="${p.id}">Descargar PDF</button></div>`;
  target.scrollIntoView({behavior:'smooth',block:'start'});
}

async function showProducts(){
  const ps=await api('/products');state.products=ps;
  const categories=new Map();
  for(const p of ps)categories.set(p.category,(categories.get(p.category)||0)+1);
  const categoryHtml=`<div class="card"><h3>Categorías del menú</h3><p class="mini">Para borrar una categoría con productos, debes elegir a cuál moverlos. No se eliminarán artículos ni ventas.</p>${
    Array.from(categories).sort((a,b)=>a[0].localeCompare(b[0],'es')).map(([name,count])=>
      '<div class="lineitem row"><span><b>'+esc(name)+'</b> · '+count+' producto(s)</span>'+
      '<button class="red small" data-action="delete-category" data-name="'+esc(name)+'" data-count="'+count+'">Borrar categoría</button></div>').join('')||'<p class="mini">Sin categorías todavía.</p>'
  }</div>`;
  const importHtml=`<div class="card"><h3>Menú de las cuatro fotografías</h3><p class="mini">61 productos organizados en 12 categorías, con precios en pesos mexicanos. Revisa los cambios antes de aplicar; los productos ya registrados se actualizan, no se duplican.</p><button class="outline" data-action="photo-preview">Revisar menú fotografiado</button><div id="photo-menu-preview" style="margin-top:10px"></div></div>`;
  return categoryHtml+importHtml+`<div class="card"><h3>Agregar producto al menú</h3><p>Registra precios reales; no se cargan productos ficticios.</p><form id="product-add" class="formrow"><label>Nombre<input name="name" required></label><div class="formgrid"><label>Categoría<input name="category" value="General"></label><label>Destino<select name="station"><option value="kitchen">Cocina</option><option value="bar">Barra</option></select></label><label>Precio ($)<input name="price" type="number" min="0" step="0.01" required></label><label>Existencias iniciales<input name="stock" type="number" min="0" value="0"></label></div><label class="inline"><input name="inventory_enabled" type="checkbox" style="width:auto"> Control individual de inventario</label><button class="primary">Agregar producto</button></form></div>
  <h2 class="section">Menú y personalización (${ps.length})</h2><div class="cards">${ps.map(p=>`<details class="card product-config-admin"><summary><div class="row"><div><h3>${esc(p.name)}</h3><div class="mini">${esc(p.category)} · ${p.station==='kitchen'?'Cocina':'Barra'} · Base ${money(p.price)}</div></div>${pill(p.status)}</div></summary><div class="product-config-content">
    <div class="row"><span>Precio vigente</span><b class="price">${money(p.current_price??p.price)}</b></div>${p.promotion?`<div class="note">Promoción activa: ${esc(p.promotion.name)}</div>`:''}<div class="row"><button class="outline small" data-action="edit-product" data-id="${p.id}">Editar producto</button><button class="red small" data-action="archive-product" data-id="${p.id}">Eliminar producto</button></div>
    <h4>Presentaciones</h4>${(p.variants||[]).map(v=>`<div class="lineitem row"><span>${esc(v.name)} · ${money(v.price)}</span><button class="ghost small" data-action="toggle-variant" data-id="${v.id}" data-active="${!v.active}">${v.active?'Desactivar':'Activar'}</button></div>`).join('')||'<p class="mini">Sin presentaciones adicionales.</p>'}
    <form id="variant-add-${p.id}" class="formrow"><div class="formgrid"><label>Nueva presentación<input name="name" required maxlength="100" placeholder="Grande, chico, familiar…"></label><label>Precio ($)<input name="price" type="number" min="0" step="0.01" required></label></div><button class="outline small">Agregar presentación</button></form>
    <h4>Grupos de modificadores</h4>${(p.modifier_groups||[]).map(g=>`<div class="modifier-admin-group"><div class="row"><b>${esc(g.name)}</b><span class="mini">Seleccionar ${g.min_select}–${g.max_select}</span></div>${g.options.map(o=>`<div class="lineitem row"><span>${esc(o.name)} · +${money(o.price_delta)}</span><button class="ghost small" data-action="toggle-option" data-id="${o.id}" data-active="${!o.active}">${o.active?'Desactivar':'Activar'}</button></div>`).join('')}<form id="modifier-option-add-${g.id}" class="formrow"><div class="formgrid"><label>Opción<input name="name" required maxlength="100" placeholder="Queso extra, aguacate…"></label><label>Precio adicional ($)<input name="price_delta" type="number" min="0" step="0.01" value="0" required></label></div><button class="outline small">Agregar opción</button></form></div>`).join('')||'<p class="mini">Sin modificadores configurados.</p>'}
    <form id="modifier-group-add-${p.id}" class="formrow"><div class="formgrid"><label>Nuevo grupo<input name="name" required maxlength="100" placeholder="Extras, guarnición…"></label><label>Mínimo de opciones<input name="min_select" type="number" min="0" max="10" value="0"></label><label>Máximo de opciones<input name="max_select" type="number" min="1" max="10" value="1"></label></div><button class="outline small">Crear grupo de modificadores</button></form>
    <h4>Promociones por día y horario</h4>${(p.promotions||[]).map(pr=>`<div class="lineitem"><div class="row"><b>${esc(pr.name)}</b><button class="ghost small" data-action="toggle-promotion" data-id="${pr.id}" data-active="${!pr.active}">${pr.active?'Desactivar':'Activar'}</button></div><div class="mini">${pr.price!==null?`Precio $${pr.price}`:`Descuento ${pr.discount_percent}%`} · Días ${esc(pr.weekdays)} · ${esc(pr.start_time)}–${esc(pr.end_time)}${pr.start_date?' · Desde '+esc(pr.start_date):''}${pr.end_date?' · Hasta '+esc(pr.end_date):''}</div></div>`).join('')||'<p class="mini">Sin promociones configuradas.</p>'}
    <form id="promotion-add-${p.id}" class="formrow"><label>Nombre de promoción<input name="name" required maxlength="120" placeholder="Precio especial de fin de semana"></label><div class="formgrid"><label>Precio promocional ($)<input name="price" type="number" min="0" step="0.01"></label><label>O descuento (%)<input name="discount_percent" type="number" min="1" max="90"></label><label>Días (0=lun … 6=dom)<input name="weekdays" value="0,1,2,3,4,5,6" required></label><label>Hora inicial<input name="start_time" type="time" value="00:00" required></label><label>Hora final<input name="end_time" type="time" value="23:59" required></label><label>Fecha inicial (opcional)<input name="start_date" type="date"></label><label>Fecha final (opcional)<input name="end_date" type="date"></label></div><p class="mini">Completa solo precio promocional o porcentaje de descuento. Si dejas las fechas vacías, la promoción se repite según los días y horario.</p><button class="outline small">Crear promoción</button></form>
  </div></details>`).join('')||'<div class="empty">Todavía no hay productos registrados.</div>'}</div><div class="card" style="margin-top:16px"><h3>Productos eliminados</h3><p class="mini">Los productos eliminados desaparecen del menú, pero sus ventas e historial se conservan.</p><button class="outline small" data-action="list-archived-products">Consultar eliminados</button><div id="archived-products-list"></div></div>`;
}
async function showInventory(){const [s,ps]=await Promise.all([api('/settings'),api('/products')]);state.products=ps;return `<div class="card"><h3>Control general de inventario</h3><div class="row"><span>Estado actual</span>${pill(s.inventory_on?'open':'pending')}</div><p>${s.inventory_on?'Se descuentan unidades solo de productos con control individual activado.':'Desactivado: puedes vender sin descontar existencias. Los bloqueos manuales siguen vigentes.'}</p><button class="${s.inventory_on?'red':'primary'}" data-action="toggle-inventory" data-on="${!s.inventory_on}">${s.inventory_on?'Desactivar inventario general':'Activar tras revisar existencias'}</button></div><h2 class="section">Control por producto</h2><div class="cards">${ps.map(p=>`<div class="card"><div class="row"><h3>${esc(p.name)}</h3>${pill(p.status)}</div><p>Existencias registradas: <b>${p.stock}</b> · Control individual: <b>${p.inventory_enabled?'Sí':'No'}</b></p><div class="inline"><button class="outline small" data-action="edit-product" data-id="${p.id}">Configurar</button><button class="ghost small" data-action="restock" data-id="${p.id}">＋ Reponer</button></div></div>`).join('')||'<div class="empty">Agrega productos al menú para configurar su inventario.</div>'}</div>`}

async function showUsers(){
  const users=await api('/users');state.data.users=users;
  const owner=state.user?.role==='owner';
  const registration=owner?
    '<div class="card staff-pin-setup"><h3>Registrar personal</h3>'+
    '<p>Solo se necesita nombre y PIN de seis números. El Propietario asigna el perfil de trabajo.</p>'+
    '<form id="staff-add" class="formrow staff-registration-form"><div class="formgrid">'+
    '<label>Nombre del empleado<input name="name" required minlength="2" maxlength="100" autocomplete="off" placeholder="Nombre y apellido"></label>'+
    '<label>Perfil asignado<select name="role"><option value="waiter">Mesero</option><option value="cashier">Caja</option><option value="kitchen">Cocina</option><option value="bar">Barra</option><option value="admin">Administrador</option></select></label>'+
    '<label class="staff-pin-field">PIN personal<input name="pin" type="password" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="new-password" required placeholder="6 números"></label>'+
    '</div><button class="primary staff-register-button" type="submit">Registrar empleado</button></form>'+
    '<p class="mini">Cada PIN debe ser exclusivo de una persona; no uses fechas o secuencias fáciles.</p></div>':
    '<p class="note">La asignación de PIN y el registro de empleados son exclusivos del Propietario.</p>';
  const cards=users.map(u=>{
    const status=u.has_pin?
      '<span class="staff-pin-status">● PIN activado</span>':
      '<span class="staff-pin-status pending">● Falta configurar PIN</span>';
    const reset=owner?
      '<form id="staff-pin-'+Number(u.id)+'" class="staff-pin-form">'+
      '<label>Asignar PIN de 6 números<input name="pin" type="password" inputmode="numeric" minlength="6" maxlength="6" pattern="[0-9]{6}" autocomplete="new-password" placeholder="••••••" required></label>'+
      '<button class="outline small" type="submit">'+(u.has_pin?'Cambiar PIN':'Activar PIN')+'</button></form>':'';
    const toggle=owner&&u.role!=='owner'?
      '<div class="inline staff-user-actions"><button class="ghost small" data-action="toggle-user" data-id="'+Number(u.id)+'" data-active="'+(!u.active)+'">'+(u.active?'Desactivar':'Activar')+'</button></div>':'';
    return '<div class="card staff-user-card"><h3>'+esc(u.display)+'</h3><p>'+esc(labels[u.role]||u.role)+'</p>'+
      pill(u.active?'open':'rejected')+'<div style="margin-top:8px">'+status+'</div>'+reset+toggle+'</div>';
  }).join('');
  return registration+'<h2 class="section">Personal y accesos</h2>'+
    '<div class="cards staff-users-grid">'+cards+'</div>'+
    (owner?'<p class="mini">Al cambiar tu propio PIN se cierra tu sesión para proteger el acceso. Vuelve a entrar con el nuevo código.</p>':'');
}
async function showApprovals(){const rs=await api('/requests');return `<div class="cards">${rs.map(r=>`<div class="card"><div class="row"><h3>${r.kind==='reopen'?'Reapertura':'Cancelación'} · #${r.account_id}</h3>${pill(r.status)}</div><p>${esc(r.account)} · ${esc(r.requester)}</p><p>${esc(r.reason)}</p>${r.kind==='cancel'?`<p class="mini">Producto #${r.item_id} · ${r.qty} unidad(es)</p>`:''}${r.status==='pending'?`<div class="inline"><button class="primary small" data-action="approve" data-id="${r.id}" data-kind="${r.kind}" data-ok="true">Autorizar</button><button class="ghost small" data-action="approve" data-id="${r.id}" data-kind="${r.kind}" data-ok="false">Rechazar</button></div>`:''}</div>`).join('')||'<div class="empty">No hay solicitudes.</div>'}</div>`}
async function showAudit(){const rs=await api('/audit');return `<div class="card table-wrap"><table><thead><tr><th>Fecha</th><th>Usuario</th><th>Acción</th><th>Detalle</th></tr></thead><tbody>${rs.map(x=>`<tr><td>${esc(new Date(x.date).toLocaleString('es-MX'))}</td><td>${esc(x.actor)}</td><td>${esc(x.action)}</td><td class="mini">${esc(JSON.stringify(x.detail))}</td></tr>`).join('')}</tbody></table></div>`}
async function showOverview(){
  const data=await api('/overview');
  // El panel utiliza métricas agregadas; los listados completos se cargan al abrir cada módulo.
  // Los accesos solo navegan a módulos ya existentes, sin alterar API o permisos.
  const drawings={
    list:'<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h9M8 13h9M8 17h6"/>',
    table:'<rect x="3" y="5" width="18" height="5" rx="1"/><path d="M6 10v10M18 10v10M3 16h18"/>',
    food:'<path d="M4 3v9a3 3 0 0 0 6 0V3M7 3v18M18 3c-3 4-3 8 0 10h2V3M18 13v8"/>',
    kitchen:'<path d="M6 14a4 4 0 0 1-2-7 5 5 0 0 1 8-2 5 5 0 0 1 8 2 4 4 0 0 1-2 7v6H6z"/><path d="M6 17h12"/>',
    cash:'<rect x="2" y="5" width="20" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M5 9h1M18 15h1"/>',
    box:'<path d="m12 2 9 5-9 5-9-5zM3 7v10l9 5 9-5V7M12 12v10"/>',
    people:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5v1"/>',
    tips:'<circle cx="12" cy="12" r="9"/><path d="M15 9c-1-2-6-2-6 1 0 3 6 1 6 4 0 3-5 3-6 1M12 6v12"/>',
    shield:'<path d="M12 2 4 6v6c0 5 3 8 8 10 5-2 8-5 8-10V6z"/><path d="m9 12 2 2 4-4"/>',
    audit:'<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h6"/>'
  };
  const icon=name=>'<svg viewBox="0 0 24 24" width="25" height="25" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+drawings[name]+'</svg>';
  const shortcuts=[
    ['all','Cuentas','Cuentas abiertas y seguimiento','list'],
    ['tables','Mesas','Configuración y disponibilidad','table'],
    ['tickets','Estaciones','Comandas de cocina y barra','kitchen'],
    ['cashier','Cobros','Pagos pendientes y descuentos','cash'],
    ['cash','Caja','Turnos, cortes y movimientos','cash'],
    ['products','Menú','Productos, variantes y promociones','food'],
    ['inventory','Inventario','Existencias y reposiciones','box'],
    ['users','Personal','Empleados y permisos','people'],
    ['tips','Propinas','Fondo común y repartos','tips'],
    ['approvals','Autorizaciones','Solicitudes y cancelaciones','shield'],
    ['audit','Auditoría','Registro de operaciones','audit']
  ];
  shortcuts.push(['reservations','Reservaciones','Confirmar llegadas y abrir cuentas','list'],['prepconfig','Tiempos','Límites de cocina y barra','kitchen']);
  if(state.user.role==='owner')shortcuts.push(['analytics','Estadísticas','Ventas, productos y tiempos reales','cash']);
  return `<div class="premium-dashboard">
    <div class="premium-dashboard-heading">
      <div><p class="premium-eyebrow">CENTRO DE CONTROL</p><h2>Operación del restaurante</h2>
      <p>Revisa el servicio y entra directamente al módulo que necesitas.</p></div>
      <span class="premium-dashboard-live">● Información actualizada</span>
    </div>
    <div class="statgrid">${stat('Cuentas activas',data.active_accounts)}${stat('Por cobrar',data.pending_accounts)}${stat('Comandas nuevas',data.new_tickets)}${stat('Productos',data.products)}</div>
    <div class="premium-dashboard-heading premium-dashboard-second"><div><p class="premium-eyebrow">GESTIÓN Y ADMINISTRACIÓN</p><h2>Todos los módulos</h2></div></div>
    <div class="premium-shortcut-grid">
    ${shortcuts.map(([id,label,desc,drawing])=>`<button type="button" class="premium-shortcut" data-nav="${id}" aria-label="Abrir ${label}"><span class="premium-shortcut-icon">${icon(drawing)}</span><strong>${label}</strong><small>${desc}</small><span class="premium-shortcut-arrow" aria-hidden="true">›</span></button>`).join('')}
    </div>
    <div class="grid2 premium-dashboard-footer">
      <div class="card"><h3>Operación en vivo</h3><p>Consulta cuentas abiertas y el estado de los pedidos.</p><button class="primary" data-nav="all">Supervisar cuentas</button></div>
      <div class="card"><h3>Control de inventario</h3><p>Revisa existencias, productos bloqueados y reposiciones.</p><button class="outline" data-nav="inventory">Abrir inventario</button></div>
    </div>
  </div>`;
}
function formObj(form){return Object.fromEntries(new FormData(form).entries())}
function ask(message){const v=prompt(message);return v&&v.trim()?v.trim():null}
async function act(action,btn){if(await mitoteFeatureAction(action,btn))return;const id=Number(btn.dataset.id);switch(action){case 'push-enable':await enablePush();await render();break;
case 'push-test':await api('/push/test','POST',{});toast('Prueba enviada. Bloquea la pantalla para comprobar.');break;
case 'push-disable':await disablePush();await render();break;
case 'ack-ready':{
  if(!Number.isInteger(id)||id<1)throw Error('Aviso inválido');
  await api('/waiter/ready-alerts/'+id+'/ack','POST',{});
  state.data.readyAlerts=(state.data.readyAlerts||[]).map(x=>x.event_id===id?{...x,acknowledged:true,received_at:new Date().toISOString()}:x);
  toast('Recepción confirmada. Marca Entregado cuando lo hayas llevado a la mesa.');
  await render();
  break;
}
case 'deliver-ready':{
  if(!Number.isInteger(id)||id<1)throw Error('Aviso inválido');
  await api('/waiter/ready-alerts/'+id+'/deliver','POST',{});
  state.data.readyAlerts=(state.data.readyAlerts||[]).filter(x=>x.event_id!==id);
  toast('Entrega al cliente confirmada y registrada');
  await render();
  break;
}
case 'load-account-history':{
  const accountId=Number(btn.dataset.id);
  if(!Number.isSafeInteger(accountId)||accountId<1)throw Error('Cuenta inválida');
  const rows=await api(`/accounts/${accountId}/history`);
  const target=document.getElementById('account-history-'+accountId);
  if(target)target.innerHTML=accountHistoryMarkup(rows);
  btn.textContent='Actualizar historial';
  break;
}
case 'view-report':await showCashTicket(id);break;case 'period-detail':await showPeriodDetail(id);break;case 'hide-period-detail':{const el=$('#period-detail');if(el)el.innerHTML='';break}case 'void-account':{const reason=ask('Motivo para anular esta cuenta sin saldo:');if(!reason)return;if(!confirm('¿Confirmas la anulación? Esta acción quedará registrada.'))return;await api(`/accounts/${id}/void`,'POST',{reason});toast('Cuenta anulada y archivada');await go('all');break}case 'enable-sound':state.alarmArmed=true;beep('ready');toast('Sonido potente activado en esta pantalla');break;case 'reset-user':{const secret=ask(btn.dataset.role==='waiter'?'Nuevo PIN de 4 dígitos:':'Nueva contraseña (mínimo 8 caracteres):');if(!secret)return;await api(`/users/${id}`,'PATCH',{secret});toast('Clave cambiada; sesiones anteriores revocadas');await render();break}case 'toggle-user':{if(!confirm('¿Confirmar cambio de acceso?'))return;await api(`/users/${id}`,'PATCH',{active:btn.dataset.active==='true'});toast('Acceso actualizado y sesiones revocadas');await render();break}case 'open-account':state.account=id;state.draft=[];await go('orders');break;case 'admin-account':{const a=await api('/accounts/'+id);if(!state.data.users)state.data.users=await api('/users');$('#content').innerHTML=`<button class="outline small" data-nav="all">← Volver</button><h2 class="section">Cuenta #${id}</h2>${await accountDetailHtml(id,true)}`;break}case 'remove-line':state.draft.splice(id,1);persistWorkState();updateDraftUI();toast('Producto retirado del borrador');break;
case 'repeat-item':{const accountId=Number(btn.dataset.account);const qty=Number(prompt('¿Cuántas unidades deseas repetir?','1'));if(!Number.isInteger(qty)||qty<1||qty>100)throw Error('Cantidad inválida');if(!confirm('Se enviará una nueva comanda con la misma presentación, extras y notas. ¿Continuar?'))return;await api(`/accounts/${accountId}/items/${id}/repeat`,'POST',{qty,client_key:key()});toast('Producto repetido y enviado a su estación');await refreshAccountPanel(accountId,['owner','admin'].includes(state.user.role));break;}
case 'toggle-variant':await api(`/product-variants/${id}`,'PATCH',{active:btn.dataset.active==='true'});toast('Presentación actualizada');await render();break;
case 'toggle-option':await api(`/modifier-options/${id}`,'PATCH',{active:btn.dataset.active==='true'});toast('Modificador actualizado');await render();break;
case 'toggle-promotion':await api(`/promotions/${id}`,'PATCH',{active:btn.dataset.active==='true'});toast('Promoción actualizada');await render();break;
case 'send-order':{
  if(!state.draft.length)return;
  const accountId=state.account,scope='order-'+accountId;
  const items=state.draft.map(x=>({product_id:x.product_id,qty:x.qty,note:x.note,
    variant_id:x.variant_id,modifier_option_ids:x.modifier_option_ids,
    prep_minutes:x.prep_minutes,release_after_minutes:x.release_after_minutes}));
  const client_key=pendingOperationKey(scope,items);
  const result=await api(`/accounts/${accountId}/orders`,'POST',{client_key,items});
  clearPendingOperation(scope);
  state.draft=[];persistWorkState();
  toast(result.duplicate?'Comanda ya registrada; no se duplicó':'Comanda enviada a las estaciones');
  await render();break;
}case 'bill':if(!confirm('¿Enviar la cuenta a caja? No podrás agregar más productos.'))return;await api(`/accounts/${id}/request-bill`,'POST',{});toast('Cuenta enviada a caja');await go('accounts',{replace:true});break;case 'cancel-request':{const reason=ask('Motivo de la cancelación:');if(!reason)return;const qty=Number(prompt(`Cantidad a cancelar (máximo ${btn.dataset.qty}):`,'1'));if(!Number.isInteger(qty)||qty<1||qty>Number(btn.dataset.qty))throw Error('Cantidad inválida');await api(`/accounts/${btn.dataset.account}/requests`,'POST',{kind:'cancel',reason,item_id:id,qty});toast('Solicitud enviada a administración');break}case 'reopen-request':{const reason=ask('Motivo de reapertura:');if(!reason)return;await api(`/accounts/${id}/requests`,'POST',{kind:'reopen',reason});toast('Solicitud de reapertura enviada');break}case 'ticket-status':await api(`/tickets/${id}/status`,'POST',{status:btn.dataset.status});toast('Estado de comanda actualizado');await render();break;case 'approve':{const approve=btn.dataset.ok==='true';let return_stock=false;if(approve&&btn.dataset.kind==='cancel')return_stock=confirm('¿Devolver estas unidades al inventario? Solo si no fueron preparadas y realmente regresan a existencias.');await api(`/requests/${id}/decide`,'POST',{approve,return_stock});toast('Solicitud resuelta');await render();break}case 'void-discount':{const accountId=Number(btn.dataset.account);const reason=ask('Motivo para retirar el descuento:');if(!reason)return;if(!confirm('¿Retirar este descuento? La operación quedará registrada en auditoría.'))return;await api(`/accounts/${accountId}/discounts/${id}/void`,'POST',{reason});toast('Descuento retirado');await render();break;}case 'close-account':if(!confirm('¿Confirmar cierre de la cuenta completamente pagada?'))return;await api(`/accounts/${id}/close`,'POST',{});toast('Cuenta cerrada');await render();break;case 'reopen-closed':{const reason=ask('Motivo para reabrir la cuenta cerrada:');if(!reason)return;if(!confirm('¿Reabrir la cuenta? Los pagos anteriores se conservarán y la mesa anterior no se ocupará automáticamente.'))return;await api(`/accounts/${id}/reopen-closed`,'POST',{reason});toast('Cuenta reabierta; puedes asignarle una mesa desde su detalle');await refreshAccountPanel(id,true);break}case 'print-ticket':{const a=await api('/accounts/'+id);const win=window.open('','_blank');if(!win){toast('Permite ventanas emergentes para mostrar el ticket',true);return}win.document.write(ticketHtml(a));win.document.close();break}case 'print-prebill':{const win=window.open('about:blank','_blank');if(!win){toast('Permite ventanas emergentes para mostrar la pre-cuenta',true);return}try{const res=await fetch(`/api/accounts/${id}/prebill.pdf`,{headers:{Authorization:'Bearer '+state.token},cache:'no-store'});if(!res.ok)throw Error('No se pudo generar la pre-cuenta');const blob=await res.blob();win.location.href=URL.createObjectURL(blob);setTimeout(()=>URL.revokeObjectURL(win.location.href),60000)}catch(err){win.close();throw err}break}case 'download-report':case 'download-report-original':case 'download-ticket':case 'download-prebill':case 'share-ticket':{
  const cut=action==='download-report'||action==='download-report-original';
  const original=action==='download-report-original';
  const prebill=action==='download-prebill';
  const path=cut?'/api/cash/periods/'+id+'/'+(original?'report.pdf':'ticket.pdf'):
    '/api/accounts/'+id+'/'+(prebill?'prebill':'ticket')+'.pdf';
  const blob=await fetchProtectedPdf(path);
  const filename=cut?'ElMitote_Corte_'+id+(original?'_Original':'_Ticket')+'.pdf':
    'ElMitote_'+(prebill?'PreCuenta_':'Ticket_')+id+'.pdf';
  if(action==='share-ticket'&&typeof File==='function'&&navigator.canShare){
    const file=new File([blob],filename,{type:'application/pdf'});
    if(navigator.canShare({files:[file]})){
      await navigator.share({files:[file],title:'Ticket El Mitote #'+id});
      break;
    }
  }
  savePdfToDevice(blob,filename);
  toast('Descarga iniciada: '+filename);
  if(action==='share-ticket')toast('Descarga iniciada. Comparte el archivo desde WhatsApp.');
  break;
}
case 'fcm-test':{const r=await api('/fcm/test','POST',{});toast(r.message||'Prueba solicitada');break}case 'toggle-inventory':{const on=btn.dataset.on==='true';if(on&&!confirm('Antes de activar inventario debes revisar y confirmar las existencias de TODOS los productos controlados. ¿Ya realizaste el recuento?'))return;await api('/settings','PUT',{inventory_on:on,confirmed_recount:on});toast('Configuración de inventario actualizada');await render();break}case 'restock':{const p=state.products.find(x=>x.id===id);const delta=Number(prompt(`¿Cuántas unidades agregar a ${p.name}?`,'5'));if(!Number.isInteger(delta)||delta<1)throw Error('Cantidad inválida');const reason=ask('Motivo de la reposición:');if(!reason)return;await api(`/products/${id}/restock`,'POST',{delta,reason,expected_version:p.version});toast('Existencias actualizadas');await render();break}case 'delete-category':{
  const name=btn.dataset.name,count=Number(btn.dataset.count);
  const target=prompt('Borrar la categoría "'+name+'". ¿A qué otra categoría mover sus '+count+' producto(s)?','General');
  if(target===null)return;
  const destination=target.trim();
  if(!destination||destination.toLocaleLowerCase('es')===name.toLocaleLowerCase('es'))throw Error('Selecciona otra categoría válida');
  if(!confirm('¿Eliminar categoría "'+name+'" y mover sus productos a "'+destination+'"? No se borrarán ventas ni artículos.'))return;
  const result=await api('/categories/remove','POST',{name,destination,expected_visible_count:count});
  toast('Categoría eliminada; '+result.moved+' productos trasladados');
  await render();break;
}
case 'photo-preview':{
  const p=await api('/menu/photo-preview');
  state.data.photoMenuPreview=p;
  const el=$('#photo-menu-preview');
  if(!el)return;
  const changes=p.rows.filter(x=>x.status!=='unchanged');
  const conflicts=p.counts.ambiguous+p.counts.archived_conflict;
  el.innerHTML='<div class="note">Menú de '+p.total+' productos. Nuevos: '+p.counts.create+
     ' · Para actualizar: '+p.counts.update+' · Sin cambios: '+p.counts.unchanged+
     (conflicts?' · Conflictos: '+conflicts:'')+'</div>'+
    changes.map(x=>'<div class="lineitem"><b>'+esc(x.name)+'</b> · '+esc(x.category)+
      ' · <b>'+money(x.price)+'</b>'+
      (x.price_changes?'<span class="mini"> · Antes '+money(x.previous_price)+'</span>':'')+
      '<div class="mini">'+({create:'Producto nuevo',update:'Modificar registro existente',ambiguous:'Posible duplicado',archived_conflict:'Producto eliminado'}[x.status]||x.status)+'</div></div>').join('')+
    (conflicts?'<div class="note danger-note">Resuelve los posibles duplicados antes de aplicar.</div>':
    '<button class="primary" data-action="photo-apply">Confirmar y cargar los '+p.total+' productos</button>');
  el.scrollIntoView({behavior:'smooth',block:'nearest'});break;
}
case 'photo-apply':{
  const p=state.data.photoMenuPreview;
  if(!p)throw Error('Primero revisa el menú');
  const differences=p.rows.filter(x=>x.price_changes);
  const explanation=differences.slice(0,12).map(x=>x.name+': '+money(x.previous_price)+' → '+money(x.price)).join('\n');
  if(!confirm('Se crearán '+p.counts.create+' productos y se actualizarán '+p.counts.update+
    '. Los cambios de precio detectados son:\n'+explanation+
    '\n\n¿Confirmas todos los precios de las fotografías y la importación?'))return;
  const result=await api('/menu/photo-apply','POST',{preview_token:p.preview_token,confirm:true});
  state.data.photoMenuPreview=null;
  toast('Menú cargado: '+result.created+' nuevos y '+result.updated+' actualizados');
  await render();break;
}
case 'archive-product':{
  const p=state.products.find(x=>x.id===id);
  if(!p)throw Error('Producto inexistente; actualiza el catálogo');
  const reason=ask('¿Por qué eliminas '+p.name+' del menú? (mínimo 3 caracteres)');
  if(!reason)return;
  if(reason.length<3)throw Error('Escribe un motivo de al menos 3 caracteres');
  if(!confirm('¿Eliminar "'+p.name+'" del menú? Las ventas anteriores y el historial de cortes no se borrarán.'))return;
  await api('/products/'+id+'/archive','POST',{expected_version:p.version,reason});
  toast('Producto eliminado del menú y archivado de forma segura');
  await render();break;
}
case 'list-archived-products':{
  const rows=await api('/products/archived');
  const target=$('#archived-products-list');
  if(!target)return;
  target.innerHTML=rows.length?rows.map(p=>'<div class="lineitem row"><div><b>'+esc(p.name)+'</b><div class="mini">Eliminado: '+
    esc(new Date(p.archived_at).toLocaleString('es-MX'))+' · '+esc(p.reason)+'</div></div>'+
    '<button class="outline small" data-action="restore-product" data-id="'+p.id+'" data-version="'+p.version+'">Restaurar</button></div>').join(''):'<p class="mini">Sin productos eliminados.</p>';
  break;
}
case 'restore-product':{
  if(!confirm('¿Restaurar este producto al menú con su configuración anterior?'))return;
  await api('/products/'+id+'/restore','POST',{expected_version:Number(btn.dataset.version)});
  toast('Producto restaurado al menú');
  await render();break;
}
case 'edit-product':{const p=state.products.find(x=>x.id===id);const name=ask('Nombre del producto:')||p.name;const price=prompt('Precio ($):',p.price);if(price===null)return;const stock=prompt('Existencias reales:',String(p.stock));if(stock===null)return;const blocked=confirm('¿Bloquear manualmente la venta de este producto?');const inventory_enabled=confirm('¿Activar el control de inventario individual para este producto?');if(!Number.isInteger(Number(stock))||Number(stock)<0)throw Error('Existencias inválidas');await api(`/products/${id}`,'PUT',{name,price,stock:Number(stock),blocked,inventory_enabled,category:p.category,station:p.station,active:p.active,expected_version:p.version});toast('Producto actualizado');await render();break}case 'move-decision':await api(`/cash/movements/${id}/decide`,'POST',{approve:btn.dataset.approve==='true'});toast('Movimiento resuelto');await render();break;default:throw Error('Acción no disponible')}}
function ticketHtml(a){
  const discountRows=(a.discounts||[]).filter(d=>d.status==='applied').map(d=>`<tr><td>Descuento ${d.scope==='account'?'general':'por producto'+(d.item_name?' · '+esc(d.item_name):'')} · ${esc(d.reason)}</td><td style="text-align:right">-${money(d.amount)}</td></tr>`).join('');
  const tipTotal=(a.tips||[]).reduce((s,t)=>s+Number(t.amount||0),0);
  return `<!doctype html><html lang="es"><meta charset="utf-8"><title>Ticket #${a.id}</title><style>body{font:14px Arial,sans-serif;color:#111;max-width:350px;margin:20px auto;padding:12px}h2{text-align:center}img{display:block;margin:auto;max-width:140px}table{width:100%;border-collapse:collapse}td{padding:6px 0;border-bottom:1px dotted #bbb}footer{text-align:center;margin-top:20px}@media print{button{display:none}}</style><img src="/static/assets/logo.webp"><h2>EL MITOTE RESTAURANTE</h2><p style="text-align:center">Carretera Progreso Ixmiquilpan, Xochitlán, Hidalgo, C. P. 42740<br>Tel. 772 200 3943</p><hr><p><b>Cuenta #${a.id}</b> · ${esc(a.name)}<br>Mesero: ${esc(a.owner)}<br>Fecha de consulta: ${new Date().toLocaleString('es-MX')}</p><table>${a.items.map(i=>`<tr><td>${i.qty-i.cancelled_qty} × ${esc(i.name)}${i.variant_name?' · '+esc(i.variant_name):''}${i.modifiers?.length?`<br><small>Extras: ${i.modifiers.map(m=>esc(m.name)).join(', ')}</small>`:''}${i.note?`<br><small>Nota: ${esc(i.note)}</small>`:''}</td><td style="text-align:right">${money(Number(i.price)*(i.qty-i.cancelled_qty))}</td></tr>`).join('')}${discountRows}</table><p>Subtotal ${money(a.gross_total??a.total)}<br>Descuentos -${money(a.discount_total||0)}<br><b>Total ${money(a.total)}</b><br>Pagado ${money(a.paid)}<br><b>Saldo ${money(a.due)}</b></p><p>Pagos: ${a.payments.map(p=>`${esc(p.method)} ${money(p.amount)}${Number(p.tip)>0?` + propina ${money(p.tip)}`:''}`).join(' · ')||'Sin pagos'}</p><p>Propinas registradas: ${money(tipTotal)}</p><p>${a.status==='paid'?'PAGADO':'PENDIENTE'}</p><footer>Muchas gracias por tu visita, te esperamos pronto<br><small>Comprobante de consumo · No fiscal</small></footer><p><button onclick="window.print()">Imprimir / Guardar como PDF</button></p></html>`;
}
async function submitForm(form){if(await mitoteFeatureForm(form))return;const d=formObj(form);
if(form.id.startsWith('staff-pin-')){
  const id=Number(form.id.slice('staff-pin-'.length));
  if(!Number.isSafeInteger(id)||id<1)throw Error('Personal inválido');
  const response=await api('/staff/'+id+'/pin','PATCH',{pin:d.pin});
  if(response.owner_pin_changed){
    logout('expired');
    location.replace('/');
    return;
  }
  toast('PIN actualizado. El empleado ya puede entrar con el teclado numérico.');
  await render();return;
}
if(form.id.startsWith('table-edit-')){const id=Number(form.id.slice('table-edit-'.length));await api(`/tables/${id}`,'PUT',{name:d.name,area:d.area,capacity:Number(d.capacity),sort_order:Number(d.sort_order||0),active:d.active==='true'});toast('Configuración de mesa actualizada');await render();return}switch(form.id){
case 'add-line':{
  const productId=Number(d.product_id);const p=state.products.find(x=>x.id===productId);if(!p)throw Error('Producto no encontrado');
  const fd=new FormData(form);const optionIds=fd.getAll('modifier_option_ids').map(Number);
  for(const group of $$('.modifier-group',form)){const count=group.querySelectorAll('input[name="modifier_option_ids"]:checked').length;const min=Number(group.dataset.min||0),max=Number(group.dataset.max||10);if(count<min||count>max)throw Error(`Revisa el grupo de opciones: selecciona entre ${min} y ${max}`)}
  const variantId=d.variant_id?Number(d.variant_id):null;const variant=(p.variants||[]).find(v=>v.id===variantId);
  const modifierOptions=(p.modifier_groups||[]).flatMap(g=>g.options||[]).filter(o=>optionIds.includes(o.id));
  const modifierNames=modifierOptions.map(o=>o.name);const unitPrice=estimateDraftPrice(p,variantId,optionIds);
  state.draft.push({product_id:p.id,name:p.name,station:p.station,qty:Number(d.qty),note:d.note||'',variant_id:variantId,variant_name:variant?.name||'',modifier_option_ids:optionIds,modifier_names:modifierNames,prep_minutes:d.prep_minutes!==''?Number(d.prep_minutes):15,release_after_minutes:d.release_after_minutes!==''?Number(d.release_after_minutes):0,unit_price:unitPrice,promotion_name:p.promotion?.name||''});
  persistWorkState();updateDraftUI();const qtyField=form.querySelector('[name="qty"]');if(qtyField)qtyField.value='1';const noteField=form.querySelector('[name="note"]');if(noteField)noteField.value='';toast('Producto agregado al borrador');break;
}
case 'account-rename-form':{
  const accountId=Number(d.account_id);
  await api(`/accounts/${accountId}/rename`,'POST',{name:d.name,reason:d.reason});
  toast('Nombre de cuenta actualizado');
  await refreshAccountPanel(accountId,['owner','admin'].includes(state.user.role));
  break;
}
case 'account-table-form':{
  const accountId=Number(d.account_id);
  await api(`/accounts/${accountId}/transfer-table`,'POST',{table_id:d.table_id?Number(d.table_id):null,reason:d.reason});
  toast('Mesa de la cuenta actualizada');
  await refreshAccountPanel(accountId,['owner','admin'].includes(state.user.role));
  break;
}
case 'account-merge-form':{
  const accountId=Number(d.account_id);
  const targetId=Number(d.target_account_id);
  if(!confirm(`¿Unir la cuenta #${accountId} con la cuenta #${targetId}? La cuenta de origen quedará archivada como unida.`))return;
  await api(`/accounts/${accountId}/merge`,'POST',{target_account_id:targetId,reason:d.reason});
  toast(`Cuenta #${accountId} unida con #${targetId}`);
  if(['owner','admin'].includes(state.user.role))await go('all');else await go('accounts');
  break;
}
case 'account-split-form':{
  const accountId=Number(d.account_id);
  const targetId=d.target_account_id?Number(d.target_account_id):null;
  const tableId=(!targetId&&d.table_id)?Number(d.table_id):null;
  await api(`/accounts/${accountId}/split`,'POST',{
    item_id:Number(d.item_id),qty:Number(d.qty),
    target_account_id:targetId,name:d.name||null,table_id:tableId,reason:d.reason
  });
  toast('Producto trasladado correctamente; no se generó una nueva comanda');
  await refreshAccountPanel(accountId,['owner','admin'].includes(state.user.role));
  break;
}
case 'table-add':await api('/tables','POST',{name:d.name,area:d.area,capacity:Number(d.capacity),sort_order:Number(d.sort_order||0),active:true});toast('Mesa registrada');await render();break;case 'ticket-lookup':{const a=await api('/accounts/'+Number(d.id));$('#content').innerHTML=`<button class="outline small" data-nav="cashier">← Volver</button><div class="card"><h3>Ticket #${a.id} · ${esc(a.name)}</h3><p>${pill(a.status)} · Total ${money(a.total)} · Pagado ${money(a.paid)}</p><div class="inline"><button class="outline" data-action="download-ticket" data-id="${a.id}">Descargar PDF</button><button class="outline" data-action="share-ticket" data-id="${a.id}">Compartir por WhatsApp</button></div></div>`;break}case 'new-account':{const tableId=d.table_id?Number(d.table_id):null;const name=(d.name||'').trim();const payload={name,table_id:tableId};const scope='account-create';const client_key=pendingOperationKey(scope,payload);const r=await api('/accounts','POST',{...payload,client_key});clearPendingOperation(scope);state.account=r.id;state.draft=[];persistWorkState();toast(r.duplicate?'Cuenta ya registrada; no se duplicó':tableId?'Cuenta abierta en '+(state.data.tables||[]).find(t=>t.id===tableId)?.name:'Cuenta libre creada');await go('orders',{replace:true});break}case 'reassign-form':{const accountId=Number(d.account_id);await api(`/accounts/${accountId}/reassign`,'POST',{owner_id:Number(d.owner_id),reason:d.reason});toast('Cuenta reasignada');await refreshAccountPanel(accountId,true);break}case 'discount-form':{const accountId=Number(state.data.currentAccount?.id);if(!accountId)throw Error('No hay una cuenta seleccionada');const payload={scope:d.scope,mode:d.mode,value:d.value,reason:d.reason,...(d.scope==='item'?{item_id:Number(d.item_id)}:{})};if(d.scope==='item'&&!payload.item_id)throw Error('Selecciona el producto');await api(`/accounts/${accountId}/discounts`,'POST',payload);toast('Descuento aplicado y auditado');await render();break}
case 'pay-form':{const a=state.data.currentAccount;const methods=['cash','card','transfer'];const lines=methods.filter(x=>Number(d[x])>0).map(x=>({method:x,amount:d[x],tip:d[x+'_tip']||'0',...(x==='cash'&&Number(d.received)>0?{received:d.received}:{})}));if(!lines.length)throw Error('Escribe al menos un importe de consumo');for(const method of methods){if(Number(d[method+'_tip']||0)>0&&Number(d[method]||0)<=0)throw Error(`Registra primero un importe de consumo en ${method==='cash'?'efectivo':method==='card'?'tarjeta':'transferencia'} para asociar esa propina`)}let allocations=[];if(d.split_mode==='products'){allocations=$$('.allocation-qty').filter(x=>Number(x.value)>0).map(x=>({item_id:Number(x.dataset.item),qty:Number(x.value)}));if(!allocations.length)throw Error('Selecciona productos');const expected=allocations.reduce((s,x)=>{const el=$(`.allocation-qty[data-item="${x.item_id}"]`);return s+x.qty*Number(el.dataset.price)},0);const got=lines.reduce((s,x)=>s+Math.round(Number(x.amount)*100),0);if(Math.round(expected*100)!==got)throw Error(`Los productos elegidos suman ${money(expected)}; ajusta los métodos de pago`)}if(!confirm('¿Confirmar el pago y registrar las propinas por separado?'))return;const scope='payment-'+a.id;const payload={lines,allocations};const client_key=pendingOperationKey(scope,payload);const result=await api(`/accounts/${a.id}/payments`,'POST',{client_key,...payload});clearPendingOperation(scope);toast(result.duplicate?'Pago ya registrado; no se duplicó':'Pago y propinas registrados');await render();break}case 'cash-movement':{const payload={kind:d.kind,amount:d.amount,reason:d.reason};const client_key=pendingOperationKey('cash-movement',payload);const result=await api('/cash/movements','POST',{...payload,client_key});clearPendingOperation('cash-movement');toast(result.duplicate?'Movimiento ya registrado; no se duplicó':'Movimiento registrado o solicitado');await render();break;}case 'cash-close':if(!confirm('¿Confirmar corte general? El periodo quedará cerrado y no podrá modificarse.'))return;await api('/cash/close','POST',{counted:d.counted,note:d.note});toast('Corte cerrado y archivado con su PDF. Ventas anteriores fuera de las vistas activas.');await render();break;case 'cash-open':await api('/cash/open','POST',{opening:d.opening});toast('Periodo de caja abierto');await render();break;case 'product-add':await api('/products','POST',{name:d.name,category:d.category||'General',station:d.station,price:d.price,stock:Number(d.stock||0),inventory_enabled:form.querySelector('[name="inventory_enabled"]').checked});toast('Producto creado');await render();break;
case (form.id.startsWith('variant-add-')?form.id:'__no-variant__'):{const productId=Number(form.id.slice('variant-add-'.length));await api(`/products/${productId}/variants`,'POST',{name:d.name,price:d.price,active:true,sort_order:0});toast('Presentación agregada');await render();break;}
case (form.id.startsWith('modifier-group-add-')?form.id:'__no-group__'):{const productId=Number(form.id.slice('modifier-group-add-'.length));const min=Number(d.min_select||0),max=Number(d.max_select||1);if(min>max)throw Error('El mínimo no puede superar al máximo');await api(`/products/${productId}/modifier-groups`,'POST',{name:d.name,min_select:min,max_select:max,active:true,sort_order:0});toast('Grupo de modificadores creado');await render();break;}
case (form.id.startsWith('modifier-option-add-')?form.id:'__no-option__'):{const groupId=Number(form.id.slice('modifier-option-add-'.length));await api(`/modifier-groups/${groupId}/options`,'POST',{name:d.name,price_delta:d.price_delta||'0',active:true,sort_order:0});toast('Opción agregada');await render();break;}
case (form.id.startsWith('promotion-add-')?form.id:'__no-promotion__'):{const productId=Number(form.id.slice('promotion-add-'.length));if(Boolean(d.price)&&Boolean(d.discount_percent))throw Error('Indica precio o descuento, no ambos');if(!d.price&&!d.discount_percent)throw Error('Indica un precio promocional o un descuento');await api(`/products/${productId}/promotions`,'POST',{name:d.name,price:d.price||null,discount_percent:d.discount_percent?Number(d.discount_percent):null,weekdays:d.weekdays,start_time:d.start_time,end_time:d.end_time,start_date:d.start_date||null,end_date:d.end_date||null});toast('Promoción creada');await render();break;}
case 'staff-add':await api('/staff','POST',{name:d.name,role:d.role,pin:d.pin});toast('Personal registrado con PIN');await render();break;
 case 'tip-distribution-form':{
   const fd=new FormData(form);const user_ids=fd.getAll('user_ids').map(Number);
   if(!user_ids.length)throw Error('Selecciona al menos un empleado participante');
   if(!confirm(`¿Generar el reparto igualitario para ${user_ids.length} empleado(s)? Una vez creado, el reparto no se puede editar.`))return;
   await api('/tips/pool/current/distribution','POST',{user_ids,note:d.note||''});
   toast('Fondo común distribuido igualitariamente');await render();break;
 }
 case (form.id.startsWith('tip-payout-')?form.id:'__no-tip-payout__'):{
   const parts=form.id.split('-');const distributionId=Number(parts[2]),shareId=Number(parts[3]);
   if(!distributionId||!shareId)throw Error('Identificador de reparto inválido');
   const scope='tip-payout-'+distributionId+'-'+shareId;
   const payload={amount:d.amount,method:d.method,note:d.note||''};
   const client_key=pendingOperationKey(scope,payload);
   const result=await api(`/tips/pool/distributions/${distributionId}/shares/${shareId}/payouts`,'POST',
     {client_key,...payload});
   clearPendingOperation(scope);
   toast(result.duplicate?'Entrega ya registrada; no se duplicó':'Entrega de propina registrada');
   await render();break;
 }
 default:throw Error('Formulario no reconocido')}}
function urlBase64ToUint8Array(s){
  const pad='='.repeat((4-s.length%4)%4);
  const raw=atob((s+pad).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from(raw,c=>c.charCodeAt(0));
}
async function showNotifications(){
  if(window.MitoteAndroid){
    return `<div class="card"><h3>🔔 Avisos Android</h3><p>Esta APK utiliza Firebase Cloud Messaging. No necesitas activar Web Push ni configurar VAPID.</p><p>Permite las notificaciones de Android cuando el sistema lo solicite. Si acabas de entrar, espera unos segundos para registrar el teléfono.</p><button class="primary" data-action="fcm-test">Enviar prueba Android</button><p class="mini">Los avisos reales tienen un sonido más fuerte en la nueva versión del APK. Activa el permiso de notificaciones y revisa el volumen de avisos de Android.</p></div>`;
  }
  const supported=location.protocol==='https:'&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
  if(!supported)return '<div class="note danger-note">Este navegador no permite notificaciones Web Push. Abre la aplicación en Chrome para Android mediante HTTPS.</div>';
  let configured=false,subscribed=false,serverDetail='';
  try{
    const cfg=await api('/push/config');
    configured=Boolean(cfg.enabled);
    if(!configured)serverDetail=Array.isArray(cfg.diagnostic)&&cfg.diagnostic.length
      ?cfg.diagnostic.join('; '):'Configuración incompleta; revisar variables en Render';
  }catch(e){serverDetail='Error consultando servidor: '+(e?.message||'desconocido');}
  try{const reg=await navigator.serviceWorker.ready;subscribed=!!(await reg.pushManager.getSubscription());}
  catch(e){serverDetail+=(serverDetail?'; ':'')+'Error del dispositivo: '+(e?.message||'desconocido');}
  return `<div class="card"><h3>🔔 Avisos en segundo plano</h3><p>Recibe notificaciones aunque estés usando otra aplicación o tengas el teléfono bloqueado.</p>
    <p><b>Servidor:</b> ${configured?'Configurado':'Sin configurar'}${serverDetail?`<br><b>Diagnóstico:</b> ${esc(serverDetail)}`:''}<br><b>Permiso Android:</b> ${esc(Notification.permission)}<br><b>Dispositivo:</b> ${subscribed?'Suscrito':'Sin suscripción'}</p>
    <div class="inline"><button class="outline" data-action="enable-sound">♪ Probar sonido fuerte</button><button class="primary" data-action="push-enable" ${configured?'':'disabled'}>Activar notificaciones</button>
    <button class="outline" data-action="push-test" ${subscribed?'':'disabled'}>Enviar prueba</button>
    <button class="ghost" data-action="push-disable" ${subscribed?'':'disabled'}>Desactivar aquí</button></div>
    <div class="note">Debes activar esta función una vez en cada teléfono. Android controla el sonido y puede retrasar avisos por ahorro de batería. La alarma repetitiva sigue funcionando con la aplicación abierta.</div></div>`;
}
async function enablePush(){
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window))throw Error('Navegador no compatible');
  // Pedir permiso directamente desde el botón, antes de otras operaciones asíncronas.
  const permission=await Notification.requestPermission();
  if(permission!=='granted')throw Error('Permiso de notificaciones denegado. Actívalo en ajustes de Chrome/Android.');
  const cfg=await api('/push/config');
  if(!cfg.enabled)throw Error('Faltan claves VAPID en Render');
  const reg=await navigator.serviceWorker.ready;
  let sub=await reg.pushManager.getSubscription();
  const key=urlBase64ToUint8Array(cfg.public_key);
  if(sub){
    const previous=sub.options?.applicationServerKey;
    if(previous&&Array.from(new Uint8Array(previous)).join(',')!==Array.from(key).join(',')){
      await sub.unsubscribe();sub=null;
    }
  }
  if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key});
  await api('/push/subscribe','POST',sub.toJSON());
  toast('Notificaciones activadas para este teléfono');
}
async function syncPushOnLogin(){
  if(!['waiter','kitchen','bar'].includes(state.user?.role)||Notification.permission!=='granted')return;
  const cfg=await api('/push/config');if(!cfg.enabled)return;
  const reg=await navigator.serviceWorker.ready;
  const sub=await reg.pushManager.getSubscription();
  if(sub)await api('/push/subscribe','POST',sub.toJSON());
}
async function disablePush(){
  const reg=await navigator.serviceWorker.ready;
  const sub=await reg.pushManager.getSubscription();
  if(sub){await api('/push/unsubscribe','POST',{endpoint:sub.endpoint});await sub.unsubscribe();}
  toast('Notificaciones desactivadas en este teléfono');
}
document.addEventListener('pointerdown',()=>{
  // Los navegadores solo permiten audio después de una interacción del mesero.
  if(!state.user||state.alarmArmed||window.MitoteAndroid)return;
  state.alarmArmed=true;
  try{
    state.audio=state.audio||new(window.AudioContext||window.webkitAudioContext)();
    if(state.audio.state==='suspended')state.audio.resume().catch(()=>{});
  }catch{}
},{passive:true});
function stopAlarm(){clearInterval(state.alarmTimer);state.alarmTimer=null}
function beep(kind='station'){
  if(document.hidden||!state.alarmArmed)return;
  try{
    state.audio=state.audio||new (window.AudioContext||window.webkitAudioContext)();
    const ctx=state.audio;
    if(ctx.state==='suspended')ctx.resume().catch(()=>{});
    const notes=kind==='ready'?[740,980,1174,980]:[660,880,660,990];
    const time=ctx.currentTime+.025;
    const output=ctx.createDynamicsCompressor();
    output.threshold.value=-20;output.knee.value=18;output.ratio.value=4;
    output.attack.value=.005;output.release.value=.2;output.connect(ctx.destination);
    notes.forEach((freq,i)=>{
      const start=time+i*.19,stop=start+.16;
      const osc=ctx.createOscillator(),gain=ctx.createGain();
      osc.type='square';osc.frequency.setValueAtTime(freq,start);
      const peak=kind==='ready'?.24:.20;
      gain.gain.setValueAtTime(.0001,start);
      gain.gain.exponentialRampToValueAtTime(peak,start+.014);
      gain.gain.setValueAtTime(peak*.68,start+.075);
      gain.gain.exponentialRampToValueAtTime(.0001,stop);
      osc.connect(gain);gain.connect(output);
      osc.start(start);osc.stop(stop+.015);
    });
  }catch(e){}
}
function checkAlarm(){const isStation=['kitchen','bar'].includes(state.user?.role);const tickets=state.data.tickets||[];const pending=isStation&&tickets.some(t=>t.station===state.user.role&&t.status==='new');if(!pending){stopAlarm();return}if(!state.alarmTimer){beep('station');state.alarmTimer=setInterval(()=>beep('station'),8000)}}
async function syncWaiterReadyAlerts(initial=false){
  if(state.user?.role!=='waiter')return;
  if(state.waiterReadyBusy){state.waiterReadyPending=true;return;}
  state.waiterReadyBusy=true;
  try{
    const items=await api('/waiter/ready-alerts');
    if(state.user?.role!=='waiter')return;
    const userKey='mitote-ready-seen-'+state.user.id;
    let remembered=[];
    try{remembered=JSON.parse(sessionStorage.getItem(userKey)||'[]');if(!Array.isArray(remembered))remembered=[]}catch{}
    if(!state.waiterReadyInitialized||initial){
      state.waiterReadySeen=new Set(remembered.filter(x=>typeof x==='string'));
      for(const x of items)state.waiterReadySeen.add(x.id);
      state.waiterReadyInitialized=true;
    }else{
      const arrivals=items.filter(x=>!state.waiterReadySeen.has(x.id)).reverse();
      for(const row of arrivals){
        state.waiterReadySeen.add(row.id);
        toast((row.complete?'✅ Pedido completo: ':'🔔 '+row.station_name+' lista: ')+row.account+' · Comanda #'+row.batch_id);
        // En la APK el FCM nativo reproduce el sonido: evita tocarlo dos veces.
        if(!window.MitoteAndroid)beep('ready');
      }
    }
    state.data.readyAlerts=items;
    try{sessionStorage.setItem(userKey,JSON.stringify([...state.waiterReadySeen].slice(-180)))}catch{}
    // Actualizar solo el panel de avisos; no volver a cargar las cuentas.
    if(state.screen==='accounts'){
      const panel=$('.waiter-ready-card');
      if(panel)panel.outerHTML=waiterReadyAlertsHtml();
    }
  }finally{
    state.waiterReadyBusy=false;
    if(state.waiterReadyPending){state.waiterReadyPending=false;syncWaiterReadyAlerts().catch(()=>{});}
  }
}
async function listenEvents(){while(state.user&&state.token){try{
  await refreshSessionIfNeeded();const token=state.token;
  const res=await fetch('/api/events',{headers:{Authorization:'Bearer '+token},cache:'no-store'});
  if(res.status===401){logout('expired');break}if(!res.ok)throw Error('Sin eventos');
  const reader=res.body.getReader();const decoder=new TextDecoder();let pending='';
  while(state.token===token&&state.user){const {done,value}=await reader.read();if(done)break;pending+=decoder.decode(value,{stream:true});const parts=pending.split('\n\n');pending=parts.pop();if(parts.some(x=>x.includes('data: refresh'))){if(state.user?.role==='waiter')syncWaiterReadyAlerts().catch(()=>{});if(Date.now()-state.lastRefresh>1400){state.lastRefresh=Date.now();const editing=document.activeElement?.matches('input,textarea,select');if(state.user?.role==='waiter'&&state.screen==='orders'){state.pendingRefresh=true;$('#refresh')?.classList.add('has-update')}else if(!editing)await render({preserveScroll:true})}}}
 }catch{}if(!state.user)break;await new Promise(r=>setTimeout(r,3500))}}
// La pantalla principal solo muestra un teclado numérico. Los perfiles se obtienen del servidor.
const recoveryMode=new URLSearchParams(location.search).get('recuperar')==='1';
$('#login-pin-form').hidden=recoveryMode;
$('#legacy-login-form').hidden=!recoveryMode;
$('#login-recovery-link').hidden=recoveryMode;
$('#login-back-link').hidden=!recoveryMode;
const pinInput=$('#login-pin'),pinSubmit=$('#login-pin-submit');
let pinLoginBusy=false;
function loginError(message){
  const node=$('#login-error');node.textContent=message;node.hidden=!message;
}
function pinEdit(action){
  let value=pinInput.value;
  if(action==='delete')value=value.slice(0,-1);
  else if(action==='clear')value='';
  else if(/^[0-9]$/.test(action)&&value.length<6)value+=action;
  pinInput.value=value;
  pinSubmit.disabled=value.length!==6;
  loginError('');
}
$('#login-pin-form').addEventListener('click',e=>{
  const b=e.target.closest('[data-pin-digit],[data-pin-clear],[data-pin-delete]');
  if(!b)return;
  e.preventDefault();
  pinEdit(b.dataset.pinDigit??(b.hasAttribute('data-pin-delete')?'delete':'clear'));
});
document.addEventListener('keydown',e=>{
  if($('#login').hidden||recoveryMode||e.ctrlKey||e.altKey||e.metaKey||e.repeat)return;
  if(/^[0-9]$/.test(e.key)){e.preventDefault();pinEdit(e.key);}
  else if(e.key==='Backspace'){e.preventDefault();pinEdit('delete');}
  else if(e.key==='Escape'){e.preventDefault();pinEdit('clear');}
  else if(e.key==='Enter'&&pinInput.value.length===6){e.preventDefault();$('#login-pin-form').requestSubmit();}
});
async function loginSession(path,payload){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload),cache:'no-store'});
  let d={};try{d=await r.json()}catch{}
  if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'Acceso incorrecto');
  state.token=d.token;localStorage.setItem('mitote-token',d.token);
  sessionStorage.removeItem('mitote-token');state.alarmArmed=true;
  await start(d.user);
}
$('#login-pin-form').addEventListener('submit',async e=>{
  e.preventDefault();if(pinLoginBusy)return;loginError('');
  const pin=pinInput.value;
  if(!/^[0-9]{6}$/.test(pin)){loginError('Escribe un PIN de seis dígitos');return;}
  pinLoginBusy=true;pinSubmit.disabled=true;
  try{await loginSession('/api/login/pin',{pin});pinInput.value='';}
  catch(err){loginError(err.message);pinInput.value='';}
  finally{pinLoginBusy=false;pinSubmit.disabled=true;}
});
$('#legacy-login-form').addEventListener('submit',async e=>{
  e.preventDefault();loginError('');
  try{
    const username=$('#login-user').value.trim(),secret=$('#login-secret').value;
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,secret}),cache:'no-store'});
    const d=await r.json();
    if(!r.ok)throw Error(d.detail||'Acceso incorrecto');
    if(d.user?.role!=='owner')throw Error('La activación inicial solo está permitida al Propietario');
    state.token=d.token;localStorage.setItem('mitote-token',d.token);
    sessionStorage.removeItem('mitote-token');state.alarmArmed=true;
    await start(d.user);
    if(state.user?.role==='owner'&&state.screen!=='users')await go('users');
  }catch(err){loginError(err.message);}
});
$('#logout').onclick=()=>logout('manual');$('#back').onclick=goBack;$('#refresh').onclick=()=>render({preserveScroll:true});
$('#nav').addEventListener('click',e=>{const b=e.target.closest('[data-nav]');if(b)go(b.dataset.nav)});
$('#content').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.nav){go(b.dataset.nav);return}if(b.dataset.action){b.disabled=true;try{await act(b.dataset.action,b)}catch(err){toast(err.message,true)}finally{b.disabled=false}}});
$('#content').addEventListener('submit',async e=>{e.preventDefault();const form=e.target,btn=form.querySelector('button[type="submit"],button:not([type])');if(btn)btn.disabled=true;try{await submitForm(form)}catch(err){toast(err.message,true)}finally{if(btn)btn.disabled=false}});
$('#content').addEventListener('change',e=>{if(e.target.id==='order-account'){state.account=Number(e.target.value);state.draft=[];persistWorkState();render()}if(e.target.id==='order-product'){const p=state.products.find(x=>x.id===Number(e.target.value));const box=$('#product-config');if(box)box.innerHTML=orderProductConfig(p)}if(e.target.id==='cashier-account'){state.account=Number(e.target.value);render()}if(e.target.id==='split-mode'){$('#split-products').hidden=e.target.value!=='products'}});
window.addEventListener('online',()=>{$('#connection').textContent='● Conectado';$('#connection').classList.remove('off');render({preserveScroll:true})});window.addEventListener('offline',()=>{$('#connection').textContent='● Sin conexión';$('#connection').classList.add('off');stopAlarm()});
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
if(window.MitoteStartup?.boot){
  window.MitoteStartup.boot({hasToken:Boolean(state.token),restoreSession:()=>start()});
}else if(state.token)start();