import os
import requests
import logging
import time
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

agentes_cache = {
    "lista": [],
    "ultimo_indice": -1,
    "ultima_actualizacion": datetime.min
}

memoria_conversaciones = {}
clientes_procesados = set() 
MODELO_OPENROUTER = "deepseek/deepseek-chat"

def obtener_inventario_desde_wasi():
    propiedades_limpias = []
    take = 100
    skip = 0
    max_propiedades = 2000
    
    logger.info("Iniciando descarga completa del inventario desde Wasi...")
    
    while True:
        url = f"https://api.wasi.co/v1/property/search?wasi_token={os.getenv('WASI_TOKEN')}&id_company={os.getenv('WASI_COMPANY_ID')}&take={take}&skip={skip}"
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
                
            except Exception as e:
                intentos += 1
                time.sleep(5)
        
        if not exito_pagina:
            break
        if skip >= max_propiedades:
            break
            
    return "\n".join(propiedades_limpias)

def obtener_inventario():
    if datetime.now() - cache["ultima_actualizacion"] > timedelta(hours=24) or not cache["inventario_texto"]:
        inventario_nuevo = obtener_inventario_desde_wasi()
        if inventario_nuevo: 
            cache["inventario_texto"] = inventario_nuevo
            cache["ultima_actualizacion"] = datetime.now()
    return cache["inventario_texto"]

def obtener_agentes_desde_sheet():
    script_url = os.getenv("GOOGLE_SHEET_TURNOS_URL") 
    if not script_url:
        return agentes_cache["lista"]

    if datetime.now() - agentes_cache["ultima_actualizacion"] > timedelta(hours=1) or not agentes_cache["lista"]:
        try:
            response = requests.get(script_url, timeout=15)
            lista_nueva = response.json()
            if isinstance(lista_nueva, list) and len(lista_nueva) > 0:
                agentes_cache["lista"] = lista_nueva
                agentes_cache["ultima_actualizacion"] = datetime.now()
        except Exception as e:
            logger.error(f"Error cargando agentes: {e}")
            
    return agentes_cache["lista"]

def asignar_agente_round_robin():
    lista_agentes = obtener_agentes_desde_sheet()
    if not lista_agentes:
        return None
        
    agentes_cache["ultimo_indice"] += 1
    if agentes_cache["ultimo_indice"] >= len(lista_agentes):
        agentes_cache["ultimo_indice"] = 0
        
    return lista_agentes[agentes_cache["ultimo_indice"]]

