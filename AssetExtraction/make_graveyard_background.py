"""Paint a procedural graveyard dungeon background in the Castle Crayburn style.

Crayburn's tile (campaign_editor/generated/crayburn/tiles/DM_a008_CrayburnCastle01.webp)
is a misty, oblique aerial matte painting: muted blue-green grass, grey stone,
rocky hills at the top, soft fog banks and a dark vignette/black frame. This
script recreates that look procedurally: a rough rectangular graveyard with
missing/broken fence sections and an unearthed grave in one corner, lit by a
lantern hung on a spade.

Usage: python3 AssetExtraction/make_graveyard_background.py [out.png]
Needs numpy + Pillow.
"""
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "campaign" / "incdata" / "graveyard_background.png"

N = 1024
SEED = 11
random.seed(SEED)
rng = np.random.default_rng(SEED)


def fbm(size, octaves=6, base=3, persistence=0.55):
    out = np.zeros((size, size))
    amp, total = 1.0, 0.0
    for o in range(octaves):
        cells = base * (2 ** o)
        small = rng.random((cells + 1, cells + 1))
        img = Image.fromarray((small * 255).astype(np.uint8)).resize((size, size), Image.BICUBIC)
        out += amp * np.asarray(img, dtype=float) / 255.0
        total += amp
        amp *= persistence
    out /= total
    return (out - out.min()) / (out.max() - out.min())


def lerp(a, b, t):
    return a + (b - a) * t


# ---------------------------------------------------------------- projection
# Ground plane (u across, v depth; v=0 near/bottom, v=1 far/top) seen from an
# oblique camera: far rows are narrower and compressed, like Crayburn's view.
def proj(u, v):
    persp = lerp(1.0, 0.62, v)
    x = N * 0.5 + (u - 0.5) * N * 1.25 * persp + v * 70
    y = N * 0.95 - math.copysign(abs(v) ** 0.85, v) * N * 0.80
    return x, y, persp


# ---------------------------------------------------------------- terrain
yy, xx = np.mgrid[0:N, 0:N]
depth = np.clip(1 - yy / N, 0, 1)  # 0 near (bottom) .. 1 far (top)

n1 = fbm(N, base=3)
n2 = fbm(N, base=8, octaves=5)
n3 = fbm(N, base=24, octaves=3)

grass_dark = np.array([30, 44, 38])
grass_mid = np.array([66, 86, 70])
grass_light = np.array([108, 124, 100])
t = np.clip(n1 * 0.6 + n2 * 0.3 + n3 * 0.1, 0, 1)
ground = np.where(t[..., None] < 0.5,
                  lerp(grass_dark, grass_mid, (t / 0.5)[..., None]),
                  lerp(grass_mid, grass_light, ((t - 0.5) / 0.5)[..., None]))

# rocky hills along the top (like Crayburn's far cliffs)
ridge = N * (0.17 + 0.07 * fbm(N, base=4, octaves=4)[0])  # per-column ridge height
rock_mask = (yy < ridge[None, :]).astype(float)
rock_tex = fbm(N, base=10, octaves=5)
rock_col = lerp(np.array([44, 50, 54]), np.array([150, 156, 150]), (rock_tex ** 1.4)[..., None])
soft = np.clip((ridge[None, :] - yy) / 40.0, 0, 1)[..., None]
ground = ground * (1 - soft * rock_mask[..., None]) + rock_col * soft * rock_mask[..., None]

# a dark stream winding across the middle distance
stream = np.zeros((N, N))
for x in range(N):
    cy = N * 0.30 + 40 * math.sin(x / 140.0) + 22 * math.sin(x / 53.0 + 1.3)
    w = 9 + 5 * math.sin(x / 90.0)
    stream[:, x] = np.exp(-((yy[:, x] - cy) ** 2) / (2 * w * w))
water = np.array([46, 66, 78])
ground = ground * (1 - stream[..., None] * 0.85) + water * stream[..., None] * 0.85

img = Image.fromarray(np.clip(ground, 0, 255).astype(np.uint8))
draw = ImageDraw.Draw(img, "RGBA")

# ---------------------------------------------------------------- graveyard plot
# rough rectangle on the ground plane (jittered corners)
U0, U1, V0, V1 = 0.14, 0.86, 0.14, 0.62
corners = [(U0 + 0.01, V0), (U1, V0 + 0.015), (U1 - 0.015, V1), (U0, V1 - 0.01)]
poly = [proj(u, v)[:2] for u, v in corners]
# trampled, darker earth inside the plot
draw.polygon(poly, fill=(38, 46, 40, 120))

