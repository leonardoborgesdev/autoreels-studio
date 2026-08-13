#!/usr/bin/env python3
"""
Dashboard do canal de reels de repositórios do GitHub.

Dois modos:
  - "Gerar novo": cola link do repo -> gera roteiro PT-BR (Gemini, com
    fallback heurístico) e já dispara a gravação de tela (scroll_capture.py)
    em paralelo. Usuário grava a narração lendo o roteiro, clona no HeyGen
    (manual, fora do sistema) e faz upload do vídeo do avatar aqui. Depois
    escolhe a paleta de cores e compõe (pipeline_remoto.py, mode=capture).
  - "Remodelar": reaproveita reels de referência já baixados (com roteiro
    próprio escrito por fora) — sobe o avatar HeyGen daquele roteiro e
    compõe por cima do reel de referência, cobrindo a legenda original
    (pipeline_remoto.py, mode=remodel).

Cada composição reaproveita a transcrição/detecção de rosto já feita (cache
por avatar), então trocar cor e recompor é rápido.

Vídeo final fica disponível pra download, nomeado
REPOvN_dominio-caminho-do-repo_roteiro-sanitizado.mp4

Roda com gunicorn (ver canal-github-reels-dashboard.service) ou direto:
    venv/bin/python3 app.py
"""
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import unicodedata
import uuid

from flask import Flask, jsonify, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import colorize  # noqa: E402
import llm  # noqa: E402
import freecut_bundle  # noqa: E402

BASE_PIPELINE_DIR = "/opt/canal-github-reels"
DASH_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(DASH_DIR, "jobs")
VENV_PY = os.path.join(BASE_PIPELINE_DIR, "venv", "bin", "python3")
SCROLL_CAPTURE = os.path.join(BASE_PIPELINE_DIR, "scroll_capture.py")
PIPELINE = os.path.join(BASE_PIPELINE_DIR, "pipeline_remoto.py")
COUNTERS_FILE = os.path.join(DASH_DIR, "version_counters.json")

