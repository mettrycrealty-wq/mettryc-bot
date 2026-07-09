import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Set

import requests
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ============================================================
# CONFIGURACIÓN Y MODELOS
# ============================================================
HORARIOS_ACTUALIZACION_INVENTARIO = [0, 12]
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "google/gemini-2.5-flash-lite")
MODELO_RESPALDO_1 = os.getenv("MODELO_RESPALDO_1", "anthropic/claude-3.5-haiku")
MODELO_RESPALDO_2 = os.getenv("MODELO_RESPALDO_2", "openai/gpt-4o-mini")
MAX_TOKENS_IA = int(os.getenv("MAX_TOKENS_IA", "600"))
VENTANA_MENSAJE_DUPLICADO_SEGUNDOS = 15

# [Estructuras de datos simplificadas para eficiencia]
cache = {"inventario": [], "proxima_actualizacion": datetime.min}
estado_usuarios: Dict[str, dict] = {}
memoria_conversaciones: Dict[str, List[dict]] = {}
locks_usuarios: Dict[str, asyncio.Lock] = {}

# ============================================================
# FUNCIONES DE LÓGICA Y PROCESAMIENTO (Mismas funciones auxiliares...)
# ============================================================
# (Aquí mantienes tus funciones normalizar_texto, limpiar_telefono, etc.)

def obtener_estado_usuario(sender: str) -> dict:
    if sender not in estado_usuarios:
        estado_usuarios[sender] = {
            "estado": "diagnostico", 
            "filtros": {"operacion": "", "tipo": "", "zona": "", "precio": None, "hab": "", "caracteristicas": ""},
            "lead": {"nombre": "", "correo": "", "whatsapp": ""},
            "ultima_pregunta": ""
        }
    return estado_usuarios[sender]

# ============================================================
# LÓGICA PRINCIPAL (Webhook con Blindaje)
# ============================================================

@app.post("/webhook")
async def handle_request(request: Request):
    data = await request.json()
    payload = data.get("query") if isinstance(data.get("query"), dict) else data
    sender = str(payload.get("sender", "")).strip()
    mensaje_cliente = str(payload.get("message", "")).strip()

    lock = obtener_lock_usuario(sender)
    async with lock:
        estado = obtener_estado_usuario(sender)

        # 1. ANALIZAR (Capa 1)
        analisis = await asyncio.to_thread(analizar_mensaje_ia, mensaje_cliente, estado, [])
        
        # 2. FUSIONAR DATOS SIN PERDER NADA (Blindaje contra olvidos)
        filtros = estado["filtros"]
        if analisis.get("filtros"):
            for k, v in analisis["filtros"].items():
                if v and not filtros.get(k): filtros[k] = v # Solo llena si está vacío
        
        # 3. IDENTIFICAR EL DATO FALTANTE (El bot nunca se salta pasos)
        orden = ["operacion", "tipo", "zona", "precio", "hab", "caracteristicas"]
        dato_a_pedir = None
        for key in orden:
            if not filtros.get(key):
                dato_a_pedir = key
                break
        
        # 4. CAPTURAR LEAD (Si ya tiene todo)
        if not dato_a_pedir and estado["estado"] == "diagnostico":
            estado["estado"] = "mostrando"
            
        # 5. RESPUESTA (Capa 3)
        contexto = {
            "dato_a_pedir": dato_a_pedir,
            "filtros": filtros,
            "es_inicio": len(memoria_conversaciones.get(sender, [])) == 0
        }
        
        respuesta = await asyncio.to_thread(generar_respuesta_conversacional_paty, mensaje_cliente, contexto, [])
        return {"replies": [{"message": respuesta}]}
