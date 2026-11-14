"""Graphics for the Sentinel-BigEye README.

Same gold-and-ivory briefing look as sentinel-armory and sentinel-ai-systems, with the
War Room cyan for what stayed inside the house. Numbers come from the repo's own
data file (data/call-home-<date>.json), never typed in by hand.
SVGs are rendered by Windows Edge headless (edge_shot.py).
Usage: python3 -I make_graphics.py REPO_DIR
"""
import base64, json, math, pathlib, subprocess, sys

REPO = pathlib.Path(sys.argv[1]).resolve()
OUT = REPO / "media"; OUT.mkdir(parents=True, exist_ok=True)
HERE = pathlib.Path(__file__).resolve().parent
import os
PUBLIC = True
DATA = json.loads(sorted((REPO / "data").glob("call-home-*.json"))[-1].read_text())
DAY = DATA["captured_at"][:10]
LATO = pathlib.Path("/usr/share/fonts/truetype/lato")
MONO = pathlib.Path.home() / ".local/share/fonts/CascadiaMono-SemiBold.ttf"
BG1, BG2, GOLD, GOLD2, IVORY, MUTED, LINE, WARN = "#0a0a0c", "#17130d", "#d9aa55", "#f0cf8a", "#f4ecdc", "#a39b8a", "#3a3227", "#e08a6a"
CYAN = "#00d4ff"


def font(path, fam, weight):
    return f"@font-face{{font-family:'{fam}';font-weight:{weight};src:url(data:font/ttf;base64,{base64.b64encode(path.read_bytes()).decode()})}}"


FONTS = (font(LATO / "Lato-Black.ttf", "Lato", 900) + font(LATO / "Lato-Bold.ttf", "Lato", 700)
         + font(LATO / "Lato-Regular.ttf", "Lato", 400) + font(LATO / "Lato-Light.ttf", "Lato", 300) + font(MONO, "Mono", 600))

DEFS = f'''<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BG1}"/><stop offset="1" stop-color="{BG2}"/></linearGradient>
<linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{GOLD2}"/><stop offset="1" stop-color="#b8862f"/></linearGradient>
<linearGradient id="goldh" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#b8862f"/><stop offset="1" stop-color="{GOLD2}"/></linearGradient>
<linearGradient id="rule" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{GOLD}"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></linearGradient>
<linearGradient id="crule" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{CYAN}"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>
<radialGradient id="halo" cx="0.8" cy="0.35" r="0.55"><stop offset="0" stop-color="{GOLD}" stop-opacity="0.16"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
<radialGradient id="chalo" cx="0.82" cy="0.45" r="0.3"><stop offset="0" stop-color="{CYAN}" stop-opacity="0.12"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>
<radialGradient id="iris" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{CYAN}" stop-opacity="0.9"/><stop offset="0.55" stop-color="{CYAN}" stop-opacity="0.25"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>
<filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{GOLD}"/></marker>
<marker id="carrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{CYAN}"/></marker>
</defs>'''


def png(name, svg, w, h):
    html = OUT / f"_{name}.html"
    html.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{FONTS}"
                    f"html,body{{margin:0;background:transparent}}svg{{display:block}}</style></head><body>{svg}</body></html>")
    cmd = [sys.executable, "-I", str(HERE / "edge_shot.py"), str(html), str(OUT / f"{name}.png"), str(w), str(h), "--transparent"]
    if subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode:      # Edge occasionally fails its first launch
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    html.unlink()
    print("made", name)


def frame(w, h, body, rx=18, halo=True, cyan=False):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{DEFS}'
            f'<rect width="{w}" height="{h}" rx="{rx}" fill="url(#bg)"/>'
            + (f'<rect width="{w}" height="{h}" rx="{rx}" fill="url(#halo)"/>' if halo else "")
            + (f'<rect width="{w}" height="{h}" rx="{rx}" fill="url(#chalo)"/>' if cyan else "")
            + f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="{rx}" fill="none" stroke="{LINE}"/>{body}</svg>')


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(n):
    return f"{n:,}"


