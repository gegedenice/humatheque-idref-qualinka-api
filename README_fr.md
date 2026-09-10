# API Humatheque IdRef Qualinka

Service FastAPI pour aligner des noms de personnes extraits avec des notices
d'autorite françaises IdRef. Le service est conçu pour un pipeline de
catalogage dans lequel des métadonnees ont d'abord été extraites d'images de
pages de titre de thèses ou mémoires, et ou l'étape suivante
consiste à trouver le PPN IdRef le plus plausible pour chaque personne extraite.

L'API est volontairement déterministe : elle génère des PPN candidats
d'autorites, récupère des indices pour chaque candidat, calcule des scores
transparents, puis retourne soit un PPN accepté, soit un statut d'abstention.

## Pourquoi ce service 

Les données d'entrée contiennent generalement des champs extraits comme :

- auteur
- directeur
- president du jury
- rapporteurs
- membres du jury
- titre
- discipline
- etablissement
- ecole doctorale
- type de diplome
- annee de soutenance

Le service aligne une personne à la fois. Par exemple, à partir du nom extrait
`Valérie Robert` et des metadonnées documentaires environnantes, il essaie
d'identifier le PPN de l'autorite IdRef correspondante en utilisant pour chaque autorité candidate 
les indices issus de la notice d'autorite et les indices du voisinage bibliographique.

## API externes utilisees

### Qualinka `find-ra-idref`

Endpoint :

```text
https://qualinka.idref.fr/data/find-ra-idref/api/v2/req
```

Ce service est utilise pour générer les candidats. Il accepte un nom de famille
analyse et un prénom optionnel :

```text
?lastName=robert&firstName=val%C3%A9rie
```

Il retourne des PPN candidats d'autorites personnes IdRef. Il est préféré à une
simple requete Solr IdRef écrite à la main parce qu'il compacte plusieurs
stratégies de recherche propres à IdRef et gère mieux la recherche par nom de
personne.

### Qualinka `attrra`

Endpoint :

```text
https://qualinka.idref.fr/data/attrra/api/v2/req?ra_id=<PPN>
```

Ce service retourne pour PPN d'autorité donné les informations issues de la notice
d'autorité. Les champs les plus utiles sont :

- `preferedform` : libellé préféré de l'autorité, utilisé pour comparer les noms.
- `source` : texte de source bibliographique rattaché à la notice d'autorité.
- `noteGen` : notes générales, contenant souvent le diplôme, l'établissement, la discipline ou l'année.
- `bioNote` : notes biographiques, utilisées comme indice de note lorsqu'elles sont présentes.

Pour l'alignement de theses, `attrra.source`, `attrra.noteGen` et
`attrra.bioNote` peuvent être plus forts que les références liées génériques,
car ils décrivent souvent précisément pourquoi la notice d'autorite a été créée.

### IdRef `references`

Endpoint :

```text
https://www.idref.fr/services/references/<PPN>.json
```

Ce service retourne les notices bibliographiques liées à une autorite, groupées
par rôle. Ici, les roles sont conservés comme metadonnées
d'explicabilité, mais ils ne sont pas utilisés comme signal fort de classement.
(Par exemple, un directeur de thèse peut apparaitre principalement comme auteur dans IdRef, et
les libellés de rôle peuvent introduire un biais.)

## Logique d'alignement

`POST /align/person` exécute le flux complet.

1. Analyser le `name` soumis en prénom et nom.
2. Utiliser optionnellement les surcharges `first_name` et `last_name` lorsque l'analyse automatique est incertaine.
3. Interroger Qualinka `find-ra-idref` pour obtenir les PPN candidats.
4. Pour chaque PPN candidat :
   - récupérer `attrra`
   - récupérer les `references` IdRef
   - extraire les formes préférées, notes d'autorité (`noteGen` et `bioNote`), sources d'autorité et citations de references
5. Construire le contexte du document courant à partir de :
   - nom de la personne
   - titre
   - sous-titre
   - discipline
   - établissement
   - ecole doctorale
   - type de diplôme
   - année
6. Noter chaque candidat avec des composantes d'indices separées.
7. Classer les candidats par score final.
8. Accepter uniquement si le meilleur candidat dépasse le seuil et dispose d'une marge suffisante sur le deuxième candidat.

Le service utilise deux types de similarité différents :

- similarité lexicale de chaine pour le nom d'autorité lui-même -> score `name` lexical
- similarité sémantique pour les indices bibliographiques comme `attrra.source`, `attrra.noteGen`, `attrra.bioNote` et les citations de références IdRef -> score sémantique

