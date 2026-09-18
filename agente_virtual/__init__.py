"""Agente virtual conversacional de Mettryc Realty.

La capa entiende conversaciones naturales y utiliza el chatbot existente como
motor de negocio. No contiene un segundo cliente WASI ni un segundo CRM.
"""

from .engine import AgenteVirtualEngine
from .service import AgenteVirtualService

__all__ = ["AgenteVirtualEngine", "AgenteVirtualService"]
