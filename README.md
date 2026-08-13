# GitHub Reels Generator

**Cola o link de um repositório do GitHub, sai um reel vertical pronto pra postar — roteiro escrito por IA, captura de tela automática, avatar clonado, legenda karaokê e música de fundo.**

![Tela inicial](docs/screenshots/01_tela_inicial.png)

## Por quê

Divulgar projetos open source em reels/shorts é repetitivo: ler o README, escrever um roteiro que não pareça anúncio, gravar a tela rolando o repositório, sincronizar a narração com a captura, cortar, legendar. Essa dashboard automatiza tudo isso menos a parte que só um humano pode fazer — gravar a própria voz/rosto (clonados via avatar) lendo o roteiro. O resto (ler o repo, escrever, gravar tela, compor vídeo, gerar legenda + copy pro post) roda sozinho.

Dois modos:

- **Gerar novo** — cola o link do repo, a IA lê a descrição + README e escreve um roteiro em PT-BR (ou outro idioma), enquanto já dispara a gravação automática da tela rolando o repositório em segundo plano.
- **Remodelar** — reaproveita um reel de referência já baixado (de qualquer conta), sobe só o avatar com um roteiro próprio e recompõe por cima, cobrindo a legenda original.

Em ambos, depois é só subir o vídeo do avatar (clonado num serviço tipo HeyGen), escolher a paleta de cores e música, e a composição final (círculo do avatar com moldura girando, cutaways em tela cheia, legenda estilo karaokê palavra-a-palavra, mixagem de áudio com loudnorm em dois passes) roda via ffmpeg no servidor.

## Features

