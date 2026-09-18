# Mettryc Realty — Agente Virtual

## Objetivo

Transformar el chatbot actual en un agente virtual capaz de conversar de forma
natural con clientes, colegas y otras personas, sin obligarlos a seguir un
cuestionario rígido.

El agente debe entender mensajes cotidianos, conservar el contexto, detectar
cambios de tema, responder preguntas que no estén contempladas por el flujo
antiguo y volver al contexto inmobiliario cuando la persona lo retome.

## Principio principal

No se construye un segundo sistema inmobiliario.

El chatbot existente en main.py sigue siendo la fuente de verdad y el motor de
negocio de Mettryc Realty. El nuevo agente se coloca por encima de ese motor y
se encarga principalmente de conversar, comprender y decidir qué capacidad
existente debe utilizar.

~~~text
WhatsApp / Facebook / otro canal
             |
             v
       AGENTE VIRTUAL
             |
             +---- conversación natural
             +---- memoria de contexto
             +---- detección de intención
             +---- respuesta humana
             |
             v
    PUENTE HACIA main.py
             |
             +---- WASI
             +---- búsqueda de propiedades
             +---- detalles
             +---- captadores
             +---- Google Sheets
             +---- agentes en turno
             +---- asignación de leads
             +---- visitas
             +---- Telegram / avisos
             |
             v
        respuesta al usuario
~~~

## Qué permanece

Todo lo que ya funciona en main.py debe seguir siendo utilizado:

- inventario y normalización de WASI;
- búsqueda y ranking de propiedades;
- consulta de detalles;
- captadores y cruce con Google Sheets;
- agentes disponibles y asignación round-robin;
- captura y confirmación de leads;
- notificaciones;
- coordinación de visitas;
- webhook y controles de duplicados;
- sesiones y locks por usuario;
- base de conocimiento y reglas comerciales.

## Qué cambia

La conversación deja de depender principalmente de:

- listas de frases;
- menús;
- preguntas obligatorias en un orden fijo;
- múltiples excepciones para interpretar respuestas cortas;
- bloques de código que deciden cada posible giro de la conversación.

El modelo pasa a interpretar el lenguaje del usuario y escoger una acción de
negocio cuando sea necesaria.

## Comportamiento esperado

### Conversación inmobiliaria

Una persona puede decir:

"Busco una casa en Mañongo para comprar, no quiero pasar de 250 mil."

El agente extrae los criterios disponibles y consulta el inventario real de
Mettryc a través de main.py.

### Cambio de tema

Mientras se habla de una propiedad, la persona puede escribir:

"Qué bello está el día hoy, ¿verdad?"

El agente debe responder como una conversación normal. No debe reiniciar la
búsqueda ni exigir una respuesta al último dato inmobiliario.

Si luego la persona dice:

"Bueno, y esa casa tiene planta eléctrica?"

el agente debe recuperar el contexto de la propiedad y continuar.

### Cliente

Cuando una persona solicita una visita o atención humana, el agente utiliza
las funciones existentes de main.py para capturar los datos, asignar el lead al
agente de turno y realizar las notificaciones correspondientes.

### Colega inmobiliario

Cuando se identifica como colega, el agente puede consultar el inventario y
utilizar las capacidades existentes para entregar información del captador
según las reglas de Mettryc.

### Información que no está disponible

El agente no debe inventar.

Si el usuario solicita un dato que no está disponible en el contexto o en el
motor de negocio, debe decirlo de forma transparente y enviar un aviso a los
administradores para que el equipo pueda confirmar o completar la información.

### Solicitud humana

Una solicitud explícita de atención humana genera un aviso administrativo y,
además, continúa utilizando el flujo existente para que la oportunidad no se
pierda.

## Archivos nuevos

~~~text
agente_virtual/
  __init__.py
  schemas.py
  router.py
  bridge.py
  engine.py
  service.py

scripts/
  smoke_test_agente_virtual.py
~~~

### Responsabilidad de cada archivo

- schemas.py: contrato de interpretación del turno.
- router.py: comunicación con OpenRouter.
- bridge.py: único puente entre el agente y main.py.
- engine.py: cerebro conversacional y orquestación.
- service.py: servicio de entrada y concurrencia reutilizando los locks del bot.
- smoke_test_agente_virtual.py: pruebas sin WASI ni canales reales.

## Regla de oro

El agente conversacional puede interpretar y reformular, pero no sustituye los
datos de negocio.

Precios, disponibilidad, códigos, características, captadores, agentes y
acciones operativas deben provenir del motor Mettryc existente.

## Estrategia de transición

1. Probar el agente sobre main.py sin tocar producción.
2. Validar búsquedas, detalles, colegas, leads, asignaciones, visitas y avisos.
3. Probar conversaciones largas con cambios de tema.
4. Conectar el agente al webhook real.
5. Comparar resultados con el bot antiguo.
6. Sustituir gradualmente el flujo conversacional rígido cuando la equivalencia
   funcional esté comprobada.

La rama main debe permanecer como ruta de respaldo hasta completar la validación.
