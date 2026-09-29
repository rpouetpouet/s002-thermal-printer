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
#include "esp_ota_ops.h"
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
#include <stdio.h>
#include <string.h>

#include "esp_netif.h"
#include "nvs.h"

#include "lwip/sockets.h"
#include "serveur_tcp.h"
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

/* Contrôle de flux par crédits — MÊME mécanisme que l'intégration HA (ble.py), mais en
 * COMPTABILISANT les paquets au lieu d'attendre à intervalle fixe.
 *
 * L'imprimante autorise les paquets par crédits envoyés sur ff03 : `01 05` = 5 paquets,
 * `01 07` = 7 (crédit initial). On envoie tant qu'on reste dans l'autorisation cumulée, et on
 * attend un crédit dès qu'on la dépasserait. C'est le robinet réel de l'imprimante.
 *
 * ⚠️ Ne PAS remplacer par « une attente tous les N paquets » : mesuré le 29/09/2026, avec N=3
 * les crédits n'arrivent pas encore (un crédit tous les ~5 paquets) -> 3 attentes sur 3 soldées
 * par un échec, contrôle de flux ABANDONNÉ, débit remonté de 9,3 à 11,5 Ko/s et retour des
 * lignes écrasées. N=5 fonctionnait, la comptabilité fait mieux (elle s'adapte au rythme réel).
 *
 * L'ignorer complètement (416 écritures à 12,5 Ko/s sans jamais attendre) donnait de façon
 * INTERMITTENTE des lignes écrasées : même firmware, mêmes données, une fois mangé / une fois
 * propre -> signature d'un débordement de tampon. */
#define FLOW_TIMEOUT_MS 300     /* au-delà : l'imprimante tamponne, on n'attend pas plus */
#define FLOW_ABANDON_APRES 3    /* échecs cumulés avant d'abandonner le contrôle de flux */

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
static uint32_t s_notify_rx;

/* Découverte séquentielle */
enum { ST_IDLE, ST_SVC, ST_CHR, ST_DSC, ST_SUB, ST_PRINT };
static volatile int s_state = ST_IDLE;
static uint16_t s_svc_start[MAX_SVCS], s_svc_end[MAX_SVCS];
static int s_svc_nb, s_svc_i;
static uint16_t s_notify_h[MAX_NOTIFY], s_notify_svc_end[MAX_NOTIFY], s_cccd_h[MAX_NOTIFY];
static int s_notify_trouves, s_notify_i;
static bool s_write_handle_confirmee, s_pret_a_imprimer;

/* ------------------------------------------------------------------------------------------
 * Rendre la liaison a qui la demande (v2, 29/09/2026)
 *
 * L'imprimante n'accepte qu'UN client a la fois. En v1 le noeud appelait ble_gap_connect() une
 * fois au demarrage et `ble_gap_terminate` NULLE PART : il tenait donc l'imprimante en
 * permanence, et un telephone ne pouvait jamais s'y connecter. Prouve par mesure : le compteur
 * d'annonces BLE du noeud restait GELE (3693 -> 3693 sur 40 s) — un noeud qui tient la liaison
 * ne scanne plus.
 *
 * Deux facons de rendre la liaison : apres N secondes sans impression (reglable, 0 = jamais),
 * ou sur ordre explicite du client (`LIBERER`). Dans les deux cas on ne rescanne PAS : sinon le
 * noeud reprendrait la liaison aussitot et le telephone n'aurait toujours rien.
 * --------------------------------------------------------------------------------------- */
static int64_t s_dernier_echange_us;         /* horodatage de la derniere impression */
static volatile bool s_liberation_voulue;    /* vrai = liaison rendue, on ne rescanne pas */
static volatile int s_liberation_auto_s = CONFIG_S002_LIBERATION_INACTIF_S;

/* ------------------------------------------------------------------------------------------
 * Mode MANUEL / AUTO, et niveau de batterie (v3, 29/09/2026)
 *
 * - `s_maintien` : vrai = mode MANUEL, le noeud garde la liaison et le delai d'inactivite est
 *   ignore (c'est ce que demande le bouton cote Home Assistant). Faux = mode AUTO, le delai
 *   decide. En RAM volontairement : le client repose l'etat, un redemarrage revient a l'auto.
 * - `s_batterie_pct` : l'imprimante pousse sa trame d'etat (18 octets, toutes les 5 s) et le
 *   noeud la JETAIT — c'est pour ca que la batterie restait inconnue en mode noeud. Format
 *   identique a celui du chemin proxy (custom_components/s002_printer/ble.py) : en-tete de
 *   5 octets, controle de 5 octets en queue, charge a l'index 7 du corps, soit l'octet 12.
 * --------------------------------------------------------------------------------------- */
static volatile int s_batterie_pct = -1;     /* -1 = jamais recue */
static volatile unsigned s_etats_vus;
static volatile bool s_maintien;

/* Diagnostic : sans ces compteurs, un firmware qui attend son peripherique est TOTALEMENT
 * muet, ce qui est indiagnostiquable a distance (vecu le 29/09/2026 : flash verifie bon,
 * zero octet a la console, cause reelle = rien a journaliser). */
static uint32_t s_adv_vus;
static int8_t s_rssi_max = -127;

