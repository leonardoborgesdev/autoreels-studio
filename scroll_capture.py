"""
Protótipo Fase 2: navega um repositório do GitHub, desce a pagina devagar
e grava em formato vertical (Stories), pronto pra depois receber
zoom em destaques + legenda karaoke + circulo do avatar (mesmo pipeline
ja validado no piloto @rammcodes_).

v2: forca modo escuro desde o 1o frame (sem flash branco) e ritmo de
scroll calculado dinamicamente pra bater uma duracao alvo (~40-50s).

Uso: python scroll_capture.py <url_do_repo> <pasta_saida> [duracao_alvo_seg]
"""
import os
import subprocess
import sys
import time
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

VIEWPORT_W = 540
VIEWPORT_H = 960
RECORD_W = 1080
RECORD_H = 1920
TARGET_DURATION_DEFAULT = 45  # segundos de scroll (sem contar pausas)
MIN_STEP_DELAY = 0.03

# injetado ANTES de qualquer coisa carregar: fundo preto de cara,
# evita o flash branco entre abrir a aba e o CSS do site carregar
DARK_INIT_SCRIPT = """
(() => {
    const s = document.createElement('style');
    s.textContent = 'html,body{background:#0d1117 !important;}';
    document.documentElement.appendChild(s);
})();
"""


def capture(url: str, out_dir: str, target_duration: float = TARGET_DURATION_DEFAULT):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            record_video_dir=out_dir,
            record_video_size={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=2,
            color_scheme="dark",  # respeita prefers-color-scheme: dark
        )
        context.add_init_script(DARK_INIT_SCRIPT)
        page = context.new_page()
        # "esquenta" o motor de JS da pagina (compila o 1o script) antes da
        # navegacao real comecar - sem isso a 1a chamada evaluate() depois do
        # goto paga um pedaco de JIT warmup que "rouba" tempo da rolagem.
        page.evaluate("1+1")

        # "networkidle" espera a rede ficar OCIOSA por 500ms - em paginas do
        # GitHub com varios badges/imagens/beacons isso as vezes nunca acontece
        # rapido, e o goto() so retorna quando isso acontecer (ou no timeout).
        # Enquanto isso, a pagina JA renderizou visualmente e a gravacao ja
        # esta rodando - ou seja, todo esse tempo de espera "morto" (pagina
        # parada, sem rolar ainda) fica gravado no video, comendo o tempo que
        # sobra pra rolagem terminar antes do corte final (duracao da fala do
        # avatar). "load" basta pro conteudo visual estar pronto e devolve
        # o controle bem mais rapido.
        page.goto(url, wait_until="load", timeout=20000)
        time.sleep(0.6)  # pausa curta no topo (hook visual, sem flash)

        total_height = page.evaluate("document.body.scrollHeight")

        # Reserva uma fatia final da duracao alvo pra ficar parado no rodape
        # (senao a rolagem "morde" o fim da fala do avatar no meio do scroll,
        # ou pode nem chegar la se a pagina for mais alta que o normal).
        # Fracao generosa de propósito: o video final e cortado na duracao
        # falada do avatar, que so e conhecida DEPOIS dessa captura - uma
        # margem maior aqui garante que a rolagem termina cedo o bastante
        # pra sobreviver a esse corte mesmo que a fala seja um pouco mais
        # curta que os 45s "alvo".
        FOOTER_PAUSE_FRAC = 0.22
        scroll_budget = target_duration * (1 - FOOTER_PAUSE_FRAC)
        footer_pause = target_duration * FOOTER_PAUSE_FRAC

        # A rolagem inteira roda DENTRO do browser via requestAnimationFrame,
        # nao em um loop Python que chama scrollBy() passo a passo. Motivo:
        # cada page.evaluate() do Playwright eh um round-trip Python<->browser
        # que custa dezenas/centenas de ms - um loop que assume MIN_STEP_DELAY
        # (30ms) entre passos fica MUITO mais lento que isso na pratica, e um
        # calculo de "passos restantes" baseado nessa suposicao erra pra menos
        # (fica devendo distancia, some com o fim da pagina antes da hora).
        # Fazendo tudo num unico evaluate() com rAF, o avanco eh medido com
        # performance.now() do proprio browser (sem overhead de rede) e a
        # posicao de scroll em cada frame eh a fracao EXATA do tempo decorrido
        # sobre o tempo alvo - impossivel "atrasar", e ainda reage a paginas
        # que crescem por lazy-load (recalcula scrollHeight a cada frame).
        scroll_duration_ms = int(scroll_budget * 1000)
        page.evaluate(
            """
            async (durationMs) => {
                const t0 = performance.now();
                await new Promise((resolve) => {
                    function step() {
                        const elapsed = performance.now() - t0;
                        const frac = Math.min(elapsed / durationMs, 1);
                        const totalH = document.body.scrollHeight;
                        window.scrollTo(0, totalH * frac);
                        if (frac < 1) {
                            requestAnimationFrame(step);
                        } else {
                            resolve();
                        }
                    }
                    requestAnimationFrame(step);
                });
            }
            """,
            scroll_duration_ms,
        )

        # garante que chegou EXATAMENTE no fim (ultimo frame do rAF pode ter
        # arredondado um pouco pra baixo se a pagina cresceu no ultimo instante)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # pausa final parada no rodape, cobrindo o resto da duracao alvo
        # (fecha exatamente quando o avatar termina de falar)
        time.sleep(max(0.6, footer_pause))
        video_path = page.video.path()
        context.close()
        browser.close()
        return video_path