# ---- banner: an eye made of three gold segments (the cameras) around a cyan iris -------
def eye(cx, cy, s):
    out = []
    for i in range(3):
        a0, a1 = math.radians(i * 120 - 90 + 4), math.radians((i + 1) * 120 - 90 - 4)
        r0, r1 = s * 0.6, s
        large = 0
        p0 = (cx + r1 * math.cos(a0), cy + r1 * math.sin(a0)); p1 = (cx + r1 * math.cos(a1), cy + r1 * math.sin(a1))
        p2 = (cx + r0 * math.cos(a1), cy + r0 * math.sin(a1)); p3 = (cx + r0 * math.cos(a0), cy + r0 * math.sin(a0))
        d = (f"M{p0[0]:.1f} {p0[1]:.1f} A{r1} {r1} 0 {large} 1 {p1[0]:.1f} {p1[1]:.1f} L{p2[0]:.1f} {p2[1]:.1f} "
             f"A{r0} {r0} 0 {large} 0 {p3[0]:.1f} {p3[1]:.1f} Z")
        out.append(f'<path d="{d}" fill="url(#gold)" fill-opacity="{[0.95, 0.75, 0.32][i]}" stroke="{BG1}" stroke-width="2"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{s * 0.5}" fill="url(#iris)"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{s * 0.5}" fill="none" stroke="{CYAN}" stroke-opacity="0.55" stroke-width="2"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{s * 0.15}" fill="{BG1}" stroke="{CYAN}" stroke-width="3" filter="url(#glow)"/>')
    out.append(f'<circle cx="{cx - s * 0.05}" cy="{cy - s * 0.05}" r="{s * 0.04}" fill="{IVORY}"/>')
    return "".join(out)


png("banner", frame(1280, 460, f'''{eye(1040, 225, 140)}
<text x="72" y="104" font-family="Lato" font-weight="700" font-size="20" letter-spacing="7" fill="{GOLD}">SENTINEL ARMORY · OPEN SOURCE</text>
<rect x="72" y="122" width="360" height="2" fill="url(#rule)"/>
<text x="68" y="214" font-family="Lato" font-weight="900" font-size="80" letter-spacing="9" fill="{IVORY}">SENTINEL</text>
<text x="70" y="286" font-family="Lato" font-weight="300" font-size="54" letter-spacing="16" fill="url(#gold)">BIGEYE</text>
<text x="72" y="344" font-family="Lato" font-weight="400" font-size="26" fill="{IVORY}">Three cheap cameras. No cloud. Eyes that think on your own machine.</text>
<text x="72" y="424" font-family="Lato" font-weight="700" font-size="14.5" letter-spacing="2.4" fill="{MUTED}">CAM360 · DOOR · WINDOW · LOCAL AI · FACES · FOLLOW · CALL-HOME MONITOR</text>''', cyan=True), 1280, 460)

# ---- section headings ----------------------------------------------------------------
HEADS = [("problem", "THE PROBLEM"), ("built", "WHAT WAS BUILT"), ("see", "SEE IT"), ("numbers", "THE NUMBERS"),
         ("method", "HOW THEY PHONE HOME"), ("stopped", "HOW IT IS STOPPED"), ("how", "HOW IT FITS TOGETHER"),
         ("hard", "THE HARD PARTS"), ("repo", "WHAT IS IN THIS REPO"), ("status", "HONEST STATUS")]
for i, (k, t) in enumerate(HEADS, 1):
    png(f"h-{k}", frame(1280, 92, f'''
<text x="40" y="60" font-family="Mono" font-weight="600" font-size="22" fill="{GOLD}">{i:02d}</text>
<rect x="84" y="30" width="2" height="36" fill="{GOLD}" fill-opacity="0.6"/>
<text x="104" y="61" font-family="Lato" font-weight="900" font-size="32" letter-spacing="4" fill="{IVORY}">{t}</text>
<rect x="104" y="74" width="420" height="2" fill="url(#rule)"/>''', rx=14, halo=False), 1280, 92)


# ---- tiles helper -------------------------------------------------------------------
def tiles(name, items, tw=230):
    th, gap = 150, 16
    g = ""
    for i, (n, label, sub, c) in enumerate(items):
        x = 24 + i * (tw + gap)
        g += f'''<g transform="translate({x} 24)"><rect width="{tw}" height="{th}" rx="14" fill="#ffffff" fill-opacity="0.025" stroke="{LINE}"/>
<rect width="4" height="{th}" rx="2" fill="{c}" fill-opacity="0.9"/>
<text x="24" y="70" font-family="Lato" font-weight="900" font-size="{50 if len(n) < 7 else 42}" fill="{c}">{esc(n)}</text>
<text x="24" y="104" font-family="Lato" font-weight="700" font-size="18" fill="{IVORY}">{esc(label)}</text>
<text x="24" y="130" font-family="Lato" font-weight="400" font-size="15" fill="{MUTED}">{esc(sub)}</text></g>'''
    w = 48 + len(items) * tw + (len(items) - 1) * gap
    png(name, frame(w, th + 48, g, halo=False), w, th + 48)


