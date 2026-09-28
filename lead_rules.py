"""Requisitos observables de contacto para un prospecto de Mettryc."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


_BLOCKED = {
    "hola", "buenas", "gracias", "apartamento", "casa", "quiero",
    "visitar", "opcion", "cliente", "busco", "buscar", "necesito",
    "propiedad", "informacion", "si", "no", "dale", "perfecto",
    "correo", "whatsapp", "numero", "telefono", "agente", "asesor",
}


def _fold(word: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", word.lower())
                   if unicodedata.category(ch) != "Mn")


def nombre_prospecto_valido(value: Any) -> bool:
    """Acepta un nombre de pila o varios nombres, sin aceptar saludos."""
    name = str(value or "").strip()
    words = name.split()
    return bool(
        1 <= len(words) <= 6
        and all(2 <= len(word) <= 40 and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", word)
                and _fold(word) not in _BLOCKED for word in words)
    )


def contacto_prospecto_completo(lead: dict) -> bool:
    phone = re.sub(r"\D", "", str(lead.get("whatsapp") or ""))
    return bool(nombre_prospecto_valido(lead.get("nombre"))
                and 10 <= len(phone) <= 15 and lead.get("whatsapp_confirmado"))
