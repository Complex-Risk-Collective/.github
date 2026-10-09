#!/usr/bin/env python3
"""
Hazard coverage diagram: data sources (left) linked to hazards (right).

One Plotly figure, saved as
  - hazard_coverage.html : interactive; hover a source, hazard or variable to
                           highlight its links
  - hazard_coverage.png  : static image for papers and slides (also .svg/.pdf on request)

Every variable written to the daily file is shown under the hazard(s) it describes:
  filled square = main indicator, hollow square = context / supporting variable.
Solid link = source provides a main indicator; dashed link = context only.
Coverage fields (<var>_valid_frac, rtma_valid_frac) and lat/lon are named in the footer.

    python hazard_coverage_diagram.py                        # HTML + PNG
    python hazard_coverage_diagram.py --formats html png svg pdf
    python hazard_coverage_diagram.py --out figs/coverage    # custom file stem
    python hazard_coverage_diagram.py --check daily/weather_CONUS_20240601.nc
        # lists variables in a daily file that the diagram does not show (and vice versa)

Requires: pip install plotly kaleido
Static images (PNG/SVG/PDF) use kaleido, which needs Chrome; if it is missing, run
`plotly_get_chrome` once (installs a private copy) or install Google Chrome.
"""

import argparse

import numpy as np
# plotly is imported inside build_figure(), so other scripts (e.g. eda_overview.py)
# can read SOURCES / HAZARDS without plotly installed

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

# Colours: validated categorical palette (colour-blind safe, fixed order)
SOURCES = [
    ("radar", "MRMS radar",     "#2a78d6", "Reflectivity, hail, rotation,\nrain amounts, precip type"),
    ("deriv", "RTMA derived",   "#eb6834", "RH, heat index, fire-weather flag\n(per 2.5 km pixel)"),
    ("flash", "MRMS FLASH",     "#1baf7a", "Rain rarity, flood-guidance ratio,\nstreamflow, soil saturation"),
    ("light", "MRMS lightning", "#eda100", "NLDN cloud-to-ground density\nand hourly count"),
    ("swdi",  "SWDI",           "#e87ba4", "NEXRAD hail, TVS and\nstorm-cell detections"),
    ("rtma",  "RTMA surface",   "#008300", "Temperature, moisture, pressure,\nwind, visibility, ceiling, terrain"),
]

