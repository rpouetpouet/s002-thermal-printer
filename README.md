# ORGSTA S002 — imprimante thermique dans Home Assistant

Intégration custom Home Assistant pour les imprimantes thermiques **ORGSTA S002**
(protocole propriétaire **YK/CUS**, et non ESC/POS). Elle permet d'imprimer du texte et
des images depuis HA — automatisations, scripts, dashboard — **via la pile Bluetooth de
Home Assistant**, donc **à travers un proxy BLE ESP32** (`bluetooth_proxy: active: true`)
ou un adaptateur local, sans code spécifique.

## Pourquoi une intégration dédiée ?

L'ORGSTA S002 ne parle **pas ESC/POS** : son protocole a été décodé depuis
`libykDataPacket.so` de l'application Android *Snap and Tag*. Les commandes ESC/POS
classiques n'ont aucun effet sur cette imprimante.

Les caractéristiques mesurées sur le matériel, encodées dans cette intégration :

- tête **576 points** (72 octets par ligne), 300 dpi, largeur imprimable ≈ 48,8 mm ;
- image envoyée **non compressée** (le mode zlib est refusé par ce modèle) ;
- **40 lignes maximum par trame** (2 880 o) : au-delà, la trame est rejetée silencieusement ;
- une **pause de ~1 s entre deux trames referme la tâche** et l'imprimante avance 4 mm de
  blanc → les tranches sont envoyées **dos à dos** (tolérance mesurée : 400 ms) ;
- le service BLE `ff00` existe en **deux exemplaires** ; le canal d'écriture utile est la
  seconde occurrence (handle le plus haut).

## Installation (HACS)

1. HACS → *Intégrations* → menu ⋮ → **Dépôts personnalisés**
   URL : `https://github.com/rpouetpouet/s002-thermal-printer` — catégorie *Intégration*.
2. Télécharger, puis **redémarrer Home Assistant**.
3. *Paramètres → Appareils et services → Ajouter une intégration → ORGSTA S002*.
   Saisir l'adresse MAC de l'imprimante (elle s'affiche dans l'appli mobile ou sur
   l'étiquette ; l'imprimante est aussi proposée automatiquement si HA la découvre).

Aucun appairage n'est nécessaire : l'imprimante accepte les connexions BLE sans
association.

## Services

| Service | Effet |
| --- | --- |
| `s002_printer.print_test` | Motif de validation (repères + 6 bandes), ≈ 14 mm de papier |
| `s002_printer.print_text` | Imprime les lignes fournies (`text`, `scale` 1-6) |
| `s002_printer.feed` | Avance papier seule (`mm`) |
| `s002_printer.print_raw` | Raster brut en base64 (72 o/ligne, 1 = noir) — reproductible à l'octet |
| `s002_printer.print_image` | Image PNG/JPEG en base64, mise à l'échelle 576 points (`dither`, `invert`) |
| `s002_printer.diagnose` | Se connecte **sans imprimer** et renvoie le débit mesuré |

Tous les services acceptent `address` (si plusieurs imprimantes) et renvoient un compte
rendu avec **les mesures de débit** (`frames`, `avg_frame_ms`, `throughput_kbps`,
`channel_handle`) : c'est ce qui permet de juger si la liaison tient la charge.

### Exemple d'automatisation

```yaml
action:
  - service: s002_printer.print_text
    data:
      text: |
        Portail ouvert
        28/09 18:42
      scale: 2
    response_variable: cr
  - service: system_log.write
    data:
      message: "Imprimé en {{ cr.duration_s }} s ({{ cr.timings.avg_frame_ms }} ms/trame)"
```

## Réglages de transport (Options)

| Option | Défaut | Rôle |
| --- | --- | --- |
| `chunk_size` | 200 | Taille des paquets d'écriture BLE (20-237 ; MTU 240) |
| `lines_per_frame` | **8** | Lignes par trame. 8 ≈ 160 ms via proxy ; 40 (le maximum protocolaire) ≈ 870 ms → **blancs de 4 mm garantis** |
| `frame_pause_ms` | 0 | Pause entre trames (≤ 400 ms, sinon blanc garanti) |
| `feed_before_mm` / `feed_after_mm` | 0 | Marges d'avance autour de l'image |

## Résultats mesurés (chemin proxy BLE ESP32-C3 → S002)

Test de référence : image « MARVIN » encadrée, 200 lignes (16,93 mm), imprimée **continue**
depuis Home Assistant, alors que la VM HA n'a **aucun adaptateur Bluetooth**.

| grandeur | valeur |
| --- | --- |
| connexion | 460 ms (une fois l'appareil connu) ; 14-23 s au tout premier essai (découverte) |
| coût par paquet de 200 o | **54 ms** en moyenne, 118 ms max (≈ 4 ms en direct sur un hôte BLE local) |
| débit utile | **2,6 ko/s** |
| trame de 8 lignes | **155 ms** moyenne, 218 ms max → sous la tolérance de pause (400 ms) |
| impression complète | 6,4 s pour 16,93 mm (200 lignes, 25 tranches) |

## Limites connues

- **Débit via proxy BLE** : chaque écriture coûte un aller-retour Wi-Fi. Sur un ESP32-C3
  (mono-cœur, radio partagée BLE/Wi-Fi), utiliser `chunk_size` élevé et vérifier
  `avg_frame_ms` avec `s002_printer.diagnose`. Si le débit est insuffisant, une passerelle
  locale (Raspberry Pi) reste une alternative — l'architecture en `transport` de cette
  intégration est prévue pour l'accueillir.
- **Une impression à la fois** par imprimante (verrou interne).
- **Pas de compression** : une image de 200 lignes envoie 14,4 Ko utiles.
- **Texte** : police bitmap par défaut de Pillow (aucune fonte embarquée). Le rendu
  TrueType multi-polices est prévu.

## Crédits

L'architecture (services + verrou, mesures de débit, prévisualisation à venir) s'inspire de
[`cognitivegears/ha-escpos-thermal-printer`](https://github.com/cognitivegears/ha-escpos-thermal-printer)
(MIT), dont la chaîne image est réutilisable ; son encodeur ESC/POS ne l'est pas, le S002
ayant un protocole propriétaire.

## Licence

MIT.
