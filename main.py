import os
import requests
import logging
from fastapi import FastAPI, Request, Header, HTTPException
from google.generativeai import GenerativeModel, configure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-3.5-flash")

def obtener_inventario():
    # URL estructurada con los parámetros correctos para Wasi
    url = f"https://api.wasi.co/v1/properties/?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}"
    try:
        response = requests.get(url)
        data = response.json()
        
        # LOG de diagnóstico: veremos qué devuelve Wasi realmente
        logger.info(f"Respuesta cruda de Wasi: {data}")
        
        # La estructura típica de Wasi suele ser data['result']
        if 'result' in data:
            return data['result']
        return []
    except Exception as e:
        logger.error(f"Error técnico conectando a Wasi: {e}")
        return []

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        mensaje_cliente = data.get("message", "") or data.get("query", "")
        
        # Obtenemos inventario y lo convertimos a texto para la IA
        inventario = obtener_inventario()
        inventario_texto = str(inventario)[:5000] # Limitamos para no exceder el prompt
        
        prompt = f"""
        Eres el asistente premium de Mettryc Realty. 
        Inventario disponible (JSON): {inventario_texto}
        
        Si el inventario está vacío, menciona que estamos actualizando nuestro catálogo. 
        Si hay propiedades, busca la que mejor encaje con la petición: {mensaje_cliente}
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error procesando: {e}")
        return {"replies": [{"message": "Estamos ajustando los detalles técnicos del catálogo. ¿Podrías intentar en un momento?"}]}
