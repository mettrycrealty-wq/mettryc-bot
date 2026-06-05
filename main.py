import os
import requests
import logging
from fastapi import FastAPI, Request, HTTPException
from google.generativeai import GenerativeModel, configure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración inicial
configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-2.5-flash-lite")

def obtener_inventario():
    # Endpoint correcto según la API de Wasi para búsqueda
    wasi_token = os.getenv('WASI_TOKEN')
    company_id = os.getenv('WASI_COMPANY_ID')
    url = f"https://api.wasi.co/v1/property/search/?wasi_token={wasi_token}&id_company={company_id}"
    
    try:
        response = requests.get(url)
        # Log del estado para diagnóstico
        logger.info(f"Estado respuesta Wasi: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            logger.error(f"Error Wasi: {response.text}")
            return []
    except Exception as e:
        logger.error(f"Error técnico conectando a Wasi: {e}")
        return []

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        mensaje_cliente = data.get("message", "") or data.get("query", "")
        
        # Validación de seguridad
        api_key = request.headers.get("x-api-key")
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")
        
        # Obtenemos inventario
        inventario = obtener_inventario()
        inventario_texto = str(inventario)[:5000] 
        
        prompt = f"""
        Eres el asistente ejecutivo de Mettryc Realty. 
        Inventario disponible: {inventario_texto}
        
        Consulta del cliente: {mensaje_cliente}
        
        Instrucciones: Analiza el inventario y responde con los detalles de las propiedades que coincidan. 
        Sé profesional y busca siempre cerrar la cita.
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error procesando: {e}")
        return {"replies": [{"message": "Estamos ajustando los detalles del catálogo. Por favor, intenta de nuevo."}]}