def enviar_notificaciones_telegram(agente, telefono_destino, datos_lead):
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_id = os.getenv("TELEGRAM_ADMIN_ID")
    agente_id = agente.get("telegram_id")
    
    link_wa = f"https://wa.me/{telefono_destino}"
    info_cliente = f"\n\n*Datos del Cliente:*\n{datos_lead}\n\n📲 *Contactar de inmediato:* {link_wa}"
    
    url_tg = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    
    if telegram_token and agente_id:
        try:
            msg_agente = f"👤 *¡Tienes un nuevo cliente asignado!* \n{info_cliente}"
            requests.post(url_tg, json={"chat_id": agente_id, "text": msg_agente, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            logger.error(f"Error enviando Telegram al agente: {e}")
            
    if telegram_token and admin_id:
        try:
            msg_admin = f"👁️ *REPORTE DE SEGUIMIENTO ADMIN*\n👤 *Agente a cargo:* {agente['nombre']}\n{info_cliente}"
            requests.post(url_tg, json={"chat_id": admin_id, "text": msg_admin, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            logger.error(f"Error enviando Telegram al admin: {e}")

@app.post("/webhook")
async def handle_request(request: Request):
    try:
        data = await request.json()
        if request.headers.get("x-api-key") not in os.getenv("API_KEYS_AGENTES", "").split(","):
            raise HTTPException(status_code=403, detail="Acceso denegado")

        payload = data.get("query") if isinstance(data.get("query"), dict) else data
        sender = str(payload.get("sender", "")).strip()
        mensaje_cliente = str(payload.get("message", ""))
            
        if not mensaje_cliente.strip():
            return {"replies": []}

        inventario = obtener_inventario()
        
        if sender not in memoria_conversaciones:
            memoria_conversaciones[sender] = []
        
        es_numero_puro = sender.replace("+", "").replace(" ", "").isdigit()
        
        if es_numero_puro:
            requisitos_lead = "su Nombre Completo y su Correo Electrónico"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Interés: [Inmueble buscado]###"
        else:
            requisitos_lead = "su Nombre Completo, su Correo Electrónico y que te confirme OBLIGATORIAMENTE su Número de WhatsApp (con su código de país, ej: +58...)"
            etiqueta_lead = "###LEAD_CAPTURED###Nombre: [Valor real] | Correo: [Valor real] | Telefono: [Número Confirmado] | Interés: [Inmueble buscado]###"

        prompt_sistema = f"""
        Eres un Broker Inmobiliario experto de Mettryc Realty.
        INVENTARIO DISPONIBLE DE LA EMPRESA:
        {inventario}
        
        REGLAS DE ATENCIÓN:
        1. Al inicio de la conversación y durante las consultas, sé amable, muestra las opciones y responde de forma breve. Siempre proporciona el enlace crudo de la propiedad sin modificaciones. NO pidas ningún dato de entrada.
        2. Mantén un flujo de venta natural.
        
        ESTRATEGIA DE ASIGNACIÓN (SÚPER CRÍTICA):
        Solo cuando el cliente decida avanzar (visita o detalles específicos), pídele: {requisitos_lead}.
        
        ESPERA A QUE EL CLIENTE RESPONDA CON LOS DATOS REALES.
        
        ÚNICAMENTE CUANDO EL CLIENTE YA TE HAYA DADO SUS DATOS REALES (NO ANTES), escribe esta estructura exacta al final de tu mensaje:
        {etiqueta_lead}
        
        REGLA ANTI-ERRORES: Si el remitente es un nombre guardado, NO puedes generar la etiqueta ###LEAD_CAPTURED### hasta que el cliente escriba explícitamente su número de teléfono con dígitos en el chat.
        """
        
        historial_api = [{"role": "system", "content": prompt_sistema}] + memoria_conversaciones[sender] + [{"role": "user", "content": mensaje_cliente}]
        
        url_ia = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}", "Content-Type": "application/json"}
        response = requests.post(url_ia, headers=headers, json={"model": MODELO_OPENROUTER, "messages": historial_api}, timeout=30)
        respuesta_bot = response.json()['choices'][0]['message']['content']
        
        if "###LEAD_CAPTURED###" in respuesta_bot:
            # 1er Escudo: Evitar textos genéricos o corchetes alucinados
            if "[Valor real]" in respuesta_bot or "[Número Confirmado]" in respuesta_bot or "[Nombre]" in respuesta_bot:
                logger.warning(f"La IA intentó disparar un falso positivo para {sender}. Ignorando etiqueta.")
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
            
            elif sender not in clientes_procesados:
                try:
                    partes = respuesta_bot.split("###LEAD_CAPTURED###")
                    texto_cliente = partes[0].strip()
                    datos_lead_raw = partes[1].replace("###", "").strip()
                    
                    telefono_final = sender
                    numero_encontrado_en_texto = False
                    
                    # Intentamos extraer el número escrito por el cliente
                    if "Telefono:" in datos_lead_raw:
                        try:
                            sub_partes = datos_lead_raw.split("|")
                            for parte in sub_partes:
                                if "Telefono:" in parte:
                                    num_extraido = parte.split(":")[1].strip()
                                    num_limpio = "".join([c for c in num_extraido if c.isdigit() or c == "+"])
                                    if any(c.isdigit() for c in num_limpio):
                                        telefono_final = num_limpio
                                        numero_encontrado_en_texto = True
                        except Exception:
                            pass

                    # CANDADO ESTRICTO: Si el contacto está guardado con nombre, OBLIGATORIAMENTE necesitamos haber extraído números reales
                    if not es_numero_puro and not numero_encontrado_en_texto:
                        logger.warning(f"La IA intentó cerrar el lead para el contacto guardado '{sender}', pero NO recolectó el número telefónico real. Forzando solicitud.")
                        respuesta_bot = "¡Excelente! Ya tengo tu nombre y correo registrados para asignarte un asesor inmobiliario. Solo me faltaría que me confirmes, por favor, tu número de WhatsApp actual (con el código de tu país) para que el especialista asignado pueda abrir tu ficha y escribirte de inmediato."
                    else:
                        # Si todo está en orden, asignamos agente
                        agente = asignar_agente_round_robin()
                        if agente:
                            enviar_notificaciones_telegram(agente, telefono_final, datos_lead_raw)
                            texto_cliente += f"\n\n¡Perfecto! He registrado tus datos. Nuestro asesor especializado, *{agente['nombre']}*, ha sido asignado a tu caso y te contactará directamente a tu WhatsApp de inmediato."
                            clientes_procesados.add(sender)
                        respuesta_bot = texto_cliente
                        
                except Exception as e:
                    logger.error(f"Error procesando captura: {e}")
            else:
                respuesta_bot = respuesta_bot.split("###LEAD_CAPTURED###")[0].strip()
        
        memoria_conversaciones[sender].append({"role": "user", "content": mensaje_cliente})
        memoria_conversaciones[sender].append({"role": "assistant", "content": respuesta_bot})
        
        if len(memoria_conversaciones[sender]) > 20:
            memoria_conversaciones[sender] = memoria_conversaciones[sender][-20:]
            
        return {"replies": [{"message": respuesta_bot}]}
    
    except Exception as e:
        logger.error(f"Error general en el webhook: {e}")
        return {"replies": [{"message": "Estamos procesando tu solicitud, por favor escribe de nuevo."}]}
