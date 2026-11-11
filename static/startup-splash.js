/* El Mitote Comandas · inicio verificado sin recompilar la APK */
(() => {
  const byId = id => document.getElementById(id);
  const splash = byId('startup-splash');
  let busy = false;
  let lastOptions = null;
  let currentProgress = 0;

  function progress(value, message) {
    currentProgress = Math.max(currentProgress, Math.min(100, Math.round(Number(value) || 0)));
    const fill = byId('startup-progress-fill');
    const number = byId('startup-progress-value');
    const status = byId('startup-message');
    if (fill) fill.style.width = currentProgress + '%';
    if (number) number.textContent = currentProgress + '%';
    if (status && message) status.textContent = message;
  }

  function hide() {
    if (!splash) return;
    splash.classList.add('is-hidden');
    splash.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('startup-active');
  }

  function showError(message) {
    const status = byId('startup-message');
    const error = byId('startup-error');
    const retry = byId('startup-retry');
    if (status) status.textContent = 'No pudimos iniciar el sistema';
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
    if (retry) retry.hidden = false;
  }

  async function boot(options = {}) {
    if (!splash || busy) return;
    busy = true;
    lastOptions = options;
    currentProgress = 0;
    const error = byId('startup-error');
    const retry = byId('startup-retry');
    if (error) error.hidden = true;
    if (retry) {
      retry.hidden = true;
      retry.disabled = true;
    }

    try {
      progress(8, 'Iniciando El Mitote Comandas…');
      const logo = byId('startup-logo');
      if (logo && typeof logo.decode === 'function') {
        await logo.decode();
      } else if (logo && !logo.complete) {
        await new Promise((resolve, reject) => {
          logo.addEventListener('load', resolve, { once: true });
          logo.addEventListener('error', () => reject(new Error('No se pudo cargar el logotipo.')), { once: true });
        });
      }

      progress(20, 'Preparando la interfaz…');
      if (!navigator.onLine) {
        throw new Error('No hay conexión a internet. Conéctate y vuelve a intentarlo.');
      }

      progress(36, 'Conectando con el servidor…');
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 45000);
      let response;
      try {
        response = await fetch('/api/health?startup=' + Date.now(), {
          method: 'GET',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
          signal: controller.signal
        });
      } finally {
        clearTimeout(timeoutId);
      }

      progress(72, 'Verificando el servidor y la base de datos…');
      if (!response.ok) {
        if (response.status === 503) {
          throw new Error('El servidor respondió, pero la base de datos todavía no está disponible. Intenta de nuevo en unos segundos.');
        }
        throw new Error('No se pudo verificar el servidor (HTTP ' + response.status + ').');
      }

      const health = await response.json();
      if (health?.ok !== true || health.database !== 'ok') {
        throw new Error('El servidor no confirmó que la base de datos esté lista.');
      }

      progress(82, 'Servidor y base de datos listos.');
      if (options.hasToken && typeof options.restoreSession === 'function') {
        progress(88, 'Restaurando tu sesión…');
        await options.restoreSession();
        progress(97, 'Preparando tu espacio de trabajo…');
      } else {
        progress(96, 'Preparando el acceso del personal…');
      }

      progress(100, '¡Listo!');
      await new Promise(resolve => setTimeout(resolve, 220));
      hide();
    } catch (error) {
      const message = error?.name === 'AbortError'
        ? 'El servidor tardó demasiado en responder. Pulsa Reintentar.'
        : (error?.message || 'No se pudo iniciar el sistema.');
      showError(message);
    } finally {
      busy = false;
      if (retry) retry.disabled = false;
    }
  }

  const retry = byId('startup-retry');
  if (retry) retry.addEventListener('click', () => { void boot(lastOptions || {}); });

  window.MitoteStartup = { boot, progress, hide };
})();
