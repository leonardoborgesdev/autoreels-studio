#!/usr/bin/env python3
"""
Gera um pacote de projeto do FreeCut (.freecut.zip) a partir de um job do
dashboard de reels do GitHub, reaproveitando as MESMAS coordenadas que o
pipeline_remoto.py usa pra compor o video final (circulo do avatar, caixa
grande do avatar, moldura/ring, legenda e musica de fundo).

Nao mexe em pipeline_remoto.py - so LE os arquivos que ele ja deixa no
diretorio do job (words_cache.json com a deteccao de rosto + timestamps
por palavra, custom_ring.png/custom_border.png/custom_gradient.png com as
cores escolhidas, e o video final ja renderizado) e le as constantes de
posicionamento que ja estao hardcoded no pipeline (copiadas aqui, nao
importadas, pra nao acoplar em internals do pipeline).

Formato do bundle (reverse-engineered do codigo-fonte do FreeCut,
walterlow/freecut, em bundle-export-service.ts / bundle-import-service.ts /
types/bundle.ts / types/project.ts):

  meu.freecut.zip
    manifest.json   <- BundleManifest (media[], checksum sha256 do proprio
                       manifest com checksum="" antes de assinar)
    project.json    <- BundleProject (igual a Project, mas os itens da
                       timeline usam mediaRef em vez de mediaId)
    media/<sha256>/<nome-do-arquivo>   <- arquivos de midia originais

O checksum PRECISA bater exatamente com o que o FreeCut recalcula no
import (JSON.stringify(manifest com checksum="") -> sha256 hex), senao a
importacao rejeita o arquivo como corrompido. Por isso a serializacao
compacta abaixo evita qualquer coisa que o JSON.stringify do JS faria
diferente do json.dumps do Python (numeros "1.0" viram 1, ordem de chaves
preservada por insercao, sem escapar acentuados).
"""
import hashlib
import json
import os
import subprocess
import time
import uuid
import zipfile

# ---- constantes copiadas do pipeline_remoto.py (posicionamento visual) ----
CANVAS_W, CANVAS_H = 1080, 1920
FPS = 30
RING_SIZE = 299
FACE_SIZE = RING_SIZE - 16
CIRCLE_POS = {
    "capture": {"face": (759, 130), "ring": (751, 122)},
    "remodel": {"face": (758, 58), "ring": (750, 50)},
}
CAPTION_CX, CAPTION_CY = 540, 1345
CAPTION_FONTSIZE = 72
CAPTION_FONT = "Archivo Black"
SCHEMA_VERSION = 15  # CURRENT_SCHEMA_VERSION do freecut nesta versao clonada


# ---------------------------------------------------------------- ffprobe --
def _ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _num(x):
    """Mimica JSON.stringify do JS: float inteiro (1.0) vira int (1)."""
    if x is None:
        return 0
    f = float(x)
    if f.is_integer():
        return int(f)
    return round(f, 3)


def probe_media(path):
    info = _ffprobe(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or (v or a or {}).get("duration") or 0)
    width = int((v or {}).get("width") or 0)
    height = int((v or {}).get("height") or 0)
    fps = FPS
    if v and v.get("r_frame_rate"):
        try:
            n, d = v["r_frame_rate"].split("/")
            if float(d):
                fps = float(n) / float(d)
        except Exception:
            pass
    codec = (v or a or {}).get("codec_name", "")
    bitrate = int(fmt.get("bit_rate") or (v or a or {}).get("bit_rate") or 0)
    return {
        "duration": _num(duration), "width": width, "height": height,
        "fps": _num(fps), "codec": codec, "bitrate": bitrate,
    }


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def js_compact_json(obj):
    """Serializacao compacta equivalente a JSON.stringify(obj) do JS:
    sem espacos, sem escapar unicode, ordem de insercao preservada."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------- legenda (ass) --
def build_caption_chunks(words):
    """Mesma logica de agrupamento de build_ass() em pipeline_remoto.py:
    grupos de ate 6 palavras (ou menos se achar pontuacao com >=3)."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 6 or (w["word"].endswith((".", ",", "!", "?")) and len(cur) >= 3):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    out = []
    for chunk in chunks:
        text = " ".join(w["word"].upper() for w in chunk)
        out.append({"text": text, "start": chunk[0]["start"], "end": chunk[-1]["end"]})
    return out