Le score de nom est toujours fondé sur des chaines. Les scores sémantiques
bibliographiques peuvent fonctionner soit en mode lexical, soit en mode
embedding.

### Modes de similarité

#### Similarité de chaine pour les noms

Le score `name` compare le nom de personne extrait avec les formes d'autorité
candidates au moyen d'une similarité de chaine normalisée (fuzzy score basé sur la classe python SequenceMatcher).

Normalisation :

- suppression des accents
- passage en minuscules
- remplacement de la ponctuation par des espaces
- comparaison du recouvrement des tokens et de la similarité de caractères

Ce score n'est volontairement pas fondé sur des embeddings. Les noms exigent un
indice d'identité strict ; un modèle d'embedding pourrait rendre deux personnes
differentes proches parce que leurs noms ou leurs sujets sont semantiquement
voisins.

#### Similarite sémantique lexicale

C'est le mode par défaut pour les indices bibliographiques avec un simple bag-of-words + count vector (similaire dans l'esprit à CountVectorizer)

Il est utilisé lorsque :

```json
"embedding_model": ""
```

et lorsque `.env` contient :

```env
IDREF_EMBEDDING_MODEL=
```

Le service construit des vecteurs de tokens normalisés basés sur du comptage d'occurrences et calcule
une similarité cosinus. C'est léger, déterministe, et aucun modèle de machine
learning n'est chargé.

#### Similarite sémantique par embedding

Le mode embedding est utilisé lorsqu'un nom de modèle encoder sentence-transformers est
fourni.

Par requete :

