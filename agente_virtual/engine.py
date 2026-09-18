from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from .bridge import LegacyMettrycBridge
from .router import AgentModelRouter
from .schemas import BusinessActionResult, TurnAnalysis


ANALYSIS_PROMPT = """
Eres el cerebro de un agente virtual de Mettryc Realty.

Interpreta el último mensaje dentro de toda la conversación y del estado comercial
actual. Tu trabajo es decidir qué quiere la persona y qué datos nuevos aportó.

Este agente NO funciona como un menú ni como un cuestionario rígido. Una persona
puede cambiar de tema, conversar casualmente, volver a hablar de una propiedad,