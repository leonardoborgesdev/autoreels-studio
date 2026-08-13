#!/usr/bin/env python3
"""
Pipeline completo: fundo (captura de tela) + avatar (circulo + tela cheia
nos ultimos 6s + 2 cortes no meio) + legenda karaoke + borda girando.
Roda no servidor (Linux), sem depender do Windows local.

Uso: python3 pipeline_remoto.py <bg.mp4> <avatar.mp4> <nome_saida>
     [--ring PNG] [--gradient PNG] [--border PNG] [--caption-color "#9333EA"]
     [--work-dir DIR] [--force-retranscribe]

Cores customizadas: se --ring/--gradient/--border não forem passados, usa os
assets fixos originais (circle_ring.png / gradient_bg.png / avatar_box_border.png).
A dashboard gera versões coloridas via colorize.py e passa os paths aqui.

Cache de transcrição/rosto: se o mesmo avatar.mp4 (path + mtime) já foi
processado antes neste work_dir, reaproveita a transcrição e a posição do
rosto (words_cache.json) — só refaz a composição final. Isso permite trocar
a paleta de cores e recompor em segundos, sem rodar o whisper de novo.
"""
import argparse
import json
import os
import random
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
FONT = "Archivo Black"

DEFAULT_RING = os.path.join(BASE, "circle_ring.png")
DEFAULT_GRADIENT = os.path.join(BASE, "gradient_bg.png")
DEFAULT_BORDER = os.path.join(BASE, "avatar_box_border.png")
DEFAULT_CAPTION_HEX = "#9333EA"  # equivalente ao antigo &H00EA3393