```json
{
  "name": "Valérie Robert",
  "title": "...",
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

Globalement via `.env` :

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

En mode embedding, le service encode le contexte du document courant et chaque
texte d'indice candidat avec `SentenceTransformer(...).encode(...,
normalize_embeddings=True)`, puis calcule une similarité par produit scalaire.
Comme les vecteurs sont normalisés, le produit scalaire correspond à une
similarite cosinus.

Le mode embedding peut améliorer la proximité bibliographique, surtout lorsque
la formulation differe entre les metadonnées extraites et les indices IdRef. 
(Mais le chargement du modèle augmente aussi la latence de la premiere requete).

La réponse de `/align/person` inclut :

```json
"similarity": {
  "type": "lexical",
  "model": null
}
```

ou :

```json
"similarity": {
  "type": "embedding",
  "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

## Calcul Du Score

Chaque candidat reçoit cinq scores de composantes.

### `name`

Similarité de chaine entre le nom de personne extrait et les formes d'autorité
candidates :

- `attrra.preferedform[*].value`
- prénom + nom du candidat provenant de `find-ra-idref`

Ce score est séparé de la similarité semantique afin qu'un candidat ayant un
sujet proche mais un mauvais appariement de nom ne "gagne" pas trop facilement.

### `attrra_source`

Meilleure similarité sémantique entre le contexte du document courant et chaque
valeur de `attrra.source`.

Ce score utilise par defaut la similarité sémantique lexicale, ou la similarité
sémantique par embedding lorsque `embedding_model` / `IDREF_EMBEDDING_MODEL` est
defini.

Il est fortement pondéré parce que `source` peut contenir des indices proches
d'une thèse, comme le titre, la date, l'établissement et le nom de l'auteur.

### `attrra_note`

Meilleure similarité sémantique entre le contexte du document courant et chaque
valeur de `attrra.noteGen` ou `attrra.bioNote`.

Ce score utilise le même mode sémantique que `attrra_source`.

Il est utile lorsque les notes contiennent des informations comme :

```text
Titulaire d'un doctorat d'université en médecine spécialisée (Nancy 1,2003)
Auteur d'une thèse en Sciences cognitives, psychologie et neurocognition à Université Grenoble Alpes en 2023
```

### `references`

Moyenne top-k des similarités sémantiques entre le contexte du document courant
et les citations de références liées au candidat dans IdRef.

Le service utilise un top-k plutot qu'une moyenne de toutes les références. Cela
évite de pénaliser les auteurs prolifiques dont la bibliographie large diluerait
le signal.

Par defaut :

```text
reference_top_k = 3
```

### `institution_year`

Petit score déterministe de cohérence :

- `+0.50` si l'établissement extrait apparait dans les indices du candidat
- `+0.25` si l'école doctorale extraite apparait dans les indices du candidat
- `+0.25` si l'année extraite apparait dans les indices du candidat

Le score est plafonne à `1.0`.

### Score Final

```text
final =
  weight_name * name
+ weight_attrra_source * attrra_source
+ weight_attrra_note * attrra_note
+ weight_references * references
+ weight_institution_year * institution_year
```

Poids par défaut :

```text
weight_name = 0.40
weight_attrra_source = 0.25
weight_attrra_note = 0.15
weight_references = 0.15
weight_institution_year = 0.05
```

Seuils par défaut :

```text
accept_threshold = 0.65
margin_threshold = 0.08
```

Logique de décision :

```text
if no candidates:
    status = "not_found"
elif top.final < accept_threshold:
    status = "low_confidence"
elif top.final - second.final < margin_threshold:
    status = "ambiguous"
else:
    status = "accepted"
```

`best_ppn` n'est renseigné que lorsque `status` vaut `accepted`.

## Endpoints API

La documentation interactive est disponible à :

```text
/docs
```

### `GET /health`

Controle de santé du conteneur.

Réponse :

```json
{"ok": true}
```

### `GET /find-person`

Exécute uniquement la génération de candidats via Qualinka `find-ra-idref`.

Paramètres de requête :

| Paramètre | Requis | Description |
|---|---:|---|
| `name` | non | Nom complet de la personne à analyser |
| `first_name` | non | Surcharge du prénom analyse |
| `last_name` | non | Surcharge du nom analyse |
| `max_results` | non | Nombre maximum de candidats, par defaut `20` |

`name` ou `last_name` doit etre fourni.

Exemple :

```bash
curl "http://localhost:8000/find-person?name=Val%C3%A9rie%20Robert"
```

### `GET /attrra/{ppn}`

Recupère les indices Qualinka `attrra` pour un PPN IdRef.

Exemple :

```bash
curl "http://localhost:8000/attrra/076642860"
```

### `GET /references/{ppn}`

Recupère les références bibliographiques IdRef liées à un PPN.

Exemple :

```bash
curl "http://localhost:8000/references/076642860?max_docs_per_role=10"
```

### `POST /align/person`

Exécute le pipeline complet d'alignement.

Corps de requête :

```json
{
  "name": "Valérie Robert",
  "title": "Satisfaction et vécu périopératoire des patients opérés sous anesthésie péribulbaire",
  "subtitle": "",
  "discipline": "médecine spécialisée",
  "institution": "Nancy 1",
  "doctoral_school": "",
  "degree_type": "Thèse d'exercice",
  "year": "2003",
  "max_candidates": 20,
  "max_docs_per_role": 20,
  "reference_top_k": 3,
  "embedding_model": "",
  "accept_threshold": 0.65,
  "margin_threshold": 0.08,
  "weight_name": 0.40,
  "weight_attrra_source": 0.25,
  "weight_attrra_note": 0.15,
  "weight_references": 0.15,
  "weight_institution_year": 0.05
}
```

`embedding_model` controle le scoring sémantique des indices bibliographiques :

- chaine vide : similarité cosinus lexicale sur tokens
- nom de modèle sentence-transformers non vide : similarité cosinus par embedding

Exemple :

```bash
curl -X POST "http://localhost:8000/align/person" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "name": "Valérie Robert",
    "title": "Satisfaction et vécu périopératoire des patients opérés sous anesthésie péribulbaire",
    "discipline": "médecine spécialisée",
    "institution": "Nancy 1",
    "degree_type": "Thèse d'exercice",
    "year": "2003"
  }'
```

Forme de la réponse :

```jsonc
{
  "source": "idref_qualinka_alignment",
  "similarity": {
    "type": "lexical",
    "model": null
  },
  "query": {
    "name": "Valérie Robert",
    "title": "...",
    "subtitle": "",
    "discipline": "médecine spécialisée",
    "institution": "Nancy 1",
    "doctoral_school": "",
    "degree_type": "Thèse d'exercice",
    "year": "2003"
  },
  "candidate_search": {
    "full_name": "Valérie Robert",
    "first_name": "Valérie",
    "last_name": "Robert",
    "parsed_first_name": "Valérie",
    "parsed_last_name": "Robert",
    "url": "https://qualinka.idref.fr/...",
    "error": null
  },
  "status": "accepted",
  "best_ppn": "076642860",
  "best_candidate": {
    "ppn": "076642860",
    "first_name": "Valérie",
    "last_name": "Robert",
    "url": "https://www.idref.fr/076642860",
    "score": {
      "final": 0.6783,
      "name": 1.0,
      "attrra_source": 0.743,
      "attrra_note": 0.421,
      "references": 0.0,
      "institution_year": 0.75
    },
    "evidence": {
      "preferred_forms": ["Robert, Valérie"],
      "best_attrra_source": "...",
      "best_attrra_note": "...",
      "best_references": []
    },
    "errors": []
  },
  "candidates": []
}
```

## Alignement des organisations

`POST /align/thesis-organizations` aligne le bloc « organisations » de la même
extraction sur des PPN IdRef — les champs que les routes personnes ignorent :

```json
{
  "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
  "co_tutelle_institutions": [],
  "doctoral_school": "Ecole doctorale 493 ERASME",
  "defense_year": "2015"
}
```

### Pourquoi la méthode diffère de celle des personnes

Les personnes forment un univers ouvert : des millions d'autorités, des homonymes
partout, donc les candidats doivent être générés à la volée par Qualinka puis
départagés par des indices bibliographiques. Les organisations sont l'inverse :

- L'univers est **fermé et petit** — 231 codes d'établissement ABES, quelques
  centaines d'écoles doctorales — et chaque thèse réutilise les mêmes entités.
- Le risque n'est pas l'homonymie mais les **versions temporelles d'un même
  établissement** : « Paris 4 » / « Université Paris-Sorbonne » / « Sorbonne
  Université » sont trois PPN pour une entité continue, et seule l'année de
  soutenance les sépare.
- Les libellés extraits sont administrativement flous mais lexicalement proches
  d'une forme officielle ou d'une forme rejetée (« Université de Paris - Sorbonne »
  vs « Université Paris-Sorbonne »).

Il n'y a donc **ni service de génération de candidats, ni mode embedding ici**. Un
embedding rendrait « Université Paris-Sorbonne » et « Sorbonne Université »
identiques, c'est-à-dire exactement la confusion à éviter. Les candidats sont
résolus contre un **référentiel local** construit une fois hors ligne, notés par
similarité de chaîne, puis filtrés par fenêtre de validité.

### Le référentiel local

Construit par `idref_org_alignment/`, à partir de deux sources publiques (pas de
Qualinka) :

| Étape | Source | Ce qu'elle apporte |
|---|---|---|
| `parse_abes.py` | `documentation_abes_codes_etab.htm` | les 231 codes d'établissement ABES |
| `harvest_idref.py` A | `theses.fr/api/v1/theses/recherche/?q=nnt:*CODE*&nombre=1` | `etabSoutenancePpn` — la jointure code → PPN, qui n'existe nulle part ailleurs |
| `harvest_idref.py` B | `https://www.idref.fr/{ppn}.json` | UNIMARC `210$a/$c` (libellé + fenêtre de validité), `410$a` (formes rejetées), `510` (prédécesseurs, successeurs, membres, écoles doctorales) |
| `harvest_idref.py` C | SPARQL `data.idref.fr`, une requête sur `prefLabel ~ "^École doctorale"` | les écoles doctorales, que les notices d'établissement ne listent pas |
| `build_index.py` | — | `index_recherche.json` + `index_recherche.meta.json` (date de build, endpoints sources, volumétrie) |

L'étape B est ce qui fait fonctionner l'ensemble. L'ABES appelle `PA04`
**« Paris 4 »** ; l'extraction dit **« Université de Paris - Sorbonne »**. Ces deux
chaînes ne s'apparient à aucun seuil. La liste des `410$a` contient *« Université
de Paris-Sorbonne »*, qui normalise en correspondance exacte. Médiane : 6 libellés
par entité.

Le construire d'abord (accès réseau à `theses.fr` et `www.idref.fr`, ~15 min,
entièrement mis en cache et reprenable) :

