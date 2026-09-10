# -*- coding: utf-8 -*-
"""
Étape 3 du pipeline : retrouver le PPN IdRef de chaque établissement de soutenance.

Principe (fourni par Géraldine) : le référentiel ABES ne contient pas le PPN.
Pour un code établissement, on interroge le SRU du Sudoc sur le NNT d'une thèse
soutenue dans cet établissement (query `nnt=AAAACODE*`, troncature à droite
uniquement), on prend l'unique notice UNIMARC renvoyée, et on lit :
  - le PPN de l'établissement de soutenance dans le 711 dont un subfield $4 == "295",
    sous-champ $3 ;
  - un label alternatif dans le sous-champ $a de ce même 711.

L'année AAAA est déduite des dates de validité du référentiel de base ; on essaie
plusieurs années candidates jusqu'à trouver une notice exploitable.

Entrée  : etablissements_base.json
Sortie  : etablissements_ppn.json   (mêmes objets, champs ppn / label_idref remplis)
          _cache_ppn.json           (checkpoint : reprise possible après interruption)

Réseau requis : sudoc.abes.fr (SRU). À lancer depuis un environnement qui l'atteint.
"""
import json, re, time, sys, os
import xml.etree.ElementTree as ET
import requests

BASE_IN   = "etablissements_base.json"
OUT       = "etablissements_ppn.json"
CACHE     = "_cache_ppn.json"

SRU_BASE  = "https://sudoc.abes.fr/cbs/sru/"
SRU_NS    = {"srw": "http://www.loc.gov/zing/srw/"}
ROLE_ETAB_SOUTENANCE = "295"      # code fonction UNIMARC $4 de l'établissement de soutenance
ANNEE_COURANTE = time.gmtime().tm_year
DELAI = 0.5                        # politesse entre requêtes (s)
TIMEOUT = 40


# --------------------------------------------------------------------------- #
# Client SRU (adapté de ton client d'un autre projet)
# --------------------------------------------------------------------------- #
class SRUClient:
    def __init__(self):
        self.base_url = SRU_BASE
        self.headers = {"Accept": "application/xml"}

    def search(self, query, max_records=1):
        params = {
            "operation": "searchRetrieve",
            "version": "1.1",
            "recordSchema": "unimarc",
            "maximumRecords": max_records,
            "startRecord": 1,
            "query": query,
        }
        r = requests.get(self.base_url, headers=self.headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content.decode("utf-8"))
        records = []
        recs = root.find(".//srw:records", SRU_NS)
        if recs is not None:
            for rec in recs.findall(".//srw:record", SRU_NS):
                data = rec.find(".//srw:recordData", SRU_NS)
                if data is not None:
                    marc = data.find("./record")     # pas de namespace dans recordData
                    if marc is not None:
                        records.append(marc)
        return records


def etab_soutenance_from_record(record):
    """Retourne (ppn, label) du 711 dont un $4 == 295, sinon (None, None)."""
    for df in record.findall(".//datafield[@tag='711']"):
        codes4 = [sf.text for sf in df.findall("subfield[@code='4']")]
        if ROLE_ETAB_SOUTENANCE in codes4:
            ppn = df.find("subfield[@code='3']")
            lab = df.find("subfield[@code='a']")
            ppn = ppn.text.strip() if ppn is not None and ppn.text else None
            lab = lab.text.strip() if lab is not None and lab.text else None
            if ppn:
                return ppn, lab
    return None, None


# --------------------------------------------------------------------------- #
# Choix des années candidates pour le NNT
# --------------------------------------------------------------------------- #
def _year(d):
    return int(d[:4]) if d and d[:4].isdigit() else None

def candidate_years(rec):
    lo = _year(rec.get("date_debut"))
    hi = _year(rec.get("date_fin"))
    end = (hi - 1) if hi else ANNEE_COURANTE - 1      # pas l'année en cours (dépôts incomplets)
    start = lo if lo else end - 20
    start = max(start, 1985)                           # NNT électronique généralisé
    end = min(end, ANNEE_COURANTE - 1)
    if start > end:
        start = end
    prefs = []
    # d'abord des années « sûres » (beaucoup de thèses déposées), puis balayage descendant
    for y in (min(end, 2018), end, end - 1, end - 2, (start + end) // 2, start):
        if start <= y <= end and y not in prefs:
            prefs.append(y)
    for y in range(end, start - 1, -1):
        if y not in prefs:
            prefs.append(y)
    return prefs


# --------------------------------------------------------------------------- #
def main():
    etabs = json.load(open(BASE_IN, encoding="utf-8"))
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    client = SRUClient()

    for i, rec in enumerate(etabs, 1):
        code = rec["code"]
        if code in cache:                              # reprise
            rec.update(cache[code]); continue

        found_ppn = found_lab = used_year = None
        for y in candidate_years(rec):
            query = f"nnt={y}{code}*"
            try:
                records = client.search(query, max_records=1)
            except Exception as e:
                sys.stderr.write(f"[{code}] {y} ERREUR SRU: {e}\n")
                time.sleep(DELAI); continue
            time.sleep(DELAI)
            if records:
                ppn, lab = etab_soutenance_from_record(records[0])
                if ppn:
                    found_ppn, found_lab, used_year = ppn, lab, y
                    break
        rec["ppn"] = found_ppn
        rec["label_idref"] = found_lab
        rec["_ppn_via_nnt_annee"] = used_year          # traçabilité (année ayant fonctionné)
        cache[code] = {"ppn": found_ppn, "label_idref": found_lab,
                       "_ppn_via_nnt_annee": used_year}
        json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=2)
        etat = "OK " + found_ppn if found_ppn else "-- introuvable"
        print(f"[{i}/{len(etabs)}] {code:5} {etat}")

    json.dump(etabs, open(OUT, "w"), ensure_ascii=False, indent=2)
    n = sum(1 for r in etabs if r.get("ppn"))
    print(f"\nTerminé. PPN trouvés : {n}/{len(etabs)}  ->  {OUT}")


if __name__ == "__main__":
    main()
