# Correcciones de Paty — 27 de septiembre de 2026

## Punto de recuperación

Base revisada: `5d0fe7b8a70337cc748e77ebf098765e1edbe3be`.
Respaldo verificado: `backup/pre-correcciones-2026-09-26`.
El respaldo preserva código; no incluye variables de Render, inventario ni sesiones.

## Cambios

| Área | Archivos y funciones | Resultado |
| --- | --- | --- |
| WASI | `main.py`: `obtener_inventario_wasi`, `actualizar_inventario`, `consultar_detalle_propiedad_wasi` | Listado y fichas individuales separados; solo los detalles completos se almacenan en su caché, por 300 segundos. Un fallo de página no reemplaza el inventario completo. Un listado vacío confirmado sí se acepta. |
| Estado del inmueble | `normalizar_propiedad_wasi` | Lee `id_status_on_page` e `id_availability`; los vendidos, alquilados, inactivos y eliminados no se presentan como disponibles. Conserva el campo `built_area`. |
| Preguntas | `bridge.detail`, `responder_pregunta_propiedad` | Ambas rutas usan la misma fuente documental completa y conservan la opción seleccionada. Los errores de WASI/IA se distinguen de un dato ausente. Las referencias fallidas no reutilizan otra propiedad. |
| Conversación | `engine.process`, `_enforce_legacy_business_intent`, `bridge.apply_analysis` | Un comentario casual no lanza otra búsqueda por tener filtros guardados. Consultar una opción conserva el lote. Cambiar criterios permite reconsiderar inmuebles ya enviados. |
| Captador | `atender_solicitud_captador` | Corrige el nombre indefinido en la rama de captador sin teléfono. |
| Sesiones | `conversation_store.py`, `main.procesar_turno`, `service.py` | Redis opcional comparte sesiones, locks por usuario y deduplicación entre procesos. Ambos webhooks usan la misma frontera. |
| Estado operativo | `/health`, `/admin/status`, `/admin/refresh` | Muestran frescura del inventario y fallos de actualización; una falla no se anuncia como ausencia de inmuebles. |

Las reglas de estado de WASI se contrastaron con su documentación oficial:
[estados](https://api.wasi.co/docs/guide/fields/property-states.html),
[disponibilidad](https://api.wasi.co/docs/guide/fields/property-availability.html) y
[consultas de propiedades](https://api.wasi.co/docs/guide/properties.html).

## Pruebas reproducibles

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q main.py agente_virtual conversation_store.py
python -m unittest discover -s tests -v
```

El script `scripts/smoke_test_agente_virtual.py` ahora ejecuta las regresiones del
motor real. El simulador anterior no implementaba funciones que el motor ya
invocaba y fallaba antes de probar una conversación.

La suite cubre fichas completas, caché con vencimiento, respuesta de propiedad
equivocada, inactividad, inventario incompleto/vacío, cambio de tema, referencias
por posición/código, preguntas en ambos flujos, anuncio de Mercado Libre,
confirmación de rol, zona ambigua, fichas para colegas, confirmación de lead,
duplicados, sesiones compartidas, errores de almacenamiento y reintentos.

HTTP a WASI, respuestas de IA, Telegram y asignación externa se controlan en las
pruebas. Redis se simula con dos clientes independientes y Lua. Estas pruebas
validan el código; no certifican los datos ni servicios de producción.

## Activar persistencia en Render

1. Disponer de Redis compatible con conexiones privadas/TLS, persistencia de
   datos y una política de memoria apropiada para conservar sesiones. La
   durabilidad depende también de la configuración del servicio Redis.
2. Configurar `PATY_REDIS_URL` como secreto en Render. No escribir la URL con
   credenciales en el repositorio. Todas las instancias de Paty deben usar el
   mismo servicio y `PATY_REDIS_PREFIX`.
3. Desplegar el código y comprobar `/health`: `persistencia: redis` y
   `persistencia_disponible: true`.
4. Conversar con un sender de prueba, elegir una propiedad, reiniciar una
   instancia y preguntar por la misma opción. Verificar también el reenvío del
   mismo `message_id` a otra instancia: no debe volver a ejecutarse.

| Variable | Valor por defecto | Uso |
| --- | --- | --- |
| `PATY_REDIS_URL` | Vacía | Con vacía sigue usando memoria local. Con URL activa persistencia compartida. |
| `PATY_REDIS_PREFIX` | `mettryc:paty:v1` | Separar pruebas y producción. |
| `PATY_SESSION_TTL_SECONDS` | `604800` | Retención de sesión: 7 días desde el último turno. |
| `PATY_TURN_TIMEOUT_SECONDS` | `240` | Límite del turno con Redis; el lock tiene 60 segundos adicionales. |
| `PROPERTY_DETAIL_TTL_SECONDS` | `300` | Vigencia de ficha individual WASI. |

Activar Redis inicia sesiones compartidas nuevas; no migra memoria de un
proceso previo. Si Redis está configurado y falla, los webhooks responden HTTP
503 para permitir reintento, sin crear silenciosamente otra conversación.
El cliente del webhook debe respetar ese reintento. Estado y marca de duplicado
se guardan juntos; una caída entre un aviso externo y el guardado todavía puede
repetir ese aviso. No se promete ejecución única de efectos externos.

El inventario y el contador round-robin continúan siendo locales a cada proceso.
Redis comparte el estado conversacional, no toda la infraestructura del bot.

## Comprobaciones de producción pendientes

- Confirmar el SHA desplegado en Render y `AGENTE_VIRTUAL_ACTIVO`.
- Consultar un código real cuyo listado tenga menos datos que su ficha WASI.
- Comparar la respuesta con los campos reales de WASI.
- Probar preguntas por opción, cambio de tema y regreso a la propiedad.
- Activar Redis y comprobar recuperación después de reinicio.

La lectura pública de `/health` no estuvo disponible desde la herramienta de
consulta utilizada. No se accedió a credenciales ni se modificó configuración
de Render durante estas pruebas.

## Recuperación

Revertir el commit/PR de estas correcciones mediante un nuevo commit y volver a
desplegar permite recuperar el comportamiento anterior conservando el historial
Git. La rama de respaldo conserva la referencia exacta anterior. También se
puede desplegar ese SHA desde Render si su configuración lo permite. No hace
falta reescribir `main` ni usar un push forzado.

La desactivación de Redis vuelve al estado local y no recupera automáticamente
las sesiones compartidas. El retorno a la versión anterior requiere verificar
por separado las variables y el despliegue de Render.
