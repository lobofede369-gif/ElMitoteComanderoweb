# El Mitote Comandas — salida a producción

## Cambios de esta rama

- El endpoint `/api/health` verifica una conexión real a la base de datos y responde HTTP 503 si PostgreSQL no está disponible.
- Se declara `/api/health` como health check de Render en `render.yaml`.
- Se impide abrir dos turnos de caja simultáneamente mediante un índice único parcial en SQLite y PostgreSQL; el endpoint devuelve HTTP 409 ante una carrera.
- Los cobros adquieren los bloqueos en un orden consistente: primero el turno de caja y después la cuenta.
- Los movimientos de efectivo aceptan una clave idempotente. La PWA conserva la misma clave al reintentar y el servidor rechaza reutilizarla con datos distintos.
- La migración del outbox de notificaciones es aditiva y ya no reinicia el estado de entrega en cada arranque.
- Las notificaciones distinguen la aceptación del proveedor, la recepción del mesero y la entrega al cliente.

## Límites que requieren intervención del propietario

La configuración observada en Render usa un servicio web Free y PostgreSQL Free. La base de datos informa vencimiento el **3 de diciembre de 2026**. El plan Free no es una base operativa adecuada para caja en vivo ni ofrece las garantías de continuidad y recuperación necesarias.

Antes de operar como sistema principal:

1. Aprobar y aplicar un plan de pago apropiado para el servicio web y PostgreSQL.
2. Confirmar backups administrados y probar una restauración en un entorno separado.
3. Configurar en el servicio Render real el health check `/api/health` si el servicio no está administrado mediante sincronización de Blueprint.
4. Desplegar primero en staging y verificar que la migración aditiva termina sin errores.
5. Ejecutar una prueba física de notificaciones con dos teléfonos Android: aplicación abierta, segundo plano, pantalla bloqueada, ahorro de batería y reasignación de mesero.
6. Ejecutar un turno piloto completo antes de depender exclusivamente del POS.

No se debe cambiar el plan de Render ni generar cargos sin autorización explícita del propietario. Esta rama no modifica la base de datos de producción ni despliega cambios a Render.