/* Contrôle de flux */
static volatile int s_credits;
static volatile int s_paquets;             /* paquets ecrits */
static volatile int s_paquets_autorises;   /* cumul des credits recus, en paquets */
static SemaphoreHandle_t s_credit_event;
static int s_flux_attentes, s_flux_waits, s_flux_timeouts;
static int64_t s_flux_attente_us, s_flux_attente_max_us;
static bool s_flux_abandonne;

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
    ESP_LOGI(TAG, "    flux : %d attentes | %d credits obtenus | %d sans credit | attente moy "
                  "%.0f ms | max %.0f ms %s",
             s_flux_attentes, s_flux_waits, s_flux_timeouts,
             s_flux_waits ? (double)s_flux_attente_us / s_flux_waits / 1000.0 : 0.0,
             s_flux_attente_max_us / 1000.0,
             s_flux_abandonne ? "| CONTROLE DE FLUX ABANDONNE" : "");
    ESP_LOGI(TAG, "    paquets : %d ecrits / %d autorises par l'imprimante", s_paquets,
             s_paquets_autorises);
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
/** Attend un crédit de flux, à l'identique de l'intégration HA (`_attendre_credit`).
 *  Un crédit non reçu dans le délai ne doit JAMAIS bloquer l'impression (l'imprimante
 *  tamponne) : après FLOW_ABANDON_APRES échecs cumulés, le contrôle de flux est abandonné. */
static bool attendre_credit(void)
{
    if (s_flux_abandonne) {
        return false;
    }
    s_flux_attentes++;
    xSemaphoreTake(s_credit_event, 0);     /* attente vierge : les crédits passés sont déjà comptés */
    const int avant = s_paquets_autorises;
    const int64_t t0 = esp_timer_get_time();
    while (s_paquets_autorises == avant) {
        if (xSemaphoreTake(s_credit_event, pdMS_TO_TICKS(FLOW_TIMEOUT_MS)) != pdTRUE) {
            break;
        }
        if (esp_timer_get_time() - t0 >= (int64_t)FLOW_TIMEOUT_MS * 1000) {
            break;
        }
    }
    const int64_t attente = esp_timer_get_time() - t0;
    const bool obtenu = (s_paquets_autorises != avant);
    if (obtenu) {
        s_flux_waits++;
        s_flux_attente_us += attente;
        if (attente > s_flux_attente_max_us) {
            s_flux_attente_max_us = attente;
        }
    } else {
        s_flux_timeouts++;
        if (s_flux_timeouts >= FLOW_ABANDON_APRES) {
            s_flux_abandonne = true;
            ESP_LOGW(TAG, "controle de flux ABANDONNE apres %d attentes sans credit — "
                          "l'imprimante tamponne, on continue sans caler", s_flux_timeouts);
        }
    }
    return obtenu;
}

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
            /* Écriture sans callback : on laisse respirer la pile avant la suivante (120 ms :
             * en dessous, la procédure GATT suivante est refusée avec rc=6). */
            vTaskDelay(pdMS_TO_TICKS(120));
        }
        offset += n;
        s_paquets++;
        /* On n'attend QUE si l'on depasse ce que l'imprimante a autorise : c'est son robinet. */
        int securite = 0;
        while (!s_flux_abandonne && s_paquets > s_paquets_autorises && securite++ < 64) {
            if (!attendre_credit()) {
                break;
            }
        }
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
#ifndef CONFIG_S002_MODE_SERVEUR
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
 * Test LONG : le raster de la recette, embarque dans le binaire (tools/make_recipe.py)
 *
 * Le motif de 24 lignes ne couvre que 2 mm de papier : il prouve que les tranches se
 * raccordent, pas que le debit tient sur une longue impression. Ici : 1099 lignes = 93 mm.
 *
 * ⚠️ LEÇON MESURÉE (29/09/2026) — c'est le DÉBIT EN TROP qui casse le papier, pas le manque :
 *   * 20 mm/s x 11,81 lignes/mm x 72 o = 17 006 o/s est la vitesse MAXIMALE de la tête, pas sa
 *     consommation réelle : l'imprimante se règle elle-même par crédits de flux et consomme en
 *     vrai ~9 000 o/s (mesuré : 83 crédits pour 79 128 o, soit un crédit tous les ~5 paquets) ;
 *   * en poussant 12 500 o/s SANS respecter les crédits, le tampon déborde et les lignes
 *     sortent ÉCRASÉES — et de façon INTERMITTENTE (même firmware, même données : une fois
 *     mangé, une fois propre). 12 écritures passaient, 416 non ;
 *   * en respectant les crédits, le débit se cale à ~9 200 o/s et le papier sort propre.
 * Le proxy, lui, délivrait 1 700 o/s : bien plus lent que la consommation réelle -> blancs.
 * -------------------------------------------------------------------------------------- */
extern const uint8_t recette_bin_start[] asm("_binary_recette_bin_start");
extern const uint8_t recette_bin_end[] asm("_binary_recette_bin_end");

#define DEBIT_IMPRIMANTE_O_S 17006.0  /* 20 mm/s x 11,81 lignes/mm x 72 o */

