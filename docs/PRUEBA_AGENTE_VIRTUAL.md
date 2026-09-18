# Probar el Agente Virtual desde el módulo de autoresponder

## Endpoint recomendado para pruebas

Usa:

POST /webhook-agente-virtual

Este endpoint fuerza el agente virtual y no requiere activar el modo nuevo en
el webhook de producción.

Debe enviarse el mismo encabezado de autenticación que usa el webhook actual:

x-api-key: <API_KEY>

## Formato

```json
{
  "sender": "whatsapp:+584120000077",
  "message": "Hola, busco una casa en Mañongo para comprar"
}
```

La respuesta mantiene el formato esperado por el autoresponder:

```json
{
  "replies": [
    {
      "message": "..."
    }
  ]
}
```

## Prueba sugerida

Envía varios mensajes usando el mismo sender para comprobar memoria:

1. "Hola, busco una casa en Mañongo"
2. "En venta, hasta 250 mil"
3. "¿Tienes alguna con planta eléctrica?"
4. "Qué calor hace hoy 😄"
5. "Bueno, ¿y la primera de las que me mostraste?"
6. "Quiero hablar con un asesor"

La conversación debe conservar el contexto entre mensajes.

## Activar el agente sobre /webhook

Cuando el comportamiento del endpoint de prueba esté validado, configura:

```
AGENTE_VIRTUAL_ACTIVO=true
```

Con esa variable el /webhook normal utiliza primero el Agente Virtual. Si el
Agente Virtual falla, main.py conserva el flujo legacy como fallback.

## Modelo

El agente utiliza:

```
OPENROUTER_API_KEY=<tu clave>
OPENROUTER_MAIN_MODEL=google/gemini-2.5-flash-lite
```

También acepta OPENROUTER_MODEL y MODELO_AGENTE_PRINCIPAL como alternativas.

## Importante

No se modificó la fuente de verdad inmobiliaria. WASI, Google Sheets,
captadores, agentes en turno, asignación de leads, visitas y notificaciones
siguen siendo ejecutados por main.py.

La ruta /webhook normal permanece compatible con el chatbot anterior mientras
se valida el agente.
