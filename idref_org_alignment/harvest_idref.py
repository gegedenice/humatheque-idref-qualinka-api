# -*- coding: utf-8 -*-
"""
Étape 2 du pipeline : construire le référentiel des organisations depuis IdRef.

Remplace enrich_ppn.py (SRU/NNT) et enrich_ecoles_doctorales.py (SPARQL), gardés
sur disque pour l'audit mais superseded.

A. code ABES -> PPN de l'établissement
   theses.fr/api/v1/theses/recherche/?q=nnt:*CODE*&nombre=1 -> etabSoutenancePpn
   La troncature à gauche fonctionne : aucune année à deviner (c'est tout l'intérêt
   par rapport au SRU, qui exige une année candidate et échoue si on tombe à côté).

B. PPN -> libellés, dates, et tout le graphe local
   https://www.idref.fr/{ppn}.json — les 510 sont typés par $5 / $0 :
     210$a/$c    libellé de référence + fenêtre de validité
     410$a       formes rejetées (variantes de saisie) -> indispensables au matching
     510 $5 a|.. prédécesseur       ($3 = PPN)
     510 $5 b|.. successeur
     510 $5 r|.. établissements membres / composantes
     510 $5 x|xpx + $0 "École doctorale"  -> écoles doctorales
   On explore un niveau : chaque ED / prédécesseur / successeur découvert est
   récupéré à son tour (pour ses propres 410 et ses liens retour).

Entrée  : etablissements_base.json          (squelette des codes ABES)
Sortie  : organisations_full.json
          _cache_org.json  (checkpoint : PPN -> notice parsée, reprise possible)
          _cache_code_ppn.json (checkpoint : code ABES -> PPN)

Réseau requis : theses.fr et www.idref.fr.
"""
import json, os, re, sys, time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_IN     = "etablissements_base.json"
OUT         = "organisations_full.json"
CACHE_ORG   = "_cache_org.json"
CACHE_CODE  = "_cache_code_ppn.json"
CACHE_ED    = "_cache_ed_ppn.json"

SPARQL      = "https://data.idref.fr/sparql"
THESES_API  = "https://theses.fr/api/v1/theses/recherche/"
IDREF_JSON  = "https://www.idref.fr/{ppn}.json"
USER_AGENT  = "humatheque-idref-qualinka-api/0.1 (referentiel organisations)"
DELAI       = 0.2
TIMEOUT     = 40
RETRIES     = 2


def get_json(url, timeout=TIMEOUT, accept="application/json"):
    """GET + parse JSON, avec quelques essais. Retourne None en cas d'échec."""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt == RETRIES:
                sys.stderr.write(f"  ! {url[:80]} -> {e}\n")
                return None
            time.sleep(1.0 * (2 ** attempt))
    return None


# --------------------------------------------------------------------------- #
# A. code ABES -> PPN
# --------------------------------------------------------------------------- #
def ppn_for_code(code):
    url = THESES_API + "?" + urlencode({"q": f"nnt:*{code}*", "nombre": 1})
    d = get_json(url)
    if not d or not d.get("theses"):
        return None, None, 0
    t = d["theses"][0]
    return t.get("etabSoutenancePpn"), t.get("etabSoutenanceN"), d.get("totalHits", 0)


# --------------------------------------------------------------------------- #
# B. PPN -> notice IdRef parsée
# --------------------------------------------------------------------------- #
def _subfields(df):
    """[(code, content), ...] — $ codes sont tantôt str tantôt int dans le JSON IdRef."""
    sf = df.get("subfield")
    if sf is None:
        return []
    if isinstance(sf, dict):
        sf = [sf]
    return [(str(s.get("code")), str(s.get("content"))) for s in sf if s.get("content") is not None]


def _first(subs, code):
    for c, v in subs:
        if c == code:
            return v
    return None


def _all(subs, code):
    return [v for c, v in subs if c == code]


DATE_RANGE = re.compile(r"(\d{3}[\dX.])\s*-\s*(\d{3}[\dX.])?")