# --------------------------------------------------------------- builder --
class Bundle:
    def __init__(self, project_name):
        self.project_name = project_name
        self.media = []       # manifest media entries
        self.media_files = {}  # originalId -> abs path (pra empacotar)
        self.tracks = []
        self.items = []
        self._track_order = 0

    def add_media(self, path, mime_type):
        meta = probe_media(path)
        sha = sha256_file(path)
        media_id = str(uuid.uuid4())
        fname = os.path.basename(path)
        entry = {
            "originalId": media_id,
            "relativePath": f"media/{sha}/{fname}",
            "fileName": fname,
            "fileSize": os.path.getsize(path),
            "sha256": sha,
            "mimeType": mime_type,
            "metadata": meta,
        }
        self.media.append(entry)
        self.media_files[media_id] = path
        return media_id, meta

    def add_track(self, name, kind):
        track_id = str(uuid.uuid4())
        self.tracks.append({
            "id": track_id, "name": name, "kind": kind, "height": 60,
            "locked": False, "visible": True, "muted": False, "solo": False,
            "order": self._track_order,
        })
        self._track_order += 1
        return track_id

    def add_item(self, item):
        item.setdefault("id", str(uuid.uuid4()))
        self.items.append(item)
        return item["id"]

    def build_manifest(self):
        manifest = {
            "version": "1.0",
            "createdAt": int(time.time() * 1000),
            "editorVersion": "1.0.0",
            "projectId": str(uuid.uuid4()),
            "projectName": self.project_name,
            "media": self.media,
            "checksum": "",
        }
        checksum = hashlib.sha256(js_compact_json(manifest).encode("utf-8")).hexdigest()
        manifest["checksum"] = checksum
        return manifest

    def build_project(self, duration_seconds):
        return {
            "id": str(uuid.uuid4()),
            "name": self.project_name,
            "description": (
                "Gerado automaticamente pela dashboard do canal de reels do GitHub "
                "(mesma posicao de avatar/legenda/musica do video final)."
            ),
            "createdAt": int(time.time() * 1000),
            "updatedAt": int(time.time() * 1000),
            "duration": _num(duration_seconds),
            "schemaVersion": SCHEMA_VERSION,
            "metadata": {"width": CANVAS_W, "height": CANVAS_H, "fps": FPS, "backgroundColor": "#000000"},
            "timeline": {
                "tracks": self.tracks,
                "items": self.items,
                "currentFrame": 0,
                "zoomLevel": 1,
            },
        }

    def write_zip(self, out_path, duration_seconds):
        manifest = self.build_manifest()
        project = self.build_project(duration_seconds)
        with zipfile.ZipFile(out_path, "w") as z:
            for entry in self.media:
                src = self.media_files[entry["originalId"]]
                z.write(src, entry["relativePath"], compress_type=zipfile.ZIP_STORED)
            z.writestr("project.json", json.dumps(project, indent=2, ensure_ascii=False),
                       compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False),
                       compress_type=zipfile.ZIP_DEFLATED)
        return out_path


def _crop_ratios(src_w, src_h, crop_x, crop_y, crop_w, crop_h):
    return {
        "left": round(crop_x / src_w, 6),
        "top": round(crop_y / src_h, 6),
        "right": round((src_w - (crop_x + crop_w)) / src_w, 6),
        "bottom": round((src_h - (crop_y + crop_h)) / src_h, 6),
        "refit": True,
    }


