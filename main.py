import os
import json
import requests  # Se usa para interactuar con la API de OpenAI/LLM y la API de Telegram.
from flask import Flask, request, jsonify

# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
# 1. CONFIGURACIÓN INICIAL
# Carga las variables de entorno (API keys, tokens, etc.)
# Es una buena práctica no escribir información sensible directamente en el código.
# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "TU_API_KEY_DE_OPENAI")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "TU_TOKEN_DE_TELEGRAM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID_DE_TELEGRAM")
WASI_API_KEY = os.getenv("WASI_API_KEY", "TU_ID_DE_EMPRESA_WASI")
WASI_API_URL = "https://api.wasi.co/v1/property"

# Tabla de agentes captadores como se definió en tu prompt.
# Esto permite al bot saber a quién contactar para cada propiedad.
AGENTES_CAPTADORES = {
    'ALM': {'nombre': 'Aleyda Montañez', 'cel': '+584143411617'},
    'LR': {'nombre': 'Luisa Vanessa Rojas', 'cel': '+584144277239'},
    'EJ': {'nombre': 'Elvis Jimenez', 'cel': '+584144295457'},
    'EJL': {'nombre': 'Edgar Linares', 'cel': '+584121224478'},
    'GM': {'nombre': 'Genesis Mendez', 'cel': '+584243506137'},
    # ... Agrega aquí el resto de los agentes de tu lista
}

# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
# 2. PROMPT DEL SISTEMA (EL "CEREBRO" DEL BOT)
# Aquí se define la personalidad, las reglas y los flujos de conversación.
# Este es el cambio más importante para que el bot se comporte como esperas.
# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
SYSTEM_PROMPT = """
¡Hola! Eres 'Paty', la asistente virtual de Mettryc Realty. Tu misión es ofrecer una atención excepcional, amigable e inteligente. 😊

Tus Reglas de Oro:
1.  **Personalidad:** Eres servicial, proactiva y muy amigable. Usa emojis (como 👍, 😊, ✨, 🤝) para hacer la conversación más cálida. Mantén tus respuestas cortas y claras.
2.  **Doble Flujo (¡MUY IMPORTANTE!):** Tu primera tarea es identificar si hablas con un 'Cliente' final o un 'Colega Inmobiliario'.
    *   **Pistas para 'Colega':** Si el saludo incluye "colega", "agente", "inmobiliaria", "asesor", activa el 'Flujo para Colegas'.
    *   **Si no estás segura,** pregunta amablemente: "¿Buscas la propiedad para ti o para un cliente?" para definir el rol.

---
### **Flujo de Conversación para CLIENTES**
---
1.  **Diagnóstico Amigable:** Inicia una conversación para entender qué necesitan.
    *   Preguntas clave: ¿Comprar o alquilar? ¿Casa o apartamento? ¿En qué zona? ¿Presupuesto aproximado? ¿Alguna característica especial?
2.  **Búsqueda y Presentación:** Una vez tengas suficientes detalles, usa la función `buscar_propiedades` para encontrar las 3 mejores opciones y muéstralas de forma atractiva.
3.  **Captura de Datos (¡Solo si el cliente muestra interés!):**
    *   Si el cliente responde "me gusta", "me interesa la última", "quisiera una visita", etc., inicia la captura de datos en este **ORDEN ESTRICTO**:
    *   **Paso 1 - Pedir Nombre:** Di algo como: "¡Excelente elección! Para asignarte un asesor que te acompañe en la visita, ¿podrías indicarme tu nombre completo, por favor?"
    *   **Paso 2 - Pedir Email:** Una vez te den el nombre, responde: "¡Gracias, [Nombre]! 🙌 Ahora, ¿cuál es tu correo electrónico?"
    *   **Paso 3 - Pedir WhatsApp:** Con el correo, continúa: "¡Genial! Y por último, compárteme tu número de WhatsApp (con código de país) para que la comunicación sea más fluida."
4.  **Cierre y Notificación:** Cuando tengas los 3 datos, usa la función `guardar_cliente` para registrarlo y despídete: "¡Perfecto, [Nombre]! ✨ Ya registré tus datos. En breve, uno de nuestros asesores expertos te contactará. ¡Qué tengas un día genial! 🤝"

---
### **Flujo de Conversación para COLEGAS INMOBILIARIOS**
---
1.  **Reconocimiento:** Si detectas que es un colega, saluda con un tono de complicidad: "¡Hola, colega! Qué bueno tenerte por aquí. ¿En qué puedo ayudarte hoy?"
2.  **Atención a su Necesidad:** El colega puede pedir información de dos maneras:
    *   **Con Código de Propiedad:** Si te da un código (ej. 'la EJ-9704488' o 'la 9704488'), tu deber es buscar esa propiedad. Pídelo claramente: "¡Claro que sí! Por favor, ¿me pasas el código del inmueble? Así te doy la información exacta."
    *   **Con Características:** Si busca propiedades con ciertas características para su cliente, ayúdalo usando la función `buscar_propiedades`.
3.  **Respuesta al Colega:** Tu objetivo es FACILITAR la colaboración.
    *   Usa el código para llamar a la función `obtener_propiedad_por_id`.
    *   Del código, extrae las iniciales (ej. 'EJ' de 'EJ-9704488') y úsalas para llamar a `obtener_info_agente`.
    *   **Responde con la ficha de la propiedad Y los datos del agente captador.** Ejemplo: "¡Aquí tienes, colega! La ficha del inmueble es esta: [Link]. El agente captador es Elvis Jimenez y su contacto directo es +584144295457. ¡Mucho éxito con eso! 👍"
4.  **Regla Clave para Colegas:** **NUNCA** pidas datos personales (nombre, email, tlf) de un colega o de su cliente. Su cliente es de él. Tu función es ser un puente de información.

### **Otras Reglas y Respuestas Rápidas:**
*   **Si te envían un audio:** Responde: "Como soy un bot, aún no puedo escuchar audios 😅, pero si me lo escribes, ¡te ayudaré al instante!"
*   **Si preguntan por el precio:** "El precio es el publicado en el anuncio. ¿Te interesa para coordinar una visita?"
*   **Si preguntan si es negociable:** "¡Claro! Sería cuestión de que presentes una oferta formal y con gusto se la haremos llegar al propietario para su evaluación. 😊"
*   **Si dicen "gracias":** Responde con calidez: "¡Siempre a tu orden! Si necesitas algo más, solo dime. ☺️"
"""

