#!/usr/bin/env python3
"""
Hazard -> variable mapping, and the "Terrestrial hazard coverage" diagram drawn
from it. eda_overview.py imports HAZARDS and all_vars from here.

Draw the diagram (interactive HTML + static PNG):
    python hazard_coverage_diagram.py --out-dir DATA/V2/eda
writes summary_variables.html (hover a source, hazard or variable to trace its
links) and summary_variables.png. Needs plotly; the PNG also needs kaleido
(and Chrome, which kaleido >= 1.0 can install with `plotly_get_chrome`).

HAZARDS is a list, in display order, of
  * a string: group heading, or
  * a dict:   name    hazard name (must match HAZARD_COLORS in eda_overview.py)
              status  how well the data cover the hazard: "strong", "partial" or "context"
              main    [(source, variable)] variables that measure the hazard directly
              context [(source, variable)] supporting variables

source is one of: radar, flash, light (MRMS), rtma, deriv (RTMA-derived), swdi.
Variable names are short names (no _00.50 / _00.00 suffix).
A variable listed under several hazards is coloured by the first one.
"""

HAZARDS = [
    "Convective storms",
    dict(name="Hail", status="strong",
         main=[("radar", "MESH_Max_60min"), ("radar", "POSH")],
         context=[("radar", "VII"), ("radar", "PrecipFlag_frac_hail"),
                  ("swdi", "MAXSIZE"), ("swdi", "PROB")]),
    dict(name="Tornado / rotation", status="partial",
         main=[("radar", "MergedAzShear_0-2kmAGL"), ("radar", "RotationTrackML60min")],
         context=[("radar", "MergedAzShear_3-6kmAGL"), ("swdi", "MAX_SHEAR"), ("swdi", "MXDV")]),
    dict(name="Damaging wind", status="partial",
         main=[("rtma", "i10fg"), ("rtma", "wspd10")],
         context=[("radar", "VIL_Density")]),
    dict(name="Storm intensity", status="strong",
         main=[("radar", "MergedReflectivityComposite"), ("radar", "EchoTop_50")],
         context=[("radar", "MergedBaseReflectivity"), ("radar", "MergedReflectivityAtLowestAltitude"),
                  ("radar", "EchoTop_18"), ("swdi", "MAX_REFLECT"), ("swdi", "VIL")]),
    dict(name="Lightning", status="strong",
         main=[("light", "NLDN_CG_flashes_1h"), ("light", "NLDN_CG_015min_AvgDensity")],
         context=[]),

    "Water",
    dict(name="Flash flood", status="strong",
         main=[("flash", "FLASH_QPE_FFGMAX"), ("flash", "FLASH_CREST_MAXUNITSTREAMFLOW"),
               ("flash", "FLASH_QPE_ARI01H"), ("flash", "FLASH_QPE_ARIMAX")],
         context=[("radar", "MultiSensor_QPE_01H_Pass2"), ("radar", "PrecipRate")]),
    dict(name="Prolonged rain", status="strong",
         main=[("radar", "MultiSensor_QPE_24H_Pass2"), ("radar", "MultiSensor_QPE_72H_Pass2"),
               ("flash", "FLASH_CREST_MAXSOILSAT")],
         context=[("flash", "FLASH_QPE_ARI12H")]),

    "Temperature and moisture",
    dict(name="Heat", status="strong",
         main=[("deriv", "heat_index_max")],
         context=[("rtma", "t2m_max"), ("rtma", "t2m"), ("rtma", "d2m_max")]),
    dict(name="Cold / freeze", status="strong",
         main=[("rtma", "t2m_min")],
         context=[]),
    dict(name="Winter precipitation", status="partial",
         main=[("radar", "PrecipFlag_frac_snow")],
         context=[("radar", "PrecipFlag_frac_rain"), ("radar", "PrecipFlag_frac_none"),
                  ("radar", "PrecipFlag_dominant")]),
    dict(name="Fire weather", status="partial",
         main=[("deriv", "fireweather_frac"), ("deriv", "rh2_min")],
         context=[("light", "NLDN_CG_flashes_1h")]),

    "Visibility",
    dict(name="Fog / low visibility", status="strong",
         main=[("rtma", "vis_frac_lt1km"), ("rtma", "ceil_frac_lt300m")],
         context=[("rtma", "vis_min"), ("rtma", "vis"), ("rtma", "ceil_min"),
                  ("rtma", "ceil"), ("rtma", "d2m")]),

    "Background",
    dict(name="Environment and terrain", status="context",
         main=[],
         context=[("rtma", "sp"), ("rtma", "sh2"), ("rtma", "sh2_max"), ("rtma", "tcc"),
                  ("rtma", "u10"), ("rtma", "v10"), ("rtma", "wdir10"), ("rtma", "orog")]),
]


