import os
import requests
import logging
import time
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Sistemas de Caché y Memoria
cache = {
    "inventario_texto": "",
    "ultima_actualizacion": datetime.min
}

sheets_cache = {
    "agentes": [],
    "captadores": {},
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set() 

# --- CONFIGURACIÓN ESTRATÉGICA DE MODELOS ---
MODELO_PRINCIPAL = "google/gemini-1.5-flash"
MODELO_RESPALDO = "anthropic/claude-3-5-haiku" 

# --- FUNCIONES DE SOPORTE ---

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    
    logger.info("Iniciando descarga completa de propiedades ACTIVAS desde Wasi...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}&status=1"
        exito_pagina = False
        intentos = 0
        
        while intentos < 3 and not exito_pagina:
            try:
                response = requests.get(url, timeout=30)
                data = response.json()
                contador_pagina = 0
                for key, value in data.items():
                    if isinstance(value, dict) and key.isdigit():
                        contador_pagina += 1
                        id_prop = value.get('id_property')
                        enlace_web = f"https://www.mettryc.com/inmueble/{id_prop}"
                        
                        user_data = value.get('user_data', {})
                        asesor_encargado = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or "Asesor Mettryc"
                        
                        prop = (
                            f"-[ID: {id_prop}] {value.get('title')} | "
                            f"Ciudad: {value.get('city_label')} | Zona: {value.get('zone_label')} | "
                            f"Venta: {value.get('sale_price_label')} | Renta: {value.get('rent_price_label')} | "
                            f"Área: {value.get('area')}m2 | Hab: {value.get('bedrooms')} | Baños: {value.get('bathrooms')} | "
                            f"Enlace: {enlace_web}"
                        )
                        propiedades_limpias.append(prop)
                
                exito_pagina = True
                
                if contador_pagina < take: 
                    return "\n".join(propiedades_limpias)
                
                skip += take
                time.sleep(2)
            except Exception:
                intentos += 1
                time.sleep(5)
                
        if not exito_pagina: 
            break
            
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def sincronizar_google_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url: return
    if datetime.now() - sheets_cache["ultima_actualizacion"] > timedelta(hours=1) or not sheets_cache["agentes"]:
        try:
            response = requests.get(script_url, timeout=15)
            payload_sheet = response.json()
            if isinstance(payload_sheet, dict):
                sheets_cache["agentes"] = payload_sheet.get("agentes", [])
                sheets_cache["captadores"] = payload_sheet.get("captadores", {})
                sheets_cache["ultima_actualizacion"] = datetime.now()
        except Exception as e: logger.error(f"Error sincronizando Google Sheets: {e}")

def asignar_agente_round_robin():
    sincronizar_google_sheet()
    lista_agentes = sheets_cache["agentes"]
    if not lista_agentes: return None
    sheets_cache["ultimo_indice"] = (sheets_cache["ultimo_indice"] + 1) % len(lista_agentes)
    return lista_agentes[sheets_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = str(agente.get("telegram_id", "")).strip()
    link_wa = f"https://wa.me/{telefono_destino.replace('+', '').replace(' ', '')}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar de inmediato:* {link_wa}"
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    if telegram_token and agente_id and agente_id != "None":
        try: requests.post(url_tg, json={"chat_id": agente_id, "text": f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar agente: {e}")
    if telegram_token and admin_id:
        try: requests.post(url_tg, json={"chat_id": admin_id, "text": f"👁️ *REPORTE ADMIN*\n👤 *Agente a cargo:* {agente.get('nombre')}\n{info_cliente}", "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Error notificar admin: {e}")

# --- FUNCIÓN IA BLINDADA ---
def consultar_ia(historial):
    url_ia = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
    
    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_PRINCIPAL, "messages": historial}, timeout=30)
        data = response.json()
        if 'choices' in data: return data['choices'][0]['message']['content']
    except Exception as e: logger.warning(f"Error principal: {e}")

    try:
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_RESPALDO, "messages": historial}, timeout=30)
        data = response.json()
        if 'choices' in data: return data['choices'][0]['message']['content']
    except Exception as e: logger.error(f"Falla total: {e}")
    
    return "Lo siento, mi sistema está experimentando una breve pausa. ¿Podrías escribirme de nuevo en un minuto? 🙏"

# --- WEBHOOK ---
@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
        
        if not mensaje_cliente.strip(): return {"replies": []}

        inventario = obtener_inventario()
        sincronizar_google_sheet()
        directorio = "\n".join([f"- {k}: WhatsApp {v}" for k, v in sheets_cache["captadores"].items()])

        # Verificamos si el sender de WhatsApp es un número real o un nombre (Ej: MiguelangelSalazar)
        sender_limpio = sender.replace("+", "").replace(" ", "")
        es_numero_puro = sender_limpio.isdigit()

        prompt_sistema = f"""
        Eres Paty, la asistente virtual de Mettryc Realty. Eres muy amigable y das respuestas cortas.
        
        INVENTARIO: {inventario}
        DIRECTORIO: {directorio}

        FLUJO PASO A PASO PARA CLIENTES:
        Paso 1: Pregunta SOLO la zona.
        Paso 2: Pregunta SOLO el presupuesto.
        Paso 3: Pregunta SOLO por detalles (habs/baños).
        Paso 4: Muestra 3 propiedades que encajen.
        Paso 5: Si le gusta alguna, dile: "¡Excelente! Para que un asesor te contacte de inmediato, confírmame por favor tu Nombre, Apellido, Correo y número de WhatsApp."
        
        ⚠️ REGLA CRÍTICA PARA EL PASO 5: Cuando pidas los datos, DETENTE. NO ESCRIBAS NADA MÁS. Espera a que el usuario te responda en su próximo mensaje con sus datos. NUNCA inventes los datos ni uses corchetes.
        
        ⚡ DISPARADOR DE ASIGNACIÓN ⚡
        Única y exclusivamente en un NUEVO mensaje, cuando ya hayas leído los datos reales que el usuario te dio, escribe al final:
        ###LEAD_CAPTURED###Nombre: [Dato Real] | Correo: [Dato Real] | Telefono: [Numero Real]###
        """
        
        if sender not in memoria_conversaciones: memoria_conversaciones[sender] = []
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        respuesta_bot = consultar_ia(historial_api)
        
        # --- BLOQUE DE SEGURIDAD EXTREMA ---
        if "###LEAD_CAPTURED###" in respuesta_bot:
            partes = respuesta_bot.split("###LEAD_CAPTURED###")
            texto_cliente = partes[0].strip()
            datos_lead_raw = partes[1].replace("###", "").strip()
            
            # 1. Filtro Anti-Alucinaciones (Rechaza plantillas literales)
            palabras_prohibidas = ["[", "]", "Su Nombre", "Su Correo", "Su WhatsApp", "Dato Real", "Numero Real"]
            
            if any(palabra in datos_lead_raw for palabra in palabras_prohibidas):
                logger.warning(f"Falso positivo bloqueado para {sender}: IA usó plantillas.")
                # Le devolvemos solo el texto donde pide los datos, borrando la etiqueta falsa
                respuesta_bot = texto_cliente
                
            elif sender not in clientes_procesados:
                # 2. Extracción segura de números
                nums = re.findall(r'\+?\d{8,15}', datos_lead_raw)
                telefono_final = nums[0] if nums else None
                
                # 3. Validación de enlace roto (Si no hay número en los datos Y el sender es un nombre)
                if not telefono_final and not es_numero_puro:
                    logger.warning(f"Enlace roto evitado. Solicitando WhatsApp real a: {sender}")
                    respuesta_bot = texto_cliente + "\n\n(Nota de sistema: Como no puedo ver tu número de teléfono automáticamente, por favor escríbelo aquí con tu código de país para poder asignarte al asesor)."
                else:
                    if not telefono_final:
                        telefono_final = sender # Si no dio número pero el sender sí lo es, lo usamos.
                        
                    agente = asignar_agente_round_robin()
                    if agente:
                        enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                        texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido asignado a tu caso y te contactará directamente a tu WhatsApp de inmediato."
                        clientes_procesados.add(sender)
                    respuesta_bot = texto_cliente
            else:
                respuesta_bot = texto_cliente
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        # Limitar memoria para no exceder tokens
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    except Exception as e:
        logger.error(f"Error crítico general: {e}", exc_info=True)
        return {"replies": [{"message": "Lo siento, estamos procesando tu solicitud. Por favor, escribe de nuevo."}]}