```bash
cd idref_org_alignment && python run_all.py && python lookup.py --selftest
```

Couverture actuelle : **208 / 231** codes ABES résolus en PPN (les manques sont
des entrées historiques antérieures au NNT, par exemple un doctorat de 1896 : pas
de NNT, donc pas de clé de jointure), **268 établissements** et **516 écoles
doctorales**, dont 421 portent un numéro d'ED et 396 un établissement de
rattachement.

`IDREF_ORG_INDEX_PATH` pointe sur le `index_recherche.json` produit. Si le fichier
est absent, les routes organisations renvoient `503` avec la commande de
construction ; `/align/person` n'est pas affectée.

### Structure de l'index

Ni JSON hiérarchique, ni base graphe : une **liste plate indexée par PPN, chaque
nœud portant ses listes d'adjacence** — ce qui *est* un graphe en listes
d'adjacence, la représentation standard à cette taille. Cela reste un seul
fichier, sans dépendance supplémentaire, et un parcours linéaire de ~780 nœuds
avec `SequenceMatcher` coûte ~2 ms. Le PPN est à la fois la sortie et la clé de
jointure de chaque arête, donc les codes ABES deviennent une simple liste
(plusieurs codes peuvent pointer sur un même PPN).

```json
{
  "type": "etablissement",
  "ppn": "026403633",
  "codes": ["PA04"],
  "label_officiel": "Université Paris-Sorbonne",
  "labels": ["Université Paris-Sorbonne", "Paris 4", "Université de Paris-Sorbonne",
             "Université Paris IV", "Université de Paris 4"],
  "numeros": [],
  "date_debut": "1970",
  "date_fin": "2017",
  "predecesseurs": [],
  "successeurs": [{"ppn": "221333754", "label": "Sorbonne Université"}],
  "ecoles_doctorales": ["149119070", "..."]
}
```