static void print_recette(uint8_t write_handle)
{
    const size_t octets = (size_t)(recette_bin_end - recette_bin_start);
    const int lignes_total = (int)(octets / BYTES_PER_LINE);
    const int par_trame = CONFIG_S002_LINES_PER_FRAME;
    const int total_tranches = (lignes_total + par_trame - 1) / par_trame;

    ESP_LOGI(TAG, "--- TEST LONG : recette %d lignes = %.1f mm = %u octets, %d tranches ---",
             lignes_total, lignes_total / 11.81, (unsigned)octets, total_tranches);
    ESP_LOGI(TAG, "    l'imprimante consomme %.0f o/s ; le lien doit faire au moins autant",
             DEBIT_IMPRIMANTE_O_S);

    const uint8_t tok = 0x01;
    send_frame(write_handle, MSG_TOKEN, &tok, 1);
    vTaskDelay(pdMS_TO_TICKS(SETTLE_AFTER_TOKEN_MS));
    const uint8_t largeur[2] = {0x40, 0x02};
    send_frame(write_handle, MSG_PAPER_SIZE, largeur, 2);
    vTaskDelay(pdMS_TO_TICKS(SETTLE_AFTER_WIDTH_MS));

    const int64_t t_debut = esp_timer_get_time();
    int64_t ecoule = 0;
    int tranche = 0;
    for (int i = 0; i < lignes_total; i += par_trame) {
        int n = (lignes_total - i) < par_trame ? (lignes_total - i) : par_trame;
        send_frame(write_handle, MSG_IMAGE_SLICE, &recette_bin_start[i * BYTES_PER_LINE],
                   (size_t)n * BYTES_PER_LINE);
        tranche++;
        if (tranche % 20 == 0) {
            ecoule = esp_timer_get_time() - t_debut;
            double debit = (double)((size_t)tranche * par_trame * BYTES_PER_LINE) * 1e6 / ecoule;
            ESP_LOGI(TAG, "    %d/%d tranches | %.2f s | %.0f o/s | %s",
                     tranche, total_tranches, ecoule / 1e6, debit,
                     debit < DEBIT_IMPRIMANTE_O_S ? "EN RETARD" : "dans les temps");
        }
    }
    int64_t duree = esp_timer_get_time() - t_debut;

    const uint8_t avance[2] = {200, 0};
    send_frame(write_handle, MSG_FEED, avance, 2);

    double debit_final = (double)octets * 1e6 / (double)duree;
    ESP_LOGI(TAG, "=== RECETTE ENVOYEE : %u o en %.2f s = %.0f o/s (besoin %.0f o/s) ===",
             (unsigned)octets, duree / 1e6, debit_final, DEBIT_IMPRIMANTE_O_S);
    ESP_LOGI(TAG, "  debit calcule sur la vitesse MAX de la tete : %.0f%% "
                  "(le papier se juge sur le respect des credits, pas sur ce pourcentage)",
             100.0 * debit_final / DEBIT_IMPRIMANTE_O_S);
    if (s_flux_timeouts > 0) {
        ESP_LOGW(TAG, "  %d attente(s) de credit sans reponse : debit probablement trop eleve",
                 s_flux_timeouts);
    }
    stats_report();
}
#endif /* !CONFIG_S002_MODE_SERVEUR : fin du banc de mesure (motif de test + recette) */

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
    /* On ne s'abonne QU'AUX canaux notify du service qui porte le handle d'écriture.
     * L'imprimante expose ff00 DEUX fois (une copie « service0001 » sans usage) : s'abonner
     * aux 4 canaux faisait attendre des écritures sur les descripteurs de la copie inutilisée
     * — 3 timeouts de 3 s avant chaque impression (mesuré le 29/09/2026). */
    const bool service_du_canal =
        (CONFIG_S002_WRITE_HANDLE >= s_svc_start[s_svc_i]) &&
        (CONFIG_S002_WRITE_HANDLE <= s_svc_end[s_svc_i]);
    if ((chr->properties & BLE_GATT_CHR_PROP_NOTIFY) && s_notify_trouves < MAX_NOTIFY) {
        if (!service_du_canal) {
            ESP_LOGI(TAG, "  notify 0x%04x ignore : service 0x%04x..0x%04x sans usage (copie de ff00)",
                     chr->val_handle, s_svc_start[s_svc_i], s_svc_end[s_svc_i]);
            return 0;
        }
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
                /* ⚠️ mesurer=false : NimBLE ne rappelle PAS le callback de fin pour une écriture
                 * de descripteur CCCD — mesurer=true ne rapportait donc que des timeouts de 3 s
                 * (2 x 3 s perdus avant chaque impression, mesuré le 29/09/2026), alors que
                 * l'abonnement fonctionnait bel et bien. On écrit donc sans mesure, en laissant
                 * 120 ms entre deux abonnements pour ne pas empiler deux procédures GATT. */
                ble_write_block(s_cccd_h[s_notify_i], cccd_val, 2, false);
            } else {
                ESP_LOGW(TAG, "  notify 0x%04x sans CCCD : pas d'abonnement possible",
                         s_notify_h[s_notify_i]);
            }
            s_notify_i++;
        }
        ESP_LOGI(TAG, "  abonnements termines — pret a imprimer");
        s_pret_a_imprimer = true;
        /* L'horloge d'inactivite part de la CONNEXION et non de 0 : sans ca, le premier passage
         * du chien de garde croirait l'imprimante inactive depuis toujours et la libererait
         * aussitot apres l'avoir prise. */
        s_dernier_echange_us = esp_timer_get_time();
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

