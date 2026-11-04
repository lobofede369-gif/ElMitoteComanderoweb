# El Mitote Comandas — ampliación 1, 2 y 4

Estado: rama de revisión. No desplegar en producción sin validar con PostgreSQL y revisar privacidad.

## 1. Preparación mixta

- Cada lote \`Batch\` guarda su propio límite general en \`batch_prep\`.
- Cada producto de un envío conserva el \`prep_minutes\` efectivo en \`order_item_meta\`.
- Configuraciones de administración en \`/api/preparation/config\` y \`/api/preparation/products/{id}\`.
- Cocina y barra leen \`/api/preparation/tickets\` y pueden marcar cada producto listo.
- Cuando todos los productos activos de una estación están listos, se usa el flujo existente de \`ticket_status\`, incluyendo el aviso al mesero.
- Si se marca toda la estación lista manualmente, los productos pendientes se consideran concluidos a la hora del ticket.
- La tarea periódica registra alertas únicas por producto y envío mediante \`prep_alerts\` para evitar spam al reiniciar.
- Nuevos envíos no reinician los anteriores; las configuraciones nuevas no afectan las instantáneas ya guardadas.

## 2. Reservaciones

- Administración puede crear, confirmar, cancelar, marcar ausencia y registrar llegada.
- La llegada requiere mesa libre con capacidad suficiente, mesero activo y turno de caja abierto.
- La operación atómica crea una cuenta y ocupación de mesa, vinculadas a la reservación.
- El reintento devuelve la misma cuenta (idempotencia). La cuenta pagada pasa la reservación a \`served\`.
- El endpoint **servidor a servidor** \`POST /api/reservations/ingest\` acepta:
  \`\`\`json
  {
    "external_id": "web-reserva-123",
    "name": "Cliente de prueba",
    "phone": "",
    "party_size": 2,
    "scheduled_at": "2026-11-06T20:00:00-06:00",
    "note": ""
  }
  \`\`\`
  Encabezado: \`Authorization: Bearer <RESERVATIONS_INGEST_TOKEN>\`. Definir un secreto aleatorio largo SOLO en variables del servidor. **Nunca** incluirlo en JavaScript de la web pública.
- La integración de la página web pública todavía requiere que SU backend envíe las reservaciones a este endpoint. Si la página funciona solo en el navegador, crear un servicio intermediario autenticado. No se ha modificado el sitio web de clientes.

## 4. Estadísticas del propietario

- \`GET /api/owner/analytics?start=AAAA-MM-DD&end=AAAA-MM-DD\` (solo propietario; máximo 94 días).
- Ventas, cobros por método y ticket promedio se basan en \`Payment.cents\`; no incluyen \`PaymentTip\`.
- Productos por fecha de envío y cantidades netas de cancelaciones (indicador operativo, no equivalencia exacta con cobros).
- Rotación de mesas desde eventos históricos \`Audit.table.assign\`; tiempos promedio por estación desde tickets listos.
- No se afirma rentabilidad hasta que existan costos reales por producto.

## Despliegue y pruebas

1. Ejecutar el CI y la prueba \`python -m scripts.test_new_features\`.
2. Probar migración **en copia PostgreSQL de staging**, no directamente contra producción; las nuevas tablas se crean por \`Base.metadata.create_all\`, sin cambios a tablas previas.
3. Configurar \`RESERVATIONS_INGEST_TOKEN\` en Render si se conectará la web.
4. Validar rol propietario, administración, mesero, cocina y barra en dispositivos reales.
5. Verificar que los cálculos y avisos funcionen con la app cerrada; no duplicar push/FCM.
6. Revisar la cobertura real de los registros de auditoría de mesas y la precisión de diferencias horarias.

La rama no se fusiona automáticamente. La publicación de Render queda pendiente de revisión.
