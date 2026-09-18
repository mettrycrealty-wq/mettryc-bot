from __future__ import annotations

from typing import Literal, Any

from pydantic import BaseModel, ConfigDict, Field


Role = Literal["cliente", "colega_inmobiliario", "desconocido"]

Intent = Literal[
    "conversacion_casual",
    "busqueda_propiedad",
    "detalle_propiedad",
    "pregunta_propiedad",
    "seleccion_propiedad",
    "mas_propiedades",
    "captador",
    "visita",
    "atencion_humana",
    "informacion_mettryc",
    "informacion_no_disponible",
    "captura_datos",
    "unknown",
]


class TurnAnalysis(BaseModel):
    """Interpretación del turno. No ejecuta acciones de negocio."""

    model_config = ConfigDict(extra="ignore")

    role: Role = "desconocido"
    intent: Intent = "unknown"

    operation: Literal["venta", "alquiler"] | None = None
    property_type: str | None = None
    city: str | None = None
    zone: str | None = None

    min_budget: float | None = Field(default=None, ge=0)
    max_budget: float | None = Field(default=None, ge=0)
    bedrooms: int | None = Field(default=None, ge=0)
    bathrooms: int | None = Field(default=None, ge=0)
    parking: int | None = Field(default=None, ge=0)
    min_m2: float | None = Field(default=None, ge=0)
    max_m2: float | None = Field(default=None, ge=0)
    features: list[str] = Field(default_factory=list)

    property_code: str | None = None
    property_position: int | None = Field(default=None, ge=1, le=5)

    human_requested: bool = False
    information_not_available: bool = False
    unknown_information: str | None = None
    reasoning_summary: str = ""


class BusinessActionResult(BaseModel):
    """Resultado interno de una acción del motor legacy."""

    ok: bool = True
    name: str
    data: dict[str, Any] | None = None
    message: str = ""