static void liberer_liaison(const char *raison)
{
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        ESP_LOGI(TAG, "liberation demandee (%s) : la liaison est deja rendue", raison);
        return;
    }
    /* On note l'intention AVANT de couper : le gestionnaire de deconnexion la relit pour ne
     * PAS relancer la recherche (sinon on reprendrait l'imprimante immediatement). */
    s_liberation_voulue = true;
    ESP_LOGW(TAG, "LIBERATION de l'imprimante (%s) : elle redevient visible pour un telephone",
             raison);
    ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
}

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
            .itvl_min = CONFIG_S002_ITVL_MIN,  /* 6 = 7,5 ms : le levier principal */
            .itvl_max = CONFIG_S002_ITVL_MAX,  /* 12 = 15 ms par defaut */
            .latency = 0,
            .supervision_timeout = 400,  /* 400 x 10 ms = 4 s */
            .min_ce_len = 0,
            .max_ce_len = 0,
        };
        int rc = ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &s_target, 5000, &cp, gap_event_cb, NULL);
        ESP_LOGI(TAG, "connexion demandee (rc=%d) : intervalle demande %.1f-%.1f ms", rc,
                 CONFIG_S002_ITVL_MIN * 1.25, CONFIG_S002_ITVL_MAX * 1.25);
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
            .itvl_min = CONFIG_S002_ITVL_MIN,
            .itvl_max = CONFIG_S002_ITVL_MAX,
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
        if (s_liberation_voulue) {
            /* Liberation VOLONTAIRE : surtout ne pas relancer la recherche, sinon le noeud
             * reprendrait l'imprimante dans la seconde et elle resterait inaccessible au
             * telephone. C'est la prochaine impression qui declenchera la reprise. */
            s_pret_a_imprimer = false;
            ESP_LOGW(TAG, "imprimante RENDUE — elle est de nouveau annoncee et connectable par un "
                          "autre appareil (telephone) ; le noeud la reprendra a la prochaine "
                          "impression");
            return 0;
        }
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
            if (n >= 2 && buf[0] == 0x01 && buf[1] > 0) {
                /* Le second octet est le NOMBRE DE PAQUETS autorises : `01 05` = 5,
                 * `01 07` = 7 (credit initial). On cumule. */
                s_credits++;
                s_paquets_autorises += buf[1];
                xSemaphoreGive(s_credit_event);
            }
            /* Trame d'ETAT (18 octets) : elle porte le niveau de batterie. Le noeud la jetait,
             * d'ou une batterie toujours inconnue en mode noeud alors que le chemin proxy la
             * lisait tres bien. On applique la MEME regle que ble.py : l'octet 12 est la
             * charge en pourcent, encadre d'un en-tete et d'un controle de 5 octets chacun.
             * Le garde `n >= 13` est celui de ble.py ; et on ne retient que 1..100, sinon une
             * trame d'un autre type ecraserait une valeur valide. */
            if (n >= 13) {
                uint8_t charge = buf[12];
                s_etats_vus++;
                if (charge > 0 && charge <= 100) {
                    if (s_batterie_pct != (int)charge) {
                        ESP_LOGI(TAG, "  batterie : %u %%", (unsigned)charge);
                    }
                    s_batterie_pct = (int)charge;
                }
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
/* --------------------------------------------------------------------------------------
 * Provisionnement Wi-Fi : les identifiants vivent en NVS, PAS dans le binaire.
 *
 * Pourquoi : un mot de passe Wi-Fi compile en dur finit dans l'image, dans l'historique de
 * build et dans les sauvegardes. Ici on l'ecrit une fois par la console USB
 * (`WIFI <ssid> <motdepasse>`), sur la machine qui a le cable — le secret ne transite par
 * aucun chat, aucun depot, aucun fichier de configuration versionne.
 * ------------------------------------------------------------------------------------ */
#define NVS_ESPACE_WIFI "s002"

static bool nvs_lire_wifi(char *ssid, size_t taille_ssid, char *pass, size_t taille_pass)
{
    nvs_handle_t h;
    if (nvs_open(NVS_ESPACE_WIFI, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t l1 = taille_ssid, l2 = taille_pass;
    bool ok = (nvs_get_str(h, "ssid", ssid, &l1) == ESP_OK) && l1 > 1;
    if (ok) {
        /* Un mot de passe absent est legitime (reseau ouvert) : on ne le traite pas en echec. */
        if (nvs_get_str(h, "pass", pass, &l2) != ESP_OK) {
            pass[0] = '\0';
        }
    }
    nvs_close(h);
    return ok;
}

static esp_err_t nvs_ecrire_wifi(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_ESPACE_WIFI, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(h, "ssid", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "pass", pass);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

/* Le serveur TCP est demarre par l'evenement « adresse IP obtenue », jamais avant : lwIP
 * n'existe qu'a partir de la, et appeler socket() trop tot fait planter la pile
 * (assert tcpip_send_msg_wait_sem, « Invalid mbox ») — constate au premier flash de la v1. */
static bool tcp_reception(const uint8_t *donnees, size_t longueur);
static void tcp_commande(const char *commande, char *reponse, size_t taille);
static bool s_serveur_demarre;

/* ------------------------------------------------------------------------------------------
 * Mise a jour du firmware par le RESEAU (v2, 29/09/2026)
 *
 * Pourquoi : atteindre le port USB du C3 demande de demonter l'imprimante. Le premier flash
 * « compatible OTA » se fait donc par USB, et tous les suivants passent par le reseau.
 *
 * Protocole : le client envoie « OTA <octets> » en mode texte, attend « OTA PRET », puis envoie
 * le binaire brut. L'ecriture va dans la partition INACTIVE : une coupure au milieu ne peut pas
 * casser le firmware qui tourne (le redemarrage n'a lieu qu'apres une image validee).
 * --------------------------------------------------------------------------------------- */
static int ota_recevoir(int fd, size_t octets)
{
    const esp_partition_t *cible = esp_ota_get_next_update_partition(NULL);
    if (cible == NULL) {
        ESP_LOGE(TAG, "OTA : aucune partition inactive — la table de partitions n'a pas d'ota_1 ?");
        return -1;
    }
    ESP_LOGW(TAG, "OTA : %u octets a ecrire dans '%s' (0x%lx), version qui tourne = '%s'",
             (unsigned)octets, cible->label, (unsigned long)cible->address,
             esp_ota_get_running_partition()->label);

    esp_ota_handle_t ota = 0;
    esp_err_t err = esp_ota_begin(cible, octets, &ota);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA : esp_ota_begin -> %s", esp_err_to_name(err));
        return -1;
    }
    /* Tampon STATIQUE : 4 Ko sur la pile d'une tache de 5 Ko la fait deborder (deja vecu sur ce
     * meme serveur TCP le 29/09/2026). */
    static uint8_t bloc[4096];
    size_t reste = octets;
    while (reste > 0) {
        size_t demande = reste < sizeof bloc ? reste : sizeof bloc;
        int n = recv(fd, bloc, demande, 0);
        if (n <= 0) {
            ESP_LOGE(TAG, "OTA : flux interrompu, %u octets manquants", (unsigned)reste);
            esp_ota_abort(ota);
            return -1;
        }
        err = esp_ota_write(ota, bloc, (size_t)n);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "OTA : esp_ota_write -> %s", esp_err_to_name(err));
            esp_ota_abort(ota);
            return -1;
        }
        reste -= (size_t)n;
    }
    err = esp_ota_end(ota);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA : image refusee -> %s", esp_err_to_name(err));
        return -1;
    }
    err = esp_ota_set_boot_partition(cible);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA : partition de demarrage -> %s", esp_err_to_name(err));
        return -1;
    }
    ESP_LOGW(TAG, "OTA : image acceptee — redemarrage dans 1 s");
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
    return 0;   /* jamais atteint */
}

