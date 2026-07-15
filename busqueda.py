import logging
import re
from copy import deepcopy
from typing import List, Optional, Tuple

# Importamos las herramientas que extrajimos previamente a utils.py
from utils import (
    convertir_float,
    convertir_entero,
    normalizar_tipo_propiedad,
    normalizar_texto,
    zona_coincide,
    formato_moneda
)

# Configuramos el logger para este archivo
logger = logging.getLogger("mettryc-chatbot")

# ============================================================
# RANKING Y EVALUACIÓN ESTRICTA
# ============================================================

def obtener_precio(propiedad: dict, operacion: str) -> float:
    if operacion == "alquiler":
        return convertir_float(propiedad.get("precio_renta_float"))
    return convertir_float(propiedad.get("precio_venta_float"))


def coincide_tipo(propiedad: dict, tipo_buscado: str) -> bool:
    if not tipo_buscado: return True
    buscado = normalizar_tipo_propiedad(tipo_buscado)
    tipo_wasi = normalizar_tipo_propiedad(propiedad.get("tipo_propiedad_wasi", ""))
    titulo = normalizar_texto(propiedad.get("titulo", ""))

    if buscado == "casa":
        aceptados = {"casa", "quinta", "townhouse", "apartoquinta"}
        return any(tipo in tipo_wasi or tipo in titulo for tipo in aceptados)
    if buscado == "apartamento":
        return any(tipo in tipo_wasi or tipo in titulo for tipo in ["apartamento", "penthouse"])

    return buscado in tipo_wasi or buscado in titulo


def evaluar_propiedad_estricta(original: dict, filtros: dict) -> Tuple[bool, str, dict]:
    propiedad = deepcopy(original)
    
    operacion = filtros.get("tipo_operacion")
    tipo = filtros.get("tipo_propiedad")
    zona = filtros.get("zona")
    presupuesto = filtros.get("presupuesto_max")
    habs_req = filtros.get("habitaciones_min")
    banos_req = filtros.get("banos_min")
    garajes_req = filtros.get("garajes_min")
    caracteristicas = filtros.get("caracteristicas", [])

    precio = obtener_precio(propiedad, operacion) if operacion else obtener_precio(propiedad, "venta")
    
    # 1. 🛡️ ELIMINATORIO: Operación y Precio
    if operacion and precio <= 0:
        return False, "rechazada_operacion", propiedad

    # 2. 🛡️ ELIMINATORIO: Tipo de Inmueble
    if tipo and not coincide_tipo(propiedad, tipo):
        return False, "rechazada_tipo", propiedad

    # 3. 🛡️ ELIMINATORIO: Zona
    if zona and not zona_coincide(zona, propiedad.get("zona", ""), propiedad.get("ciudad", "")):
        return False, "rechazada_zona", propiedad

    es_exacta = True
    diferencias = []

    # 4. 🛡️ TOLERANCIA 20%: Presupuesto
    if presupuesto and precio > 0:
        if precio > presupuesto:
            if precio <= presupuesto * 1.20:
                es_exacta = False
                diferencias.append(f"Inversión {formato_moneda(precio)}")
            else:
                return False, "rechazada_precio", propiedad

    # 5. 🛡️ TOLERANCIA +/- 1: Espacios
    for val_req, key_prop, label in [(habs_req, "habitaciones", "habs"), (banos_req, "banos", "baños"), (garajes_req, "garajes", "puestos")]:
        if val_req is not None and val_req > 0:
            val_prop = convertir_entero(propiedad.get(key_prop))
            if val_prop < val_req - 1 or val_prop > val_req + 1:
                return False, f"rechazada_{key_prop}", propiedad
            elif val_prop != val_req:
                es_exacta = False
                diferencias.append(f"{val_prop} {label}")

    # 6. 🛡️ RECONOCIMIENTO DE PALABRAS CLAVE
    texto_propiedad = normalizar_texto(" ".join([str(propiedad.get("titulo", "")), str(propiedad.get("descripcion", "")), str(propiedad.get("caracteristicas_texto", ""))]))
    for car in caracteristicas:
        if normalizar_texto(car) not in texto_propiedad:
            es_exacta = False
            diferencias.append(f"No especifica '{car}'")

    propiedad["_coincidencia"] = "exacta" if es_exacta else "aproximada"
    propiedad["_diferencias"] = diferencias
    propiedad["operacion_buscada"] = operacion

    return True, ("exacta" if es_exacta else "tolerancia"), propiedad


def buscar_mejores_propiedades(estado: dict, inventario: List[dict], cantidad: int = 3) -> Tuple[List[dict], str]:
    excluir = {str(pid) for pid in estado.get("propiedades_enviadas", [])}
    exactas = []
    tolerancia = []
    pasaron_zona_tipo = 0
    
    stats = {"rechazada_operacion": 0, "rechazada_tipo": 0, "rechazada_zona": 0, "rechazada_precio": 0, "rechazada_habitaciones": 0, "rechazada_banos": 0, "rechazada_garajes": 0}
    
    for original in inventario:
        if str(original.get("id", "")) in excluir:
            continue
            
        is_match, category, prop = evaluar_propiedad_estricta(original, estado["filtros"])
        
        if is_match:
            if category == "exacta": 
                exactas.append(prop)
            else: 
                tolerancia.append(prop)
        else:
            stats[category] = stats.get(category, 0) + 1
            if category in ["rechazada_precio", "rechazada_habitaciones", "rechazada_banos", "rechazada_garajes"]:
                pasaron_zona_tipo += 1

    op = estado["filtros"].get("tipo_operacion", "venta")
    exactas = sorted(exactas, key=lambda p: obtener_precio(p, op))
    tolerancia = sorted(tolerancia, key=lambda p: obtener_precio(p, op))
    
    resultado = exactas[:cantidad]
    if len(resultado) < cantidad:
        resultado.extend(tolerancia[:cantidad - len(resultado)])
        
    motivo_falla = "precio_o_caracteristicas" if not resultado and pasaron_zona_tipo > 0 else "zona_o_tipo"
    
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Evaluadas: {len(inventario)}")
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Descartadas -> Zona: {stats.get('rechazada_zona',0)} | Tipo: {stats.get('rechazada_tipo',0)} | Operación: {stats.get('rechazada_operacion',0)} | Precio: {stats.get('rechazada_precio',0)}")
    logger.info(f"📊 [MOTOR DE BÚSQUEDA] Encontradas -> Exactas: {len(exactas)} | Tolerancia: {len(tolerancia)}")
    
    return resultado, motivo_falla


def buscar_por_codigo(codigo: str, inventario: List[dict]) -> Optional[dict]:
    codigo_limpio = re.sub(r"\D", "", str(codigo or ""))
    if not codigo_limpio:
        return None
    for propiedad in inventario:
        if str(propiedad.get("id", "")) == codigo_limpio:
            return deepcopy(propiedad)
    return None