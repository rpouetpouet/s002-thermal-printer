"""Constantes du protocole YK/CUS de l'imprimante thermique ORGSTA S002.

Toutes les valeurs proviennent de mesures réelles sur le matériel (voir le skill
`orgsta-s002-printer`) et non d'une documentation constructeur.
"""

import re

DOMAIN = "s002_printer"
NAME = "ORGSTA S002 Thermal Printer"
VERSION = "0.2.1"

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
# ⚠️ Écriture AVEC réponse : sans elle l'imprimante reçoit les octets et n'imprime RIEN
# (panne silencieuse, constatée le 28/09 via proxy).
DEFAULT_WRITE_RESPONSE = True
# Fenêtre de contrôle de flux : l'imprimante acquitte (`01 05`) tous les 5 paquets.
FLOW_WINDOW = 5
FLOW_TIMEOUT_MS = 300

# --- Géométrie (mesurée : cadre à 2 mm du bord gauche, 49 mm de large, 200 lignes = 16 mm) ---
PRINT_WIDTH_DOTS = 576          # 576 points = 48,8 mm ≈ 49 mm mesurés
BYTES_PER_LINE = PRINT_WIDTH_DOTS // 8   # 72 octets par ligne
DOTS_PER_MM = 11.81             # 300 dpi
# Limite de trame mesurée : 40 lignes (2 880 o) acceptées, 42 (3 024 o) refusées.
MAX_LINES_PER_FRAME = 40
# Défaut PRUDENT pour un chemin proxifié (le cas normal ici) : 8 lignes ≈ 3 paquets
# ≈ 160 ms, bien sous la tolérance de pause de ~400 ms. Le maximum protocolaire
# (40 lignes ≈ 870 ms via proxy) provoque des blancs de 4 mm entre trames : mesuré.
DEFAULT_LINES_PER_FRAME = 8
# Budget visé pour UNE trame d'image, en millisecondes. La tolérance de pause mesurée de
# l'imprimante est de ~400 ms : au-delà, elle referme la tâche et avance 4 mm de BLANC.
# On vise plus bas pour garder une marge (un pic de latence ne doit pas coûter du papier).
FRAME_BUDGET_MS = 250
MIN_FRAME_BUDGET_MS = 50
MAX_FRAME_BUDGET_MS = 2000
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
CONF_WRITE_RESPONSE = "write_response"

# Transport d'impression — deux chemins, même protocole YK, même code d'impression :
#   * « proxy » : le Bluetooth de Home Assistant via les proxies ESP32 (chemin historique).
#     Mesuré à 116 ms par paquet sur ce réseau, ce qui dépasse la tolérance de pause de
#     l'imprimante (400 ms) et produit des blancs sur les longues impressions.
#   * « node »  : un ESP32-C3 dédié, posé près de l'imprimante, joignable en TCP sur le réseau
#     local. Mesuré à ~14 ms par paquet, et c'est LUI qui gère les crédits de flux annoncés par
#     l'imprimante (Home Assistant se contente de lui transmettre les trames).
CONF_TRANSPORT = "transport"
TRANSPORT_PROXY = "proxy"
TRANSPORT_NODE = "node"
DEFAULT_TRANSPORT = TRANSPORT_PROXY
TRANSPORTS = (TRANSPORT_PROXY, TRANSPORT_NODE)
CONF_NODE_HOST = "node_host"
CONF_NODE_PORT = "node_port"
DEFAULT_NODE_PORT = 3333

# Un libellé recopié depuis un fichier de configuration (« node_host: 192.168.42.62 »),
# des guillemets ou un « :port » collé se glissent facilement dans un champ de formulaire.
_PREFIXE_HOTE = re.compile(
    r"^\s*(?:node[_-]?host|h[oô]te?|host|adresse)\s*[=:]\s*", re.IGNORECASE
)
_PORT_COLLE = re.compile(r"^(?P<hote>[^:/\s]+):(?P<port>\d{1,5})$")


def normaliser_hote_node(valeur: object) -> tuple[str, int | None]:
    """Nettoie l'adresse de nœud saisie par l'utilisateur.

    Sans ce nettoyage, recopier « node_host: 192.168.42.62 » dans le champ donne un hôte
    qui n'existe pas : l'impression échoue en « Name does not resolve », message qui ne
    désigne pas la vraie cause (on a cherché ailleurs dans un premier temps).

    Renvoie l'hôte nettoyé et, si l'utilisateur a collé « hôte:port », le port à utiliser.
    """
    texte = str(valeur or "")
    for _ in range(2):        # un libellé peut précéder des guillemets, et inversement
        texte = texte.strip().strip("\"'").strip()
        texte = _PREFIXE_HOTE.sub("", texte, count=1)
    port: int | None = None
    correspondance = _PORT_COLLE.match(texte)
    if correspondance:
        texte, port = correspondance.group("hote"), int(correspondance.group("port"))
    return texte.strip(), port