# path from the bottom edge up to the gate
gate_u = 0.47
path_pts = []
for i in range(40):
    s = i / 39
    u = lerp(0.36, gate_u, s) + 0.03 * math.sin(s * 5)
    v = lerp(-0.08, V0, s)
    x, y, p = proj(u, v)
    path_pts.append((x, y, p))
for x, y, p in path_pts:
    r = 22 * p
    draw.ellipse([x - r, y - r * 0.45, x + r, y + r * 0.45], fill=(92, 84, 66, 160))
# path continues inside towards the back
for i in range(30):
    s = i / 29
    x, y, p = proj(gate_u + 0.02 * math.sin(s * 4), lerp(V0, V1 - 0.05, s))
    r = 13 * p
    draw.ellipse([x - r, y - r * 0.45, x + r, y + r * 0.45], fill=(84, 78, 62, 110))

img = img.filter(ImageFilter.GaussianBlur(2.0))
draw = ImageDraw.Draw(img, "RGBA")

# brush strokes: short dabs sampled from the underlying colour, oriented
# roughly along the ground plane, give the matte-painting texture
base_arr = np.asarray(img, dtype=float)
for _ in range(60000):
    x = random.uniform(0, N - 1)
    y = random.uniform(0, N - 1)
    c = base_arr[int(y), int(x)]
    j = random.uniform(-14, 14)
    col = tuple(int(max(0, min(255, c[k] + j + random.uniform(-6, 6)))) for k in range(3)) + (110,)
    ln = random.uniform(4, 11) * (0.6 + 0.6 * y / N)
    ang = random.uniform(-0.35, 0.35)
    draw.line([(x, y), (x + ln * math.cos(ang), y + ln * math.sin(ang))], fill=col, width=random.choice((2, 3, 3, 4)))

# ---------------------------------------------------------------- drawables (depth sorted)
items = []  # (screen_y, callable)

MOON = (-0.6, -1.0)  # light from top-left


def shadow(x, y, w, h, p):
    sx, sy = x + 10 * p, y + 3 * p
    draw.ellipse([sx - w, sy - h * 0.18, sx + w * 1.6, sy + h * 0.18], fill=(10, 14, 14, 90))


def headstone(u, v, kind):
    x, y, p = proj(u, v)
    w = (12 + random.uniform(-2, 3)) * p
    h = (24 + random.uniform(-4, 8)) * p
    tilt = random.uniform(-0.18, 0.18) if random.random() < 0.35 else random.uniform(-0.05, 0.05)
    shade = random.randint(84, 118)
    base = (shade - 6, shade, shade + 2)
    dark = (int(shade * 0.55), int(shade * 0.6), int(shade * 0.62))

    def paint():
        shadow(x, y, w, h, p)
        if kind == "cross":
            pts = [(x - w * 0.25, y), (x + w * 0.25, y), (x + w * 0.25 + tilt * h, y - h * 1.2),
                   (x - w * 0.25 + tilt * h, y - h * 1.2)]
            draw.polygon(pts, fill=base)
            cy = y - h * 0.85
            draw.polygon([(x - w * 0.8 + tilt * h * 0.85, cy - w * 0.2), (x + w * 0.8 + tilt * h * 0.85, cy - w * 0.2),
                          (x + w * 0.8 + tilt * h * 0.85, cy + w * 0.2), (x - w * 0.8 + tilt * h * 0.85, cy + w * 0.2)],
                         fill=base)
            draw.line([(x + w * 0.25, y), (x + w * 0.25 + tilt * h, y - h * 1.2)], fill=dark, width=max(1, int(2 * p)))
            return
        top = y - h
        pts = [(x - w, y), (x + w, y), (x + w + tilt * h, top), (x - w + tilt * h, top)]
        draw.polygon(pts, fill=base)
        # rounded cap
        cx = x + tilt * h
        draw.ellipse([cx - w, top - w * 0.9, cx + w, top + w * 0.9], fill=base)
        # shaded right face (moonlight from the left)
        draw.polygon([(x + w * 0.45, y), (x + w, y), (x + w + tilt * h, top), (x + w * 0.45 + tilt * h, top)], fill=dark)
        # moss
        if random.random() < 0.5:
            draw.ellipse([x - w, y - h * 0.35, x + w * 0.2, y], fill=(64, 84, 52, 150))

    items.append((y, paint))


