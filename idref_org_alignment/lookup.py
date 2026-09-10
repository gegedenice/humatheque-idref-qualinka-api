# -*- coding: utf-8 -*-
"""
Étape 4 : la brique de recherche — alignement lexical SANS IA.

Le scoring vit désormais dans `app.py` (fonctions `align_organization`,
`org_similarity`, ...), pour qu'il n'existe qu'à un seul endroit : ce fichier
n'est plus qu'un CLI d'inspection au-dessus.

Pourquoi lexical strict : l'univers est fermé et petit ; le risque n'est pas
l'homonymie mais de confondre deux versions temporelles proches (« Grenoble 1 »
/ « Grenoble » / « Université Grenoble Alpes »). C'est l'année qui tranche, pas
la sémantique.

CLI :
    python lookup.py "Université Grenoble Alpes" --annee 2018
    python lookup.py "École doctorale mathématiques" --type ecole_doctorale --parent 184668794
    python lookup.py --selftest
"""
import json, os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def align(label, annee=None, type="etablissement", parent_ppn="", **kw):
    return app.align_organization(app.AlignOrganizationRequest(
        label=label,
        kind="doctoral_school" if type == "ecole_doctorale" else "institution",
        year=str(annee or ""), parent_ppn=parent_ppn or "", **kw))


def _top(r):
    return (r["candidates"] or [{}])[0]


def selftest():
    """Cas réels sur le référentiel construit. Aucun réseau."""
    def show(titre, r):
        print(f"\n« {r['query']['label']} » (annee={r['query']["year"] or '-'})  [{titre}]")
        print(f"   -> {r['status']:13} ppn={r['ppn']} score={r['score']} marge={r['margin']} "
              f"scope={r['candidate_scope']}/{r['pool_size']}")
        for c in r["candidates"][:3]:
            print(f"      {c['score']:.3f} {str(c['ppn']):12} valide={c['valid_for_year']} "
                  f"[{c['date_debut']}..{c['date_fin']}] {c['label_officiel']}")
        if r["redirect"]:
            print(f"      redirect -> {r['redirect']['ppn']} {r['redirect']['label_officiel']}")

    # 1. le libellé d'extraction réel tombe sur la variante 410 « Université de Paris-Sorbonne »
    r = align("Université de Paris - Sorbonne", 1995)
    show("PA04 / 026403633", r)
    assert r["status"] == "accepted" and r["ppn"] == "026403633", r["status"]

    # 2. même libellé, année hors fenêtre (210$c s'arrête en 2017) : le filtre temporel
    #    écarte PA04 et c'est le successeur qui gagne de lui-même (« Sorbonne Université »
    #    porte une variante 410 proche). Le champ `redirect` reste le filet pour les cas
    #    où le successeur ne matche pas lexicalement.
    r = align("Université de Paris - Sorbonne", 2020)
    show("hors fenêtre -> successeur", r)
    assert r["ppn"] == "221333754", (r["status"], r["ppn"])

    # 3. deux versions temporelles homonymes -> deux PPN distincts
    a, b = align("Université Grenoble Alpes", 2018), align("Université Grenoble Alpes", 2021)
    show("GREA (ComUE, 2015-2020)", a)
    show("GRAL (2020-)", b)
    assert _top(a)["ppn"] != _top(b)["ppn"], "l'année ne départage pas"

    # 4. libellés ABES bruts
    for lib, an in (("Grenoble 1", 1990), ("Aix-Marseille 2", 1990)):
        r = align(lib, an)
        show("libellé ABES brut", r)
        assert r["status"] in ("accepted", "ambiguous"), r["status"]

    # 5. école doctorale : le scope parent doit réduire le vivier
    ed = next(x for x in json.load(open(app.ORG_INDEX_PATH, encoding="utf-8"))
              if x["type"] == "ecole_doctorale" and x["etablissements"])
    parent = ed["etablissements"][0]
    glob = align(ed["label_officiel"], type="ecole_doctorale")
    scoped = align(ed["label_officiel"], type="ecole_doctorale", parent_ppn=parent)
    show("ED, vivier global", glob)
    show("ED, vivier réduit au parent", scoped)
    assert scoped["pool_size"] < glob["pool_size"], "le scope parent n'a rien réduit"
    assert scoped["ppn"] == ed["ppn"]

    # 6. structure pré-ED sans notice IdRef : abstention attendue, pas un faux positif
    r = align("U.E.N. HISTOIRES", 1995, type="ecole_doctorale")
    show("attendu : abstention", r)
    assert r["status"] != "accepted", "faux positif sur une structure sans autorité IdRef"

    print("\nselftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("label", nargs="?", help="libellé à aligner")
    ap.add_argument("--annee", type=int, default=None)
    ap.add_argument("--type", default="etablissement",
                    choices=["etablissement", "ecole_doctorale"])
    ap.add_argument("--parent", default="", help="PPN de l'établissement (scope des ED)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    elif args.label:
        print(json.dumps(align(args.label, annee=args.annee, type=args.type,
                               parent_ppn=args.parent), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
