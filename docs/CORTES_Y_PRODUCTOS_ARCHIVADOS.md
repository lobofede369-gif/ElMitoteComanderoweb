# El Mitote Comandas — productos eliminados y archivo por corte de caja

## Reglas

- **Eliminar producto** (administrador/propietario): retiro lógico, auditado y con motivo. Desaparece del catálogo de ventas y de configuración de tiempos; NO se borran \`order_items\`, promociones, movimientos, tickets ni datos de cortes previos. El producto puede restaurarse desde **Menú → Productos eliminados**, sin perder su ID ni inventario.
- **Ventas y comandas activas**: las listas \`/api/accounts\`, \`/api/tickets\` y \`/api/preparation/tickets\` se limitan al turno de caja abierto; sin turno, devuelven una lista vacía. Las cuentas antiguas siguen disponibles en el historial y por consulta puntual de folio, conforme a permisos.
- **Al cerrar caja** (solo administrador/propietario): el cierre exige cuentas finalizadas, movimientos resueltos y reparto de propinas configurado. En la misma transacción se guarda \`cash_cut_archives\` (detalle JSON completo + archivo PDF binario). Si falla la generación del PDF, el cierre no se confirma.
- **Historial del corte**: \`/api/cash/history\`, \`/api/cash/periods/{id}\` y \`/api/cash/periods/{id}/report.pdf\` consultan la instantánea guardada cuando existe. El PDF incluye ventas por cuenta, propinas separadas, cobros por cajero y movimientos de efectivo del corte.
- Los cortes anteriores a esta actualización no tienen una instantánea histórica guardada retroactivamente; conservan la consulta y generación de PDF previas.
- Las métricas analíticas globales siguen disponibles para el propietario por fechas. Los módulos de operación inmediata no muestran cobros de cortes cerrados.
- Los registros y los PDF se conservan en PostgreSQL; **no se borran físicamente las ventas ni las propinas**. Crear un turno nuevo limpia únicamente las vistas activas.

## Compatibilidad

Solo se añaden las tablas \`archived_products\` y \`cash_cut_archives\`; no se modifican las columnas existentes. \`Base.metadata.create_all(engine)\` crea ambas durante el inicio. Validar primero con pruebas SQLite y PostgreSQL 18 en GitHub Actions.

## Comprobaciones antes de publicar

1. Que eliminar un producto no permita volver a venderlo ni borre pedidos históricos.
2. Que el botón Restaurar recupere el producto y mantenga su versión/inventario.
3. Que cerrar caja genere JSON+PDF con el mismo ID de turno.
4. Que el PDF guardado sea idéntico en descargas posteriores, incluso si cambian datos históricos.
5. Que las vistas activas queden vacías al cerrar y al abrir turno nuevo.
6. Que las operaciones antiguas de cobro, propinas, notificaciones y tickets no sufran regresiones.
