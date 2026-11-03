/* El Mitote Comandas · POS UI v3
 * Complemento visual para el app.js original. No reemplaza lógica, API, permisos ni cobros.
 * Requiere cargar DESPUÉS de /static/app.js como script clásico con defer.
 */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const ICONS = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
    plus: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    list: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 9h9M8 13h9M8 17h6"/>',
    receipt: '<path d="M5 3h14v18l-3-2-4 2-4-2-3 2z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    cash: '<rect x="2" y="5" width="20" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M5 9h1M18 15h1"/>',
    menu: '<path d="M4 5h16M4 12h16M4 19h16"/>',
    food: '<path d="M4 3v8a3 3 0 0 0 6 0V3M7 3v18M17 3c-3 4-3 8 0 10h3V3M17 13v8"/>',
    chef: '<path d="M6 13a4 4 0 0 1-2-7 5 5 0 0 1 8-2 5 5 0 0 1 8 2 4 4 0 0 1-2 7v7H6z"/><path d="M6 16h12"/>',
    cup: '<path d="M5 5h12v10a6 6 0 0 1-12 0zM17 7h2a3 3 0 0 1 0 6h-2M4 21h15"/>',
    people: '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5v1"/>',
    boxes: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    shield: '<path d="M12 2 4 6v6c0 5 3 8 8 10 5-2 8-5 8-10V6z"/><path d="m9 12 2 2 4-4"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/>',
    audit: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3v-1h6v1M8 9h8M8 13h8M8 17h5"/>'
  };
  const ICON_BY_SCREEN = {overview:'home',accounts:'list',all:'list',new:'plus',orders:'food',station:'chef',history:'history',tickets:'chef',cashier:'cash',cash:'receipt',products:'menu',inventory:'boxes',users:'people',approvals:'shield',audit:'audit'};
  const PRIMARY = {owner:['overview','all','cashier','products'],admin:['overview','all','cashier','products'],waiter:['accounts','new','orders'],cashier:['cashier','cash'],kitchen:['station','history'],bar:['station','history']};
  const SHORT_LABELS = {overview:'Inicio',all:'Cuentas',accounts:'Cuentas',new:'Nueva',orders:'Comandas',cashier:'Cobros',products:'Menú',cash:'Caja',station:'Tickets',history:'Historial'};
  const TILES = [
    {screen:'all',icon:'list',label:'Cuentas',hint:'Ver todas las cuentas'},
    {screen:'cashier',icon:'cash',label:'Cobrar',hint:'Pagos pendientes'},
    {screen:'tickets',icon:'chef',label:'Estaciones',hint:'Cocina y barra'},
    {screen:'products',icon:'menu',label:'Productos',hint:'Menú y precios'},
    {screen:'users',icon:'people',label:'Personal',hint:'Usuarios y PIN'},
    {screen:'inventory',icon:'boxes',label:'Inventario',hint:'Existencias y control'}
  ];
  function icon(name, size = 24) {
    return `<svg class="pos-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.list}</svg>`;
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function available() {
    try { return typeof state !== 'undefined' && !!state.user; } catch { return false; }
  }
  function navTo(screen) {
    if (typeof go === 'function') void go(screen);
    closeMore();
    window.scrollTo({top:0,behavior:'instant'});
  }
  function ensureMoreSheet() {
    if ($('#pos-more')) return;
    const panel = document.createElement('div');
    panel.id = 'pos-more';
    panel.className = 'pos-more';
    panel.hidden = true;
    panel.innerHTML = `<div class="pos-more-backdrop" data-pos-close="1"></div><section class="pos-more-panel" role="dialog" aria-modal="true" aria-labelledby="pos-more-title"><div class="pos-more-head"><h2 id="pos-more-title">Todos los módulos</h2><button type="button" class="pos-close" data-pos-close="1" aria-label="Cerrar menú">×</button></div><div class="pos-more-list"></div></section>`;
    $('#app').appendChild(panel);
  }
  function closeMore() {
    const panel = $('#pos-more');
    if (panel) panel.hidden = true;
    document.body.classList.remove('pos-drawer-open');
  }
  function openMore() {
    const panel = $('#pos-more');
    if (!panel) return;
    const list = $('#nav');
    const buttons = $$('button[data-nav]', list);
    $('.pos-more-list', panel).innerHTML = buttons.map(b => {
      const screen = b.dataset.nav;
      const label = b.textContent.trim();
      return `<button type="button" class="pos-more-link ${screen === state.screen ? 'active' : ''}" data-pos-go="${escapeHtml(screen)}">${icon(ICON_BY_SCREEN[screen] || 'list',23)}<span>${escapeHtml(label)}</span></button>`;
    }).join('');
    panel.hidden = false;
    document.body.classList.add('pos-drawer-open');
    $('.pos-close', panel)?.focus();
  }
  function enhanceNav() {
    if (!available()) return;
    const nav = $('#nav');
    if (!nav) return;
    const role = state.user.role;
    const primaries = PRIMARY[role] || [];
    const buttons = $$('button[data-nav]', nav);
    for (const b of buttons) {
      const screen = b.dataset.nav;
      const iconName = ICON_BY_SCREEN[screen] || 'list';
      b.classList.toggle('pos-primary-nav', primaries.includes(screen));
      b.classList.toggle('pos-secondary-nav', !primaries.includes(screen));
      if (!b.dataset.posIcon) {
        const span = b.querySelector('span');
        if (span) span.innerHTML = icon(iconName, 21);
        b.dataset.posIcon = '1';
      }
      const title = (sections[role] || []).find(item => item[0] === screen)?.[2] || screen;
      b.setAttribute('aria-label', title);
      b.setAttribute('title', title);
      if (!b.querySelector('.pos-nav-label')) {
        // Original button has an icon span and a text node. Keep the desktop label intact.
        for (const node of [...b.childNodes]) {
          if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
            const text = document.createElement('span');
            text.className = 'pos-nav-label';
            text.textContent = SHORT_LABELS[screen] || title;
            b.replaceChild(text, node);
          }
        }
      }
    }
    const existing = $('[data-pos-more-button]', nav);
    if (buttons.length > primaries.length && !existing) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'navbtn pos-more-button';
      more.dataset.posMoreButton = '1';
      more.innerHTML = `${icon('more',21)}<span class="pos-nav-label">Más</span>`;
      more.setAttribute('aria-label','Ver todos los módulos');
      nav.appendChild(more);
    }
    const more = $('[data-pos-more-button]', nav);
    if (more) more.classList.toggle('active', !primaries.includes(state.screen));
  }
  function tileMarkup(tile, primary = false) {
    return `<button type="button" class="pos-tile ${primary?'pos-tile-primary':''}" data-pos-go="${tile.screen}"><span class="pos-tile-icon">${icon(tile.icon,28)}</span><span class="pos-tile-title">${escapeHtml(tile.label)}</span><span class="pos-tile-hint">${escapeHtml(tile.hint)}</span></button>`;
  }
  function enhanceHome() {
    if (!available()) return;
    const content = $('#content');
    if (!content || $('.pos-shortcuts', content)) return;
    if (state.screen === 'overview' && ['owner','admin'].includes(state.user.role)) {
      const area = document.createElement('section');
      area.className = 'pos-shortcuts';
      area.innerHTML = `<div class="pos-section-title"><span>ACCESOS RÁPIDOS</span><strong>Operación del restaurante</strong></div><div class="pos-tiles">${TILES.map((t,i)=>tileMarkup(t,i===1)).join('')}</div>`;
      content.prepend(area);
    } else if (state.screen === 'accounts' && state.user.role === 'waiter') {
      const area = document.createElement('section');
      area.className = 'pos-shortcuts';
      area.innerHTML = `<div class="pos-section-title"><span>OPERACIÓN</span><strong>¿Qué necesitas hacer?</strong></div><div class="pos-tiles pos-tiles-two">${tileMarkup({screen:'new',icon:'plus',label:'Nueva cuenta',hint:'Abrir una cuenta'},true)}${tileMarkup({screen:'orders',icon:'food',label:'Comandas',hint:'Agregar y enviar productos'})}</div>`;
      content.prepend(area);
    }
  }
  let selectedCategory = '*';
  let catalogScroll = 0;
  function enhanceOrder() {
    if (!available() || state.screen !== 'orders') return;
    const content = $('#content');
    const form = $('#add-line');
    if (!form || $('#pos-catalog')) return;
    const products = Array.isArray(state.products) ? state.products : [];
    if (!products.length) return;
    const categories = [...new Set(products.map(p => String(p.category || 'General')))];
    const gallery = document.createElement('section');
    gallery.id = 'pos-catalog';
    gallery.className = 'pos-catalog';
    gallery.innerHTML = `<div class="pos-catalog-top"><div><span class="pos-kicker">TOMA DE COMANDA</span><h3>Elige los productos</h3></div><span class="pos-product-count">${products.length} disponibles</span></div><div class="pos-catalog-fields"><label>Cantidad<input id="pos-qty" type="number" min="1" max="100" inputmode="numeric" value="1" aria-label="Cantidad"></label><label>Indicaciones (opcional)<input id="pos-note" maxlength="300" placeholder="Ej. sin cebolla" aria-label="Indicaciones del producto"></label></div><div class="pos-categories" role="group" aria-label="Filtrar categorías"><button type="button" class="pos-category active" data-pos-category="*">Todos</button>${categories.map((c,i)=>`<button type="button" class="pos-category" data-pos-category="${i}">${escapeHtml(c)}</button>`).join('')}</div><div class="pos-product-grid">${products.map((p,i)=>`<button type="button" class="pos-product" data-pos-add="${i}" data-pos-cat="${categories.indexOf(String(p.category||'General'))}"><span class="pos-product-name">${escapeHtml(p.name)}</span><span class="pos-product-meta">${p.station === 'kitchen' ? 'Cocina' : 'Barra'}</span><span class="pos-product-bottom"><strong>${Number(p.price||0).toLocaleString('es-MX',{style:'currency',currency:'MXN'})}</strong><span class="pos-product-plus">+</span></span></button>`).join('')}</div><p class="pos-catalog-help">Toca un producto para agregarlo a la comanda. Después pulsa «Enviar comanda».</p>`;
    const legacy = form.closest('.card');
    const title = legacy?.previousElementSibling;
    if (title?.classList.contains('section')) title.textContent = 'Selección de productos';
    if (legacy) {
      legacy.before(gallery);
      const details = document.createElement('details');
      details.className = 'pos-advanced';
      details.innerHTML = '<summary>Agregar desde el formulario clásico</summary>';
      legacy.before(details);
      details.appendChild(legacy);
    } else form.before(gallery);
    // Keep the existing app.js workflow intact; only arrange its DOM for waiter POS.
    const accountCard = content.firstElementChild;
    if (accountCard?.classList.contains('card')) accountCard.classList.add('pos-account-picker');
    const headings = [...content.children].filter(el => el.matches('h2.section'));
    for (const heading of headings) {
      if (heading.textContent.includes('Comanda por enviar')) {
        heading.classList.add('pos-draft-heading');
        const draftCard = heading.nextElementSibling;
        if (draftCard?.classList.contains('card')) {
          draftCard.classList.add('pos-draft-card');
          const subtotal = state.draft.reduce((sum, item) => {
            const p = products.find(x => Number(x.id) === Number(item.product_id));
            return sum + Number(item.qty) * Number(p?.price || 0);
          }, 0);
          const count = state.draft.reduce((sum, item) => sum + Number(item.qty), 0);
          const info = document.createElement('div');
          info.className = 'pos-draft-total';
          info.innerHTML = `<span>${count} artículo${count === 1 ? '' : 's'} · Subtotal estimado</span><strong>${subtotal.toLocaleString('es-MX',{style:'currency',currency:'MXN'})}</strong>`;
          const send = draftCard.querySelector('[data-action="send-order"]');
          if (send) {
            draftCard.insertBefore(info, send);
            send.textContent = 'Enviar comanda';
          }
          draftCard.querySelectorAll('.lineitem').forEach((line, i) => {
            const controls = document.createElement('div');
            controls.className = 'pos-qty-controls';
            controls.innerHTML = `<button type="button" data-pos-adjust="-1" data-pos-index="${i}" aria-label="Reducir cantidad">−</button><span>${Number(state.draft[i]?.qty || 0)}</span><button type="button" data-pos-adjust="1" data-pos-index="${i}" aria-label="Aumentar cantidad">+</button>`;
            line.appendChild(controls);
          });
        }
      } else if (heading.textContent.includes('Detalle y solicitudes')) {
        heading.classList.add('pos-details-heading');
        const detailCard = heading.nextElementSibling;
        if (detailCard?.classList.contains('card')) detailCard.classList.add('pos-details-card');
      } else heading.classList.add('pos-catalog-heading');
    }
    const draftCard = $('.pos-draft-card', content);
    const billCard = draftCard?.nextElementSibling;
    if (billCard?.classList.contains('card')) billCard.classList.add('pos-bill-card');
    // Preserve category choice between taps, even though app.js re-renders the screen.
    const activeCategory = $$('.pos-category', gallery).find(b => b.dataset.posCategory === selectedCategory);
    if (activeCategory) {
      $$('.pos-category', gallery).forEach(b => b.classList.toggle('active', b === activeCategory));
      $$('.pos-product', gallery).forEach(p => { p.hidden = selectedCategory !== '*' && p.dataset.posCat !== selectedCategory; });
    }
    gallery.scrollTop = catalogScroll;
  }
  function enhanceTickets() {
    if (!available()) return;
    if (!['station','tickets'].includes(state.screen)) return;
    const content = $('#content');
    if (!content || $('.pos-station-hint', content)) return;
    const hint = document.createElement('div');
    hint.className = 'pos-station-hint';
    hint.innerHTML = `${icon('chef',22)}<span>Comandas en tiempo real · Confirma cada ticket al recibirlo y márcalo listo al terminar.</span>`;
    content.prepend(hint);
  }
  let pending = false;
  function scheduleEnhance() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (!available()) return;
      const isWaiter = state.user.role === 'waiter';
      document.body.classList.toggle('pos-waiter', isWaiter);
      document.body.classList.toggle('pos-order-screen', isWaiter && state.screen === 'orders');
      document.body.classList.toggle('pos-new-screen', isWaiter && state.screen === 'new');
      document.body.classList.toggle('pos-accounts-screen', isWaiter && state.screen === 'accounts');
      if (isWaiter && state.screen === 'new') {
        const newCard = $('#content > .card');
        if (newCard) newCard.classList.add('pos-new-account-card');
      }
      ensureMoreSheet();
      enhanceNav();
      enhanceHome();
      enhanceOrder();
      enhanceTickets();
      document.body.classList.add('pos-ui-ready');
    });
  }
  document.addEventListener('click', event => {
    const btn = event.target.closest('[data-pos-go],[data-pos-add],[data-pos-adjust],[data-pos-category],[data-pos-more-button],[data-pos-close]');
    if (!btn) return;
    if (btn.hasAttribute('data-pos-go')) {
      event.preventDefault(); navTo(btn.dataset.posGo);
    } else if (btn.hasAttribute('data-pos-more-button')) {
      event.preventDefault(); openMore();
    } else if (btn.hasAttribute('data-pos-close')) {
      event.preventDefault(); closeMore();
    } else if (btn.hasAttribute('data-pos-category')) {
      event.preventDefault();
      const category = btn.dataset.posCategory;
      selectedCategory = category;
      $$('.pos-category').forEach(b => b.classList.toggle('active', b === btn));
      $$('.pos-product').forEach(p => { p.hidden = category !== '*' && p.dataset.posCat !== category; });
    } else if (btn.hasAttribute('data-pos-adjust')) {
      event.preventDefault();
      if (!available() || state.screen !== 'orders') return;
      const i = Number(btn.dataset.posIndex), delta = Number(btn.dataset.posAdjust);
      if (!Number.isInteger(i) || !state.draft[i] || ![-1,1].includes(delta)) return;
      const next = Number(state.draft[i].qty) + delta;
      if (next <= 0) state.draft.splice(i, 1);
      else if (next <= 100) state.draft[i].qty = next;
      catalogScroll = $('#pos-catalog')?.scrollTop || 0;
      if (typeof render === 'function') void render();
    } else if (btn.hasAttribute('data-pos-add')) {
      event.preventDefault();
      if (!available() || state.screen !== 'orders') return;
      const p = state.products[Number(btn.dataset.posAdd)];
      const qty = Number($('#pos-qty')?.value);
      if (!p || !Number.isInteger(qty) || qty < 1 || qty > 100) {
        if (typeof toast === 'function') toast('Indica una cantidad válida entre 1 y 100', true);
        return;
      }
      const note = $('#pos-note')?.value?.trim() || '';
      // Misma estructura de borrador que usa el formulario original.
      const existing = state.draft.find(x => Number(x.product_id) === Number(p.id) && x.note === note && Number(x.qty) + qty <= 100);
      if (existing) existing.qty = Number(existing.qty) + qty;
      else state.draft.push({product_id:p.id,name:p.name,station:p.station,qty,note});
      catalogScroll = $('#pos-catalog')?.scrollTop || 0;
      if (typeof toast === 'function') toast(`${qty} × ${p.name} agregado a la comanda`);
      if (typeof render === 'function') void render();
    }
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeMore();
  });
  function init() {
    const nav = $('#nav'), content = $('#content');
    if (!nav || !content) return;
    const obs = new MutationObserver(scheduleEnhance);
    obs.observe(nav,{childList:true,subtree:true});
    obs.observe(content,{childList:true,subtree:true});
    scheduleEnhance();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',init,{once:true});
  else init();
})();
