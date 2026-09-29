#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Client TCP pour le nœud d'impression S002.
Permet de communiquer avec le nœud ESP32-C3 qui fait office de pont BLE
vers l'imprimante thermique ORGSTA S002.
"""

import argparse
import socket
import sys
import importlib.util
import pathlib
import sys as _sys
import types

# Import des encodeurs YK depuis l'intégration (évite d'importer le paquet HA)
_PAQUET = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "s002_printer"
_pkg = types.ModuleType("s002_printer"); _pkg.__path__ = [str(_PAQUET)]
_sys.modules["s002_printer"] = _pkg
_spec = importlib.util.spec_from_file_location("s002_printer.yk", _PAQUET / "yk.py")
yk = importlib.util.module_from_spec(_spec); _sys.modules["s002_printer.yk"] = yk
_spec.loader.exec_module(yk)


class NoeudS002Erreur(Exception):
    """Exception levée en cas d'erreur de communication avec le nœud."""
    pass


class NoeudS002:
    """Client pour le nœud d'impression S002 via TCP."""

    def __init__(self, hote: str, port: int = 3333, timeout: float = 30.0):
        self.hote = hote
        self.port = port
        self.timeout = timeout
        self.socket = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self):
        """Établit une connexion TCP avec le nœud."""
        if self.socket is not None:
            self.close()
        try:
            self.socket = socket.create_connection((self.hote, self.port), timeout=self.timeout)
        except OSError as err:
            raise NoeudS002Erreur(f"Nœud {self.hote}:{self.port} injoignable — {err}") from err

    def close(self):
        """Ferme la connexion TCP."""
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _envoyer(self, donnees: bytes) -> int:
        """Envoie la TOTALITÉ des données sur la connexion TCP.

        `socket.send()` peut n'envoyer qu'une partie du tampon (et sur un flux d'impression de
        plusieurs dizaines de kilo-octets, c'est la règle plutôt que l'exception) : cela
        tronquerait des trames. `sendall()` est la seule primitive correcte ici.
        """
        if self.socket is None:
            raise NoeudS002Erreur("Pas de connexion établie")
        try:
            self.socket.sendall(donnees)
        except OSError as err:
            raise NoeudS002Erreur(f"Envoi interrompu : {err}") from err
        return len(donnees)

    def _recevoir_ligne(self) -> str:
        """Reçoit une ligne terminée par \\n."""
        if self.socket is None:
            raise NoeudS002Erreur("Pas de connexion établie")
        ligne = b""
        while True:
            caractere = self.socket.recv(1)
            if not caractere:
                raise NoeudS002Erreur("Connexion fermée prématurément")
            if caractere == b'\n':
                break
            ligne += caractere
        return ligne.decode('utf-8')

    def ping(self) -> bool:
        """Envoie PING et retourne True si le nœud répond PONG."""
        try:
            self._envoyer(b'PING\n')
            reponse = self._recevoir_ligne()
            return reponse.strip() == 'PONG'
        except Exception as e:
            raise NoeudS002Erreur(f"Échec du ping: {e}")

    def commande_texte(self, texte: str) -> str:
        """Envoie une commande texte brute et retourne la reponse du noeud.

        Le noeud v2 comprend PING, STATUS, LIBERER, CONNECTER et LIBERATION <secondes>.
        `LIBERER` rend l'imprimante a l'instant (un telephone peut alors s'y connecter) ;
        elle est reprise automatiquement a la prochaine impression.
        """
        try:
            self._envoyer(texte.strip().upper().encode() + b'\n')
            return self._recevoir_ligne().strip()
        except Exception as e:
            raise NoeudS002Erreur(f"Echec de la commande {texte!r}: {e}")

    def statut(self) -> dict:
        """Envoie STATUS et retourne le dictionnaire parsé de la réponse."""
        try:
            self._envoyer(b'STATUS\n')
            reponse = self._recevoir_ligne()
            resultat = {}
            for paire in reponse.strip().split():
                if '=' in paire:
                    cle, valeur = paire.split('=', 1)
                    # Essayer de convertir en entier si possible
                    try:
                        resultat[cle] = int(valeur)
                    except ValueError:
                        resultat[cle] = valeur
                else:
                    # Cas où il n'y a pas de = (devrait pas arriver selon le protocole)
                    resultat[paire] = ''
            return resultat
        except Exception as e:
            raise NoeudS002Erreur(f"Échec du statut: {e}")

    def envoyer_trames(self, donnees: bytes) -> int:
        """Envoie un flux binaire commençant par 0x64."""
        if not donnees:
            raise NoeudS002Erreur("Données vides")
        if donnees[0] != 0x64:
            raise NoeudS002Erreur("Le premier octet doit être 0x64 pour le mode binaire")
        return self._envoyer(donnees)

    def imprimer_raster(self, raster: bytes, largeur_points: int = 576) -> dict:
        """
        Construit et envoie la séquence complète validée sur ce matériel.
        
        Séquence : frame_token(1) -> frame_paper_size(576, 2) -> tranches d'image -> frame_feed(20, 0)
        """
        if len(raster) % yk.BYTES_PER_LINE != 0:
            raise NoeudS002Erreur(
                f"Le raster doit être un multiple de {yk.BYTES_PER_LINE} octets "
                f"(reçu {len(raster)} octets)"
            )
        
        # Construction de la séquence complète
        trames = []
        
        # 1. Frame token avec compteur 1
        trames.append(yk.frame_token(1))
        
        # 2. Frame paper size avec compteur 2
        trames.append(yk.frame_paper_size(largeur_points, 2))
        
        # 3. Tranches d'image (en commençant au compteur 3)
        compteur = 3
        for trame, compteur_utilise in yk.iter_image_frames(
            raster, 
            lines_per_frame=yk.MAX_LINES_PER_FRAME,
            start_counter=compteur
        ):
            trames.append(trame)
            compteur = compteur_utilise + 1  # Le compteur utilisé + 1 pour la prochaine trame
        
        # 4. Frame feed avec le prochain compteur
        trames.append(yk.frame_feed(20, compteur))
        
        # Concaténation de toutes les trames
        donnees_complete = b''.join(trames)
        
        # Envoi en mode binaire
        nb_octets = self.envoyer_trames(donnees_complete)
        
        return {
            'nb_trames': len(trames),
            'nb_octets': nb_octets,
            'largeur_points': largeur_points,
            'hauteur_lignes': len(raster) // yk.BYTES_PER_LINE
        }


