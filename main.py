import os
import requests
from fastapi import FastAPI, Header, HTTPException
from google.generativeai import GenerativeModel, configure

app = FastAPI()

# Configuración de claves (obtenidas de las variables de entorno de Render)
API_KEYS_AGENTES = os.getenv("API_KEYS_AGENTES", "clave123").split(",")
WASI_TOKEN = os.getenv("WASI_TOKEN")
configure(api_key=os.getenv("GEMINI_API_KEY"))
model = GenerativeModel("gemini-1.5-flash")

@app.post("/webhook")
async def handle_request(data: dict, x_api_key: str = Header(None)):
    if x_api_key not in API_KEYS_AGENTES:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    mensaje_cliente = data.get("query", "")
    
    # Consulta a Wasi (Simulada para estructura)
    # Aquí iría tu lógica real de conexión a la API de Wasi
    inventario = "Propiedades: Casa en Valencia 300k, Apartamento en Barquisimeto 150k"
    
    # IA Procesando
    prompt = f"Eres un broker premium de Mettryc Realty. Inventario: {inventario}. Cliente: {mensaje_cliente}"
    response = model.generate_content(prompt)
    
    return {"replies": [response.text]}