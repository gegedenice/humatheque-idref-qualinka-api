# Alignement des organisations sur IdRef — établissements de soutenance & écoles doctorales

Brique du workflow de catalogage assisté par IA (thèses/mémoires → Unimarc), en
pendant de la brique « personnes ». Contrairement à celle-ci, **l'alignement des
organisations n'est PAS un problème de désambiguïsation à la volée** : l'univers
est fermé et petit (231 établissements de soutenance, ~quelques centaines d'écoles
doctorales), chaque entité revient des milliers de fois dans le corpus. On construit
donc **une fois** une table de correspondance (référentiel → IdRef), puis on
interroge par simple recherche lexicale déterministe. Aucun embedding : le risque
n'est pas l'homonymie mais la confusion entre versions temporelles proches
(« Grenoble 1 » / « Grenoble » / « Université Grenoble Alpes »), que seule
**l'année de soutenance** permet de trancher.

## Pipeline

| Étape | Script | Réseau | Entrée → Sortie |
|------|--------|--------|-----------------|
| 1 | `parse_abes.py` | non | page HTML ABES → `etablissements_base.json` |
| 2 | `harvest_idref.py` | **theses.fr + idref.fr** | base → `organisations_full.json` |
| 3 | `build_index.py` | non | full → `index_recherche.json` (+ `index_recherche.meta.json`, provenance) |
| 4 | `app.py` / `lookup.py` | non | libellé + année → PPN + libellé officiel |

Tout lancer : `python run_all.py` (l'étape 1 est sautée si
`etablissements_base.json` existe : elle demande `bs4`/`lxml` et la page HTML source).

> `enrich_ppn.py` (SRU Sudoc) et `enrich_ecoles_doctorales.py` (SPARQL) sont
> **remplacés** par `harvest_idref.py`. Ils restent sur le disque pour
> l'auditabilité mais ne sont plus dans le pipeline.

## Logique de chaque étape

**1. `parse_abes.py`** — parse la page ABES « Tables des codes des universités et
établissements ». Extrait, par code : le nom, les dates de validité, et les
annotations « Voir … » qu'il structure en `voir_apres` (successeurs, avec plages
de dates, scissions « BOR1, BOR2 ou BOR3 », chaînages « puis », conditions par
discipline). Les `voir_avant` (prédécesseurs) sont **dérivés par inversion** du
graphe. `note_source` conserve l'annotation brute (auditable).

**2. `harvest_idref.py`** — deux passes, `urllib` seul (aucune dépendance ajoutée) :

*A. code ABES → PPN.* Le code d'établissement n'existe dans aucune notice ni
aucun index de recherche IdRef ; il n'apparaît qu'au milieu du NNT. On interroge
`theses.fr/api/v1/theses/recherche/?q=nnt:*{CODE}*&nombre=300` et on lit
`etabSoutenancePpn`. La troncature à gauche fonctionne, donc **aucune année à
deviner** — c'est le gain sur `enrich_ppn.py`, qui essayait une liste d'années
candidates contre le SRU et abandonnait s'il tombait à côté. Les codes
antérieurs au NNT (PAFD, 1896) ne renvoient rien et restent `ppn: null` ; le SRU
ne les trouvait pas non plus.

Deux pièges, tous deux corrigés **en gardant l'échantillon, pas le premier hit** :

1. *Le joker de tête matche le code ailleurs dans le NNT.* Un NNT vaut
   `AAAA` + code sur 4 caractères + suffixe : `*HESA*` remonte `2016EHESA001`
   (EHESS, pas HESAM), `*LY01*` remonte `2020GRALY015` (Grenoble). Les 231 codes
   ABES font tous exactement 4 caractères sans collision de préfixe, donc le
   filtre positionnel `nnt[4:8] == code` est exact. Sans lui, 6 PPN étaient faux
   et portaient deux codes contradictoires (bug « EHES | HESA »).
2. *theses.fr n'est pas exempt de NNT mal rattachés.* On vote donc à la majorité
   sur `etabSoutenancePpn`. `nombre=300` et pas 50 : les 50 premiers hits d'AGPT
   sont majoritairement d'anciennes thèses INA-PG et faisaient gagner le mauvais
   PPN.

*B. PPN → libellés, dates, graphe local.* `https://www.idref.fr/{ppn}.json`. Une
notice donne tout, parce que les zones 510 sont **typées** :

| zone | sens |
|---|---|
| `210$a` / `210$c` | libellé de référence + fenêtre de validité faisant autorité |
| `410$a` | formes rejetées / variantes — 8 pour GREA, 8 pour PA04 |
| `510 $5 a\|...` | prédécesseur (PPN en `$3`) |
| `510 $5 b\|...` | successeur |
| `510 $0 "École doctorale"` | **école doctorale**, avec PPN et dates |
| autres `510` | membres, composantes, laboratoires, équipes |

C'est le typage par `$0` qui fait foi, pas `$5` : `x|xp` couvre aussi les labos et
les composantes. On parcourt un niveau (toute ED / prédécesseur / successeur
découvert en 510 est à son tour récupéré), avec cache par PPN (`_cache_org.json`,
`_cache_code_ppn.json`) donc reprise possible.

