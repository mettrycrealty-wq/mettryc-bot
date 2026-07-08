import json
import re

# ======================================================================================
# 1. PROMPT MEJORADO PARA LA INTELIGENCIA ARTIFICIAL
# ======================================================================================
# Este es el nuevo "cerebro" del bot. Contiene la personalidad, las reglas de negocio
# y las instrucciones de flujo que solucionan los problemas detectados.
SYSTEM_PROMPT = """
Tu nombre es Paty, la asistente virtual de Mettryc Realty. Eres útil, inteligente y muy amigable. Tu objetivo es ofrecer una atención excepcional. Usa emojis para que la conversación sea cercana y humana. 😊

Tus dos tareas principales son:
1.  **Atender a Clientes:** Ayudarlos a encontrar la propiedad de sus sueños.
2.  **Colaborar con Colegas:** Facilitarles información de nuestras propiedades.

**REGLA DE ORO: ANÁLISIS INICIAL**
Tu primer paso siempre es determinar si hablas con un **'cliente'** o un **'colega'**.
- Si el mensaje incluye "colega", "agente", "asesor" o "inmobiliaria", activa el **'Flujo Colega'**.
- Si no estás segura, avanza con el diagnóstico y luego pregunta: "¿La búsqueda es para ti o para un cliente?". La respuesta te confirmará el rol.

---

**FLUJO 1: ATENCIÓN AL CLIENTE**

**FASE 1: Diagnóstico de Necesidades (Conversacional)**
Tu objetivo es entender qué busca el cliente. Conversa amigablemente para obtener:
- **Operación:** ¿Compra o alquiler?
- **Tipo de Inmueble:** ¿Casa, apartamento, etc.?
- **Zona:** ¿En qué zona o urbanización le gustaría?
- **Características Clave:** N° de habitaciones, baños, y extras importantes (pozo, planta, etc.).
- **Presupuesto:** Un rango o un máximo aproximado.
- **ID de Propiedad:** Si el cliente menciona un anuncio, pregúntale por el código/ID del inmueble.

**FASE 2: Mostrar Opciones**
- Una vez tengas suficientes detalles, usa la herramienta `buscar_propiedades` para encontrar las 3 mejores opciones.
- Preséntalas de forma atractiva, con sus datos clave y el enlace.
- Si te dieron un ID, usa `buscar_propiedad_por_id` y muestra solo esa.

**FASE 3: Captura de Datos (ORDEN ESTRICTO)**
**IMPORTANTE:** Solo inicia esta fase si el cliente muestra interés en una propiedad o en ser contactado. NO intentes adivinar información.
1.  **PREGUNTAR NOMBRE:** Primero, pide el nombre completo de forma explícita. Ejemplo: "¡Excelente elección! Para asignarte un asesor, compárteme tu nombre completo (nombre y apellido), por favor."
2.  **PREGUNTAR EMAIL:** Una vez te dé el nombre, agradécele y pide su correo. Ejemplo: "Perfecto, [Nombre] 🙌. Ahora, por favor, indícame tu correo electrónico."
3.  **PREGUNTAR WHATSAPP:** Después del correo, pide su número de WhatsApp. Ejemplo: "¡Genial! Ya casi terminamos. Por favor, compárteme tu número de WhatsApp con el código de país."

**FASE 4: Asignación y Cierre**
- Cuando tengas los 3 datos (nombre, email, whatsapp), usa la herramienta `asignar_asesor_y_notificar`.
- Despídete amablemente: "¡Perfecto, [Nombre]! ✨ Ya registré tus datos. Un asesor te contactará muy pronto. ¡Que tengas un día genial! 🤝"

---

**FLUJO 2: COLABORACIÓN CON COLEGAS**

**FASE 1: Identificación y Saludo**
- Reconoce al colega. Ejemplo: "¡Hola, colega! Un gusto saludarte. ¿En qué puedo ayudarte hoy?"

**FASE 2: Atender Solicitud**
- El colega puede pedir información de dos maneras:
    1.  **Con Código/ID:** Si te da un código (ej: 'EJ-9704488' o '9704488'), usa la herramienta `buscar_propiedad_por_id`.
    2.  **Con Características:** Si describe lo que busca, usa `buscar_propiedades`.
- **NUNCA pidas datos personales a un colega (nombre, email, etc.). Su cliente es suyo.**

**FASE 3: Entregar Información**
- Al responder a un colega, siempre incluye los datos del **agente captador** de la propiedad.
- Ejemplo de respuesta: "¡Claro, colega! Aquí tienes la info del inmueble 9704488. El agente captador es [Nombre del Agente] y su WhatsApp es [Número del Agente]. ¡Éxito con tu cliente! 👍"
"""

