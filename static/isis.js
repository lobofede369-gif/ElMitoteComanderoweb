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
   Realismo (v7): timbre de teléfono al entrar; si Santiago habla mientras
   Isis habla, ella se calla y lo escucha (interrupción); si la respuesta
   tarda, suena un tono suave y el estado dice que está revisando (v13:
   antes decía una frase de espera con la voz del teléfono, que era la
   voz robótica que se oía cada vez que una petición tardaba); y cada
   ~75 segundos, entre turnos, avisa sola si apareció algo nuevo que
   necesita atención.
   Añadido (v10.5): la frase de espera sale a los 3 s (antes a 1.5 s salía
   casi siempre) y se deja terminar antes de la respuesta real, para no
   cortarla a media palabra ("Dame…").
   Voz rápida (v13): el chat va por streaming (SSE): las frases de la
   respuesta de Claude se hablan conforme se completan, sin esperar a
   la respuesta entera; si el stream falla antes del primer evento, va
   por el chat normal. La espera ya no se habla (tono suave) y la
   respuesta real ya no espera a que termine ninguna espera.
   Resumen al entrar (v8): después del saludo y las alertas, Isis dice sola
   cómo cerró el día anterior (ventas de ayer, lo más vendido, lo que se
   acabó), una sola vez por entrada al panel.
   Fluidez (v9): Dora habla por frases (la siguiente se genera mientras suena
   la actual), un poco más pausada, con respiros entre frases; la frase de
   espera va con la voz del teléfono para que salga al instante; y el
   servidor se despierta al entrar, no antes de cada mensaje.
   Voz con OpenAI (v10): cada frase se pide primero al servidor
   (POST /api/isis-voice/tts, voz coral de gpt-4o-mini-tts); si el servidor
   no tiene la llave o OpenAI falla, la frase suena con Dora y si Dora
   tampoco puede, con la voz del teléfono.
   Interruptor de voz (v10.1): dos botones, "Gratis (Dora)" y "De paga
   (OpenAI)", guardados en localStorage ('isis-voice-engine'). En modo
   gratis isis.js NUNCA llama al endpoint de TTS, así no se gasta ningún
   crédito; en modo de paga el comportamiento es el de la v10.
   Pulido de voz (v10.2): el audio llega en streaming (MediaSource) y la
   frase empieza a sonar con el primer trozo; "Probar voces" deja oír la
   misma frase en coral, nova y shimmer y guardar la favorita en
   localStorage ('isis-openai-voice'); velocidad 0.95 y pausa de 200 ms
   entre frases, igual que Dora.
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
  let kokoroAudio = null; // el <audio> de la frase actual (Dora u OpenAI)
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
  // Realismo (v7): interrupción, avisos a media llamada y timbre.
  let interruptRecognizer = null; // escucha de interrupción mientras Isis habla
  let interruptOff = false; // true al detenerla a propósito (no rearmar)
  let interruptRestarts = 0; // rearmes por respuesta hablada (tope anti-bucle)
  let speakStartedAt = 0;
  let speakGen = 0; // generación del speak en curso: el finally de un speak
  // viejo (p. ej. la frase de espera cortada por la respuesta real) no
  // debe apagar la escucha de interrupción del speak nuevo.
  const saidAlerts = new Set(); // alertas ya dichas (entrada + media llamada)
  let lastMidCheck = 0; // último chequeo de avisos a media llamada
  // Fluidez (v9, ritmo despierto en v10.5): por frases, para que no suene
  // a robot ni se trabe generando respuestas largas. La velocidad subió
  // (Dora 1.05, pausa 100 ms) porque a 0.95 con pausas de 200 ms la voz
  // sonaba lenta y adormilada en el teléfono. La espera ya no se habla
  // (v13): es un tono suave, nunca la voz del teléfono.
  const KOKORO_SPEED = 1.05; // kokoro-js: generate(text, { voice, speed })
  const PART_PAUSE_MS = 100; // respiro entre frases
  const WAIT_DELAY_MS = 3000; // el tono de espera solo sale si la respuesta sí tarda (v13: ya no se habla la frase de espera)
  const CHAT_TIMEOUT_MS = 15000; // la petición de chat no cuelga la llamada
  const OPENAI_TTS_TIMEOUT_MS = 8000; // por frase: si tarda, Dora cubre
  const STREAM_STALL_MS = 15000; // sin ningún trozo del stream en 15 s: se aborta y va por chat normal
  const STREAM_TIMEOUT_MS = 60000; // tope total del chat en streaming (Claude generando)
  let streamCtl = null; // AbortController del chat-stream en curso (se aborta al colgar o al pedir otra cosa)
  let openaiTTS = false; // v10: el servidor tiene llave de OpenAI (lo dice /status)
  // Interruptor de voz (v10.1): motor efectivo ('openai' | 'kokoro'). Se
  // resuelve al entrar con lo guardado y lo que dice el servidor; el de
  // paga solo vale si el servidor tiene llave.
  const VOICE_ENGINE_FLAG = 'isis-voice-engine';
  let voiceEngine = 'kokoro';
  // Función pura (probable sin navegador): sin elección guardada, el motor
  // es OpenAI si el servidor tiene llave y Dora si no; una elección
  // guardada de OpenAI solo vale si el servidor tiene llave.
  function resolveVoiceEngine(stored, openaiConfigured) {
    if (stored === 'kokoro') return 'kokoro';
    if (stored === 'openai') return openaiConfigured ? 'openai' : 'kokoro';
    return openaiConfigured ? 'openai' : 'kokoro';
  }
  function readStoredVoiceEngine() {
    try {
      const v = localStorage.getItem(VOICE_ENGINE_FLAG);
      return (v === 'openai' || v === 'kokoro') ? v : null;
    } catch (_) { return null; }
  }
  // La única puerta a OpenAI: en modo gratis esto es false y speak() nunca
  // pide audio al servidor, así no se gasta ningún crédito.
  function voiceEngineAllowsOpenAI() {
    return voiceEngine === 'openai' && openaiTTS;
  }
  // Refresca el interruptor: marca el motor activo, deshabilita el de paga
  // si el servidor no tiene llave y deja la nota corta.
  function applyVoiceEngine() {
    voiceEngine = resolveVoiceEngine(readStoredVoiceEngine(), openaiTTS);
    const kBtn = $('#isis-engine-kokoro');
    const oBtn = $('#isis-engine-openai');
    const note = $('#isis-engine-note');
    if (kBtn) kBtn.classList.toggle('active', voiceEngine === 'kokoro');
    if (oBtn) {
      oBtn.classList.toggle('active', voiceEngine === 'openai');
      oBtn.disabled = !openaiTTS;
    }
    if (note) {
      if (!openaiTTS) {
        note.hidden = false;
        note.textContent = 'La voz de paga no está disponible (sin llave en el servidor).';
      } else if (voiceEngine === 'kokoro') {
        note.hidden = false;
        note.textContent = 'Voz gratis (Dora): no se gastan créditos de OpenAI.';
      } else {
        note.hidden = true;
        note.textContent = '';
      }
    }
    // La prueba de voces depende del motor: en gratis se esconde.
    applyVoiceTryout();
  }
  // Cambia el motor y lo guarda: el de paga no se puede elegir si el
  // servidor no tiene llave (el botón ya está deshabilitado, esto es el
  // seguro). La próxima entrada al panel arranca con el motor elegido.
  function setVoiceEngine(engine) {
    if (engine === 'openai' && !openaiTTS) return;
    if (engine !== 'openai' && engine !== 'kokoro') return;
    try { localStorage.setItem(VOICE_ENGINE_FLAG, engine); } catch (_) {}
    applyVoiceEngine();
    const stateEl = $('#isis-voice-state');
    if (stateEl) stateEl.textContent = voiceEngine === 'openai'
      ? 'Voz de Isis: de paga (OpenAI).'
      : 'Voz de Isis: gratis (Dora).';
  }
  // Elegir la voz de OpenAI (v10.2): la favorita de Santiago entre las
  // 11 que acepta el servidor. Se guarda solo en ESTE teléfono
  // ('isis-openai-voice') y se manda en cada petición de voz; el servidor
  // sigue con su voz por defecto para los demás. Sin elección, coral.
  const OPENAI_VOICE_FLAG = 'isis-openai-voice';
  const OPENAI_VOICES = ['coral', 'nova', 'shimmer', 'alloy', 'ash', 'ballad',
                         'echo', 'fable', 'onyx', 'sage', 'verse'];
  const OPENAI_TRY_VOICES = ['coral', 'nova', 'shimmer'];
  const VOICE_TRY_SAMPLE = 'Hola Santiago, soy Isis, ¿qué mesa revisamos primero?';
  // Función pura (probable sin navegador): lo guardado solo vale si es
  // una voz de la lista; si no, coral.
  function resolveOpenAIVoice(stored) {
    return OPENAI_VOICES.includes(stored) ? stored : 'coral';
  }
  function readStoredOpenAIVoice() {
    try { return localStorage.getItem(OPENAI_VOICE_FLAG); } catch (_) { return null; }
  }
  function getOpenAIVoice() {
    return resolveOpenAIVoice(readStoredOpenAIVoice());
  }
  function setOpenAIVoice(voice) {
    if (!OPENAI_VOICES.includes(voice)) return;
    try { localStorage.setItem(OPENAI_VOICE_FLAG, voice); } catch (_) {}
    applyVoiceTryout();
  }
  // Refresca "Probar voces": la fila solo existe si el servidor tiene
  // llave; si el motor está en gratis, la prueba pide cambiar a de paga
  // y no deja oír la muestra (cada escucha gasta créditos).
  function applyVoiceTryout() {
    const row = $('#isis-try-row');
    const pick = $('#isis-try-pick');
    const note = $('#isis-try-note');
    if (row) row.hidden = !openaiTTS;
    if (pick) {
      pick.querySelectorAll('[data-try-voice]').forEach((b) =>
        b.classList.toggle('active', b.getAttribute('data-try-voice') === getOpenAIVoice()));
      if (voiceEngine !== 'openai') pick.hidden = true;
    }
    if (note) {
      if (!openaiTTS) {
        note.hidden = true;
        note.textContent = '';
      } else if (voiceEngine !== 'openai') {
        note.hidden = false;
        note.textContent = 'Para probar las voces, cambia primero a la voz de paga.';
      } else {
        note.hidden = true;
        note.textContent = '';
      }
    }
  }
  // La prueba de voces (v10.2): la misma frase en la voz elegida. Tocar
  // una voz la elige Y la hace sonar, así Santiago escucha y decide a la
  // vez. Solo en modo de paga: cada muestra gasta créditos. Al terminar,
  // si la llamada sigue, Isis vuelve a escuchar.
  async function playVoiceSample(voice) {
    if (!voiceEngineAllowsOpenAI()) { applyVoiceTryout(); return; }
    await speak(VOICE_TRY_SAMPLE, { forceVoice: voice });
    if (inCall) maybeListen();
  }
  // El reproductor en streaming que está sonando (v10.2): stopSpeaking lo
  // cancela, para que al colgar o interrumpir no siga llegando audio.
  let openaiStreamPlayer = null;
  const kokoroUrls = new Set(); // audios generados (Dora u OpenAI), por reproducir y liberar
  // La voz descargada vive solo en la memoria de la página: al recargar se
  // perdía y había que descargarla de nuevo. Por eso se guarda una marca en
  // localStorage y al abrir el panel se vuelve a cargar sola desde la caché
  // del navegador (los archivos del modelo ya están guardados ahí).
  const KOKORO_FLAG = 'isis-kokoro-ready';

  // Saludos de Isis (v5 "persona real", v12 con más variantes): rotan para
  // no sonar siempre igual. La hora es la del teléfono, así que "buenas
  // tardes" o "buenas noches" salen solas. Un poco de juego ligero en
  // algunos; nunca en confirmaciones ni datos. Bloque autocontenido (nada
  // fuera de él se usa aquí).
  // ISIS_GREETINGS_BEGIN
  const ISIS_GREETINGS = [
    'Hola, Santiago. Buenas {parte}, ¿qué necesitas?',
    'Hola, Santiago, soy Isis. Dime, ¿qué se ofrece?',
    '¿Qué onda, Santiago? Aquí estoy, ¿qué necesitas?',
    'Hola, Santiago. Te escucho, ¿qué hacemos?',
    'Hola, Santiago. Buenas {parte}, ¿qué se mueve hoy?',
    'Hola, Santiago, soy Isis. ¿Qué mesa, qué venta, qué pendiente?',
    '¿Qué tal, Santiago? Aquí ando, ¿qué se ofrece?',
    'Hola, Santiago. Buenas {parte}, ¿empezamos con las mesas o con la caja?',
  ];
  function isisGreeting(hour) {
    const parte = hour < 12 ? 'días' : hour < 19 ? 'tardes' : 'noches';
    const g = ISIS_GREETINGS[Math.floor(Math.random() * ISIS_GREETINGS.length)];
    return g.replace('{parte}', parte);
  }
  // ISIS_GREETINGS_END

  function status(text, mode) {
    const el = $('#isis-status');
    if (el) el.textContent = text || '';
    const orb = $('#isis-orb');
    if (orb) orb.className = 'isis-orb' + (mode ? ' ' + mode : '');
  }

  // Detalle escrito (v12): el análisis largo de una explicación se muestra
  // en el panel sin hablarse; solo existe cuando el servidor lo manda.
  // Se limpia con cada respuesta nueva que no trae detalle, para que no
  // quede colgado el detalle de un turno anterior.
  // ISIS_DETAIL_BEGIN
  function setDetail(text) {
    const el = $('#isis-detail');
    if (!el) return;
    if (text) { el.textContent = text; el.hidden = false; }
    else { el.textContent = ''; el.hidden = true; }
  }
  // ISIS_DETAIL_END

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

  async function speak(text, opts) {
    if (!text) return;
    stopSpeaking();
    const gen = ++speakGen;
    speaking = true;
    speakStartedAt = Date.now();
    interruptRestarts = 0;
    status('Hablando…', 'speaking');
    startInterruptListener();
    try {
      // Voz (v10.1): OpenAI solo si el interruptor está en de paga y el
      // servidor tiene llave; si no, Dora, luego teléfono.
      if (voiceEngineAllowsOpenAI()) await speakOpenAI(text, opts && opts.forceVoice);
      else if (kokoroTTS) await speakKokoro(text);
      else await speakPhone(text);
    } finally {
      // Solo apaga la escucha si este sigue siendo el speak en curso.
      if (gen === speakGen) stopInterruptListener();
    }
  }

  function stopSpeaking() {
    stopInterruptListener();
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (e) {}
    // El audio en streaming de OpenAI también se cancela: si no, seguiría
    // llegando y sonando aunque Isis ya se haya callado.
    try { if (openaiStreamPlayer) openaiStreamPlayer.cancel(); } catch (e) {}
    openaiStreamPlayer = null;
    // La voz de Kokoro no usa speechSynthesis sino un <audio>: hay que
    // pausarlo aparte o seguiría sonando al colgar.
    try { if (kokoroAudio) { kokoroAudio.pause(); kokoroAudio = null; } } catch (e) {}
    // Los audios de Dora ya generados pero aún no sonados se liberan: si no,
    // quedarían colgados en memoria tras una interrupción.
    kokoroUrls.forEach((u) => { try { URL.revokeObjectURL(u); } catch (e) {} });
    kokoroUrls.clear();
    speaking = false;
  }

  // Fluidez (v9): partir la respuesta en frases para que Dora empiece a
  // hablar en cuanto esté lista la primera, sin esperar a generar todo.
  // No se parte el punto entre dígitos ("Corona 1.2", "40.50 pesos") ni los
  // montos: solo se parte en punto, ?, !, ; o … seguidos de espacio o fin.
  // Voz despierta (v10.5): después las frases cortas se fusionan
  // (mergeSpeechParts) hasta ~160 caracteres, para que cada parte rinda
  // 2-4 s de audio y la generación (~1.5 s por frase) ya no supere a la
  // reproducción, que era lo que dejaba silencios de 1.5 s entre frases.
  function splitIntoSpeechParts(text) {
    const clean = (text || '').replace(/\s+/g, ' ').trim();
    if (!clean) return [];
    const parts = [];
    let start = 0;
    const re = /[.?!;…](?=\s|$)/g;
    let m;
    while ((m = re.exec(clean)) !== null) {
      const piece = clean.slice(start, m.index + 1).trim();
      if (piece) parts.push(piece);
      start = m.index + 1;
    }
    const tail = clean.slice(start).trim();
    if (tail) parts.push(tail);
    // Una frase muy larga también se parte por comas, para que respire y
    // ninguna parte tarde demasiado en generarse.
    const out = [];
    for (const p of parts) {
      if (p.length <= 120) { out.push(p); continue; }
      let cur = '';
      for (const seg of p.split(',')) {
        const chunk = cur ? cur + ',' + seg : seg;
        if (chunk.length > 120 && cur) { out.push(cur.trim()); cur = seg.trim(); }
        else cur = chunk;
      }
      if (cur.trim()) out.push(cur.trim());
    }
    // Voz despierta (v10.5): las frases muy cortas se fusionan hasta ~160
    // caracteres (tope ~200) para que cada parte rinda 2-4 s de audio.
    // Antes cada frase era una petición de voz aparte (~1.5 s hasta el
    // primer trozo) y con partes de 0.3-1 s la llamada quedaba llena de
    // silencios. Una parte sola más larga que 200 se queda como está.
    return mergeSpeechParts(out.filter(Boolean));
  }

  // Fusiona partes cortas consecutivas: no fusiona si la acumulada ya
  // llegó a ~160 caracteres, ni si el resultado pasaría de ~200.
  function mergeSpeechParts(parts) {
    const merged = [];
    for (const p of parts) {
      const last = merged[merged.length - 1];
      if (last && last.length < 160 && last.length + 1 + p.length <= 200) {
        merged[merged.length - 1] = last + ' ' + p;
      } else {
        merged.push(p);
      }
    }
    return merged;
  }

  // Números a palabras en la voz (v10.4): espejo en JS del normalizador
  // del servidor (_isis_tts_text en app.py). Dora y la voz del teléfono
  // leen el texto CRUDO de la respuesta — sin esto, Dora decía los números
  // dígito por dígito ("1 0 0 pesos") porque nunca pasaban por el servidor.
  // Se aplica SOLO a Dora y a la voz del teléfono (generateKokoroPart y
  // speakPhonePart): la ruta de OpenAI ya se normaliza en el servidor y no
  // debe normalizarse dos veces. El texto que se muestra en pantalla NO
  // cambia: solo cambia la copia que se habla. Los códigos (6+ dígitos
  // seguidos que no son dinero) se dejan en dígitos: Isis nunca dice PINs
  // ni claves. Port fiel de la lógica de Python, no un diseño nuevo.
  const ISIS_UNITS_ES = ['cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete',
    'ocho', 'nueve', 'diez', 'once', 'doce', 'trece', 'catorce', 'quince',
    'dieciséis', 'diecisiete', 'dieciocho', 'diecinueve'];
  const ISIS_VEINTI_ES = { 21: 'veintiuno', 22: 'veintidós', 23: 'veintitrés', 24: 'veinticuatro',
    25: 'veinticinco', 26: 'veintiséis', 27: 'veintisiete', 28: 'veintiocho', 29: 'veintinueve' };
  const ISIS_TENS_ES = { 30: 'treinta', 40: 'cuarenta', 50: 'cincuenta', 60: 'sesenta',
    70: 'setenta', 80: 'ochenta', 90: 'noventa' };
  const ISIS_HUNDREDS_ES = { 100: 'ciento', 200: 'doscientos', 300: 'trescientos', 400: 'cuatrocientos',
    500: 'quinientos', 600: 'seiscientos', 700: 'setecientos', 800: 'ochocientos', 900: 'novecientos' };

  function isisNumPalabras(n) {
    n = parseInt(n, 10);
    if (Number.isNaN(n) || n < 0 || n > 999999999) return null;
    if (n < 20) return ISIS_UNITS_ES[n];
    if (n === 20) return 'veinte';
    if (n < 30) return ISIS_VEINTI_ES[n];
    if (n < 100) {
      const d = Math.floor(n / 10), r = n % 10;
      const w = ISIS_TENS_ES[d * 10];
      return r === 0 ? w : w + ' y ' + ISIS_UNITS_ES[r];
    }
    if (n < 1000) {
      if (n === 100) return 'cien';
      const d = Math.floor(n / 100), r = n % 100;
      const w = ISIS_HUNDREDS_ES[d * 100];
      return r === 0 ? w : w + ' ' + isisNumPalabras(r);
    }
    if (n < 1000000) {
      const d = Math.floor(n / 1000), r = n % 1000;
      const w = d === 1 ? 'mil' : isisNumPalabras(d) + ' mil';
      return r === 0 ? w : w + ' ' + isisNumPalabras(r);
    }
    const d = Math.floor(n / 1000000), r = n % 1000000;
    const w = d === 1 ? 'un millón' : isisNumPalabras(d) + ' millones';
    return r === 0 ? w : w + ' ' + isisNumPalabras(r);
  }

  function isisNumPalabrasM(n) {
    // Como isisNumPalabras, pero apocopado antes de un sustantivo masculino
    // ('uno' -> 'un'): 1 -> 'un', 21 -> 'veintiún', 31 -> 'treinta y un'.
    const w = isisNumPalabras(n);
    if (w === null) return null;
    if (w === 'veintiuno') return 'veintiún';
    if (w.endsWith('uno')) return w.slice(0, -3) + 'un';
    return w;
  }

  function isisTtsText(text) {
    let t = String(text || '').replace(/\s+/g, ' ').trim();
    t = t.replace(/\$(\d+)/g, '$1 pesos'); // "$40" -> "40 pesos"
    t = t.replace(/&/g, ' y ').replace(/\+/g, ' más ').replace(/°/g, ' grados ');
    t = t.replace(/#/g, ' número ').replace(/@/g, ' arroba ');
    t = t.replace(/\.\.\./g, '…');

    t = t.replace(/(\d[\d,]*)(?:\.(\d{1,2}))?\s*pesos?\b/g, (m, g1, g2) => {
      const entero = parseInt(g1.replace(/,/g, ''), 10);
      const c = g2 ? parseInt(g2.padEnd(2, '0'), 10) : 0;
      const w = isisNumPalabrasM(entero);
      if (w === null) return m;
      let noun;
      if (entero >= 1000000) noun = 'de pesos';
      else if (entero === 1 && c === 0) noun = 'peso';
      else noun = 'pesos';
      if (c === 0) return w + ' ' + noun;
      const cw = isisNumPalabrasM(c);
      if (cw === null) return m;
      const cnoun = c === 1 ? 'centavo' : 'centavos';
      return w + ' ' + noun + ' con ' + cw + ' ' + cnoun;
    });

    t = t.replace(/(\d+(?:\.\d+)?)\s*%/g, (m, num) => {
      if (num.indexOf('.') !== -1) {
        const parts = num.split('.', 2);
        const iw = isisNumPalabras(parseInt(parts[0], 10));
        if (iw === null) return m;
        const fw = parts[1].split('').map((d) => ISIS_UNITS_ES[parseInt(d, 10)]).join(' ');
        return iw + ' punto ' + fw + ' por ciento';
      }
      const w = isisNumPalabras(parseInt(num, 10));
      return w === null ? m : w + ' por ciento';
    });

    t = t.replace(/\b(\d{1,2}):(\d{2})\b/g, (m, h, mm) => {
      const hh = parseInt(h, 10), m2 = parseInt(mm, 10);
      if (hh > 23 || m2 > 59) return m;
      const hw = isisNumPalabras(hh);
      if (m2 === 0) return hw + ' en punto';
      if (m2 === 15) return hw + ' y cuarto';
      if (m2 === 30) return hw + ' y media';
      if (m2 < 30) return hw + ' y ' + isisNumPalabras(m2);
      return hw + ' ' + isisNumPalabras(m2);
    });

    t = t.replace(/(\d[\d,]*(?:\.\d+)?)/g, (m, raw) => {
      const digits = raw.replace(/,/g, '');
      if (digits.indexOf('.') !== -1) {
        const parts = digits.split('.', 2);
        const iw = parts[0] ? isisNumPalabras(parseInt(parts[0], 10)) : null;
        if (iw === null) return m;
        const fw = parts[1].split('').map((d) => ISIS_UNITS_ES[parseInt(d, 10)]).join(' ');
        return iw + ' punto ' + fw;
      }
      if (digits.length >= 6) return m; // código/ID: se deja en dígitos
      const w = isisNumPalabras(parseInt(digits, 10));
      return w === null ? m : w;
    });

    // Espacio tras el punto si falta ("Hola.Santiago" -> "Hola. Santiago");
    // el punto entre dígitos ("1.2", "40.50") ya no existe aquí: se volvió palabras.
    t = t.replace(/([.!?…])(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])/g, '$1 ');
    t = t.replace(/\s+/g, ' ').trim();
    if (t && !/[.?!…]$/.test(t)) t += '.';
    return t;
  }

  // Interrupción (v7): si Santiago habla mientras Isis habla, ella se calla
  // y lo escucha. Un reconocedor aparte escucha solo interrupciones mientras
  // suena la voz; el eco del arranque se ignora (primer segundo) y solo
  // valen resultados finales, no parciales. El botón de micrófono no se usa
  // para esto porque en llamada ese botón es "Colgar".
  function startInterruptListener() {
    if (!SpeechRecognition || !inCall || muted || interruptRecognizer) return;
    interruptOff = false;
    try {
      const r = new SpeechRecognition();
      r.lang = 'es-MX';
      r.interimResults = true;
      r.continuous = true;
      r.maxAlternatives = 1;
      r.onresult = (e) => {
        if (!speaking || !inCall || muted) return;
        if (Date.now() - speakStartedAt < 1000) return;
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const res = e.results[i];
          if (res.isFinal && res[0] && res[0].transcript && res[0].transcript.trim()) {
            handleInterrupt(res[0].transcript.trim());
            return;
          }
        }
      };
      r.onerror = () => {};
      r.onend = () => {
        interruptRecognizer = null;
        // Se cortó solo (silencio): si Isis sigue hablando, rearmar en
        // silencio, con tope para no girar en bucle si el servicio falla.
        if (!interruptOff && speaking && inCall && !muted && interruptRestarts < 5) {
          interruptRestarts += 1;
          startInterruptListener();
        }
      };
      r.start();
      interruptRecognizer = r;
    } catch (e) { interruptRecognizer = null; }
  }

  function stopInterruptListener() {
    interruptOff = true;
    try { if (interruptRecognizer) interruptRecognizer.abort(); } catch (e) {}
    interruptRecognizer = null;
  }

  function handleInterrupt(text) {
    if (!inCall || !speaking) return;
    stopInterruptListener();
    stopListening();
    stopSpeaking();
    status('Te escucho, dime.', 'listening');
    sendToIsis(text);
  }

  // Timbre de llamada (v7): dos tonos de teléfono generados con WebAudio,
  // sin archivos externos. Suena solo al entrar al panel, antes del saludo
  // (no al volver a llamar con "Llamar a Isis"). Si el audio no está
  // permitido, no rompe nada: la llamada sigue con el saludo normal.
  async function playRingtone() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
      const ring = (t0, dur) => {
        const o1 = ctx.createOscillator();
        const o2 = ctx.createOscillator();
        const g = ctx.createGain();
        o1.frequency.value = 440;
        o2.frequency.value = 480;
        o1.connect(g); o2.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.12, t0 + 0.05);
        g.gain.setValueAtTime(0.12, t0 + dur - 0.05);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
        o1.start(t0); o2.start(t0); o1.stop(t0 + dur); o2.stop(t0 + dur);
      };
      const t = ctx.currentTime + 0.05;
      ring(t, 0.4);
      ring(t + 0.6, 0.4);
      await new Promise((resolve) => setTimeout(resolve, 1100));
      try { await ctx.close(); } catch (e) {}
    } catch (e) {}
  }

  // Tono de espera (v13): cuando la respuesta tarda, suena un bip suave
  // y el estado dice que está revisando. NADA de voz del teléfono: la
  // frase de espera hablada era justo la voz robótica que Santiago oía
  // cada vez que una petición tardaba. Usa WebAudio, como el timbre, y
  // no rompe nada si el audio no está permitido.
  async function playWaitTone() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = 'sine';
      o.frequency.value = 880;
      o.connect(g); g.connect(ctx.destination);
      const t = ctx.currentTime + 0.05;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.05, t + 0.03);
      g.gain.setValueAtTime(0.05, t + 0.12);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
      o.start(t); o.stop(t + 0.2);
      await new Promise((resolve) => setTimeout(resolve, 250));
      try { await ctx.close(); } catch (e) {}
    } catch (e) {}
  }

  // Voz del teléfono (v9): también por frases, cada una con su propio seguro
  // de 15 s: si una frase se atora, se cancela y se sigue con la siguiente,
  // en vez de dejar la llamada colgada 30 s.
  function speakPhonePart(text, gen) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      try {
        // Solo la copia para la voz pasa por el normalizador (v10.4):
        // la voz del teléfono leería "1 0 0" dígito por dígito si no.
        const u = new SpeechSynthesisUtterance(isisTtsText(text));
        u.lang = 'es-MX';
        u.rate = 1;
        const voices = window.speechSynthesis.getVoices();
        const v = voices.find((x) => /es[-_]MX/i.test(x.lang)) || voices.find((x) => /^es/i.test(x.lang));
        if (v) u.voice = v;
        // Al terminar de hablar no se pone estado aquí: quien llamó a
        // speak() decide qué sigue (en llamada, volver a escuchar).
        u.onend = finish;
        u.onerror = finish;
        window.speechSynthesis.speak(u);
        setTimeout(() => {
          if (!done && gen === speakGen) {
            try { window.speechSynthesis.cancel(); } catch (e) {}
            finish();
          }
        }, 15000);
      } catch (e) { finish(); }
    });
  }

  async function speakPhone(text, keepSpeaking) {
    const gen = speakGen;
    const parts = splitIntoSpeechParts(text);
    for (let i = 0; i < parts.length; i++) {
      if (gen !== speakGen || !speaking) return;
      await speakPhonePart(parts[i], gen);
      if (gen !== speakGen || !speaking) return;
      if (i + 1 < parts.length) await new Promise((r) => setTimeout(r, PART_PAUSE_MS));
    }
    // keepSpeaking: se usa como respaldo de una parte de Dora; el `speaking`
    // lo apaga el speak de Dora en curso, no esta parte.
    if (!keepSpeaking) speaking = false;
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
        if (stateEl) stateEl.textContent = 'No se pudo descargar la voz, toca de nuevo para reintentarlo. Mientras, hablo con la voz del teléfono.';
        return null;
      }
    })();
    return kokoroPromise;
  }

  async function generateKokoroPart(text, gen) {
    try {
      // speed KOKORO_SPEED (kokoro-js: generate(text, { voice, speed }),
      // default 1): a 1.05 Dora suena despierta; a 0.95 sonaba adormilada.
      // No hay control de
      // tono ni de emoción en kokoro-js: la naturalidad sale de la velocidad
      // y de las pausas entre frases, no de parámetros que no existen.
      // Solo la copia para la voz pasa por el normalizador (v10.4):
      // Dora leería "1 0 0" dígito por dígito si no.
      const audio = await kokoroTTS.generate(isisTtsText(text), { voice: 'ef_dora', speed: KOKORO_SPEED });
      if (gen !== speakGen || !speaking) return null;
      const blob = audio.toBlob ? audio.toBlob() : new Blob([audio], { type: 'audio/wav' });
      const url = URL.createObjectURL(blob);
      kokoroUrls.add(url);
      return url;
    } catch (e) {
      return null;
    }
  }

  // Reproduce el audio de una frase ya generada (Dora u OpenAI): el mismo
  // <audio> para ambas, para que pararlo al colgar o interrumpir sea igual.
  async function playAudioUrl(url) {
    const el = new Audio(url);
    kokoroAudio = el;
    try { await el.play(); } catch (e) {
      if (kokoroAudio === el) kokoroAudio = null;
      kokoroUrls.delete(url);
      try { URL.revokeObjectURL(url); } catch (e2) {}
      return;
    }
    await new Promise((resolve) => { el.onended = resolve; el.onerror = resolve; el.onpause = resolve; });
    if (kokoroAudio === el) kokoroAudio = null;
    kokoroUrls.delete(url);
    try { URL.revokeObjectURL(url); } catch (e) {}
  }

  // Voz de Dora (v9): por frases, con la siguiente ya generándose mientras
  // suena la actual. Antes esperaba a generar TODA la respuesta antes de
  // sonar: en respuestas largas eso era la traba principal. Kokoro además
  // corre mejor con frases cortas (100-200 tokens) que con textos largos.
  async function speakKokoro(text) {
    const gen = speakGen;
    const parts = splitIntoSpeechParts(text);
    let nextUrlPromise = null;
    for (let i = 0; i < parts.length; i++) {
      if (gen !== speakGen || !speaking) return;
      let url;
      if (nextUrlPromise) {
        url = await nextUrlPromise;
        nextUrlPromise = null;
      } else {
        url = await generateKokoroPart(parts[i], gen);
      }
      if (gen !== speakGen || !speaking) {
        if (url) { kokoroUrls.delete(url); try { URL.revokeObjectURL(url); } catch (e) {} }
        return;
      }
      if (url) {
        // Generar la siguiente parte mientras suena esta.
        if (i + 1 < parts.length) nextUrlPromise = generateKokoroPart(parts[i + 1], gen);
        await playAudioUrl(url);
      } else {
        // Si Dora falló en esta parte, voz del teléfono para no trabarse.
        await speakPhone(parts[i], true);
      }
      if (gen !== speakGen || !speaking) return;
      // Pausa corta entre frases, para que respire.
      if (i + 1 < parts.length) await new Promise((r) => setTimeout(r, PART_PAUSE_MS));
    }
    speaking = false;
  }

  // Voz con OpenAI en streaming (v10.2): el servidor manda el audio por
  // trozos en cuanto OpenAI los genera (stream=true). fetch() resuelve
  // con las cabeceras, así que pedir la frase es rápido: el reproductor
  // que devuelve es lo que suena después, por trozos (MediaSource) o
  // entero si el navegador no puede (entonces se acumula el audio y
  // suena como en la v10). Devuelve null si falló antes de sonar: esa
  // frase suena con Dora. Un 503 (sin llave en el servidor) apaga
  // OpenAI en esta llamada para no pagar un intento por frase.
  async function requestOpenAIPart(text, gen, voiceOverride) {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), OPENAI_TTS_TIMEOUT_MS);
    let r;
    try {
      r = await fetch('/api/isis-voice/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ text, voice: voiceOverride || getOpenAIVoice() }),
        cache: 'no-store',
        signal: c.signal,
      });
    } catch (e) {
      clearTimeout(t);
      return null;
    }
    clearTimeout(t);
    if (!r.ok) {
      if (r.status === 503) {
        openaiTTS = false;
        // Sin llave en el servidor: el botón de paga se deshabilita solo
        // y el resto de la llamada va con Dora.
        applyVoiceEngine();
      }
      return null;
    }
    if (gen !== speakGen || !speaking) {
      try { if (r.body && r.body.cancel) await r.body.cancel(); } catch (e) {}
      return null;
    }
    return makeOpenAIPlayer(r, gen, c);
  }

  // Reproductor de una frase de OpenAI: { play(), cancel() }. play()
  // devuelve true si la frase se oyó (aunque sea en parte) y false si no
  // sonó nada (entonces la frase suena con Dora). Con MediaSource
  // (Chrome/Android) los trozos se anexan en cuanto llegan y la frase
  // empieza a sonar con el primero; sin MediaSource se espera al audio
  // completo. Si el chorro se corta a media frase, suena lo que llegó.
  function makeOpenAIPlayer(r, gen, abortCtl) {
    const reader = (r.body && typeof r.body.getReader === 'function') ? r.body.getReader() : null;
    const canStream = !!(window.MediaSource && typeof window.MediaSource.isTypeSupported === 'function' &&
      window.MediaSource.isTypeSupported('audio/mpeg') && reader);
    let cancelled = false;
    let el = null;
    let msUrl = null;
    function cancel() {
      cancelled = true;
      try { abortCtl.abort(); } catch (e) {}
      try { if (reader && reader.cancel) reader.cancel(); } catch (e) {}
      try { if (el) el.pause(); } catch (e) {}
    }
    function cleanup() {
      if (openaiStreamPlayer && openaiStreamPlayer.cancel === cancel) openaiStreamPlayer = null;
      if (kokoroAudio === el) kokoroAudio = null;
      if (msUrl) { kokoroUrls.delete(msUrl); try { URL.revokeObjectURL(msUrl); } catch (e) {} msUrl = null; }
    }
    async function play() {
      if (cancelled || gen !== speakGen || !speaking) return false;
      openaiStreamPlayer = { cancel };
      // Sin streaming: acumular todo el audio y sonar como en la v10.
      if (!canStream) {
        try {
          const blob = await r.blob();
          if (cancelled || gen !== speakGen || !speaking || !blob || !blob.size) return false;
          const url = URL.createObjectURL(blob);
          kokoroUrls.add(url);
          await playAudioUrl(url);
          return true;
        } catch (e) {
          return false;
        } finally {
          openaiStreamPlayer = null;
        }
      }
      const ms = new MediaSource();
      msUrl = URL.createObjectURL(ms);
      kokoroUrls.add(msUrl);
      el = new Audio();
      el.src = msUrl;
      kokoroAudio = el;
      let heard = false; // al menos un trozo se anexó (la frase sí sonó)
      const endedP = new Promise((resolve) => {
        el.onended = () => resolve(true);
        el.onerror = () => resolve(false);
        el.onpause = () => resolve(true);
      });
      // Bombeo en segundo plano: anexa cada trozo en cuanto llega, en
      // orden (SourceBuffer no acepta dos appends a la vez).
      const pump = (async () => {
        try {
          if (ms.readyState !== 'open') {
            await new Promise((resolve, reject) => {
              const to = setTimeout(() => reject(new Error('sourceopen timeout')), 5000);
              const onOpen = () => { clearTimeout(to); resolve(); };
              ms.addEventListener('sourceopen', onOpen, { once: true });
            });
          }
          const sb = ms.addSourceBuffer('audio/mpeg');
          let chain = Promise.resolve();
          const queueAppend = (chunk) => {
            chain = chain.then(() => new Promise((resolve) => {
              const doAppend = () => {
                try { sb.appendBuffer(chunk); } catch (e) {}
                resolve();
              };
              if (sb.updating) {
                const onEnd = () => { try { sb.removeEventListener('updateend', onEnd); } catch (e) {} doAppend(); };
                sb.addEventListener('updateend', onEnd);
              } else {
                doAppend();
              }
            }));
            return chain;
          };
          for (;;) {
            if (cancelled || gen !== speakGen || !speaking) break;
            // Si un trozo tarda más de 10 s, se corta el chorro: suena
            // lo que llegó, en vez de quedarse en silencio para siempre.
            const res = await Promise.race([
              reader.read(),
              new Promise((resolve) => setTimeout(() => resolve('stall'), 10000)),
            ]);
            if (res === 'stall' || !res || res.done) break;
            if (res.value && res.value.byteLength) {
              heard = true;
              await queueAppend(res.value);
            }
          }
          await chain.catch(() => {});
        } catch (e) {
          // El chorro falló: si ya sonó algo, que termine con eso.
        }
        if (!cancelled && ms.readyState === 'open') {
          try { ms.endOfStream(); } catch (e) {}
        }
      })();
      try {
        await el.play();
      } catch (e) {
        cancel();
        await pump.catch(() => {});
        cleanup();
        return false;
      }
      // Esperar a que el audio termine, o a que el bombeo muera primero.
      const first = await Promise.race([
        endedP.then((ok) => ({ kind: 'ended', ok })),
        pump.then(() => ({ kind: 'pump' })),
      ]);
      if (first.kind === 'ended') {
        cancel();
        await pump.catch(() => {});
        cleanup();
        return heard || first.ok;
      }
      // El bombeo terminó primero: si no llegó nada, no hay que sonar.
      if (!heard) {
        try { el.pause(); } catch (e) {}
        cancel();
        cleanup();
        return false;
      }
      // Hay audio en el búfer: esperar a que suene entero (con tope).
      const ok = await Promise.race([
        endedP,
        new Promise((resolve) => setTimeout(() => resolve(false), 30000)),
      ]);
      cleanup();
      return heard || ok;
    }
    return { play, cancel };
  }

  // Voz con OpenAI (v10, streaming en v10.2): por frases, igual que Dora
  // (v9): las siguientes ya se piden mientras suena la actual. Voz
  // despierta (v10.5): se piden DOS frases por adelantado, no una: con
  // partes cortas la reproducción se acababa antes de que llegara la
  // siguiente y quedaban huecos de ~1.5 s. Si OpenAI falla en una frase,
  // esa frase suena con Dora (o la voz del teléfono); si falla dos veces
  // seguidas en la misma respuesta, el resto va directo con Dora para no
  // pagar el tiempo de espera frase por frase.
  async function speakOpenAI(text, voiceOverride) {
    const gen = speakGen;
    const parts = splitIntoSpeechParts(text);
    let nextPlayers = []; // v10.5: cola de hasta 2 frases ya pedidas
    let openaiFails = 0;
    let useOpenAI = true;
    // Cancela todas las frases pedidas que aún no sonaron (al interrumpir,
    // colgar, cambiar de generación o dejar de usar OpenAI).
    const cancelQueued = () => {
      for (const p of nextPlayers) p.then((pl) => { if (pl) pl.cancel(); }).catch(() => {});
      nextPlayers = [];
    };
    // Pide por adelantado las frases desde `from` hasta tener 2 en cola.
    const prefetch = (from) => {
      while (useOpenAI && nextPlayers.length < 2 && from < parts.length) {
        nextPlayers.push(requestOpenAIPart(parts[from], gen, voiceOverride));
        from++;
      }
    };
    for (let i = 0; i < parts.length; i++) {
      if (gen !== speakGen || !speaking) {
        cancelQueued();
        return;
      }
      let heard = false;
      if (useOpenAI) {
        let player = null;
        if (nextPlayers.length) {
          player = await nextPlayers.shift();
        } else {
          player = await requestOpenAIPart(parts[i], gen, voiceOverride);
        }
        if (gen !== speakGen || !speaking) {
          if (player) player.cancel();
          cancelQueued();
          return;
        }
        if (player) {
          prefetch(i + 1);
          heard = await player.play();
        }
      }
      if (heard) {
        openaiFails = 0;
      } else {
        // Sin audio (OpenAI falló o ya no se intenta): Dora, luego teléfono.
        if (useOpenAI) {
          openaiFails += 1;
          if (openaiFails >= 2) {
            useOpenAI = false;
            // Las frases que ya se estaban pidiendo con OpenAI se
            // cancelan en vez de quedar colgadas en memoria.
            cancelQueued();
          }
        }
        if (kokoroTTS) {
          const kUrl = await generateKokoroPart(parts[i], gen);
          if (kUrl) await playAudioUrl(kUrl);
          else await speakPhone(parts[i], true);
        } else {
          await speakPhone(parts[i], true);
        }
      }
      if (gen !== speakGen || !speaking) {
        cancelQueued();
        return;
      }
      // Pausa corta entre frases, para que respire.
      if (i + 1 < parts.length) await new Promise((r) => setTimeout(r, PART_PAUSE_MS));
    }
    speaking = false;
  }

  // Habla las frases que van llegando por el stream, en orden (v13).
  // Es la misma cadena que speakOpenAI por frase (OpenAI con prefetch
  // de la siguiente parte ya en cola, Dora, teléfono) pero la cola se
  // llena mientras Claude sigue respondiendo, así Isis empieza a hablar
  // sin esperar a la respuesta completa. queue: {parts: [], done: false}.
  // Devuelve cuando se hablaron todas las partes y el stream terminó.
  async function speakStreamParts(queue, gen) {
    let i = 0;
    let openaiFails = 0;
    let useOpenAI = voiceEngineAllowsOpenAI();
    let nextPlayer = null;
    const cancelNext = () => {
      if (nextPlayer) nextPlayer.then((pl) => { if (pl) pl.cancel(); }).catch(() => {});
      nextPlayer = null;
    };
    for (;;) {
      if (gen !== speakGen || !speaking) { cancelNext(); return; }
      if (i >= queue.parts.length) {
        if (queue.done) break;
        await new Promise((r) => setTimeout(r, 60));
        continue;
      }
      const part = queue.parts[i];
      let heard = false;
      if (useOpenAI) {
        let player = nextPlayer;
        nextPlayer = null;
        if (!player) player = await requestOpenAIPart(part, gen);
        if (gen !== speakGen || !speaking) { if (player) player.cancel(); cancelNext(); return; }
        if (player) {
          // Prefetch de la siguiente parte si ya llegó (v10.5: 2 en cola).
          if (i + 1 < queue.parts.length) nextPlayer = requestOpenAIPart(queue.parts[i + 1], gen);
          heard = await player.play();
        }
      }
      if (!heard) {
        if (useOpenAI) {
          openaiFails += 1;
          if (openaiFails >= 2) useOpenAI = false;
        }
        if (kokoroTTS) {
          const url = await generateKokoroPart(part, gen);
          if (url) await playAudioUrl(url);
          else await speakPhone(part, true);
        } else {
          await speakPhone(part, true);
        }
      } else {
        openaiFails = 0;
      }
      if (gen !== speakGen || !speaking) { cancelNext(); return; }
      i++;
      if (i < queue.parts.length || !queue.done) await new Promise((r) => setTimeout(r, PART_PAUSE_MS));
    }
    speaking = false;
  }

  // El servidor gratis de Render se duerme si nadie lo usa y tarda en
  // despertar: la primera petición puede fallar aunque internet esté bien.
  // Por eso se despierta una sola vez al entrar a la llamada (en show(), sin
  // esperar), y antes de cada mensaje solo un wake corto con tope: si tarda,
  // no se bloquea la llamada, el tono de espera cubre el hueco.
  async function wakeServer(timeoutMs) {
    try {
      const c = new AbortController();
      const t = setTimeout(() => c.abort(), timeoutMs || 2000);
      await fetch('/api/health', { cache: 'no-store', signal: c.signal });
      clearTimeout(t);
    } catch (e) {}
  }

  // La petición de chat con tope: si el servidor se cuelga (no solo falla),
  // no deja la llamada en silencio para siempre; el reintento es el que
  // decide si fue un error o solo lentitud. client_key (v13): una llave
  // única por frase; si el reintento repite la misma llave, el servidor
  // devuelve la respuesta guardada sin ejecutar nada dos veces.
  async function postChat(message, clientKey) {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), CHAT_TIMEOUT_MS);
    try {
      return await fetch('/api/isis-voice/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ message, confirm_token: pendingToken || undefined, client_key: clientKey || undefined }),
        cache: 'no-store',
        signal: c.signal,
      });
    } finally {
      clearTimeout(t);
    }
  }

  // Chat en streaming (v13): el servidor va soltando las frases de la
  // respuesta de Claude en cuanto se completan (eventos SSE 'part') y el
  // detalle escrito ('detail'); al final llega 'done' con el mismo JSON
  // del chat normal. Devuelve {partial, doneData} o lanza si el stream
  // falló antes del primer evento (entonces el llamador usa el chat
  // normal). Si se corta a mitad (partial), lo ya hablado no se repite:
  // el llamador pide la respuesta completa por chat normal.
  async function postChatStream(message, clientKey, handlers) {
    const c = new AbortController();
    streamCtl = c;
    const overall = setTimeout(() => { try { c.abort(); } catch (e) {} }, STREAM_TIMEOUT_MS);
    let stall = null;
    const armStall = () => {
      clearTimeout(stall);
      stall = setTimeout(() => { try { c.abort(); } catch (e) {} }, STREAM_STALL_MS);
    };
    try {
      armStall();
      const r = await fetch('/api/isis-voice/chat-stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ message, confirm_token: pendingToken || undefined, client_key: clientKey || undefined }),
        cache: 'no-store',
        signal: c.signal,
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      if (!r.body || typeof r.body.getReader !== 'function') throw new Error('sin stream');
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let sawEvent = false;
      let doneData = null;
      for (;;) {
        const res = await reader.read();
        if (res.done) break;
        armStall();
        buf += dec.decode(res.value || new Uint8Array(), { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of raw.split('\n')) {
            if (!line.startsWith('data:')) continue;
            let ev;
            try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
            sawEvent = true;
            if (ev.type === 'part') handlers.onPart(ev.text);
            else if (ev.type === 'detail') handlers.onDetail(ev.text);
            else if (ev.type === 'done') doneData = ev;
          }
        }
      }
      if (!doneData) {
        if (!sawEvent) throw new Error('stream sin eventos');
        return { partial: true, doneData: null };
      }
      return { partial: false, doneData };
    } finally {
      clearTimeout(overall);
      clearTimeout(stall);
      if (streamCtl === c) streamCtl = null;
    }
  }

  async function sendToIsis(message) {
    // Si había un stream anterior en curso (interrupción), se aborta: la
    // petición nueva lo sustituye. El cliente genera una llave única por
    // frase (v13): el reintento repite la misma llave y el servidor no
    // ejecuta nada dos veces.
    try { if (streamCtl) streamCtl.abort(); } catch (e) {}
    streamCtl = null;
    const clientKey = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : ('m' + Date.now() + Math.floor(Math.random() * 1e9));
    busy = true;
    const heard = $('#isis-heard');
    if (heard) heard.textContent = 'Tú: ' + message;
    status('Pensando…', 'thinking');
    // Espera (v13): tono suave + texto, nunca voz del teléfono. La
    // respuesta real se habla en cuanto llega, sin esperar a nada.
    let waitTonel = false;
    const waitTimer = setTimeout(async () => {
      if (!inCall || waitTonel) return;
      waitTonel = true;
      status('Estoy revisando…', 'thinking');
      await playWaitTone();
    }, WAIT_DELAY_MS);
    await wakeServer(2000);
    // Cola de frases del stream + hablador que arranca con la primera.
    const queue = { parts: [], done: false };
    let spokeStream = false;
    let speakerPromise = null;
    const startSpeaker = () => {
      if (speakerPromise) return;
      stopSpeaking();
      const gen = ++speakGen;
      speaking = true;
      speakStartedAt = Date.now();
      interruptRestarts = 0;
      status('Hablando…', 'speaking');
      startInterruptListener();
      speakerPromise = (async () => {
        try { await speakStreamParts(queue, gen); }
        finally { if (gen === speakGen) stopInterruptListener(); }
      })();
    };
    const applyDone = (data) => {
      pendingToken = data.needs_confirmation ? data.confirm_token : null;
      const reply = data.reply || 'Listo.';
      const rep = $('#isis-reply');
      if (rep) rep.textContent = 'Isis: ' + reply + (pendingToken ? ' (Di "sí" para confirmar o "no" para cancelar.)' : '');
      // v12: el detalle escrito se muestra sin hablarse; sin detalle, se limpia.
      setDetail(data.detail || '');
      return reply;
    };
    // 1) Streaming; si ni arranca, chat normal con su reintento.
    let streamRes = null;
    let streamFailedEarly = false;
    try {
      streamRes = await postChatStream(message, clientKey, {
        onPart: (text) => {
          if (!text) return;
          queue.parts.push(text);
          spokeStream = true;
          if (inCall) startSpeaker();
        },
        onDetail: (text) => { setDetail(text || ''); },
      });
    } catch (e) {
      streamFailedEarly = true;
    }
    clearTimeout(waitTimer);
    if (!streamFailedEarly && streamRes && streamRes.partial) {
      // Se cortó a mitad de una respuesta que ya se estaba hablando:
      // se pide la respuesta completa por chat normal. Nada se ejecutó
      // dos veces: la respuesta parcial era solo texto.
      queue.done = true;
      if (speakerPromise) await speakerPromise;
      streamFailedEarly = true;
    }
    if (streamFailedEarly) {
      let data = {};
      let r = null;
      try {
        r = await postChat(message, clientKey);
        if (!r.ok) throw new Error('HTTP ' + r.status);
      } catch (e) {
        // Un reintento solo, con espera corta y la misma llave.
        status('Estoy despertando el servidor, dame un momento…', 'thinking');
        await new Promise((resolve) => setTimeout(resolve, 1500));
        await wakeServer(2000);
        try {
          r = await postChat(message, clientKey);
        } catch (e2) { r = null; }
      }
      if (!r || !r.ok) {
        const reply = (r && (r.status === 401 || r.status === 403))
          ? 'Tu sesión se venció. Sal y vuelve a entrar con el PIN de Isis.'
          : 'No me pude conectar al restaurante, parece que el servidor está dormido o no hay internet. Espérame tantito e inténtalo de nuevo.';
        const rep = $('#isis-reply');
        if (rep) rep.textContent = 'Isis: ' + reply;
        setDetail('');
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
      const reply = applyDone(data);
      if (inCall) await speak(reply);
      busy = false;
      await maybeMidCallAlert();
      maybeListen();
      return;
    }
    // El stream respondió: 'done' trae el mismo JSON del chat normal.
    queue.done = true;
    const data = (streamRes && streamRes.doneData) || {};
    const reply = applyDone(data);
    if (spokeStream) {
      // Las frases ya se hablaron por el stream: no se vuelve a hablar
      // la respuesta (eso la repetiría). Solo se espera a que el
      // hablador termine y se vuelve a escuchar.
      if (speakerPromise) await speakerPromise;
    } else if (inCall) {
      await speak(reply);
    }
    busy = false;
    await maybeMidCallAlert();
    maybeListen();
  }

  // Avisos a media llamada (v7): cada ~75 segundos, entre turnos, Isis
  // revisa si apareció algo nuevo que necesita atención y lo dice en corto.
  // Nunca interrumpe a Santiago a media frase (solo corre después de que
  // Isis termina de hablar) y nunca repite una alerta ya dicha.
  async function maybeMidCallAlert() {
    if (!inCall || muted) return;
    if (Date.now() - lastMidCheck < 75000) return;
    lastMidCheck = Date.now();
    try {
      // Con tope: si el servidor está dormido, este chequeo no cuelga la
      // llamada esperando; simplemente no hay aviso en este turno.
      const alertCtl = new AbortController();
      const alertTimer = setTimeout(() => alertCtl.abort(), 8000);
      let r;
      try {
        r = await fetch('/api/isis-voice/status', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store', signal: alertCtl.signal });
      } finally {
        clearTimeout(alertTimer);
      }
      const d = await r.json().catch(() => ({}));
      if (!inCall || !Array.isArray(d.alerts)) return;
      // La llave de OpenAI puede aparecer (o desaparecer) a media llamada.
      if (typeof d.openai_tts_configured === 'boolean') {
        openaiTTS = d.openai_tts_configured;
        applyVoiceEngine();
      }
      const fresh = d.alerts.filter((a) => typeof a === 'string' && a.trim() && !saidAlerts.has(a.trim())).slice(0, 3);
      if (!fresh.length) return;
      fresh.forEach((a) => saidAlerts.add(a.trim()));
      const text = 'Oye, Santiago, te aviso que ' + fresh.join(' ');
      const rep = $('#isis-reply');
      if (rep) rep.textContent = 'Isis: ' + text;
      setDetail('');
      await speak(text);
    } catch (e) {}
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
          endCall('No pude usar el micrófono. Revisa el permiso y vuelve a llamarme.');
          return;
        }
        if (err && err !== 'no-speech') {
          listenErrors += 1;
          if (listenErrors >= 5) {
            status('El micrófono me falló varias veces. Mejor cuelga y vuelve a llamar.', '');
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
      status('Te escucho, dime.', 'listening');
    } catch (e) {
      listening = false;
      status('No pude abrir el micrófono. Revisa el permiso.', '');
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
    lastMidCheck = Date.now();
    maybeListen();
  }

  function endCall(message) {
    inCall = false;
    muted = false;
    busy = false;
    // v13: abortar el stream en curso, para que no siga llegando nada.
    try { if (streamCtl) streamCtl.abort(); } catch (e) {}
    streamCtl = null;
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
    status(message || 'Llamada terminada. Toca "Llamar a Isis" si quieres volver a empezar.', '');
  }

  function toggleMute() {
    if (!inCall) return;
    muted = !muted;
    if (muted) {
      stopListening();
      status('Micrófono en silencio, no te escucho.', '');
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
    status('Conectando…', '');
    // Despertar del servidor (v9): se dispara al entrar, sin esperar, para
    // que esté despierto cuando Santiago hable; antes cada mensaje pagaba
    // el despertar completo antes de contestar.
    wakeServer(8000);
    // Alertas proactivas (v4): hasta 3 frases cortas de lo que necesita
    // atención ahora (caja sin abrir, cuenta vieja, stock bajo,
    // autorización pendiente). Se piden solo aquí, una vez por entrada al
    // panel: si cuelga y vuelve a llamar en la misma sesión no se repiten.
    // Si el status falla o no trae alertas, la llamada sigue normal.
    let entryAlerts = [];
    let morningBrief = '';
    try {
      // Con tope: si el servidor está dormido, la entrada no se queda
      // colgada esperando el status; la llamada sigue con el saludo.
      const entryCtl = new AbortController();
      const entryTimer = setTimeout(() => entryCtl.abort(), 10000);
      let r;
      try {
        r = await fetch('/api/isis-voice/status', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store', signal: entryCtl.signal });
      } finally {
        clearTimeout(entryTimer);
      }
      const d = await r.json().catch(() => ({}));
      const stateEl = $('#isis-voice-state');
      if (stateEl && !d.fableai_configured) stateEl.textContent = 'Sin clave de FableAI, solo entiendo órdenes directas en español. La clave se pone en el servidor.';
      // Voz con OpenAI (v10): si el servidor tiene la llave, las frases
      // se piden ahí primero; si no, Dora o la voz del teléfono.
      openaiTTS = !!d.openai_tts_configured;
      if (Array.isArray(d.alerts)) entryAlerts = d.alerts.filter((a) => typeof a === 'string' && a.trim()).slice(0, 3);
      // Resumen de ayer (v8): cómo cerró el día anterior.
      if (typeof d.morning_brief === 'string' && d.morning_brief.trim()) morningBrief = d.morning_brief.trim();
    } catch (e) {}
    // Interruptor de voz (v10.1): resuelve el motor con lo guardado y lo
    // que dice el servidor, y refresca los botones (el de paga se
    // deshabilita si no hay llave). Si el status falló, openaiTTS es false
    // y el motor queda en Dora.
    applyVoiceEngine();
    const stateEl = $('#isis-voice-state');
    if (stateEl && voiceEngine === 'openai') stateEl.textContent = 'Voz de Isis: de paga (OpenAI).';
    // Las alertas de entrada ya quedan marcadas como dichas, para que los
    // avisos de media llamada no las repitan.
    entryAlerts.forEach((a) => saidAlerts.add(a.trim()));
    // Si la voz ya se descargó una vez, cargarla sola al entrar (desde la
    // caché del navegador, sin descargar de nuevo) para que el saludo y las
    // respuestas ya suenen con la voz de Dora. Si tarda más de 10 s, seguir
    // con la voz del teléfono para no dejarte esperando: la carga sigue en
    // segundo plano y Dora se activa sola en cuanto esté lista.
    let kokoroReady = false;
    try {
      if (localStorage.getItem(KOKORO_FLAG) === '1') {
        const stateEl = $('#isis-voice-state');
        if (stateEl) stateEl.textContent = 'Cargando la voz de Isis…';
        kokoroReady = !!(await Promise.race([
          loadKokoro(),
          new Promise((resolve) => setTimeout(() => resolve(null), 10000)),
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
    // Timbre corto de teléfono antes del saludo (v7), como cuando te
    // contestan. Solo al entrar; al volver a llamar no vuelve a sonar.
    await playRingtone();
    // Saludo corto y distinto cada vez (v5), según la hora del teléfono.
    // La nota de Dora solo sale si el motor no es OpenAI (v10.1): con
    // OpenAI el saludo ya suena con esa voz aunque Dora no esté lista.
    const voiceNote = (!kokoroReady && voiceEngine !== 'openai') ? ' (La voz de Dora todavía no está lista, toca "Descargar voz de Isis".)' : '';
    const greet = isisGreeting(new Date().getHours());
    const rep = $('#isis-reply');
    if (rep) rep.textContent = 'Isis: ' + greet + voiceNote;
    setDetail('');
    inCall = true;
    muted = false;
    busy = false;
    updateCallUI();
    startCallTimer();
    lastMidCheck = Date.now();
    await speak(greet);
    // Después del saludo, Isis avisa lo que necesita atención ahora, en
    // corto (una frase por alerta, "Ojo:" al inicio si hay más de una).
    // Solo si la llamada sigue activa (no colgó durante el saludo).
    if (entryAlerts.length && inCall) {
      const alertsText = (entryAlerts.length > 1 ? 'Ojo: ' : '') + entryAlerts.join(' ');
      if (rep) rep.textContent = 'Isis: ' + greet + voiceNote + ' ' + alertsText;
      await speak(alertsText);
    }
    // Después de las alertas, Isis dice sola cómo cerró ayer (v8), en
    // corto, una sola vez por entrada. Solo si la llamada sigue activa.
    if (morningBrief && inCall) {
      if (rep) rep.textContent = (rep.textContent || 'Isis: ') + ' ' + morningBrief;
      await speak(morningBrief);
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
    const engK = $('#isis-engine-kokoro');
    if (engK) engK.addEventListener('click', () => setVoiceEngine('kokoro'));
    const engO = $('#isis-engine-openai');
    if (engO) engO.addEventListener('click', () => setVoiceEngine('openai'));
    // Probar voces (v10.2): muestra las tres voces y suena la muestra en
    // la favorita guardada; tocar una voz la elige Y la hace sonar.
    const tryBtn = $('#isis-try-voices');
    if (tryBtn) tryBtn.addEventListener('click', () => {
      if (!voiceEngineAllowsOpenAI()) { applyVoiceTryout(); return; }
      const pick = $('#isis-try-pick');
      if (pick) pick.hidden = false;
      playVoiceSample(getOpenAIVoice());
    });
    document.querySelectorAll('[data-try-voice]').forEach((b) =>
      b.addEventListener('click', () => {
        const v = b.getAttribute('data-try-voice');
        if (!voiceEngineAllowsOpenAI()) { applyVoiceTryout(); return; }
        setOpenAIVoice(v);
        playVoiceSample(v);
      }));
    // El interruptor arranca con lo guardado (el status de la entrada lo
    // corrige en cuanto dice si hay llave de OpenAI o no).
    applyVoiceEngine();
    applyVoiceTryout();
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

  // _test es solo para las pruebas locales (sin navegador): expone la
  // decisión del motor y el habla para verificar que en modo gratis no se
  // llama nunca al endpoint de OpenAI; sendToIsis con setInCall sirve
  // para verificar el tono de espera en VM (que no use la voz del
  // teléfono y que colgar lo corte de inmediato).
  window.IsisVoice = { show, startListening, stopListening, loadKokoro, exit, startCall, endCall, toggleMute,
    _test: { resolveVoiceEngine, getVoiceEngine: () => voiceEngine, setVoiceEngine,
      resolveOpenAIVoice, getOpenAIVoice, setOpenAIVoice, playVoiceSample, isisTtsText,
      splitIntoSpeechParts, mergeSpeechParts, setDetail, playWaitTone,
      requestOpenAIPart, makeOpenAIVoicePlayer: makeOpenAIPlayer,
      setOpenaiAvailable: (v) => { openaiTTS = !!v; applyVoiceEngine(); }, speak,
      sendToIsis, setInCall: (v) => { inCall = !!v; } } };
})();