Pourquoi l'étape B n'est pas optionnelle : l'ABES appelle PA04 **« Paris 4 »**,
l'extraction dit **« Université de Paris - Sorbonne »**. Ces deux libellés ne
matchent à aucun seuil. La liste `410$a` contient *« Université de
Paris-Sorbonne »*, qui normalise en correspondance exacte.

**3. `build_index.py`** — aplatit le référentiel en une liste plate d'entités
recherchables, **clé = PPN** (et non code ABES : le PPN est à la fois la sortie et
la clé de jointure de toutes les arêtes ; plusieurs codes peuvent pointer le même
PPN). Chaque nœud porte ses listes d'adjacence.

*Pourquoi ni JSON hiérarchique ni base graphe :* une liste plate + listes
d'adjacence **est** un graphe — la représentation standard à cette taille. ~750
nœuds parcourus linéairement avec `SequenceMatcher`, c'est ~2 ms ; un index
inversé ou une base graphe serait de la cérémonie. Et ça reste un seul fichier
JSON, sans dépendance.

```json
{
  "type": "etablissement",
  "ppn": "026403633",
  "codes": ["PA04"],
  "label_officiel": "Université Paris-Sorbonne",
  "labels": ["Université Paris-Sorbonne", "Paris 4", "Université de Paris-Sorbonne",
             "Université Paris IV", "Université de Paris 4", "..."],
  "numeros": [],
  "date_debut": "1970", "date_fin": "2017",
  "successeurs": [{"ppn": "221333754", "label": "Sorbonne Université", "date_debut": "2018"}],
  "predecesseurs": [], "ecoles_doctorales": [], "etablissements": []
}
```

**4. La recherche** — le scoring vit dans `app.py` (`align_organization`,
`org_similarity`) et est exposé par les routes `POST /align/organization`,
`POST /align/thesis-organizations` et `GET /org-index/search`. `lookup.py` n'est
plus qu'un CLI au-dessus, pour qu'il n'existe qu'une seule implémentation.

Normalisation (minuscules, sans accents, sans ponctuation, mots-vides retirés) +
score combiné `SequenceMatcher` (ordre) + Jaccard de tokens (mots partagés) +
bonus d'inclusion. Filtre temporel par année. Décision sur le modèle de la brique
personnes : `accepted` / `ambiguous` / `low_confidence` / `not_found`. Seul
`accepted` renvoie un PPN.

Quatre différences avec la version initiale de `lookup.py` :

1. **Score contre toutes les `labels`** (5 à 10 variantes par entité au lieu d'une).
   Le gain de précision le plus important, et il vient gratuitement de l'étape B.
2. **Vivier des ED réduit à l'établissement parent.** Une fois
   `granting_institution` résolu, les écoles doctorales candidates sont
   restreintes aux `ecoles_doctorales` de ce nœud (∪ celles de ses prédécesseurs
   et successeurs, une ED survivant à la version d'établissement qui l'a créée),
   avec repli sur le vivier global si vide.
3. **Numéros d'ED** (`« ED 472 »`, `« ED472 »`) confrontés aux numéros extraits des
   libellés : signal décisif quand il est présent.
4. **Redirection temporelle.** Quand le meilleur candidat est hors fenêtre mais a
   un successeur valide cette année-là, le successeur est exposé dans `redirect`
   — **jamais accepté automatiquement**, c'est un indice pour l'humain.

```bash
python lookup.py "Université Grenoble Alpes" --annee 2018
python lookup.py "École doctorale mathématiques" --type ecole_doctorale --parent 184668794
python lookup.py --selftest        # la vérification exécutable du référentiel
```

## Points d'attention validés par les tests

- **L'année de soutenance est indispensable** pour désambiguïser les versions
  temporelles homonymes. Sans elle (ou quand deux notices IdRef coexistent pour la
  même période, cas « Université Grenoble Alpes » 2018), la réponse est `ambiguous`
  et aucun PPN n'est renvoyé.
- **Les variantes `410$a` (étape 2) conditionnent la qualité de la recherche** : le
  `nom` ABES est souvent réduit à la ville (« Bordeaux », « Chambéry ») ou au numéro
  (« Paris 4 »), qui matche mal un libellé de page de titre du type « Université de
  Paris - Sorbonne ». L'enrichissement n'est donc pas optionnel.
- Les cas non résolus (établissement disparu sans NNT électronique, structure
  antérieure aux écoles doctorales) tombent en `low_confidence` / `not_found` →
  file de validation humaine. C'est le comportement sûr : mieux vaut ne pas aligner
  que mal aligner.
- **Mémoires (master) :** theses.fr / le NNT ne couvrent que le doctorat. Un
  établissement présent uniquement via des mémoires n'aura pas de PPN par la voie
  NNT → repli `allow_remote` (recherche SPARQL `foaf:Organization` sur
  data.idref.fr, désactivé par défaut) + validation humaine. Même repli pour les
  partenaires de co-tutelle étrangers, absents par construction de la table ABES.
