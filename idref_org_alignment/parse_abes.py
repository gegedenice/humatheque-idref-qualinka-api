# -*- coding: utf-8 -*-
"""Parse la page ABES CodesUnivEtab -> referentiel etablissements de soutenance."""
import re, json, unicodedata
from bs4 import BeautifulSoup
from datetime import datetime

SRC="./abes_documentation_codes_etab.htm"

def clean(s):
    return re.sub(r'\s+',' ', (s or '').replace('\xa0',' ')).strip()

DATE=r'\d{4}-\d{2}-\d{2}'
def norm_date(d):
    d=d.strip()
    return d if re.fullmatch(DATE,d) else d

def parse_annotation(ann):
    """Retourne (liste_clauses_successeurs, note_brute).
    Chaque clause: {codes:[...], date_debut, date_fin, condition}."""
    ann=clean(ann)
    if not ann: return [], ""
    # normaliser 'à partir du' -> 'à partir de'
    a=ann.replace('à partir du','à partir de').replace('a partir de','à partir de')
    clauses=[]
    # decouper en segments sur 'Voir' et 'puis'
    # on garde l'ordre; 'puis' introduit une clause liee au meme voir precedent
    segs=re.split(r'\bVoir\b', a)
    for seg in segs[1:]:
        # 'puis' peut introduire plusieurs sous-clauses
        for sub in re.split(r'\bpuis\b', seg):
            sub=sub.strip()
            if not sub: continue
            codes=re.findall(r'\b([A-Z][A-Z0-9]{2,4})\b', sub)
            # condition entre parentheses
            cond=None
            mc=re.search(r'\(([^)]*th[eè]ses[^)]*)\)', sub, re.I)
            if mc: cond=clean(mc.group(1))
            dd=df=None
            mr=re.search(r'de\s*('+DATE+r')\s*à\s*('+DATE+r')', sub)
            mp=re.search(r'à partir de\s*('+DATE+r')', sub)
            if mr: dd,df=mr.group(1),mr.group(2)
            elif mp: dd=mp.group(1)
            if codes:
                clauses.append({"codes":codes,"date_debut":dd,"date_fin":df,"condition":cond})
    return clauses, ann

def extract_name_dates(nom):
    """(2017-2020),(1441-1970) dans le nom -> (debut,fin, nom_court)."""
    dd=df=None
    m=re.search(r'\((\d{4})\s*-\s*(\d{4})\)', nom)
    if m: dd,df=m.group(1),m.group(2)
    else:
        m2=re.search(r'\((\d{4})\s*-\s*\)', nom)  # (2016- )
        if m2: dd=m2.group(1)
    return dd,df

def main():
    txt=open(SRC,'rb').read().decode('utf-8')
    soup=BeautifulSoup(txt,'lxml')
    t=soup.find_all('table')[2]  # table par code
    etabs={}
    for r in t.find_all('tr'):
        cells=[clean(c.get_text(' ')) for c in r.find_all(['td','th'])]
        if len(cells)!=2: continue
        code_cell,nom=cells
        if code_cell.lower()=='code' or code_cell=='' : continue
        m=re.match(r'^([A-Z0-9]{3,5})\b', code_cell)
        if not m: continue
        code=m.group(1)
        ann=code_cell[m.end():].strip()
        clauses,note=parse_annotation(ann)
        nd,nf=extract_name_dates(nom)
        # date_fin etab = plus petite date_debut parmi successeurs
        succ_dates=[c["date_debut"] for c in clauses if c["date_debut"]]
        date_fin=nf or (min(succ_dates) if succ_dates else None)
        rec=etabs.get(code)
        if rec is None:
            etabs[code]={"code":code,"nom":nom,"date_debut":nd,"date_fin":date_fin,
                         "voir_apres":clauses,"voir_avant":[],"note_source":note or None,
                         "ppn":None,"label_idref":None,"ecoles_doctorales":[]}
        else:
            # doublon eventuel -> fusion annotations
            rec["voir_apres"].extend(clauses)
    # deriver voir_avant par inversion du graphe des successeurs
    for code,rec in etabs.items():
        for cl in rec["voir_apres"]:
            for tgt in cl["codes"]:
                if tgt in etabs:
                    etabs[tgt]["voir_avant"].append({
                        "codes":[code],"date_debut":cl["date_debut"],
                        "date_fin":cl["date_fin"],"condition":cl["condition"]})
    # date_debut etab: si absent, max date_debut parmi predecesseurs
    for rec in etabs.values():
        if not rec["date_debut"]:
            preds=[v["date_debut"] for v in rec["voir_avant"] if v["date_debut"]]
            if preds: rec["date_debut"]=max(preds)
    out=sorted(etabs.values(), key=lambda r:r["code"])
    print("etablissements:",len(out))
    print("avec date_fin:",sum(1 for r in out if r["date_fin"]))
    print("avec voir_apres:",sum(1 for r in out if r["voir_apres"]))
    print("avec voir_avant:",sum(1 for r in out if r["voir_avant"]))
    json.dump(out, open("etablissements_base.json","w"), ensure_ascii=False, indent=2)
    # apercu de quelques cas
    for c in ("GREA","GREN","CHAM","AIXM","AIX1","BORU","BESA","AGUY"):
        r=next((x for x in out if x["code"]==c),None)
        if r: print("\n",json.dumps(r,ensure_ascii=False))
    return out

main()