# (group header) or dict(name, status, main=[(source, variable)], ctx=[(source, variable)])
# Variable names as in the daily file, MRMS suffix (_00.50 / _00.00) dropped.
HAZARDS = [
    "Convective storms",
    dict(name="Hail", status="strong",
         main=[("radar", "MESH_Max_60min"), ("radar", "POSH")],
         ctx=[("radar", "VII"), ("radar", "PrecipFlag_frac_hail"),
              ("swdi", "MAXSIZE"), ("swdi", "PROB")]),
    dict(name="Tornado / rotation", status="partial",
         main=[("radar", "MergedAzShear_0-2kmAGL"), ("radar", "RotationTrackML60min")],
         ctx=[("radar", "MergedAzShear_3-6kmAGL"), ("swdi", "MAX_SHEAR"), ("swdi", "MXDV")]),
    dict(name="Damaging wind", status="partial",
         main=[("rtma", "i10fg"), ("rtma", "wspd10")],
         ctx=[("radar", "VIL_Density")]),
    dict(name="Storm intensity", status="strong",
         main=[("radar", "MergedReflectivityComposite"), ("radar", "EchoTop_50")],
         ctx=[("radar", "MergedBaseReflectivity"), ("radar", "MergedReflectivityAtLowestAltitude"),
              ("radar", "EchoTop_18"), ("swdi", "MAX_REFLECT"), ("swdi", "VIL")]),
    dict(name="Lightning", status="strong",
         main=[("light", "NLDN_CG_flashes_1h"), ("light", "NLDN_CG_015min_AvgDensity")],
         ctx=[]),
    "Water",
    dict(name="Flash flood", status="strong",
         main=[("flash", "FLASH_QPE_FFGMAX"), ("flash", "FLASH_CREST_MAXUNITSTREAMFLOW"),
               ("flash", "FLASH_QPE_ARI01H"), ("flash", "FLASH_QPE_ARIMAX")],
         ctx=[("radar", "MultiSensor_QPE_01H_Pass2"), ("radar", "PrecipRate")]),
    dict(name="Prolonged rain", status="strong",
         main=[("radar", "MultiSensor_QPE_24H_Pass2"), ("radar", "MultiSensor_QPE_72H_Pass2"),
               ("flash", "FLASH_CREST_MAXSOILSAT")],
         ctx=[("flash", "FLASH_QPE_ARI12H")]),
    "Temperature and moisture",
    dict(name="Heat", status="strong",
         main=[("deriv", "heat_index_max")],
         ctx=[("rtma", "t2m_max"), ("rtma", "t2m"), ("rtma", "d2m_max")]),
    dict(name="Cold / freeze", status="strong",
         main=[("rtma", "t2m_min")],
         ctx=[]),
    dict(name="Winter precipitation", status="partial",
         main=[("radar", "PrecipFlag_frac_snow")],
         ctx=[("radar", "PrecipFlag_frac_rain"), ("radar", "PrecipFlag_frac_none"),
              ("radar", "PrecipFlag_dominant")]),
    dict(name="Fire weather", status="partial",
         main=[("deriv", "fireweather_frac"), ("deriv", "rh2_min")],
         ctx=[("light", "NLDN_CG_flashes_1h")]),
    "Visibility",
    dict(name="Fog / low visibility", status="strong",
         main=[("rtma", "vis_frac_lt1km"), ("rtma", "ceil_frac_lt300m")],
         ctx=[("rtma", "vis_min"), ("rtma", "vis"), ("rtma", "ceil_min"), ("rtma", "ceil"),
              ("rtma", "d2m")]),
    "Background",
    dict(name="Environment and terrain", status="context",
         main=[],
         ctx=[("rtma", "sp"), ("rtma", "sh2"), ("rtma", "sh2_max"), ("rtma", "tcc"),
              ("rtma", "u10"), ("rtma", "v10"), ("rtma", "wdir10"), ("rtma", "orog")]),
]

# Fields in every daily file that are not hazard variables (named in the footer)
NON_HAZARD_PATTERNS = ("_valid_frac", "rtma_valid_frac", "lat", "lon")
MRMS_SUFFIXES = ("_00.50", "_00.00")

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
INK = "#16212b"
MUTED = "#5b6b78"
LINE = "#d6dee4"
SURFACE = "#ffffff"
BG = "#f7f9fa"
GROUP = "#1f6f8b"
STATUS = {"strong": ("#2f7d4f", "#e4f2e9"), "partial": ("#a8660b", "#fbf0dc"),
          "context": ("#5b6b78", "#eceff2")}
SANS = "Arial, Helvetica, DejaVu Sans, sans-serif"
MONO = "Menlo, Consolas, DejaVu Sans Mono, monospace"
PX_PER_X = 14            # 100 x-units -> 1400 px
PX_PER_Y = 14 * 0.62     # vertical unit is shorter than horizontal

# Geometry (data units; y grows downward)
W = 100
SRC_X0, SRC_X1 = 1, 27
HZ_X0, HZ_X1 = 50, 99
VAR_COLS = 2             # variables per row inside a hazard box
VAR_COL_W = 23.5         # width of one variable column
VAR_LINE = 1.9           # height of one variable row
HZ_HEAD = 2.6            # hazard name line
HZ_PAD = 0.9
HZ_GAP = 0.9
GRP_H = 2.6


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------
def all_vars(item):
    return [(s, v, True) for s, v in item["main"]] + [(s, v, False) for s, v in item["ctx"]]