def _parse_dates(c_values):
    """$c porte tantôt un lieu, tantôt une plage « 1970-2017 » / « 1995-.... »."""
    for v in c_values:
        m = DATE_RANGE.search(v)
        if m:
            d0, d1 = m.group(1), m.group(2)
            # « 199. » / « 199X » : borne floue, on garde tel quel (lookup la traite en ouverte)
            return d0, (d1 if d1 and d1.isdigit() else None)
    return None, None


def parse_idref(ppn, rec):
    """Notice UNIMARC IdRef -> dict normalisé. Voir docstring du module pour les champs."""
    dfs = rec.get("record", {}).get("datafield", [])
    if isinstance(dfs, dict):
        dfs = [dfs]

    label_officiel, labels = None, []
    d0 = d1 = None
    predecesseurs, successeurs, membres, ecoles = [], [], [], []

    for df in dfs:
        tag = str(df.get("tag"))
        subs = _subfields(df)
        if tag == "210":
            a = _first(subs, "a")
            if a:
                label_officiel = a
                labels.append(a)
            d0, d1 = _parse_dates(_all(subs, "c"))
        elif tag == "410":
            a = _first(subs, "a")
            if a:
                labels.append(a)
        elif tag == "510":
            a, p5, p3 = _first(subs, "a"), _first(subs, "5") or "", _first(subs, "3")
            note = _first(subs, "0") or ""
            if not a or a.startswith("****"):        # lignes de séparation « **** Écoles doctorales **** »
                continue
            ld0, ld1 = _parse_dates(_all(subs, "c"))
            link = {"ppn": p3, "label": a, "note": note or None,
                    "date_debut": ld0, "date_fin": ld1}
            kind = p5[:1]
            # le typage fiable est la note $0, pas $5 : x|xp couvre aussi labos,
            # composantes et équipes de recherche (185 liens sans note sur 493 notices).
            if "cole doctorale" in note:
                ecoles.append(link)
            elif kind == "a":
                predecesseurs.append(link)
            elif kind == "b":
                successeurs.append(link)
            elif kind in ("r", "z", "x"):
                membres.append(link)

    return {
        "ppn": ppn,
        "label_officiel": label_officiel,
        "labels": sorted({l.strip() for l in labels if l and l.strip()}),
        "date_debut": d0,
        "date_fin": d1,
        "predecesseurs": predecesseurs,
        "successeurs": successeurs,
        "membres": membres,
        "ecoles_doctorales": ecoles,
    }


def fetch_org(ppn, cache):
    if ppn in cache:
        return cache[ppn]
    rec = get_json(IDREF_JSON.format(ppn=ppn))
    time.sleep(DELAI)
    parsed = parse_idref(ppn, rec) if rec else None
    cache[ppn] = parsed
    return parsed


ED_QUERY = """PREFIX foaf: <http://xmlns.com/foaf/0.1/>
SELECT DISTINCT ?ed WHERE {
 ?ed a foaf:Organization. ?ed skos:prefLabel ?label.
 FILTER(regex(?label,"^.cole doctorale","i"))
} LIMIT 20000"""


def ppn_ecoles_doctorales():
    """Découverte des écoles doctorales : une seule requête SPARQL pour tout IdRef.

    Les notices d'établissement ne citent presque jamais leurs ED en 510 (16 sur 202
    ici) : c'est l'ED qui pointe vers son établissement, pas l'inverse. Une requête
    globale sur le prefLabel « École doctorale… » les ramène toutes ; on récupère
    ensuite chaque notice comme les autres, ce qui donne en prime les variantes 410
    (« ED 217 », « EDMSTII ») que le SPARQL ne fournit pas.
    """
    if os.path.exists(CACHE_ED):
        return json.load(open(CACHE_ED, encoding="utf-8"))
    url = SPARQL + "?" + urlencode({"query": ED_QUERY, "format": "json", "timeout": "0"})
    data = get_json(url, timeout=300, accept="application/sparql-results+json")  # ~95 s, mis en cache
    ppns = sorted({m.group(1) for b in (data or {}).get("results", {}).get("bindings", [])
                   for m in [re.search(r"idref\.fr/(\w+)/id", b["ed"]["value"])] if m})
    json.dump(ppns, open(CACHE_ED, "w"), ensure_ascii=False, indent=1)
    return ppns