def main():
    """Point d'entrée en ligne de commande."""
    parser = argparse.ArgumentParser(description='Client pour le nœud d\'impression S002')
    parser.add_argument('hote', help='Adresse IP ou nom d\'hôte du nœud')
    parser.add_argument('commande',
                       choices=['ping', 'statut', 'imprimer', 'liberer', 'connecter',
                                'liberation'],
                       help='Commande à exécuter (liberation prend les secondes dans '
                            'fichier_raster)')
    parser.add_argument('fichier_raster', nargs='?', 
                       help='Fichier raster binaire (requis pour la commande imprimer)')
    parser.add_argument('--port', type=int, default=3333,
                       help='Port TCP du nœud (défaut: 3333)')
    parser.add_argument('--timeout', type=float, default=30.0,
                       help='Timeout en secondes (défaut: 30.0)')
    
    args = parser.parse_args()
    
    try:
        with NoeudS002(args.hote, args.port, args.timeout) as noeud:
            if args.commande == 'ping':
                if noeud.ping():
                    print("PONG")
                    return 0
                else:
                    print("Réponse inattendue au ping", file=sys.stderr)
                    return 1
                    
            elif args.commande == 'statut':
                statut_dict = noeud.statut()
                # Format "clé=valeur clé=valeur"
                output = ' '.join(f'{k}={v}' for k, v in statut_dict.items())
                print(output)
                return 0
                
            elif args.commande in ('liberer', 'connecter'):
                print(noeud.commande_texte(args.commande.upper()))
                return 0

            elif args.commande == 'liberation':
                secondes = args.fichier_raster or '120'
                print(noeud.commande_texte(f'LIBERATION {secondes}'))
                return 0

            elif args.commande == 'imprimer':
                if not args.fichier_raster:
                    print("Erreur: fichier_raster requis pour la commande imprimer", 
                          file=sys.stderr)
                    return 1
                    
                try:
                    with open(args.fichier_raster, 'rb') as f:
                        raster = f.read()
                except IOError as e:
                    print(f"Erreur lors de la lecture du fichier raster: {e}", 
                          file=sys.stderr)
                    return 1
                    
                resultat = noeud.imprimer_raster(raster)
                print(f"Impression réussie: {resultat['nb_trames']} trames, "
                      f"{resultat['nb_octets']} octets envoyés")
                return 0
    
    except NoeudS002Erreur as e:
        print(f"Erreur du nœud: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Erreur inattendue: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())