def box_height(item):
    rows = -(-len(all_vars(item)) // VAR_COLS)
    return HZ_PAD * 2 + HZ_HEAD + VAR_LINE * rows


def var_slots(item, y):
    """(source, variable, is_main, x_marker, y_center) for each variable in a hazard box."""
    out = []
    for k, (s, v, m) in enumerate(all_vars(item)):
        row, col = divmod(k, VAR_COLS)
        out.append((s, v, m, HZ_X0 + 1.3 + col * VAR_COL_W,
                    y + HZ_PAD + HZ_HEAD + VAR_LINE * (row + 0.5)))
    return out


def layout_hazards(y0):
    """Return list of (item, y_top, height) and total bottom y."""
    placed, y = [], y0
    for item in HAZARDS:
        if isinstance(item, str):
            placed.append((item, y, GRP_H))
            y += GRP_H
        else:
            h = box_height(item)
            placed.append((item, y, h))
            y += h + HZ_GAP
    return placed, y


def hazard_links():
    """(source, hazard, is_main): solid if the source gives a main variable, else dashed."""
    links = []
    for item in HAZARDS:
        if isinstance(item, str):
            continue
        main_srcs = list(dict.fromkeys(s for s, _ in item["main"]))
        ctx_srcs = [s for s in dict.fromkeys(s for s, _ in item["ctx"]) if s not in main_srcs]
        links += [(s, item["name"], True) for s in main_srcs]
        links += [(s, item["name"], False) for s in ctx_srcs]
    return links


def diagram_variables():
    return {v for item in HAZARDS if isinstance(item, dict) for _, v, _ in all_vars(item)}


def check_file(nc_path):
    """Compare the variables in a daily file with those in the diagram."""
    import xarray as xr

    with xr.open_dataset(nc_path) as ds:
        names = list(ds.data_vars)

    def short(n):
        for suf in MRMS_SUFFIXES:
            if n.endswith(suf):
                return n[: -len(suf)]
        return n

    file_vars = {short(n) for n in names
                 if not n.endswith("_valid_frac") and n not in ("lat", "lon", "rtma_valid_frac")}
    shown = diagram_variables()
    missing, extra = sorted(file_vars - shown), sorted(shown - file_vars)
    print(f"{len(file_vars)} hazard variables in file, {len(shown)} in diagram")
    print("In file but not in diagram:", missing or "none")
    print("In diagram but not in file:", extra or "none")
    return missing, extra


def rgba(hex_color, a):
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def bezier(x1, y1, x2, y2, n=60):
    t = np.linspace(0, 1, n)[:, None]
    mx = (x1 + x2) / 2
    p0, p1, p2, p3 = map(np.array, [(x1, y1), (mx, y1), (mx, y2), (x2, y2)])
    pts = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3
    return pts[:, 0], pts[:, 1]


def box_polygon(x0, y0, x1, y1):
    return [x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0]


def build_figure(interactive=True):
    import plotly.graph_objects as go
    top = 9
    placed, bottom = layout_hazards(top)
    H = bottom + 8
    color_of = {sid: color for sid, _, color, _ in SOURCES}
    name_of = {sid: name for sid, name, _, _ in SOURCES}

    shapes, ann, traces = [], [], []
    boxes = []   # shape index of each source / hazard box, for hover highlighting

    def text(x, y, s, size, color=INK, bold=False, family=SANS, anchor="left", **kw):
        ann.append(dict(x=x, y=y, text=f"<b>{s}</b>" if bold else s, showarrow=False,
                        xanchor=anchor, yanchor="middle", align=anchor,
                        font=dict(size=size, color=color, family=family), **kw))

    def rect(x0, y0, x1, y1, fill, line=None, layer="below"):
        shapes.append(dict(type="rect", x0=x0, y0=y0, x1=x1, y1=y1, fillcolor=fill,
                           line=dict(color=line or fill, width=1 if line else 0), layer=layer))

    # ---- header ----------------------------------------------------------
    text(SRC_X0, 2.6, "Terrestrial hazard coverage", 26, bold=True)
    text(SRC_X0, 5.2, "CONUS · 50 km cells (EPSG:5070) · hourly · every variable in the daily file"
         + (" · hover a source, hazard or variable to trace its links" if interactive else ""),
         14, MUTED)
    text(SRC_X0, 7.6, "DATA SOURCES", 12, MUTED, bold=True)
    text(HZ_X0, 7.6, "HAZARDS · VARIABLES", 12, MUTED, bold=True)

    # ---- hazards -----------------------------------------------------------
    hz_pos, hover_items = {}, []
    for item, y, h in placed:
        if isinstance(item, str):
            text(HZ_X0, y + GRP_H * 0.55, item.upper(), 13, GROUP, bold=True)
            continue
        rect(HZ_X0, y, HZ_X1, y + h, SURFACE, LINE)
        boxes.append(dict(i=len(shapes) - 1, kind="hz", key=item["name"]))
        text(HZ_X0 + 1.2, y + HZ_PAD + HZ_HEAD / 2, item["name"], 16, bold=True)
        fg, bgc = STATUS[item["status"]]
        text(HZ_X1 - 1.2, y + HZ_PAD + HZ_HEAD / 2, item["status"].upper(), 11, fg, bold=True,
             anchor="right", bgcolor=bgc, borderpad=3)
        hz_pos[item["name"]] = (y, h)
        hover_items.append(("hz", item["name"], None, box_polygon(HZ_X0, y, HZ_X1, y + h),
                            f"<b>{item['name']}</b><br>coverage: {item['status']}<br>"
                            f"main: {', '.join(v for _, v in item['main']) or '–'}<br>"
                            f"context: {', '.join(v for _, v in item['ctx']) or '–'}"))
        for src, var, main, vx, vy in var_slots(item, y):
            c = color_of[src]
            shapes.append(dict(type="rect", x0=vx, y0=vy - 0.45, x1=vx + 0.9, y1=vy + 0.45,
                               fillcolor=c if main else SURFACE, line=dict(color=c, width=1.5),
                               layer="above"))
            text(vx + 1.5, vy, var, 12, INK if main else MUTED, family=MONO)
            hover_items.append(("var", item["name"], src,
                                box_polygon(vx - 0.1, vy - VAR_LINE / 2, vx + VAR_COL_W - 0.5,
                                            vy + VAR_LINE / 2),
                                f"<b>{var}</b><br>{'main' if main else 'context'} variable<br>"
                                f"from {name_of[src]} → {item['name']}"))

    # ---- sources -------------------------------------------------------------
    n = len(SOURCES)
    span_top, span_bot = top + 1, bottom - HZ_GAP
    box_h = 6.4
    step = (span_bot - span_top - box_h) / (n - 1)
    src_pos = {}
    for i, (sid, name, color, desc) in enumerate(SOURCES):
        y = span_top + i * step
        rect(SRC_X0, y, SRC_X1, y + box_h, SURFACE, LINE)
        boxes.append(dict(i=len(shapes) - 1, kind="src", key=sid, color=color))
        rect(SRC_X0, y, SRC_X0 + 1.0, y + box_h, color, layer="above")
        text(SRC_X0 + 2.2, y + 1.5, name, 17, bold=True)
        ann.append(dict(x=SRC_X0 + 2.2, y=y + 2.6, text=desc.replace("\n", "<br>"), showarrow=False,
                        xanchor="left", yanchor="top", align="left",
                        font=dict(size=12, color=MUTED, family=SANS)))
        src_pos[sid] = (y, box_h)
        hover_items.append(("src", None, sid, box_polygon(SRC_X0, y, SRC_X1, y + box_h),
                            f"<b>{name}</b><br>{desc.replace(chr(10), ' ')}"))

    # ---- links ----------------------------------------------------------------
    links = hazard_links()
    for sid, _, color, _ in SOURCES:
        mine = sorted([l for l in links if l[0] == sid], key=lambda l: hz_pos[l[1]][0])
        sy, sh = src_pos[sid]
        for k, (_, hz, main) in enumerate(mine):
            hy, hh = hz_pos[hz]
            xs, ys = bezier(SRC_X1, sy + sh * (k + 1) / (len(mine) + 1), HZ_X0, hy + hh / 2)
            traces.append(go.Scatter(
                x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                opacity=0.75 if main else 0.6,
                line=dict(color=color, width=2.6 if main else 1.7, dash="solid" if main else "dash"),
                meta=dict(kind="link", src=sid, hz=hz, main=main)))

    # ---- invisible hover areas (drawn on top of the links) ---------------------
    for kind, hz, src, (px, py), tip in hover_items:
        traces.append(go.Scatter(
            x=px, y=py, mode="lines", fill="toself", fillcolor="rgba(0,0,0,0)",
            line=dict(width=0, color="rgba(0,0,0,0)"), hoveron="fills", hoverinfo="text",
            text=tip, showlegend=False, meta=dict(kind=kind, src=src, hz=hz)))

    # ---- legend + footer ------------------------------------------------------------
    ly = bottom + 2.6
    traces.append(go.Scatter(x=[SRC_X0, SRC_X0 + 4], y=[ly, ly], mode="lines", hoverinfo="skip",
                             showlegend=False, line=dict(color=INK, width=2.6)))
    traces.append(go.Scatter(x=[SRC_X0 + 16, SRC_X0 + 20], y=[ly, ly], mode="lines", hoverinfo="skip",
                             showlegend=False, line=dict(color=INK, width=1.7, dash="dash")))
    text(SRC_X0 + 5, ly, "main indicator", 13, MUTED)
    text(SRC_X0 + 21, ly, "context only", 13, MUTED)
    shapes.append(dict(type="rect", x0=SRC_X0 + 32, y0=ly - 0.45, x1=SRC_X0 + 32.9, y1=ly + 0.45,
                       fillcolor=INK, line=dict(color=INK, width=1.5)))
    text(SRC_X0 + 33.5, ly, "main variable", 13, MUTED)
    shapes.append(dict(type="rect", x0=SRC_X0 + 45, y0=ly - 0.45, x1=SRC_X0 + 45.9, y1=ly + 0.45,
                       fillcolor=SURFACE, line=dict(color=INK, width=1.5)))
    text(SRC_X0 + 46.5, ly, "context variable", 13, MUTED)
    text(SRC_X0, ly + 3, "Also in every file: a &lt;var&gt;_valid_frac coverage field for each MRMS "
         "variable, rtma_valid_frac, and lat/lon. MRMS names shown without the _00.50 / _00.00 "
         "suffix. Definitions: DATA_DICTIONARY.md", 12, MUTED)

    fig = go.Figure(traces)
    fig.update_layout(
        shapes=shapes, annotations=ann, meta=dict(boxes=boxes, line=LINE, accent=GROUP),
        width=int(W * PX_PER_X), height=int(H * PX_PER_Y),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor=BG, plot_bgcolor=BG,
        xaxis=dict(range=[0, W], visible=False, fixedrange=True),
        yaxis=dict(range=[H, 0], visible=False, fixedrange=True),
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=LINE, font=dict(family=SANS, size=13, color=INK)),
        hovermode="closest", dragmode=False,
    )
    return fig


