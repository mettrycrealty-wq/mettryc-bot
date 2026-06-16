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
    # Usaremos una variable específica para el sheet de asignación
    sheet_id = os.getenv("GOOGLE_SHEET_TURNOS_ID") 
    if not sheet_id:
        logger.warning("GOOGLE_SHEET_TURNOS_ID no configurada. Omitiendo descarga de agentes.")
        return agentes_cache["lista"]

    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        try:
            response = requests.get(url, timeout=10)
            lines = response.text.splitlines()
            reader = csv.reader(lines)
            
            next(reader) # Saltamos los encabezados
            lista_nueva = []
            
            for row in reader:
                if row and len(row) >= 2:
                    lista_nueva.append({
                        "nombre": row[0].strip(),
                        "correo": row[1].strip(),
                        "telegram_id": row[2].strip() if len(row) >= 3 else "" # Captura la 3ra columna
                    })
            
            if lista_nueva:
                agentes_cache["lista"] = lista_nueva
                agentes_cache["ultima_actualizacion"] = datetime.now()
                logger.info(f"Sincronizados {len(lista_nueva)} agentes para turnos.")
        except Exception as e:
            logger.error(f"Error cargando lista de turnos: {e}")
            
    return agentes_cache["lista"]

def asignar_agente_round_robin():
    lista_agentes = obtener_agentes_desde_sheet()
    if not lista_agentes:
        return None
        
    agentes_cache["ultimo_indice"] += 1
    if agentes_cache["ultimo_indice"] >= len(lista_agentes):
        agentes_cache["ultimo_indice"] = 0
        
    return lista_agentes[agentes_cache["ultimo_indice"]]

def enviar_notificacion_agente(agente, whatsapp_cliente, datos_lead):
    # Enviar Email
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    if smtp_user and smtp_pass:
        try:
            asunto = f"🔥 NUEVO CLIENTE ASIGNADO - Mettryc Realty"
            cuerpo = f"Hola {agente['nombre']},\n\nSe te ha asignado un nuevo lead:\n\n{datos_lead}\n\nWHATSAPP:\nhttps://wa.me/{whatsapp_cliente}"
            msg = MIMEText(cuerpo)
            msg['Subject'] = asunto
            msg['From'] = smtp_user
            msg['To'] = agente['correo']
            
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [agente['correo']], msg.as_string())
            server.quit()
        except Exception as e:
            logger.error(f"Error correo: {e}")

    # Enviar Telegram
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_id = agente.get("telegram_id")
    
    if telegram_token and telegram_id:
        try:
            mensaje_tg = f"🚨 *NUEVO LEAD ASIGNADO* 🚨\n\nHola {agente['nombre']},\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar ahora:*\n[Abrir WhatsApp](https://wa.me/{whatsapp_cliente})"
            url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            payload = {
                "chat_id": telegram_id,
                "text": mensaje_tg,
                "parse_mode": "Markdown"
            }
            requests.post(url_tg, json=payload, timeout=5)
            logger.info(f"Notificación Telegram enviada a {agente['nombre']}.")
        except Exception as e:
            logger.error(f"Error enviando Telegram: {e}")

def consultar_ia(mensajes):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json"
    }
    try:
        respuesta = requests.post(url, headers=headers, json={"model": MODELO_OPENROUTER, "messages": mensajes})
        return respuesta.json()['choices'][0]['message']['content']
    except Exception as e:
        logger.error(f"Error OpenRouter: {e}")
        return "Estamos experimentando alta demanda, intenta en un momento."

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
        
        REGLAS DE ORO:
        1. RESPUESTAS CORTAS Y PRECISAS. Máximo 3 opciones con enlace crudo.
        2. RESTRICCIÓN DE ZONA ABSOLUTA.
        3. CERO ALUCINACIONES.
        
        4. ESTRATEGIA DE CAPTURA DE LEADS (OBLIGATORIA):
        Si el cliente demuestra interés en comprar/visitar/saber más de un inmueble, pausa la venta y pide amablemente sus datos de contacto UNO A UNO:
        - Primero solicítale su Nombre Completo.
        - Cuando te dé el nombre, solicítale su Correo Electrónico.
        
        En el mensaje exacto donde el cliente ya te haya entregado Nombre y Correo, OBLIGATORIAMENTE incluye esta etiqueta al final de tu texto:
        ###LEAD_CAPTURED###Nombre: [Nombre del cliente] | Correo: [Correo] | Interés: [Lo que busca]###
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}]
        historial_api.extend(memoria_conversaciones[sender])
        historial_api.append({"role": "user", "content": mensaje_cliente})
        
        respuesta_bot = consultar_ia(historial_api)
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            try:
                partes = respuesta_bot.split("###LEAD_CAPTURED###")
                texto_cliente = partes[0].strip()
                datos_lead_raw = partes[1].replace("###", "").strip()
                
                agente = asignar_agente_round_robin()
                
                if agente:
                    enviar_notificacion_agente(agente, sender, datos_lead_raw)
                    texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido notificado y se comunicará contigo de inmediato."
                
                respuesta_bot = texto_cliente
            except Exception as e:
                logger.error(f"Error procesando lead: {e}")
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor intenta nuevamente."}]}
