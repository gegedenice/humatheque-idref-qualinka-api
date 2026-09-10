# -*- coding: utf-8 -*-
"""
Étape 3 : aplatir le référentiel harvesté en index de recherche lexicale.

Entrée : organisations_full.json  (harvest_idref.py)
Sortie : index_recherche.json

Une liste plate d'entités, clé = PPN, chaque nœud portant ses listes d'adjacence
(prédécesseurs / successeurs / écoles doctorales). C'est un graphe en liste
d'adjacence : ~750 nœuds, un seul fichier JSON, aucune dépendance. Un index
inversé ou une base graphe serait de la cérémonie à cette taille.

  - type            : "etablissement" | "ecole_doctorale"
  - ppn             : PPN IdRef (clé de sortie et clé de jointure des arêtes)
  - codes           : codes ABES (plusieurs codes peuvent pointer le même PPN)
  - label_officiel  : 210$a
  - labels          : 210$a + toutes les variantes 410$a + noms ABES/theses.fr
  - numeros         : numéros d'école doctorale extraits des libellés ("ED 217" -> "217")
  - date_debut/fin  : 210$c, fenêtre de validité (désambiguïsation temporelle)
  - predecesseurs / successeurs / ecoles_doctorales / etablissements : arêtes
"""
import datetime, json, os, re

IN = "organisations_full.json"
OUT = "index_recherche.json"
META = "index_recherche.meta.json"
SOURCES = ["https://theses.fr/api/v1/theses/recherche/",
           "https://www.idref.fr/{ppn}.json",
           "https://data.idref.fr/sparql"]

ED_NUM = re.compile(r"\bed\s*(\d{1,4})\b", re.I)


def variantes(label):
    """Variantes utiles au matching. La normalisation (casse/accents) se fait à la
    comparaison, pas ici."""
    if not label:
        return []
    v = {label.strip()}
    # retirer une parenthèse finale de lieu/date : « ... (Chambéry ; 2007-2021) », « (ComUE) »
    sans_par = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
    if sans_par:
        v.add(sans_par)
    return [x for x in v if x]


def numeros(labels):
    return sorted({m.lstrip("0") or "0" for l in labels for m in ED_NUM.findall(l)})


def main():
    data = json.load(open(IN, encoding="utf-8"))
    orgs = data["organisations"]

    # codes ABES + noms ABES/theses.fr, regroupés par PPN
    codes, noms_abes = {}, {}
    for e in data["etablissements_abes"]:
        p = e.get("ppn")
        if not p:
            continue
        codes.setdefault(p, []).append(e["code"])
        noms_abes.setdefault(p, set()).update(variantes(e.get("nom")))
        noms_abes[p].update(variantes(e.get("nom_theses")))

    # écoles doctorales : celles découvertes par SPARQL (prefLabel « École doctorale… »)
    # + celles citées en 510 $0 « École doctorale » par un établissement.
    ed_ppns = set(data.get("ppn_ecoles_doctorales", []))
    ed_parents = {}
    for ppn, o in orgs.items():
        for ed in o.get("ecoles_doctorales", []):
            if ed.get("ppn"):
                ed_parents.setdefault(ed["ppn"], []).append(ppn)
    ed_ppns |= set(ed_parents)

    # rattachement inverse : sur la notice de l'ED, l'établissement est cité en 510.
    # C'est la direction la plus fréquente (l'établissement ne cite presque jamais ses ED).
    etab_ppns = set(orgs) - ed_ppns
    for ppn in ed_ppns & set(orgs):
        for lien in orgs[ppn].get("membres", []):
            if lien.get("ppn") in etab_ppns:
                ed_parents.setdefault(ppn, []).append(lien["ppn"])

    # réciproque : chaque établissement porte la liste de ses ED (c'est ce que
    # l'aligneur utilise pour restreindre le vivier des ED au parent).
    etab_eds = {}
    for ed_ppn, parents in ed_parents.items():
        for p in parents:
            etab_eds.setdefault(p, set()).add(ed_ppn)

    index = []
    for ppn, o in orgs.items():
        labels = sorted(set(o.get("labels") or []) | set(variantes(o.get("label_officiel")))
                        | noms_abes.get(ppn, set()))
        index.append({
            "type": "ecole_doctorale" if ppn in ed_ppns else "etablissement",
            "ppn": ppn,
            "codes": sorted(codes.get(ppn, [])),
            "label_officiel": o.get("label_officiel"),
            "labels": labels,
            "numeros": numeros(labels),
            "date_debut": o.get("date_debut"),
            "date_fin": o.get("date_fin"),
            "predecesseurs": o.get("predecesseurs", []),
            "successeurs": o.get("successeurs", []),
            "ecoles_doctorales": sorted(etab_eds.get(ppn, set())),
            "etablissements": sorted(set(ed_parents.get(ppn, []))),
        })

    # écoles doctorales citées en 510 mais dont la notice n'a pas été récupérée :
    # on garde le libellé du lien, mieux que rien pour le matching.
    connus = set(orgs)
    for ed_ppn, parents in ed_parents.items():
        if ed_ppn in connus:
            continue
        lien = next(ed for p in parents for ed in orgs[p]["ecoles_doctorales"] if ed["ppn"] == ed_ppn)
        labels = sorted(set(variantes(lien.get("label"))))
        index.append({
            "type": "ecole_doctorale", "ppn": ed_ppn, "codes": [],
            "label_officiel": lien.get("label"), "labels": labels, "numeros": numeros(labels),
            "date_debut": lien.get("date_debut"), "date_fin": lien.get("date_fin"),
            "predecesseurs": [], "successeurs": [], "ecoles_doctorales": [],
            "etablissements": sorted(parents),
        })

    json.dump(index, open(OUT, "w"), ensure_ascii=False, indent=2)
    n_e = sum(1 for x in index if x["type"] == "etablissement")
    n_ed = sum(1 for x in index if x["type"] == "ecole_doctorale")
    n_num = sum(1 for x in index if x["numeros"])

    # provenance : l'index est un artefact reconstructible mais les sources amont
    # bougent, donc « depuis quand » est la seule question qui compte. Fichier
    # frere volontairement : l'index reste une liste plate, l'aligneur ne change pas.
    json.dump({
        "genere_le": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_locale": {"fichier": IN,
                          "modifie_le": datetime.datetime.fromtimestamp(
                              os.path.getmtime(IN)).astimezone().isoformat(timespec="seconds")},
        "sources_distantes": SOURCES,
        "codes_abes": len(data["etablissements_abes"]),
        "codes_abes_avec_ppn": sum(1 for e in data["etablissements_abes"] if e.get("ppn")),
        "notices_idref": len(orgs),
        "entites": len(index), "etablissements": n_e, "ecoles_doctorales": n_ed,
        "avec_numero_ed": n_num,
    }, open(META, "w"), ensure_ascii=False, indent=2)
    print(f"Index écrit : {OUT}  ({n_e} établissements, {n_ed} écoles doctorales, "
          f"{n_num} avec numéro d'ED)")
    print(f"Provenance : {META}")
    print(f"  libellés/entité (médiane) : "
          f"{sorted(len(x['labels']) for x in index)[len(index)//2]}")


if __name__ == "__main__":
    main()
