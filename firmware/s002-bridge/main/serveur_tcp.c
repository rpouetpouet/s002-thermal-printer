/* Serveur TCP du noeud d'impression S002.
 *
 * Le noeud reste BETEMENT simple, volontairement : il ne connait ni le protocole YK ni la
 * rasterisation. Un client (Home Assistant, ou tools/s002_node_client.py) lui envoie les
 * trames deja construites, et c'est ici qu'on les ecrit en Bluetooth avec le controle de flux.
 *
 * Deux modes sur la meme connexion, tranches par le PREMIER octet recu :
 *   * 0x64 -> flux BINAIRE : chaque bloc recu part tel quel vers l'imprimante ;
 *   * autre -> flux TEXTUEL : une commande par ligne (`PING`, `STATUS`).
 * Un flux ne change jamais de nature en cours de connexion, ce qui evite toute ambiguite.
 *
 * UN SEUL client a la fois : deux flux binaires entrelaces garberaient les trames de
 * l'imprimante (c'est deja la raison du verrou cote Home Assistant).
 */

#include "serveur_tcp.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include <sys/time.h>

#include "esp_log.h"

static const char *TAG = "s002_tcp";

#define TAILLE_TAMPON 4096
#define TAILLE_LIGNE 256
/* 120 s et non 30 s : une impression longue peut se derouler sans qu'un seul octet circule
 * (le client attend que le noeud ait fini d'ecrire en BLE). Un delai trop court couperait une
 * impression en cours. */
#define TIMEOUT_RECV_S 120
#define PREMIER_OCTET_TRAME 0x64

static s002_reception_cb_t s_reception;
static s002_commande_cb_t s_commande;
static s002_ota_cb_t s_ota;
static volatile bool s_client_actif;
static volatile bool s_demarre;
static uint32_t s_clients;
static uint32_t s_octets;
static uint32_t s_blocs;
static char s_derniere_cmd[TAILLE_LIGNE];

/* ------------------------------------------------------------------------------------ */

static void envoyer_texte(int fd, const char *texte)
{
    size_t reste = strlen(texte);
    const char *p = texte;
    while (reste > 0) {
        int n = send(fd, p, reste, 0);
        if (n <= 0) {
            return;
        }
        p += n;
        reste -= (size_t)n;
    }
}

/* Traite une ligne complete : repond TOUJOURS quelque chose, y compris en cas d'erreur — un
 * client qui n'obtient pas de reponse ne peut pas distinguer « occupe » de « plante ». */
static void traiter_ligne(int fd, char *ligne)
{
    /* 384 et non 256 : la chaine de STATUS a grandi au fil des versions (ajout de `reset=`, de
     * `uptime=` et de plusieurs compteurs) et depassait le tampon, tronquee EN SILENCE par
     * snprintf. La fin perdue etait justement la partie diagnostic. */
    char reponse[512] = {0};   /* la trame d'etat en clair a rallonge la reponse STATUS */
    strncpy(s_derniere_cmd, ligne, sizeof(s_derniere_cmd) - 1);

    /* La commande OTA est traitee ICI, pas dans le rappel : apres la ligne annoncee, le client
     * envoie le binaire BRUT, que seul le detenteur du socket peut consommer. On repond d'abord
     * « OTA PRET » pour que le client ne devine pas quand commencer. */
    if (s_ota != NULL && strncasecmp(ligne, "OTA ", 4) == 0) {
        size_t octets = (size_t)strtoul(ligne + 4, NULL, 10);
        ESP_LOGW(TAG, "commande OTA : %u octets annonces", (unsigned)octets);
        envoyer_texte(fd, "OTA PRET\n");
        int r = s_ota(fd, octets);
        envoyer_texte(fd, r == 0 ? "OTA OK REDEMARRAGE\n" : "OTA ECHEC\n");
        return;
    }

    if (s_commande != NULL) {
        s_commande(ligne, reponse, sizeof reponse);
    }
    if (reponse[0] == '\0') {
        snprintf(reponse, sizeof reponse, "ERREUR commande_inconnue");
    }
    ESP_LOGI(TAG, "commande \"%s\" -> \"%s\"", ligne, reponse);
    envoyer_texte(fd, reponse);
    envoyer_texte(fd, "\n");
}

/* ------------------------------------------------------------------------------------ */