def generate_freecut_bundle(job_id, state, jdir, out_path):
    """Monta o .freecut.zip pro job. Levanta RuntimeError com uma mensagem
    clara se faltar algo essencial (video final, video de fundo/avatar)."""
    mode = state.get("kind", "capture")
    output_video = state.get("output_video")
    bg_video = state.get("bg_video")
    avatar_video = state.get("avatar_video")
    if not output_video or not os.path.exists(output_video):
        raise RuntimeError("vídeo final ainda não está pronto")
    if not bg_video or not os.path.exists(bg_video):
        raise RuntimeError("vídeo de fundo não encontrado")
    if not avatar_video or not os.path.exists(avatar_video):
        raise RuntimeError("vídeo do avatar não encontrado")

    out_meta = probe_media(output_video)
    duration = out_meta["duration"]
    total_frames = max(1, round(duration * FPS))

    project_name = state.get("output_name") or state.get("slot_label") or job_id
    b = Bundle(project_name)

    # --- midias ---
    bg_id, bg_meta = b.add_media(bg_video, "video/mp4")
    avatar_id, avatar_meta = b.add_media(avatar_video, "video/mp4")

    music_id = None
    if state.get("music_path") and os.path.exists(state["music_path"]):
        mime = "audio/mpeg" if state["music_path"].lower().endswith(".mp3") else "audio/wav"
        music_id, music_meta = b.add_media(state["music_path"], mime)

    ring_png = os.path.join(jdir, "custom_ring.png")
    if not os.path.exists(ring_png):
        ring_png = os.path.join(os.path.dirname(jdir), "circle_ring.png")  # fallback (asset fixo)
    ring_id = None
    if os.path.exists(ring_png):
        ring_id, ring_meta = b.add_media(ring_png, "image/png")

    # --- tracks (ordem: fundo embaixo -> legenda em cima; audio por fora) ---
    t_bg = b.add_track("Fundo (tela)", "video")
    t_avatarbox = b.add_track("Avatar - caixa grande (cutaway)", "video")
    t_face = b.add_track("Avatar - círculo do rosto", "video")
    t_ring = b.add_track("Moldura do círculo", "video")
    t_caption = b.add_track("Legenda", "video")
    t_music = b.add_track("Música de fundo", "audio") if music_id else None

    # --- item: fundo cobrindo a tela toda ---
    b.add_item({
        "trackId": t_bg, "from": 0, "durationInFrames": total_frames,
        "label": "Fundo (captura de tela)", "type": "video", "mediaRef": bg_id,
        "sourceWidth": bg_meta["width"], "sourceHeight": bg_meta["height"],
        "transform": {"x": 0, "y": 0, "width": CANVAS_W, "height": CANVAS_H},
    })

    # --- posicoes que dependem da deteccao de rosto (words_cache.json) ---
    words_cache_path = os.path.join(jdir, "words_cache.json")
    fx = fy = None
    words = []
    if os.path.exists(words_cache_path):
        try:
            cache = json.load(open(words_cache_path, encoding="utf-8"))
            face = cache.get("face")
            if face and len(face) == 2:
                fx, fy = face
            words = cache.get("words") or []
        except Exception:
            pass

    src_w, src_h = avatar_meta["width"] or 1080, avatar_meta["height"] or 1920
    if fx is not None:
        face_x = int(fx - 300)
        face_y = int(fy - 300)
        box_x = int(fx - 430)
        box_y = int(fy - 0.57 * 1700)
        box_y = max(0, min(box_y, 1920 - 1700))
        box_x = max(0, min(box_x, 1080 - 860))

        # caixa grande do avatar (cutaway), tela cheia a partir de 110,110
        b.add_item({
            "trackId": t_avatarbox, "from": 0, "durationInFrames": total_frames,
            "label": "Avatar - caixa grande", "type": "video", "mediaRef": avatar_id,
            "sourceWidth": src_w, "sourceHeight": src_h,
            "crop": _crop_ratios(src_w, src_h, box_x, box_y, 860, 1700),
            "transform": {"x": 0, "y": 0, "width": 860, "height": 1700},
        })

        # circulo do rosto (posicao fixa na tela por modo)
        pos = CIRCLE_POS.get(mode, CIRCLE_POS["capture"])
        ring_x, ring_y = pos["ring"]
        ring_cx, ring_cy = ring_x + 149, ring_y + 149
        face_offset_x = ring_cx - CANVAS_W / 2
        face_offset_y = ring_cy - CANVAS_H / 2
        b.add_item({
            "trackId": t_face, "from": 0, "durationInFrames": total_frames,
            "label": "Avatar - círculo do rosto", "type": "video", "mediaRef": avatar_id,
            "sourceWidth": src_w, "sourceHeight": src_h,
            "crop": _crop_ratios(src_w, src_h, face_x, face_y, 600, 600),
            "transform": {
                "x": face_offset_x, "y": face_offset_y,
                "width": FACE_SIZE, "height": FACE_SIZE, "cornerRadius": FACE_SIZE / 2,
            },
        })

        if ring_id:
            b.add_item({
                "trackId": t_ring, "from": 0, "durationInFrames": total_frames,
                "label": "Moldura (ring)", "type": "image", "mediaRef": ring_id,
                "sourceWidth": ring_meta["width"], "sourceHeight": ring_meta["height"],
                "transform": {
                    "x": face_offset_x, "y": face_offset_y,
                    "width": RING_SIZE, "height": RING_SIZE,
                },
            })

    # --- legenda: um bloco de texto por "chunk" (mesmo agrupamento do ASS),
    # com o texto inteiro do chunk (SEM o destaque palavra-a-palavra do
    # karaoke original - ver limitação no relatório) ---
    caption_hex = (state.get("last_colors") or {}).get("caption") or "#9333EA"
    caption_x = CAPTION_CX - CANVAS_W / 2
    caption_y = CAPTION_CY - CANVAS_H / 2
    if words:
        for chunk in build_caption_chunks(words):
            frm = max(0, round(chunk["start"] * FPS))
            to = max(frm + 1, round(chunk["end"] * FPS))
            b.add_item({
                "trackId": t_caption, "from": frm, "durationInFrames": to - frm,
                "label": "Legenda", "type": "text", "text": chunk["text"],
                "fontSize": CAPTION_FONTSIZE, "fontFamily": CAPTION_FONT,
                "fontWeight": "bold", "color": caption_hex,
                "textAlign": "center", "verticalAlign": "middle",
                "stroke": {"width": 4, "color": "#000000"},
                "transform": {"x": caption_x, "y": caption_y, "width": 900},
            })

    # --- musica de fundo ---
    if music_id:
        music_start = state.get("music_start") or 0.0
        b.add_item({
            "trackId": t_music, "from": 0, "durationInFrames": total_frames,
            "label": "Música de fundo", "type": "audio", "mediaRef": music_id,
            "sourceStart": round(music_start * FPS), "volume": 0.10,
        })

    return b.write_zip(out_path, duration)
