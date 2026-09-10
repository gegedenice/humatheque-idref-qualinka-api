#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "gradio>=4.0.0",
#   "requests>=2.31.0",
# ]
# ///
"""Small Gradio client for the Humatheque IdRef Qualinka API.

Run:
  UV_CACHE_DIR=/root/.cache/uv uv run --script gradio_app.py
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

import gradio as gr
import requests


DEFAULT_API_URL = os.getenv("IDREF_QUALINKA_API_URL", "https://idref-linker.smartbiblia.fr")
DEFAULT_API_KEY = os.getenv("IDREF_QUALINKA_API_KEY", "")
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

PERSON_FIELDS = [
    "author",
    "advisor",
    "jury_president",
    "reviewers",
    "committee_members",
]

FIELD_LABELS = {
    "author": "Auteur",
    "advisor": "Directeur",
    "jury_president": "President du jury",
    "reviewers": "Rapporteurs",
    "committee_members": "Membres du jury",
}

EXAMPLE_JSON = """{
  "title": "La question de l'hygiène aux Indes-Néerlandaises",
  "subtitle": "Les enjeux médicaux, culturels et sociaux",
  "author": "Gani Achmad Jae Lani",
  "degree_type": "Thèse de doctorat",
  "discipline": "Histoire et civilisations",
  "granting_institution": "École des Hautes Études en Sciences Sociales",
  "co_tutelle_institutions": [],
  "doctoral_school": "École doctorale de l'EHESS",
  "defense_year": 2017,
  "advisor": "Gérard Jorland",
  "jury_president": "",
  "reviewers": "",
  "committee_members": ["Romain Bertrand", "Patrice Bourdelais", "Charles Illouz", "Annick Opinel", "Patrick Zylberman", "Gérard Jorland"],
  "language": "fre",
  "confidence": 0.98
}"""


def split_people(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value).split("|")
    names = []
    for item in raw_items:
        name = str(item).strip()
        if name:
            names.append(name)
    return names


def normalize_name_key(name: str) -> str:
    return " ".join(name.lower().split())


def parse_extraction_json(json_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise gr.Error(f"JSON invalide: {exc}") from exc
    if not isinstance(payload, dict):
        raise gr.Error("Le JSON colle doit etre un objet.")
    return payload


def extract_people(payload: dict[str, Any]) -> list[dict[str, Any]]:
    people_by_key: dict[str, dict[str, Any]] = {}
    for field in PERSON_FIELDS:
        for name in split_people(payload.get(field)):
            key = normalize_name_key(name)
            if key not in people_by_key:
                people_by_key[key] = {"name": name, "roles": []}
            if field not in people_by_key[key]["roles"]:
                people_by_key[key]["roles"].append(field)
    return list(people_by_key.values())


def people_to_rows(people: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for person in people:
        labels = [FIELD_LABELS.get(role, role) for role in person["roles"]]
        rows.append([person["name"], " | ".join(labels)])
    return rows


def people_to_choices(people: list[dict[str, Any]]) -> list[str]:
    choices = []
    for person in people:
        labels = [FIELD_LABELS.get(role, role) for role in person["roles"]]
        choices.append(f"{person['name']} — {' | '.join(labels)}")
    return choices


def selected_choice_to_name(choice: str) -> str:
    return choice.split(" — ", 1)[0].strip()


def build_align_payload(
    extraction: dict[str, Any],
    selected_name: str,
    use_embeddings: bool,
    max_candidates: int,
    max_docs_per_role: int,
    reference_top_k: int,
    accept_threshold: float,
    margin_threshold: float,
    weight_name: float,
    weight_attrra_source: float,
    weight_attrra_note: float,
    weight_references: float,
    weight_institution_year: float,
) -> dict[str, Any]:
    return {
        "name": selected_name,
        "title": str(extraction.get("title") or ""),
        "subtitle": str(extraction.get("subtitle") or ""),
        "discipline": str(extraction.get("discipline") or ""),
        "institution": str(extraction.get("granting_institution") or extraction.get("institution") or ""),
        "doctoral_school": str(extraction.get("doctoral_school") or ""),
        "degree_type": str(extraction.get("degree_type") or ""),
        "year": str(extraction.get("defense_year") or extraction.get("year") or ""),
        "max_candidates": int(max_candidates),
        "max_docs_per_role": int(max_docs_per_role),
        "reference_top_k": int(reference_top_k),
        "embedding_model": EMBEDDING_MODEL if use_embeddings else "",
        "accept_threshold": float(accept_threshold),
        "margin_threshold": float(margin_threshold),
        "weight_name": float(weight_name),
        "weight_attrra_source": float(weight_attrra_source),
        "weight_attrra_note": float(weight_attrra_note),
        "weight_references": float(weight_references),
        "weight_institution_year": float(weight_institution_year),
    }


def status_badge(status: str) -> str:
    colors = {
        "accepted": ("#dcfce7", "#166534"),
        "ambiguous": ("#fef9c3", "#854d0e"),
        "low_confidence": ("#fee2e2", "#991b1b"),
        "not_found": ("#e5e7eb", "#374151"),
    }
    bg, fg = colors.get(status, ("#e5e7eb", "#374151"))
    return (
        f'<span style="display:inline-block;padding:4px 9px;border-radius:999px;'
        f'background:{bg};color:{fg};font-weight:700;font-size:13px">{html.escape(status)}</span>'
    )


def score_table(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    score = candidate.get("score") or {}
    rows = []
    for key in ["final", "name", "attrra_source", "attrra_note", "references", "institution_year"]:
        value = score.get(key)
        value_text = "" if value is None else f"{float(value):.4f}"
        rows.append(
            "<tr>"
            f"<td>{html.escape(key)}</td>"
            f"<td><strong>{value_text}</strong></td>"
            "</tr>"
        )
    return (
        '<table class="score-table">'
        "<thead><tr><th>Composante</th><th>Score</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def evidence_block(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    evidence = candidate.get("evidence") or {}
    forms = evidence.get("preferred_forms") or []
    best_refs = evidence.get("best_references") or []

    def value_block(label: str, value: Any) -> str:
        if value is None or value == []:
            content = '<span class="muted">Aucun indice</span>'
        elif isinstance(value, list):
            content = "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in value) + "</ul>"
        else:
            content = f"<p>{html.escape(str(value))}</p>"
        return f"<div class='evidence-item'><h4>{label}</h4>{content}</div>"

    return (
        "<div class='evidence-grid'>"
        + value_block("Formes preferees", forms)
        + value_block("Meilleure source attrra", evidence.get("best_attrra_source"))
        + value_block("Meilleure note attrra", evidence.get("best_attrra_note"))
        + value_block("Meilleures references", best_refs)
        + "</div>"
    )


def candidates_table(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return '<p class="muted">Aucun candidat.</p>'
    rows = []
    for candidate in candidates:
        score = candidate.get("score") or {}
        forms = (candidate.get("evidence") or {}).get("preferred_forms") or []
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(candidate.get('url') or '')}' target='_blank'>{html.escape(candidate.get('ppn') or '')}</a></td>"
            f"<td>{html.escape(candidate.get('first_name') or '')}</td>"
            f"<td>{html.escape(candidate.get('last_name') or '')}</td>"
            f"<td>{float(score.get('final') or 0):.4f}</td>"
            f"<td>{html.escape(' | '.join(str(form) for form in forms[:3]))}</td>"
            "</tr>"
        )
    return (
        '<table class="candidates-table">'
        "<thead><tr><th>PPN</th><th>Prenom</th><th>Nom</th><th>Final</th><th>Formes</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def score_value(candidate: dict[str, Any], key: str) -> float:
    try:
        return max(0.0, min(1.0, float((candidate.get("score") or {}).get(key) or 0.0)))
    except Exception:
        return 0.0


def weighted_context_score(candidate: dict[str, Any], weights: dict[str, Any]) -> float:
    components = [
        ("attrra_source", "attrra_source"),
        ("attrra_note", "attrra_note"),
        ("references", "references"),
        ("institution_year", "institution_year"),
    ]
    weighted_sum = 0.0
    weight_sum = 0.0
    for score_key, weight_key in components:
        try:
            weight = float(weights.get(weight_key, 0.0))
        except Exception:
            weight = 0.0
        if weight <= 0.0:
            continue
        weighted_sum += weight * score_value(candidate, score_key)
        weight_sum += weight
    if weight_sum == 0.0:
        return sum(score_value(candidate, key) for key, _ in components) / len(components)
    return weighted_sum / weight_sum


def candidate_profile_plot(result: dict[str, Any]) -> str:
    candidates = (result.get("candidates") or [])[:12]
    if not candidates:
        return '<p class="muted">Aucun profil candidat a afficher.</p>'

    weights = result.get("score_weights") or {}
    width = 760
    height = 420
    left = 70
    right = 34
    top = 28
    bottom = 64
    plot_w = width - left - right
    plot_h = height - top - bottom
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#4f46e5", "#be123c"]

    def x_pos(value: float) -> float:
        return left + value * plot_w

    def y_pos(value: float) -> float:
        return top + (1.0 - value) * plot_h

    grid = []
    for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
        x = x_pos(tick)
        y = y_pos(tick)
        grid.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" class="plot-grid" />')
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" class="plot-grid" />')
        grid.append(f'<text x="{x:.1f}" y="{top + plot_h + 22}" class="plot-tick">{tick:.2f}</text>')
        grid.append(f'<text x="{left - 12}" y="{y + 4:.1f}" class="plot-tick" text-anchor="end">{tick:.2f}</text>')

    points = [
        '<circle cx="{:.1f}" cy="{:.1f}" r="7" fill="#111827"><title>Profil de reference: correspondance parfaite avec le document courant</title></circle>'.format(
            x_pos(1.0),
            y_pos(1.0),
        ),
        '<text x="{:.1f}" y="{:.1f}" class="plot-label">document courant</text>'.format(
            x_pos(1.0) - 112,
            y_pos(1.0) - 10,
        ),
    ]
    legend_rows = []
    for idx, candidate in enumerate(candidates):
        score = candidate.get("score") or {}
        ppn = str(candidate.get("ppn") or "")
        label = ppn or f"candidat {idx + 1}"
        x = score_value(candidate, "name")
        y = weighted_context_score(candidate, weights)
        final = float(score.get("final") or 0.0)
        color = colors[idx % len(colors)]
        radius = 5.0 + min(7.0, final * 7.0)
        points.append(
            '<circle cx="{:.1f}" cy="{:.1f}" r="{:.1f}" fill="{}" fill-opacity="0.82" stroke="#111827" stroke-width="1">'
            "<title>PPN {} | name {:.3f} | contexte {:.3f} | final {:.3f}</title></circle>".format(
                x_pos(x),
                y_pos(y),
                radius,
                color,
                html.escape(label),
                x,
                y,
                final,
            )
        )
        points.append(
            '<text x="{:.1f}" y="{:.1f}" class="plot-point-label">{}</text>'.format(
                x_pos(x) + 8,
                y_pos(y) - 8,
                html.escape(label),
            )
        )
        legend_rows.append(
            "<tr>"
            f"<td><span class='legend-dot' style='background:{color}'></span>{html.escape(label)}</td>"
            f"<td>{x:.3f}</td>"
            f"<td>{y:.3f}</td>"
            f"<td>{final:.3f}</td>"
            "</tr>"
        )

    svg = f"""
    <svg class="profile-plot" viewBox="0 0 {width} {height}" role="img" aria-label="Carte des profils de similarite">
      <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
      {''.join(grid)}
      <line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="plot-axis" />
      <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="plot-axis" />
      <text x="{left + plot_w / 2:.1f}" y="{height - 18}" class="plot-axis-label">similarite du nom</text>
      <text x="18" y="{top + plot_h / 2:.1f}" class="plot-axis-label" transform="rotate(-90 18 {top + plot_h / 2:.1f})">similarite contexte documentaire</text>
      {''.join(points)}
    </svg>
    """
    return (
        '<div class="plot-wrap">'
        f"{svg}"
        '<p class="plot-note">Chaque point represente le profil de similarite d un PPN candidat par rapport au document courant. '
        "L axe X utilise le score du nom; l axe Y agrege les indices documentaires avec les coefficients actifs.</p>"
        '<table class="plot-legend"><thead><tr><th>PPN</th><th>Nom</th><th>Contexte</th><th>Final</th></tr></thead>'
        f"<tbody>{''.join(legend_rows)}</tbody></table>"
        "</div>"
    )


def render_result(result: dict[str, Any]) -> str:
    status = result.get("status") or ""
    best_ppn = result.get("best_ppn")
    best = result.get("best_candidate")
    similarity = result.get("similarity") or {}
    query = result.get("query") or {}

    best_html = ""
    if best:
        best_html = f"""
        <section class="panel">
          <h3>Meilleur candidat</h3>
          <p><strong>PPN :</strong> <a href="{html.escape(best.get('url') or '')}" target="_blank">{html.escape(best.get('ppn') or '')}</a></p>
          <p><strong>Nom :</strong> {html.escape((best.get('first_name') or '') + ' ' + (best.get('last_name') or ''))}</p>
          {score_table(best)}
          {evidence_block(best)}
        </section>
        """

    return f"""
    <div class="result">
      <section class="panel">
        <h3>Decision</h3>
        <p>{status_badge(status)}</p>
        <p><strong>PPN accepte :</strong> {html.escape(str(best_ppn)) if best_ppn else '<span class="muted">aucun</span>'}</p>
        <p><strong>Mode de similarite :</strong> {html.escape(str(similarity.get('type') or ''))}
        {f" / {html.escape(str(similarity.get('model')))}" if similarity.get('model') else ""}</p>
        <p><strong>Nom aligne :</strong> {html.escape(str(query.get('name') or ''))}</p>
      </section>
      {best_html}
      <section class="panel">
        <h3>Tous les candidats</h3>
        {candidates_table(result.get('candidates') or [])}
      </section>
      <section class="panel">
        <h3>Carte des profils de similarite</h3>
        {candidate_profile_plot(result)}
      </section>
    </div>
    """


def parse_json_ui(json_text: str) -> tuple[list[list[str]], gr.Dropdown, str, dict[str, Any]]:
    payload = parse_extraction_json(json_text)
    people = extract_people(payload)
    if not people:
        return [], gr.Dropdown(choices=[], value=None), "Aucun nom trouve dans les champs personnes.", payload
    choices = people_to_choices(people)
    summary = f"{len(people)} personne(s) unique(s) trouvee(s). Selectionnez une personne puis lancez l'alignement."
    return people_to_rows(people), gr.Dropdown(choices=choices, value=choices[0]), summary, payload


def align_selected_ui(
    extraction: dict[str, Any] | None,
    selected_person: str | None,
    api_url: str,
    api_key: str,
    use_embeddings: bool,
    max_candidates: int,
    max_docs_per_role: int,
    reference_top_k: int,
    accept_threshold: float,
    margin_threshold: float,
    weight_name: float,
    weight_attrra_source: float,
    weight_attrra_note: float,
    weight_references: float,
    weight_institution_year: float,
) -> tuple[str, str]:
    if not extraction:
        raise gr.Error("Collez et analysez d'abord le JSON d'extraction.")
    if not selected_person:
        raise gr.Error("Selectionnez une personne a aligner.")
    selected_name = selected_choice_to_name(selected_person)
    payload = build_align_payload(
        extraction,
        selected_name,
        use_embeddings,
        max_candidates,
        max_docs_per_role,
        reference_top_k,
        accept_threshold,
        margin_threshold,
        weight_name,
        weight_attrra_source,
        weight_attrra_note,
        weight_references,
        weight_institution_year,
    )

    base_url = api_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["X-API-Key"] = api_key.strip()

    try:
        response = requests.post(f"{base_url}/align/person", headers=headers, json=payload, timeout=180)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise gr.Error(f"Erreur lors de l'appel API: {exc}") from exc
    except ValueError as exc:
        raise gr.Error("La reponse API n'est pas un JSON valide.") from exc

    return render_result(result), json.dumps(result, ensure_ascii=False, indent=2)


# --- Organisations -----------------------------------------------------------
# Bloc ajoute : il consomme /align/thesis-organizations et ne touche a rien du
# pipeline personnes. Les statuts sont les memes (accepted / ambiguous /
# low_confidence / not_found) donc status_badge est reutilise tel quel.

ORG_SLOT_LABELS = {
    "granting_institution": "Etablissement de soutenance",
    "co_tutelle_institutions": "Cotutelle",
    "doctoral_school": "Ecole doctorale",
}


def org_candidates_table(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return '<p class="muted">Aucun candidat.</p>'
    rows = []
    for candidate in candidates:
        period = "-".join(
            str(candidate.get(key) or "") for key in ("date_debut", "date_fin")
        ).strip("-")
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(candidate.get('url') or '')}' target='_blank'>{html.escape(str(candidate.get('ppn') or ''))}</a></td>"
            f"<td>{html.escape(str(candidate.get('label_officiel') or ''))}</td>"
            f"<td>{float(candidate.get('score') or 0):.4f}</td>"
            f"<td>{'oui' if candidate.get('valid_for_year') else 'non'}</td>"
            f"<td>{html.escape(period)}</td>"
            f"<td>{html.escape(' | '.join(candidate.get('codes') or []))}</td>"
            "</tr>"
        )
    return (
        '<table class="candidates-table">'
        "<thead><tr><th>PPN</th><th>Libelle officiel</th><th>Score</th>"
        "<th>Valide l'annee</th><th>Periode</th><th>Codes ABES</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def org_block(title: str, block: dict[str, Any] | None) -> str:
    if not block:
        return (
            f"<section class='panel'><h3>{html.escape(title)}</h3>"
            "<p class='muted'>Champ absent de l'extraction.</p></section>"
        )
    ppn = block.get("ppn")
    ppn_html = (
        f"<a href='https://www.idref.fr/{html.escape(str(ppn))}' target='_blank'>{html.escape(str(ppn))}</a>"
        f" — {html.escape(str(block.get('label_officiel') or ''))}"
        if ppn
        else '<span class="muted">aucun</span>'
    )
    redirect = block.get("redirect")
    redirect_html = ""
    if redirect:
        redirect_html = (
            "<p><strong>Redirection temporelle :</strong> "
            f"<a href='https://www.idref.fr/{html.escape(str(redirect.get('ppn') or ''))}' target='_blank'>"
            f"{html.escape(str(redirect.get('ppn') or ''))}</a> "
            f"{html.escape(str(redirect.get('label_officiel') or ''))} "
            f"<span class='muted'>({html.escape(str(redirect.get('reason') or ''))} — indice, jamais accepte automatiquement)</span></p>"
        )
    remote = block.get("remote")
    remote_html = ""
    if remote:
        remote_html = (
            "<h4>Repli distant (data.idref.fr SPARQL)</h4>"
            + (f"<p class='muted'>{html.escape(str(remote.get('error')))}</p>" if remote.get("error") else "")
            + org_candidates_table(remote.get("candidates") or [])
        )
    return f"""
    <section class="panel">
      <h3>{html.escape(title)} — {html.escape(str((block.get('query') or {}).get('label') or ''))}</h3>
      <p>{status_badge(str(block.get('status') or ''))}</p>
      <p><strong>PPN accepte :</strong> {ppn_html}</p>
      <p><strong>Score :</strong> {float(block.get('score') or 0):.4f}
         &nbsp;<strong>Marge :</strong> {float(block.get('margin') or 0):.4f}
         &nbsp;<strong>Vivier :</strong> {html.escape(str(block.get('candidate_scope') or ''))} ({int(block.get('pool_size') or 0)} entites)</p>
      {redirect_html}
      {org_candidates_table(block.get('candidates') or [])}
      {remote_html}
    </section>
    """


def render_org_result(result: dict[str, Any]) -> str:
    query = result.get("query") or {}
    year = html.escape(str(query.get("year") or "")) or '<span class="muted">non renseignee</span>'
    blocks = [org_block(ORG_SLOT_LABELS["granting_institution"], result.get("granting_institution"))]
    for item in result.get("co_tutelle_institutions") or []:
        blocks.append(org_block(ORG_SLOT_LABELS["co_tutelle_institutions"], item))
    blocks.append(org_block(ORG_SLOT_LABELS["doctoral_school"], result.get("doctoral_school")))
    return (
        "<div class='result'>"
        "<section class='panel'><h3>Alignement des organisations</h3>"
        f"<p><strong>Annee de soutenance :</strong> {year}</p>"
        "<p class='muted'>L'ecole doctorale est cherchee dans le perimetre de l'etablissement "
        "quand celui-ci est accepte (vivier « parent »).</p></section>"
        + "".join(blocks)
        + "</div>"
    )


def align_orgs_ui(
    json_text: str,
    api_url: str,
    api_key: str,
    allow_remote: bool,
    org_max_candidates: int,
    org_accept_threshold: float,
    org_margin_threshold: float,
    org_low_threshold: float,
    org_floor_threshold: float,
) -> tuple[str, str]:
    extraction = parse_extraction_json(json_text)
    payload = {
        "granting_institution": str(extraction.get("granting_institution") or ""),
        "co_tutelle_institutions": split_people(extraction.get("co_tutelle_institutions")),
        "doctoral_school": str(extraction.get("doctoral_school") or ""),
        "year": str(extraction.get("defense_year") or extraction.get("year") or ""),
        "allow_remote": bool(allow_remote),
        "max_candidates": int(org_max_candidates),
        "accept_threshold": float(org_accept_threshold),
        "margin_threshold": float(org_margin_threshold),
        "low_threshold": float(org_low_threshold),
        "floor_threshold": float(org_floor_threshold),
    }
    if not any([payload["granting_institution"], payload["co_tutelle_institutions"], payload["doctoral_school"]]):
        raise gr.Error("Aucune organisation dans le JSON (granting_institution / co_tutelle_institutions / doctoral_school).")

    headers = {"Content-Type": "application/json"}
    if api_key.strip():
        headers["X-API-Key"] = api_key.strip()
    # allow_remote interroge data.idref.fr, qui repond en 30-95 s : timeout large.
    try:
        response = requests.post(
            f"{api_url.rstrip('/')}/align/thesis-organizations",
            headers=headers, json=payload, timeout=300 if allow_remote else 120,
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise gr.Error(f"Erreur lors de l'appel API: {exc}") from exc
    except ValueError as exc:
        raise gr.Error("La reponse API n'est pas un JSON valide.") from exc

    return render_org_result(result), json.dumps(result, ensure_ascii=False, indent=2)


CSS = """
.gradio-container {
  max-width: 1280px !important;
}
.muted {
  color: #6b7280;
}
.panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 14px 16px;
  margin: 12px 0;
  background: #fff;
}
.panel h3 {
  margin-top: 0;
}
.score-table,
.candidates-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
}
.score-table th,
.score-table td,
.candidates-table th,
.candidates-table td {
  border-bottom: 1px solid #e5e7eb;
  padding: 7px 8px;
  text-align: left;
  vertical-align: top;
}
.score-table th,
.candidates-table th {
  background: #f9fafb;
  font-weight: 700;
}
.evidence-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.evidence-item {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px;
  background: #f9fafb;
}
.evidence-item h4 {
  margin: 0 0 6px 0;
}
.evidence-item p,
.evidence-item ul {
  margin: 0;
}
.plot-wrap {
  overflow-x: auto;
}
.profile-plot {
  width: 100%;
  max-width: 760px;
  min-width: 560px;
  display: block;
}
.plot-grid {
  stroke: #e5e7eb;
  stroke-width: 1;
}
.plot-axis {
  stroke: #111827;
  stroke-width: 1.4;
}
.plot-tick,
.plot-label,
.plot-point-label,
.plot-axis-label {
  fill: #374151;
  font-size: 12px;
}
.plot-axis-label {
  font-weight: 700;
}
.plot-point-label {
  font-size: 11px;
}
.plot-note {
  color: #6b7280;
  font-size: 13px;
  margin: 8px 0 10px 0;
}
.plot-legend {
  width: 100%;
  max-width: 760px;
  border-collapse: collapse;
}
.plot-legend th,
.plot-legend td {
  border-bottom: 1px solid #e5e7eb;
  padding: 6px 8px;
  text-align: left;
}
.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 999px;
  margin-right: 7px;
}
@media (max-width: 800px) {
  .evidence-grid {
    grid-template-columns: 1fr;
  }
}
"""


with gr.Blocks(css=CSS, title="IdRef Qualinka Alignment") as demo:
    extraction_state = gr.State({})

    gr.Markdown(
        """
        # Alignement IdRef / Qualinka

        Collez le JSON produit par l'extraction VLM, analysez les personnes,
        puis lancez l'alignement IdRef personne par personne.
        """
    )

    with gr.Row():
        with gr.Column(scale=3):
            json_input = gr.Code(
                value=EXAMPLE_JSON,
                language="json",
                label="JSON d'extraction",
                lines=22,
            )
        with gr.Column(scale=2):
            api_url = gr.Textbox(value=DEFAULT_API_URL, label="URL de l'API IdRef Qualinka")
            api_key = gr.Textbox(value=DEFAULT_API_KEY, label="API key", type="password")
            use_embeddings = gr.Checkbox(
                value=False,
                label=f"Utiliser le mode embedding ({EMBEDDING_MODEL})",
            )
            with gr.Accordion("Parametres avances", open=False):
                max_candidates = gr.Slider(1, 100, value=20, step=1, label="Max candidats")
                max_docs_per_role = gr.Slider(0, 200, value=20, step=1, label="Max references par role")
                reference_top_k = gr.Slider(1, 20, value=3, step=1, label="Top-k references")
                accept_threshold = gr.Slider(0.0, 1.0, value=0.65, step=0.01, label="Seuil d'acceptation")
                margin_threshold = gr.Slider(0.0, 1.0, value=0.08, step=0.01, label="Marge d'ambiguite")
                gr.Markdown("### Coefficients du score final")
                weight_name = gr.Slider(0.0, 1.0, value=0.40, step=0.01, label="Poids nom")
                weight_attrra_source = gr.Slider(0.0, 1.0, value=0.25, step=0.01, label="Poids source attrra")
                weight_attrra_note = gr.Slider(0.0, 1.0, value=0.15, step=0.01, label="Poids note attrra")
                weight_references = gr.Slider(0.0, 1.0, value=0.15, step=0.01, label="Poids references")
                weight_institution_year = gr.Slider(0.0, 1.0, value=0.05, step=0.01, label="Poids institution / annee")

    parse_btn = gr.Button("1. Analyser les personnes", variant="primary")
    parse_status = gr.Markdown()

    with gr.Row():
        people_table = gr.Dataframe(
            headers=["Nom", "Roles extraits"],
            datatype=["str", "str"],
            label="Personnes uniques extraites",
            interactive=False,
            wrap=True,
        )
        selected_person = gr.Dropdown(label="Personne a aligner", choices=[])

    align_btn = gr.Button("2. Lancer l'alignement pour la personne selectionnee", variant="primary")

    result_html = gr.HTML(label="Resultat lisible")
    with gr.Accordion("Reponse API brute", open=False):
        raw_json = gr.Code(language="json", label="Reponse API brute", lines=18)

    parse_btn.click(
        parse_json_ui,
        inputs=[json_input],
        outputs=[people_table, selected_person, parse_status, extraction_state],
    )

    align_btn.click(
        align_selected_ui,
        inputs=[
            extraction_state,
            selected_person,
            api_url,
            api_key,
            use_embeddings,
            max_candidates,
            max_docs_per_role,
            reference_top_k,
            accept_threshold,
            margin_threshold,
            weight_name,
            weight_attrra_source,
            weight_attrra_note,
            weight_references,
            weight_institution_year,
        ],
        outputs=[result_html, raw_json],
    )


    gr.Markdown("---\n## Alignement des organisations")
    gr.Markdown(
        "Utilise `granting_institution`, `co_tutelle_institutions` et `doctoral_school` "
        "du meme JSON, avec `defense_year` pour departager les versions temporelles "
        "d'un etablissement. Referentiel local, aucun appel reseau sauf repli explicite."
    )

    with gr.Accordion("Parametres organisations", open=False):
        org_allow_remote = gr.Checkbox(
            value=False,
            label="Repli distant data.idref.fr (SPARQL) si echec local — lent (30-95 s)",
        )
        org_max_candidates = gr.Slider(1, 50, value=5, step=1, label="Max candidats")
        org_accept_threshold = gr.Slider(0.0, 1.0, value=0.72, step=0.01, label="Seuil d'acceptation")
        org_margin_threshold = gr.Slider(0.0, 1.0, value=0.08, step=0.01, label="Marge d'ambiguite")
        org_low_threshold = gr.Slider(0.0, 1.0, value=0.45, step=0.01, label="Plancher low_confidence")
        org_floor_threshold = gr.Slider(0.0, 1.0, value=0.30, step=0.01, label="Plancher not_found")

    org_align_btn = gr.Button("Aligner les organisations", variant="primary")
    org_result_html = gr.HTML(label="Resultat organisations")
    with gr.Accordion("Reponse API brute (organisations)", open=False):
        org_raw_json = gr.Code(language="json", label="Reponse API brute (organisations)", lines=18)

    org_align_btn.click(
        align_orgs_ui,
        inputs=[
            json_input,
            api_url,
            api_key,
            org_allow_remote,
            org_max_candidates,
            org_accept_threshold,
            org_margin_threshold,
            org_low_threshold,
            org_floor_threshold,
        ],
        outputs=[org_result_html, org_raw_json],
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7862"))
    demo.launch(server_name="0.0.0.0", server_port=port)
