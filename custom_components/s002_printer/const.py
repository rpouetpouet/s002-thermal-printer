"""Constantes du protocole YK/CUS de l'imprimante thermique ORGSTA S002.

Toutes les valeurs proviennent de mesures réelles sur le matériel (voir le skill
`orgsta-s002-printer`) et non d'une documentation constructeur.
"""

DOMAIN = "s002_printer"
NAME = "ORGSTA S002 Thermal Printer"
VERSION = "0.1.0"

# --- Transport BLE -------------------------------------------------------------------
# L'imprimante expose le service ff00 en DEUX exemplaires (appareil multi-link) ;
# l'écriture utile est la seconde occurrence (handle le plus haut, `service000c` sous BlueZ).
SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_STATE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_FLOW_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

# Taille d'un paquet d'écriture. La MTU négociée est de 240 octets (annoncée par
# l'imprimante), donc 237 utiles au maximum ; on reste à 200 comme sur le banc d'essai.
DEFAULT_CHUNK_SIZE = 200
# Fenêtre de contrôle de flux : l'imprimante acquitte (`01 05`) tous les 5 paquets.
FLOW_WINDOW = 5
FLOW_TIMEOUT_MS = 900

# --- Géométrie (mesurée : cadre à 2 mm du bord gauche, 49 mm de large, 200 lignes = 16 mm) ---
PRINT_WIDTH_DOTS = 576          # 576 points = 48,8 mm ≈ 49 mm mesurés
BYTES_PER_LINE = PRINT_WIDTH_DOTS // 8   # 72 octets par ligne
DOTS_PER_MM = 11.81             # 300 dpi
# Limite de trame mesurée : 40 lignes (2 880 o) acceptées, 42 (3 024 o) refusées.
MAX_LINES_PER_FRAME = 40
# Tolérance de pause mesurée : 400 ms tolérées, 1 000 ms referment la tâche (blanc de 4 mm).
DEFAULT_FRAME_PAUSE_MS = 0       # 0 = dos à dos (le plus sûr) ; < 200 ms = marge prudente

# --- Types de message YK ------------------------------------------------------------------
MSG_TOKEN = 0x80        # cusGetBleTokenBytes, payload 1 octet 0x01
MSG_PAPER_SIZE = 0x0F   # cusSetPaperSize, payload = largeur en uint16 LE
MSG_IMAGE_SLICE = 0x00  # cusPkgImgSlice, payload = données raster brutes de la tranche
MSG_FEED = 0x02         # cusFeedPaper, payload = uint16 LE (unité ≈ 0,1 mm)
MSG_STATUS = 0x10       # cusGetPrinterStatus, payload vide
MSG_BLANK_IMAGE = 0x01  # cusGetBlankImage — ⚠️ PAS une image : une ligne répétée N fois
MSG_POWER_OFF = 0x31
MSG_INIT_TASK = 0x50
MSG_END_TASK = 0x51
MSG_CANCEL_TASK = 0x52

# --- Chronologie MESURÉE sur le matériel (script validé du 28/09) ----------------------
# Après le token puis après la largeur, l'imprimante a besoin d'une respiration avant
# d'accepter l'image : 400 ms dans la recette qui a produit « MARVIN » sans défaut.
# Le compteur de trame démarre à 1 et s'incrémente à chaque trame émise.
SETTLE_AFTER_TOKEN_MS = 400
SETTLE_AFTER_WIDTH_MS = 400

FRAME_START = 0x64
FRAME_END = 0x9B

# Encodage de l'avance papier : 50 unités ≈ 5 mm.
FEED_UNITS_PER_MM = 10

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_CHUNK_SIZE = "chunk_size"
CONF_FRAME_PAUSE_MS = "frame_pause_ms"
CONF_FEED_BEFORE_MM = "feed_before_mm"
CONF_FEED_AFTER_MM = "feed_after_mm"
