/* Nœud-pont BLE dédié à l'imprimante thermique ORGSTA S002 — banc de mesure v0.
 *
 * OBJECTIF DE CETTE VERSION : mesurer, pas imprimer joli. On veut savoir
 *   (1) si on arrive à se connecter (l'adresse de l'imprimante est LOCALEMENT ADMINISTRÉE
 *       → type `random` : un central qui se connecte en `public` échoue sans message clair) ;
 *   (2) quel intervalle de connexion l'imprimante négocie réellement ;
 *   (3) combien de temps prend UNE écriture avec réponse (à comparer aux 116 ms/paquet du
 *       proxy ESPHome et aux ~4 ms mesurés en direct depuis la Pi) ;
 *   (4) ce que l'imprimante négocie en MTU et en DLE.
 *
 * Le vrai juge reste le papier : le motif doit sortir continu, sans blanc.
 *
 * CE QUE CE FIRMWARE N'EST PAS : il n'encode rien. Les trames sont construites ici au même
 * format que `custom_components/s002_printer/yk.py` (validé octet pour octet sur le
 * matériel) parce qu'en v0 il n'y a pas encore de serveur TCP ; en v1 le nœud recevra ces
 * trames telles quelles depuis Home Assistant et se contentera de les écrire.
 *
 * ⚠️ CONTRAINTE NIMBLE QUI A DICTÉ L'ARCHITECTURE : une seule opération GATT peut être en
 * vol à la fois. Lancer une découverte de caractéristiques depuis le callback de découverte
 * de services renvoie `EBUSY` (et l'erreur est facile à manquer). D'où la machine à états
 * ci-dessous, qui n'enchaîne l'étape suivante qu'à la réception de `BLE_HS_EDONE`.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_att.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "os/os_mbuf.h"

static const char *TAG = "s002";

/* --------------------------------------------------------------------------------------
 * Constantes du protocole YK (mesurées sur le matériel — skill orgsta-s002-printer)
 * -------------------------------------------------------------------------------------- */
#define FRAME_START 0x64
#define FRAME_END 0x9B
#define MSG_IMAGE_SLICE 0x00
#define MSG_FEED 0x02
#define MSG_PAPER_SIZE 0x0F
#define MSG_TOKEN 0x80

#define BYTES_PER_LINE 72       /* 576 points / 8 = 48,8 mm à 300 dpi */
#define CHUNK_SIZE 200          /* taille d'écriture validée (MTU annoncée 240) */
#define SETTLE_AFTER_TOKEN_MS 400
#define SETTLE_AFTER_WIDTH_MS 400
#define WRITE_TIMEOUT_MS 3000
#define TOLERANCE_MS 400        /* tolérance de pause mesurée de l'imprimante */

#define MAX_SVCS 8
#define MAX_NOTIFY 4
#define CCCD_UUID16 0x2902

/* --------------------------------------------------------------------------------------
 * État
 * -------------------------------------------------------------------------------------- */
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint8_t s_counter = 1;           /* compteur roulant global (mod 64), démarre à 1 */
static ble_addr_t s_target;
static SemaphoreHandle_t s_write_done;  /* libéré UNIQUEMENT par la fin d'une écriture */
static int64_t s_t0;
static int64_t s_worst_us, s_best_us = INT64_MAX, s_total_us;
static uint32_t s_write_count, s_write_timeouts;
static uint32_t s_notify_rx, s_credits;

/* Découverte séquentielle */
enum { ST_IDLE, ST_SVC, ST_CHR, ST_DSC, ST_SUB, ST_PRINT };
static volatile int s_state = ST_IDLE;
static uint16_t s_svc_start[MAX_SVCS], s_svc_end[MAX_SVCS];
static int s_svc_nb, s_svc_i;
static uint16_t s_notify_h[MAX_NOTIFY], s_notify_svc_end[MAX_NOTIFY], s_cccd_h[MAX_NOTIFY];
static int s_notify_trouves, s_notify_i;
static bool s_write_handle_confirmee, s_pret_a_imprimer;

