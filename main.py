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

def obtener_inventario_desde_wasi():
    # URI correcta según la documentación (sin barra al final)
    url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}"
    
    try:
        response = requests.get(url)
        data = response.json()
        propiedades_limpias = []
        
        # Magia de la documentación: Iteramos solo sobre las llaves numéricas de Wasi
        for key, value in data.items():
            if key.isdigit():
                # Limpiamos las observaciones para no saturar a la IA
                obs = str(value.get('observations', ''))[:150].replace('\n', ' ')
                
                # Creamos un bloque de texto ultra-optimizado para Gemini
                prop = (
                    f"[ID: {value.get('id_property')}] {value.get('title')} | "
                    f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                    f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                    f"Área: {value.get('area')} m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                    f"Detalles: {obs}"
                )
                propiedades_limpias.append(prop)
                
        # Unimos todo en un solo documento fácil de leer para la IA
        return "\n".join(propiedades_limpias)
        
    except Exception as e:
        logger.error(f"Error procesando Wasi: {e}")
        return ""

def obtener_inventario():
    # Caché estricto de 24 horas
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        logger.info("Actualizando caché de Wasi (cada 24h)...")
        inventario_nuevo = obtener_inventario_desde_wasi()
        
        if inventario_nuevo: # Solo actualizamos si Wasi devolvió datos
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
            
    return cache["inventario_texto"]

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        api_key = request.headers.get("x-api-key")
        
        # Validar agentes
        if api_key not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        mensaje_cliente = data.get("message", "") or data.get("query", "")
        inventario = obtener_inventario()
        
        # Prompt estructurado a prueba de errores
        prompt = f"""
        Eres el Broker Inmobiliario de Mettryc Realty.
        
        AQUÍ ESTÁ TU INVENTARIO EXACTO Y ACTUALIZADO:
        {inventario}
        
        REGLAS DE ORO:
        1. El cliente pide: "{mensaje_cliente}"
        2. Busca en el inventario de arriba las propiedades que mejor coincidan (por zona o precio).
        3. NO INVENTES PROPIEDADES. NO CRUCES PRECIOS DE OTRAS PROPIEDADES. Responde estrictamente con la información proporcionada en el inventario.
        4. Si no hay una propiedad que encaje perfectamente, ofrécele la alternativa más cercana que tengamos.
        5. Siempre menciona el [ID] y la Ciudad de la propiedad para generar confianza.
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos consultando nuestra base de datos, por favor intenta nuevamente en unos segundos."}]}
