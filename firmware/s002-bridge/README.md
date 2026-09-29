# `s002-bridge` — nœud-pont BLE dédié (ESP32-C3)

Remplace le chemin « proxy ESPHome » par un pont **dédié**, placé près de l'imprimante : plus
d'aller-retour Home Assistant → Wi-Fi → proxy → BLE **par paquet**, qui coûtait 116 ms par écriture
de 200 octets et provoquait des blancs de 4 mm sur les impressions longues.

> **v0 = banc de mesure**, pas encore le pont final. Elle se connecte, découvre, imprime un motif
> de test et chronomètre chaque écriture. Le TCP viendra en v1 (voir
> `references/transport-esp32-node.md` dans le skill `orgsta-s002-printer`).

## Ce que la v0 mesure (et pourquoi ces quatre choses)

| mesure | pourquoi c'est elle qui compte |
| --- | --- |
| connexion réussie avec le **type d'adresse annoncé** | l'adresse de l'imprimante est localement administrée (`random`) ; un central qui se connecte en `public` échoue sans message clair |
| **intervalle de connexion négocié** | c'est le levier principal : intervalle court = ~4 ms/paquet, intervalle long = blancs |
| **durée d'une écriture avec réponse** | le chiffre à comparer aux 116 ms/paquet du proxy et aux ~4 ms mesurés en direct depuis la Pi |
| **MTU et DLE négociées** | inconnues à ce jour : à mesurer, pas à supposer |

Le juge final reste le papier : le motif doit sortir **continu**, sans blanc.

## Compiler

```bash
. ~/esp/esp-idf/export.sh
cd firmware/s002-bridge
idf.py set-target esp32c3
idf.py menuconfig      # « S002 bridge (v0) » : SSID / mot de passe Wi-Fi
idf.py -j2 build
```

## Flasher et lire le résultat

```bash
idf.py -p /dev/ttyUSB0 flash monitor     # ou /dev/ttyACM0 (USB-Serial-JTAG du C3)
```

`menuconfig` → *S002 bridge (v0)* :

- **Wi-Fi SSID / password** — vide = mesure BLE seule (sans coexistence radio, à faire en premier
  pour établir la référence) ;
- **Adresse MAC** — `06:03:DD:EC:16:4D` par défaut ;
- **Handles** — écriture `0x0011`, notify état `0x000e`, flux `0x0013` (mesurés le 28/09). La v0
  **affiche le plan réel** des services/caractéristiques : si le handle d'écriture configuré
  n'apparaît pas dans la découverte, elle le dit explicitement au lieu d'écrire dans le vide.

À lire dans le moniteur :

```
CONNECTE : intervalle reel X ms          <- doit etre proche de 7,5-15 ms
MTU negociee : N                          <- 240 attendu
ecriture  1 :   X ms                      <- doit tourner autour de 4 ms, pas 116
=== BILAN : N ecritures | moy X ms | max Y ms ===
```

## Une contrainte NimBLE qui dicte l'architecture du code

Une seule opération GATT peut être en vol à la fois : lancer une découverte de caractéristiques
**depuis** le callback de découverte de services renvoie `EBUSY` — et l'erreur passe facilement
inaperçue. Le code enchaîne donc les étapes via une machine à états qui n'avance qu'à la réception
de `BLE_HS_EDONE`.

Deux autres pièges traités dans le code :

- **l'abonnement aux notifications se fait en écrivant sur le descripteur CCCD** (`0x2902`) : il
  n'existe pas de fonction `ble_gattc_subscribe` dans NimBLE ;
- **un crédit de flux (`01 05`) n'est pas une fin d'écriture** : le libérer sur le sémaphore
  d'écriture désynchroniserait la sérialisation des écritures.

## Écriture AVEC réponse, obligatoire

Sans réponse, l'imprimante reçoit les octets et **n'imprime rien**, en silence (constaté le 28/09
via proxy). Le code utilise `ble_gattc_write_flat`, qui attend l'acquittement.
