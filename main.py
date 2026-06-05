import os
import requests
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from google.generativeai import GenerativeModel, configure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración
configure(api_key=os.getenv("GEMINI_API_KEY"))
# Cambiado al modelo solicitado
model = GenerativeModel("gemini-2.5-flash-lite")

# Sistema de Caché Global
cache = {
    "inventario": [],
    "ultima_actualizacion": datetime.min
}

def obtener_inventario_desde_wasi():
    url = f"https://api.wasi.co/v1/property/search/?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.json().get('result', [])
        return []
    except Exception as e:
        logger.error(f"Error conectando a Wasi: {e}")
        return []

def obtener_inventario():
    # Si han pasado más de 24 horas o el caché está vacío, actualizamos
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        logger.info("Actualizando inventario desde Wasi...")
        cache["inventario"] = obtener_inventario_desde_wasi()
        cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario"]

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        # Validación de seguridad
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        mensaje_cliente = data.get("message", "") or data.get("query", "")
        inventario = obtener_inventario()
        
        # Convertimos solo lo necesario a texto para no saturar la IA
        inventario_texto = str(inventario)
        
        prompt = f"""
        Eres el asistente ejecutivo de Mettryc Realty.
        
        INVENTARIO (Base de datos oficial):
        {inventario_texto}
        
        REGLAS INQUEBRANTABLES:
        1. Analiza el inventario y responde basándote ÚNICAMENTE en estos datos.
        2. Si no hay coincidencia exacta con lo que pide el usuario, NO inventes propiedades. Sé honesto y ofrece alternativas reales presentes en el inventario.
        3. Indica siempre el nombre o ID de la propiedad que sugieres.
        4. Mantén un tono profesional y enfocado en concretar la cita.
        
        Consulta del usuario: {mensaje_cliente}
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error: {e}")
        return {"replies": [{"message": "Estamos ajustando los detalles técnicos del catálogo. Intenta en un momento."}]}