def measure_loudnorm(wav_path, target_i=-16, target_tp=-1.5, target_lra=6):
    """Loudnorm em dois passes: mede a faixa real primeiro, depois aplica
    ganho fixo (linear=true). O modo de passe unico (usado antes) e um
    filtro ADAPTATIVO que vai ajustando o ganho ao longo do audio -- em
    clipes curtos (30-45s) isso produz um efeito de "bombeamento" bem
    perceptivel, o volume parece crescer conforme o video avanca. Passe
    duplo mede tudo de uma vez e aplica uma correcao ESTATICA, sem rampa.
    """
    import json as _json
    cmd = (
        f'ffmpeg -i "{wav_path}" -af '
        f'loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json '
        f'-f null - '
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    raw = result.stderr + result.stdout
    start = raw.rfind("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}"
    try:
        stats = _json.loads(raw[start:end + 1])
        return (
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:"
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:linear=true"
        )
    except (KeyError, ValueError):
        return f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}"


def run(cmd):
    subprocess.run(cmd, shell=True, check=True)


WORD_FIXES = {
    "envidia": "Nvidia",
    "envidia,": "Nvidia,",
    "envidia.": "Nvidia.",
    "nvidia": "Nvidia",
    "dipsik": "DeepSeek",
    "dipsik,": "DeepSeek,",
    "dipsick": "DeepSeek",
    "unifei": "UniFace",
    "unifei,": "UniFace,",
    "unifei.": "UniFace.",
    "foxignups,": "FckSignups,",
    "foxignups.": "FckSignups.",
    "foxignups": "FckSignups",
    "cançou": "cansou",
    "codix": "Codex",
    "códix": "Codex",
    "groc,": "Groq,",
    "groc.": "Groq.",
    "groc": "Groq",
    "alama.": "Ollama.",
    "alama,": "Ollama,",
    "alama": "Ollama",
    "respalhar.": "espalhar.",
    "respalhar": "espalhar",
    "sende,": "Send,",
    "sende.": "Send.",
    "sende": "Send",
    "funcionei": "funciona",
    "funcionei.": "funciona.",
    "pirapir.": "peer a peer.",
    "pirapir": "peer a peer",
    # revisao 12/08: correcoes achadas revisando os 7 videos publicados
    "resonix": "Reasonix",
    "resonix,": "Reasonix,",
    "resonix.": "Reasonix.",
    "parametros": "parâmetros",
    "parametros,": "parâmetros,",
    "parametros.": "parâmetros.",
    "recognition": "Rekognition",
    "recognition,": "Rekognition,",
    "recognition.": "Rekognition.",
    "mt": "MIT",
    "mt,": "MIT,",
    "mt.": "MIT.",
    "pensor": "open source",
    "pensor,": "open source,",
    "pensor.": "open source.",
    "pensors": "open source",
    "pensors,": "open source,",
}


def transcribe(avatar_wav):
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, info = model.transcribe(avatar_wav, language="pt", word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words:
            text = w.word.strip()
            fixed = WORD_FIXES.get(text.lower())
            words.append({"word": fixed if fixed else text, "start": w.start, "end": w.end})
    return words


def fix_word_sequence(words):
    for i in range(len(words) - 1):
        a, b = words[i]["word"], words[i + 1]["word"]
        a_clean, b_clean = a.strip(".,").lower(), b.strip(".,").lower()
        if a_clean == "i" and b.lower().lstrip("-").startswith("agigante"):
            words[i]["word"] = "IA"
            words[i + 1]["word"] = "GIGANTE." if b.endswith(".") else "GIGANTE"
        elif a_clean == "geek" and b_clean.startswith("hub"):
            words[i]["word"] = "GitHub"
            words[i + 1]["word"] = "" if b.endswith((",", ".")) and len(b_clean) <= 4 else b
        elif a_clean == "wi" and b.lower().lstrip("-").startswith("fi"):
            words[i]["word"] = "Wi-Fi" if not b.endswith((",", ".")) else f"Wi-Fi{b[-1]}"
            words[i + 1]["word"] = ""
        elif a_clean == "a" and b_clean.startswith("irllm"):
            words[i]["word"] = ""
            words[i + 1]["word"] = f"AirLLM{b[-1]}" if b.endswith((",", ".")) else "AirLLM"
        elif a_clean == "a" and b_clean == "p":
            words[i]["word"] = ""
            words[i + 1]["word"] = f"API{b[-1]}" if b.endswith((",", ".")) else "API"
        elif a_clean == "a" and b_clean.startswith("paz"):
            words[i]["word"] = ""
            words[i + 1]["word"] = f"Apache{b[-1]}" if b.endswith((",", ".")) else "Apache"
        elif a_clean == "de" and b_clean.startswith("psique"):
            words[i]["word"] = ""
            words[i + 1]["word"] = f"DeepSeek{b[-1]}" if b.endswith((",", ".")) else "DeepSeek"
    return [w for w in words if w["word"]]


def detect_face(frame_png):
    import cv2
    img = cv2.imread(frame_png)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = sorted(cascade.detectMultiScale(gray, 1.1, 5), key=lambda f: -f[2])
    x, y, w, h = faces[0]
    return x + w / 2, y + h / 2


def hex_to_ass(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H00{b}{g}{r}".upper()


def build_ass(words, out_path, caption_hex=DEFAULT_CAPTION_HEX):
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 6 or (w["word"].endswith((".", ",", "!", "?")) and len(cur) >= 3):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)

    def ts(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,3,5,50,50,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    HIGHLIGHT, WHITE, CX, CY, BS = hex_to_ass(caption_hex), "&H00FFFFFF", 540, 1345, chr(92)
    lines = []
    for chunk in chunks:
        n = len(chunk)
        split_at = (n + 1) // 2 if n > 4 else n
        for i, w in enumerate(chunk):
            p1, p2 = [], []
            for j, w2 in enumerate(chunk):
                color = HIGHLIGHT if j == i else WHITE
                piece = "{" + BS + "c" + color + "}" + w2["word"].upper()
                (p1 if j < split_at else p2).append(piece)
            body = " ".join(p1) + ((BS + "N" + " ".join(p2)) if p2 else "")
            text = "{" + BS + f"pos({CX},{CY})" + "}" + body
            lines.append(f"Dialogue: 0,{ts(w['start'])},{ts(w['end'])},Cap,,0,0,0,,{text}")
    open(out_path, "w", encoding="utf-8").write(header + "\n".join(lines))
    return len(lines)


def _load_cache(work_dir, avatar_path):
    cache_path = os.path.join(work_dir, "words_cache.json")
    if not os.path.exists(cache_path):
        return None
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        return None
    if cache.get("avatar_path") != avatar_path:
        return None
    if cache.get("avatar_mtime") != os.path.getmtime(avatar_path):
        return None
    return cache


def _save_cache(work_dir, avatar_path, words, fx, fy):
    cache_path = os.path.join(work_dir, "words_cache.json")
    json.dump(
        {"avatar_path": avatar_path, "avatar_mtime": os.path.getmtime(avatar_path), "words": words, "face": [fx, fy]},
        open(cache_path, "w", encoding="utf-8"),
    )


# Posição do pequeno círculo do avatar (ring+rosto), por modo.
# "capture": o mesmo lugar de sempre (topo), some pros cortes em tela cheia.
# "remodel": mesmo canto por padrão (ajuste fácil aqui se quiser mudar sem
#   mexer no resto do pipeline) — fica sempre visível, sem cortes em tela
#   cheia, porque o fundo já é o próprio reel de referência.
CIRCLE_POS = {
    "capture": {"face": (759, 130), "ring": (751, 122)},
    "remodel": {"face": (758, 58), "ring": (750, 50)},
}

CAPTION_COVER = os.path.join(BASE, "caption_cover.png")


def build_video(
    bg_path, avatar_path, out_name, work_dir,
    ring_path=None, gradient_path=None, border_path=None,
    caption_hex=DEFAULT_CAPTION_HEX, force_retranscribe=False, mode="capture",
    music_path=None, music_start=None,
):
    os.makedirs(work_dir, exist_ok=True)
    ring_path = ring_path or DEFAULT_RING
    gradient_path = gradient_path or DEFAULT_GRADIENT
    border_path = border_path or DEFAULT_BORDER

    cache = None if force_retranscribe else _load_cache(work_dir, avatar_path)
    if cache:
        print("ETAPA:cache", flush=True)
        print("usando transcricao e rosto em cache (so recompondo com as novas cores)", flush=True)
        words = cache["words"]
        fx, fy = cache["face"]
    else:
        print("ETAPA:transcrevendo", flush=True)
        wav = os.path.join(work_dir, "audio.wav")
        run(f'ffmpeg -v error -y -i "{avatar_path}" -vn -ar 16000 -ac 1 "{wav}"')

        words = fix_word_sequence(transcribe(wav))
        print("transcricao concluida, palavras:", len(words), flush=True)

        print("ETAPA:rosto", flush=True)
        frame_png = os.path.join(work_dir, "frame.png")
        run(f'ffmpeg -v error -y -ss 5 -i "{avatar_path}" -frames:v 1 "{frame_png}"')
        fx, fy = detect_face(frame_png)
        print("rosto detectado:", fx, fy, flush=True)

        _save_cache(work_dir, avatar_path, words, fx, fy)

    duration = words[-1]["end"] if words else 40.0
    print("duracao avatar:", duration, flush=True)

    wav = os.path.join(work_dir, "audio.wav")
    if not os.path.exists(wav):
        run(f'ffmpeg -v error -y -i "{avatar_path}" -vn -ar 16000 -ac 1 "{wav}"')
    loudnorm_filter = measure_loudnorm(wav)
    print("loudnorm (2 passes):", loudnorm_filter[:60] + "...", flush=True)

    ass_path = os.path.join(work_dir, "captions.ass")
    n = build_ass(words, ass_path, caption_hex=caption_hex)
    print("legendas:", n, flush=True)

    face_x = int(fx - 300)
    face_y = int(fy - 300)
    box_x = int(fx - 430)
    box_y_target = 0.57
    box_y = int(fy - box_y_target * 1700)
    box_y = max(0, min(box_y, 1920 - 1700))
    box_x = max(0, min(box_x, 1080 - 860))

    d = duration

    # musica de fundo (opcional): ambiente bem baixo, trecho aleatorio (ou
    # escolhido na dashboard) pra nao repetir sempre a mesma parte da faixa.
    music_start_resolved = None
    if music_path:
        music_start_resolved = music_start if music_start is not None else 0.0
        print(f"musica de fundo: {music_path} a partir de {music_start_resolved:.1f}s", flush=True)

    cut1_t = 8.0
    cut2_t = 20.0 if d > 30 else 15.0
    cut3_t = max(d - 6, 10)
    cut1 = "between(t\\,8\\,10.2)"
    cut2 = "between(t\\,20\\,22.2)" if d > 30 else "between(t\\,15\\,17.2)"
    cut3 = f"gte(t\\,{cut3_t:.1f})"
    sfx1_ms, sfx2_ms, sfx3_ms = int(cut1_t * 1000), int(cut2_t * 1000), int(cut3_t * 1000)
    cuts = f"{cut1}+{cut2}+{cut3}"

    out_path = os.path.join(work_dir, f"{out_name}.mp4")
    print("ETAPA:compondo", flush=True)
    print(f"iniciando composicao final (ffmpeg, modo={mode})...", flush=True)

    pos = CIRCLE_POS.get(mode, CIRCLE_POS["capture"])
    ring_x, ring_y = pos["ring"]
    fcx, fcy = pos["face"]

    if mode == "remodel":
        # Fundo = o proprio reel de referencia (ja tem gente falando + legenda
        # dele queimada). Cobrimos a legenda original com caption_cover.png
        # e deixamos o circulo do avatar novo sempre visivel no canto, sem
        # cortes em tela cheia (nao faz sentido aqui, o fundo ja eh a cena
        # inteira).
        #
        # Esses reels de referencia costumam terminar com um frame de
        # encerramento (ex: print do perfil do Instagram) nos ultimos ~1-2s.
        # Cortamos isso fora e damos LOOP no restante (em vez de congelar o
        # ultimo frame com tpad), senao esse frame de encerramento fica
        # congelado na tela por dezenas de segundos quando o avatar fala
        # mais tempo do que o bg original dura.
        probe = subprocess.run(
            f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{bg_path}"',
            shell=True, capture_output=True, text=True,
        )
        try:
            bg_duration = float(probe.stdout.strip())
        except ValueError:
            bg_duration = 0.0

        # Muitos desses reels de referencia abrem com um card de "post" (X/Instagram
        # do proprio criador) por 1-3s antes de entrar no conteudo real (repo/site).
        # Detecta o primeiro corte de cena forte dentro dos primeiros 4s e usa isso
        # como ponto de entrada, pulando esse card de abertura. Se nao achar corte
        # de cena claro, comeca do zero normalmente (nao tem card de intro).
        intro_offset = 0.0
        scdet = subprocess.run(
            f'ffmpeg -v error -i "{bg_path}" -t 4 -vf "select=\'gt(scene,0.35)\',metadata=print" -f null - 2>&1',
            shell=True, capture_output=True, text=True,
        )
        for line in (scdet.stdout + scdet.stderr).splitlines():
            if "pts_time:" in line:
                try:
                    t = float(line.split("pts_time:")[1].split()[0])
                    if 0.5 < t < 4.0:
                        intro_offset = t
                        break
                except (ValueError, IndexError):
                    pass
        if intro_offset:
            print(f"card de abertura detectado, pulando {intro_offset:.2f}s", flush=True)

        bg_trim = max(bg_duration - intro_offset - 2.0, 3.0) if bg_duration > 5.0 else max(bg_duration - intro_offset, 1.0)
        bg_trimmed_path = os.path.join(work_dir, "bg_trimmed.mp4")
        run(f'ffmpeg -v error -y -ss {intro_offset:.2f} -i "{bg_path}" -t {bg_trim:.2f} -an -c:v libx264 -preset veryfast -crf 18 "{bg_trimmed_path}"')

        cover_path = CAPTION_COVER if os.path.exists(CAPTION_COVER) else None
        # indices dos inputs: 0 bg, 1 avatar, 2 mask, 3 ring, 4 whoosh, 5 gradient,
        # 6 border (sempre presente agora, pros cortes em tela cheia), 7 cover (se existir)
        cover_idx = 7 if cover_path else None
        cover_input = f'-i "{cover_path}" \\\n' if cover_path else ""
        cover_chain = f"[bgraw][{cover_idx}:v]overlay=0:0:enable='eq({cuts},0)'[bgcov];\n" if cover_path else ""
        bg_after_cover = "bgcov" if cover_path else "bgraw"

        music_idx = (8 if cover_path else 7)
        music_input = f'-stream_loop -1 -i "{music_path}" \\\n' if music_path else ""
        music_filter_line = (
            f'[{music_idx}:a]atrim=start={music_start_resolved:.2f}:duration={d:.2f},'
            f'asetpts=PTS-STARTPTS,volume=0.10[music];\n'
        ) if music_path else ""
        # normalize=0 pra nao mexer no volume ja calibrado de voz+sfx: por
        # padrao o amix divide a soma por N de entradas (perderia ~2dB de
        # voz so por causa da musica entrar); aqui a musica de ambiente
        # (ja bem baixa, volume=0.10) so soma por cima, sem re-balancear
        # o mix de voz+sfx que ja estava correto.
        voxmix_plus_music = (
            "[voxmix][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[premix];\n[premix]"
            if music_path else "[voxmix]"
        )

        RING_SIZE = 299
        FACE_SIZE = RING_SIZE - 16
        ring_cx, ring_cy = ring_x + 149, ring_y + 149
        big_ring_x, big_ring_y = int(ring_cx - RING_SIZE / 2), int(ring_cy - RING_SIZE / 2)
        big_face_x, big_face_y = int(ring_cx - FACE_SIZE / 2), int(ring_cy - FACE_SIZE / 2)
        stretch_factor = max(d / bg_trim, 1.0) if bg_trim > 0 else 1.0
        cmd = f'''ffmpeg -y \
-i "{bg_trimmed_path}" \
-i "{avatar_path}" \
-i "{BASE}/circle_mask.png" \
-i "{ring_path}" \
-i "{BASE}/transition_whoosh.wav" \
-i "{gradient_path}" \
-loop 1 -framerate 30 -t {d+1:.1f} -i "{border_path}" \
{cover_input}{music_input}-filter_complex "
[0:v]scale=1080:1920,setpts={stretch_factor:.4f}*PTS[bgraw];
[1:v]split=2[av1][av2];
[av1]crop=600:600:{face_x}:{face_y},scale={FACE_SIZE}:{FACE_SIZE}:flags=lanczos,unsharp=5:5:1.4:5:5:0.7[facecrop];
[2:v]scale={FACE_SIZE}:{FACE_SIZE}[maskbig];
[maskbig]format=gray[maskg];
[facecrop][maskg]alphamerge[facea];
[3:v]scale={RING_SIZE}:{RING_SIZE}[ringbig];
[av2]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,crop=860:1700:{box_x}:{box_y},unsharp=5:5:1.4:5:5:0.7[avatarbox];
[6:v]hue=h='t*140':s=1.2[spinborder];
{cover_chain}[{bg_after_cover}][facea]overlay={big_face_x}:{big_face_y}:enable='eq({cuts},0)'[s1];
[s1][ringbig]overlay={big_ring_x}:{big_ring_y}:enable='eq({cuts},0)'[s2];
[s2][5:v]overlay=0:0:enable='{cuts}'[s3];
[s3][avatarbox]overlay=110:110:enable='{cuts}'[s4];
[s4][spinborder]overlay=100:100:enable='{cuts}'[s5];
[s5]trim=duration={d:.2f},ass={ass_path}[outv];
[4:a]adelay={sfx1_ms}|{sfx1_ms}[sfx1];
[4:a]adelay={sfx2_ms}|{sfx2_ms}[sfx2];
[4:a]adelay={sfx3_ms}|{sfx3_ms}[sfx3];
[1:a]{loudnorm_filter},highpass=f=90,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=100:makeup=1.5[avclean];
{music_filter_line}[avclean][sfx1][sfx2][sfx3]amix=inputs=4:duration=first:dropout_transition=0:normalize=0[voxmix];
{voxmix_plus_music}alimiter=limit=0.95:attack=5:release=50[outa]
" -map "[outv]" -map "[outa]" -c:v libx264 -preset slow -crf 14 -pix_fmt yuv420p -c:a aac -b:a 224k "{out_path}"'''
    else:
        music_idx = 7
        music_input = f'-stream_loop -1 -i "{music_path}" \\\n' if music_path else ""
        music_filter_line = (
            f'[{music_idx}:a]atrim=start={music_start_resolved:.2f}:duration={d:.2f},'
            f'asetpts=PTS-STARTPTS,volume=0.10[music];\n'
        ) if music_path else ""
        voxmix_plus_music = (
            "[voxmix][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[premix];\n[premix]"
            if music_path else "[voxmix]"
        )

        cmd = f'''ffmpeg -y \
-i "{bg_path}" \
-i "{avatar_path}" \
-i "{BASE}/circle_mask.png" \
-i "{ring_path}" \
-i "{BASE}/transition_whoosh.wav" \
-i "{gradient_path}" \
-loop 1 -framerate 30 -t {d+1:.1f} -i "{border_path}" \
{music_input}-filter_complex "
[0:v]scale=1080:1920,setpts=PTS-STARTPTS[bgraw];
[1:v]split=2[av1][av2];
[av1]crop=600:600:{face_x}:{face_y},scale=283:283:flags=lanczos,unsharp=5:5:1.4:5:5:0.7[facecrop];
[2:v]format=gray[maskg];
[facecrop][maskg]alphamerge[facea];
[av2]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,crop=860:1700:{box_x}:{box_y},unsharp=5:5:1.4:5:5:0.7[avatarbox];
[6:v]hue=h='t*140':s=1.2[spinborder];
[bgraw]tpad=stop_mode=clone:stop_duration=10[bgmain];
[bgmain][facea]overlay=759:130:enable='eq({cuts},0)'[s1];
[s1][3:v]overlay=751:122:enable='eq({cuts},0)'[s2];
[s2][5:v]overlay=0:0:enable='{cuts}'[s3];
[s3][avatarbox]overlay=110:110:enable='{cuts}'[s4];
[s4][spinborder]overlay=100:100:enable='{cuts}'[s5];
[s5]trim=duration={d:.2f},ass={ass_path}[outv];
[4:a]adelay={sfx1_ms}|{sfx1_ms}[sfx1];
[4:a]adelay={sfx2_ms}|{sfx2_ms}[sfx2];
[4:a]adelay={sfx3_ms}|{sfx3_ms}[sfx3];
[1:a]{loudnorm_filter},highpass=f=90,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=100:makeup=1.5[avclean];
{music_filter_line}[avclean][sfx1][sfx2][sfx3]amix=inputs=4:duration=first:dropout_transition=0:normalize=0[voxmix];
{voxmix_plus_music}alimiter=limit=0.95:attack=5:release=50[outa]
" -map "[outv]" -map "[outa]" -c:v libx264 -preset slow -crf 14 -pix_fmt yuv420p -c:a aac -b:a 224k "{out_path}"'''
    run(cmd)
    print("VIDEO_PRONTO:", out_path, flush=True)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bg")
    ap.add_argument("avatar")
    ap.add_argument("name")
    ap.add_argument("--ring", default=None)
    ap.add_argument("--gradient", default=None)
    ap.add_argument("--border", default=None)
    ap.add_argument("--caption-color", default=DEFAULT_CAPTION_HEX)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--force-retranscribe", action="store_true")
    ap.add_argument("--mode", choices=["capture", "remodel"], default="capture")
    ap.add_argument("--music", default=None, help="mp3 de musica de fundo (volume baixo)")
    ap.add_argument("--music-start", type=float, default=None, help="segundo de inicio na musica (aleatorio se omitido)")
    args = ap.parse_args()

    work_dir = args.work_dir or os.path.join(BASE, "saida_" + args.name)
    build_video(
        args.bg, args.avatar, args.name, work_dir,
        ring_path=args.ring, gradient_path=args.gradient, border_path=args.border,
        caption_hex=args.caption_color, force_retranscribe=args.force_retranscribe,
        mode=args.mode, music_path=args.music, music_start=args.music_start,
    )