static void sur_ip_obtenue(void *arg, esp_event_base_t base, int32_t id, void *donnees)
{
    (void)arg; (void)base; (void)id;
    const ip_event_got_ip_t *evenement = (const ip_event_got_ip_t *)donnees;
    char ip_txt[16];
    ESP_LOGI(TAG, "Wi-Fi : adresse IP %s",
             esp_ip4addr_ntoa(&evenement->ip_info.ip, ip_txt, sizeof ip_txt));

    if (s_serveur_demarre) {
        return;
    }
    if (serveur_tcp_demarrer(CONFIG_S002_PORT_TCP, tcp_reception, tcp_commande, ota_recevoir) == ESP_OK) {
        s_serveur_demarre = true;
        ESP_LOGI(TAG, "serveur TCP en ecoute sur le port %d", CONFIG_S002_PORT_TCP);
    } else {
        ESP_LOGE(TAG, "serveur TCP : demarrage impossible sur le port %d", CONFIG_S002_PORT_TCP);
    }
}

static esp_netif_t *s_netif;

static const char *s_source_wifi = "aucune";
static int s_wifi_echecs;
static int s_derniere_raison_wifi = -1;
static char s_ssid[33];
static bool s_balayage_demande;
static bool s_wifi_init, s_wifi_demarre, s_wifi_associe;
static int s_dernier_evenement_wifi = -1;

/* Balayage Wi-Fi : la mesure qui tranche. « Pas d'adresse IP » ne dit pas si le reseau cible
 * est seulement visible depuis l'endroit ou le noeud est pose (le C3 ne fait que du 2,4 GHz :
 * un reseau en 5 GHz seul est tout simplement invisible). On liste donc ce qui est vu. */
static void balayer_wifi(void)
{
    wifi_scan_config_t cfg = { .show_hidden = false };
    if (esp_wifi_scan_start(&cfg, true) != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi : balayage impossible");
        return;
    }
    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    ESP_LOGI(TAG, "Wi-Fi : %u reseau(x) 2,4 GHz visible(s) depuis le noeud", (unsigned)n);
    if (n > 0) {
        if (n > 16) {
            n = 16;
        }
        wifi_ap_record_t *aps = calloc(n, sizeof(wifi_ap_record_t));
        if (aps != NULL && esp_wifi_scan_get_ap_records(&n, aps) == ESP_OK) {
            for (int i = 0; i < n; i++) {
                bool le_notre = (s_ssid[0] != '\0')
                                && (strcmp((const char *)aps[i].ssid, s_ssid) == 0);
                ESP_LOGI(TAG, "    %s\"%s\" canal %d RSSI %d dBm", le_notre ? ">>> " : "    ",
                         (const char *)aps[i].ssid, aps[i].primary, aps[i].rssi);
            }
        }
        free(aps);
    }
    if (s_ssid[0] != '\0') {
        ESP_LOGI(TAG, "  reseau recherche : \"%s\"", s_ssid);
    }
}

static void tache_balayage(void *param)
{
    (void)param;
    balayer_wifi();
    vTaskDelete(NULL);
}

/* Surveillance de demarrage : si aucune adresse IP au bout de 8 s, on VEUT savoir pourquoi (le
 * reseau cible est-il seulement visible ?). Conditionner ce balayage a un compteur d'echecs
 * etait une erreur : sans evenement de deconnexion, il ne se declenchait jamais. */
static void tache_surveillance_reseau(void *param)
{
    (void)param;
    for (int tour = 0; tour < 6 && !s_serveur_demarre; tour++) {
        vTaskDelay(pdMS_TO_TICKS(8000));
        if (s_serveur_demarre) {
            vTaskDelete(NULL);
            return;
        }
        ESP_LOGW(TAG, "Wi-Fi : pas d'adresse IP apres %d s (init=%d demarre=%d associe=%d, "
                      "dernier evenement=%d) : relance de l'association puis balayage",
                 8 * (tour + 1), s_wifi_init, s_wifi_demarre, s_wifi_associe,
                 s_dernier_evenement_wifi);
        esp_wifi_connect();
        balayer_wifi();
    }
    vTaskDelete(NULL);
}

