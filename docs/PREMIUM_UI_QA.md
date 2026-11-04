# El Mitote Comandas — QA del rediseño premium

**Rama:** `design/obsidian-titanium-v1`  
**Objetivo:** cambiar exclusivamente presentación y navegación de accesos del panel inicial. No migrar datos ni cambiar endpoints del servidor.

## Alcance implementado
- Login y componentes visuales generales (Obsidian Gold + Titanium Pro).
- Cabeceras, navegación, menú Más, estados de conexión, botones, tarjetas, formularios y tablas.
- Inicio de propietario/administrador: panel con 11 accesos directos a módulos existentes.
- Mesero: listado de cuentas, cuenta libre/mesa, catálogo, cantidad, notas, variantes/modificadores y borrador.
- Cocina/barra: nueva, recibida, preparando, lista, historial y notas.
- Caja: cuentas pendientes, métodos de pago, descuentos, propinas y movimientos.
- Inventario, mesas, productos/promociones, usuarios, autorizaciones, fondo de propinas y auditoría.
- Presentación adaptable: teléfono vertical, teléfono horizontal y tablet.

## Pruebas manuales obligatorias antes de fusionar a main
1. **Login y permisos:** acceder con todos los roles; probar credenciales incorrectas, expiración de sesión y botón Salir. Verificar que no aparece contenido protegido antes del login.
2. **Mesero:** abrir cuenta libre y de mesa; añadir, modificar y quitar productos; variantes, modificadores y notas; enviar; repetir pedidos; solicitar cuenta.
3. **Cocina y barra:** confirmar recepción, preparar, marcar listo y comprobar el historial. Verificar alertas con aplicación abierta y pantalla bloqueada.
4. **Caja:** precuenta, descuento autorizado, pagos parciales/mixtos, propinas, comprobantes y cierre con cuentas pendientes.
5. **Inventario y menú:** alta/edición, stock, productos bloqueados, variantes, promociones, activación del control general y reabastecimiento.
6. **Gestión:** usuarios y permisos, mesas, cancelaciones/reaperturas, auditoría, fondo común y reparto en partes iguales.
7. **Responsive:** 320 px, 360 px, 390 px, tablet y Android horizontal. Confirmar que ningún botón queda tapado por navegación inferior.
8. **Rendimiento:** agregar/quitar/cambiar cantidades rápidamente; revisar que no se pierden borradores ni aparecen comandas duplicadas.
9. **Integridad:** confirmar que no hubo cambios a rutas API, modelo de base de datos o backend.
10. **Regresión:** verificar que el servicio worker actualiza assets y que la pantalla no regresa al login al navegar.

## Seguridad de publicación
- El rediseño permanece en esta rama. Render está configurado para usar `main` y no debe cambiarse a esta rama sin preparar un ambiente de pruebas.
- No usar base de datos real para pruebas de interacción.
- Fusionar únicamente tras revisión visual y funcional, y preparar después una nueva compilación de APK si corresponde.

## Archivos principales
- `static/theme-obsidian-titanium.css`: sistema visual base.
- `static/theme-obsidian-modules.css`: rediseño de todos los módulos y dashboard.
- `static/app.js`: solo mejora de accesos del panel inicial; no cambia API.
- `static/index.html` y `static/sw.js`: carga y caché de versiones.
- `.github/workflows/premium-ui-check.yml`: comprobaciones estáticas automáticas.
