"""Encodeur du protocole YK/CUS de l'ORGSTA S002 + génération de rasters.

Format de trame (décodé depuis `libykDataPacket.so`, vérifié sur le matériel) :

    octet 0    = 0x64
    octet 1    = type de message
    octet 2    = compteur roulant global (mod 64, incrémenté à chaque trame)
    octets 3-4 = longueur du payload (uint16 little-endian)
    octets 5.. = payload
    puis        4 octets de contrôle (zéros côté hôte)
    puis        0x9b (terminateur)

Contraintes MESURÉES à respecter impérativement :
  * un payload d'image doit contenir un nombre ENTIER de lignes (multiple de 72 o) ;
  * au plus 40 lignes par trame (2 880 o) : 42 lignes sont refusées silencieusement ;
  * entre deux trames d'image, une pause > ~1 s referme la tâche et l'imprimante
    avance 48 lignes (4 mm) de blanc → viser 0 ms, ou au plus ~200 ms.
"""

from __future__ import annotations

import logging
import pathlib
import struct
from typing import Iterator

from .const import (
    BYTES_PER_LINE,
    DEFAULT_CHUNK_SIZE,
    DOTS_PER_MM,
    FEED_UNITS_PER_MM,
    FRAME_END,
    FRAME_START,
    MAX_LINES_PER_FRAME,
    MSG_FEED,
    MSG_IMAGE_SLICE,
    MSG_PAPER_SIZE,
    MSG_TOKEN,
    PRINT_WIDTH_DOTS,
)

_LOGGER = logging.getLogger(__name__)

# Police du texte imprimé : EMBARQUÉE dans l'intégration, parce que rien ne garantit ce qui est
# installé sur la machine qui fait tourner Home Assistant. DejaVu Sans Condensed Bold est grasse
# (des fûts de 2 à 3 points au lieu d'1 seul) et condensée pour ne pas trop perdre de caractères
# par ligne. Mesures à l'échelle d'impression 2 : 5,0 % de points noirs contre 3,1 % avec la police
# par défaut de Pillow, pour 40 caractères par ligne au lieu de 45. La police par défaut ne sait
# pas écrire les accents (ils sortaient en carré vide) ; celle-ci les gère tous.
_POLICE_EMBARQUEE = pathlib.Path(__file__).parent / "fonts" / "DejaVuSansCondensed-Bold.ttf"
# Repli si le fichier venait à manquer (dépôt incomplet) : polices grasses du système.
_POLICES_SYSTEME = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _police_texte(taille: int):
    """Renvoie la police grasse du texte imprimé.

    Ordre de recherche : la police embarquée, puis les polices grasses du système, puis la police
    par défaut de Pillow — un texte plus clair vaut mieux qu'une impression qui échoue.
    """
    from PIL import ImageFont  # import tardif : Pillow n'est requis que pour dessiner

    for chemin in (_POLICE_EMBARQUEE, *_POLICES_SYSTEME):
        try:
            return ImageFont.truetype(str(chemin), taille)
        except (OSError, ValueError):
            continue
    _LOGGER.warning(
        "S002 : police grasse introuvable — repli sur la police par défaut, le texte sera plus clair"
    )
    return ImageFont.load_default(size=taille)


# --------------------------------------------------------------------------------------
# Trames
# --------------------------------------------------------------------------------------
def build_frame(msg_type: int, payload: bytes = b"", counter: int = 0) -> bytes:
    """Construit une trame YK complète."""
    if len(payload) > 0xFFFF:
        raise ValueError(f"payload trop long : {len(payload)} octets")
    return (
        bytes([FRAME_START, msg_type, counter & 0x3F])
        + len(payload).to_bytes(2, "little")
        + payload
        + b"\x00\x00\x00\x00"
        + bytes([FRAME_END])
    )


def frame_token(counter: int = 0) -> bytes:
    """Trame d'ouverture de session (`cusGetBleTokenBytes`), payload 0x01."""
    return build_frame(MSG_TOKEN, b"\x01", counter)


def frame_paper_size(width_dots: int = PRINT_WIDTH_DOTS, counter: int = 0) -> bytes:
    """Déclare la largeur (l'imprimante en déduit le nombre d'octets par ligne)."""
    return build_frame(MSG_PAPER_SIZE, struct.pack("<H", width_dots), counter)


