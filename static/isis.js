'use strict';
/* Isis embebida en El Mitote: panel SOLO por voz, en modo llamada.
   Al entrar con el PIN la llamada arranca sola: Isis saluda y empieza a
   escuchar sin tocar nada. Se habla de corrido, como por teléfono:
   cuando Isis termina de responder, vuelve a escuchar sola; mientras
   ella habla no escucha (para no oírse a sí misma). Colgar termina la
   llamada y deja el botón "Llamar a Isis" para volver a empezar.
   Al entrar, Isis avisa por voz lo que necesita atención ahora
   (alertas del servidor, una sola vez por entrada) y al colgar le pide
   al servidor que borre el historial de la llamada.
   Reconocimiento: Web Speech API (es-MX). Voz: Kokoro ef_dora, que se
   descarga solo cuando el usuario toca "Descargar voz de Isis" (~88 MB,
   una sola vez); si no esta descargada, habla con la voz del telefono. */
(function () {
  const $ = (s) => document.querySelector(s);
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  let token = '';
  let listening = false;
  let recognizer = null;
  let pendingToken = null;
  let kokoroState = 'idle'; // idle | loading | ready | failed
  let kokoroTTS = null;
  let kokoroPromise = null;
  let speaking = false;
  let kokoroAudio = null; // el <audio> de la respuesta actual de Dora
  // Modo llamada: inCall = la llamada está activa; muted = micrófono
  // silenciado (la llamada sigue, Isis deja de oírte); busy = Isis está
  // pensando o contestando, así que todavía no se debe escuchar.
  let inCall = false;
  let muted = false;
  let busy = false;
  let listenTimer = null;
  let listenErrors = 0; // errores seguidos de reconocimiento (sin contar no-speech)
  let callStart = 0;
  let callTimerId = null;
  // La voz descargada vive solo en la memoria de la página: al recargar se
  // perdía y había que descargarla de nuevo. Por eso se guarda una marca en
  // localStorage y al abrir el panel se vuelve a cargar sola desde la caché
  // del navegador (los archivos del modelo ya están guardados ahí).
  const KOKORO_FLAG = 'isis-kokoro-ready';

  function status(text, mode) {
    const el = $('#isis-status');
    if (el) el.textContent = text || '';
    const orb = $('#isis-orb');
    if (orb) orb.className = 'isis-orb' + (mode ? ' ' + mode : '');
  }

  // El botón grande es el control de la llamada (Llamar / Colgar) y el
  // botón chico es el silencio. El estado de los botones lo manda inCall.
  function updateCallUI() {
    const mic = $('#isis-mic');
    if (mic) {
      mic.textContent = inCall ? 'Colgar' : 'Llamar a Isis';
      mic.classList.toggle('stop', inCall);
      // Sin reconocimiento de voz (p. ej. WebView de la app instalada) no
      // se puede llamar: el botón queda deshabilitado.
      mic.disabled = !SpeechRecognition;
    }
    const mute = $('#isis-mute');
    if (mute) {
      mute.hidden = !inCall;
      mute.textContent = muted ? 'Quitar silencio' : 'Silenciar micrófono';
      mute.classList.toggle('muted', muted);
    }
  }

  function fmtTime(ms) {
    const s = Math.floor(ms / 1000);
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  function updateTimer() {
    const el = $('#isis-call-timer');
    if (el) el.textContent = 'En llamada con Isis · ' + fmtTime(Date.now() - callStart);
  }

  function startCallTimer() {
    callStart = Date.now();
    const el = $('#isis-call-timer');
    if (el) el.hidden = false;
    updateTimer();
    clearInterval(callTimerId);
    callTimerId = setInterval(updateTimer, 1000);
  }

  function stopCallTimer() {
    clearInterval(callTimerId);
    callTimerId = null;
    const el = $('#isis-call-timer');
    if (el) el.hidden = true;
  }

  function speak(text) {
    if (!text) return Promise.resolve();
    stopSpeaking();
    speaking = true;
    status('Isis hablando…', 'speaking');
    if (kokoroTTS) return speakKokoro(text);
    return speakPhone(text);
  }

  function stopSpeaking() {
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (e) {}
    // La voz de Kokoro no usa speechSynthesis sino un <audio>: hay que
    // pausarlo aparte o seguiría sonando al colgar.
    try { if (kokoroAudio) { kokoroAudio.pause(); kokoroAudio = null; } } catch (e) {}
    speaking = false;
  }

  function speakPhone(text) {
    return new Promise((resolve) => {
      try {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = 'es-MX';
        u.rate = 1;
        const voices = window.speechSynthesis.getVoices();
        const v = voices.find((x) => /es[-_]MX/i.test(x.lang)) || voices.find((x) => /^es/i.test(x.lang));
        if (v) u.voice = v;
        // Al terminar de hablar no se pone estado aquí: quien llamó a
        // speak() decide qué sigue (en llamada, volver a escuchar).
        u.onend = () => { speaking = false; resolve(); };
        u.onerror = () => { speaking = false; resolve(); };
        window.speechSynthesis.speak(u);
        setTimeout(() => { if (speaking) { speaking = false; resolve(); } }, 30000);
      } catch (e) { speaking = false; resolve(); }
    });
  }

  async function loadKokoro(opts) {
    if (kokoroState === 'ready') return kokoroTTS;
    if (kokoroState === 'loading') return kokoroPromise;
    kokoroState = 'loading';
    const stateEl = $('#isis-voice-state');
    if (stateEl) stateEl.textContent = 'Descargando la voz de Isis (~88 MB, una sola vez)…';
    kokoroPromise = (async () => {
      try {
        const mod = await import('https://cdn.jsdelivr.net/npm/kokoro-js@1.2.1/+esm');
        // kokoro-js 1.2.1 usa from_pretrained con el modelo ONNX, no .from()
        // con 'hexgrad/Kokoro-82M' (ese repo no tiene archivos ONNX y fallaba).
        const tts = await mod.KokoroTTS.from_pretrained('onnx-community/Kokoro-82M-v1.0-ONNX', { dtype: 'q8', device: 'wasm' });
        kokoroTTS = tts;
        kokoroState = 'ready';
        try { localStorage.setItem(KOKORO_FLAG, '1'); } catch (_) {}
        if (stateEl) stateEl.textContent = 'Voz de Isis lista (Dora).';
        const btn = $('#isis-download-voice');
        if (btn) btn.textContent = 'Voz de Isis lista ✓';
        // Que se escuche el cambio de inmediato con la voz nueva.
        if (opts && opts.announce) {
          await speak('Hola, soy Isis. Ya tengo mi voz lista.');
          if (inCall) maybeListen();
        }
        return tts;
      } catch (e) {
        try { console.error('Kokoro:', e); } catch (_) {}
        kokoroTTS = null;
        kokoroState = 'idle'; // permite reintentar tocando de nuevo
        kokoroPromise = null;
        try { localStorage.removeItem(KOKORO_FLAG); } catch (_) {}
        if (stateEl) stateEl.textContent = 'No se pudo descargar la voz; toca de nuevo para reintentar. Mientras tanto, Isis hablará con la voz del teléfono.';
        return null;
      }
    })();
    return kokoroPromise;
  }

  async function speakKokoro(text) {
    try {
      const audio = await kokoroTTS.generate(text, { voice: 'ef_dora' });
      const blob = audio.toBlob ? audio.toBlob() : new Blob([audio], { type: 'audio/wav' });
      const url = URL.createObjectURL(blob);
      const el = new Audio(url);
      kokoroAudio = el;
      await el.play();
      await new Promise((resolve) => { el.onended = resolve; el.onerror = resolve; el.onpause = resolve; });
      kokoroAudio = null;
      URL.revokeObjectURL(url);
    } catch (e) {
      kokoroAudio = null;
      await speakPhone(text);
      return;
    }
    speaking = false;
  }

  // El servidor gratis de Render se duerme si nadie lo usa y tarda en
  // despertar: la primera petición puede fallar aunque internet esté bien.
  // Por eso, antes de hablar con Isis se despierta el servidor y, si la
  // petición falla, se reintenta una vez sola.
  async function wakeServer() {
    try {
      const c = new AbortController();
      const t = setTimeout(() => c.abort(), 8000);
      await fetch('/api/health', { cache: 'no-store', signal: c.signal });
      clearTimeout(t);
    } catch (e) {}
  }

  async function sendToIsis(message) {
    busy = true;
    const heard = $('#isis-heard');
    if (heard) heard.textContent = 'Tú: ' + message;
    status('Isis pensando…', 'thinking');
    await wakeServer();
    let data = {};
    let r = null;
    try {
      r = await fetch('/api/isis-voice/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ message, confirm_token: pendingToken || undefined }),
        cache: 'no-store',
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
    } catch (e) {
      // Un reintento solo, por si el servidor apenas estaba despertando.
      status('El servidor se está despertando, reintentando…', 'thinking');
      await new Promise((resolve) => setTimeout(resolve, 4000));
      await wakeServer();
      try {
        r = await fetch('/api/isis-voice/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
          body: JSON.stringify({ message, confirm_token: pendingToken || undefined }),
          cache: 'no-store',
        });
      } catch (e2) { r = null; }
    }
    if (!r || !r.ok) {
      const reply = (r && (r.status === 401 || r.status === 403))
        ? 'Tu sesión expiró. Sal y vuelve a entrar con el PIN de Isis.'
        : 'No pude conectar con el restaurante, parece que el servidor está dormido o sin internet. Espera un momento e inténtalo de nuevo.';
      const rep = $('#isis-reply');
      if (rep) rep.textContent = 'Isis: ' + reply;
      // Si el usuario colgó mientras llegaba la respuesta, ya no se habla.
      if (inCall) await speak(reply);
      busy = false;
      maybeListen();
      return;
    }
    try {
      data = await r.json();
    } catch (e) {
      data = {};
    }
    pendingToken = data.needs_confirmation ? data.confirm_token : null;
    const reply = data.reply || 'Listo.';
    const rep = $('#isis-reply');
    if (rep) rep.textContent = 'Isis: ' + reply + (pendingToken ? ' (Di “sí, confirma” o “no”.)' : '');
    if (inCall) await speak(reply);
    busy = false;
    maybeListen();
  }

  // Vuelve a escuchar solo si la llamada sigue activa, el micrófono no
  // está silenciado y Isis no está pensando ni hablando.
  function maybeListen() {
    if (!inCall || muted || listening || speaking || busy) return;
    if (!SpeechRecognition) return;
    clearTimeout(listenTimer);
    listenTimer = setTimeout(startListening, 700);
  }

  function startListening() {
    if (!SpeechRecognition) {
      status('Este navegador no tiene reconocimiento de voz. Usa Chrome en Android.', '');
      return;
    }
    stopSpeaking();
    try {
      recognizer = new SpeechRecognition();
      recognizer.lang = 'es-MX';
      recognizer.interimResults = false;
      recognizer.maxAlternatives = 1;
      recognizer.onresult = (e) => {
        const text = e.results[0][0].transcript || '';
        listening = false;
        listenErrors = 0;
        if (text.trim()) sendToIsis(text.trim());
        else maybeListen();
      };
      recognizer.onerror = (e) => {
        listening = false;
        const err = (e && e.error) || '';
        if (err === 'not-allowed' || err === 'service-not-allowed') {
          // Sin permiso de micrófono no tiene caso reintentar en bucle.
          endCall('No pude usar el micrófono. Revisa el permiso y vuelve a llamar.');
          return;
        }
        if (err && err !== 'no-speech') {
          listenErrors += 1;
          if (listenErrors >= 5) {
            status('El reconocimiento de voz falló varias veces. Cuelga y vuelve a llamar.', '');
            return;
          }
        }
        // 'no-speech' (silencio) y demás errores: volver a escuchar en
        // silencio, sin spamear mensajes de error.
        maybeListen();
      };
      recognizer.onend = () => {
        // El reconocimiento se corta solo por silencio: si la llamada
        // sigue, volver a escuchar. Si Isis está contestando (busy) o
        // hablando, no: sendToIsis vuelve a escuchar al terminar.
        listening = false;
        maybeListen();
      };
      recognizer.start();
      listening = true;
      listenErrors = 0; // arrancó bien: los errores de antes no cuentan
      status('Escuchando… habla ahora.', 'listening');
    } catch (e) {
      listening = false;
      status('No pude abrir el micrófono.', '');
    }
  }

  function stopListening() {
    listening = false;
    clearTimeout(listenTimer);
    try { recognizer && recognizer.stop(); } catch (e) {}
  }

  // La llamada arranca sola al entrar: temporizador desde el inicio,
  // saludo de Isis y, al terminar de hablarlo, a escuchar.
  function startCall() {
    if (!SpeechRecognition) {
      status('Este navegador no tiene reconocimiento de voz. Usa Chrome en Android.', '');
      return;
    }
    inCall = true;
    muted = false;
    busy = false;
    updateCallUI();
    startCallTimer();
    maybeListen();
  }

  function endCall(message) {
    inCall = false;
    muted = false;
    busy = false;
    stopListening();
    stopSpeaking();
    stopCallTimer();
    updateCallUI();
    // Limpieza en el servidor (v4): borrar el historial de la llamada.
    // Best effort: no se espera el resultado ni se bloquea el colgado si
    // el servidor no responde (el historial expira solo a los 30 min).
    try {
      fetch('/api/isis-voice/end-call', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      }).catch(() => {});
    } catch (e) {}
    status(message || 'Llamada terminada. Toca "Llamar a Isis" para volver a empezar.', '');
  }

  function toggleMute() {
    if (!inCall) return;
    muted = !muted;
    if (muted) {
      stopListening();
      status('Micrófono silenciado. Isis no te escucha.', '');
    } else {
      maybeListen();
    }
    updateCallUI();
  }

  async function show(state) {
    token = (state && state.token) || localStorage.getItem('mitote-token') || '';
    const panel = $('#isis-panel');
    if (panel) panel.hidden = false;
    // El botón de llamar queda bloqueado hasta que el saludo empiece: si
    // no, se podría llamar antes de tiempo e Isis se escucharía a sí misma.
    const micBtn = $('#isis-mic');
    if (micBtn) micBtn.disabled = true;
    status('Conectando con Isis…', '');
    // Alertas proactivas (v4): hasta 3 frases cortas de lo que necesita
    // atención ahora (caja sin abrir, cuenta vieja, stock bajo,
    // autorización pendiente). Se piden solo aquí, una vez por entrada al
    // panel: si cuelga y vuelve a llamar en la misma sesión no se repiten.
    // Si el status falla o no trae alertas, la llamada sigue normal.
    let entryAlerts = [];
    try {
      const r = await fetch('/api/isis-voice/status', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      const stateEl = $('#isis-voice-state');
      if (stateEl && !d.fableai_configured) stateEl.textContent = 'Sin clave de FableAI: Isis entiende órdenes directas en español. El propietario la configura en el servidor.';
      if (Array.isArray(d.alerts)) entryAlerts = d.alerts.filter((a) => typeof a === 'string' && a.trim()).slice(0, 3);
    } catch (e) {}
    // Si la voz ya se descargó una vez, cargarla sola al entrar (desde la
    // caché del navegador, sin descargar de nuevo) para que el saludo y las
    // respuestas ya suenen con la voz de Dora. Si tarda más de 25 s, seguir
    // con la voz del teléfono para no dejarte esperando.
    let kokoroReady = false;
    try {
      if (localStorage.getItem(KOKORO_FLAG) === '1') {
        const stateEl = $('#isis-voice-state');
        if (stateEl) stateEl.textContent = 'Cargando la voz de Isis…';
        kokoroReady = !!(await Promise.race([
          loadKokoro(),
          new Promise((resolve) => setTimeout(() => resolve(null), 25000)),
        ]));
      }
    } catch (e) {}
    // Sin reconocimiento de voz no se puede llamar: se avisa, el botón de
    // llamar queda deshabilitado y no arranca el temporizador.
    if (!SpeechRecognition) {
      updateCallUI();
      status('Este navegador no tiene reconocimiento de voz. Usa Chrome en Android.', '');
      return;
    }
    const greet = 'Hola, soy Isis, tu asistente de El Mitote. Te ayudo con todo lo del restaurante: puedo decirte qué mesas están libres, cuánto debe una mesa, cuánto queda en inventario, cómo va la caja, o hacer el corte. Dime, ¿qué necesitas?';
    const rep = $('#isis-reply');
    if (rep) rep.textContent = 'Isis: ' + greet + (kokoroReady ? '' : ' (Voz de Dora aún no cargada: toca "Descargar voz de Isis".)');
    inCall = true;
    muted = false;
    busy = false;
    updateCallUI();
    startCallTimer();
    await speak(greet);
    // Después del saludo, Isis avisa lo que necesita atención ahora, en
    // corto (una frase por alerta, "Ojo:" al inicio si hay más de una).
    // Solo si la llamada sigue activa (no colgó durante el saludo).
    if (entryAlerts.length && inCall) {
      const alertsText = (entryAlerts.length > 1 ? 'Ojo: ' : '') + entryAlerts.join(' ');
      if (rep) rep.textContent = 'Isis: ' + greet + (kokoroReady ? '' : ' (Voz de Dora aún no cargada: toca "Descargar voz de Isis".)') + ' ' + alertsText;
      await speak(alertsText);
    }
    maybeListen();
  }

  function exit() {
    inCall = false;
    stopListening();
    stopSpeaking();
    stopCallTimer();
    localStorage.removeItem('mitote-token');
    sessionStorage.removeItem('mitote-token');
    location.reload();
  }

  document.addEventListener('DOMContentLoaded', () => {
    const mic = $('#isis-mic');
    if (mic) mic.addEventListener('click', () => (inCall ? endCall() : startCall()));
    const mute = $('#isis-mute');
    if (mute) mute.addEventListener('click', toggleMute);
    const dl = $('#isis-download-voice');
    if (dl) dl.addEventListener('click', () => loadKokoro({ announce: true }));
    const ex = $('#isis-exit');
    if (ex) ex.addEventListener('click', exit);
    const tg = $('#isis-toggle-chat');
    if (tg) tg.addEventListener('click', () => {
      const card = document.querySelector('.isis-card');
      if (!card) return;
      card.hidden = !card.hidden;
      tg.textContent = card.hidden ? 'Ver conversación' : 'Ocultar conversación';
    });
  });

  window.IsisVoice = { show, startListening, stopListening, loadKokoro, exit, startCall, endCall, toggleMute };
})();
