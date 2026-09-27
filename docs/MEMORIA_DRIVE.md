# Activar memoria de Paty en Google Drive

Preparado el 27 de septiembre de 2026. La función queda desactivada hasta configurar Render.
Respaldo de código previo: `backup/pre-memoria-drive-2026-09-27`, commit `d7be7c71e1ecc98b98b0d1d939323148dac3b65b`.

## 1. Crear el servicio de memoria

1. Abre https://script.google.com y crea un **proyecto nuevo** llamado `Paty - memoria Drive`.
2. Copia todo `apps_script/paty_memory/Code.gs` del repositorio en el archivo `Código.gs` y guarda. No uses el proyecto del inventario ni el de aprendizaje.
3. Selecciona `instalarMemoriaPaty` y pulsa **Ejecutar**. Autoriza el acceso a Drive con la cuenta propietaria.
4. En **Configuración del proyecto > Propiedades del script** aparecerán `PATY_FOLDER_ID` y `PATY_TOKEN`. Conserva la carpeta privada y copia el token directamente a Render; no lo publiques ni lo envíes por chat.
5. **Implementar > Nueva implementación > Aplicación web**: ejecutar como **Yo**, acceso **Cualquier persona**. El endpoint comprueba el token en cada solicitud; la carpeta no se comparte. Si tu organización impide esta opción, hay que resolver esa restricción antes de activar la memoria.
6. Copia la URL que termina en `/exec`. No uses `/dev`. Para actualizar el código después, crea una nueva versión de la implementación existente.

## 2. Configurar Render

En el servicio `mettryc-bot`, abre **Environment**, agrega y guarda:

| Variable | Valor |
|---|---|
| `PATY_MEMORY_BACKEND` | `drive` |
| `PATY_DRIVE_URL` | URL `/exec` de la aplicación web |
| `PATY_DRIVE_TOKEN` | Valor de `PATY_TOKEN` |
| `PATY_SESSION_TTL_SECONDS` | `604800` (7 días sin actividad) |

No necesitas `PATY_REDIS_URL`. Comienza con una instancia y un proceso; los bloqueos también protegen contra ejecuciones superpuestas, pero Drive está pensado aquí para tráfico bajo. Espera a que Render complete el despliegue.

## 3. Verificar antes de darlo por activo

- `/health` debe mostrar `persistencia: "google_drive"` y `persistencia_disponible: true`. El estado global también depende del inventario WASI.
- Desde una conversación de prueba, solicita propiedades y selecciona la segunda. Reinicia Render y pregunta un detalle de esa propiedad: debe conservar selección, listado y filtros.
- Reenvía el mismo identificador de mensaje: no debe procesarse dos veces dentro de la ventana de deduplicación.
- Comprueba que aparezca un archivo `paty-<hash>.json` en la carpeta privada.
- Una URL o token incorrectos debe producir indisponibilidad/503, sin iniciar una conversación vacía.

No se migran automáticamente las sesiones que ya estaban sólo en RAM o en Redis.

## Funcionamiento y límites

`conversation_store_from_environment` selecciona Drive, Redis o memoria. `DriveConversationStore.run` adquiere una concesión temporal, recupera el estado, ejecuta el turno y guarda estado y deduplicación juntos. Apps Script serializa las operaciones breves con ScriptLock; el identificador de concesión impide que un proceso atrasado sobrescriba otro. Los reintentos de transporte reutilizan el identificador y no repiten el procesamiento dentro de esa ejecución.

Se conservan filtros, historial limitado por el bot, último lote, propiedad activa y datos de contacto presentes en la sesión. Los nombres de archivo usan SHA-256, pero el contenido contiene datos personales: limita el acceso a la cuenta y carpeta. El TTL impide restaurar sesiones vencidas; **no elimina físicamente los archivos antiguos**. Para borrar datos, detén el bot y elimina los archivos correspondientes; no los edites durante el servicio. Las copias y la papelera de Drive tienen retención independiente.

Cada turno hace solicitudes adicionales a Google. Latencia, cuotas o errores de Drive bloquean el turno con 503. El estado se guarda antes de devolver la respuesta, pero no existe garantía de entrega exactamente una vez al canal: si se pierde la respuesta después del guardado, un reintento puede ser deduplicado. Los efectos externos del callback (por ejemplo notificar un lead) tampoco son transaccionales con Drive. Mantén observación durante la prueba real y usa Redis si el volumen crece.

## Recuperación

Para retirar esta integración, restablece `PATY_MEMORY_BACKEND` al backend anterior (`memory` si no tenías Redis, `redis` si estaba configurado) y redespliega. Esto deja de usar el historial de Drive; no lo migra. Para revertir también el código, despliega el commit del respaldo. No elimines la carpeta mientras decides recuperar conversaciones.

## Pruebas y referencias

`python -m unittest discover -s tests -v` y `node --test tests/drive_script.test.cjs` prueban cliente, recuperación, errores, deduplicación y concesiones sobre servicios simulados. La aceptación real requiere ejecutar la sección 3 con una cuenta Google y Render configurados.

Documentación oficial: https://developers.google.com/apps-script/guides/web y https://developers.google.com/apps-script/guides/content (la respuesta JSON usa redirecciones); https://developers.google.com/apps-script/reference/lock/lock-service y https://developers.google.com/apps-script/guides/services/quotas.