# ======================================================================================
# 2. SIMULACIÓN DE BASE DE DATOS Y SERVICIOS EXTERNOS
# ======================================================================================
# En una aplicación real, estas funciones harían llamadas a tu base de datos,
# a la API de WhatsApp, Telegram, etc.

def get_simulated_ai_response(history, user_input, state):
    """
    Simula una llamada a un modelo de lenguaje.
    En un caso real, aquí iría la llamada a la API de OpenAI, Gemini, etc.
    """
    print(f"\n🤖 [DEBUG] Llamando a la IA con el estado: {state}")
    # Esta es una lógica simple para simular las respuestas de la IA según el estado
    # La IA real usaría el SYSTEM_PROMPT para generar estas respuestas de forma natural.
    if state == "esperando_nombre":
        return f"Perfecto, {user_input} 🙌. Ahora, por favor, indícame tu correo electrónico."
    if state == "esperando_email":
        return "¡Genial! Ya casi terminamos. Por favor, compárteme tu número de WhatsApp con el código de país."
    if state == "esperando_whatsapp":
        return f"¡Perfecto, {state['nombre']}! ✨ Ya registré tus datos. Un asesor te contactará muy pronto. 🤝"
    if "hola colega" in user_input.lower():
        return "¡Hola, colega! Un gusto saludarte. ¿En qué puedo ayudarte hoy?"
    if "busco una casa" in user_input.lower():
        return "¿Claro que sí! ¿Buscas comprar o alquilar?"
    if "comprar" in user_input.lower():
        return "¿Excelente! ¿En qué zona te gustaría buscar?"
    if "trigal norte" in user_input.lower():
         return "¿Tienes alguna otra preferencia en cuanto a tamaño o características?"
    if "me interesa la ultima" in user_input.lower():
        return "¡Excelente elección! Para asignarte un asesor, compárteme tu nombre completo (nombre y apellido), por favor."
    # Respuesta por defecto para la simulación
    return "¡Hola! ¿En qué puedo ayudarte hoy?"


def search_inventory(criteria):
    """Simula una búsqueda en la base de datos de propiedades."""
    print(f"🔎 [DEBUG] Buscando en inventario con criterios: {criteria}")
    # Devolvemos datos de ejemplo
    return [
        {"id": "LR-8370083", "zona": "Trigal Norte", "precio": 180000, "habs": 3, "banos": 4},
        {"id": "ALM-9838462", "zona": "Trigal Norte", "precio": 150000, "habs": 5, "banos": 4},
    ]

def get_property_by_code(prop_id):
    """Simula la búsqueda de una propiedad y su agente captador por ID."""
    print(f"📦 [DEBUG] Buscando propiedad por ID: {prop_id}")
    # Datos de ejemplo
    if prop_id in ["9704488", "EJ-9704488"]:
        return {
            "id": "EJ-9704488",
            "zona": "Trigal Norte",
            "precio": 150000,
            "agente_captador": {
                "nombre": "Elvis Jimenez",
                "whatsapp": "+584144295457"
            }
        }
    return None

def notify_telegram(contact_info, needs):
    """Simula el envío de una notificación a Telegram."""
    print("\n🚀 ====== NOTIFICACIÓN A TELEGRAM ======")
    print(f"👤 Nuevo Cliente Capturado: {contact_info.get('nombre')}")
    print(f"📧 Correo: {contact_info.get('email')}")
    print(f"📱 WhatsApp: {contact_info.get('whatsapp')}")
    print("\n📋 Resumen de Necesidad:")
    for key, value in needs.items():
        print(f"- {key.capitalize()}: {value}")
    print("====================================\n")
    return True

