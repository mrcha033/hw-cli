"""Generate a self-contained HTML review report from a review bundle."""
from __future__ import annotations

import base64
import json
from html import escape
from pathlib import Path
from typing import Any

_STATUS_COLOR = {
    "pass": "#22c55e",
    "fail": "#ef4444",
    "blocked": "#f97316",
    "candidate": "#a855f7",
    "released": "#3b82f6",
}

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Review Report: {project_name} {revision}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:1.5rem 2rem;background:#0f172a;color:#e2e8f0;}}
h1{{margin:0 0 .25rem;font-size:1.5rem;}}
.meta{{color:#94a3b8;font-size:.85rem;margin-bottom:1.5rem;}}
.summary{{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem;}}
.chip{{padding:.3rem .75rem;border-radius:999px;font-size:.8rem;font-weight:600;background:#1e293b;}}
section{{margin-bottom:1.75rem;}}
h2{{font-size:1rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin:0 0 .6rem;}}
table{{border-collapse:collapse;width:100%;font-size:.85rem;}}
th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #1e293b;}}
th{{color:#94a3b8;font-weight:500;}}
.badge{{display:inline-block;padding:.1rem .5rem;border-radius:4px;font-size:.75rem;font-weight:700;color:#fff;}}
details{{background:#1e293b;border-radius:.4rem;margin-bottom:.4rem;}}
summary{{padding:.5rem .75rem;cursor:pointer;font-size:.85rem;}}
pre{{margin:0;padding:.5rem .75rem;font-size:.75rem;white-space:pre-wrap;color:#94a3b8;}}
.warn{{color:#fbbf24;}} .info{{color:#60a5fa;}} .ok{{color:#4ade80;}}
.unresolved{{background:#7f1d1d;border-radius:.4rem;padding:.5rem .75rem;margin-bottom:.4rem;font-size:.85rem;}}
.three-d-grid{{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(16rem,.85fr);gap:1rem;align-items:stretch;}}
.assembly-viewer{{min-height:27rem;position:relative;overflow:hidden;border:1px solid #334155;border-radius:.55rem;background:#111827;}}
.assembly-viewer canvas,.assembly-viewer img{{display:block;width:100%;height:100%;min-height:27rem;object-fit:contain;}}
.assembly-viewer canvas{{position:absolute;inset:0;}}
.three-d-card{{background:#1e293b;border-radius:.55rem;padding:.85rem 1rem;font-size:.85rem;}}
.three-d-card p{{margin:.2rem 0 .75rem;line-height:1.45;}}
.three-d-card ul{{padding-left:1.15rem;margin:.45rem 0;}}
.model-table{{margin-top:.8rem;font-size:.76rem;}}
@media (max-width: 760px){{.three-d-grid{{grid-template-columns:1fr;}} .assembly-viewer,.assembly-viewer canvas,.assembly-viewer img{{min-height:20rem;}}}}
</style>
</head>
<body>
<h1>Review Report: {project_name}</h1>
<div class="meta">Revision {revision} &bull; Backend: {backend} &bull; {target_use} &bull; Generated {generated_at} &bull; Hash: {bundle_hash:.12}</div>
<div class="summary">
  <span class="chip ok">{pass_count} pass</span>
  <span class="chip warn">{blocked_count} blocked</span>
  <span class="chip" style="color:#ef4444">{fail_count} fail</span>
  <span class="chip">{total_count} total gates</span>
</div>
{assumptions_html}
{requirements_html}
{placement_html}
{three_d_html}
<section>
<h2>Gate Reports</h2>
{gate_table}
</section>
{failures_html}
</body>
</html>
"""


def _badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#64748b")
    return f'<span class="badge" style="background:{color}">{escape(status)}</span>'


def _assumptions_html(assumptions: dict[str, Any] | None) -> str:
    if not assumptions:
        return ""
    crit = assumptions.get("unresolved_critical", 0)
    names = assumptions.get("unresolved_critical_names", [])
    if not crit:
        return ""
    items = "".join(
        f'<div class="unresolved">&#9888; Critical assumption unresolved: <strong>{escape(n)}</strong></div>'
        for n in names
    )
    return f"<section><h2>Critical Assumptions ({crit} unresolved)</h2>{items}</section>"


def _requirements_html(requirements: dict[str, Any] | None) -> str:
    if not requirements or not requirements.get("active_unresolved_count"):
        return ""
    unresolved = requirements.get("active_unresolved", [])
    rows = "".join(
        f"<tr><td>{escape(str(r.get('id', '')))}</td><td>{escape(str(r.get('source', '')))}</td>"
        f"<td>{escape(str(r.get('category', '')))}</td>"
        f"<td>{'&#9888; release-blocking' if r.get('release_blocking') else ''}</td></tr>"
        for r in unresolved
    )
    return f"""<section><h2>Unresolved Requirements ({len(unresolved)})</h2>
<table><thead><tr><th>ID</th><th>Source</th><th>Category</th><th>Flags</th></tr></thead>
<tbody>{rows}</tbody></table></section>"""


def _placement_html(placement: dict[str, Any] | None) -> str:
    if not placement:
        return ""
    unenforced = ", ".join(escape(k) for k in placement.get("unenforced_constraint_kinds", [])) or "none"
    sources = ", ".join(
        f"{escape(str(k))}: {v}" for k, v in sorted(placement.get("source_counts", {}).items())
    )
    return f"""<section><h2>Placement Proposal</h2>
<table><tbody>
<tr><th>Board</th><td>{placement.get('board_width_mm')} &times; {placement.get('board_height_mm')} mm</td></tr>
<tr><th>Placements</th><td>{placement.get('placement_count')}</td></tr>
<tr><th>Constraints</th><td>{placement.get('constraint_count')}</td></tr>
<tr><th>Unenforced constraints</th><td class="warn">{unenforced}</td></tr>
<tr><th>Source distribution</th><td>{sources}</td></tr>
</tbody></table></section>"""


def _three_d_html(preview: dict[str, Any] | None, vrml_payload: str | None = None) -> str:
    if not preview:
        return ""
    status = str(preview.get("status", "unavailable"))
    source = escape(str(preview.get("source", "existing 3D-model source not recorded")))
    note = escape(str(preview.get("note", "")))
    models = preview.get("models", []) if isinstance(preview.get("models"), list) else []
    available = preview.get("available_model_count", sum(1 for model in models if model.get("available")))
    count = preview.get("model_count", len(models))
    fallback_image = preview.get("fallback_image")
    viewer_asset = preview.get("viewer_asset")
    interactive = bool(preview.get("interactive") and vrml_payload and viewer_asset)
    model_rows = "".join(
        f"<tr><td>{escape(str(model.get('reference', '')))}</td>"
        f"<td>{escape(str(model.get('footprint', '')))}</td>"
        f"<td>{escape(str(model.get('model', '')))}</td>"
        f"<td>{'available' if model.get('available') else 'missing'}</td></tr>"
        for model in models
    )
    coverage = f"{available} of {count} model references available"
    if not fallback_image and not interactive:
        reason = escape(str(preview.get("reason", "preview assets were not generated")))
        return f"""<section><h2>3D Assembly Preview</h2>
<div class=\"three-d-card\"><p class=\"warn\">Native 3D preview unavailable: {reason}</p>
<p>Source: {source}. Coverage: {coverage}.</p></div></section>"""

    image_html = (
        f'<img src="{escape(str(fallback_image), quote=True)}" alt="Native KiCad isometric assembly preview">'
        if fallback_image else ""
    )
    interactive_html = ""
    if interactive:
        interactive_html = (
            f'<script id="hw-review-vrml" type="application/octet-stream">{escape(vrml_payload)}</script>'
            f'<script src="{escape(str(viewer_asset), quote=True)}"></script>'
            '<script>window.HWReview3D&&window.HWReview3D.mount("hw-review-3d","hw-review-vrml");</script>'
        )
    interaction_note = "Drag to orbit, scroll to zoom, double-click to reset." if interactive else "Native KiCad isometric rendering."
    model_detail = (
        f"<details class=\"model-table\"><summary>Source-model coverage ({coverage})</summary>"
        f"<table><thead><tr><th>Ref</th><th>Footprint</th><th>Existing model</th><th>State</th></tr></thead>"
        f"<tbody>{model_rows}</tbody></table></details>"
        if models else ""
    )
    return f"""<section><h2>3D Assembly Preview</h2>
<div class=\"three-d-grid\"><div id=\"hw-review-3d\" class=\"assembly-viewer\">{image_html}</div>
<div class=\"three-d-card\">{_badge(status)}
<p>{interaction_note}</p><p>Source: {source}.</p><p>{coverage}.</p>
<p class=\"warn\">{note}</p>{model_detail}</div></div>{interactive_html}</section>"""


def _gate_table(gate_reports: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{escape(r['gate'])}</td><td>{_badge(r['status'])}</td>"
        f"<td>{len(r.get('failures', []))}</td>"
        f"<td>{escape(', '.join(str(v) for v in list(r.get('metrics', {}).values())[:3]))}</td></tr>"
        for r in gate_reports
    )
    return f"""<table><thead><tr><th>Gate</th><th>Status</th><th>Findings</th><th>Key metrics</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def _failures_html(gate_reports: list[dict[str, Any]]) -> str:
    failing = [r for r in gate_reports if r.get("failures")]
    if not failing:
        return ""
    parts = []
    for r in failing:
        inner = "".join(
            f"<pre>[{escape(f.get('severity', 'error'))}] {escape(f.get('code', ''))} — {escape(f.get('message', ''))}"
            + (f"\n  path: {escape(f['path'])}" if f.get("path") else "")
            + (f"\n  details: {escape(json.dumps(f.get('details', {})))}" if f.get("details") else "")
            + "</pre>"
            for f in r["failures"]
        )
        parts.append(
            f"<details><summary>{_badge(r['status'])} {escape(r['gate'])} ({len(r['failures'])} findings)</summary>{inner}</details>"
        )
    return f"<section><h2>Findings Detail</h2>{''.join(parts)}</section>"


def render_html(bundle: dict[str, Any], vrml_payload: str | None = None) -> str:
    project = bundle.get("project", {})
    summary = bundle.get("summary", {})
    gate_reports = bundle.get("gate_reports", [])
    html = _HTML_TEMPLATE.format(
        project_name=escape(str(project.get("name", ""))),
        revision=escape(str(project.get("revision", ""))),
        backend=escape(str(project.get("backend", ""))),
        target_use=escape(str(project.get("target_use", ""))),
        generated_at=escape(str(bundle.get("generated_at", ""))),
        bundle_hash=escape(str(bundle.get("bundle_hash", ""))),
        pass_count=summary.get("pass", 0),
        blocked_count=summary.get("blocked", 0),
        fail_count=summary.get("fail", 0),
        total_count=summary.get("total", 0),
        assumptions_html=_assumptions_html(bundle.get("assumptions")),
        requirements_html=_requirements_html(bundle.get("requirements")),
        placement_html=_placement_html(bundle.get("placement")),
        three_d_html=_three_d_html(bundle.get("three_d_preview"), vrml_payload),
        gate_table=_gate_table(gate_reports),
        failures_html=_failures_html(gate_reports),
    )
    return html


def generate_html_report(bundle_path: Path, output_path: Path | None = None) -> Path:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if output_path is None:
        output_path = bundle_path.parent / "report.html"
    vrml_payload = _load_vrml_payload(bundle_path.parent, bundle)
    output_path.write_text(render_html(bundle, vrml_payload), encoding="utf-8")
    return output_path


def _load_vrml_payload(review_dir: Path, bundle: dict[str, Any]) -> str | None:
    preview = bundle.get("three_d_preview")
    if not isinstance(preview, dict) or not preview.get("interactive"):
        return None
    asset = preview.get("vrml_asset")
    if not isinstance(asset, str) or Path(asset).is_absolute():
        return None
    candidate = (review_dir / asset).resolve()
    try:
        candidate.relative_to(review_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return base64.b64encode(candidate.read_bytes()).decode("ascii")