/* Evenements Wi-Fi. Le CODE DE RAISON est l'information decisive :
 *   15  = mot de passe refuse (handshake)
 *   201 = point d'acces introuvable (hors portee, ou reseau uniquement en 5 GHz — le C3 ne
 *         fait que du 2,4 GHz)
 *   205 = point d'acces sature
 * Sans lui, « pas d'adresse IP » ne dit pas s'il faut corriger le mot de passe ou demenager
 * l'antenne : on ne peut que constater. */
static void sur_evenement_wifi(void *arg, esp_event_base_t base, int32_t id, void *donnees)
{
    (void)arg;
    (void)base;
    s_dernier_evenement_wifi = (int)id;
    if (id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "Wi-Fi : interface prete -> demande d'association");
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_CONNECTED) {
        s_wifi_associe = true;
        ESP_LOGI(TAG, "Wi-Fi : associe (attente du bail DHCP)");
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *e = (const wifi_event_sta_disconnected_t *)donnees;
        s_wifi_associe = false;
        s_wifi_echecs++;
        s_derniere_raison_wifi = (int)e->reason;
        /* On ne journalise pas les tentatives a l'infini : les 5 premieres suffisent a
         * diagnostiquer, ensuite on espace pour ne pas noyer le journal. */
        if (s_wifi_echecs <= 5 || s_wifi_echecs % 20 == 0) {
            ESP_LOGW(TAG, "Wi-Fi : association ECHOUEE, raison %d (essai %d)",
                     (int)e->reason, s_wifi_echecs);
        }
        if (s_wifi_echecs == 5 && !s_balayage_demande) {
            s_balayage_demande = true;
            /* Une seule fois : au-dela, le balayage lui-meme perturbe les tentatives. */
            xTaskCreate(tache_balayage, "balayage_wifi", 4096, NULL, 4, NULL);
        }
        if (s_wifi_echecs < 100) {
            esp_wifi_connect();  /* une coupure passagere ne doit pas laisser le noeud muet */
        }
    }
}

/* Etat reseau, journalisable A TOUT MOMENT. La ligne unique du demarrage ne suffit pas : une
 * capture qui s'attache apres le boot ne la voit jamais, et on se retrouve a ne pas savoir si
 * le noeud a une adresse IP ou non (vecu le 29/09/2026). */
static void journal_reseau(void)
{
    char ip_txt[16] = "aucune";
    if (s_netif != NULL) {
        esp_netif_ip_info_t ip = {0};
        if (esp_netif_get_ip_info(s_netif, &ip) == ESP_OK && ip.ip.addr != 0) {
            esp_ip4addr_ntoa(&ip.ip, ip_txt, sizeof ip_txt);
        }
    }
    ESP_LOGI(TAG, "reseau : identifiants=%s ip=%s serveur=%s (port %d), imprimante=%s",
             s_source_wifi, ip_txt, s_serveur_demarre ? "en ecoute" : "inactif",
             CONFIG_S002_PORT_TCP, s_pret_a_imprimer ? "prete" : "pas trouvee");
    ESP_LOGI(TAG, "reseau : wifi init=%d demarre=%d associe=%d evenement=%d echecs=%d raison=%d",
             s_wifi_init, s_wifi_demarre, s_wifi_associe, s_dernier_evenement_wifi,
             s_wifi_echecs, s_derniere_raison_wifi);
}

static void wifi_start(void)
{
    char ssid[33] = {0}, pass[65] = {0};
    /* NVS d'abord (provisionnement sur place), Kconfig ensuite (banc de mesure). */
    if (nvs_lire_wifi(ssid, sizeof ssid, pass, sizeof pass)) {
        s_source_wifi = "NVS";
        ESP_LOGI(TAG, "Wi-Fi : identifiants lus en NVS (SSID de %u caracteres)",
                 (unsigned)strlen(ssid));
    } else {
        s_source_wifi = "Kconfig";
        strncpy(ssid, CONFIG_S002_WIFI_SSID, sizeof ssid - 1);
        strncpy(pass, CONFIG_S002_WIFI_PASSWORD, sizeof pass - 1);
    }

    if (strlen(ssid) == 0) {
        /* Sans identifiants, la pile reseau n'est pas initialisee : demarrer le serveur ici
         * ferait planter lwIP. On ne demarre donc rien et on dit comment provisionner. */
        ESP_LOGW(TAG, "aucun SSID : le noeud ne sera PAS joignable en TCP");
        ESP_LOGW(TAG, "  provisionner par la console USB : WIFI <ssid> <motdepasse>");
        return;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Nom DHCP : le noeud apparait sous un nom lisible dans la liste des baux, ce qui evite
     * d'avoir a retrouver son IP au hasard. */
    s_netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(s_netif, "s002-noeud");
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                              sur_ip_obtenue, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                              sur_evenement_wifi, NULL));

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    s_wifi_init = true;
    strncpy(s_ssid, ssid, sizeof s_ssid - 1);
    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, pass, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    s_wifi_demarre = true;
    xTaskCreate(tache_surveillance_reseau, "surveillance_reseau", 4096, NULL, 4, NULL);

    /* Pas de modem sleep : carte alimentée, et le power save Wi-Fi est la première cause de
     * pics de latence — donc de blancs. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "Wi-Fi demarre (SSID \"%s\", power save DESACTIVE) : attente de l'adresse IP", ssid);
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

/* --------------------------------------------------------------------------------------
 * Serveur TCP (v1) : le noeud devient joignable sur le reseau local.
 *
 * Le partage des roles ne change pas : le client (Home Assistant, ou bien l'outil
 * tools/s002_node_client.py du depot) construit les trames YK, et le noeud se contente de les
 * ecrire en Bluetooth — c'est LUI qui gere les credits de flux annonces par l'imprimante.
 * ------------------------------------------------------------------------------------ */
