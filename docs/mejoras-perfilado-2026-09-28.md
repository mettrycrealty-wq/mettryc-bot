# Perfilado y continuidad conversacional — 28 de septiembre de 2026

## Recuperación

Antes de los cambios se creó y verificó la rama `backup/pre-perfilado-2026-09-28-1437`, sobre `db7088af3202ab3962205ee9e465c747c20883d6` (árbol `a3b34efacdf75e0ccb166d18479f51667004d720`). Permite recuperar el código anterior. No es una copia de los datos de Drive, Google Sheets ni de variables de Render.

Para volver a ese código, desplegar ese commit en Render o revertir el PR mediante un nuevo commit. Evitar reescribir el historial de main.

## Cambios

- `search_profile.py`: reglas comunes de perfilado, inicial separada, reparación de preguntas de rol y respuestas honestas a marcadores de fotos, agenda y enlaces que fallan.
- `main.py`, `agente_virtual/bridge.py`, `agente_virtual/engine.py`: antes de una búsqueda general se requieren operación, tipo y ciudad. Zona y presupuesto se solicitan y pueden quedar abiertos por decisión explícita. Se ofrece una oportunidad de indicar habitaciones, baños, estacionamiento y características. Las consultas por código o anuncio mantienen su acceso directo.
- Confirmación de visita inequívoca continúa a datos de contacto. Las respuestas transaccionales no se reescriben con el modelo. Se conservan nombre completo, WhatsApp y correo obligatorios y confirmación posterior.
- La inicial no sustituye el precio total. Se conserva la necesidad de financiamiento y se aclara que las condiciones requieren confirmación del propietario.
- Se reconoce la presentación explícita de un asesor con nombre y empresa.
- Las superficies numéricas con punto decimal de WASI conservan ese punto al interpretarse: `418.11` no se convierte en `41811`. Fichas con hasta dos decimales, ausencia de dormitorios/baños/garajes como N/D y cero explícito conservado.
- `agente_virtual/learning.py`: guarda características, preferencias abiertas y campos pendientes dentro del JSON `filters`; no requiere columnas nuevas ni cambiar Apps Script. `learning_analyzer.py` cuenta perfilado y listados generales con perfil completo/incompleto cuando contienen la nueva instrumentación. Datos históricos sin ella no se reinterpretan.

## Verificación

Pruebas automatizadas con WASI/LLM/Telegram controlados: perfilado incompleto, preferencias opcionales, reanudación tras confirmar rol, acceso por código, separación inicial/total, colega explícito, visita sin reescritura, fotos y enlaces, áreas decimales, registro y análisis del perfil. Se mantiene la suite previa de inventario, entrega, persistencia y leads.

Después de desplegar:

1. En una conversación nueva, enviar «Busco una casa para comprar». Confirmar rol si se solicita: deben pedirse ubicación y presupuesto antes de listar.
2. Indicar ciudad, zona o cualquier zona, presupuesto y preferencias. Revisar que se utilicen en la selección.
3. Consultar un código directamente: debe atenderse sin exigir perfil general.
4. Solicitar visita y proporcionar nombre completo, WhatsApp y correo; confirmar y comprobar la notificación real en Telegram.
5. Revisar nuevos eventos `conversation_turn` en Sheets: estado `perfilando_busqueda` y JSON `filters` con `perfil_faltante` y características. Verificar después el embudo de captura, confirmación y entrega.

## Límites

Los datos defectuosos del inventario requieren corrección en la fuente. El código no inventa superficies o condiciones de financiamiento. La clasificación libre sigue dependiendo del modelo. Estas mejoras incorporan reglas y medición; no equivalen a reentrenamiento automático general. El optimizador existente sigue limitado a variantes predefinidas de ofrecimiento de asesor.