/* Diagnostic : sans ces compteurs, un firmware qui attend son peripherique est TOTALEMENT
 * muet, ce qui est indiagnostiquable a distance (vecu le 29/09/2026 : flash verifie bon,
 * zero octet a la console, cause reelle = rien a journaliser). */
static uint32_t s_adv_vus;
static int8_t s_rssi_max = -127;

/** Cherche le nom annonce (types AD 0x08 = nom abrege, 0x09 = nom complet) et le compare.
 *  L'imprimante S002 annonce son nom ; sa MAC, elle, est une adresse privee NON RESOLVABLE
 *  (premier octet 0x06 => bits 7-6 = 00), donc susceptible de changer : filtrer sur la MAC
 *  seule rend le pont aveugle apres un cycle d'alimentation de l'imprimante. */
static const char *adv_nom(const uint8_t *data, uint8_t len, char *tampon, size_t taille)
{
    uint8_t i = 0;
    while (i + 1 < len) {
        uint8_t l = data[i];
        if (l == 0) {
            break;
        }
        if (i + 1 + l > len) {
            break;
        }
        uint8_t type = data[i + 1];
        if (type == 0x08 || type == 0x09) {
            size_t n = (l - 1) < (taille - 1) ? (l - 1) : (taille - 1);
            memcpy(tampon, &data[i + 2], n);
            tampon[n] = '\0';
            return tampon;
        }
        i += l + 1;
    }
    return NULL;
}

static uint32_t s_noms_journalises;

/* --------------------------------------------------------------------------------------
 * Statistiques d'écriture
 * -------------------------------------------------------------------------------------- */
static void stats_add(int64_t duree_us)
{
    s_write_count++;
    s_total_us += duree_us;
    if (duree_us > s_worst_us) {
        s_worst_us = duree_us;
    }
    if (duree_us < s_best_us) {
        s_best_us = duree_us;
    }
    ESP_LOGI(TAG, "  ecriture %2u : %6.1f ms%s", (unsigned)s_write_count, duree_us / 1000.0,
             (duree_us / 1000) > TOLERANCE_MS ? "   <-- AU-DELA DE LA TOLERANCE (400 ms)" : "");
}

static void stats_report(void)
{
    if (s_write_count == 0) {
        ESP_LOGW(TAG, "aucune ecriture mesuree");
        return;
    }
    double moy = (double)s_total_us / s_write_count / 1000.0;
    ESP_LOGI(TAG, "=== BILAN : %u ecritures | moy %.1f ms | min %.1f ms | max %.1f ms | "
                  "timeouts %u | notify %u (credits %u) ===",
             (unsigned)s_write_count, moy, s_best_us / 1000.0, s_worst_us / 1000.0,
             (unsigned)s_write_timeouts, (unsigned)s_notify_rx, (unsigned)s_credits);
    ESP_LOGI(TAG, "    reference : proxy ESPHome 116 ms/paquet | Pi en direct ~4 ms/paquet "
                  "| tolerance 400 ms");
}

/* --------------------------------------------------------------------------------------
 * Construction des trames (même format que yk.py)
 *   octet 0 = 0x64 | 1 = type | 2 = compteur (mod 64) | 3-4 = longueur (uint16 LE)
 *   octets 5.. = payload | puis 4 zéros de contrôle | puis 0x9B
 * -------------------------------------------------------------------------------------- */
static size_t build_frame(uint8_t *out, uint8_t type, const uint8_t *payload, size_t len)
{
    out[0] = FRAME_START;
    out[1] = type;
    out[2] = s_counter++ & 0x3F;
    out[3] = (uint8_t)(len & 0xFF);
    out[4] = (uint8_t)((len >> 8) & 0xFF);
    if (len) {
        memcpy(&out[5], payload, len);
    }
    memset(&out[5 + len], 0, 4);
    out[5 + len + 4] = FRAME_END;
    return 5 + len + 5;
}

/* --------------------------------------------------------------------------------------
 * Écriture BLE avec réponse, découpée en paquets de CHUNK_SIZE octets, strictement en série
 * -------------------------------------------------------------------------------------- */
