# -*- coding: utf-8 -*-
"""
Orchestrateur du pipeline d'alignement des organisations (établissements de
soutenance + écoles doctorales) sur IdRef.

Étapes :
  1. parse_abes.py     -> etablissements_base.json   (hors ligne, nécessite bs4/lxml)
  2. harvest_idref.py  -> organisations_full.json    (réseau : theses.fr + idref.fr)
  3. build_index.py    -> index_recherche.json       (hors ligne)
                          + index_recherche.meta.json (provenance : date, sources, volumétrie)

Ensuite la recherche se fait via app.py (routes /align/organization et
/align/thesis-organizations) ou en CLI via lookup.py.

harvest_idref.py remplace enrich_ppn.py (SRU) et enrich_ecoles_doctorales.py
(SPARQL) : une seule notice idref.fr/{ppn}.json donne libellés, variantes 410,
dates et écoles doctorales. Les deux anciens scripts sont conservés pour
l'auditabilité mais ne sont plus dans le pipeline.

L'étape 1 est sautée si etablissements_base.json existe déjà (elle demande
bs4/lxml et la page HTML ABES source).

À lancer depuis un environnement qui atteint theses.fr et www.idref.fr.
"""
import os, subprocess, sys

ETAPES = [
    ("Parsing du référentiel ABES",          "parse_abes.py"),
    ("Harvest theses.fr + IdRef",            "harvest_idref.py"),
    ("Construction de l'index de recherche", "build_index.py"),
]

for titre, script in ETAPES:
    if script == "parse_abes.py" and os.path.exists("etablissements_base.json"):
        print(f"\n# {titre} : etablissements_base.json présent, étape sautée.")
        continue
    print(f"\n{'='*70}\n# {titre}\n{'='*70}")
    r = subprocess.run([sys.executable, script])
    if r.returncode != 0:
        print(f"ÉCHEC à l'étape « {titre} » (code {r.returncode}). Arrêt.")
        sys.exit(r.returncode)

print("\nPipeline terminé. Vérification : python lookup.py --selftest")