def frame_feed(mm: float, counter: int = 0) -> bytes:
    """Avance papier : 50 unités ≈ 5 mm, soit ≈ 0,1 mm par unité (uint16 LE)."""
    units = int(round(mm * FEED_UNITS_PER_MM))
    if not 0 < units <= 0xFFFF:
        raise ValueError(f"avance hors bornes : {mm} mm")
    return build_frame(MSG_FEED, struct.pack("<H", units), counter)


def iter_image_frames(
    raster: bytes,
    lines_per_frame: int = MAX_LINES_PER_FRAME,
    start_counter: int = 0,
) -> Iterator[tuple[bytes, int]]:
    """Découpe un raster en trames d'image successives.

    Rend `(trame, compteur_utilisé)` ; le compteur est global à la session et
    s'incrémente pour CHAQUE trame émise (y compris token/taille/avance).
    """
    if lines_per_frame < 1 or lines_per_frame > MAX_LINES_PER_FRAME:
        raise ValueError(
            f"lines_per_frame doit être entre 1 et {MAX_LINES_PER_FRAME} "
            f"(limite de trame mesurée)"
        )
    lines = split_lines(raster)
    counter = start_counter & 0x3F
    for i in range(0, len(lines), lines_per_frame):
        payload = b"".join(lines[i : i + lines_per_frame])
        yield build_frame(MSG_IMAGE_SLICE, payload, counter), counter
        counter = (counter + 1) & 0x3F