Les nœuds d'école doctorale ont la même forme avec `"type": "ecole_doctorale"`,
`numeros: ["493"]` et la réciproque `etablissements: ["02640463X", ...]`.

### Logique d'alignement

`align_organization` exécute, pour un libellé :

1. Construire le **vivier de candidats**. Établissements : tous. Écoles
   doctorales : si `parent_ppn` est connu, seulement les écoles de cet
   établissement — élargi aux écoles de ses prédécesseurs et successeurs, une
   école survivant à la version d'établissement sous laquelle elle a été créée.
   Un vivier restreint vide retombe sur le global. Rapporté dans
   `candidate_scope` et `pool_size`.
2. **Noter chaque candidat** contre *tous* ses libellés, en gardant le meilleur.
3. **Filtrer par fenêtre de validité** : les candidats valides à l'année de
   soutenance l'emportent en bloc ; si aucun ne convient, tous sont classés.
4. **Décider** `accepted` / `ambiguous` / `low_confidence` / `not_found`.
5. Si le meilleur candidat est hors période, attacher un **`redirect`** vers son
   successeur valide cette année-là.
6. Optionnellement, sur un échec local, lancer la **recherche SPARQL distante**.

La route composite fait le vrai travail de l'étape 1 : elle aligne d'abord
`granting_institution` et passe son PPN comme `parent_ppn` pour `doctoral_school`.
Sur l'exemple ci-dessus, cela fait passer le vivier de 516 candidats à 3.

### Calcul du score

Un seul score de chaîne, aucun poids à régler, volontairement **différent** de
`name_similarity` des personnes — celle-ci ne gère pas les mots vides, ce qui
compte ici (« Université **de** Paris ») :

```text
score = 0.5 * SequenceMatcher(libellé_normalisé, libellé_candidat_normalisé)
      + 0.4 * Jaccard(tokens_libellé, tokens_candidat)     # mots vides retirés
      + 0.1 * (1 si tokens_libellé ⊆ tokens_candidat sinon 0)
```

La normalisation est le `normalize_text` existant : accents supprimés, minuscules,
ponctuation remplacée par des espaces. Les tokens retirent en plus les mots vides
français et anglais (`de`, `du`, `la`, `et`, `the`, `of`, …).

- Le **ratio de séquence** capte l'ordre des mots et les petites coquilles.
- Le **Jaccard** capte les formes réordonnées ou tronquées (« Sorbonne Paris Nord,
  Université de »).
- Le **bonus d'inclusion** récompense un libellé extrait strictement inclus dans
  une forme officielle (« Paris 13 » ⊂ « Université Paris 13 Nord »).

**Surcharge par numéro d'ED.** Si la requête contient un numéro d'école doctorale
(« ED 472 », « ED472 », « Ecole doctorale 493 ERASME ») et que le candidat porte le
même numéro dans `numeros`, le score est plancher à `0.95`. C'est un identifiant
décisif, qui survit à un renommage complet de l'école.

Le score final est le maximum sur les variantes de libellé du candidat — jamais
une moyenne : une seule bonne variante suffit.

### Seuils et décision

```text
accept_threshold = 0.72
margin_threshold = 0.08
low_threshold    = 0.45
floor_threshold  = 0.30
```

```text
if top < floor_threshold:                        status = "not_found"
elif top < low_threshold:                        status = "low_confidence"
elif top >= accept_threshold and margin >= margin_threshold:
                                                 status = "accepted"
elif top >= accept_threshold:                    status = "ambiguous"
else:                                            status = "low_confidence"
```

