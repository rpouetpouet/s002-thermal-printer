cmake_minimum_required(VERSION 3.16)

# Nœud-pont BLE/Wi-Fi dédié à l'imprimante ORGSTA S002.
# v0 = banc de mesure du lien BLE (scan → connexion → découverte → écriture → chronométrage).
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(s002-bridge)