cams = DATA["per_camera"]
total = DATA["attempts_today"]
hours = max(0.1, (int(DATA["captured_at"][11:13]) * 60 + int(DATA["captured_at"][14:16])) / 60)
local_hours = hours + 1  # captured_at is UTC; the day is counted from local midnight (CET)
tiles("numbers", [
    (num(total), "attempts to call home", f"{DAY}, 00:00 to {int(local_hours):02d}:{int(local_hours % 1 * 60):02d}", GOLD),
    (num(round(total / local_hours)), "per hour", "about one every 2.5 seconds", GOLD),
    (f"{round(100 * cams.get('cam360', 0) / max(1, total))} %", "from the 360 camera", f"{num(cams.get('cam360', 0))} attempts", IVORY),
    (str(DATA["picture_uploads"]), "pictures it tried to send", f"{DATA['picture_upload_bytes'] // 1024:,} KB of your home", WARN),
    ("0", "left the house", "everything stayed home", CYAN),
])

# ---- what they try to do: horizontal bars ---------------------------------------------
kinds = DATA["per_kind"][:9]
top = max(k["attempts"] for k in kinds)
rowh, x0, bw = 52, 360, 700
g = ""
for i, k in enumerate(kinds):
    y = 34 + i * rowh
    w = max(4, bw * math.sqrt(k["attempts"] / top))  # square-root scale: 13,000 and 4 on one page; labelled with the real numbers
    camtxt = (" · ".join(f"{c} {num(n)}" for c, n in k["by_camera"].items()) if len(k["by_camera"]) > 1
              else "from " + next(iter(k["by_camera"]), "?"))
    g += f'''<text x="{x0 - 20}" y="{y + 22}" text-anchor="end" font-family="Lato" font-weight="700" font-size="19" fill="{IVORY}">{esc(k["kind"])}</text>
<rect x="{x0}" y="{y + 6}" width="{w:.1f}" height="22" rx="4" fill="url(#goldh)"/>
<text x="{x0 + w + 12:.1f}" y="{y + 23}" font-family="Lato" font-weight="900" font-size="19" fill="{GOLD2}">{num(k["attempts"])}</text>
<text x="{x0 + w + 12 + len(num(k["attempts"])) * 12 + 14:.1f}" y="{y + 23}" font-family="Lato" font-weight="400" font-size="15" fill="{MUTED}">{esc(camtxt)}</text>'''
H = 34 + len(kinds) * rowh + 40
g += f'<text x="{x0}" y="{H - 18}" font-family="Lato" font-weight="400" font-size="14" fill="{MUTED}">Bar length on a square-root scale so the small ones stay visible; the numbers are exact. {DAY}.</text>'
png("kinds", frame(1280, H, g, halo=False), 1280, H)

# ---- the method: nine steps ----------------------------------------------------------
STEPS = [("DNS", "look up the", "vendor's servers"), ("WHERE", "ask which", "region to use"), ("SIGN IN", "prove it is", "on your account"),
         ("KEY", "hand over the", "live-video password"), ("LINE", "keep a permanent", "line open"), ("P2P", "register with", "servers in China"),
         ("REPORT", "say it saw", "a person"), ("UPLOAD", "send the", "picture"), ("REMOTE", "ask for remote", "logging / firmware")]
sw = 1280 / 9
g = ""
for i, (tag, a, b) in enumerate(STEPS):
    cx = i * sw + sw / 2
    hot = tag in ("KEY", "UPLOAD", "P2P")
    c = WARN if hot else GOLD
    if i:
        g += f'<path d="M{cx - sw + 46} 96 H{cx - 46}" stroke="{GOLD}" stroke-opacity="0.45" stroke-width="2" stroke-dasharray="5 6"/>'
    g += f'''<circle cx="{cx}" cy="96" r="40" fill="{BG1}" stroke="{c}" stroke-width="2.5"/>
<text x="{cx}" y="72" text-anchor="middle" font-family="Mono" font-weight="600" font-size="13" fill="{MUTED}">{i + 1}</text>
<text x="{cx}" y="104" text-anchor="middle" font-family="Lato" font-weight="900" font-size="{16 if len(tag) < 6 else 13}" fill="{c}">{tag}</text>
<text x="{cx}" y="166" text-anchor="middle" font-family="Lato" font-weight="700" font-size="16" fill="{IVORY}">{esc(a)}</text>
<text x="{cx}" y="188" text-anchor="middle" font-family="Lato" font-weight="400" font-size="16" fill="{MUTED}">{esc(b)}</text>'''
g += f'<text x="640" y="236" text-anchor="middle" font-family="Lato" font-weight="400" font-size="16" fill="{MUTED}">What the 360 camera does at every start, from our own captures. Red: the steps that hand over your home: the key to the live video, servers abroad, pictures.</text>'
png("method", frame(1280, 262, g, halo=False), 1280, 262)