# rows of headstones, leaving the gate path and the grave corner clear
GRAVE_U, GRAVE_V = 0.77, 0.555  # back-right corner
for vi in range(7):
    v = lerp(V0 + 0.05, V1 - 0.05, vi / 6)
    for ui in range(12):
        u = lerp(U0 + 0.045, U1 - 0.045, ui / 11) + random.uniform(-0.012, 0.012)
        vv = v + random.uniform(-0.012, 0.012)
        if abs(u - gate_u) < 0.04:
            continue
        if abs(u - GRAVE_U) < 0.09 and abs(vv - GRAVE_V) < 0.08:
            continue
        if random.random() < 0.13:
            continue
        headstone(u, vv, "cross" if random.random() < 0.18 else "stone")


# ---------------------------------------------------------------- fence
def fence_side(a, b, gate=None):
    n = 22
    for i in range(n):
        s0, s1 = i / n, (i + 1) / n
        u0, v0 = lerp(a[0], b[0], s0), lerp(a[1], b[1], s0)
        u1, v1 = lerp(a[0], b[0], s1), lerp(a[1], b[1], s1)
        if gate is not None and abs((u0 + u1) / 2 - gate) < 0.035:
            continue
        r = random.random()
        if r < 0.16:
            continue  # missing section
        broken = r < 0.30
        x0, y0, p0 = proj(u0, v0)
        x1, y1, p1 = proj(u1, v1)
        h0, h1 = 36 * p0, 36 * p1
        lean = random.uniform(-6, 6) * p0 if broken else random.uniform(-1, 1)

        def paint(x0=x0, y0=y0, x1=x1, y1=y1, h0=h0, h1=h1, lean=lean, broken=broken, p0=p0):
            col = (58, 48, 38)
            hi = (120, 104, 84)
            wpost = max(3, int(5 * p0))
            draw.line([(x0, y0), (x0 + lean, y0 - h0)], fill=col, width=wpost)
            draw.line([(x0 - 1, y0), (x0 - 1 + lean, y0 - h0)], fill=hi, width=1)
            if broken:
                # one rail snapped and hanging to the ground
                draw.line([(x0 + lean * 0.6, y0 - h0 * 0.65), ((x0 + x1) / 2, y1 - 2)], fill=col, width=max(2, int(3 * p0)))
            else:
                for f in (0.35, 0.75):
                    draw.line([(x0 + lean * f, y0 - h0 * f), (x1, y1 - h1 * f)], fill=col, width=max(2, int(3 * p0)))
                    draw.line([(x0 + lean * f, y0 - h0 * f - 1), (x1, y1 - h1 * f - 1)], fill=hi, width=1)

        items.append(((y0 + y1) / 2, paint))
        # fallen fence pieces in the grass
        if broken and random.random() < 0.6:
            fx, fy, fp = proj((u0 + u1) / 2 + random.uniform(-0.01, 0.01), (v0 + v1) / 2 - 0.02)

            def fallen(fx=fx, fy=fy, fp=fp):
                draw.line([(fx - 16 * fp, fy), (fx + 14 * fp, fy - 4 * fp)], fill=(62, 52, 40), width=max(2, int(3 * fp)))

            items.append((fy - 1, fallen))


c = corners
fence_side(c[0], c[1], gate=gate_u)  # front (gate)
fence_side(c[1], c[2])               # right
fence_side(c[3], c[2])               # back
fence_side(c[0], c[3])               # left

# gate posts
for gu in (gate_u - 0.04, gate_u + 0.04):
    gx, gy, gp = proj(gu, V0 + 0.003)

    def gpost(gx=gx, gy=gy, gp=gp):
        draw.rectangle([gx - 4 * gp, gy - 34 * gp, gx + 4 * gp, gy], fill=(70, 72, 74))
        draw.rectangle([gx - 5 * gp, gy - 38 * gp, gx + 5 * gp, gy - 33 * gp], fill=(96, 98, 98))

    items.append((gy + 0.5, gpost))


# ---------------------------------------------------------------- unearthed grave + lantern
gx, gy, gp = proj(GRAVE_U, GRAVE_V)
gp *= 1.5


