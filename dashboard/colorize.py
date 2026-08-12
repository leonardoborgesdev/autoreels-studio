"""
Gera as versões coloridas (por paleta escolhida pelo usuário) dos assets visuais
do pipeline: anel do círculo do avatar, fundo gradiente atrás do avatar em tela
cheia, borda animada, e a cor de destaque da legenda karaokê.

Reaproveita a FORMA original dos PNGs fixos (via canal alpha), só recolore.
"""
import os

import numpy as np
from PIL import Image

BASE = "/opt/canal-github-reels"
ORIG_RING = os.path.join(BASE, "circle_ring.png")
ORIG_GRADIENT = os.path.join(BASE, "gradient_bg.png")
ORIG_BORDER = os.path.join(BASE, "avatar_box_border.png")


def hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hex_to_ass(h):
    """Converte '#RRGGBB' pro formato de cor ASS (&H00BBGGRR, ordem invertida)."""
    r, g, b = hex_to_rgb(h)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _diagonal_gradient_rgb(w, h, colors):
    """Gradiente diagonal (canto sup. esquerdo -> inferior direito) com N cores."""
    stops = np.array([hex_to_rgb(c) for c in colors], dtype=np.float32)
    n = len(stops)
    xs = np.linspace(0, 1, w, dtype=np.float32)
    ys = np.linspace(0, 1, h, dtype=np.float32)
    xx, yy = np.meshgrid(xs, ys)
    t = (xx + yy) / 2.0  # 0..1 diagonal

    if n == 1:
        out = np.tile(stops[0], (h, w, 1))
    else:
        seg = t * (n - 1)
        idx = np.clip(seg.astype(int), 0, n - 2)
        local_t = (seg - idx)[..., None]
        c0 = stops[idx]
        c1 = stops[idx + 1]
        out = c0 + (c1 - c0) * local_t
    return out.astype(np.uint8)


def make_gradient_bg(colors, out_path, size=(1080, 1920)):
    w, h = size
    rgb = _diagonal_gradient_rgb(w, h, colors)
    Image.fromarray(rgb, "RGB").save(out_path)
    return out_path


def _recolor_with_alpha(template_path, colors, out_path):
    """Pega o alpha (forma) do PNG original e pinta por baixo um gradiente novo."""
    template = Image.open(template_path).convert("RGBA")
    w, h = template.size
    alpha = np.array(template)[:, :, 3]
    rgb = _diagonal_gradient_rgb(w, h, colors)
    rgba = np.dstack([rgb, alpha])
    Image.fromarray(rgba, "RGBA").save(out_path)
    return out_path


def make_ring(colors, out_path):
    return _recolor_with_alpha(ORIG_RING, colors, out_path)


def make_border(colors, out_path):
    return _recolor_with_alpha(ORIG_BORDER, colors, out_path)


DEFAULT_RING_COLORS = ["#FF69B4", "#5A83EC"]
DEFAULT_BG_COLORS = ["#FF69B4", "#24D1ED"]
DEFAULT_BORDER_COLORS = ["#FF69B4", "#5A83EC"]
DEFAULT_CAPTION_COLOR = "#9333EA"


def generate_palette_assets(work_dir, ring_colors=None, bg_colors=None, border_colors=None):
    """Gera ring/border/gradient customizados dentro de work_dir. Retorna os paths.

    ring/border/fundo são 3 controles independentes agora (antes a borda sempre
    reaproveitava as cores do anel)."""
    os.makedirs(work_dir, exist_ok=True)
    ring_colors = ring_colors or DEFAULT_RING_COLORS
    bg_colors = bg_colors or DEFAULT_BG_COLORS
    border_colors = border_colors or DEFAULT_BORDER_COLORS
    ring_path = os.path.join(work_dir, "custom_ring.png")
    border_path = os.path.join(work_dir, "custom_border.png")
    gradient_path = os.path.join(work_dir, "custom_gradient.png")
    make_ring(ring_colors, ring_path)
    make_border(border_colors, border_path)
    make_gradient_bg(bg_colors, gradient_path)
    return {"ring": ring_path, "border": border_path, "gradient": gradient_path}
