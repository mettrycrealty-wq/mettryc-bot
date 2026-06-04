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
        # DIAGNÓSTICO: Imprimir todo lo que llega para ver qué campos tiene
        logger.info(f"Datos completos recibidos: {data}")
        
        # Intentamos obtener el mensaje de varios campos posibles
        mensaje_cliente = data.get("message", "") or data.get("query", "") or ""
        
        if not mensaje_cliente:
            logger.warning("No se pudo detectar el mensaje en los campos comunes")
            
        # IA procesando
        response = model.generate_content(f"Actúa como asesor de Mettryc Realty. Responde: {mensaje_cliente}")
        
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