def grave():
    # dirt mound (left of the pit)
    mx, my = gx - 30 * gp, gy - 2 * gp
    draw.ellipse([mx - 26 * gp, my - 12 * gp, mx + 22 * gp, my + 8 * gp], fill=(70, 52, 38))
    draw.ellipse([mx - 18 * gp, my - 15 * gp, mx + 12 * gp, my + 2 * gp], fill=(92, 70, 50))
    # pit
    pit = [(gx - 14 * gp, gy - 5 * gp), (gx + 22 * gp, gy - 8 * gp), (gx + 26 * gp, gy + 6 * gp), (gx - 10 * gp, gy + 9 * gp)]
    draw.polygon(pit, fill=(14, 10, 8))
    draw.line(pit + [pit[0]], fill=(58, 42, 30), width=max(1, int(2 * gp)))
    # toppled headstone behind the pit
    draw.polygon([(gx - 6 * gp, gy - 16 * gp), (gx + 22 * gp, gy - 19 * gp), (gx + 24 * gp, gy - 11 * gp), (gx - 4 * gp, gy - 9 * gp)],
                 fill=(124, 126, 124))
    # spade driven into the mound, leaning
    sx, sy = mx + 2 * gp, my - 4 * gp
    tx, ty = sx + 12 * gp, sy - 44 * gp
    draw.polygon([(sx - 5 * gp, sy + 4 * gp), (sx + 5 * gp, sy + 3 * gp), (sx + 4 * gp, sy - 8 * gp), (sx - 4 * gp, sy - 7 * gp)],
                 fill=(120, 124, 126))
    draw.line([(sx, sy - 7 * gp), (tx, ty)], fill=(78, 56, 36), width=max(2, int(3 * gp)))
    draw.line([(tx - 5 * gp, ty), (tx + 5 * gp, ty - 1 * gp)], fill=(78, 56, 36), width=max(2, int(3 * gp)))
    # lantern hanging from the handle grip
    lx, ly = tx + 3 * gp, ty + 4 * gp
    draw.line([(tx + 3 * gp, ty), (lx, ly + 3 * gp)], fill=(30, 26, 22), width=1)
    draw.rectangle([lx - 4 * gp, ly + 3 * gp, lx + 4 * gp, ly + 13 * gp], fill=(255, 196, 96), outline=(40, 30, 20))
    draw.rectangle([lx - 2 * gp, ly + 5 * gp, lx + 2 * gp, ly + 11 * gp], fill=(255, 240, 190))


items.append((gy + 10 * gp, grave))
LANTERN = (gx - 28 * gp + 15 * gp + 3 * gp, gy - 2 * gp - 4 * gp - 44 * gp + 12 * gp)


# ---------------------------------------------------------------- dead trees
def tree(x, y, size, p):
    segs = []

    def grow(x, y, size, ang, depth):
        if depth > 5 or size < 3:
            return
        x2 = x + size * math.cos(math.radians(ang))
        y2 = y + size * math.sin(math.radians(ang))
        segs.append((x, y, x2, y2, max(1, int((6 - depth) * p))))
        for k in (-1, 1):
            if random.random() < 0.9:
                grow(x2, y2, size * random.uniform(0.58, 0.74), ang + k * random.uniform(18, 40), depth + 1)

    grow(x, y, size, -90 + random.uniform(-8, 8), 0)

    def paint():
        draw.ellipse([x - size * 0.5, y - 4 * p, x + size * 0.9, y + 5 * p], fill=(10, 14, 14, 80))
        for x1, y1, x2, y2, w in segs:
            draw.line([(x1, y1), (x2, y2)], fill=(24, 26, 26), width=w)

    items.append((y, paint))


for u, v, s in [(0.08, 0.42, 62), (0.12, 0.30, 50), (0.90, 0.36, 66), (0.86, 0.62, 44),
                (0.30, 0.66, 40), (0.62, 0.70, 36), (0.04, 0.10, 70), (0.95, 0.08, 58)]:
    x, y, p = proj(u, v)
    tree(x, y, s * p, p)

# bushes / clumps of dark grass
for _ in range(220):
    u, v = random.uniform(-0.1, 1.1), random.uniform(-0.1, 0.72)
    if U0 < u < U1 and V0 < v < V1:
        continue
    x, y, p = proj(u, v)

    def bush(x=x, y=y, p=p):
        r = random.uniform(4, 10) * p
        draw.ellipse([x - r, y - r * 0.6, x + r, y + r * 0.3], fill=(34, 48, 36, 170))
        draw.ellipse([x - r * 0.6, y - r * 0.8, x + r * 0.3, y - r * 0.1], fill=(64, 82, 58, 140))

    items.append((y, bush))

for _, fn in sorted(items, key=lambda it: it[0]):
    fn()

# ---------------------------------------------------------------- painterly pass
img = img.filter(ImageFilter.ModeFilter(5))
img = img.filter(ImageFilter.GaussianBlur(0.9))
arr = np.asarray(img, dtype=float)

