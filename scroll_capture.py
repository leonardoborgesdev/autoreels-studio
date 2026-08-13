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
# evita o flash branco entre abrir a aba e o CSS do site carregar.
# TAMBEM forca a pagina a se achar sempre visivel/em primeiro plano -
# o Chromium aplica "Intensive Timer Throttling" (poda tanto o
# requestAnimationFrame QUANTO setTimeout/setInterval) em paginas que ele
# considera em segundo plano, e uma pagina headless do Playwright conta
# como "hidden" mesmo sem trocar de aba de verdade. Foi essa a causa real
# dos travamentos que sobreviveram a troca de rAF por setTimeout - as duas
# APIs sofrem do mesmo throttling baseado na Page Visibility API. Sobrescrever
# document.hidden/visibilityState (e bloquear o evento visibilitychange)
# faz o navegador nunca aplicar esse throttling, na raiz do problema.
DARK_INIT_SCRIPT = """
(() => {
    const s = document.createElement('style');
    s.textContent = 'html,body{background:#0d1117 !important;}';
    document.documentElement.appendChild(s);

    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    const blockVisibilityEvent = (e) => { e.stopImmediatePropagation(); };
    document.addEventListener('visibilitychange', blockVisibilityEvent, true);
})();
"""


def capture(url: str, out_dir: str, target_duration: float = TARGET_DURATION_DEFAULT):
    with sync_playwright() as p:
        # flags do Chromium feitas especificamente pra desligar o throttling
        # de timer/renderer que o navegador aplica em paginas que ele
        # considera em segundo plano - override de document.hidden via JS
        # nao bloqueia esse throttling porque ele e decidido num nivel mais
        # interno de agendamento do proprio Chromium, nao pela JS da pagina.
        # Foi isso que causava o padrao "roda normal por um tempo, depois
        # quase congela" mesmo depois de todas as outras correcoes.
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
            ],
        )
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

        # Espera as imagens da pagina (screenshots pesados em READMEs, etc)
        # terminarem de baixar E DECODIFICAR antes de comecar a rolagem
        # cronometrada. A versao anterior so esperava o evento 'load' (rede),
        # que dispara ANTES do decode da imagem terminar - e imagens com
        # loading="lazy" (comum em screenshot de README) nem comecam a
        # carregar até quase entrarem na viewport, entao essa espera inicial
        # simplesmente nao pegava elas. Aqui: forca loading="eager" em TODAS
        # as imagens da pagina (nao só as visiveis) e usa img.decode(), que
        # so resolve depois do decode de verdade terminar - decode de uma
        # imagem grande trava a thread principal por um tempo real (para
        # ate o loop de scroll), e como o scroll e medido por tempo de
        # RELOGIO, esse trecho parado vira um salto pra frente quando a
        # thread libera ("trava no meio e desce tudo de uma vez" - foi
        # esse o padrao confirmado via diff de frames: o salto acontecia
        # bem na altura dos screenshots grandes embutidos no README).
        # Timeout generoso pra nao travar pra sempre numa imagem quebrada.
        try:
            page.evaluate("""
                () => {
                    Array.from(document.images).forEach(img => { img.loading = 'eager'; });
                    return Promise.race([
                        Promise.all(
                            Array.from(document.images).map(img =>
                                (img.decode ? img.decode() : Promise.resolve()).catch(() => {})
                            )
                        ),
                        new Promise(resolve => setTimeout(resolve, 8000)),
                    ]);
                }
            """, timeout=10000)
        except Exception:
            pass  # timeout do playwright ou pagina sem imagens - segue o baile

        # Passada de "aquecimento": rola pra baixo em saltos ANTES de comecar
        # a gravacao cronometrada, forcando o navegador a fazer o layout/
        # pintura de cada trecho da pagina uma vez (decode() sozinho nao
        # cobre isso - o CUSTO REAL que trava a thread principal e o
        # layout+paint da imagem grande quando ela entra na viewport pela
        # primeira vez, nao so o decode dos bytes). Depois volta pro topo -
        # na passada real (gravada e cronometrada), o navegador ja tem tudo
        # em cache de pintura e nao trava de novo. Silencioso, fora da
        # gravacao (o context.new_page ainda nao tem video anexado aqui? -
        # tem, mas ninguem assiste esse trecho no video final porque o
        # recorte final e cortado a partir do inicio da rolagem cronometrada).
        try:
            page.evaluate("""
                async (viewportH) => {
                    const totalH = document.body.scrollHeight;
                    // passos fixos (10) podiam pular mais que uma tela inteira em
                    // paginas longas - uma imagem inteira ficava entre dois
                    // pulos e NUNCA entrava na viewport durante o aquecimento,
                    // entao seguia sem layout/paint feitos e travava a thread
                    // do mesmo jeito na passada real. Calculado pra cada passo
                    // avancar no maximo 70% de uma tela de viewport, garantindo
                    // sobreposicao e cobertura completa da pagina. Teto de 60
                    // passos pra pagina com altura instavel/crescente nao
                    // travar esse loop pra sempre.
                    const steps = Math.min(60, Math.max(10, Math.ceil(totalH / (viewportH * 0.7))));
                    for (let i = 1; i <= steps; i++) {
                        window.scrollTo(0, (totalH * i) / steps);
                        await new Promise(r => setTimeout(r, 90));
                    }
                    window.scrollTo(0, 0);
                    await new Promise(r => setTimeout(r, 200));
                }
            """, VIEWPORT_H)
        except Exception:
            # timeout do playwright (pagina com altura instavel demais) -
            # segue sem aquecimento completo em vez de travar o job inteiro
            page.evaluate("window.scrollTo(0, 0)")
        page.evaluate("""
            () => new Promise(resolve => {
                const flash = document.createElement('div');
                flash.style.cssText = 'position:fixed;inset:0;background:#ff00ff;z-index:2147483647;';
                document.body.appendChild(flash);
                setTimeout(() => { flash.remove(); resolve(); }, 700);
            })
        """)

        total_height = page.evaluate("document.body.scrollHeight")

        # SEM PRAZO: rola num ritmo humano constante (px/s calculado a partir
        # da duracao alvo) e deixa levar o tempo real que precisar - nada de
        # "correr" pra bater um horario. A composicao final DEPOIS corta/
        # estica o fundo pra bater com a duracao real da fala do avatar
        # (trim + tpad no pipeline_remoto.py), entao nao ha necessidade de
        # forcar a captura bruta a terminar num tempo exato - essa pressao
        # de prazo era JUSTAMENTE o que causava o "corre pra recuperar
        # atraso" (mesmo com limites de velocidade de catch-up) sempre que
        # a thread principal travava um instante (imagem pesada, avatares de
        # contribuidor carregando via API, etc). Sem prazo, uma trava real
        # so atrasa o fim da gravacao (fica mais alguns segundos rolando no
        # ritmo normal) em vez de aparecer como um salto na gravacao.
        FOOTER_PAUSE_SEC = 3.0
        normal_speed_px_s = (page.evaluate("document.body.scrollHeight") or 1) / max(
            target_duration * 0.78, 1
        )

        # teto de seguranca: sem prazo fixo, mas nao pode rodar pra sempre -
        # se a altura da pagina cresce dinamicamente mais rapido do que o
        # scroll avanca (lazy-load continuo, embed com conteudo crescente,
        # etc), o loop "persegue" um alvo que nunca para de crescer. Um teto
        # generoso (4x a duracao alvo) ainda deixa muito mais folga que o
        # prazo rigido de antes, mas evita rodar indefinidamente e travar a
        # fila de jobs - foi exatamente isso que aconteceu na 1a tentativa
        # sem prazo (webm cresceu pra 21MB+ e passou de 5 minutos rodando).
        max_scroll_ms = int(target_duration * 1.3 * 1000)
        page.evaluate(
            """
            async (args) => {
                const [speedPxPerSec, maxMs] = args;
                const t0 = performance.now();
                let lastT = t0;
                await new Promise((resolve) => {
                    function step() {
                        const now = performance.now();
                        const dt = (now - lastT) / 1000;
                        lastT = now;
                        const totalH = document.body.scrollHeight;
                        const newY = Math.min(
                            (window.scrollY || 0) + speedPxPerSec * dt,
                            totalH
                        );
                        window.scrollTo(0, newY);
                        if (newY < totalH && (now - t0) < maxMs) {
                            setTimeout(step, 16);
                        } else {
                            resolve();
                        }
                    }
                    setTimeout(step, 16);
                });
            }
            """,
            [normal_speed_px_s, max_scroll_ms],
        )

        # garante que chegou EXATAMENTE no fim
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # pausa final curta e fixa parada no rodape (nao proporcional a
        # nenhum orcamento de tempo - a duracao total real da gravacao
        # agora e o que for, e o corte final se ajusta a fala do avatar
        # depois de qualquer forma)
        time.sleep(FOOTER_PAUSE_SEC)
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