# ---- architecture ---------------------------------------------------------------------
def box(x, y, w, h, title, lines, c=GOLD):
    s = (f'<g transform="translate({x} {y})"><rect width="{w}" height="{h}" rx="16" fill="#ffffff" fill-opacity="0.03" stroke="{c}" stroke-opacity="0.6" stroke-width="1.6"/>'
         f'<text x="{w / 2}" y="34" text-anchor="middle" font-family="Lato" font-weight="900" font-size="18" letter-spacing="2" fill="{c}">{esc(title)}</text>')
    for k, line in enumerate(lines):
        s += f'<text x="{w / 2}" y="{64 + k * 24}" text-anchor="middle" font-family="Lato" font-weight="{700 if k == 0 else 400}" font-size="16" fill="{IVORY if k == 0 else MUTED}">{esc(line)}</text>'
    return s + "</g>"


g = box(24, 40, 300, 230, "THE CAMERAS", ["three cheap Wi-Fi cameras", "360: iCam365 / Tange (CS2 P2P)", "door: iWFCam, H.264", "window: iWFCam W2, MJPEG",
                                          "each one wants to call home", "every few seconds"], WARN)
g += box(390, 24, 470, 262, "CAMERA MACHINE", ["isolated Wi-Fi, no way out", "local bridges: video + every control", "stand-in clouds answer \"OK\"",
                                                     "and keep what they get", "firewall redirects DNS + cloud ports", "reverse tunnel to the main PC"])
g += box(926, 40, 330, 230, "MAIN MACHINE · BIGEYE", ["the page on localhost:8770", "recording, motion, Home/Away", "AI on the GPU: people, cats", "faces, follow, call-home monitor",
                                                 "saves only to this PC", "auto-fix when a camera drops"], CYAN)
g += f'''<path d="M326 155 H382" stroke="{WARN}" stroke-width="3" marker-end="url(#arrow)"/>
<path d="M862 155 H918" stroke="{CYAN}" stroke-width="3" marker-end="url(#carrow)"/>
<g transform="translate(390 306)"><rect width="470" height="54" rx="12" fill="{WARN}" fill-opacity="0.06" stroke="{WARN}" stroke-opacity="0.5" stroke-dasharray="6 6"/>
<text x="235" y="34" text-anchor="middle" font-family="Lato" font-weight="700" font-size="16" fill="{WARN}">✕ internet · vendor clouds · servers in China</text></g>
<path d="M625 288 V300" stroke="{WARN}" stroke-width="2" stroke-dasharray="3 4"/>'''
png("architecture", frame(1280, 384, g, halo=False), 1280, 384)

# ---- what was built: feature cards ------------------------------------------------------
FEATS = [("LIVE VIEW", "all cameras on one page", "buttons never queue behind video"), ("RECORD", "snapshots + MP4 on this PC", "3 s before a motion alarm"),
         ("AI ON THE GPU", "person · cat · dog", "about 5 ms per picture"), ("FACES + PETS", "\"Alex\" and Whiskers", "known faces are not recorded"),
         ("360 CONTROL", "any spot as a favourite", "night mode, quality, follow"), ("DOOR + WINDOW", "every control the bridge knows", "real night-vision read-back"),
         ("SELF-HEALING", "auto-fix after 90 s", "key-frame replay for the door"), ("CALL-HOME", "live monitor of every attempt", "printable reports")]
cw, ch = 296, 140
g = ""
for i, (t, a, b) in enumerate(FEATS):
    x, y = 24 + (i % 4) * (cw + 16), 24 + (i // 4) * (ch + 16)
    c = CYAN if t == "CALL-HOME" else GOLD
    g += f'''<g transform="translate({x} {y})"><rect width="{cw}" height="{ch}" rx="14" fill="#ffffff" fill-opacity="0.025" stroke="{LINE}"/>
<rect width="4" height="{ch}" rx="2" fill="{c}"/><text x="24" y="44" font-family="Lato" font-weight="900" font-size="20" letter-spacing="2" fill="{c}">{t}</text>
<text x="24" y="80" font-family="Lato" font-weight="700" font-size="17" fill="{IVORY}">{esc(a)}</text>
<text x="24" y="106" font-family="Lato" font-weight="400" font-size="15" fill="{MUTED}">{esc(b)}</text></g>'''
png("built", frame(1280, 24 * 2 + ch * 2 + 16, g, halo=False), 1280, 24 * 2 + ch * 2 + 16)
print("all graphics made from", sorted((REPO / "data").glob("call-home-*.json"))[-1].name)