static int write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                    struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    (void)arg;
    int64_t duree = esp_timer_get_time() - s_t0;
    if (error->status == 0) {
        stats_add(duree);
    } else {
        ESP_LOGE(TAG, "  ecriture REFUSEE : status=%d", error->status);
    }
    xSemaphoreGive(s_write_done);
    return 0;
}

/** Écrit un bloc en paquets de CHUNK_SIZE, une écriture en vol à la fois.
 *  `mesurer=false` pour les écritures de service (abonnement CCCD) qu'on ne veut pas
 *  mélanger aux mesures du chemin d'impression. */
static bool ble_write_block(uint16_t handle, const uint8_t *data, size_t len, bool mesurer)
{
    size_t offset = 0;
    while (offset < len) {
        size_t n = (len - offset) > CHUNK_SIZE ? CHUNK_SIZE : (len - offset);
        s_t0 = esp_timer_get_time();
        int rc = ble_gattc_write_flat(s_conn_handle, handle, data + offset, n,
                                      mesurer ? write_cb : NULL, NULL);
        if (rc != 0) {
            ESP_LOGE(TAG, "  ble_gattc_write_flat refuse : rc=%d", rc);
            return false;
        }
        if (mesurer &&
            xSemaphoreTake(s_write_done, pdMS_TO_TICKS(WRITE_TIMEOUT_MS)) != pdTRUE) {
            s_write_timeouts++;
            ESP_LOGE(TAG, "  TIMEOUT d'ecriture (> %d ms) — lien BLE perdu ?", WRITE_TIMEOUT_MS);
            return false;
        }
        if (!mesurer) {
            /* Écriture sans callback : on laisse respirer la pile avant la suivante. */
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        offset += n;
    }
    return true;
}

static void send_frame(uint8_t handle, uint8_t type, const uint8_t *payload, size_t len)
{
    static uint8_t buf[5 + 4096 + 5];
    if (len > 4096) {
        ESP_LOGE(TAG, "payload trop long : %u", (unsigned)len);
        return;
    }
    size_t n = build_frame(buf, type, payload, len);
    if (!ble_write_block(handle, buf, n, true)) {
        ESP_LOGE(TAG, "trame type 0x%02x NON envoyee", type);
    }
}

/* --------------------------------------------------------------------------------------
 * Motif de test : bordure + repères + hâchures, pour juger le papier à l'oeil
 * -------------------------------------------------------------------------------------- */
static void build_test_raster(uint8_t *raster, int lignes)
{
    for (int i = 0; i < lignes; i++) {
        uint8_t *l = &raster[i * BYTES_PER_LINE];
        for (int b = 0; b < BYTES_PER_LINE; b++) {
            uint8_t v = 0;
            if (i == 0 || i == lignes - 1) {
                v = 0xFF;                       /* bordure haute et basse */
            } else {
                if (b == 0 || b == BYTES_PER_LINE - 1) {
                    v = 0xFF;                   /* bords gauche et droit */
                }
                if ((b % 8) == 0) {
                    v |= 0x80;                  /* repère tous les 64 points ≈ 5,4 mm */
                }
                if ((i % 12) == 0 && (b & 1) == 0) {
                    v |= 0xAA;                  /* hâchures : jugent la densité et les blancs */
                }
            }
            l[b] = v;
        }
    }
}

/* --------------------------------------------------------------------------------------
 * Séquence d'impression validée : token → pause → largeur → pause → tranches → avance
 * -------------------------------------------------------------------------------------- */
static void print_test_pattern(uint8_t write_handle)
{
    const int lignes_total = CONFIG_S002_TEST_PATTERN_LINES;
    const int par_trame = CONFIG_S002_LINES_PER_FRAME;
    static uint8_t raster[200 * BYTES_PER_LINE];

    build_test_raster(raster, lignes_total);

    ESP_LOGI(TAG, "--- token d'ouverture (compteur %u) ---", (unsigned)(s_counter & 0x3F));
    const uint8_t tok = 0x01;
    send_frame(write_handle, MSG_TOKEN, &tok, 1);
    vTaskDelay(pdMS_TO_TICKS(SETTLE_AFTER_TOKEN_MS));

    ESP_LOGI(TAG, "--- largeur papier 576 points (48,8 mm) ---");
    const uint8_t largeur[2] = {0x40, 0x02}; /* 576 en uint16 LE */
    send_frame(write_handle, MSG_PAPER_SIZE, largeur, 2);
    vTaskDelay(pdMS_TO_TICKS(SETTLE_AFTER_WIDTH_MS));

    ESP_LOGI(TAG, "--- %d lignes en tranches de %d lignes, DOS A DOS ---", lignes_total, par_trame);
    for (int i = 0; i < lignes_total; i += par_trame) {
        int n = (lignes_total - i) < par_trame ? (lignes_total - i) : par_trame;
        send_frame(write_handle, MSG_IMAGE_SLICE, &raster[i * BYTES_PER_LINE],
                   (size_t)n * BYTES_PER_LINE);
    }

    ESP_LOGI(TAG, "--- avance papier 20 mm ---");
    const uint8_t avance[2] = {200, 0}; /* 200 unités ≈ 20 mm */
    send_frame(write_handle, MSG_FEED, avance, 2);

    stats_report();
    ESP_LOGI(TAG, "motif envoye : le papier tranche (continu = succes, blanc = echec)");
}

/* --------------------------------------------------------------------------------------
 * Découverte séquentielle : services → caractéristiques → descripteurs → abonnement
 * -------------------------------------------------------------------------------------- */
static void etape_suivante(void);

static int dsc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                  uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc, void *arg)
{
    (void)conn_handle;
    (void)arg;
    if (error->status == BLE_HS_EDONE) {
        if (s_notify_i < MAX_NOTIFY && s_cccd_h[s_notify_i] != 0) {
            ESP_LOGI(TAG, "  CCCD trouve pour notify 0x%04x -> 0x%04x", s_notify_h[s_notify_i],
                     s_cccd_h[s_notify_i]);
        } else {
            ESP_LOGW(TAG, "  pas de CCCD trouve pour notify 0x%04x : pas d'abonnement possible",
                     s_notify_h[s_notify_i]);
        }
        s_notify_i++;
        etape_suivante();
        return 0;
    }
    if (error->status != 0) {
        return 0;
    }
    if (dsc->uuid.u.type == BLE_UUID_TYPE_16 &&
        ble_uuid_u16((const ble_uuid_t *)&dsc->uuid) == CCCD_UUID16) {
        /* On garde le PREMIER CCCD de la plage : la plage va jusqu'a la fin du service, donc
         * ecraser a chaque trouvaille faisait retenir le CCCD d'une AUTRE caracteristique
         * (observe le 29/09 : 0x000e et 0x0013 pointaient tous deux sur 0x0014). */
        if (s_notify_i < MAX_NOTIFY && s_cccd_h[s_notify_i] == 0) {
            s_cccd_h[s_notify_i] = dsc->handle;
        }
    }
    return 0;
}