`ppn` n'est renseigné que lorsque `status` vaut `accepted`. Les seuils sont plus
hauts que ceux des personnes (`0.72` contre `0.65`) parce que les libellés sont
beaucoup plus courts et qu'un univers fermé rend les quasi-correspondances peu
coûteuses à rejeter : soit la bonne réponse est dans le référentiel avec une
variante correspondante, soit elle n'y est pas du tout.

Le double plancher existe pour séparer les deux modes d'échec dans la file de
validation humaine : `low_confidence` signifie *quelque chose de plausible a été
trouvé et demande un regard*, `not_found` signifie *rien dans le référentiel ne
ressemble à cela*.

### Redirection temporelle

Les fenêtres de validité viennent de `210$c` : elles font autorité, elles ne sont
pas devinées. Quand la meilleure entité est hors période pour l'année de
soutenance et possède un successeur valide cette année-là, la réponse porte :

```json
"redirect": {
  "reason": "matched_label_out_of_period",
  "from_ppn": "026403633",
  "ppn": "221333754",
  "label_officiel": "Sorbonne Université",
  "date_debut": "2018",
  "date_fin": null
}
```

C'est un **indice pour l'humain, jamais une acceptation automatique** : une thèse
de 2020 dont la couverture porte « Université Paris-Sorbonne » est une question de
catalogage, pas d'appariement de chaînes. Dans la plupart des cas, la redirection
n'est même pas nécessaire : le successeur porte généralement l'ancien nom parmi
ses propres `410$a` et remporte simplement le classement.

### Recherche distante