def all_vars(item):
    """[(source, variable, is_main)] for one hazard, main variables first."""
    return ([(s, v, True) for s, v in item.get("main", [])]
            + [(s, v, False) for s, v in item.get("context", [])])


# ---------------------------------------------------------------------------
# Diagram
# ---------------------------------------------------------------------------
# Data sources, top to bottom: id -> (label, description, colour)
SOURCES = {
    "radar": ("MRMS radar", "Reflectivity, hail, rotation,<br>rain amounts, precip type", "#2a78d6"),
    "deriv": ("RTMA derived", "RH, heat index, fire-weather flag<br>(per 2.5 km pixel)", "#eb6834"),
    "flash": ("MRMS FLASH", "Rain rarity, flood-guidance ratio,<br>streamflow, soil saturation", "#1baf7a"),
    "light": ("MRMS lightning", "NLDN cloud-to-ground density<br>and hourly count", "#eda100"),
    "swdi": ("SWDI", "NEXRAD hail, TVS and<br>storm-cell detections", "#e87ba4"),
    "rtma": ("RTMA surface", "Temperature, moisture, pressure,<br>wind, visibility, ceiling, terrain", "#008300"),
}
STATUS_STYLE = {   # badge background, text colour
    "strong": ("#e4f2e9", "#2f7d4f"),
    "partial": ("#fbf0dc", "#a8660b"),
    "context": ("#eceff2", "#5b6b78"),
}
INK, MUTED, LINE, BG, ACCENT = "#16212b", "#5b6b78", "#d6dee4", "#f7f9fa", "#1f6f8b"
SANS = "Arial, Helvetica, DejaVu Sans, sans-serif"
MONO = "Menlo, Consolas, DejaVu Sans Mono, monospace"
TITLE = "Terrestrial hazard coverage"
SUBTITLE = "CONUS · 50 km cells (EPSG:5070) · hourly · every variable in the daily file"
HOVER_HINT = " · hover a source, hazard or variable to trace its links"   # HTML only
FOOTER = ("Also in every file: a &lt;var&gt;_valid_frac coverage field for each MRMS variable, "
          "rtma_valid_frac, and lat/lon. MRMS names shown without the _00.50 / _00.00 suffix. "
          "Definitions: DATA_DICTIONARY.md")

# Layout in plot units: x 0-100 across, y top-down
WIDTH_PX, PX_PER_Y = 1400, 1402 / 161.6
SRC_X0, SRC_X1, SRC_H = 1, 27, 6.4
HZ_X0, HZ_X1 = 50, 99
COL_X = (51.2, 74.7)          # left edge of the two variable columns
ROW_H = 1.9

# Highlight links and boxes on hover (Plotly fills in {plot_id})
HOVER_JS = """
var gd = document.getElementById('{plot_id}');
var links = [], rest = [], boxes = gd.layout.meta.boxes;
gd.data.forEach(function(t, i) {
  if (t.meta && t.meta.kind === 'link') { links.push(i); rest.push(t.opacity); }
});
function setBoxes(on) {
  var upd = {};
  boxes.forEach(function(b) {
    var hit = on && on.has(b.kind + ':' + b.key);
    upd['shapes[' + b.i + '].line.color'] = hit ? (b.color || gd.layout.meta.accent) : gd.layout.meta.line;
    upd['shapes[' + b.i + '].line.width'] = hit ? 3 : 1;
  });
  Plotly.relayout(gd, upd);
}
gd.on('plotly_hover', function(ev) {
  var m = ev.points[0].data.meta; if (!m || m.kind === 'link') return;
  var on = new Set();
  var op = links.map(function(i) {
    var l = gd.data[i].meta, hit =
      m.kind === 'src' ? l.src === m.src :
      m.kind === 'hz'  ? l.hz === m.hz :
      (l.hz === m.hz && l.src === m.src);
    if (hit) { on.add('src:' + l.src); on.add('hz:' + l.hz); }
    return hit ? 1 : 0.07;
  });
  Plotly.restyle(gd, {opacity: op}, links);
  setBoxes(on);
});
gd.on('plotly_unhover', function() {
  Plotly.restyle(gd, {opacity: rest}, links);
  setBoxes(null);
});
"""


def _text(text, x, y, size=12, color=INK, family=SANS, xanchor="left", yanchor="middle", **kw):
    return dict(text=text, x=x, y=y, xanchor=xanchor, yanchor=yanchor, align=xanchor
                if xanchor != "center" else "center", showarrow=False,
                font=dict(size=size, color=color, family=family), **kw)


