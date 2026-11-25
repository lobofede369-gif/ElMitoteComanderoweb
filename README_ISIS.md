# Isis embebida en El Mitote (nueva, desde cero)

Isis vive DENTRO de esta misma app: mismo servicio de Render, misma base
de datos, mismos endpoints. No es la Isis de Render de Santiago (esa queda
aparte, como su asistente personal) y no usa el token `ISIS_API_TOKEN` ni
ningún proxy externo.

## Cómo entra

- El propietario crea el usuario Isis UNA vez, con su sesión de propietario:
  `POST /api/isis-voice/setup` con `{"pin": "TU_PIN_DE_6_DIGITOS"}` (6 dígitos, distinto
  al de cualquier otra persona). Si ya existe, ese mismo endpoint solo le
  cambia el PIN.
- Isis entra como todo el personal: en la pantalla de PIN escribe su código
  de 6 dígitos. Su usuario es `isis`, con rol `owner`.
- Al entrar, NO ve el dashboard normal: solo ve el panel de voz (orbe
  JARVIS, micrófono, conversación). Los demás perfiles no cambian nada.

## Solo por voz

- Todo se pide hablando (Web Speech API, es-MX, Chrome en Android).
- No hay botones para operar el restaurante en ese panel.
- Voz de Isis: Kokoro `ef_dora` (femenina, español). Se descarga solo
  cuando se toca "Descargar voz de Isis" (~88 MB, una sola vez, mejor con
  Wi-Fi) y luego queda guardada. Si no está descargada o falla, Isis habla
  con la voz del teléfono.
- Antes de cualquier cambio (abrir turno, crear mesa, agregar comanda,
  cobrar, cerrar cuenta, corte, reponer, ajustar inventario, anular,
  movimiento de caja) Isis dice lo que va a hacer y espera un
  "sí, confirma". Si dices "no" o "cancela", no hace nada. La confirmación
  vence a los 2 minutos.

## Qué sabe hacer (solo Mitote)

### v2: personalidad, memoria y los 14 módulos

- **Personalidad (simulada, no consciencia real):** Isis responde con
  tono cálido, mexicano, profesional y breve, con frases variadas. Se
  alegra cuando algo sale bien y avisa con preocupación cuando algo anda
  mal (caja corta, inventario bajo, comanda atrasada). No siente ni tiene
  conciencia: es un estilo programado, no una persona.
- **Memoria (solo del restaurante):** "Recuerda que el precio de la
  birria es 120" lo guarda; "¿Qué recuerdas del precio de la birria?" lo
  consulta; "Olvida el precio de la birria" lo borra. En cada respuesta
  con IA, Isis también recibe sus memorias guardadas y un resumen del día
  (ventas, caja). Nunca guardar aquí datos personales sensibles.
- **Los 14 módulos por voz** (lectura sin confirmación, escritura SIEMPRE
  con confirmación por voz antes de ejecutarse):
  1. Cuentas — cuentas abiertas; cambiar una cuenta de mesa.
  2. Mesas — libres/ocupadas; cambiar capacidad de una mesa.
  3. Estaciones — comandas del turno; recibir, preparar y marcar lista.
  4. Cobros — cuentas por cobrar; aplicar descuento (porcentaje o pesos).
  5. Caja — estado del turno; historial de cortes.
  6. Menú — qué hay en el menú; agregar producto, archivar, agregar
     presentación, poner promoción, cambiar precio.
  7. Inventario — existencias; movimientos; registrar insumo; reponer;
     ajustar.
  8. Personal — quién trabaja; registrar empleado con PIN; cambiar PIN
     (Isis NO puede cambiar su propio PIN por voz).
  9. Propinas — fondo del turno; repartir entre empleados activos.
  10. Autorizaciones — solicitudes pendientes; autorizar o rechazar.
  11. Auditoría — últimos movimientos (solo lectura).
  12. Reservaciones — activas; crear, confirmar y recibir (abre cuenta).
  13. Tiempos — tiempo límite de preparación; cambiarlo por comanda o
      por producto.
  14. Estadísticas — ventas del día, cuentas pagadas, ticket promedio,
      lo más vendido, preparación promedio (solo lectura).

Lecturas (sin confirmación): mesas libres/ocupadas, saldo y detalle de una
cuenta, inventario y existencias de un producto, resumen y estado de caja.

Escrituras (con confirmación): abrir turno, cerrar turno / corte de caja,
crear mesa, abrir cuenta en una mesa, agregar productos a una cuenta,
cobrar una cuenta (todo o un importe, en efectivo, tarjeta o
transferencia), cerrar cuenta pagada, reponer inventario, ajustar
inventario, anular cuenta sin consumo, registrar entrada/salida de caja.

Ejemplos de voz:
- "¿Qué mesas están libres?"
- "¿Cuánto debe la mesa 3?"
- "¿Cuánto queda de cerveza?"
- "¿Cómo va la caja?"
- "Abre el turno con 500"
- "Agrega 2 cervezas a la mesa 3"
- "Cobra la mesa 3 todo en efectivo"
- "Haz el corte con 1200 contados"
- "Recuerda que el precio de la birria es 120"
- "¿Qué comandas hay en cocina?"
- "Marca la comanda 1 como lista"
- "Aplica un descuento del 10 por ciento a la mesa 3"
- "Reserva para Ana de 4 personas hoy a las 8 de la noche"
- "¿Cómo van las ventas hoy?"

"Cobrar" significa cobrarle al cliente y cerrar su cuenta, no pagar
proveedores. Para cobrar, Isis primero pide la cuenta si la mesa sigue
abierta, como hace el mesero en la app normal.

## Cerebro

- Sin clave de FableAI, Isis entiende órdenes directas en español con un
  intérprete local (lo que se probó en local).
- Con clave, además entiende lenguaje libre: el servidor llama a Claude
  Sonnet 5.5 vía FableAI (`https://api.fableaiapi.com/v1/chat/completions`)
  y convierte la orden al mismo formato interno. La clave vive SOLO en la
  variable de entorno `FABLEAI_API_KEY` del servidor en Render. Nunca va
  en el código, nunca en el navegador, nunca en localStorage.
- `GET /api/isis-voice/status` (solo propietario) dice si el usuario Isis
  existe y si la clave de FableAI está configurada, sin mostrar la clave.

## Despliegue (cuando Santiago lo apruebe)

1. Revisar esta rama `isis-voz` y fusionarla a `main`.
2. En Render, en el servicio `el-mitote-comandas`, agregar la variable
   `FABLEAI_API_KEY` con la clave de FableAI (opcional: sin ella Isis
   funciona con órdenes directas).
3. Redesplegar. La app vieja de Isis en Render no se toca.
4. Con la sesión del propietario, crear el PIN de Isis (endpoint de
   arriba) y probarlo en el teléfono: entrar con ese PIN, hablarle a Isis
   y confirmar una lectura primero, luego una escritura pequeña.

## Pruebas hechas en local (base SQLite de prueba, no producción)

- Login propietario, creación de Isis con PIN, login de Isis con PIN.
- Lecturas: mesas, caja, saldo, inventario.
- Flujo completo con confirmación: abrir turno $500, crear mesa, abrir
  cuenta, agregar 2 cervezas (inventario 10 → 8), cobrar todo en efectivo,
  cerrar cuenta, caja $600 esperado, reponer 5 (8 → 13), corte con $600.
- Un mesero NO puede usar `/api/isis-voice/chat` (403) y su dashboard
  normal sigue funcionando (200 en `/api/tables`).
- `static/isis.js` y `static/isis.css` se sirven (200) y el `index.html`
  incluye el panel `#isis-panel`.
