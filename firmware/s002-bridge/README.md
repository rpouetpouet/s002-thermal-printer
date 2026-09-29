# `s002-bridge` — nœud-pont BLE dédié (ESP32-C3)

Pont **dédié**, placé près de l'imprimante ORGSTA S002 : il remplace le chemin « proxy ESPHome »,
dont l'aller-retour Home Assistant → Wi-Fi → proxy → BLE **par paquet** coûtait 116 ms par écriture
de 200 octets et provoquait des blancs de 4 mm sur les impressions longues.

| chemin | coût par paquet de 200 o | une page de 93 mm |
| --- | --- | --- |
| proxy ESPHome (HA) | 116 ms moy. (614 max) | ~45 s (blancs) |
| **ce nœud (C3, Wi-Fi)** | **13,2 ms moy.** | **~2 s, propre** |
| Raspberry Pi en direct | ~4 ms | — |

Le nœud **reste bête** : il ne connaît ni le protocole YK ni les types de message. Home Assistant
construit les trames (`custom_components/s002_printer/yk.py`) et le nœud écrit des octets BLE avec
réponse en comptant les crédits de flux. Une seule source de vérité, moins de code embarqué.

## Ce que fait la v2 (29/09/2026)

1. **Rend la liaison BLE quand il ne s'en sert pas.** L'imprimante n'accepte qu'**un client à la
   fois** : en v1 le nœud appelait `ble_gap_connect()` une fois au démarrage et **n'appelait
   `ble_gap_terminate()` nulle part**, donc il gardait l'imprimante en permanence et aucun
   téléphone ne pouvait s'y connecter. Désormais la liaison est rendue après *N* secondes sans
   impression (`0` = jamais, comme en v1) et **reprise toute seule** à la prochaine impression
   (~1 à 3 s, invisible pour Home Assistant).
2. **Se met à jour par le réseau (OTA).** Atteindre le port USB du C3 demande d'ouvrir le boîtier
   de l'imprimante : le premier flash « compatible OTA » se fait par USB, **tous les suivants
   passent par le Wi-Fi** (`tools/ota_pousser.py`). L'image va dans la partition **inactive** :
   une coupure en cours de transfert ne casse pas le firmware qui tourne.

## L'interface réseau (port 3333, un client à la fois)

Le **premier octet** décide du mode :

