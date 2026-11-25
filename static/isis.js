'use strict';
/* Isis embebida en El Mitote: panel SOLO por voz.
   No hay botones para operar el restaurante: todo se pide hablando.
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
    const mic = $('#isis-mic');
    if (mic) {
      mic.textContent = listening ? '■ Parar de escuchar' : '🎙 Hablar con Isis';
      mic.classList.toggle('stop', listening);
    }
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
        u.onend = () => { speaking = false; status('Toca el micrófono y habla.', ''); resolve(); };
        u.onerror = () => { speaking = false; status('Toca el micrófono y habla.', ''); resolve(); };
        window.speechSynthesis.speak(u);
        setTimeout(() => { if (speaking) { speaking = false; status('Toca el micrófono y habla.', ''); resolve(); } }, 30000);
      } catch (e) { speaking = false; status('Toca el micrófono y habla.', ''); resolve(); }
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
        if (opts && opts.announce) await speak('Hola, soy Isis. Ya tengo mi voz lista.');
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
      await el.play();
      await new Promise((resolve) => { el.onended = resolve; el.onerror = resolve; });
      URL.revokeObjectURL(url);
    } catch (e) {
      await speakPhone(text);
      return;
    }
    speaking = false;
    status('Toca el micrófono y habla.', '');
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
      await speak(reply);
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
    await speak(reply);
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
        if (text.trim()) sendToIsis(text.trim());
        else status('No te escuché. Toca el micrófono y habla.', '');
      };
      recognizer.onerror = () => { listening = false; status('No te escuché. Toca el micrófono y habla.', ''); };
      recognizer.onend = () => { if (listening) { listening = false; status('Toca el micrófono y habla.', ''); } };
      recognizer.start();
      listening = true;
      status('Escuchando… habla ahora.', 'listening');
    } catch (e) {
      listening = false;
      status('No pude abrir el micrófono.', '');
    }
  }

  function stopListening() {
    listening = false;
    try { recognizer && recognizer.stop(); } catch (e) {}
    status('Toca el micrófono y habla.', '');
  }

  async function show(state) {
    token = (state && state.token) || localStorage.getItem('mitote-token') || '';
    const panel = $('#isis-panel');
    if (panel) panel.hidden = false;
    status('Toca el micrófono y habla.', '');
    try {
      const r = await fetch('/api/isis-voice/status', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      const stateEl = $('#isis-voice-state');
      if (stateEl && !d.fableai_configured) stateEl.textContent = 'Sin clave de FableAI: Isis entiende órdenes directas en español. El propietario la configura en el servidor.';
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
    const greet = 'Hola, soy Isis, tu asistente de El Mitote. Te ayudo con todo lo del restaurante: puedo decirte qué mesas están libres, cuánto debe una mesa, cuánto queda en inventario, cómo va la caja, o hacer el corte. Dime, ¿qué necesitas?';
    const rep = $('#isis-reply');
    if (rep) rep.textContent = 'Isis: ' + greet + (kokoroReady ? '' : ' (Voz de Dora aún no cargada: toca "Descargar voz de Isis".)');
    speak(greet);
  }

  function exit() {
    stopListening();
    stopSpeaking();
    localStorage.removeItem('mitote-token');
    sessionStorage.removeItem('mitote-token');
    location.reload();
  }

  document.addEventListener('DOMContentLoaded', () => {
    const mic = $('#isis-mic');
    if (mic) mic.addEventListener('click', () => (listening ? stopListening() : startListening()));
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

  window.IsisVoice = { show, startListening, stopListening, loadKokoro, exit };
})();
