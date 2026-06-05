import os
import requests
import logging
from fastapi import FastAPI, Request, Header, HTTPException
from google.generativeai import GenerativeModel, configure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración
configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-3.5-flash")

# Tus credenciales de Wasi
WASI_COMPANY_ID = "3966678"
WASI_TOKEN = "uKfp_v0xp_QH5h_m3OI"

def obtener_inventario():
    # URL oficial de Wasi para listar propiedades
    url = f"https://api.wasi.co/v1/properties/?wasi_token={WASI_TOKEN}&id_company={WASI_COMPANY_ID}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.json().get('result', [])
        return []
    except Exception as e:
        logger.error(f"Error conectando a Wasi: {e}")
        return []

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        
        # Validar clave de agente
        api_key = request.headers.get("x-api-key")
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        mensaje_cliente = data.get("message", "") or data.get("query", "")
        
        # Consultar inventario
        inventario = obtener_inventario()
        
        # Prompt potenciado con tu inventario real
        prompt = f"""
        Eres el asistente premium de Mettryc Realty. 
        Aquí tienes el inventario actual de propiedades: {inventario}.
        
        Consulta del cliente: {mensaje_cliente}
        
        Instrucciones:
        1. Responde de forma ejecutiva.
        2. Si la consulta coincide con una propiedad del inventario, ofrece los detalles (precio, ubicación, descripción).
        3. Invita siempre a una visita en Valencia, Carabobo o Barquisimeto según corresponda.
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
