import os
import requests
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistema de Caché Original
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

# MEMORIA TEMPORAL: Guardará las conversaciones por número de teléfono
memoria_conversaciones = {}

# ELIGE TU MODELO AQUÍ (Puedes cambiarlo cuando quieras)
MODELO_OPENROUTER = "deepseek/deepseek-chat" 
# Alternativas: "meta-llama/llama-3.3-70b-instruct" o "google/gemini-2.0-flash-001"

def obtener_inventario_desde_wasi():
    url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}"
    propiedades_limpias = []
    
    try:
        response = requests.get(url)
        data = response.json()
        
        for key, value in data.items():
            if key.isdigit():
                id_prop = value.get('id_property')
                enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                
                prop = (
                    f"-[ID: {id_prop}] {value.get('title')} | "
                    f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                    f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                    f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                    f"Enlace: {enlace_web}"
                )
                propiedades_limpias.append(prop)
                
    except Exception as e:
        logger.error(f"Error procesando Wasi: {e}")
        
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

# --- NUEVO MOTOR: OPENROUTER ---
def consultar_ia(mensajes):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODELO_OPENROUTER,
        "messages": mensajes
    }
    
    try:
        respuesta = requests.post(url, headers=headers, json=payload)
        datos = respuesta.json()
        return datos['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Error en OpenRouter: {e}")
        return "Estamos experimentando una alta demanda, por favor intenta en un momento."

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = payload.get("sender", "cliente_general")
        mensaje_cliente = payload.get("message", "")
        
        if not isinstance(mensaje_cliente, str):
            mensaje_cliente = str(mensaje_cliente)
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        inventario = obtener_inventario()
        
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty altamente eficiente.
        
        INVENTARIO DISPONIBLE (Única fuente de verdad):
        {inventario}
        
        REGLAS DE ORO (NO NEGOCIABLES):
        1. RESPUESTAS CORTAS Y PRECISAS: Lista las propiedades (1-2 líneas c/u) con su enlace al final.
        2. EXACTAMENTE 3 OPCIONES MÁXIMO que coincidan al 80%.
        3. RESTRICCIÓN DE ZONA ABSOLUTA: Busca SOLO en la zona/ciudad pedida. Si no hay, dilo amablemente y no ofrezcas de otras zonas.
        4. CERO ALUCINACIONES: Prohibido inventar propiedades o precios.
        """
        
        # Estructura de mensajes para OpenRouter (Formato Universal OpenAI)
        historial_api = [{"role": "system", "content": prompt_sistema}]
        historial_api.extend(memoria_conversaciones[sender])
        historial_api.append({"role": "user", "content": mensaje_cliente})
        
        # Llamada a la IA
        respuesta_bot = consultar_ia(historial_api)
        
        # Guardamos memoria
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos consultando nuestra base de datos, por favor intenta nuevamente."}]}