- **Roteiro por IA** — Gemini ou Claude (você escolhe o motor a cada job, com fallback heurístico se os dois falharem), em PT-BR ou inglês, lendo descrição + README real do repositório via API do GitHub
- **Captura de tela automática** — Playwright rola a página do repositório sozinho (README, código, gráficos) e grava em 1080×1920
- **Avatar circular com moldura animada** — detecção de rosto (OpenCV) recorta e centraliza automaticamente o avatar clonado, com anel decorativo girando (`hue` rotation no ffmpeg) e cutaways em tela cheia nos momentos certos
- **Legenda estilo karaokê** — transcrição palavra-a-palavra (faster-whisper) vira legenda `.ass` com a palavra atual destacada em cor, sincronizada por timestamp
- **Paleta de cores customizável** — anel, borda giratória, fundo do texto e cor do destaque da legenda, gerados por job (`colorize.py`)
- **Música de fundo opcional** — mixada por baixo da voz sem re-balancear o volume já calibrado (loudnorm de dois passes, sem o "bombeamento" do modo adaptativo de passe único)
- **Legenda do post + DM automática** — depois do vídeo pronto, um segundo passe de IA já escreve o texto pronto pra colar no Instagram (mesmo padrão de formato aprovado manualmente) e a mensagem de DM automática
- **Abrir no editor (FreeCut)** — a partir do vídeo final, um botão gera um projeto do [FreeCut](https://github.com/walterlow/freecut) (editor de vídeo 100% no navegador, self-hosted) já com o círculo do avatar, a legenda e a música na posição exata da composição, pronto pra ajustar arrastando — ver [`freecut-integration/`](freecut-integration/)
- **Histórico** — todas as versões renderizadas de cada job, com as cores usadas em cada uma

## Como funciona (visão geral)

```
link do repo → roteiro (IA)  ──┐
                                 ├─ pipeline_remoto.py (ffmpeg) → vídeo final → legenda/DM (IA)
captura de tela (Playwright) ──┤
avatar (upload manual)       ──┘
```

A dashboard (Flask) guarda o estado de cada job em disco (`dashboard/jobs/<id>/state.json`) e dispara as etapas pesadas (captura de tela, composição ffmpeg, chamadas de IA) em threads separadas, com log em tempo real na própria página. `pipeline_remoto.py` é o núcleo: detecta o rosto no vídeo do avatar, cacheia a transcrição por avatar (`words_cache.json`) pra recompor rápido quando só a cor muda, e monta o filtro complexo do ffmpeg (fundo + avatar recortado em círculo + moldura + legenda `.ass` + música).

## Screenshots

**Tela inicial** — cola o link, escolhe idioma e motor de IA (Gemini ou Claude), acompanha os jobs recentes:

![Tela inicial](docs/screenshots/01_tela_inicial.png)

**Wizard de 7 etapas** (Análise → Roteiro → Captura de tela → Avatar → Cores → Música & render → Resultado) — cada fase confirma o que a IA leu antes de seguir:

![Análise do repositório](docs/screenshots/02_analise_repositorio.png)

**Modo Remodelar** — reaproveita um reel de referência, mostra o preview exato de como o avatar novo vai cobrir o original antes de compor:

![Remodelar - referência](docs/screenshots/03_remodelar_referencia.png)

**Slots do modo Remodelar** — status de roteiro/avatar por reel, upload direto pela dashboard (sem `scp`):

![Slots do Remodelar](docs/screenshots/04_tela_remodelar_slots.png)

**Histórico** — todos os vídeos já compostos, dos dois modos, com download e "abrir no editor" (FreeCut) direto do card:

![Histórico](docs/screenshots/05_historico.png)

## Tech Stack

- Python / Flask + gunicorn
- ffmpeg (composição, mixagem, ASS burn-in)
- faster-whisper (transcrição palavra-a-palavra)
- OpenCV (detecção de rosto)
- Playwright (captura de tela automática do repositório)
- Gemini / Claude (roteiro e legenda do post via IA, com fallback heurístico)
- [FreeCut](https://github.com/walterlow/freecut) (React + WebCodecs/WebGPU) para o editor visual opcional

## Setup

```bash
git clone https://github.com/leonardoborgesdev/github-reels-generator.git
cd github-reels-generator
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
```

Variáveis de ambiente (opcionais — sem elas, cai no fallback heurístico de roteiro):

```bash
export GEMINI_API_KEY=your_key_here
```

Rodar a dashboard:

```bash
cd dashboard
../venv/bin/python3 app.py          # dev, porta 8099
# ou, em produção:
../venv/bin/gunicorn --workers 1 --threads 8 --timeout 600 --bind 0.0.0.0:8099 app:app
```

Composição direta pela linha de comando (sem a dashboard):

```bash
venv/bin/python3 pipeline_remoto.py fundo.mp4 avatar.mp4 nome_saida \
  --caption-color "#9333EA" --mode capture
```

### Editor visual (FreeCut) — opcional

O botão "Abrir no editor" da tela de resultado depende de uma instância própria do FreeCut rodando ao lado (porta 8100 por padrão). Ele não é vendorizado neste repositório por ser um projeto de terceiros grande (~50 MB de código-fonte + `node_modules`) — instruções e o patch de 1 linha usados (idioma padrão PT-BR) estão em [`freecut-integration/README.md`](freecut-integration/README.md).

## Limitações conhecidas

- O projeto gerado pro FreeCut reproduz a posição exata do círculo do avatar, da caixa grande usada nos cutaways, da moldura e da música — mas a legenda vira blocos de texto por trecho (mesmo agrupamento e tempo do `.ass` original), sem o destaque palavra-a-palavra em cores alternadas do karaokê original. Reproduzir isso exigiria mapear a API de animação por-caractere do FreeCut, fora do escopo desta integração.
- A importação do projeto no FreeCut exige 2 cliques manuais do usuário (selecionar o arquivo `.freecut.zip` e escolher uma pasta de destino) — é uma exigência de segurança do navegador (File System Access API) e não pode ser pulada por automação.

## Debugging notes: captura de tela travando/pulando ("trava e desce tudo de uma vez")

Esse bug consumiu ~10 rodadas de correção (12-13/08/2026) até a causa raiz de verdade ser encontrada. Documentado aqui pra nunca mais perder esse contexto.

**Sintoma**: em repositórios com README pesado (imagens grandes, avatares de contribuidor), a gravação de tela ficava parada por um bom tempo e depois "descia tudo de uma vez" — ou, em casos piores, ficava rolando por minutos sem nunca terminar.

**Causa raiz real**: o Chromium aplica um throttling interno de timers (`setTimeout`/`setInterval`, e antes disso também `requestAnimationFrame`) em páginas que ele classifica como "em segundo plano" — o que uma página headless do Playwright sempre é, do ponto de vista do agendador interno do Chromium, **mesmo que a própria página nunca tenha trocado de aba de verdade**. Um override de `document.hidden`/`document.visibilityState` via JavaScript (tentado numa das rodadas) **não resolve** isso, porque essa decisão de throttling acontece num nível de agendamento mais interno do navegador, abaixo do que a JS da página consegue enxergar ou controlar.

O padrão observado batia exatamente com isso: a rolagem avançava normal por um tempo (tipicamente ~1-2 minutos), depois praticamente congelava, só voltando a se mexer esporadicamente até estourar qualquer teto de tempo definido.

**Diagnóstico que confirmou** (e descartou hipóteses erradas no caminho — inclusive "altura da página crescendo dinamicamente", verificado e refutado com um teste direto de `document.body.scrollHeight` parado por 10s sem nenhuma rolagem, que mostrou o valor **completamente estável**):

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':540,'height':960}, device_scale_factor=2)
    page.goto(URL, wait_until='load', timeout=20000)
    for i in range(8):
        print(page.evaluate('document.body.scrollHeight'))
        time.sleep(1.5)
```

**Fix definitivo** (em `scroll_capture.py`, na chamada `p.chromium.launch()`):

```python
browser = p.chromium.launch(
    headless=True,
    args=[
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-ipc-flooding-protection",
    ],
)
```

Essas são flags padrão, bem documentadas, usadas justamente em automação/testes headless pra desligar esse comportamento de "economia de energia" do Chromium que não faz sentido nesse contexto. Depois desse fix, capturas que antes travavam por 4+ minutos (batendo em tetos de segurança) passaram a completar em ~80s, sem nenhum salto detectável.

**Camadas de proteção que ficaram no código, mesmo já não sendo a causa raiz** (defesa em profundidade, úteis contra outros tipos de trava real de thread principal — decode de imagem pesada, etc):
- Espera de `img.decode()` com `loading="eager"` forçado antes de começar a rolagem cronometrada.
- Passada de "aquecimento" (rola até o fim e volta, silenciosa, fora do vídeo final) pra forçar o layout/pintura de cada trecho da página uma vez antes da gravação de verdade.
- Rolagem **sem prazo fixo**: em vez de forçar terminar numa duração exata (o que gerava "corrida pra recuperar atraso" = salto visível), rola num ritmo humano constante e deixa levar o tempo real que precisar — a composição final já corta/estica pra bater com a duração real da fala do avatar, então não há necessidade de um tempo de captura exato. Só com um teto de segurança (1.3x a duração alvo) pra nunca rodar indefinidamente.
- Exportação final em CFR (`-vsync cfr -r 25`, não `-vsync vfr`) — um vídeo de frame rate variável pode reproduzir corretamente quando extraído frame a frame via ffmpeg, mas trava/pula na reprodução real do `<video>` do navegador mesmo com os timestamps tecnicamente corretos.
- Corte de início preciso por marcador visual (flash magenta full-screen de 700ms, detectado por análise de cor média do frame) em vez de medir tempo de parede em Python, que podia ficar dessincronizado do timeline real do vídeo gravado.

## License

MIT — see [LICENSE](LICENSE).