- `0x64` → **flux binaire** : des trames YK telles quelles, écrites en BLE (c'est ce que le
  transport `node` de l'intégration Home Assistant utilise) ;
- **tout autre octet** → **commande texte**, terminée par `\n`, réponse sur la même connexion.

| commande | effet | réponse |
| --- | --- | --- |
| `PING` | — | `PONG` |
| `STATUS` | état complet | `etat=… liaison=tenue\|libre partition=ota_0\|ota_1 inactif=…s liberation=…s paquets=… credits=… ecritures=… annonces=… rssi=… memoire=…` |
| `LIBERER` | rend l'imprimante **tout de suite** | `LIBERE` / `DEJA_LIBRE` |
| `CONNECTER` | reprend la liaison | `RECHERCHE` / `DEJA_CONNECTE` |
| `LIBERATION <s>` | règle le délai d'inactivité (0 = jamais) | `LIBERATION <s>S` |
| `OTA <octets>` | reçoit `octets` octets bruts = une image d'application | `OTA PRET`, puis `OTA OK REDEMARRAGE` |

### Prouver ce qui se passe, sans papier

Deux compteurs suffisent, et ils ne mentent pas :

- **`annonces` gelé** entre deux relevés = le nœud tient la liaison (il ne scanne plus) ;
- **`ecritures`** : relever **juste avant** la commande d'impression, puis après. L'écart doit égaler
  le nombre de morceaux envoyés par Home Assistant. C'est la seule preuve que le papier est passé
  par le nœud ;
- **`partition`** : passer de `ota_0` à `ota_1` est la preuve qu'une mise à jour OTA a été prise.

## Compiler

```bash
. ~/esp/esp-idf/export.sh
cd firmware/s002-bridge
idf.py set-target esp32c3
idf.py build
```

ESP-IDF **v6.0.3** (la branche v5.4 est incompatible avec CMake 4.2 d'Ubuntu 26.04).
⚠️ `sdkconfig` (généré) **contient le mot de passe Wi-Fi** : non versionné (`.gitignore`),
seul `sdkconfig.defaults` l'est.

## Flasher par USB (une fois, ou en secours)

⚠️ **Ne jamais écrire la région `0x9000` (NVS) : c'est là que vivent les identifiants Wi-Fi.**
`idf.py flash` écrit toute la table depuis 0x0 et **efface le Wi-Fi** — flasher région par région.

```bash
~/esptool-venv/bin/python -m esptool --chip esp32c3 -p /dev/serial/by-id/usb-Espressif_* \
  --before default_reset --after watchdog_reset write_flash \
  --flash_mode dio --flash_freq 80m --flash_size 4MB \
  0x0 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0xf000 build/ota_data_initial.bin \
  0x20000 build/s002-bridge.bin
```

- `--after watchdog_reset` : les Super Mini ne câblent pas RTS à EN, donc `hard_reset` ne fait
  rien et la puce **reste en mode téléchargement** ;
- le port change de nom après un reset : toujours passer par `/dev/serial/by-id/usb-Espressif_*` ;
- esptool **v4 (pip)** utilise `write_flash` et des tirets bas (`--flash_mode`) ; **v5 (ESP-IDF)**
  utilise des tirets. Les deux commandes ci-dessus sont celles de la v4.

## Mettre à jour par le réseau (OTA)

```bash
python tools/ota_pousser.py build/s002-bridge.bin            # --hote 192.168.42.62 --port 3333
```

Le script annonce la taille, attend `OTA PRET`, envoie l'image (~13 s pour 1,05 Mo, ~85 Ko/s),
puis **relit la partition** : `ota_0` → `ota_1` = mise à jour confirmée.

## Table de partitions (`partitions.csv`)

```
nvs      0x9000   24 Ko     <- identifiants Wi-Fi : NE JAMAIS TRONQUER NI EFFACER
otadata  0xf000    8 Ko
phy_init 0x11000   4 Ko
ota_0    0x20000  1856 Ko
ota_1    0x1f0000 1856 Ko
```

L'application démarre à `0x20000` (et non `0x10000`) parce qu'`otadata` a besoin de 8 Ko après la
NVS. Les 60 Ko libres entre `0x11000` et `0x20000` ne servent à rien : gratter dedans voudrait dire
toucher à la NVS ou à `otadata`.

## Contraintes matérielles et pièges (tous vécus)

- **Écriture BLE AVEC réponse obligatoire** : sans acquittement l'imprimante reçoit les octets et
  **n'imprime rien**, en silence.
- **Une seule opération GATT en vol** (NimBLE) : lancer une découverte depuis le callback d'une
  autre renvoie `EBUSY` silencieusement → machine à états qui n'avance que sur `BLE_HS_EDONE`.
- **Pas de `ble_gattc_subscribe`** : l'abonnement s'écrit sur le descripteur CCCD (`0x2902`).
- **Un crédit de flux (`01 05`) n'est pas une fin d'écriture** : ne pas libérer le sémaphore
  d'écriture dessus, ça désynchronise la sérialisation.
- **Tampon de réception TCP en `static`** : 5 Ko sur la pile d'une tâche la fait déborder (vécu).
- **Console sur USB-Serial-JTAG** (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`) : sinon, sur une carte
  dont seule la prise USB est branchée, la ROM répond mais **aucun log applicatif** n'apparaît.
- La liste des services/caractéristiques est **affichée au démarrage** : si le handle d'écriture
  configuré n'y apparaît pas, le firmware le dit au lieu d'écrire dans le vide.