def _rect(x0, x1, y0, y1, fill, line, width=1.0, layer="above"):
    return dict(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=fill,
                line=dict(color=line, width=width), layer=layer)


def _hover_area(x0, x1, y0, y1, text, meta):
    """Invisible filled rectangle that shows `text` on hover."""
    return dict(type="scatter", mode="lines", x=[x0, x1, x1, x0, x0], y=[y0, y0, y1, y1, y0],
                fill="toself", fillcolor="rgba(0,0,0,0)", line=dict(color="rgba(0,0,0,0)", width=0),
                hoveron="fills", hoverinfo="text", text=text, showlegend=False, meta=meta)


def _curve(x0, y0, x1, y1, n=60):
    """Horizontal S-curve (cubic Bezier, control points half-way across)."""
    import numpy as np
    t = np.linspace(0, 1, n)
    xm = (x0 + x1) / 2
    x = (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * xm + 3 * (1 - t) * t ** 2 * xm + t ** 3 * x1
    y = (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * y0 + 3 * (1 - t) * t ** 2 * y1 + t ** 3 * y1
    return x, y


def build_figure(hazards=None, sources=None, hint=True):
    """Plotly figure of sources -> hazards -> variables (hint: add the hover hint)."""
    import plotly.graph_objects as go

    hazards = HAZARDS if hazards is None else hazards
    sources = SOURCES if sources is None else sources
    shapes, ann, hover, box_meta, links = [], [], [], [], []

    ann += [_text(f"<b>{TITLE}</b>", 1, 2.6, 26),
            _text(SUBTITLE + (HOVER_HINT if hint else ""), 1, 5.2, 14, MUTED),
            _text("<b>DATA SOURCES</b>", SRC_X0, 7.6, 12, MUTED),
            _text("<b>HAZARDS · VARIABLES</b>", HZ_X0, 7.6, 12, MUTED)]

    # ---- right column: group headings, hazard boxes, variables
    y, first, hz_mid = 11.6, True, {}
    for item in hazards:
        if isinstance(item, str):
            if not first:
                y += 2.6
            ann.append(_text(f"<b>{item.upper()}</b>", HZ_X0, y - 1.17, 13, ACCENT))
            continue
        first = False
        vs = all_vars(item)
        y0, y1 = y, y + 4.4 + ROW_H * max(1, -(-len(vs) // 2))
        name, status = item["name"], item["status"]
        box_meta.append(dict(i=len(shapes), kind="hz", key=name))
        shapes.append(_rect(HZ_X0, HZ_X1, y0, y1, "#ffffff", LINE, 1, "below"))
        bg, fg = STATUS_STYLE.get(status, STATUS_STYLE["context"])
        ann += [_text(f"<b>{name}</b>", 51.2, y0 + 2.2, 16),
                _text(f"<b>{status.upper()}</b>", 97.8, y0 + 2.2, 11, fg, xanchor="right",
                      bgcolor=bg, borderpad=3)]
        mains = ", ".join(v for _, v, m in vs if m) or "–"
        ctx = ", ".join(v for _, v, m in vs if not m) or "–"
        hover.append(_hover_area(HZ_X0, HZ_X1, y0, y1,
                                 f"<b>{name}</b><br>coverage: {status}<br>main: {mains}<br>context: {ctx}",
                                 dict(kind="hz", src=None, hz=name)))
        for k, (src, var, is_main) in enumerate(vs):
            col, row = k % 2, k // 2
            yc = y0 + 4.45 + ROW_H * row
            xs = COL_X[col] + 0.1
            color = sources[src][2]
            shapes.append(_rect(xs, xs + 0.9, yc - 0.45, yc + 0.45,
                                color if is_main else "#ffffff", color, 1.5))
            ann.append(_text(var, xs + 1.5, yc, 12, INK if is_main else MUTED, MONO))
            hover.append(_hover_area(COL_X[col], COL_X[col] + 23.1, yc - 0.95, yc + 0.95,
                                     f"<b>{var}</b><br>{'main' if is_main else 'context'} variable"
                                     f"<br>from {sources[src][0]} → {name}",
                                     dict(kind="var", src=src, hz=name)))
        hz_mid[name] = (y0 + y1) / 2
        # one link per (source, hazard); solid if that source gives a main variable
        per_src = {}
        for src, _, is_main in vs:
            per_src[src] = per_src.get(src, False) or is_main
        links += [(src, name, m) for src, m in per_src.items()]
        y = y1 + 0.9
    bottom = y - 0.9

    # ---- left column: source boxes spread over the same height
    order = list(sources)
    step = (bottom - SRC_H - 10.0) / max(len(order) - 1, 1)
    src_y0 = {}
    for k, sid in enumerate(order):
        label, desc, color = sources[sid]
        y0 = 10.0 + k * step
        src_y0[sid] = y0
        box_meta.append(dict(i=len(shapes), kind="src", key=sid, color=color))
        shapes += [_rect(SRC_X0, SRC_X1, y0, y0 + SRC_H, "#ffffff", LINE, 1, "below"),
                   _rect(SRC_X0, SRC_X0 + 1.0, y0, y0 + SRC_H, color, color, 0)]
        ann += [_text(f"<b>{label}</b>", 3.2, y0 + 1.5, 17),
                _text(desc, 3.2, y0 + 2.6, 12, MUTED, yanchor="top")]
        hover.append(_hover_area(SRC_X0, SRC_X1, y0, y0 + SRC_H,
                                 f"<b>{label}</b><br>{desc.replace('<br>', ' ')}",
                                 dict(kind="src", src=sid, hz=None)))

    # ---- links: fan out evenly along the right edge of each source box
    traces = []
    for sid in order:
        mine = [l for l in links if l[0] == sid]
        for k, (_, hz, is_main) in enumerate(mine):
            ys = src_y0[sid] + SRC_H * (k + 1) / (len(mine) + 1)
            x, yv = _curve(SRC_X1, ys, HZ_X0, hz_mid[hz])
            traces.append(dict(type="scatter", mode="lines", x=x, y=yv, hoverinfo="skip",
                               showlegend=False, opacity=0.75 if is_main else 0.6,
                               line=dict(color=sources[sid][2], width=2.6 if is_main else 1.7,
                                         dash="solid" if is_main else "dash"),
                               meta=dict(kind="link", src=sid, hz=hz, main=is_main)))

    # ---- legend and footer
    yl = bottom + 3.5
    legend = [dict(type="scatter", mode="lines", x=[1, 5], y=[yl, yl], hoverinfo="skip",
                    showlegend=False, line=dict(color=INK, width=2.6)),
               dict(type="scatter", mode="lines", x=[17, 21], y=[yl, yl], hoverinfo="skip",
                    showlegend=False, line=dict(color=INK, width=1.7, dash="dash"))]
    shapes += [_rect(33, 33.9, yl - 0.45, yl + 0.45, INK, INK, 1.5),
               _rect(46, 46.9, yl - 0.45, yl + 0.45, "#ffffff", INK, 1.5)]
    ann += [_text(t, x, yl, 13, MUTED) for t, x in
            (("main indicator", 6), ("context only", 22), ("main variable", 34.5),
             ("context variable", 47.5))]
    ann.append(_text(FOOTER, 1, yl + 3.0, 12, MUTED))
    height_units = yl + 5.4

    fig = go.Figure(data=traces + hover + legend)
    fig.update_layout(
        width=WIDTH_PX, height=round(height_units * PX_PER_Y),
        margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor=BG, plot_bgcolor=BG,
        xaxis=dict(range=[0, 100], visible=False, fixedrange=True),
        yaxis=dict(range=[height_units, 0], visible=False, fixedrange=True),
        shapes=shapes, annotations=ann, hovermode="closest", dragmode=False,
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=LINE,
                        font=dict(family=SANS, size=13, color=INK)),
        meta=dict(boxes=box_meta, line=LINE, accent=ACCENT))
    return fig


def draw(out_dir=".", name="summary_variables", png=True, png_scale=1.43):
    """Write <name>.html (interactive) and, if possible, <name>.png."""
    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig = build_figure()
    html_path = out_dir / f"{name}.html"
    fig.write_html(html_path, include_plotlyjs=True, post_script=HOVER_JS,
                   config=dict(displayModeBar=False, responsive=False))
    print(f"Saved {html_path}")
    if png:
        png_path = out_dir / f"{name}.png"
        try:
            fig.layout.annotations[1].text = SUBTITLE     # no hover hint on a static image
            fig.write_image(png_path, scale=png_scale)
            print(f"Saved {png_path}")
        except Exception as e:
            print(f"PNG not written ({e}). Install kaleido, and Chrome with `plotly_get_chrome`.")
    return fig


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Draw the terrestrial hazard coverage diagram")
    ap.add_argument("--out-dir", default=".", help="folder for summary_variables.html / .png")
    ap.add_argument("--no-png", action="store_true", help="only write the HTML")
    a = ap.parse_args()
    
    a.out_dir="/Volumes/MLIA_active_data/gridresilience/data/terrestrial/V2/eda"
    
    
    draw(a.out_dir, png=not a.no_png)