static int chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                  const struct ble_gatt_chr *chr, void *arg)
{
    (void)conn_handle;
    (void)arg;
    if (error->status == BLE_HS_EDONE) {
        s_svc_i++;
        etape_suivante();
        return 0;
    }
    if (error->status != 0) {
        ESP_LOGW(TAG, "  erreur de decouverte des caracteristiques : status=%d", error->status);
        return 0;
    }
    ESP_LOGI(TAG, "  char : def=0x%04x val=0x%04x props=0x%02x%s", chr->def_handle, chr->val_handle,
             chr->properties,
             (chr->val_handle == CONFIG_S002_WRITE_HANDLE) ? "   <-- HANDLE D'ECRITURE CONFIGURE"
                                                           : "");
    if (chr->val_handle == CONFIG_S002_WRITE_HANDLE) {
        s_write_handle_confirmee = true;
    }
    if ((chr->properties & BLE_GATT_CHR_PROP_NOTIFY) && s_notify_trouves < MAX_NOTIFY) {
        s_notify_h[s_notify_trouves] = chr->val_handle;
        s_notify_svc_end[s_notify_trouves] = s_svc_end[s_svc_i];
        s_notify_trouves++;
    }
    return 0;
}

static int svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                  const struct ble_gatt_svc *svc, void *arg)
{
    (void)conn_handle;
    (void)arg;
    if (error->status == BLE_HS_EDONE) {
        ESP_LOGI(TAG, "  services trouves : %d", s_svc_nb);
        s_state = ST_CHR;
        s_svc_i = 0;
        etape_suivante();
        return 0;
    }
    if (error->status != 0) {
        ESP_LOGW(TAG, "  erreur de decouverte de service : status=%d", error->status);
        return 0;
    }
    char uuid[40] = "128 bits";
    if (svc->uuid.u.type == BLE_UUID_TYPE_16) {
        snprintf(uuid, sizeof(uuid), "0x%04x", ble_uuid_u16((const ble_uuid_t *)&svc->uuid));
    }
    ESP_LOGI(TAG, "  service : handles 0x%04x..0x%04x uuid=%s", svc->start_handle, svc->end_handle,
             uuid);
    if (s_svc_nb < MAX_SVCS) {
        s_svc_start[s_svc_nb] = svc->start_handle;
        s_svc_end[s_svc_nb] = svc->end_handle;
        s_svc_nb++;
    }
    return 0;
}