`allow_remote: true`, **désactivée par défaut**. Ne se déclenche que si le
résultat local est `not_found` ou `low_confidence`. Elle lance une requête SPARQL
`data.idref.fr` en direct sur `foaf:Organization` / `skos:prefLabel`, filtrée sur
le token non générique le plus long du libellé (chercher « universitat » ramène la
moitié de l'Europe avant Heidelberg), puis renote les résultats avec la même
similarité locale.

Les résultats sont renvoyés **à part** sous `remote`, marqués
`source: "idref_sparql"`, et jamais fusionnés dans `candidates`. Cela couvre les
partenaires de co-tutelle étrangers et les établissements présents seulement via
des mémoires, absents par construction de la table ABES. Compter 30 à 95 s :
l'endpoint est lent, le timeout est donc relevé à 90 s au minimum pour ce seul
appel.

### `POST /align/thesis-organizations`

La route appelée par le pipeline.

Corps de requête :

```json
{
  "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
  "co_tutelle_institutions": [],
  "doctoral_school": "Ecole doctorale 493 ERASME",
  "defense_year": "2015",
  "allow_remote": false,
  "max_candidates": 5,
  "accept_threshold": 0.72,
  "margin_threshold": 0.08,
  "low_threshold": 0.45,
  "floor_threshold": 0.30
}
```

Le champ canonique est `year`, comme partout ailleurs dans cette API : la même
forme de requête se généralise à d'autres sources que les thèses. La clé du schéma
d'extraction, `defense_year`, est acceptée comme alias : son bloc peut donc être
posté tel quel. Tous les seuils sont surchargeables par requête, comme pour
`/align/person`.

Exemple :

```bash
curl -X POST "http://localhost:8000/align/thesis-organizations" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
    "co_tutelle_institutions": [],
    "doctoral_school": "Ecole doctorale 493 ERASME",
    "defense_year": "2015"
  }'
```

Forme de la réponse — un résultat `/align/organization` par champ d'entrée,
`co_tutelle_institutions` étant une liste de ces résultats, `null` pour un champ
d'entrée vide :

```jsonc
{
  "source": "idref_org_referential",
  "query": {
    "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
    "co_tutelle_institutions": [],
    "doctoral_school": "Ecole doctorale 493 ERASME",
    "year": "2015"
  },
  "granting_institution": {
    "source": "idref_org_referential",
    "query": {"label": "...", "kind": "institution", "year": "2015", "parent_ppn": null},
    "candidate_scope": "global",
    "pool_size": 268,
    "status": "accepted",
    "ppn": "02640463X",
    "label_officiel": "Université Sorbonne Paris Nord",
    "score": 0.9815,
    "margin": 0.2448,
    "redirect": null,
    "candidates": [
      {
        "ppn": "02640463X",
        "label_officiel": "Université Sorbonne Paris Nord",
        "type": "etablissement",
        "codes": ["PA13"],
        "score": 0.9815,
        "valid_for_year": true,
        "date_debut": "1970",
        "date_fin": null,
        "url": "https://www.idref.fr/02640463X"
      }
    ],
    "error": null
  },
  "co_tutelle_institutions": [],
  "doctoral_school": {
    "candidate_scope": "parent",   // restreint par le PPN d'établissement ci-dessus
    "pool_size": 3,                // au lieu de 516
    "status": "accepted",
    "ppn": "177537957",
    "label_officiel": "École doctorale Érasme",
    "score": 0.7583,
    "margin": 0.2206
  }
}
```

### `POST /align/organization`

Un libellé à la fois — la primitive sur laquelle la route composite est bâtie.
Utile pour réaligner un seul champ après correction humaine, ou pour aligner une
organisation qui ne vient pas d'un bloc de thèse.

| Champ | Défaut | Description |
|---|---|---|
| `label` | requis | Libellé d'organisation extrait |
| `kind` | `"institution"` | `"institution"` ou `"doctoral_school"` |
| `year` | vide | Année de soutenance ; sélectionne la version temporelle (`defense_year` accepté en alias) |
| `parent_ppn` | vide | PPN d'établissement restreignant la recherche d'école doctorale |
| `allow_remote` | `false` | Recherche SPARQL distante sur échec local |
| `max_candidates` | `5` | Nombre de candidats retournés |
| `accept_threshold` | `0.72` | Score minimum pour accepter |
| `margin_threshold` | `0.08` | Marge minimale entre premier et deuxième |
| `low_threshold` | `0.45` | En dessous : `low_confidence` |
| `floor_threshold` | `0.30` | En dessous : `not_found` |
| `timeout`, `retries`, `backoff` | défauts d'environnement | Recherche distante uniquement |

```bash
curl -X POST "http://localhost:8000/align/organization" \
  -H "Content-Type: application/json" \
  -d '{"label": "Ecole doctorale 493 ERASME", "kind": "doctoral_school",
       "year": "2015", "parent_ppn": "02640463X"}'
```

### `GET /org-index/search`

Inspection du référentiel, suivant la convention de `/find-person`,
`/attrra/{ppn}`, `/references/{ppn}`. Même scoring, même forme de réponse.

| Paramètre | Requis | Description |
|---|---:|---|
| `q` | oui | Libellé à confronter au référentiel |
| `year` | non | Année de soutenance |
| `type` | non | `etablissement` (défaut) ou `ecole_doctorale` |
| `parent_ppn` | non | PPN d'établissement restreignant les écoles doctorales |
| `max_results` | non | Défaut `5` |

```bash
curl "http://localhost:8000/org-index/search?q=Paris%2013&year=2015"
```

### Limites connues

- **Établissements antérieurs à 1985.** 23 codes ABES n'ont pas de PPN parce
  qu'ils précèdent le NNT : aucune thèse ne les porte. Ces libellés retombent sur
  un appariement lexical global ou sur `not_found`.
- **Structures hors IdRef.** `"doctoral_school": "U.E.N. HISTOIRES"` — une
  structure sorbonnarde antérieure aux ED — renvoie `not_found` (meilleur score
  `0.28`). Il n'existe pas de notice d'autorité à trouver : s'abstenir est la
  bonne réponse, pas un seuil à contourner.
- **120 écoles doctorales n'ont pas de lien vers un établissement**, la restriction
  par `parent_ppn` ne les atteint donc pas ; elles restent trouvables globalement.
- Le référentiel est un **instantané**. Relancer `run_all.py` après des fusions
  d'établissements ; les caches rendent la relance peu coûteuse.

## Authentification

L'authentification est optionnelle.

Définir `IDREF_API_KEY` dans `.env` pour obliger les clients à envoyer :

```text
X-API-Key: <your key>
```

Si `IDREF_API_KEY` est vide, les endpoints sont publics.

## Variables D'environnement

Copier `.example.env` vers `.env` et ajuster les valeurs.

| Variable | Defaut | Description |
|---|---|---|
| `PORT` | `8000` | Port du serveur HTTP |
| `IDREF_API_KEY` | vide | Clé API optionnelle |
| `FIND_RA_ENDPOINT` | endpoint public Qualinka | Endpoint de génération de candidats |
| `ATTRRA_ENDPOINT` | endpoint public Qualinka | Endpoint d'enrichissement d'autorité |
| `REFERENCES_ENDPOINT` | endpoint public IdRef | Endpoint de références liées |
| `IDREF_USER_AGENT` | `humatheque-idref-qualinka-api/0.1` | User-Agent envoyé aux services publics |
| `IDREF_HTTP_TIMEOUT` | `20.0` | Timeout HTTP en secondes |
| `IDREF_MAX_RETRIES` | `2` | Nombre de tentatives pour les requêtes externes |
| `IDREF_BACKOFF_BASE` | `1.0` | Base de backoff exponentiel |
| `IDREF_MAX_CANDIDATES` | `20` | Limite de candidats par defaut |
| `IDREF_MAX_DOCS_PER_ROLE` | `20` | Nombre de documents de référence par rôle par défaut |
| `IDREF_REFERENCE_TOP_K` | `3` | Nombre de meilleures références moyennees |
| `IDREF_ACCEPT_THRESHOLD` | `0.65` | Score minimum pour accepter |
| `IDREF_MARGIN_THRESHOLD` | `0.08` | Marge minimale entre le premier et le deuxieme score |
| `IDREF_EMBEDDING_MODEL` | vide | Modèle sentence-transformers optionnel par defaut pour la similarité sémantique par embedding |
| `IDREF_WEIGHT_NAME` | `0.40` | Poids par défaut de la similarité du nom |
| `IDREF_WEIGHT_ATTRRA_SOURCE` | `0.25` | Poids par défaut de la similarité avec `attrra.source` |
| `IDREF_WEIGHT_ATTRRA_NOTE` | `0.15` | Poids par défaut de la similarité avec `noteGen`/`bioNote` |
| `IDREF_WEIGHT_REFERENCES` | `0.15` | Poids par défaut de la similarité avec les citations de références IdRef |
| `IDREF_WEIGHT_INSTITUTION_YEAR` | `0.05` | Poids par défaut de la cohérence établissement, école doctorale et année |
| `IDREF_ORG_INDEX_PATH` | `idref_org_alignment/index_recherche.json` | Référentiel local des organisations ; fichier absent = routes organisations en `503` |
| `IDREF_ORG_SPARQL_ENDPOINT` | `https://data.idref.fr/sparql` | Endpoint de la recherche distante optionnelle |
| `IDREF_ORG_ACCEPT_THRESHOLD` | `0.72` | Score minimum pour accepter une organisation |
| `IDREF_ORG_MARGIN_THRESHOLD` | `0.08` | Marge minimale entre premier et deuxième candidat |
| `IDREF_ORG_LOW_THRESHOLD` | `0.45` | En dessous, statut `low_confidence` |
| `IDREF_ORG_FLOOR_THRESHOLD` | `0.30` | En dessous, statut `not_found` |
| `IDREF_ORG_MAX_CANDIDATES` | `5` | Limite de candidats organisations par défaut |

Mode embedding pour toutes les requêtes :

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Lorsque cette variable est vide, toutes les requêtes utilisent la similarité
sémantique lexicale, sauf si le corps de requête fournit explicitement
`embedding_model`.

## Execution Locale

```bash
cp .example.env .env
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## Docker

```bash
docker build -t humatheque-idref-qualinka-api .
docker run --env-file .env -p 8000:8000 humatheque-idref-qualinka-api
```

## Notes opérationnelles

- Le service effectue des appels HTTP bloquants vers les endpoints publics
  IdRef et Qualinka. FastAPI les execute dans un threadpool pour les endpoints
  API.
- Les echecs en amont sont retournés dans les champs `errors` des candidats
  ou dans les champs `error` des réponses lorsque c'est possible.
- L'alignement est conservateur : préfère s'abstenir plutot que forcer un PPN
  IdRef.
- Les libellés de rôle des références IdRef sont conservés dans les indices,
  mais ne sont pas utilisés comme signal fort de scoring.
- Pour les traitements par lots, garder `max_candidates` et
  `max_docs_per_role` bornes afin d'éviter des appels lents aux services
  publics.
- L'alignement des organisations ne fait **aucun** appel réseau en mode par
  défaut : il ne lit que le référentiel local, il est donc assez rapide pour du
  traitement par lots. Seul `allow_remote: true` interroge `data.idref.fr`, dont
  l'endpoint répond en 30 à 95 s.
- Le référentiel des organisations est chargé paresseusement dans une variable
  globale du module au premier usage et n'est jamais rechargé : redémarrer le
  service après reconstruction de `index_recherche.json`.
  `index_recherche.meta.json`, écrit à côté par `build_index.py`, indique quand
  l'index a été construit et depuis quelles sources — à consulter avant de
  soupçonner l'aligneur d'être périmé.
