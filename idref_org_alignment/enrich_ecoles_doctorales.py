# -*- coding: utf-8 -*-
"""
Étape 4 du pipeline : rattacher à chaque établissement (via son PPN) la liste de
ses écoles doctorales, avec leur PPN, label et dates de validité.

On interroge l'endpoint SPARQL de data.idref.fr avec la requête fournie par
Géraldine : on cherche les foaf:Organization liées au PPN de l'établissement et
dont le prefLabel contient « cole docto » (École doctorale…). dateOfBirth /
dateOfDeath donnent les dates de début / fin.

Entrée  : etablissements_ppn.json
Sortie  : etablissements_full.json  (champ ecoles_doctorales rempli)
          _cache_ed.json            (checkpoint par PPN)

Réseau requis : data.idref.fr (SPARQL). À lancer où il est joignable.
"""
import json, re, time, os, sys
import requests

IN    = "etablissements_ppn.json"
OUT   = "etablissements_full.json"
CACHE = "_cache_ed.json"

SPARQL = "https://data.idref.fr/sparql"
DELAI, TIMEOUT = 0.5, 60

QUERY_TPL = """#ECOLESDOC
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX marcrel: <http://id.loc.gov/vocabulary/relators/>
PREFIX dcterms: <http://purl.org/dc/terms/>
select distinct ?structure ?label ?date_debut ?date_fin
 where {{
  ?structure a foaf:Organization.
  ?structure skos:prefLabel ?label.
  ?structure ?lien <http://www.idref.fr/{ppn}/id>.
OPTIONAL{{?structure foaf:dateOfBirth ?date_debut.}}
OPTIONAL{{?structure foaf:dateOfDeath ?date_fin.}}
FILTER(regex(?label,"cole docto"))
}}
ORDER BY desc (?date_debut), (?date_fin)"""

PPN_RE = re.compile(r"idref\.fr/(\w+)/id")


def ppn_from_uri(uri):
    m = PPN_RE.search(uri or "")
    return m.group(1) if m else None


def fetch_ed(ppn):
    params = {"query": QUERY_TPL.format(ppn=ppn), "format": "json",
              "timeout": "0", "debug": "on"}
    r = requests.get(SPARQL, params=params, headers={"Accept": "application/sparql-results+json"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    eds = []
    seen = set()
    for b in data.get("results", {}).get("bindings", []):
        ed_ppn = ppn_from_uri(b.get("structure", {}).get("value"))
        if not ed_ppn or ed_ppn in seen:
            continue
        seen.add(ed_ppn)
        eds.append({
            "ppn":   ed_ppn,
            "label": (b.get("label", {}) or {}).get("value"),
            "date_debut": (b.get("date_debut", {}) or {}).get("value"),
            "date_fin":   (b.get("date_fin", {}) or {}).get("value"),
        })
    return eds


def main():
    etabs = json.load(open(IN, encoding="utf-8"))
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    for i, rec in enumerate(etabs, 1):
        ppn = rec.get("ppn")
        if not ppn:
            rec["ecoles_doctorales"] = []
            continue
        if ppn in cache:
            rec["ecoles_doctorales"] = cache[ppn]; continue
        try:
            eds = fetch_ed(ppn)
        except Exception as e:
            sys.stderr.write(f"[{rec['code']}] {ppn} ERREUR SPARQL: {e}\n")
            eds = []
        rec["ecoles_doctorales"] = eds
        cache[ppn] = eds
        json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=2)
        time.sleep(DELAI)
        print(f"[{i}/{len(etabs)}] {rec['code']:5} {ppn:12} -> {len(eds)} école(s) doctorale(s)")

    json.dump(etabs, open(OUT, "w"), ensure_ascii=False, indent=2)
    tot = sum(len(r.get("ecoles_doctorales", [])) for r in etabs)
    print(f"\nTerminé. {tot} rattachements ED au total  ->  {OUT}")


if __name__ == "__main__":
    main()