def find_marker_frame(video_path: str, max_check: float = 15.0, step: float = 0.12) -> float:
    """Acha o instante exato (com pequena margem depois) do flash magenta
    solido injetado logo apos a passada de aquecimento - muito mais preciso
    que medir tempo de parede em Python, que podia ficar dessincronizado do
    timeline real do video gravado."""
    tmp_dir = os.path.join(os.path.dirname(video_path) or ".", "_tmp_marker")
    os.makedirs(tmp_dir, exist_ok=True)
    t = 0.0
    found = None
    while t < max_check:
        out = os.path.join(tmp_dir, f"m_{t:.1f}.png")
        subprocess.run(
            f'ffmpeg -v error -y -ss {t} -i "{video_path}" -frames:v 1 "{out}"',
            shell=True,
        )
        if os.path.exists(out):
            arr = np.array(Image.open(out).convert("RGB"))
            os.remove(out)
            r, g, b = arr[..., 0].mean(), arr[..., 1].mean(), arr[..., 2].mean()
            # magenta solido: vermelho e azul altos, verde baixo
            if r > 180 and b > 180 and g < 80:
                found = t
                break
        t += step
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    if found is None:
        return 0.0
    return found + 0.9  # margem depois do flash (dura 700ms) pra garantir que ja sumiu de vez