/** Enchaîne l'étape courante ; appelée à chaque `BLE_HS_EDONE`. */
static void etape_suivante(void)
{
    switch (s_state) {
    case ST_CHR:
        if (s_svc_i < s_svc_nb) {
            ESP_LOGI(TAG, "  caracteristiques du service 0x%04x..0x%04x", s_svc_start[s_svc_i],
                     s_svc_end[s_svc_i]);
            ble_gattc_disc_all_chrs(s_conn_handle, s_svc_start[s_svc_i], s_svc_end[s_svc_i],
                                    chr_cb, NULL);
        } else {
            ESP_LOGI(TAG, "  notify detectes : %d", s_notify_trouves);
            s_state = ST_DSC;
            s_notify_i = 0;
            etape_suivante();
        }
        break;

    case ST_DSC:
        if (s_notify_i < s_notify_trouves) {
            ble_gattc_disc_all_dscs(s_conn_handle, s_notify_h[s_notify_i],
                                    s_notify_svc_end[s_notify_i], dsc_cb, NULL);
        } else {
            s_state = ST_SUB;
            s_notify_i = 0;
            etape_suivante();
        }
        break;

    case ST_SUB: {
        /* Abonnement aux notifications : on écrit sur le descripteur CCCD.
         * Il n'existe PAS de fonction `ble_gattc_subscribe` dans NimBLE.
         * ⚠️ Cette branche est appelée UNE fois (depuis le dernier `BLE_HS_EDONE`) : elle doit
         * donc BOUCLER sur tous les descripteurs. Sans la boucle, un seul abonnement était
         * traité et l'impression ne démarrait jamais (bug constate le 29/09/2026). */
        while (s_notify_i < s_notify_trouves) {
            if (s_cccd_h[s_notify_i] != 0) {
                const uint8_t cccd_val[2] = {0x01, 0x00}; /* notifications activees */
                ESP_LOGI(TAG, "  abonnement notify 0x%04x (CCCD 0x%04x)", s_notify_h[s_notify_i],
                         s_cccd_h[s_notify_i]);
                ble_write_block(s_cccd_h[s_notify_i], cccd_val, 2, false);
            } else {
                ESP_LOGW(TAG, "  notify 0x%04x sans CCCD : pas d'abonnement possible",
                         s_notify_h[s_notify_i]);
            }
            s_notify_i++;
        }
        ESP_LOGI(TAG, "  abonnements termines — pret a imprimer");
        s_pret_a_imprimer = true;
        s_state = ST_PRINT;
        break;
    }
    default:
        break;
    }
}