def find_content_start(video_path: str, max_check: float = 10.0, step: float = 0.3, std_threshold: float = 12.0) -> float:
    """A gravacao comeca antes da pagina realmente carregar (o tempo de
    'networkidle' fica gravado como tela vazia/uniforme). Essa funcao acha
    o primeiro instante em que aparece conteudo de verdade (desvio padrao
    de luminancia acima do limiar), com uma margem de seguranca de volta."""
    tmp_dir = os.path.join(os.path.dirname(video_path) or ".", "_tmp_probe")
    os.makedirs(tmp_dir, exist_ok=True)
    t = 0.0
    found = 0.0
    while t < max_check:
        out = os.path.join(tmp_dir, f"p_{t:.1f}.png")
        subprocess.run(
            f'ffmpeg -v error -y -ss {t} -i "{video_path}" -frames:v 1 "{out}"',
            shell=True,
        )
        if os.path.exists(out):
            std = np.array(Image.open(out).convert("L")).std()
            os.remove(out)
            if std > std_threshold:
                found = max(0.0, t - step)
                break
        t += step
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    return found


def zoom_and_export(raw_video_path: str, out_mp4_path: str, zoom: float = 1.0, auto_trim: bool = True):
    """Amplia o frame bruto (540x960, layout mobile do GitHub) pra 1080x1920
    com lanczos - fica nitido e sem cortar nada (zoom=1.0 = so upscale limpo).
    Se quiser um zoom real de destaque, usar zoom>1 SOMENTE em pontos
    especificos da pagina (nao no video inteiro, senao corta conteudo
    nas bordas - foi testado e fica ruim).
    auto_trim: corta a tela vazia do inicio (antes do site carregar de vez)."""
    crop_w = int(VIEWPORT_W / zoom)
    crop_h = int(VIEWPORT_H / zoom)
    x = (VIEWPORT_W - crop_w) // 2
    y = (VIEWPORT_H - crop_h) // 2

    trim_arg = ""
    if auto_trim:
        start = find_content_start(raw_video_path)
        if start > 0:
            trim_arg = f"-ss {start:.2f}"

    subprocess.run(
        f'ffmpeg -v error -y {trim_arg} -i "{raw_video_path}" '
        f'-vf "crop={crop_w}:{crop_h}:{x}:{y},scale=1080:1920:flags=lanczos" '
        f'-c:v libx264 -pix_fmt yuv420p "{out_mp4_path}"',
        shell=True, check=True,
    )
    return out_mp4_path


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/tgdrive/teldrive"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else TARGET_DURATION_DEFAULT
    raw_path = capture(url, out_dir, duration)
    final_path = os.path.join(out_dir, "captura_final_1080x1920.mp4")
    zoom_and_export(raw_path, final_path)
    print("VIDEO_SALVO:", final_path)
