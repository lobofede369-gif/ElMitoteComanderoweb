'use strict';

/* Panel principal premium de El Mitote.
   Consume las API existentes: no introduce cifras ficticias ni modifica permisos. */
async function showOverview() {
  const results = await Promise.allSettled([
    api('/overview'),
    api('/tables'),
    api('/tickets'),
    api('/cash'),
    api('/products'),
    api('/settings')
  ]);
  if (results[0].status !== 'fulfilled') throw results[0].reason;
  const summary = results[0].value || {};
  const tables = results[1].status === 'fulfilled' && Array.isArray(results[1].value) ? results[1].value : [];
  const tickets = results[2].status === 'fulfilled' && Array.isArray(results[2].value) ? results[2].value : [];
  const cash = results[3].status === 'fulfilled' ? (results[3].value || {open:false}) : {open:false};
  const products = results[4].status === 'fulfilled' && Array.isArray(results[4].value) ? results[4].value : [];
  const settings = results[5].status === 'fulfilled' ? (results[5].value || {}) : {};
  state.data.overview = summary;
  state.data.tables = tables;
  state.data.tickets = tickets;
  state.data.cash = cash;
  state.products = products;

  const activeTables = tables.filter(t => t.active);
  const occupiedTables = activeTables.filter(t => t.status === 'occupied');
  const freeTables = activeTables.filter(t => t.status === 'available');
  const newTickets = Math.max(Number(summary.new_tickets || 0), tickets.filter(t => t.status === 'new').length);
  const pendingTickets = tickets.filter(t => t.status !== 'ready').length;
  const stationCount = station => tickets.filter(t => t.station === station && t.status !== 'ready').length;
  const salesMethods = [
    {key:'cash',label:'Efectivo'},
    {key:'card',label:'Tarjeta'},
    {key:'transfer',label:'Transferencia'}
  ].map(x => ({...x,value:cash.open ? Number(cash[x.key] || 0) : 0}));
  const salesTotal = salesMethods.reduce((sum,x) => sum + x.value, 0);
  const cashExpected = cash.open ? Number(cash.expected || 0) : 0;
  const inventoryEnabled = settings.inventory_on !== false;
  const lowStock = inventoryEnabled
    ? products.filter(p => p.inventory_enabled && p.active !== false && !p.blocked && Number(p.stock || 0) <= 3)
        .sort((a,b) => Number(a.stock || 0) - Number(b.stock || 0))
    : [];
  const iconPaths = {
    sales:'<path d="M3 17 9 11l4 4 8-9"/><path d="M15 6h6v6"/><path d="M4 20h3v-4H4zM10 20h3v-7h-3zM16 20h3v-9h-3z"/>',
    tables:'<path d="M3 6h18v5H3zM6 11v9M18 11v9M3 16h18"/>',
    orders:'<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
    kitchen:'<path d="M6 14a4 4 0 0 1-2-7 5 5 0 0 1 8-2 5 5 0 0 1 8 2 4 4 0 0 1-2 7v6H6z"/><path d="M6 17h12"/>',
    bar:'<path d="M5 4h14l-6 8v6h-2v-6z"/><path d="M8 21h8"/>',
    cash:'<rect x="2" y="5" width="20" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M5 9h1M18 15h1"/>',
    box:'<path d="m12 2 9 5-9 5-9-5zM3 7v10l9 5 9-5V7M12 12v10"/>',
    menu:'<path d="M4 5h16M4 12h16M4 19h16"/><path d="M8 3v18"/>',
    users:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5v1"/>',
    tips:'<circle cx="12" cy="12" r="9"/><path d="M15 9c-1-2-6-2-6 1 0 3 6 1 6 4 0 3-5 3-6 1M12 6v12"/>',
    report:'<path d="M4 20V11h4v9M10 20V5h4v15M16 20V2h4v18"/>',
    shield:'<path d="M12 2 4 6v6c0 5 3 8 8 10 5-2 8-5 8-10V6z"/><path d="m9 12 2 2 4-4"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'
  };
  const icon = name => '<svg viewBox="0 0 24 24" width="27" height="27" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (iconPaths[name] || iconPaths.orders) + '</svg>';
  const navAllowed = screen => (sections[state.user.role] || []).some(x => x[0] === screen);
  const shortcutSpecs = [
    ['all','Cuentas','Seguimiento de cuentas','orders'],
    ['tables','Gestionar mesas','Ocupación del salón','tables'],
    ['products','Ver menú','Productos y precios','menu'],
    ['inventory','Inventario','Stock y reposición','box'],
    ['cash','Corte de caja','Turno y movimientos','cash']
  ].filter(x => navAllowed(x[0]));
  const shortcutsHtml = shortcutSpecs.map(x =>
    '<button type="button" class="mitote-quick-action" data-nav="' + x[0] + '" aria-label="Abrir ' + esc(x[1]) + '">' +
      '<span class="mitote-quick-icon">' + icon(x[3]) + '</span><strong>' + esc(x[1]) + '</strong><small>' + esc(x[2]) + '</small></button>'
  ).join('');
  const moreSpecs = [
    ['tickets','Estaciones','Comandas de cocina y barra','kitchen'],
    ['cashier','Cobros','Pagos y saldos pendientes','cash'],
    ['tips','Propinas','Fondo y distribución','tips'],
    ['users','Personal','Usuarios y permisos','users'],
    ['audit','Auditoría','Registro de operaciones','shield'],
    ['reservations','Reservaciones','Llegadas y reservas','clock'],
    ['prepconfig','Tiempos de preparación','Cocina y barra','clock'],
    ['analytics','Estadísticas','Indicadores y resultados','report']
  ].filter(x => navAllowed(x[0]));
  const moreHtml = moreSpecs.map(x =>
    '<button type="button" class="mitote-module-link" data-nav="' + x[0] + '">' +
      '<span>' + icon(x[3]) + '</span><span><strong>' + esc(x[1]) + '</strong><small>' + esc(x[2]) + '</small></span><b aria-hidden="true">›</b></button>'
  ).join('');
  const salesHtml = salesMethods.map(x => {
    const pct = salesTotal > 0 ? Math.round(x.value / salesTotal * 100) : 0;
    return '<div class="mitote-sales-method"><span>' + x.label + '</span><div class="mitote-sales-track"><i style="width:' + pct + '%"></i></div><b>' + money(x.value) + '</b></div>';
  }).join('');
  const tableHtml = tables.slice(0,10).map(t => {
    const status = t.status === 'occupied' ? 'occupied' : t.active ? 'available' : 'inactive';
    const label = status === 'occupied' ? 'Ocupada' : status === 'available' ? 'Libre' : 'Inactiva';
    return '<div class="mitote-table-tile ' + status + '"><strong>' + esc(t.name) + '</strong><small>' + label + '</small></div>';
  }).join('') || '<p class="mitote-empty">No hay mesas registradas.</p>';
  const ticketHtml = tickets.slice(0,4).map(t => {
    const station = t.station === 'bar' ? 'Barra' : 'Cocina';
    const stateLabel = t.status === 'ready' ? 'Lista' : t.status === 'acknowledged' ? 'Recibida' : 'Nueva';
    const created = t.created_at ? new Date(t.created_at).getTime() : NaN;
    const elapsed = Number.isFinite(created) ? Math.max(0,Math.floor((Date.now() - created) / 60000)) + ' min' : '—';
    return '<div class="mitote-ticket-row"><span class="mitote-ticket-icon">' + icon(t.station === 'bar' ? 'bar' : 'kitchen') + '</span><div class="mitote-ticket-copy"><strong>Comanda #' + Number(t.id) + '</strong><small>' + esc(t.account || 'Cuenta') + ' · ' + esc(station) + ' · ' + esc(t.waiter || 'Sin mesero') + '</small></div><span class="mitote-ticket-time">' + elapsed + '</span><span class="mitote-ticket-state ' + esc(t.status) + '">' + stateLabel + '</span></div>';
  }).join('') || '<p class="mitote-empty">No hay comandas activas en las estaciones.</p>';
  const lowStockHtml = lowStock.slice(0,4).map(p =>
    '<span class="mitote-stock-chip">' + esc(p.name) + ' · ' + Number(p.stock || 0) + ' ' + esc(p.inventory_unit || 'ud.') + '</span>'
  ).join('');
  const inventoryTitle = !inventoryEnabled ? 'Control de inventario desactivado' : lowStock.length ? lowStock.length + ' producto(s) con stock bajo' : 'Inventario sin alertas';
  const inventoryText = !inventoryEnabled ? 'Activa el control desde el módulo Inventario.' : lowStock.length ? lowStockHtml : 'Las existencias controladas están por encima del mínimo.';
  const timeLabel = new Date().toLocaleTimeString('es-MX',{hour:'2-digit',minute:'2-digit'});
  const salesTitle = cash.open ? 'Ventas del turno' : 'Turno cerrado';
  const cashTitle = cash.open ? 'Caja actual' : 'Caja sin turno abierto';
  const stationBar = stationCount('bar');
  const stationKitchen = stationCount('kitchen');
  const quickTitle = 'Acciones rápidas';
  return '<div class="mitote-dashboard">' +
    '<section class="mitote-dash-hero"><div class="mitote-dash-hero-copy"><span class="mitote-kicker">EL MITOTE · RESTAURANTE</span><h2>Panel principal</h2><p>Resumen operativo del restaurante</p></div><div class="mitote-dash-hero-meta"><span class="mitote-live-dot"></span><span>Datos del sistema</span><small>Actualizado ' + timeLabel + '</small></div></section>' +
    '<section class="mitote-sales-card"><div class="mitote-sales-main"><span class="mitote-sales-label">' + salesTitle + '</span><strong>' + (cash.open ? money(salesTotal) : '—') + '</strong><small>' + (cash.open ? 'Pagos registrados en el turno actual' : 'Abre un turno para registrar ventas') + '</small></div><div class="mitote-sales-breakdown">' + salesHtml + '</div></section>' +
    '<section class="mitote-metric-grid">' +
      '<article class="mitote-metric-card gold"><span class="mitote-metric-icon">' + icon('tables') + '</span><div><small>Mesas ocupadas</small><strong>' + occupiedTables.length + '</strong><em>' + occupiedTables.length + ' de ' + activeTables.length + ' activas</em></div></article>' +
      '<article class="mitote-metric-card green"><span class="mitote-metric-icon">' + icon('tables') + '</span><div><small>Mesas libres</small><strong>' + freeTables.length + '</strong><em>Listas para asignar</em></div></article>' +
      '<article class="mitote-metric-card gold"><span class="mitote-metric-icon">' + icon('orders') + '</span><div><small>Comandas nuevas</small><strong>' + newTickets + '</strong><em>' + pendingTickets + ' en estaciones</em></div></article>' +
    '</section>' +
    '<section class="mitote-station-grid">' +
      '<button type="button" class="mitote-station-card bar" data-nav="tickets"><span class="mitote-station-icon">' + icon('bar') + '</span><span><strong>Barra</strong><small>' + stationCount('bar') + ' comandas en proceso</small><em>Ver estación</em></span><b aria-hidden="true">›</b></button>' +
      '<button type="button" class="mitote-station-card kitchen" data-nav="tickets"><span class="mitote-station-icon">' + icon('kitchen') + '</span><span><strong>Cocina</strong><small>' + stationCount('kitchen') + ' comandas en proceso</small><em>Ver estación</em></span><b aria-hidden="true">›</b></button>' +
    '</section>' +
    '<section class="mitote-cash-inventory-grid">' +
      '<button type="button" class="mitote-cash-card" data-nav="cash"><span class="mitote-cash-icon">' + icon('cash') + '</span><span><small>' + cashTitle + '</small><strong>' + (cash.open ? money(cashExpected) : '—') + '</strong><em>' + (cash.open ? 'Efectivo esperado · turno #' + Number(cash.id || 0) : 'Consulta el historial o abre un turno') + '</em></span><b class="mitote-cash-status ' + (cash.open ? 'open' : 'closed') + '">' + (cash.open ? 'Abierta' : 'Cerrada') + '</b><i aria-hidden="true">›</i></button>' +
      '<button type="button" class="mitote-inventory-alert ' + (lowStock.length ? 'alert' : 'ok') + '" data-nav="inventory"><span class="mitote-alert-icon">' + icon('box') + '</span><span><small>Alertas de inventario</small><strong>' + inventoryTitle + '</strong><em>' + inventoryText + '</em></span><b>Ver detalles ›</b></button>' +
    '</section>' +
    '<section class="mitote-dashboard-section"><div class="mitote-section-heading"><h3>' + quickTitle + '</h3><span>Acceso directo</span></div><div class="mitote-quick-grid">' + shortcutsHtml + '</div></section>' +
    '<section class="mitote-lower-grid">' +
      '<article class="mitote-live-panel"><div class="mitote-section-heading"><h3>Estado de mesas</h3><button type="button" data-nav="tables">Ver todas ›</button></div><div class="mitote-table-grid">' + tableHtml + '</div></article>' +
      '<article class="mitote-live-panel"><div class="mitote-section-heading"><h3>Comandas en estaciones</h3><button type="button" data-nav="tickets">Ver todas ›</button></div><div class="mitote-ticket-list">' + ticketHtml + '</div></article>' +
    '</section>' +
    (moreHtml ? '<section class="mitote-dashboard-section mitote-more-modules"><div class="mitote-section-heading"><h3>Más módulos</h3><span>Herramientas de administración</span></div><div class="mitote-module-grid">' + moreHtml + '</div></section>' : '') +
  '</div>';
}
