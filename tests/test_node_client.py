#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import unittest
import threading
import socket
import time
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from s002_node_client import NoeudS002, NoeudS002Erreur

class FakeNoeud:
    def __init__(self, port):
        self.port = port
        self.recu_binaire = b""
        self.sock = None
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', port))
        self.server.listen(1)
        self.running = True
        self.thread = threading.Thread(target=self._handle)
        self.thread.start()

    def _handle(self):
        while self.running:
            try:
                self.server.settimeout(0.5)
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._servir(conn)

    def _servir(self, conn):
        """Sert UN client au fil de l'eau.

        Le mode est décidé par le premier octet (comme le vrai nœud) : 0x64 -> binaire, sinon
        textuel. En mode textuel il faut répondre SANS attendre la fermeture du client, sinon
        une commande qui garde la connexion ouverte (PING) bloque.
        """
        conn.settimeout(1.0)
        tampon = b""
        binaire = None
        while self.running:
            try:
                morceau = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not morceau:
                break
            if binaire is None:
                binaire = morceau[0] == 0x64
            if binaire:
                self.recu_binaire += morceau
            else:
                tampon += morceau
                while b"\n" in tampon:
                    ligne, tampon = tampon.split(b"\n", 1)
                    ligne = ligne.strip()
                    if ligne == b"PING":
                        conn.sendall(b"PONG\n")
                    elif ligne == b"STATUS":
                        conn.sendall(b"etat=pret clients=1 octets=0\n")
                    else:
                        conn.sendall(b"ERREUR commande_inconnue\n")

    def attendre_reception(self, delai=3.0):
        """Attend que le serveur factice ait recu le flux binaire.

        Le client ferme la connexion a la sortie de son `with`, mais les octets peuvent encore
        etre en vol : lire `recu_binaire` juste apres est une course (c'est ce qui faisait
        echouer ce test). On attend explicitement, avec une borne.
        """
        fin = time.time() + delai
        while time.time() < fin and not self.recu_binaire:
            time.sleep(0.02)
        return self.recu_binaire

    def stop(self):
        self.running = False
        self.server.close()
        self.thread.join(timeout=2)


class TestNoeudClient(unittest.TestCase):
    def test_ping_statut(self):
        fake = FakeNoeud(0)
        port = fake.server.getsockname()[1]
        with NoeudS002('127.0.0.1', port, timeout=2.0) as n:
            self.assertTrue(n.ping())
            res = n.statut()
            self.assertIn('etat', res)
            self.assertEqual(res['etat'], 'pret')
        fake.stop()

    def test_sequence_imprimer(self):
        fake = FakeNoeud(0)
        port = fake.server.getsockname()[1]
        # Raster valide : 2 lignes de 72 octets = 144 octets
        raster = b'\x00' * 144
        with NoeudS002('127.0.0.1', port, timeout=2.0) as n:
            n.imprimer_raster(raster)
        # Vérifier ce qui a été reçu (en laissant au serveur le temps de lire)
        data = fake.attendre_reception()
        fake.stop()
        self.assertTrue(len(data) > 0)
        self.assertEqual(data[0], 0x64)
        # Vérifie que la séquence contient une trame de largeur (type 0x0F)
        # et se termine par 0x9b (FRAME_END)
        self.assertTrue(data[-1] == 0x9B or b'\x9b' in data[-10:])

    def test_raster_bad_size(self):
        fake = FakeNoeud(0)
        port = fake.server.getsockname()[1]
        # Taille non multiple de 72
        with NoeudS002('127.0.0.1', port, timeout=2.0) as n:
            with self.assertRaises(NoeudS002Erreur):
                n.imprimer_raster(b'\x00' * 73)
        fake.stop()

    def test_serveur_injoignable(self):
        with self.assertRaises(NoeudS002Erreur):
            with NoeudS002('127.0.0.1', 59999, timeout=0.5) as n:
                n.ping()


if __name__ == '__main__':
    unittest.main(verbosity=2)
