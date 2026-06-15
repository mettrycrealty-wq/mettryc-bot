import os
import requests
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
# Nueva librería oficial
from google import genai 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración con la nueva librería
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Sistema de Caché 24h
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

# MEMORIA TEMPORAL
memoria_conversaciones = {}

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
        
        try:
            response = requests.get(url)
            data = response.json()
            contador_pagina = 0
            
            for key, value in data.items():
                if key.isdigit():
                    contador_pagina += 1
                    id_prop = value.get('id_property')
                    
                    # Log dentro del bucle, donde value existe
                    logger.info(f"Procesando ID: {id_prop} - Agente: {value.get('id_agent')}")
                    
                    enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                    
                    prop = (
                        f"-[ID: {id_prop}] {value.get('title')} | "
                        f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                        f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                        f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                        f"Enlace: {enlace_web}"
                    )
                    propiedades_limpias.append(prop)
            
            if contador_pagina < take:
                break
            skip += take
            
        except Exception as e:
            logger.error(f"Error procesando Wasi: {e}")
            break
            
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = payload.get("sender", "cliente_general")
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        inventario = obtener_inventario()
        
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty altamente eficiente.
        INVENTARIO DISPONIBLE:
        {inventario}
        
        REGLAS:
        1. RESPUESTAS CORTAS: Lista propiedades con características en 1-2 líneas y enlace.
        2. MÁXIMO 3 OPCIONES.
        3. RESTRICCIÓN DE ZONA ABSOLUTA: Si pide una zona, busca solo allí. Si no hay, sé honesto y no inventes.
        4. CERO ALUCINACIONES: No inventes precios ni datos.
        
        EL CLIENTE DICE: "{mensaje_cliente}"
        """
        
        # Nueva sintaxis de Google GenAI
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt_sistema
        )
        
        respuesta_bot = response.text
        
        # Gestión de memoria simple
        memoria_conversaciones[sender].append({"role": "user", "parts": [mensaje_cliente]})
        memoria_conversaciones[sender].append({"role": "model", "parts": [respuesta_bot]})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos ajustando los detalles del catálogo."}]}
