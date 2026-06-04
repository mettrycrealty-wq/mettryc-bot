import os
import requests
import logging
from fastapi import FastAPI, Request, Header, HTTPException
from google.generativeai import GenerativeModel, configure

# Configurar logs para ver el flujo en la consola de Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración inicial
configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-3.5-flash")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        # Validar clave
        claves_permitidas = os.getenv("API_KEYS_AGENTES", "").split(",")
        if api_key not in claves_permitidas:
            logger.error("Intento de acceso con clave inválida")
            raise HTTPException(status_code=403, detail="Acceso denegado")

        # Ajuste: El log mostró que el campo se llama 'message', no 'query'
        mensaje_cliente = data.get("message", "")
        logger.info(f"Mensaje recibido: {mensaje_cliente}")
        
        # IA procesando
        response = model.generate_content(f"Actúa como asesor experto de Mettryc Realty. Responde de forma ejecutiva y profesional: {mensaje_cliente}")
        
        # Ajuste: AutoResponder espera una lista de strings o una lista de objetos
        # El formato {"replies": [texto]} es el más compatible con la app
        return {"replies": [response.text]}
    
    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