def main():
    etabs = json.load(open(BASE_IN, encoding="utf-8"))
    cache_code = json.load(open(CACHE_CODE, encoding="utf-8")) if os.path.exists(CACHE_CODE) else {}
    cache_org = json.load(open(CACHE_ORG, encoding="utf-8")) if os.path.exists(CACHE_ORG) else {}

    # --- A : codes -> PPN ---------------------------------------------------- #
    print(f"# A. {len(etabs)} codes ABES -> PPN (theses.fr)")
    for i, rec in enumerate(etabs, 1):
        code = rec["code"]
        if code not in cache_code:
            ppn, nom, hits = ppn_for_code(code)
            cache_code[code] = {"ppn": ppn, "nom_theses": nom, "hits": hits}
            json.dump(cache_code, open(CACHE_CODE, "w"), ensure_ascii=False, indent=1)
            time.sleep(DELAI)
        c = cache_code[code]
        rec["ppn"], rec["nom_theses"] = c["ppn"], c.get("nom_theses")
        if i % 25 == 0 or i == len(etabs):
            n = sum(1 for r in etabs[:i] if r.get("ppn"))
            print(f"  [{i}/{len(etabs)}] {n} PPN trouvés")

    # --- B : PPN -> notices IdRef, exploration d'un niveau ------------------- #
    seeds = sorted({r["ppn"] for r in etabs if r.get("ppn")})
    print(f"\n# B. {len(seeds)} notices établissement (idref.fr/{{ppn}}.json)")
    for i, ppn in enumerate(seeds, 1):
        fetch_org(ppn, cache_org)
        if i % 25 == 0 or i == len(seeds):
            json.dump(cache_org, open(CACHE_ORG, "w"), ensure_ascii=False, indent=1)
            print(f"  [{i}/{len(seeds)}]")

    lies = set()
    for ppn in seeds:
        org = cache_org.get(ppn)
        if not org:
            continue
        for key in ("ecoles_doctorales", "predecesseurs", "successeurs"):
            lies.update(l["ppn"] for l in org[key] if l.get("ppn"))
    lies -= set(seeds)
    print(f"\n# B'. {len(lies)} notices liées (ED, prédécesseurs, successeurs)")
    for i, ppn in enumerate(sorted(lies), 1):
        fetch_org(ppn, cache_org)
        if i % 50 == 0 or i == len(lies):
            json.dump(cache_org, open(CACHE_ORG, "w"), ensure_ascii=False, indent=1)
            print(f"  [{i}/{len(lies)}]")
    json.dump(cache_org, open(CACHE_ORG, "w"), ensure_ascii=False, indent=1)

    # --- C : écoles doctorales (découverte SPARQL globale) ------------------- #
    eds = [p for p in ppn_ecoles_doctorales() if p not in cache_org]
    print(f"\n# C. {len(eds)} notices d'école doctorale")
    for i, ppn in enumerate(eds, 1):
        fetch_org(ppn, cache_org)
        if i % 50 == 0 or i == len(eds):
            json.dump(cache_org, open(CACHE_ORG, "w"), ensure_ascii=False, indent=1)
            print(f"  [{i}/{len(eds)}]")
    json.dump(cache_org, open(CACHE_ORG, "w"), ensure_ascii=False, indent=1)

    out = {"etablissements_abes": etabs,
           "ppn_ecoles_doctorales": ppn_ecoles_doctorales(),
           "organisations": {p: o for p, o in cache_org.items() if o},
           "ppn_racines": seeds}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)

    n_ppn = sum(1 for r in etabs if r.get("ppn"))
    n_ed = sum(1 for p in ppn_ecoles_doctorales() if cache_org.get(p))
    print(f"\nTerminé -> {OUT}")
    print(f"  codes ABES avec PPN : {n_ppn}/{len(etabs)}")
    print(f"  notices récupérées  : {sum(1 for o in cache_org.values() if o)}")
    print(f"  écoles doctorales   : {n_ed}")


if __name__ == "__main__":
    main()