static void traiter_client(int fd)
{
    /* STATIQUE et non local : 4 Ko sur la pile d'une tache de 5 Ko la fait deborder des le
     * premier client (constate : « Guru Meditation Error: Stack protection fault » dans la
     * tache « serveur_tcp », puis redemarrage de la carte et connexion TCP reinitialisee).
     * Le partage est sans risque : UN SEUL client est servi a la fois (invariant du module). */
    static uint8_t tampon[TAILLE_TAMPON];
    char ligne[TAILLE_LIGNE];
    size_t n_ligne = 0;
    bool binaire = false;
    bool premier_bloc = true;

    struct timeval delai = { .tv_sec = TIMEOUT_RECV_S, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &delai, sizeof delai);

    s_clients++;
    ESP_LOGI(TAG, "client connecte (n°%u)", (unsigned)s_clients);

    for (;;) {
        int n = recv(fd, tampon, sizeof tampon, 0);
        if (n == 0) {
            ESP_LOGI(TAG, "client deconnecte proprement");
            break;
        }
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                ESP_LOGW(TAG, "client muet depuis %d s : fermeture", TIMEOUT_RECV_S);
            } else {
                ESP_LOGW(TAG, "recv() a echoue : %d", errno);
            }
            break;
        }

        s_octets += (uint32_t)n;
        if (premier_bloc) {
            binaire = (tampon[0] == PREMIER_OCTET_TRAME);
            premier_bloc = false;
            if (binaire) {
                s002_debut_impression();   /* les mesures de silence valent pour CETTE impression */
            }
            ESP_LOGI(TAG, "flux %s", binaire ? "BINAIRE (trames YK)" : "TEXTUEL (commandes)");
        }

        if (binaire) {
            /* Le tampon fait 4096 o, soit plus qu'une trame maximale (~2900 o) : un bloc recu
             * est donc toujours une suite entiere de trames, jamais une trame tronquee. */
            s_blocs++;
            if (!s_reception(tampon, (size_t)n)) {
                /* Ecriture definitivement refusee (apres tentatives) : la suite du flux ne peut
                 * plus rien rattraper, l'imprimante fermera la tache et le papier portera un
                 * trou. On COUPE le flux pour que le client voie l'echec au lieu de croire
                 * l'impression terminee sur un rapport sans erreur. */
                ESP_LOGE(TAG, "bloc de %d octets REFUSE par le pont BLE — flux interrompu", n);
                break;
            }
            continue;
        }

        for (int i = 0; i < n; i++) {
            char c = (char)tampon[i];
            if (c == '\n' || c == '\r') {
                if (n_ligne > 0) {
                    ligne[n_ligne] = '\0';
                    traiter_ligne(fd, ligne);
                    n_ligne = 0;
                }
                continue;
            }
            if (n_ligne + 1 < sizeof ligne) {
                ligne[n_ligne++] = c;
            } else {
                /* Ligne trop longue : on la jette ENTIEREMENT plutot que d'executer un
                 * fragment de commande (le silence serait pire qu'une erreur explicite). */
                n_ligne = 0;
                snprintf(ligne, sizeof ligne, "ERREUR ligne_trop_longue");
                traiter_ligne(fd, ligne);
            }
        }
    }

    close(fd);
}

static void tache_serveur(void *param)
{
    int fd_ecoute = (int)(intptr_t)param;

    for (;;) {
        struct sockaddr_in client;
        socklen_t taille = sizeof client;
        int fd = accept(fd_ecoute, (struct sockaddr *)&client, &taille);
        if (fd < 0) {
            ESP_LOGW(TAG, "accept() a echoue : %d", errno);
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        if (s_client_actif) {
            /* Deux flux binaires entrelaces garberaient les trames : on refuse le second
             * client tout de suite, en le disant. */
            ESP_LOGW(TAG, "client deja connecte : nouvelle connexion refusee");
            envoyer_texte(fd, "ERREUR occupe_un_client_a_la_fois\n");
            close(fd);
            continue;
        }

        s_client_actif = true;
        traiter_client(fd);
        s_client_actif = false;
    }
}

/* ------------------------------------------------------------------------------------ */

esp_err_t serveur_tcp_demarrer(uint16_t port,
                               s002_reception_cb_t reception,
                               s002_commande_cb_t commande,
                               s002_ota_cb_t ota)
{
    if (s_demarre) {
        return ESP_ERR_INVALID_STATE;
    }

    /* Le socket est ouvert ICI et non dans la tache : une erreur de port est ainsi remontee
     * immediatement a l'appelant (demarrer un serveur muet serait indiagnosticable). */
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        ESP_LOGE(TAG, "socket() a echoue (%d), memoire insuffisante ?", errno);
        return ESP_FAIL;
    }

    int oui = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &oui, sizeof oui);

    struct sockaddr_in adresse = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons(port),
    };
    if (bind(fd, (struct sockaddr *)&adresse, sizeof adresse) != 0) {
        ESP_LOGE(TAG, "bind() sur le port %u a echoue (%d)", (unsigned)port, errno);
        close(fd);
        return ESP_FAIL;
    }
    if (listen(fd, 1) != 0) {
        ESP_LOGE(TAG, "listen() a echoue (%d)", errno);
        close(fd);
        return ESP_FAIL;
    }

    s_reception = reception;
    s_commande = commande;
    s_ota = ota;
    s_demarre = true;

    if (xTaskCreate(tache_serveur, "serveur_tcp", 5120, (void *)(intptr_t)fd, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "creation de la tache serveur impossible");
        close(fd);
        s_demarre = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void serveur_tcp_etat(char *tampon, size_t taille)
{
    snprintf(tampon, taille, "clients=%u recu=%u blocs=%u cmd=%s",
             (unsigned)s_clients, (unsigned)s_octets, (unsigned)s_blocs,
             s_derniere_cmd[0] != '\0' ? s_derniere_cmd : "-");
}