# ---- modo "Remodelar" (reels de referência já baixados + roteiro próprio) ----
REMODEL_DIR = os.path.join(BASE_PIPELINE_DIR, "remodel_sources")
REMODEL_BG_DIR = os.path.join(REMODEL_DIR, "bg")
REMODEL_ROTEIRO_DIR = os.path.join(REMODEL_DIR, "roteiros")
REMODEL_MAP_FILE = os.path.join(REMODEL_DIR, "MAPA_VIDEOS_ROTEIROS.txt")
ALLOWED_BG_EXT = {".mp4", ".mov", ".mkv", ".webm"}

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(REMODEL_BG_DIR, exist_ok=True)
os.makedirs(REMODEL_ROTEIRO_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024  # 600MB, avatar/bg em HD pode ser grande

_lock = threading.Lock()
_counter_lock = threading.Lock()

# --- autenticação básica simples (dashboard interno, exposto por IP até o DNS ser resolvido) ---
AUTH_FILE = os.path.join(DASH_DIR, "auth_secret.txt")


def _get_or_create_password():
    if os.path.exists(AUTH_FILE):
        return open(AUTH_FILE, encoding="utf-8").read().strip()
    pwd = secrets.token_urlsafe(9)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(pwd)
    os.chmod(AUTH_FILE, 0o600)
    return pwd


DASH_PASSWORD = _get_or_create_password()
DASH_USER = "henrique"


# auth desativada a pedido do usuario


# ---------------- job storage (arquivos em disco, simples e sobrevive a restart) ----------------

def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def state_path(job_id):
    return os.path.join(job_dir(job_id), "state.json")


def load_state(job_id):
    try:
        with open(state_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    # tmp filename precisa ser único por chamada: do_generate_script e do_capture
    # rodam em threads paralelas pro mesmo job_id, e um nome fixo de tmp causava
    # FileNotFoundError no os.replace quando uma thread roubava o tmp da outra.
    os.makedirs(job_dir(state["id"]), exist_ok=True)
    tmp = state_path(state["id"]) + f".tmp.{os.getpid()}.{threading.get_ident()}"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path(state["id"]))


def append_log(state, msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    state.setdefault("log", []).append(f"[{ts}] {msg}")
    save_state(state)


def list_jobs(kind=None):
    jobs = []
    if not os.path.isdir(JOBS_DIR):
        return jobs
    for jid in sorted(os.listdir(JOBS_DIR), reverse=True):
        st = load_state(jid)
        if st and (kind is None or st.get("kind", "capture") == kind):
            jobs.append(st)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


def list_all_outputs():
    """Todos os vídeos finais já gerados, de qualquer job/slot, mais recentes primeiro."""
    outs = []
    for st in list_jobs():
        title = (st.get("repo_info") or {}).get("full_name") or st.get("repo_url") or st.get("slot_label") or st["id"]
        for idx, o in enumerate(st.get("outputs", [])):
            if o.get("path") and os.path.exists(o["path"]):
                outs.append({
                    "job_id": st["id"],
                    "idx": idx,
                    "kind": st.get("kind", "capture"),
                    "title": title,
                    "name": o.get("name"),
                    "created_at": o.get("created_at") or st.get("created_at"),
                    "roteiro": st.get("roteiro", ""),
                    "has_thumb": bool(o.get("thumb")),
                })
    outs.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return outs


# ---------------- nomeação dos arquivos finais ----------------

def slugify(text, maxlen=None):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    text = re.sub(r"-{2,}", "-", text)
    if maxlen:
        text = text[:maxlen].rstrip("-")
    return text or "x"


def next_version(reponame):
    """Contador global por nome de repo (persistido em disco), pra numerar
    REPOv1, REPOv2... a cada composição final gerada, mesmo em jobs diferentes."""
    with _counter_lock:
        counters = {}
        if os.path.exists(COUNTERS_FILE):
            try:
                counters = json.load(open(COUNTERS_FILE, encoding="utf-8"))
            except Exception:
                counters = {}
        v = counters.get(reponame, 0) + 1
        counters[reponame] = v
        json.dump(counters, open(COUNTERS_FILE, "w", encoding="utf-8"))
        return v


def build_output_name(repo_info, roteiro):
    reponame = re.sub(r"[^A-Za-z0-9]", "", repo_info["repo"]).upper() or "REPO"
    version = next_version(reponame)
    domain_path = slugify(repo_info["html_url"].replace("https://", "").replace("http://", ""), maxlen=60)
    roteiro_slug = slugify(roteiro, maxlen=120)
    return f"{reponame}v{version}_{domain_path}_{roteiro_slug}"


def build_output_name_remodel(slot_label, roteiro):
    base = re.sub(r"[^A-Za-z0-9]", "", slot_label).upper() or "REMODEL"
    version = next_version(base)
    roteiro_slug = slugify(roteiro, maxlen=120)
    return f"{base}v{version}_remodel_{roteiro_slug}"


# ---------------- thumbnails ----------------

def generate_thumbnail(video_path, thumb_path):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", video_path, "-frames:v", "1",
             "-vf", "scale=270:-1", thumb_path],
            check=True, timeout=30,
        )
        return True
    except Exception:
        return False


# ---------------- workers em background ----------------

