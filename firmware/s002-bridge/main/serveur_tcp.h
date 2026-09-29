#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* Appelee pour chaque bloc d'octets BINAIRES recus (trames YK brutes, concatenees).
 * Retourne true si le bloc a ete ecrit sur le BLE, false sinon. */
typedef bool (*s002_reception_cb_t)(const uint8_t *donnees, size_t longueur);

/* Appelee pour une COMMANDE TEXTE (ligne sans le \n final). Doit remplir `reponse`
 * (chaine terminee par \0, longueur max `taille`). */
typedef void (*s002_commande_cb_t)(const char *commande, char *reponse, size_t taille);

/* Appelee sur la commande « OTA <octets> » : doit lire EXACTEMENT `octets` octets sur `fd` et les
 * ecrire dans la partition INACTIVE. Retourne 0 si l'image a ete acceptee — le noeud redemarre
 * alors de lui-meme. Traitee ici et non dans le rappel de commande : apres la ligne, c'est du
 * flux BRUT qu'il faut consommer, ce qu'un rappel qui ne voit que du texte ne peut pas faire. */
typedef int (*s002_ota_cb_t)(int fd, size_t octets);

/* Demarre le serveur. Retourne ESP_OK, ou une erreur si l'ouverture du socket echoue.
 * Cree sa propre tache FreeRTOS. Ne bloque pas. */
/** Remise a zero des mesures de silence, au debut d'un flux d'impression. */
void s002_debut_impression(void);

esp_err_t serveur_tcp_demarrer(uint16_t port,
                               s002_reception_cb_t reception,
                               s002_commande_cb_t commande,
                               s002_ota_cb_t ota);

/* Remplit `tampon` avec un resume lisible (pour les logs) : nb de clients servis,
 * octets recus, derniere commande. Ne doit jamais bloquer. */
void serveur_tcp_etat(char *tampon, size_t taille);