# brush grain
grain = fbm(N, base=96, octaves=2)
arr *= (0.92 + 0.16 * grain)[..., None]

# cool moonlit grade + atmospheric perspective (far = paler, bluer)
moon = np.array([150, 172, 182])
arr = arr * np.array([0.92, 0.98, 1.04])
haze = (depth ** 2.2 * 0.30)[..., None]
arr = arr * (1 - haze) + moon * haze

# lantern glow: warm light pooling around the unearthed grave
lx, ly = LANTERN
d2 = (xx - lx) ** 2 + ((yy - ly) * 1.6) ** 2
glow = np.exp(-d2 / (2 * 70.0 ** 2))[..., None]
core = np.exp(-d2 / (2 * 6.0 ** 2))[..., None]
warm = np.array([255, 150, 60])
arr = arr * (1 + glow * 0.55) + warm * glow * 0.22 + np.array([255, 220, 150]) * core * 0.45

# fog banks: low wispy layers, thicker in the middle distance and on the left
fog = fbm(N, base=4, octaves=5, persistence=0.6)
wisps = fbm(N, base=12, octaves=3)
band = np.exp(-((yy - N * 0.42) ** 2) / (2 * (N * 0.16) ** 2)) * 0.7 + np.exp(-((yy - N * 0.86) ** 2) / (2 * (N * 0.08) ** 2)) * 0.5
fog_a = np.clip((fog * 0.7 + wisps * 0.3 - 0.42) * 1.8, 0, 1) * band
fog_a = np.clip(fog_a, 0, 0.42)[..., None]
fog_col = np.array([168, 186, 190])
arr = arr * (1 - fog_a) + fog_col * fog_a

# vignette + the black frame Crayburn's tile has
dist = np.sqrt(((xx - N * 0.52) / (N * 0.55)) ** 2 + ((yy - N * 0.5) / (N * 0.55)) ** 2)
vig = np.clip(1.05 - 0.55 * np.clip(dist - 0.45, 0, None) ** 1.2, 0.25, 1.0)[..., None]
arr *= vig
frame = np.ones((N, N))
edge = fbm(N, base=40, octaves=2)
frame[yy < 40 + 14 * edge] = 0
frame[xx < 30 + 12 * edge] = 0
frame[yy > N - 18 - 8 * edge] = 0
frame[xx > N - 14 - 6 * edge] = 0
frame = np.asarray(Image.fromarray((frame * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(6)), dtype=float) / 255
arr *= frame[..., None]

out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
OUT.parent.mkdir(parents=True, exist_ok=True)
out.save(OUT)
print(f"wrote {OUT}")

# ---------------------------------------------------------------- map nodes (image pixels)
# Same projection as the drawing above, so markers sit on the gate and the grave.
NODES = {
    "Road": proj(0.429, 0.0)[:2],                 # bottom of the path, entrance from the road
    "GraveyardGate": proj(gate_u, V0)[:2],         # broken gate on the front fence
    "UnearthedGrave": proj(GRAVE_U, GRAVE_V)[:2],  # open grave with the lantern
}
NODES = {k: (round(x), round(y)) for k, (x, y) in NODES.items()}


def bezier_path(a, b, bulge, steps=24):
    """Quadratic Bezier from node a to node b, control point pushed sideways by `bulge` px."""
    (x0, y0), (x1, y1) = NODES[a], NODES[b]
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    cx, cy = mx - dy / length * bulge, my + dx / length * bulge
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        pts.append((round(x), round(y)))
    return {"from": a, "to": b, "control": [round(cx), round(cy)], "points": pts}


PATHS = [
    bezier_path("Road", "GraveyardGate", bulge=-60),
    bezier_path("GraveyardGate", "UnearthedGrave", bulge=70),
]
NODES_JSON = OUT.with_name(OUT.stem + "_nodes.json")
NODES_JSON.write_text(json.dumps({"size": [N, N], "nodes": NODES, "paths": PATHS}, indent=2) + "\n")
print(f"wrote {NODES_JSON}")

marked = out.convert("RGB")
md = ImageDraw.Draw(marked)
for path in PATHS:
    md.line(path["points"], fill=(255, 214, 120), width=4, joint="curve")
for name, (x, y) in NODES.items():
    md.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(255, 214, 120), outline=(20, 20, 20), width=3)
    md.text((x + 16, y - 8), name, fill=(255, 240, 200))
marked.save(OUT.with_name(OUT.stem + "_nodes_preview.png"))