static bool tcp_reception(const uint8_t *donnees, size_t longueur)
{
    s_dernier_echange_us = esp_timer_get_time();

    if (!s_pret_a_imprimer) {
        if (s_liberation_voulue) {
            ESP_LOGI(TAG, "impression demandee : reprise de l'imprimante (elle avait ete rendue)");
            s_liberation_voulue = false;
            start_scan();
        }
        /* On ATTEND la liaison au lieu de jeter le bloc : un bloc jete trouerait le raster envoye
         * par le client (l'image sortirait avec une bande manquante, en silence). Le client, lui,
         * patiente sur son socket — c'est prevu, il n'envoie sa suite qu'apres avoir recu ses
         * credits. 10 s de garde : au-dela, c'est presque toujours une imprimante eteinte. */
        int attente_ms = 0;
        while (!s_pret_a_imprimer && attente_ms < 10000) {
            vTaskDelay(pdMS_TO_TICKS(100));
            attente_ms += 100;
        }
        if (!s_pret_a_imprimer) {
            ESP_LOGE(TAG, "imprimante injoignable apres %d s : bloc de %u octets refuse "
                          "(imprimante eteinte, hors portee, ou deja prise par un telephone)",
                     attente_ms / 1000, (unsigned)longueur);
            return false;
        }
        ESP_LOGI(TAG, "imprimante reprise en %.1f s", attente_ms / 1000.0);
    }
    /* mesurer = true : les compteurs de debit alimentent le journal periodique. */
    return ble_write_block(CONFIG_S002_WRITE_HANDLE, donnees, longueur, true);
}

/* Reponse du noeud a une commande texte : ce qu'il SAIT de lui-meme. Utile au diagnostic,
 * mais ce n'est PAS une preuve d'impression (seule la trame d'etat 0x10 de l'imprimante
 * l'est). */
static void tcp_commande(const char *commande, char *reponse, size_t taille)
{
    if (strcasecmp(commande, "PING") == 0) {
        snprintf(reponse, taille, "PONG");
    } else if (strcasecmp(commande, "LIBERER") == 0) {
        /* Rendre l'imprimante a l'instant : c'est ce que Home Assistant expose par un bouton.
         * On repasse AUSSI en mode auto : rendre la liaison en mode manuel serait contradictoire
         * (le mode manuel dit « garde-la »), et le bouton « liberer » veut dire « laisse-la
         * partir ». L'entite cote Home Assistant relit `maintien=` sur ce meme STATUS, donc son
         * interrupteur se remet tout seul sur auto. */
        bool tenue = (s_conn_handle != BLE_HS_CONN_HANDLE_NONE);
        s_maintien = false;
        liberer_liaison("demande du client");
        snprintf(reponse, taille, tenue ? "LIBERE" : "DEJA_LIBRE");
    } else if (strncasecmp(commande, "MAINTENIR ", 10) == 0) {
        /* Mode MANUEL (1) : le noeud garde la liaison, le delai d'inactivite est ignore.
         * Mode AUTO (0) : le delai decide. Passer en manuel alors que la liaison est rendue la
         * reprend tout de suite — sinon l'interrupteur afficherait « maintenu » sans effet. */
        s_maintien = (atoi(commande + 10) != 0);
        ESP_LOGW(TAG, "mode %s (delai d'inactivite %s)", s_maintien ? "MANUEL" : "auto",
                 s_maintien ? "ignore" : "actif");
        if (s_maintien && s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
            s_liberation_voulue = false;
            start_scan();
            snprintf(reponse, taille, "MAINTIEN ACTIF RECHERCHE");
        } else {
            snprintf(reponse, taille, s_maintien ? "MAINTIEN ACTIF" : "MAINTIEN INACTIF");
        }
    } else if (strcasecmp(commande, "CONNECTER") == 0) {
        s_liberation_voulue = false;
        if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
            start_scan();
            snprintf(reponse, taille, "RECHERCHE");
        } else {
            snprintf(reponse, taille, "DEJA_CONNECTE");
        }
    } else if (strncasecmp(commande, "LIBERATION ", 11) == 0) {
        /* Reglage a chaud du delai d'inactivite : evite de reflasher pour passer de 120 s a 5 min.
         * Volontairement en RAM et non en NVS : c'est le client qui le repose, et une valeur
         * oubliee ne doit pas survivre a un redemarrage (le defaut du firmware reste la reference). */
        int secondes = atoi(commande + 11);
        if (secondes < 0) {
            secondes = 0;
        }
        s_liberation_auto_s = secondes;
        ESP_LOGW(TAG, "delai de liberation regle a %d s (0 = jamais)", s_liberation_auto_s);
        snprintf(reponse, taille, "LIBERATION %dS", s_liberation_auto_s);
    } else if (strcasecmp(commande, "STATUS") == 0) {
        int64_t inactif_us = s_dernier_echange_us > 0 ? esp_timer_get_time() - s_dernier_echange_us : 0;
        /* La partition qui tourne est exposee volontairement : c'est la PREUVE qu'une mise a
         * jour par le reseau a bien ete prise en compte (ota_0 -> ota_1), sans avoir a ouvrir
         * l'imprimante ni a brancher un cable. */
        /* `batterie=-1` = aucune trame d'etat recue depuis le demarrage (l'imprimante les
         * pousse toutes les 5 s quand la liaison est tenue). `etats=` permet de distinguer
         * « aucune trame » de « trames recues mais charge illisible ». */
        snprintf(reponse, taille,
                 "etat=%s liaison=%s mode=%s maintien=%s batterie=%d etats=%u partition=%s "
                 "inactif=%llds liberation=%ds paquets=%d autorises=%d credits=%d attentes=%d "
                 "timeouts=%d ecritures=%u annonces=%u rssi=%d memoire=%u",
                 s_pret_a_imprimer ? "pret" : "attente",
                 s_conn_handle != BLE_HS_CONN_HANDLE_NONE ? "tenue" : "libre",
                 s_maintien ? "manuel" : "auto", s_maintien ? "oui" : "non",
                 s_batterie_pct, (unsigned)s_etats_vus,
                 esp_ota_get_running_partition()->label,
                 (long long)(inactif_us / 1000000), s_liberation_auto_s, s_paquets,
                 s_paquets_autorises, s_credits, s_flux_attentes, s_flux_timeouts,
                 (unsigned)s_write_count, (unsigned)s_adv_vus, s_rssi_max,
                 (unsigned)esp_get_free_heap_size());
    } else {
        snprintf(reponse, taille, "ERREUR commande_inconnue");
    }
}