def run_and_log(cmd, state_id, on_etapa=None):
    state = load_state(state_id)
    append_log(state, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("ETAPA:") and on_etapa:
            on_etapa(line.split(":", 1)[1])
        state = load_state(state_id)
        append_log(state, line)
    proc.wait()
    return proc.returncode


def _now_iso():
    return datetime.datetime.now().isoformat()


def set_render_step(job_id, step):
    """Atualiza a etapa atual da composição e cronometra cada substep
    (transcrevendo/rosto/compondo/...) pra dar timer em tempo real no front."""
    state = load_state(job_id)
    now = _now_iso()
    prev = state.get("render_step")
    state.setdefault("step_started_at", {})
    state.setdefault("step_finished_at", {})
    if prev and prev != step:
        state["step_finished_at"][prev] = now
    state["render_step"] = step
    state["step_started_at"][step] = now
    save_state(state)


def do_capture(job_id):
    state = load_state(job_id)
    state["capture_status"] = "capturando"
    state["capture_started_at"] = _now_iso()
    state["capture_finished_at"] = None
    append_log(state, "Iniciando gravação de tela do repositório...")
    jdir = job_dir(job_id)
    rc = run_and_log([VENV_PY, SCROLL_CAPTURE, state["repo_info"]["html_url"], jdir, "60"], job_id)
    state = load_state(job_id)
    out_path = os.path.join(jdir, "captura_final_1080x1920.mp4")
    if rc == 0 and os.path.exists(out_path):
        state["bg_video"] = out_path
        state["bg_version"] = state.get("bg_version", 0) + 1
        state["capture_status"] = "pronto"
        append_log(state, "Gravação de tela concluída.")
    else:
        state["capture_status"] = "erro"
        append_log(state, f"Falha na gravação de tela (código {rc}).")
    state["capture_finished_at"] = _now_iso()
    save_state(state)


def do_generate_script(job_id, idioma="pt", engine="gemini", tema_extra=None):
    """Gera o roteiro via LLM. Funciona tanto pro modo 'Gerar novo' (usa
    repo_info: descrição + README) quanto pro modo 'Remodelar' (não tem
    repo_info; usa o rótulo do slot + o tema/contexto opcional digitado pelo
    usuário como 'descrição')."""
    state = load_state(job_id)
    state["script_status"] = "gerando"
    state["script_started_at"] = _now_iso()
    state["script_finished_at"] = None
    save_state(state)
    engine_label = "Claude" if engine == "claude" else "Gemini"
    idioma_label = "English" if idioma == "en" else "PT-BR"
    append_log(state, f"Gerando roteiro ({engine_label} / fallback, {idioma_label})...")
    info = state.get("repo_info")
    if info:
        full_name, description, readme_text = info["full_name"], info["description"], info["readme"]
        stars, language = info["stars"], info["language"]
    else:
        full_name = state.get("slot_label", job_id)
        description = tema_extra or state.get("roteiro_tema") or ""
        readme_text = ""
        stars, language = None, None
    try:
        texto, origem = llm.gerar_roteiro(
            full_name, description, readme_text, stars, language, idioma=idioma, engine=engine,
        )
        state = load_state(job_id)
        state["roteiro"] = texto
        state["roteiro_origem"] = origem
        state["roteiro_idioma"] = idioma
        state["roteiro_engine"] = engine
        state["script_status"] = "pronto"
        append_log(state, f"Roteiro gerado (origem: {origem}).")
    except Exception as e:  # noqa: BLE001
        state = load_state(job_id)
        state["script_status"] = "erro"
        append_log(state, f"Erro ao gerar roteiro: {e}")
    state["script_finished_at"] = _now_iso()
    save_state(state)


def do_render(job_id, ring_colors, bg_colors, border_colors, caption_color):
    state = load_state(job_id)
    mode = state.get("kind", "capture")
    state["render_status"] = "renderizando"
    state["render_step"] = "preparando"
    state["render_started_at"] = _now_iso()
    state["render_finished_at"] = None
    state["step_started_at"] = {"preparando": state["render_started_at"]}
    state["step_finished_at"] = {}
    state["last_colors"] = {"ring": ring_colors, "bg": bg_colors, "border": border_colors, "caption": caption_color}
    append_log(state, "Gerando assets de cor personalizados...")
    save_state(state)

    jdir = job_dir(job_id)
    try:
        assets = colorize.generate_palette_assets(
            jdir, ring_colors=ring_colors, bg_colors=bg_colors, border_colors=border_colors,
        )
    except Exception as e:  # noqa: BLE001
        state = load_state(job_id)
        state["render_status"] = "erro"
        state["render_finished_at"] = _now_iso()
        append_log(state, f"Erro gerando assets de cor: {e}")
        save_state(state)
        return

    if mode == "remodel":
        out_name = build_output_name_remodel(state.get("slot_label", job_id), state.get("roteiro", ""))
    else:
        out_name = build_output_name(state["repo_info"], state.get("roteiro", ""))
    state = load_state(job_id)
    state["current_output_name"] = out_name
    append_log(state, f"Nome de saída: {out_name}")
    save_state(state)

    cmd = [
        VENV_PY, PIPELINE, state["bg_video"], state["avatar_video"], out_name,
        "--ring", assets["ring"], "--gradient", assets["gradient"], "--border", assets["border"],
        "--caption-color", caption_color, "--work-dir", jdir, "--mode", mode,
    ]
    if state.get("music_path"):
        cmd += ["--music", state["music_path"]]
        if state.get("music_start") is not None:
            cmd += ["--music-start", str(state["music_start"])]
        append_log(state, f"Música de fundo: {os.path.basename(state['music_path'])}"
                           + (f" (início manual: {state['music_start']}s)" if state.get("music_start") is not None else " (trecho aleatório)"))
    rc = run_and_log(cmd, job_id, on_etapa=lambda s: set_render_step(job_id, s))

    state = load_state(job_id)
    out_path = os.path.join(jdir, f"{out_name}.mp4")
    if rc == 0 and os.path.exists(out_path):
        thumb_path = os.path.join(jdir, f"{out_name}_thumb.jpg")
        generate_thumbnail(out_path, thumb_path)
        state["output_video"] = out_path
        state["output_name"] = out_name
        state["output_version"] = state.get("output_version", 0) + 1
        state.setdefault("outputs", []).append({
            "name": out_name, "path": out_path, "colors": state["last_colors"],
            "created_at": datetime.datetime.now().isoformat(),
            "thumb": thumb_path if os.path.exists(thumb_path) else None,
        })
        state["render_status"] = "pronto"
        state["render_step"] = "pronto"
        append_log(state, "Vídeo final pronto para download.")
    else:
        state["render_status"] = "erro"
        state["render_step"] = "erro"
        append_log(state, f"Falha na renderização final (código {rc}).")
    last_step = state.get("render_step")
    now = _now_iso()
    state.setdefault("step_finished_at", {})
    if last_step:
        state["step_finished_at"][last_step] = now
    state["render_finished_at"] = now
    save_state(state)

    if state.get("render_status") == "pronto":
        # gera a legenda do post + o fluxo de DM automática logo depois que o
        # vídeo termina de compor, numa thread separada pra não segurar o
        # vídeo já pronto pra download enquanto o LLM responde.
        threading.Thread(target=do_generate_legenda_dm, args=(job_id,), daemon=True).start()


# ---------------- legenda do post + DM automática (mesmo motor do roteiro) ----------------

def _legenda_dm_contexto(state):
    """Monta (nome, description, readme, stars, language, gatilho, repo_url)
    a partir do state do job, cobrindo tanto o modo 'capture' (tem repo_info
    completo) quanto o modo 'remodel' (só tem rótulo do slot + roteiro; tenta
    achar a URL do repositório dentro do próprio roteiro, se tiver)."""
    if state.get("kind") == "remodel":
        nome = state.get("slot_label") or state["id"]
        description = state.get("roteiro_tema") or ""
        readme_text = ""
        stars, language = None, None
        m = re.search(r"https?://github\.com/[^\s)\]]+", state.get("roteiro") or "")
        repo_url = (m.group(0).rstrip(".,") if m else (state.get("repo_url") or ""))
    else:
        info = state.get("repo_info") or {}
        nome = info.get("full_name") or state.get("repo_url") or state["id"]
        description = info.get("description") or ""
        readme_text = info.get("readme") or ""
        stars, language = info.get("stars"), info.get("language")
        repo_url = info.get("html_url") or state.get("repo_url") or ""
    gatilho = llm._gatilho_de(nome)
    return nome, description, readme_text, stars, language, gatilho, repo_url


def do_generate_legenda_dm(job_id):
    """Gera a legenda do post do Instagram + o fluxo de DM (2 passos), no
    mesmo padrão usado manualmente pros primeiros vídeos do canal, usando o
    mesmo motor de LLM (gemini/claude) escolhido pro roteiro desse job."""
    state = load_state(job_id)
    if not state:
        return
    state["legenda_dm_status"] = "gerando"
    append_log(state, "Gerando legenda do post + DM automática...")

    nome, description, readme_text, stars, language, gatilho, repo_url = _legenda_dm_contexto(state)
    idioma = state.get("roteiro_idioma") or "pt"
    engine = state.get("roteiro_engine") or "gemini"

    try:
        texto, origem = llm.gerar_legenda_dm(
            nome, description, state.get("roteiro", ""), readme_text, gatilho, repo_url,
            stars=stars, language=language, idioma=idioma, engine=engine,
        )
        state = load_state(job_id)
        legenda_path = os.path.join(job_dir(job_id), "legenda_dm.txt")
        with open(legenda_path, "w", encoding="utf-8") as f:
            f.write(texto)
        state["legenda_dm"] = texto
        state["legenda_dm_origem"] = origem
        state["legenda_dm_path"] = legenda_path
        state["legenda_dm_status"] = "pronto"
        append_log(state, f"Legenda do post + DM geradas (origem: {origem}).")
    except Exception as e:  # noqa: BLE001
        state = load_state(job_id)
        state["legenda_dm_status"] = "erro"
        append_log(state, f"Erro ao gerar legenda/DM: {e}")
    save_state(state)


# ---------------- modo Remodelar: slots a partir dos reels de referência ----------------

def _parse_remodel_map():
    """Lê remodel_sources/MAPA_VIDEOS_ROTEIROS.txt se existir. Formato tolerante,
    uma linha por reel: 'arquivo.mp4 -> repo/nome (roteiro_08.txt)' ou similar —
    aqui só tentamos achar, em cada linha, um nome de arquivo .mp4 e um nome de
    arquivo .txt; o resto vira o rótulo. Se o arquivo não existir ainda, não tem
    problema — os slots funcionam sem ele (roteiro fica editável na tela)."""
    mapping = {}
    if not os.path.exists(REMODEL_MAP_FILE):
        return mapping
    try:
        for line in open(REMODEL_MAP_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            mp4 = re.search(r"[\w\-.]+\.mp4", line)
            txt = re.search(r"[\w\-.]+\.txt", line)
            if not mp4:
                continue
            mapping[mp4.group(0)] = {"roteiro_file": txt.group(0) if txt else None, "raw": line}
    except Exception:
        pass
    return mapping


def ensure_remodel_slots():
    """Garante que existe um job (kind=remodel) pra cada vídeo de fundo presente
    em remodel_sources/bg/. Roda toda vez que a tela de Remodelar é aberta —
    barato (só lista arquivos) e deixa a tela sempre atualizada assim que
    alguém sobe um bg novo, sem precisar reiniciar o dashboard."""
    if not os.path.isdir(REMODEL_BG_DIR):
        return
    mapping = _parse_remodel_map()
    existing_by_bg = {}
    for st in list_jobs(kind="remodel"):
        if st.get("bg_video"):
            existing_by_bg[os.path.basename(st["bg_video"])] = st

    for fname in sorted(os.listdir(REMODEL_BG_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_BG_EXT:
            continue
        if fname in existing_by_bg:
            continue
        slug = slugify(os.path.splitext(fname)[0], maxlen=24)
        job_id = f"remodel-{slug}"
        if load_state(job_id):
            continue
        info = mapping.get(fname, {})
        roteiro_text = ""
        if info.get("roteiro_file"):
            rpath = os.path.join(REMODEL_ROTEIRO_DIR, info["roteiro_file"])
            if os.path.exists(rpath):
                try:
                    roteiro_text = open(rpath, encoding="utf-8").read().strip()
                except Exception:
                    roteiro_text = ""
        state = {
            "id": job_id,
            "kind": "remodel",
            "slot_label": os.path.splitext(fname)[0],
            "bg_video": os.path.join(REMODEL_BG_DIR, fname),
            "bg_source_note": info.get("raw", ""),
            "created_at": datetime.datetime.now().isoformat(),
            "roteiro": roteiro_text,
            "roteiro_origem": "mapa" if roteiro_text else None,
            "roteiro_idioma": "pt",
            "roteiro_engine": "gemini",
            "avatar_video": None,
            "avatar_version": 0,
            "bg_version": 1,
            "output_version": 0,
            "render_status": "aguardando",
            "render_step": None,
            "output_video": None,
            "music_path": None,
            "music_start": None,
            "outputs": [],
            "log": [],
        }
        save_state(state)


# ---------------- rotas ----------------

@app.route("/")
def index():
    return render_template("index.html", jobs=list_jobs(kind="capture"), active_tab="novo")


@app.route("/remodelar")
def remodelar():
    ensure_remodel_slots()
    return render_template(
        "remodelar.html", jobs=list_jobs(kind="remodel"),
        map_exists=os.path.exists(REMODEL_MAP_FILE), active_tab="remodelar",
    )


@app.route("/historico")
def historico():
    return render_template("historico.html", outputs=list_all_outputs(), active_tab="historico")


@app.route("/job/<job_id>")
def job_page(job_id):
    state = load_state(job_id)
    if not state:
        return "Job não encontrado", 404
    is_remodel = state.get("kind") == "remodel"
    template = "remodel_job.html" if is_remodel else "job.html"
    return render_template(
        template, job=state,
        default_ring=colorize.DEFAULT_RING_COLORS,
        default_bg=colorize.DEFAULT_BG_COLORS,
        default_border=colorize.DEFAULT_BORDER_COLORS,
        default_caption=colorize.DEFAULT_CAPTION_COLOR,
        active_tab="remodelar" if is_remodel else "novo",
    )


@app.route("/api/jobs", methods=["POST"])
def create_job():
    data = request.json or request.form
    repo_url = (data.get("repo_url") or "").strip()
    if not repo_url:
        return jsonify({"error": "informe o link do repositório"}), 400
    idioma = data.get("idioma") or "pt"
    engine = data.get("engine") or "gemini"
    if idioma not in llm.IDIOMAS:
        idioma = "pt"
    if engine not in llm.ENGINES:
        engine = "gemini"
    try:
        info = llm.fetch_repo_info(repo_url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex[:10]
    state = {
        "id": job_id,
        "kind": "capture",
        "repo_url": repo_url,
        "repo_info": info,
        "created_at": datetime.datetime.now().isoformat(),
        "script_status": "gerando",
        "roteiro_idioma": idioma,
        "roteiro_engine": engine,
        "capture_status": "aguardando",
        "avatar_video": None,
        "avatar_version": 0,
        "bg_version": 0,
        "output_version": 0,
        "render_status": "aguardando",
        "render_step": None,
        "output_video": None,
        "music_path": None,
        "music_start": None,
        "outputs": [],
        "log": [],
    }
    save_state(state)

    threading.Thread(target=do_generate_script, args=(job_id, idioma, engine), daemon=True).start()
    threading.Thread(target=do_capture, args=(job_id,), daemon=True).start()

    return jsonify({"job_id": job_id, "redirect": url_for("job_page", job_id=job_id)})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    return jsonify(state)


@app.route("/api/jobs/<job_id>/roteiro", methods=["POST"])
def update_roteiro(job_id):
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    texto = (request.json or request.form).get("roteiro", "")
    state["roteiro"] = texto
    state["roteiro_editado_manual"] = True
    save_state(state)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/gerar_roteiro", methods=["POST"])
def gerar_roteiro_route(job_id):
    """(Re)gera o roteiro via LLM com o idioma/motor escolhidos na tela.
    Funciona pro modo 'Gerar novo' (usa repo_info já buscado) e pro modo
    'Remodelar' (usa o rótulo do slot + tema/contexto opcional digitado)."""
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    if state.get("script_status") == "gerando":
        return jsonify({"error": "já tem uma geração de roteiro rodando pra esse job"}), 409

    data = request.json or request.form
    idioma = data.get("idioma") or state.get("roteiro_idioma") or "pt"
    engine = data.get("engine") or state.get("roteiro_engine") or "gemini"
    if idioma not in llm.IDIOMAS:
        idioma = "pt"
    if engine not in llm.ENGINES:
        engine = "gemini"
    tema = (data.get("tema") or "").strip()
    if tema:
        state["roteiro_tema"] = tema
        save_state(state)

    threading.Thread(target=do_generate_script, args=(job_id, idioma, engine, tema or None), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/regravar_tela", methods=["POST"])
def recapture(job_id):
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    threading.Thread(target=do_capture, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}


@app.route("/api/jobs/<job_id>/avatar", methods=["POST"])
def upload_avatar(job_id):
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    if "avatar" not in request.files:
        return jsonify({"error": "arquivo 'avatar' ausente"}), 400
    f = request.files["avatar"]
    filename = secure_filename(f.filename or "avatar.mp4")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXT:
        return jsonify({"error": f"extensão {ext} não suportada"}), 400
    dest = os.path.join(job_dir(job_id), f"avatar{ext}")
    f.save(dest)

    state = load_state(job_id)
    state["avatar_video"] = dest
    state["avatar_version"] = state.get("avatar_version", 0) + 1
    append_log(state, f"Avatar recebido ({filename}). Escolha as cores e clique em Compor vídeo.")
    save_state(state)
    return jsonify({"ok": True})


ALLOWED_MUSIC_EXT = {".mp3"}


@app.route("/api/jobs/<job_id>/musica", methods=["POST"])
def upload_music(job_id):
    """Upload opcional de MP3 de música de fundo (ambiência, volume bem baixo
    na composição final). Se 'music_start' vier no form, usa esse segundo fixo
    como início do trecho; se não vier, o pipeline sorteia um trecho aleatório
    da faixa a cada composição (pra não repetir sempre a mesma parte)."""
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    if "musica" not in request.files:
        return jsonify({"error": "arquivo 'musica' ausente"}), 400
    f = request.files["musica"]
    filename = secure_filename(f.filename or "musica.mp3")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_MUSIC_EXT:
        return jsonify({"error": f"extensão {ext} não suportada (use .mp3)"}), 400
    dest = os.path.join(job_dir(job_id), f"musica{ext}")
    f.save(dest)

    music_start_raw = (request.form.get("music_start") or "").strip()
    music_start = None
    if music_start_raw:
        try:
            music_start = max(0.0, float(music_start_raw))
        except ValueError:
            return jsonify({"error": "music_start inválido"}), 400

    state = load_state(job_id)
    state["music_path"] = dest
    state["music_start"] = music_start
    append_log(
        state,
        f"Música de fundo recebida ({filename})."
        + (f" Início manual: {music_start}s." if music_start is not None else " Trecho aleatório a cada composição."),
    )
    save_state(state)
    return jsonify({"ok": True})


@app.route("/api/remodel/upload_bg", methods=["POST"])
def upload_remodel_bg():
    """Upload manual de um reel de referência (bg) direto pela dashboard —
    evita depender de scp pra cada um dos 12 vídeos."""
    if "bg" not in request.files:
        return jsonify({"error": "arquivo 'bg' ausente"}), 400
    f = request.files["bg"]
    filename = secure_filename(f.filename or "bg.mp4")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_BG_EXT:
        return jsonify({"error": f"extensão {ext} não suportada"}), 400
    dest = os.path.join(REMODEL_BG_DIR, filename)
    f.save(dest)
    ensure_remodel_slots()
    return jsonify({"ok": True, "filename": filename})


def _valid_hex(h):
    return bool(re.match(r"^#[0-9a-fA-F]{6}$", h or ""))


@app.route("/api/jobs/<job_id>/compor", methods=["POST"])
def compose(job_id):
    state = load_state(job_id)
    if not state:
        return jsonify({"error": "não encontrado"}), 404
    if not state.get("bg_video"):
        return jsonify({"error": "vídeo de fundo ausente"}), 400
    if not state.get("avatar_video"):
        return jsonify({"error": "envie o vídeo do avatar primeiro"}), 400

    data = request.json or request.form
    ring_colors = data.get("ring_colors") or colorize.DEFAULT_RING_COLORS
    bg_colors = data.get("bg_colors") or colorize.DEFAULT_BG_COLORS
    border_colors = data.get("border_colors") or colorize.DEFAULT_BORDER_COLORS
    caption_color = data.get("caption_color") or colorize.DEFAULT_CAPTION_COLOR

    for h in list(ring_colors) + list(bg_colors) + list(border_colors) + [caption_color]:
        if not _valid_hex(h):
            return jsonify({"error": f"cor inválida: {h}"}), 400

    if state.get("render_status") == "renderizando":
        return jsonify({"error": "já tem uma composição rodando pra esse job"}), 409

    threading.Thread(
        target=do_render, args=(job_id, ring_colors, bg_colors, border_colors, caption_color), daemon=True,
    ).start()
    return jsonify({"ok": True})


def _safe_under_base(path):
    real = os.path.realpath(path)
    return real.startswith(os.path.realpath(BASE_PIPELINE_DIR)) or real.startswith(os.path.realpath(JOBS_DIR))


@app.route("/job/<job_id>/download")
def download(job_id):
    state = load_state(job_id)
    if not state or not state.get("output_video"):
        return "Vídeo ainda não está pronto", 404
    path = state["output_video"]
    if not _safe_under_base(path) or not os.path.exists(path):
        return "Arquivo não encontrado", 404
    name = state.get("output_name", "reel") + ".mp4"
    return send_file(path, as_attachment=True, download_name=name)


FREECUT_ORIGIN = "https://editor.automatrixapps99x.win"


@app.route("/job/<job_id>/freecut_bundle")
def freecut_bundle_download(job_id):
    """Gera (ou reaproveita, se ainda válido) o pacote de projeto do FreeCut
    (.freecut.zip) pro vídeo final desse job, com o avatar/legenda/música já
    posicionados igual ao vídeo composto.

    Servido com CORS liberado pra origem do FreeCut (editor.automatrixapps99x.win)
    porque agora é buscado direto via fetch() pelo próprio FreeCut (autoimport
    por query param, ver routes/projects/index.tsx) em vez de baixado pro
    disco do usuário e importado manualmente."""
    state = load_state(job_id)
    if not state or not state.get("output_video"):
        return jsonify({"error": "vídeo final ainda não está pronto"}), 404
    jdir = job_dir(job_id)
    out_path = os.path.join(jdir, "projeto_freecut.freecut.zip")
    try:
        needs_build = (
            not os.path.exists(out_path)
            or os.path.getmtime(out_path) < os.path.getmtime(state["output_video"])
        )
        if needs_build:
            freecut_bundle.generate_freecut_bundle(job_id, state, jdir, out_path)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"falha ao gerar projeto do editor: {e}"}), 500
    name = (state.get("output_name") or "reel") + ".freecut.zip"
    resp = send_file(out_path, as_attachment=True, download_name=name)
    resp.headers["Access-Control-Allow-Origin"] = FREECUT_ORIGIN
    resp.headers["Vary"] = "Origin"
    return resp


@app.route("/job/<job_id>/legenda_dm")
def legenda_dm_download(job_id):
    state = load_state(job_id)
    if not state or not state.get("legenda_dm_path"):
        return "legenda ainda não gerada", 404
    path = state["legenda_dm_path"]
    if not _safe_under_base(path) or not os.path.exists(path):
        return "arquivo não encontrado", 404
    name = (state.get("output_name") or "legenda") + "_legenda_dm.txt"
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/job/<job_id>/preview/<kind>")
def preview(job_id, kind):
    state = load_state(job_id)
    if not state:
        return "não encontrado", 404
    field = {"bg": "bg_video", "avatar": "avatar_video", "output": "output_video"}.get(kind)
    if not field or not state.get(field):
        return "não disponível", 404
    path = state[field]
    if not _safe_under_base(path) or not os.path.exists(path):
        return "arquivo não encontrado", 404
    return send_file(path)


@app.route("/job/<job_id>/thumb/<int:idx>")
def output_thumb(job_id, idx):
    state = load_state(job_id)
    if not state:
        return "não encontrado", 404
    outputs = state.get("outputs", [])
    if idx < 0 or idx >= len(outputs) or not outputs[idx].get("thumb"):
        return "sem thumbnail", 404
    path = outputs[idx]["thumb"]
    if not _safe_under_base(path) or not os.path.exists(path):
        return "arquivo não encontrado", 404
    return send_file(path)


if __name__ == "__main__":
    print(f"Senha do dashboard (usuário 'henrique'): {DASH_PASSWORD}")
    app.run(host="0.0.0.0", port=8099, debug=False, threaded=True)
