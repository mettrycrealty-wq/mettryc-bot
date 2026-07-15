import logging
import httpx
import os
import asyncio
import threading
import time
from fastapi import FastAPI, Request
from telegram_bot import notificar_lead, enviar_telegram
from google_sheets import asignar_agente_round_robin, sheets_cache
from ia_core import decidir_con_ia, redactar_resultado_ia
from busqueda import buscar_mejores_propiedades, buscar_por_codigo
from wasi_api import sincronizar_inventario_wasi, inventory_cache

# Configuración inicial
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mettryc-chatbot")
app = FastAPI()

# Cliente HTTP Global
http_client = httpx.AsyncClient()

# Base de datos en memoria para estados de chat
chats_db = {}

@app.on_event("startup")
async def startup():
    await sincronizar_inventario_wasi(force=True)

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    mensaje_entrante = payload.get("message", {}).get("text", "")
    chat_id = payload.get("message", {}).get("chat", {}).get("id")
    
    # Inicializar estado si es nuevo
    if chat_id not in chats_db:
        chats_db[chat_id] = {"historial": [], "filtros": {}, "lead": {}, "objetivo": "conversar"}
    
    estado = chats_db[chat_id]
    estado["ultimo_mensaje"] = mensaje_entrante
    
    # ============================================================
    # 🛠️ PANEL DE CONTROL (COMANDOS ADMINISTRADOR)
    # ============================================================
    mensaje_admin = str(mensaje_entrante).strip().lower()

    if mensaje_admin == "reiniciar chat":
        estado["historial"] = []
        estado["filtros"] = {}
        return {"status": "ok", "message": "Memoria reiniciada"}

    if mensaje_admin in ["reiniciar server", "reiniciar servidor"]:
        def kill_server():
            time.sleep(2)
            os._exit(1)
        threading.Thread(target=kill_server).start()
        return {"status": "ok", "message": "Reiniciando servidor..."}

    if mensaje_admin == "prueba google":
        agentes = len(sheets_cache.get("agentes", []))
        return {"status": "ok", "info": f"Google OK. Agentes: {agentes}"}

    if mensaje_admin == "prueba wasi":
        props = len(inventory_cache.get("inventario", []))
        return {"status": "ok", "info": f"Wasi OK. Propiedades: {props}"}

    if mensaje_admin == "prueba telegram":
        await enviar_telegram(str(chat_id), "🤖 PRUEBA DE CONEXIÓN: Bot Mettryc operativo.")
        return {"status": "ok", "info": "Prueba enviada"}

    if mensaje_admin == "prueba ia":
        return {"status": "ok", "info": "OpenRouter configurado correctamente." if os.getenv("OPENROUTER_API_KEY") else "API Key faltante"}
    
    # LÓGICA PRINCIPAL: IA -> BÚSQUEDA -> RESPUESTA
    decision = await decidir_con_ia(mensaje_entrante, estado)
    
    # Aplicar decisiones
    if decision.accion.tipo == "buscar_propiedades":
        estado["filtros"].update(decision.actualizaciones.dict(exclude_unset=True))
        propiedades, motivo = buscar_mejores_propiedades(estado, inventory_cache["inventario"])
        res = await redactar_resultado_ia(estado, len(propiedades), 0)
        
    # Guardar en historial
    estado["historial"].append({"role": "assistant", "content": decision.mensaje})
    
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "online"}