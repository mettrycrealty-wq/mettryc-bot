import os
import csv
import requests
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistemas de Caché
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

agentes_cache = {
    "lista": [],
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
MODELO_OPENROUTER = "deepseek/deepseek-chat"

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    max_propiedades = 2000
    
    logger.info("Iniciando descarga completa del inventario desde Wasi...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
        try:
            logger.info(f"Consultando Wasi (Propiedad {skip} a {skip + take})...")
            response = requests.get(url, timeout=15)
            data = response.json()
            contador_pagina = 0
            
            for key, value in data.items():
                if isinstance(value, dict) and key.isdigit():
                    contador_pagina += 1
                    id_prop = value.get('id_property')
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
            if skip >= max_propiedades:
                break
        except Exception as e:
            logger.error(f"Error paginando Wasi en skip {skip}: {e}")
            break
            
    logger.info(f"¡Éxito! Se almacenaron {len(propiedades_limpias)} propiedades en el servidor.")
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24):
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def obtener_agentes_desde_sheet():
    # Ahora usaremos la URL completa del Script de Google
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url:
        logger.
