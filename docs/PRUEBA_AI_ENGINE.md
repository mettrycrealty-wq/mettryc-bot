# Primera prueba del Mettryc AI Engine

## Qué se agregó

La rama `ai-engine-v1` contiene un motor conversacional nuevo, separado de `main.py` y de cualquier canal de mensajería.

Archivos principales:

- `ai_engine/schemas.py`: estado de conversación, criterios inmobiliarios y resultados de herramientas.
- `ai_engine/router.py`: cliente reutilizable para OpenRouter, con modelo principal y respaldo por variables de entorno.
- `ai_engine/engine.py`: interpreta el mensaje, actualiza el estado, ejecuta herramientas y genera la respuesta.
- `scripts/smoke_test_ai_engine.py`: prueba offline; no toca WhatsApp, WASI ni producción.

## Cómo probarlo en tu PC

Desde la carpeta del proyecto:

```powershell
python scripts/smoke_test_ai_engine.py
```

La prueba debe terminar mostrando:

```text
✅ SMOKE TEST OK
```

y datos como `Rol: colleague`, `Intención: property_search`, `Zona: Mañongo` y `Presupuesto máximo: 200000`.

## Qué significa esta prueba

Todavía no se conecta al WhatsApp actual ni sustituye `main.py`.

La prueba usa un modelo simulado y una propiedad simulada para comprobar la arquitectura básica:

`mensaje natural -> interpretación estructurada -> herramienta -> estado -> respuesta natural`

## Siguiente integración

Una vez verificada esta prueba local, el siguiente cambio será crear el adaptador de inventario de Mettryc que llame a la lógica existente de propiedades/WASI en lugar de la propiedad simulada.

Después conectaremos este motor al canal actual. Solo entonces empezaremos a retirar gradualmente las reglas conversacionales rígidas del bot antiguo.