/* --------------------------------------------------------------------------------------
 * Événements GAP
 * -------------------------------------------------------------------------------------- */
static void start_scan(void);

static int gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_DISC: {
        s_adv_vus++;
        if (event->disc.rssi > s_rssi_max) {
            s_rssi_max = event->disc.rssi;
        }

        char nom[40];
        const char *nom_vu = adv_nom(event->disc.data, event->disc.length_data, nom, sizeof(nom));

        bool mac_correspond = memcmp(event->disc.addr.val, s_target.val, 6) == 0;
        bool nom_correspond = nom_vu && strcmp(nom_vu, CONFIG_S002_NOM) == 0;

        if (!mac_correspond && !nom_correspond) {
            /* Trace des appareils NOMMES : indispensable pour diagnostiquer « l'imprimante
             * n'est pas vue » (est-elle eteinte ? son adresse a-t-elle change ?). */
            if (nom_vu && s_noms_journalises < 15) {
                s_noms_journalises++;
                ESP_LOGI(TAG, "  annonce nommee : \"%s\" %02X:%02X:%02X:%02X:%02X:%02X RSSI %d",
                         nom_vu, event->disc.addr.val[5], event->disc.addr.val[4],
                         event->disc.addr.val[3], event->disc.addr.val[2], event->disc.addr.val[1],
                         event->disc.addr.val[0], event->disc.rssi);
            }
            return 0;
        }

        ESP_LOGW(TAG, "IMPRIMANTE TROUVEE (%s) : %02X:%02X:%02X:%02X:%02X:%02X RSSI %d dBm, "
                      "type d'adresse %s",
                 nom_correspond ? "par son nom" : "par sa MAC", event->disc.addr.val[5],
                 event->disc.addr.val[4], event->disc.addr.val[3], event->disc.addr.val[2],
                 event->disc.addr.val[1], event->disc.addr.val[0], event->disc.rssi,
                 event->disc.addr.type == BLE_ADDR_RANDOM ? "random" : "public");
        if (!mac_correspond) {
            /* On mémorise la nouvelle adresse : c'est celle qu'il faudra retenir si elle a
             * changé depuis le dernier appairage. */
            memcpy(s_target.val, event->disc.addr.val, 6);
            ESP_LOGW(TAG, "  adresse differente de la MAC configuree : le S002 a change "
                          "d'adresse (NRPA). Nouvelle adresse retenue pour cette session.");
        }
        s_target.type = event->disc.addr.type;
        ble_gap_disc_cancel();

        struct ble_gap_conn_params cp = {
            .scan_itvl = 0x0060,
            .scan_window = 0x0030,
            .itvl_min = 6,               /* 6 x 1,25 ms = 7,5 ms — le levier principal */
            .itvl_max = 12,              /* 12 x 1,25 ms = 15 ms */
            .latency = 0,
            .supervision_timeout = 400,  /* 400 x 10 ms = 4 s */
            .min_ce_len = 0,
            .max_ce_len = 0,
        };
        int rc = ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &s_target, 5000, &cp, gap_event_cb, NULL);
        ESP_LOGI(TAG, "connexion demandee (rc=%d) : intervalle demande 7,5-15 ms", rc);
        return 0;
    }

    case BLE_GAP_EVENT_CONNECT: {
        if (event->connect.status != 0) {
            ESP_LOGE(TAG, "connexion ECHOUEE : status=%d", event->connect.status);
            vTaskDelay(pdMS_TO_TICKS(2000));
            start_scan();
            return 0;
        }
        s_conn_handle = event->connect.conn_handle;

        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(s_conn_handle, &desc) == 0) {
            double itvl = desc.conn_itvl * 1.25;
            ESP_LOGI(TAG, "CONNECTE : intervalle reel %.1f ms, latence %u, timeout %u ms", itvl,
                     desc.conn_latency, desc.supervision_timeout * 10);
            if (itvl > 30.0) {
                ESP_LOGW(TAG, "  intervalle > 30 ms : c'est EXACTEMENT ce qui cree les blancs "
                              "(116 ms/paquet via proxy)");
            }
        }
        ble_att_set_preferred_mtu(247);
        int rc = ble_gattc_exchange_mtu(s_conn_handle, NULL, NULL);
        ESP_LOGI(TAG, "echange de MTU demande (rc=%d)", rc);
        rc = ble_gap_set_data_len(s_conn_handle, 251, 2120);
        ESP_LOGI(TAG, "DLE demande : 251 o / 2120 us (rc=%d)", rc);
        struct ble_gap_upd_params upd = {
            .itvl_min = 6,               /* 7,5 ms */
            .itvl_max = 12,              /* 15 ms */
            .latency = 0,
            .supervision_timeout = 400,  /* 4 s */
            .min_ce_len = 0,
            .max_ce_len = 0,
        };
        rc = ble_gap_update_params(s_conn_handle, &upd);
        ESP_LOGI(TAG, "reecriture des parametres demandee (rc=%d)", rc);

        s_state = ST_SVC;
        ESP_LOGI(TAG, "debut de la decouverte des services");
        ble_gattc_disc_all_svcs(s_conn_handle, svc_cb, NULL);
        return 0;
    }

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGW(TAG, "DECONNECTE : raison %d", event->disconnect.reason);
        stats_report();
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_state = ST_IDLE;
        vTaskDelay(pdMS_TO_TICKS(1500));
        start_scan();
        return 0;

    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "MTU negociee : %u", event->mtu.value);
        return 0;

    case BLE_GAP_EVENT_CONN_UPDATE:
        if (event->conn_update.status == 0) {
            struct ble_gap_conn_desc d;
            if (ble_gap_conn_find(event->conn_update.conn_handle, &d) == 0) {
                ESP_LOGI(TAG, "parametres de connexion mis a jour : intervalle %.1f ms", d.conn_itvl * 1.25);
            }
        } else {
            ESP_LOGW(TAG, "mise a jour des parametres refusee : status=%d", event->conn_update.status);
        }
        return 0;

    case BLE_GAP_EVENT_NOTIFY_RX: {
        uint8_t buf[64];
        uint16_t len = OS_MBUF_PKTLEN(event->notify_rx.om);
        uint16_t n = len > sizeof(buf) ? sizeof(buf) : len;
        if (os_mbuf_copydata(event->notify_rx.om, 0, n, buf) == 0) {
            s_notify_rx++;
            char hex[3 * sizeof(buf) + 1];
            size_t p = 0;
            for (uint16_t i = 0; i < n && p + 3 < sizeof(hex); i++) {
                p += snprintf(hex + p, sizeof(hex) - p, "%02x", buf[i]);
            }
            ESP_LOGI(TAG, "  notify handle=0x%04x len=%u : %s", event->notify_rx.attr_handle, len,
                     hex);
            /* Crédit de flux : l'imprimante acquitte par `01 05` (et `01 07` au premier flux).
             * ⚠️ Un crédit n'est PAS une fin d'écriture : ne surtout pas libérer le sémaphore
             * d'écriture ici (cela désynchroniserait la sérialisation des écritures). */
            if (n >= 2 && buf[0] == 0x01 && (buf[1] == 0x05 || buf[1] == 0x07)) {
                s_credits++;
            }
        }
        return 0;
    }

    default:
        return 0;
    }
}

