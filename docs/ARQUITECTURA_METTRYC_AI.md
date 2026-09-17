# Mettryc AI — Arquitectura de transición

## Objetivo
Transformar el chatbot rígido actual en un agente inmobiliario conversacional capaz de atender de forma natural, consultar el inventario Mettryc, distinguir clientes de colegas inmobiliarios, coordinar oportunidades y escalar a un humano cuando corresponda.

## Principio de migración
No reemplazar de golpe el sistema actual. Separar el cerebro conversacional de la lógica de negocio y reutilizar las capacidades que ya funcionan.

## Componentes que se conservan
- Inventario y normalización de propiedades desde WASI.
- Base de conocimiento de Mettryc.
- Diccionario geográfico.
- Datos de agentes y captadores desde Google Sheets.
- Lógica de búsqueda y detalle de propiedades, inicialmente como herramientas de negocio.
- Sesiones, deduplicación y controles de concurrencia, adaptándolos posteriormente a almacenamiento persistente.
- Asignación de clientes a agentes.
- Registro y coordinación de visitas.
- Webhook/entrada actual como capa compatible durante la transición.

## Componentes que se reemplazarán progresivamente
- Clasificación basada en listas extensas de frases.
- Fallback conversacional basado en expresiones rígidas.
- Acumulación de FIX individuales para resolver variaciones de lenguaje natural.
- Flujo conversacional monolítico dentro de `main.py`.

## Nueva arquitectura lógica

```text
Canal de entrada
      |
      v
Channel Adapter
      |
      v
Conversation Service
      |
      v
Model Router
  |       |       |
  v       v       v
Luna    Terra   Astra
  |       |       |
  +-------+-------+
          |
          v
      Tool Layer
          |
   +------+-------+-------+---------+
   |              |       |         |
Inventario       CRM    Agentes   Agenda
   |              |       |         |
   +--------------+-------+---------+
                  |
                  v
          Resultado de negocio
                  |
                  v
          Channel Adapter
```

## Roles conversacionales
- `cliente`: persona que busca una propiedad para sí misma.
- `colega_inmobiliario`: agente/corredor/broker/realtor que busca inventario para un cliente.
- `empresa`: consultas generales de Mettryc.
- `humano`: conversación transferida a un miembro del equipo.
- `desconocido`: contacto aún no clasificado con certeza suficiente.

## Herramientas de negocio previstas
Las herramientas serán deterministas y controladas por el backend; el modelo decide cuándo utilizarlas.

- `buscar_propiedades`
- `buscar_propiedad_por_codigo`
- `obtener_detalle_propiedad`
- `obtener_captador`
- `buscar_agente`
- `asignar_cliente`
- `consultar_disponibilidad_visita`
- `crear_visita`
- `notificar_agente`
- `guardar_lead`
- `actualizar_conversacion`
- `consultar_conocimiento_mettryc`

## Regla de oro
El modelo nunca inventa datos inmobiliarios. Todo precio, disponibilidad, código, característica, captador, agente u horario debe venir del sistema de negocio.

## Estrategia de modelos
- Luna: atención cotidiana y alto volumen.
- Terra: razonamiento comercial/intermedio.
- Astra: casos complejos y operaciones de computadora que realmente requieran su nivel de capacidad.

El router debe permitir cambiar modelos sin modificar la lógica de negocio.

## Estado de la conversación
La memoria no dependerá únicamente del historial bruto. Se mantendrá un estado estructurado con:
- rol confirmado;
- preferencias inmobiliarias;
- propiedades mostradas/seleccionadas;
- lead;
- intención actual;
- agente asignado;
- visita;
- resumen de conversación;
- estado de transferencia a humano.

## Transición
1. Crear servicios y herramientas sin cambiar el flujo actual.
2. Implementar nuevo motor conversacional en paralelo.
3. Probar con conversaciones simuladas y casos reales anonimizados.
4. Sustituir progresivamente decisiones conversacionales rígidas.
5. Integrar la automatización de escritorio/canales como una capa independiente.
6. Retirar reglas antiguas solo después de validar equivalencia funcional.

## Seguridad operativa
- Nunca almacenar credenciales en Git.
- No modificar el bot en producción mientras se construye la nueva arquitectura.
- Mantener una ruta de rollback al sistema anterior.
- Registrar decisiones y llamadas a herramientas para poder auditar errores.
