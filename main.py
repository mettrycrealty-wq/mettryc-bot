import os
import requests
import logging
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
# Usaremos la librería estable para evitar conflictos
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
# Usamos gemini-1.5-flash que es más estable y eficiente en cuota que la versión 2.0
model = genai.GenerativeModel("gemini-1.5-flash")

app = FastAPI()

def obtener_inventario():
    # URL de Wasi
    url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}"
    try:
        response = requests.get(url)
        data = response.json()
        propiedades = []
        
        for key, value in data.items():
            if key.isdigit():
                # El log debe estar AQUÍ, dentro del bucle donde 'value' existe
                # logger.info(f"Procesando ID: {value.get('id_property')} - Agente: {value.get('id_agent')}")
                
                prop = f"- {value.get('title')} | {value.get('city_label')} | {value.get('sale_price_label')}"
                propiedades.append(prop)
        return "\n".join(propiedades[:50]) # Limitamos a 50 propiedades para no agotar la IA
    except Exception as e:
        logger.error(f"Error Wasi: {e}")
        return ""

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        mensaje = data.get("message", "")
        
        inventario = obtener_inventario()
        
        # Prompt optimizado para consumir menos tokens
        prompt = f"Eres un asistente inmobiliario. Inventario: {inventario}. Cliente pide: {mensaje}. Responde brevemente y solo con propiedades de la lista."
        
        # Intentar generar respuesta con reintento simple ante errores
        try:
            response = model.generate_content(prompt)
            return {"replies": [{"message": response.text}]}
        except Exception as e:
            if "429" in str(e):
                return {"replies": [{"message": "Estamos procesando tu solicitud, por favor espera un momento."}]}
            raise e
            
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Error técnico."}]}