static void start_scan(void)
{
    struct ble_gap_disc_params sp = {
        .itvl = 0x0060,       /* 60 ms */
        .window = 0x0030,     /* 30 ms */
        .filter_policy = 0,
        .limited = 0,
        .passive = 0,
        .filter_duplicates = 0,
    };
    int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &sp, gap_event_cb, NULL);
    ESP_LOGI(TAG, "recherche de l'imprimante %02X:%02X:%02X:%02X:%02X:%02X (rc=%d)", s_target.val[5],
             s_target.val[4], s_target.val[3], s_target.val[2], s_target.val[1], s_target.val[0],
             rc);
}

static void host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void on_sync(void)
{
    /* La raison du dernier reset est le premier diagnostic utile : elle dit si l'appli a
     * demarre proprement (POWERON) ou si elle tourne en boucle de plantage (PANIC/WDT). */
    esp_reset_reason_t raison = esp_reset_reason();
    static const char *noms[] = {"INCONNUE",   "POWERON",  "EXT",        "SW",       "PANIC",
                                 "INT_WDT",    "TASK_WDT", "WDT",        "DEEPSLEEP", "BROWNOUT",
                                 "SDIO",       "USB",      "JTAG",       "EFUSE",    "PWR_GLITCH",
                                 "CPU_LOCKUP"};
    ESP_LOGW(TAG, "raison du dernier reset : %s (%d)%s", (raison >= 0 && raison <= 15) ? noms[raison] : "?",
             (int)raison, (raison == ESP_RST_PANIC || raison == ESP_RST_TASK_WDT) ?
             "  <-- PLANTAGE : voir le log ci-dessus" : "");
    int rc = ble_hs_util_ensure_addr(0);
    ESP_LOGI(TAG, "pile BLE synchronisee (ensure_addr rc=%d)", rc);
    start_scan();
}