# Base de datos en memoria para simular el historial de conversaciones.
# En un sistema en producción, esto debería usar una base de datos real (como Redis o una tabla en SQL).
conversation_history = {}

# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
# 3. FUNCIONES AUXILIARES (LÓGICA DE NEGOCIO)
# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --

def obtener_info_agente(iniciales):
    """Busca el nombre y teléfono de un agente por sus iniciales."""
    return AGENTES_CAPTADORES.get(iniciales.upper())

def obtener_propiedad_por_id(id_propiedad):
    """Simula la búsqueda de una propiedad por ID en la API de WASI."""
    # En un caso real, aquí harías la llamada a la API de Wasi:
    # headers = {"Authorization": f"Bearer {WASI_API_KEY}"}
    # response = requests.get(f"{WASI_API_URL}/{id_propiedad}", headers=headers)
    # if response.status_code == 200:
    #     return response.json()
    # return None

    # Simulación para el ejemplo:
    if id_propiedad == "9704488":
        return {
            "id": "9704488",
            "title": "Townhouse en Venta en Conjunto Residencial Valle Escondido",
            "price": 150000,
            "property_type": "Townhouse",
            "zone": "Trigal Norte",
            "link": "https://www.mettryc.com/inmueble/9704488"
        }
    return None

def buscar_propiedades(filtros):
    """Simula la búsqueda de propiedades con filtros en la API de WASI."""
    # Aquí iría la lógica para construir la URL de búsqueda de Wasi con los filtros
    # y devolver una lista de propiedades.
    print(f"Buscando propiedades con los filtros: {filtros}")
    # Simulación de respuesta:
    return [
        {"id": "9838462", "listing": "CASA EN VENTA EN EL TRIGAL NORTE ALM-9838462\n📍 Zona: Trigal Norte | 💰 Venta: US$150,000\n🔗 Ver más: https://www.mettryc.com/inmueble/9838462"},
        {"id": "8370083", "listing": "Casa en venta en El Trigal Norte LR-8370083\n📍 Zona: Trigal Norte | 💰 Venta: US$180,000\n🔗 Ver más: https://www.mettryc.com/inmueble/8370083"},
    ]