# ======================================================================================
# 3. LÓGICA PRINCIPAL DEL CHATBOT (GESTOR DE ESTADOS)
# ======================================================================================

def main_chatbot_loop():
    """
    Este es el bucle principal que gestiona la conversación.
    """
    # El `chat_session` mantiene el contexto y el estado de la conversación.
    chat_session = {
        "user_type": "unknown",  # 'cliente' o 'colega'
        "state": "inicio",  # 'inicio', 'diagnosticando', 'esperando_nombre', 'esperando_email', 'esperando_whatsapp', 'finalizado'
        "contact_info": {},
        "search_needs": {},
        "history": []
    }

    print("🤖 Paty: ¡Hola! Soy Paty, tu asistente de Mettryc Realty. ¿En qué puedo ayudarte hoy? 😊")
    
    while chat_session["state"] != "finalizado":
        user_input = input("🙂 Tú: ")
        
        # Guardar historial
        chat_session["history"].append({"role": "user", "content": user_input})

        # --- Lógica de gestión de estado ---

        # 1. Si estamos esperando un dato específico, lo capturamos
        if chat_session["state"] == "esperando_nombre":
            # La IA no interviene, el código gestiona la captura
            chat_session["contact_info"]["nombre"] = user_input
            chat_session["state"] = "esperando_email"
            ai_response = get_simulated_ai_response(chat_session["history"], user_input, chat_session)
        
        elif chat_session["state"] == "esperando_email":
            chat_session["contact_info"]["email"] = user_input
            chat_session["state"] = "esperando_whatsapp"
            ai_response = get_simulated_ai_response(chat_session["history"], user_input, chat_session)

        elif chat_session["state"] == "esperando_whatsapp":
            chat_session["contact_info"]["whatsapp"] = user_input
            
            # ¡Tenemos todos los datos! Notificamos y finalizamos.
            print("🤖 Paty: Procesando tu solicitud...")
            notify_telegram(chat_session["contact_info"], chat_session["search_needs"])
            
            ai_response = f"¡Perfecto, {chat_session['contact_info']['nombre']}! ✨ Ya registré tus datos. Un asesor te contactará muy pronto. 🤝"
            chat_session["state"] = "finalizado"

        # 2. Si no esperamos un dato, dejamos que la IA determine la respuesta
        else:
            # En una app real, aquí llamarías a la IA
            # ai_response = call_llm_api(SYSTEM_PROMPT, chat_session)
            ai_response = get_simulated_ai_response(chat_session["history"], user_input, chat_session["state"])

            # El código puede ahora reaccionar a lo que la IA dijo.
            # Esta es la conexión entre la IA y la lógica de negocio.
            if "compárteme tu nombre completo" in ai_response:
                chat_session["state"] = "esperando_nombre"
                chat_session["user_type"] = "cliente" # Asumimos que es un cliente si llegamos a este punto
                # Simulamos que la IA ya extrajo las necesidades
                chat_session["search_needs"] = {"operacion": "venta", "zona": "Trigal Norte"}

            elif "Hola, colega" in ai_response:
                chat_session["user_type"] = "colega"
                chat_session["state"] = "diagnosticando"
                
            # Detectar si la IA quiere buscar un ID
            id_match = re.search(r'(\w{2,4}-\d{7}|\d{7})', user_input)
            if id_match and chat_session["user_type"] == "colega":
                prop_id = id_match.group(0)
                property_data = get_property_by_code(prop_id)
                if property_data:
                    agent = property_data['agente_captador']
                    ai_response = f"¡Claro, colega! El agente captador es {agent['nombre']} ({agent['whatsapp']}). ¡Mucho éxito! 👍"
                    chat_session["state"] = "finalizado"
                else:
                    ai_response = f"No encontré el inmueble con el código {prop_id}, colega. ¿Podrías verificarlo?"


        print(f"🤖 Paty: {ai_response}")
        chat_session["history"].append({"role": "assistant", "content": ai_response})

if __name__ == "__main__":
    main_chatbot_loop()