/* Console USB : sert UNE fois, a provisionner le reseau sans jamais ecrire le mot de passe
 * dans le binaire (voir nvs_ecrire_wifi). Le mot de passe n'est jamais journalise. */
static void console_task(void *param)
{
    char ligne[160];
    (void)param;
    ESP_LOGI(TAG, "console : WIFI <ssid> <motdepasse>  pour provisionner,  INFO  pour l'etat");
    while (fgets(ligne, sizeof ligne, stdin) != NULL) {
        size_t n = strlen(ligne);
        while (n > 0 && (ligne[n - 1] == '\n' || ligne[n - 1] == '\r')) {
            ligne[--n] = '\0';
        }
        if (n == 0) {
            continue;
        }
        if (strncasecmp(ligne, "WIFI ", 5) == 0) {
            char *ssid = ligne + 5;
            char *pass = strchr(ssid, ' ');
            if (pass != NULL) {
                *pass++ = '\0';
            }
            esp_err_t err = nvs_ecrire_wifi(ssid, pass != NULL ? pass : "");
            ESP_LOGI(TAG, "provisionnement Wi-Fi : ssid=\"%s\" -> %s", ssid, esp_err_to_name(err));
            if (err == ESP_OK) {
                /* Pas de redemarrage : on bascule la station a chaud, la session BLE en cours
                 * n'est pas interrompue. */
                wifi_config_t wc = {0};
                strncpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
                strncpy((char *)wc.sta.password, pass != NULL ? pass : "",
                        sizeof(wc.sta.password) - 1);
                esp_wifi_set_config(WIFI_IF_STA, &wc);
                esp_wifi_disconnect();
                esp_wifi_connect();
                ESP_LOGI(TAG, "Wi-Fi : reconnexion demandee");
            }
        } else if (strcasecmp(ligne, "INFO") == 0) {
            char tampon[192];
            serveur_tcp_etat(tampon, sizeof tampon);
            ESP_LOGI(TAG, "etat : %s", tampon);
        } else {
            ESP_LOGW(TAG, "commande inconnue : \"%s\" (attendu : WIFI <ssid> <motdepasse> | INFO)",
                     ligne);
        }
    }
    vTaskDelete(NULL);
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
    s_credit_event = xSemaphoreCreateBinary();
    s_paquets_autorises = CONFIG_S002_FLOW_AUTORISATION_INITIALE;

    wifi_start();

    /* Le serveur TCP n'est PAS demarre ici : il l'est par sur_ip_obtenue (voir wifi_start),
     * seul moment ou la pile reseau existe reellement. */
    xTaskCreate(console_task, "console", 4096, NULL, 3, NULL);

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
            journal_reseau();
        }
    }
    vTaskDelay(pdMS_TO_TICKS(300));

    if (!s_write_handle_confirmee) {
        ESP_LOGW(TAG, "ATTENTION : le handle d'ecriture configure (0x%04x) n'a pas ete vu dans la "
                      "decouverte — l'ecriture risque de partir dans le vide",
                 CONFIG_S002_WRITE_HANDLE);
    }

#ifdef CONFIG_S002_MODE_SERVEUR
    /* Mode noeud reseau : on n'imprime rien de nous-memes, c'est le client TCP qui commande.
     * Le banc de mesure reste accessible en desactivant cette option. */
    ESP_LOGI(TAG, "mode serveur : en attente des trames du client (port %d)", CONFIG_S002_PORT_TCP);
#else
    if (CONFIG_S002_TEST_RECETTE) {
        print_recette(CONFIG_S002_WRITE_HANDLE);
    } else {
        print_test_pattern(CONFIG_S002_WRITE_HANDLE);
    }
#endif

    /* Boucle de veille : cadence de 5 s pour que le delai de liberation soit respecte a peu
     * pres, journal toutes les 30 s pour ne pas noyer la console. */
    int battements_veille = 0;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        if (!s_maintien && s_liberation_auto_s > 0 && !s_liberation_voulue && s_pret_a_imprimer &&
            s_conn_handle != BLE_HS_CONN_HANDLE_NONE && s_dernier_echange_us > 0 &&
            (esp_timer_get_time() - s_dernier_echange_us) >
                (int64_t)s_liberation_auto_s * 1000000) {
            liberer_liaison("inactivite");
        }
        if (++battements_veille % 6 == 0) {
            journal_reseau();
            stats_report();
        }
    }
}
