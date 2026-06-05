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
    # Añadimos &take=100 para forzar a Wasi a enviar todo tu catálogo (hasta 100 propiedades)
    url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take=100"
    
    try:
        response = requests.get(url)
        data = response.json()
        propiedades_limpias = []
        
        for key, value in data.items():
            if key.isdigit():
                # Limpiamos las observaciones para no saturar a la IA
                obs = str(value.get('observations', ''))[:150].replace('\n', ' ')
                
                # Creamos un bloque de texto ultra-optimizado para Gemini
                prop = (
                    f"-[ID: {value.get('id_property')}] {value.get('title')} | "
                    f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                    f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                    f"Área: {value.get('area')} m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                    f"Detalles: {obs}"
                )
                propiedades_limpias.append(prop)
                
        # LOG PARA TI: Esto te dirá exactamente cuántas propiedades envió Wasi
        logger.info(f"¡ÉXITO! Se han cargado {len(propiedades_limpias)} propiedades de Wasi.")
        
        return "\n".join(propiedades_limpias)
        
    except Exception as e:
        logger.error(f"Error procesando Wasi: {e}")
        return ""

def obtener_inventario():
    # Caché estricto de 24 horas
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        logger.info("Actualizando caché de Wasi (cada 24h)...")
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
        
        REGLAS DE ORO ESTRICTAS:
        1. LEE TODO EL INVENTARIO antes de responder.
        2. El cliente pide: "{mensaje_cliente}"
        3. Busca detalladamente propiedades que coincidan en CIUDAD, ZONA o PRECIO.
        4. Si encuentras una coincidencia, ofrécela mencionando su [ID].
        5. Si no hay NINGUNA propiedad que coincida exactamente con la ciudad o zona solicitada, DEBES DECIRLO CLARAMENTE: "No tengo propiedades en esa zona exacta, pero te ofrezco esta excelente alternativa..." y ofreces otra de la lista.
        6. NO repitas la misma propiedad si el cliente pide otra zona.
        """
        
        response = model.generate_content(prompt)
        return {"replies": [{"message": response.text}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos consultando nuestra base de datos, por favor intenta nuevamente en unos segundos."}]}