def zoom_and_export(raw_video_path: str, out_mp4_path: str, zoom: float = 1.0, auto_trim: bool = True, min_trim_start: float = 0.0):
    """Amplia o frame bruto (540x960, layout mobile do GitHub) pra 1080x1920
    com lanczos - fica nitido e sem cortar nada (zoom=1.0 = so upscale limpo).
    Se quiser um zoom real de destaque, usar zoom>1 SOMENTE em pontos
    especificos da pagina (nao no video inteiro, senao corta conteudo
    nas bordas - foi testado e fica ruim).
    auto_trim: corta a tela vazia do inicio (antes do site carregar de vez).
    min_trim_start: corte minimo explicito (ex: duracao da passada de
    aquecimento que fica gravada no bruto ANTES da rolagem cronometrada
    comecar) - usado quando maior que o achado por find_content_start."""
    crop_w = int(VIEWPORT_W / zoom)
    crop_h = int(VIEWPORT_H / zoom)
    x = (VIEWPORT_W - crop_w) // 2
    y = (VIEWPORT_H - crop_h) // 2

    start = min_trim_start
    if auto_trim:
        marker = find_marker_frame(raw_video_path)
        if marker > 0:
            start = max(start, marker)
        else:
            # marcador nao encontrado (raro, mas ja aconteceu por
            # desalinhamento de amostragem) - cai pro heuristico antigo de
            # brilho em vez de comecar do zero (tela vazia de carregamento)
            start = max(start, find_content_start(raw_video_path))
    trim_arg = f"-ss {start:.2f}" if start > 0 else ""

    # -vsync cfr -r 25: reamostra o webm bruto do Playwright (que tem
    # timestamps VARIAVEIS batendo com o momento real de scroll) pra uma
    # saida de frame rate CONSTANTE, duplicando/descartando frames com base
    # no tempo real de cada um - preserva o movimento correto sem gerar um
    # mp4 de frame rate variavel. Testamos -vsync vfr antes: os frames
    # extraidos via ffmpeg pareciam lisos (o ffmpeg reamostra VFR certinho),
    # mas o PLAYER de video do navegador (elemento <video> do Chrome) nao
    # lida bem com VFR - trava e pula na reproducao real mesmo com os
    # timestamps corretos por baixo. CFR e universalmente bem suportado.
    subprocess.run(
        f'ffmpeg -v error -y {trim_arg} -i "{raw_video_path}" '
        f'-vf "crop={crop_w}:{crop_h}:{x}:{y},scale=1080:1920:flags=lanczos" '
        f'-vsync cfr -r 25 -c:v libx264 -pix_fmt yuv420p "{out_mp4_path}"',
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
