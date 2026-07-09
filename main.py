# main.py
import os
import re
import requests
import json
from flask import Flask, request, session
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

# --- CONFIGURACIÓN ---
# Carga las variables de entorno
# OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WASI_API_KEY, AGENT_ASSIGNMENT

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'una-clave-secreta-muy-segura')

# Configuración del cliente de OpenAI
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Configuración de Wasi
wasi_api_key = os.environ.get("WASI_API_KEY")
wasi_headers = {
    "id-company": "1036",
    "wasi-token": wasi_api_key
}
wasi_url_base = "https://api.wasi.co/v1/"

# --- FUNCIONES AUXILIARES DE LA IA ---

def analyze_user_intent_with_ia(conversation_history, user_message):
    """
    Usa la IA para analizar la intención del usuario, corregir y extraer datos.
    """
    # Simplificamos el historial para la IA
    history_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in conversation_history])

    prompt = f"""
    Eres un asistente inmobiliario experto para Mettryc Realty. Tu objetivo es entender y procesar la solicitud de un usuario.
    Analiza el último mensaje del usuario en el contexto de esta conversación:
    --- Historial ---
    {history_str}
    --- Fin del Historial ---

    Mensaje actual del usuario: "{user_message}"

    Basado en el mensaje actual y el historial, extrae la siguiente información en formato JSON. Si un dato no está presente, déjalo como null.
    - "intent": ¿Cuál es la intención principal del usuario? (ej: "buscar_propiedad", "pedir_informacion_id", "proporcionar_nombre", "proporcionar_email", "proporcionar_telefono", "aceptar_asignar_agente", "rechazar_opciones", "saludo")
    - "property_type": Tipo de propiedad (ej: "casa", "apartamento", "oficina").
    - "operation": Tipo de operación (ej: "venta", "alquiler"). Corrige 'comprar' a 'venta'.
    - "zone": Zona de búsqueda.
    - "min_bedrooms": Número mínimo de habitaciones.
    - "min_bathrooms": Número mínimo de baños.
    - "max_price": Presupuesto máximo como un número, sin puntos ni comas.
    - "name": Nombre completo del usuario. Extrae solo si se está pidiendo explícitamente el nombre.
    - "email": Correo electrónico del usuario.
    - "phone": Teléfono del usuario, con código de país.
    - "property_id": Si el usuario menciona un código de inmueble, extráelo.
    - "is_agent": Detecta si el usuario podría ser un colega inmobiliario (ej: usa palabras como 'colega', 'agente', 'inmobiliaria'). Devuelve true o false.

    Responde únicamente con el objeto JSON.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Error al analizar con IA: {e}")
        return {}


# --- FUNCIONES DE LÓGICA DEL BOT ---

def get_wasi_properties(filters):
    """Busca propiedades en Wasi según los filtros proporcionados."""
    query = filters.copy()
    # Mapeo de nuestros filtros a los de la API de Wasi
    if 'max_price' in query and query['max_price']:
        query['price_from'] = 0
        query['price_to'] = query['max_price']
        del query['max_price']

    # Añade solo los filtros que tienen valor
    params = {k: v for k, v in query.items() if v}
    
    try:
        response = requests.get(wasi_url_base + "property/search", headers=wasi_headers, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('properties', [])
    except requests.exceptions.RequestException as e:
        print(f"Error al buscar propiedades en Wasi: {e}")
        return []

def get_wasi_property_by_id(property_id):
    """Busca una propiedad específica por su ID en Wasi."""
    try:
        response = requests.get(f"{wasi_url_base}property/get/{property_id}", headers=wasi_headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error al obtener propiedad por ID de Wasi: {e}")
        return None

def format_properties_for_whatsapp(properties, limit=3):
    """Formatea la lista de propiedades para enviarlas por WhatsApp."""
    if not properties:
        return "No encontré propiedades que coincidan con tu búsqueda. ¿Quieres que un asesor te contacte para ayudarte personalmente?"

    message = "Perfecto. Con base en tu búsqueda, estas son algunas opciones que mejor encajan:\n\n"
    for prop in properties[:limit]:
        price = f"{int(prop['price']):,}".replace(",", ".") if prop.get('price') else "No disponible"
        message += f"*{prop.get('title', 'Propiedad sin título')}*\n"
        message += f"📍 Zona: {prop.get('zone', 'N/D')} | Ciudad: {prop.get('city', 'N/D')}\n"
        message += f"💰 {prop.get('status', 'Venta')}: US${price}\n"
        message += f"📐 Área: {prop.get('area', 'N/D')}m² | 🛏️ Habs: {prop.get('bedrooms', 'N/D')} | 🛁 Baños: {prop.get('bathrooms', 'N/D')}\n"
        message += f"🔗 Ver más: https://www.mettryc.com/inmueble/{prop['id_property']}\n\n"
    
    message += "¿Alguna te interesa para coordinar visita o prefieres que te asigne un asesor? 😊"
    return message


def format_property_for_agent(prop):
    """Formatea la ficha de una propiedad para un colega inmobiliario."""
    price = f"{int(prop['price']):,}".replace(",", ".") if prop.get('price') else "No disponible"
    agent_name = prop.get('user', {}).get('first_name', 'No') + " " + prop.get('user', {}).get('last_name', 'Asignado')
    agent_phone = prop.get('user', {}).get('phone', 'No disponible')

    message = f"*Ficha de Propiedad: {prop.get('title')}*\n\n"
    message += f"*ID:* {prop['id_property']}\n"
    message += f"*Enlace:* https://www.mettryc.com/inmueble/{prop['id_property']}\n\n"
    message += f"*Detalles:*\n"
    message += f"*- Tipo:* {prop.get('property_type', 'N/D')}\n"
    message += f"*- Zona:* {prop.get('zone', 'N/D')}, {prop.get('city', 'N/D')}\n"
    message += f"*- Precio:* US${price}\n"
    message += f"*- Habitaciones:* {prop.get('bedrooms', 'N/D')}\n"
    message += f"*- Baños:* {prop.get('bathrooms', 'N/D')}\n\n"
    message += f"*Contacto del Captador:*\n"
    message += f"*- Nombre:* {agent_name}\n"
    message += f"*- WhatsApp:* {agent_phone}\n\n"
    message += "¡Mucho éxito con tu cliente!"
    return message


def send_telegram_notification(data):
    """Envía una notificación a un grupo de Telegram."""
    agent_assignment = json.loads(os.environ.get("AGENT_ASSIGNMENT", "{}"))
    agent_name = agent_assignment.get(data.get('assigned_agent', 'default'), "No asignado")
    
    message = (
        f"*{'🙌 Nuevo Cliente Capturado' if not data.get('is_agent') else '📞 Contacto de Colega Inmobiliario'}*\n\n"
        f"👤 *Agente asignado:* {agent_name}\n\n"
        f"🙋‍♂️ *Interesado:* {data.get('name', 'N/A')}\n"
        f"✉️ *Correo:* {data.get('email', 'N/A')}\n"
        f"📱 *WhatsApp:* {data.get('phone', 'N/A')}\n"
        f"🔗 *Enlace WhatsApp:* https://wa.me/{data.get('phone', 'N/A').replace('+', '')}\n\n"
        f"*📄 Resumen de necesidad:*\n"
        f"- *Tipo de propiedad:* {data.get('property_type', 'N/A')}\n"
        f"- *Operación:* {data.get('operation', 'N/A')}\n"
        f"- *Zona:* {data.get('zone', 'N/A')}\n"
        f"- *Presupuesto máximo:* ${data.get('max_price', 'N/A'):,}\n"
        f"- *Habitaciones mínimas:* {data.get('min_bedrooms', 'N/A')}\n"
        f"- *Baños mínimos:* {data.get('min_bathrooms', 'N/A')}\n"
    )
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error al enviar notificación a Telegram: {e}")


def assign_agent_and_notify(user_data):
    """Asigna un agente y notifica al cliente y a Telegram."""
    # Lógica de asignación de agente (ej. round-robin, por zona, etc.)
    # Por ahora, asignamos a un agente por defecto.
    agent_assignment = json.loads(os.environ.get("AGENT_ASSIGNMENT", "{}"))
    agent_name = agent_assignment.get('default', "un asesor") # Cambia 'default' por tu lógica
    
    user_data['assigned_agent'] = 'default' 
    send_telegram_notification(user_data)
    
    user_name_first = user_data.get('name', 'Hola').split(' ')[0]
    return f"¡Perfecto, {user_name_first}! ✨ Ya registré tus datos y asigné tu solicitud a {agent_name}. Te contactará muy pronto. 🤝"


# --- RUTA PRINCIPAL DE FLASK ---

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    """Responde a los mensajes entrantes de WhatsApp."""
    
    incoming_msg = request.values.get('Body', '').strip()
    resp = MessagingResponse()
    msg = resp.message()
    
    # Inicializa el historial y datos del usuario en la sesión si no existen
    if 'history' not in session:
        session['history'] = []
    if 'user_data' not in session:
        session['user_data'] = {
            'is_agent': False,
            'step': 'start' # Pasos: start, awaiting_operation, awaiting_zone, ..., awaiting_name, awaiting_email, awaiting_phone, done
        }

    # Analiza el mensaje del usuario con la IA
    analysis = analyze_user_intent_with_ia(session['history'], incoming_msg)
    
    # Actualiza los datos del usuario con la información extraída por la IA
    for key, value in analysis.items():
        if value is not None and value != '':
            session['user_data'][key] = value

    current_step = session['user_data'].get('step', 'start')
    
    # Manejo de flujo especial para colegas inmobiliarios
    if session['user_data'].get('is_agent'):
        if analysis.get('property_id'):
            property_info = get_wasi_property_by_id(analysis['property_id'])
            if property_info:
                response_text = format_property_for_agent(property_info)
                session.clear() # Finaliza la conversación
            else:
                response_text = f"No encontré información para el ID {analysis['property_id']}. Por favor, verifica el código."
        elif current_step == 'start':
            response_text = "¡Hola colega! ¿Buscas alguna propiedad por su ID o necesitas ayuda con una búsqueda para un cliente?"
            session['user_data']['step'] = 'agent_menu'
        else: # Si ya es colega y no pide ID, puede hacer una búsqueda normal
            # (Aquí se podría extender la lógica para búsquedas de colegas)
            response_text = "Ok, colega. Dime qué necesitas buscar y te ayudo a encontrarlo en nuestro inventario."
        
        msg.body(response_text)
        session['history'].append({"role": "user", "content": incoming_msg})
        session['history'].append({"role": "assistant", "content": response_text})
        return str(resp)


    # --- Flujo de Conversación para Clientes ---
    
    # 1. Inicio
    if current_step == 'start':
        msg.body("¡Hola! Soy tu asistente virtual de Mettryc Realty. ¿En qué puedo ayudarte hoy? ¿Buscas comprar, alquilar o vender una propiedad?")
        session['user_data']['step'] = 'awaiting_operation'

    # 2. Esperando Operación (Venta/Alquiler)
    elif current_step == 'awaiting_operation':
        if session['user_data'].get('operation'):
            msg.body(f"¡Excelente! ¿En qué zona te gustaría buscar para {session['user_data']['operation']}?")
            session['user_data']['step'] = 'awaiting_zone'
        else:
            msg.body("No entendí bien. ¿Buscas comprar (venta) o alquilar?")
            
    # 3. Esperando Zona
    elif current_step == 'awaiting_zone':
        if session['user_data'].get('zone'):
            # Aquí se pueden añadir más preguntas (habitaciones, baños, etc.)
            # Para simplificar, vamos a buscar con lo que tenemos y luego pedimos datos de contacto
            filters = {
                'id_property_type': '1' if session['user_data'].get('property_type') == 'apartamento' else ('2' if session['user_data'].get('property_type') == 'casa' else None),
                'id_property_status': '1' if session['user_data'].get('operation') == 'venta' else ('2' if session['user_data'].get('operation') == 'alquiler' else None),
                'q': session['user_data'].get('zone')
            }
            properties = get_wasi_properties({k: v for k, v in filters.items() if v})
            response_text = format_properties_for_whatsapp(properties)
            msg.body(response_text)
            session['user_data']['step'] = 'awaiting_interest' # Espera a ver si le interesa alguna
        else:
            msg.body("¿En qué zona te gustaría buscar?")

    # 4. Esperando Interés / Decisión de contacto
    elif current_step == 'awaiting_interest':
         if 'no' in incoming_msg.lower(): # Si el usuario dice que no le gustan
                msg.body("Entendido. Para ayudarte a encontrar la propiedad ideal, ¿te parece si te asigno a uno de nuestros asesores expertos?")
                session['user_data']['step'] = 'awaiting_agent_confirmation'
         else: # Si dice que sí o selecciona una opción
            msg.body("¡Excelente! Para continuar y asignarte un asesor, por favor, compárteme tu nombre completo (nombre y apellido).")
            session['user_data']['step'] = 'awaiting_name'

    # 5. Esperando Confirmación para Asignar Agente
    elif current_step == 'awaiting_agent_confirmation':
        if 'si' in incoming_msg.lower() or 'ok' in incoming_msg.lower():
            msg.body("¡Muy bien! Para empezar, dime tu nombre completo, por favor.")
            session['user_data']['step'] = 'awaiting_name'
        else:
            msg.body("Entendido. Si cambias de opinión, no dudes en escribirme. ¡Que tengas un buen día!")
            session.clear()
            
    # 6. Esperando Nombre
    elif current_step == 'awaiting_name':
        if session['user_data'].get('name'):
            user_first_name = session['user_data']['name'].split(' ')[0]
            msg.body(f"Perfecto, {user_first_name}. 🙌 Ahora indícame tu correo electrónico.")
            session['user_data']['step'] = 'awaiting_email'
        else:
            msg.body("No pude identificar un nombre. Por favor, ¿podrías indicarme tu nombre y apellido?")
    
    # 7. Esperando Email
    elif current_step == 'awaiting_email':
        if session['user_data'].get('email') and '@' in session['user_data']['email']:
            msg.body("Genial. Por último, compárteme tu número de WhatsApp con el código de país (ej: +584141234567).")
            session['user_data']['step'] = 'awaiting_phone'
        else:
            msg.body("Parece que ese no es un correo válido. Por favor, verifica e ingrésalo de nuevo.")
            
    # 8. Esperando Teléfono y Finalización
    elif current_step == 'awaiting_phone':
        if session['user_data'].get('phone'):
            final_response = assign_agent_and_notify(session['user_data'])
            msg.body(final_response)
            session.clear() # Limpia la sesión para una nueva conversación
        else:
            msg.body("No pude validar el número de teléfono. Asegúrate de incluir el código de país (ej: +58...).")
            
    # Guarda el historial
    session['history'].append({"role": "user", "content": incoming_msg})
    session['history'].append({"role": "assistant", "content": msg.body})
    
    return str(resp)


if __name__ == "__main__":
    app.run(debug=True)
