import os
import requests
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from google.generativeai import GenerativeModel, configure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Configuración de Gemini original
configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-2.5-flash-lite")

# Sistema de Caché en Servidor (Almacena el catálogo por 24 horas)
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    
    logger.info("Iniciando descarga completa del inventario desde Wasi...")
    
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
                    enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                    
                    # Formato ultra-compacto para ahorrar tokens en Gemini y evitar el error 429
                    prop = (
                        f"-[ID: {id_prop}] {value.get('title')} | "
                        f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                        f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                        f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                        f"Enlace: {enlace_web}"
                    )
                    propiedades_limpias.append(prop)
            
            # Si la página trajo menos de 100, terminamos el bucle
            if contador_pagina < take:
                break
            
            skip += take
            
        except Exception as e:
            logger.error(f"Error paginando Wasi en skip {skip}: {e}")
            break
            
    logger.info(f"¡Éxito! Se almacenaron {len(propiedades_limpias)} propiedades en la copia del servidor.")
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    # Verifica si pasaron más de 24 horas desde la última descarga
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
            logger.info("Copia del servidor actualizada por las próximas 24 horas.")
    return cache["inventario_texto"]

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        mensaje_cliente = payload.get("message", "")
        
        if not isinstance(mensaje_cliente, str):
            mensaje_cliente = str(mensaje_cliente)
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        # Extrae la copia del inventario guardada en el servidor (rápido y sin llamar a Wasi)
        inventario = obtener_inventario()
        
        prompt_sistema = f"""
        Eres un Broker Inmobiliario de Mettryc Realty altamente eficiente.
        
        INVENTARIO DISPONIBLE:
        {inventario}
        
        REGLAS:
        1. Busca en el inventario propiedades que coincidan con la solicitud.
        2. Respuestas cortas y precisas. Máximo 3 opciones. Incluye el enlace correspondiente.
        3. Si no hay propiedades en la zona solicitada, indícalo amablemente sin inventar datos.
        
        Cliente: "{mensaje_cliente}"
        """
        
        response = model.generate_content(prompt_sistema)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor intenta en un momento."}]}
