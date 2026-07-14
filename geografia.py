# geografia.py
# Diccionario geográfico oficial de Mettryc Realty (Extraído de Excel)

DICCIONARIO_GEOGRAFICO = {
    'distrito metropolitano': {
        'caracas': ['los naranjos del cafetal', 'la florida', 'prados del este', 'colinas de bello monte', 'altamira', 'el rosal', 'chacao', 'los palos grandes', 'la castellana', 'el marques', 'mac[...]
    },
    'miranda': {
        'higuerote': ['colinas de tacarigua', 'puerto frances', 'carenero', 'chirimena', 'los totumos', 'san jorge'],
        'guarenas': ['chalet ville', 'nueva casarapa', 'el torreón', 'la guairita', 'terrazas del este', 'villa panamericana', 'ciudad casarapa'],
        'guatire': ['valle arriba', 'castillejo', 'el marques', 'la rosa', 'las rosas', 'el encantado', 'buenaventura', 'parque habitat', 'villa heroica'],
        'san antonio de los altos': ['los castores', 'el picacho', 'las minas', 'rosaleda', 'la morita', 'el amarillo'],
        'los teques': ['el tambor', 'el trigo', 'lagunetica', 'la matica', 'san pedro', 'el paso'],
        'charallave': ['ciudad miranda', 'madosa', 'los samanes', 'la estrella', 'paso real'],
        'cua': ['aparay', 'lecheria', 'nueva cua', 'san antonio de cua'],
        'santa teresa del tuy': ['ciudad losada', 'el palmar', 'las carolinas'],
        'ocotillo': ['centro'],
        'rio chico': ['san jose de barlovento', 'el guapo', 'cupira'],
    },
    'carabobo': {
        'naguanagua': ['el rincon', 'mañongo', 'la granja', 'las quintas', 'tazajal', 'el roble', 'guaparo', 'caprenco', 'bárbula', 'la campiña', 'carialinda'],
        'valencia': ['agua blanca', 'altos de guataparo', 'avenida bolivar norte', 'avenida lara', 'camoruco', 'carabobo', 'centro', 'chimenea', 'el bosque', 'el parral', 'el trigal', 'el viñedo'[...]
        'los guayos': ['buenaventura', 'paraparal', 'las aguitas', 'el roble', 'ciudad alianza', 'los cerritos'],
        'guacara': ['ciudad alianza', 'el samán', 'la pradera', 'loma linda', 'yagua', 'vigirima', 'centro'],
        'san joaquin': ['la pradera', 'el remanso', 'villa oasis', 'centro'],
        'san diego': ['el remanso', 'los jarales', 'la esmeralda', 'pueblo de san diego', 'tulipan', 'valle de oro', 'monteserino', 'morro I', 'morro II', 'paso real', 'ciudad flamengo'],
        'puerto cabello': ['rancho grande', 'santa cruz', 'cumboto', 'san esteban', 'el portuario', 'centro', 'borburata', 'patanebo'],
        'libertador': ['tocuyito', 'safari', 'la honda', 'el rincón'],
        'diego ibarra': ['mariara', 'aguas calientes', 'san joaquín'],
        'montalban': ['centro', 'el peñon', 'aguirre'],
        'miguel pena': ['lomas de funval', 'trapichito', 'ruiz pineda', 'el palotal'],
        'carlos arvelo': ['guigue', 'belen', 'tacarigua'],
    },
    'lara': {
        'barquisimeto': ['el este', 'centro', 'oeste', 'fundalara', 'santa elena', 'el parral', 'colinas de santa rosa', 'la rosaleda', 'los leones', 'nueva segovia', 'patarata', 'pueblo nuevo', '[...]
        'cabudare': ['la mora', 'valle hondo', 'el recreo', 'las mercedes', 'los rastrojos', 'agua viva', 'centro', 'el palmar'],
        'quibor': ['centro', 'la ermita', 'san rafael'],
        'el tocuyo': ['centro', 'la concordia', 'los hornos'],
        'carora': ['centro', 'torres', 'el roble'],
    },
    'aragua': {
        'maracay': ['base aragua', 'calicanto', 'carmen julia', 'centro', 'corinsa', 'delicias', 'el bosque', 'el castaño', 'el centro', 'el limon', 'el milagro', 'el piñal', 'el toro', 'fundaca[...]
        'cagua': ['centro', 'corinsa', 'fundacagua', 'la providencia', 'santa rosalia'],
        'turmero': ['centro', 'la julia', 'san mateo', 'valle lindo'],
        'el limon': ['centro', 'el piñal', 'el toro'],
        'la victoria': ['centro', 'las mercedes', 'san jose'],
    }
}

# ------------------------------------------------------------------
# Compatibility layer: allow overriding the embedded dictionary with
# data/geografia.json placed in the repository's data/ folder. This
# keeps backwards compatibility while enabling editing the geo data
# as JSON.
# ------------------------------------------------------------------
import json
from pathlib import Path
import logging

_data_path = Path(__file__).parent / "data" / "geografia.json"

if _data_path.exists():
    try:
        with _data_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                DICCIONARIO_GEOGRAFICO = loaded
            else:
                logging.getLogger(__name__).warning(
                    "data/geografia.json does not contain a JSON object; keeping embedded DICCIONARIO_GEOGRAFICO"
                )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to load data/geografia.json: %s", exc
        )