# --------------------------------------------------------------------------------------
# Outils raster
# --------------------------------------------------------------------------------------
def split_lines(raster: bytes, line_bytes: int = BYTES_PER_LINE) -> list[bytes]:
    """Découpe le raster en lignes de 72 octets, en complétant la dernière."""
    if len(raster) % line_bytes:
        raster = raster.ljust((len(raster) // line_bytes + 1) * line_bytes, b"\x00")
    return [raster[i : i + line_bytes] for i in range(0, len(raster), line_bytes)]


def blank_line(line_bytes: int = BYTES_PER_LINE) -> bytes:
    return b"\x00" * line_bytes


def solid_line(line_bytes: int = BYTES_PER_LINE, black: bool = True) -> bytes:
    return (b"\xff" if black else b"\x00") * line_bytes


def raster_stats(raster: bytes) -> dict:
    """Dimensions réelles d'un raster (utiles pour les logs et la prévisualisation)."""
    lines = len(raster) // BYTES_PER_LINE
    return {
        "lines": lines,
        "height_mm": round(lines / DOTS_PER_MM, 2),
        "width_mm": round(PRINT_WIDTH_DOTS / DOTS_PER_MM, 2),
    }


def bar_line(
    start_dot: int,
    width_dots: int,
    line_bytes: int = BYTES_PER_LINE,
) -> bytes:
    """Une ligne contenant une barre verticale noire [start_dot, start_dot+width_dots[."""
    line = bytearray(line_bytes)
    for dot in range(start_dot, min(start_dot + width_dots, line_bytes * 8)):
        if 0 <= dot:
            line[dot // 8] |= 0x80 >> (dot % 8)
    return bytes(line)


# --------------------------------------------------------------------------------------
# Motifs (tests et diagnostics)
# --------------------------------------------------------------------------------------
def stripes_raster(count: int, height: int = 24) -> bytes:
    """`count` barres verticales régulières — sert de règle visuelle sur le papier."""
    if count < 1:
        raise ValueError("count doit être >= 1")
    step = BYTES_PER_LINE // (count + 1)
    line = bytearray(BYTES_PER_LINE)
    for i in range(1, count + 1):
        for dot in range(i * step, i * step + 6):
            line[dot // 8] |= 0x80 >> (dot % 8)
    return bytes(line) * height


def test_pattern_raster() -> bytes:
    """Motif de validation : repère de début, 6 bandes distinctes, repère de fin."""
    blocks = [
        (solid_line(), 24),                                  # début : 2 mm plein
        (bar_line(280, 16), 12),                             # barre fine au centre
        (blank_line(), 12),
        (bar_line(0, 288), 12),                              # moitié gauche
        (bar_line(288, 288), 12),                            # moitié droite
        (bytes([0xAA] * BYTES_PER_LINE), 12),                # damier
        (bytes([0xF0] * 9 * 8), 12),                         # stries
        (bar_line(216, 144), 12),                            # carré central
        (blank_line(), 12),
        (solid_line(), 48),                                  # fin : 4 mm plein
    ]
    return b"".join(line * n for line, n in blocks)


# --------------------------------------------------------------------------------------
# Rendu de texte (Pillow, fourni par Home Assistant — aucune dépendance externe)
# --------------------------------------------------------------------------------------
def text_raster(
    lines: list[str],
    scale: int = 2,
    padding_dots: int = 8,
    invert: bool = False,
    wrap: bool = True,
) -> bytes:
    """Rasterise du texte en 1 bit (1 = noir).

    Utilise la police grasse embarquée (voir `_police_texte`) : le texte était trop clair, à
    cause des fûts très fins de la police par défaut de Pillow — laquelle ne sait en plus pas
    écrire les accents, qui sortaient en carré vide. `scale` multiplie la taille par un entier
    (rendu net, pas d'interpolation).

    `wrap=True` replie les lignes trop larges sur les espaces : sans cela, le texte qui
    dépasse la largeur d'impression est **tronqué en silence** (défaut constaté).
    """
    from PIL import Image, ImageDraw, ImageFont  # import tardif : Pillow n'est requis qu'ici

    font = _police_texte(11 * max(1, scale))
    probe = Image.new("1", (PRINT_WIDTH_DOTS, 8), 1)
    probe_draw = ImageDraw.Draw(probe)
    line_height = max(1, probe_draw.textbbox((0, 0), "Ag", font=font)[3]) + 4

    largeur_utile = max(8, PRINT_WIDTH_DOTS - 2 * padding_dots)
    if wrap:
        repliees: list[str] = []
        for brute in lines:
            if not brute.strip() or probe_draw.textlength(brute, font=font) <= largeur_utile:
                repliees.append(brute)
                continue
            courant = ""
            for mot in brute.split():
                essai = f"{courant} {mot}".strip()
                if courant and probe_draw.textlength(essai, font=font) > largeur_utile:
                    repliees.append(courant)
                    courant = mot
                else:
                    courant = essai
            repliees.append(courant)
        lines = repliees

    height = padding_dots * 2 + line_height * max(1, len(lines))

    img = Image.new("1", (PRINT_WIDTH_DOTS, height), 1)   # 1 = blanc dans Pillow
    draw = ImageDraw.Draw(img)
    y = padding_dots
    for text in lines:
        draw.text((padding_dots, y), text, font=font, fill=0)
        y += line_height

    if invert:
        img = img.point(lambda p: 0 if p else 1)

    # Pillow : 0 = noir, 1 = blanc sur une image "1" ; le raster YK veut 1 = noir.
    return bytes(255 - b for b in img.tobytes())


def image_to_raster(
    donnees: bytes,
    dither: bool = False,
    invert: bool = False,
    width_dots: int = PRINT_WIDTH_DOTS,
) -> bytes:
    """Convertit une image (PNG/JPEG/…) en raster 1 bit pour l'imprimante.

    `dither=False` : seuillage franc (net, adapté au texte et aux traits).
    `dither=True`  : tramage Floyd-Steinberg (utile pour les photos).
    L'image est mise à l'échelle de la largeur d'impression (576 points) en conservant
    les proportions. Sortie : même format que l'imprimante (1 = noir, MSB d'abord).
    """
    import io

    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(donnees)) as source:
        img = source.convert("L")
        if img.width != width_dots:
            hauteur = max(1, round(img.height * width_dots / img.width))
            img = img.resize((width_dots, hauteur), Image.LANCZOS)
        if invert:
            img = ImageOps.invert(img)
        img = img.convert("1") if dither else img.point(
            lambda p: 255 if p > 128 else 0
        ).convert("1")
        # Pillow : bit à 1 = blanc → on inverse pour obtenir 1 = noir (format YK).
        return bytes(255 - b for b in img.tobytes())


def chunks(data: bytes, size: int = DEFAULT_CHUNK_SIZE) -> list[bytes]:
    """Découpe une trame en paquets de taille d'écriture BLE.

    ⚠️ `int()` obligatoire : les sélecteurs numériques de HA renvoient des **flottants**
    (200.0), et `range()` les refuse — `TypeError: 'float' object cannot be interpreted
    as an integer`. Vécu le 28/09 lors du premier appel de service.
    """
    size = int(size)
    if size < 1:
        raise ValueError("taille de paquet invalide")
    return [data[i : i + size] for i in range(0, len(data), size)]
