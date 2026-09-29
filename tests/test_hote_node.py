"""Normalisation de l'option `node_host` (régression v0.2.1).

Le cas réel : un champ de formulaire rempli en style YAML (`node_host: 192.168.42.62`) a
donné un hôte inexistant, une impression échouée en « Name does not resolve » et une
recherche de panne à côté.
"""
import importlib.util
import pathlib
import unittest

RACINE = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "s002_const", RACINE / "custom_components" / "s002_printer" / "const.py"
)
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)


class TestHoteNode(unittest.TestCase):
    def test_valeur_normale_inchangee(self):
        self.assertEqual(const.normaliser_hote_node("192.168.42.62"), ("192.168.42.62", None))

    def test_libelle_recopie_est_retire(self):
        self.assertEqual(
            const.normaliser_hote_node("node_host: 192.168.42.62"), ("192.168.42.62", None)
        )
        self.assertEqual(const.normaliser_hote_node("node_host=192.168.42.62"), ("192.168.42.62", None))
        self.assertEqual(const.normaliser_hote_node("host: 192.168.42.62"), ("192.168.42.62", None))

    def test_guillemets_et_espaces(self):
        self.assertEqual(const.normaliser_hote_node('  "192.168.42.62"  '), ("192.168.42.62", None))

    def test_port_colle_est_extrait(self):
        self.assertEqual(const.normaliser_hote_node("192.168.42.62:3333"), ("192.168.42.62", 3333))

    def test_libelle_plus_port(self):
        self.assertEqual(
            const.normaliser_hote_node("node_host: 192.168.42.62:3333"), ("192.168.42.62", 3333)
        )

    def test_nom_d_hote(self):
        self.assertEqual(const.normaliser_hote_node("s002-noeud.local"), ("s002-noeud.local", None))

    def test_vide(self):
        self.assertEqual(const.normaliser_hote_node(""), ("", None))
        self.assertEqual(const.normaliser_hote_node(None), ("", None))


if __name__ == "__main__":
    unittest.main()