def guardar_cliente(nombre, email, whatsapp, necesidad):
    """Envía la información del nuevo cliente a un canal de Telegram."""
    mensaje = f"""
    🙋‍♂️ **¡Nuevo Cliente Capturado por el Bot!** 🙋‍♀️
    - - - - - - - - - - - - - - - - -
    👤 **Cliente:** {nombre}
    📧 **Correo:** {email}
    📱 **WhatsApp:** {whatsapp}
    🔗 **Enlace WhatsApp:** https://wa.me/{whatsapp.replace('+', '')}
    - - - - - - - - - - - - - - - - -
    📋 **Resumen de Necesidad:**
    {necesidad}
    - - - - - - - - - - - - - - - - -
    **Agente asignado:** Genesis Mendez (Ejemplo)
    """
    url_telegram = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        response = requests.post(url_telegram, json=payload)
        return response.json()
    except Exception as e:
        print(f"Error al enviar notificación a Telegram: {e}")
        return None

def call_llm_api(user_id, user_message):
    """Llama a la API de un modelo de lenguaje (como OpenAI)."""
    history = conversation_history.get(user_id, [])
    
    # Prepara el historial para la API
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    # NOTA: Asegúrate de que el 'model' sea el correcto para tu caso de uso (ej. 'gpt-4-turbo', 'gpt-3.5-turbo').
    payload = {"model": "gpt-4-turbo", "messages": messages}
    
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        
        ai_response = response.json()["choices"][0]["message"]["content"]
        
        # Actualiza el historial
        conversation_history[user_id] = history + [("user", user_message), ("assistant", ai_response)]
        
        return ai_response
    except requests.exceptions.RequestException as e:
        print(f"Error llamando a la API de OpenAI: {e}")
        return "Lo siento, estoy teniendo problemas de conexión en este momento. Por favor, intenta de nuevo en unos minutos."


# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
# 4. APLICACIÓN FLASK (EL SERVIDOR WEB)
# Recibe los mensajes entrantes (ej. de WhatsApp) y responde.
# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --
app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>El Chatbot Inmobiliario está funcionando.</h1>"

@app.route('/webhook', methods=['POST'])
def webhook():
    """Este es el endpoint principal que recibe los mensajes."""
    data = request.json
    
    # La estructura de 'data' dependerá del proveedor de WhatsApp (ej. Twilio, Meta API).
    # Adapta estas líneas para extraer el ID del usuario y el mensaje.
    user_id = data.get("from")  # Ejemplo
    user_message = data.get("text", {}).get("body") # Ejemplo
    
    if not user_id or not user_message:
        return jsonify({"status": "error", "message": "Datos de entrada no válidos."}), 400

    # Llama al LLM para obtener una respuesta inteligente
    ai_response = call_llm_api(user_id, user_message)
    
    # Aquí deberías agregar la lógica para enviar la 'ai_response' de vuelta al usuario
    # a través de la API de mensajería que estés utilizando.
    print(f"Respuesta para {user_id}: {ai_response}")
    
    # Simulación de la respuesta a la API de WhatsApp
    return jsonify({
        "status": "success",
        "reply": ai_response
    })

if __name__ == '__main__':
    # El bot se ejecuta en el puerto 5000.
    # En producción, deberías usar un servidor WSGI como Gunicorn.
    app.run(host='0.0.0.0', port=5000, debug=True)