/* --------------------------------------------------------------------------------------
 * Wi-Fi (facultatif en v0 : sans SSID, on mesure le BLE seul, sans coexistence radio)
 * -------------------------------------------------------------------------------------- */
static void wifi_start(void)
{
    if (strlen(CONFIG_S002_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "aucun SSID configure : mesure BLE SANS Wi-Fi (coexistence non sollicitee)");
        return;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, CONFIG_S002_WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, CONFIG_S002_WIFI_PASSWORD, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Pas de modem sleep : carte alimentée, et le power save Wi-Fi est la première cause de
     * pics de latence — donc de blancs. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_LOGI(TAG, "Wi-Fi demarre (SSID \"%s\", power save DESACTIVE)", CONFIG_S002_WIFI_SSID);
}

static bool parse_mac(const char *s, uint8_t out[6])
{
    unsigned v[6];
    if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        out[i] = (uint8_t)v[i];
    }
    return true;
}

void app_main(void)
{
    ESP_LOGI(TAG, "=== S002 bridge v0 — banc de mesure du lien BLE ===");

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    if (!parse_mac(CONFIG_S002_PRINTER_MAC, s_target.val)) {
        ESP_LOGE(TAG, "MAC illisible : \"%s\"", CONFIG_S002_PRINTER_MAC);
        return;
    }
    s_target.type = BLE_ADDR_RANDOM; /* valeur par défaut ; écrasée par l'annonce réelle */

    s_write_done = xSemaphoreCreateBinary();

    wifi_start();

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    nimble_port_freertos_init(host_task);

    ESP_LOGI(TAG, "memoire libre : %u octets", (unsigned)esp_get_free_heap_size());

    /* Attend que la découverte et les abonnements soient terminés, puis imprime UNE fois.
     * Battement de coeur toutes les 5 s : sans lui, un firmware qui attend son peripherique
     * n'affiche rien et devient indiagnostiquable a distance. */
    int battements = 0;
    while (!s_pret_a_imprimer) {
        vTaskDelay(pdMS_TO_TICKS(200));
        if (++battements % 25 == 0) {
            ESP_LOGI(TAG, "[%d s] en attente de l'imprimante : %u annonces BLE vues, "
                          "meilleur RSSI %d dBm",
                     battements / 5, (unsigned)s_adv_vus, s_rssi_max);
        }
    }
    vTaskDelay(pdMS_TO_TICKS(300));

    if (!s_write_handle_confirmee) {
        ESP_LOGW(TAG, "ATTENTION : le handle d'ecriture configure (0x%04x) n'a pas ete vu dans la "
                      "decouverte — l'ecriture risque de partir dans le vide",
                 CONFIG_S002_WRITE_HANDLE);
    }

    print_test_pattern(CONFIG_S002_WRITE_HANDLE);

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(30000));
        stats_report();
    }
}