# JavaScript added to the HTML page: dim everything except the hovered item's links
HIGHLIGHT_JS = """
var gd = document.getElementById('{plot_id}');
var links = [], rest = [], boxes = gd.layout.meta.boxes;
gd.data.forEach(function(t, i) {
  if (t.meta && t.meta.kind === 'link') { links.push(i); rest.push(t.opacity); }
});
function setBoxes(on) {           // on: set of 'src:<id>' / 'hz:<name>' keys, or null to reset
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="hazard_coverage", help="output file stem")
    ap.add_argument("--formats", nargs="+", default=["html", "png"],
                    choices=["html", "png", "svg", "pdf"])
    ap.add_argument("--cdn", action="store_true",
                    help="HTML loads plotly.js from the internet (small file) instead of embedding it")
    ap.add_argument("--check", metavar="DAILY_NC",
                    help="compare the diagram with the variables in a daily file, then exit")
    args = ap.parse_args()
    
    # args.out="/Users/marchett/Documents/Disasters/data/V2/summary_variables"

    if args.check:
        check_file(args.check)
        raise SystemExit

    for fmt in args.formats:
        path = f"{args.out}.{fmt}"
        fig = build_figure(interactive=(fmt == "html"))
        if fmt == "html":
            fig.write_html(path, include_plotlyjs="cdn" if args.cdn else True,
                           post_script=HIGHLIGHT_JS,
                           config={"displayModeBar": False, "responsive": False})
        else:
            try:
                fig.write_image(path, scale=2 if fmt == "png" else 1)
            except Exception as e:
                print(f"Could not save {path}: {e}\n"
                      "Static export needs kaleido + Chrome: run `plotly_get_chrome` once.")
                continue
        print(f"Saved {path}")
