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
model = GenerativeModel("gemini-2.5-flash-lite")

# Sistema de Caché 24h
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

# MEMORIA TEMPORAL: Guardará las conversaciones por número de teléfono
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
                    
                    # AQUÍ ESTÁ EL ENLACE (PUNTO 1) - CAMBIA EL DOMINIO POR EL TUYO
                    id_prop = value.get('id_property')
                    enlace_web = f"https://www.tudominio.com/propiedad/{id_prop}"
                    
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

        # Capturamos el remitente (teléfono) y el mensaje
        sender = data.get("sender", "cliente_general")
        mensaje_cliente = data.get("message", "") or data.get("query", "")
        inventario = obtener_inventario()
        
        # Inicializamos la memoria para este cliente si es nuevo (PUNTO 2)
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        # PROMPT ULTRA-ESTRICTO
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty altamente eficiente.
        
        INVENTARIO DISPONIBLE (Única fuente de verdad):
        {inventario}
        
        REGLAS DE ORO (NO NEGOCIABLES):
        1. RESPUESTAS CORTAS Y PRECISAS: No saludes en exceso. Da una breve introducción y lista las propiedades. Cada propiedad debe tener sus características principales en 1-2 líneas y su "Enlace" al final.
        2. EXACTAMENTE 3 OPCIONES: Si el cliente pide propiedades, ofrécele las 3 que mejor coincidan al 80%. Si hay menos de 3 que cumplan, ofrece las que haya, pero NUNCA ofrezcas más de 3.
        3. RESTRICCIÓN DE ZONA ABSOLUTA: Si el cliente indica una zona o ciudad, SOLO busca allí. Si no tienes propiedades en esa zona específica, responde CORTÉSMENTE que no hay disponibilidad en esa área por el momento y termina la respuesta. NO ofrezcas propiedades de otras zonas.
        4. CERO ALUCINACIONES: Tienes PROHIBIDO inventar información, precios o propiedades que no estén en el texto del INVENTARIO DISPONIBLE.
        5. MEMORIA: Recuerda el contexto de nuestros mensajes anteriores.
        
        EL CLIENTE DICE AHORA: "{mensaje_cliente}"
        """
        
        # Preparamos el historial para Gemini
        historial_api = list(memoria_conversaciones[sender])
        historial_api.append({"role": "user", "parts": [prompt_sistema]})
        
        # Llamada a la IA con historial
        response = model.generate_content(historial_api)
        respuesta_bot = response.text
        
        # Guardamos en la memoria la interacción (solo el mensaje, sin el inventario)
        memoria_conversaciones[sender].append({"role": "user", "parts": [mensaje_cliente]})
        memoria_conversaciones[sender].append({"role": "model", "parts": [respuesta_bot]})
        
        # Mantenemos solo los últimos 20 mensajes (10 tuyos, 10 del bot)
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos consultando nuestra base de datos, por favor intenta nuevamente en unos segundos."}]}
