"""
HTML report generator for Steve Analytics.

Generates self-contained HTML files with inline styles and Chart.js via CDN.
Open the output file in any browser — no server needed.
"""

import html
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPORTS_DIR = Path(__file__).parent.parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4"

# ──────────────────────────────────────────────────────────────────────
# Inline CSS — keeps reports self-contained and readable
# ──────────────────────────────────────────────────────────────────────
_STYLES = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #1a1a1a; background: #f5f5f5; padding: 2rem;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
  .subtitle { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
  .card {
    background: #fff; border-radius: 8px; padding: 1.5rem;
    margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .card h2 { font-size: 1.1rem; margin-bottom: 1rem; color: #333; }
  .metric-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
  .metric {
    background: #fff; border-radius: 8px; padding: 1.25rem 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08); flex: 1; min-width: 160px;
  }
  .metric .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
  .metric .value { font-size: 1.8rem; font-weight: 700; margin-top: 0.25rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th { text-align: left; padding: 0.6rem 0.75rem; border-bottom: 2px solid #e0e0e0; color: #555; font-weight: 600; }
  td { padding: 0.6rem 0.75rem; border-bottom: 1px solid #eee; }
  tr:hover td { background: #fafafa; }
  .chart-container { position: relative; height: 350px; }
  .note { font-size: 0.8rem; color: #999; margin-top: 1rem; }
</style>
"""


def _escape(val) -> str:
    """HTML-escape a value for safe embedding."""
    if pd.isna(val):
        return '<span style="color:#ccc">—</span>'
    return html.escape(str(val))


def _table_html(df: pd.DataFrame, max_rows: int = 500) -> str:
    """Render a DataFrame as an HTML table."""
    truncated = len(df) > max_rows
    df_show = df.head(max_rows)

    rows = ["<table>", "<thead><tr>"]
    for col in df_show.columns:
        rows.append(f"  <th>{html.escape(str(col))}</th>")
    rows.append("</tr></thead><tbody>")

    for _, row in df_show.iterrows():
        rows.append("<tr>")
        for col in df_show.columns:
            rows.append(f"  <td>{_escape(row[col])}</td>")
        rows.append("</tr>")

    rows.append("</tbody></table>")
    if truncated:
        rows.append(f'<p class="note">Showing {max_rows} of {len(df)} rows.</p>')
    return "\n".join(rows)


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def build_report(
    title: str,
    sections: list[dict],
    filename: str | None = None,
) -> Path:
    """
    Build a complete HTML report and save it.

    Parameters
    ----------
    title : str
        Report title shown at the top.
    sections : list[dict]
        Each dict can have:
          - "heading": str (optional card heading)
          - "table": pd.DataFrame (renders a data table)
          - "metrics": list[{"label": str, "value": str|int|float}]
          - "chart": dict with keys:
              "type": "bar"|"line"|"pie"|"doughnut"
              "labels": list[str]
              "datasets": list[{"label": str, "data": list[number], "color": str (optional)}]
          - "html": str (raw HTML to inject)
          - "text": str (paragraph text)
    filename : str, optional
        Output filename (without path). Defaults to a timestamped name.

    Returns
    -------
    Path
        Absolute path to the generated HTML file.
    """
    if filename is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{ts}.html"

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        _STYLES,
        f"<script src='{CHART_JS_CDN}'></script>",
        "</head><body><div class='container'>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p class='subtitle'>Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}</p>",
    ]

    chart_counter = 0
    chart_scripts = []

    for section in sections:
        # Metrics row (no card wrapper)
        if "metrics" in section:
            parts.append('<div class="metric-row">')
            for m in section["metrics"]:
                parts.append(
                    f'<div class="metric">'
                    f'<div class="label">{html.escape(str(m["label"]))}</div>'
                    f'<div class="value">{html.escape(str(m["value"]))}</div>'
                    f"</div>"
                )
            parts.append("</div>")

        # Card-wrapped content
        has_card_content = any(k in section for k in ("table", "chart", "html", "text"))
        if has_card_content:
            parts.append('<div class="card">')
            if "heading" in section:
                parts.append(f'<h2>{html.escape(section["heading"])}</h2>')

            if "text" in section:
                parts.append(f'<p>{html.escape(section["text"])}</p>')

            if "table" in section:
                parts.append(_table_html(section["table"]))

            if "chart" in section:
                chart_id = f"chart_{chart_counter}"
                chart_counter += 1
                c = section["chart"]

                default_colors = [
                    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
                    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
                    "#9c755f", "#bab0ac",
                ]

                datasets_js = []
                for i, ds in enumerate(c["datasets"]):
                    color = ds.get("color", default_colors[i % len(default_colors)])
                    datasets_js.append(
                        f"{{ label: {repr(ds['label'])}, data: {ds['data']}, "
                        f"backgroundColor: '{color}', borderColor: '{color}', "
                        f"borderWidth: 2, fill: false }}"
                    )

                chart_scripts.append(f"""
                    new Chart(document.getElementById('{chart_id}'), {{
                        type: '{c["type"]}',
                        data: {{
                            labels: {c["labels"]},
                            datasets: [{', '.join(datasets_js)}]
                        }},
                        options: {{
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: {{ legend: {{ position: 'bottom' }} }}
                        }}
                    }});
                """)
                parts.append(
                    f'<div class="chart-container">'
                    f'<canvas id="{chart_id}"></canvas></div>'
                )

            if "html" in section:
                parts.append(section["html"])

            parts.append("</div>")  # close .card

    parts.append("</div>")  # close .container

    if chart_scripts:
        parts.append("<script>")
        parts.append("document.addEventListener('DOMContentLoaded', function() {")
        parts.extend(chart_scripts)
        parts.append("});</script>")

    parts.append("</body></html>")

    out_path = REPORTS_DIR / filename
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path.resolve()


def open_report(path: Path) -> None:
    """Open an HTML report in the default browser."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", str(path)])
    else:
        subprocess.run(["start", str(path)], shell=True)
