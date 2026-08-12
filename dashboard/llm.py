"""
Geração de roteiro do reel a partir de um repositório GitHub (ou de um tema
livre, no modo Remodelar).

Dois motores disponíveis, escolhidos por job (não um substituindo o outro):
  - "gemini": Google Gemini via API HTTP (chave já usada em outros projetos
    do VPS, /opt/vexa-lite/brain.env).
  - "claude": Claude Code CLI em modo headless (`claude -p "..." --output-format
    text`), já autenticado no VPS pela assinatura (sem precisar de API key).

Dois idiomas disponíveis: "pt" (PT-BR, padrão) e "en" (English).

Se o motor escolhido falhar (sem chave, erro de rede, timeout etc.), cai num
gerador heurístico simples baseado no README (fallback, secundário conforme
combinado).
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
CLAUDE_BIN = "/usr/bin/claude"

ENGINES = ("gemini", "claude")
IDIOMAS = ("pt", "en")


def _load_env_file(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _gemini_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    # reaproveita a chave já usada pelo vexa-lite (brain.env) neste VPS
    env = _load_env_file("/opt/vexa-lite/brain.env")
    return env.get("GEMINI_API_KEY")


def _strip_markdown(text, max_chars=6000):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"[*_`>#-]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


# ---------------- prompts (um por idioma, mesma estrutura) ----------------

PROMPT_TEMPLATE_PT = """Você escreve roteiros de reels de Instagram sobre repositórios open source do GitHub, no estilo dos canais @github_unpacked e @rammcodes_.

Escreva em PORTUGUÊS DO BRASIL, tom de brasileiro natural falando de boca, sem parecer texto de IA, sem emojis, sem markdown, só o texto corrido que vai ser narrado em voz.

Regras obrigatórias:
- Primeiras 1-2 frases: hook chamativo que prende a atenção nos 2 primeiros segundos (pode comparar com uma ferramenta paga conhecida, um problema caro/chato que o repo resolve, ou um número/fato surpreendente). Use tensão quando fizer sentido (ex.: "para de gastar dinheiro com...", "você tá perdendo tempo fazendo...").
- Depois explica o que o repositório faz e por que isso importa, em linguagem simples (não técnica demais).
- Corpo com 2 a 3 parágrafos curtos (frases curtas, fáceis de narrar).
- Termina SEMPRE com um callback de fechamento curto que retoma o hook/gancho do início, seguido de uma chamada pedindo pra comentar a palavra "{gatilho}" (em maiúsculas) pra receber o link do repositório. Exemplo de fechamento: "Comenta {gatilho} que eu te mando o link do repositório."
- Duração alvo: entre 35 e 50 segundos falado (aproximadamente 90 a 130 palavras).
- NÃO use hashtags, NÃO use markdown, NÃO numere parágrafos, NÃO use aspas ao redor do texto todo.
- Não invente números/funcionalidades que não estão na descrição/README abaixo.

Repositório: {full_name}
Descrição oficial: {description}
Estrelas no GitHub: {stars}
Linguagem principal: {language}

Trecho do README:
{readme}

Escreva só o roteiro final, pronto pra narrar, nada mais."""

PROMPT_TEMPLATE_EN = """You write Instagram reel scripts about open source GitHub repositories, in the style of channels like @github_unpacked and @rammcodes_.

Write in ENGLISH, natural spoken tone, doesn't sound like AI-written text, no emojis, no markdown, just the flowing text that will be narrated out loud.

Mandatory rules:
- First 1-2 sentences: an attention-grabbing hook for the first 2 seconds (can compare to a well-known paid tool, an expensive/annoying problem the repo solves, or a surprising number/fact). Use tension when it makes sense (e.g. "stop paying for...", "you're wasting time doing...").
- Then explain what the repository does and why it matters, in simple language (not overly technical).
- Body with 2 to 3 short paragraphs (short sentences, easy to narrate).
- ALWAYS end with a short closing callback that ties back to the opening hook, followed by a call asking people to comment the word "{gatilho}" (in uppercase) to receive the repository link. Example closing line: "Comment {gatilho} and I'll send you the repo link."
- Target length: 35 to 50 seconds spoken (roughly 90 to 130 words).
- Do NOT use hashtags, do NOT use markdown, do NOT number paragraphs, do NOT wrap the whole text in quotes.
- Don't invent numbers/features that aren't in the description/README below.

Repository: {full_name}
Official description: {description}
GitHub stars: {stars}
Main language: {language}

README excerpt:
{readme}

Write only the final script, ready to narrate, nothing else."""

PROMPT_TEMPLATES = {"pt": PROMPT_TEMPLATE_PT, "en": PROMPT_TEMPLATE_EN}


def _gatilho_de(full_name):
    return re.sub(r"[^A-Za-z0-9]", "", full_name.split("/")[-1]).upper() or "REPO"


def build_prompt(full_name, description, readme_text, stars=None, language=None, idioma="pt"):
    idioma = idioma if idioma in IDIOMAS else "pt"
    gatilho = _gatilho_de(full_name)
    template = PROMPT_TEMPLATES[idioma]
    return template.format(
        gatilho=gatilho,
        full_name=full_name,
        description=description or "(sem descrição)" if idioma == "pt" else (description or "(no description)"),
        stars=stars if stars is not None else "?",
        language=language or "?",
        readme=_strip_markdown(readme_text, 5000) or ("(sem README)" if idioma == "pt" else "(no README)"),
    ), gatilho


def gerar_roteiro_fallback(full_name, description, readme_text, gatilho, idioma="pt"):
    readme_clean = _strip_markdown(readme_text, 800)
    primeiro_paragrafo = ""
    for bloco in readme_clean.split("\n\n"):
        bloco = bloco.strip()
        if len(bloco) > 40:
            primeiro_paragrafo = bloco
            break

    if idioma == "en":
        if not primeiro_paragrafo:
            primeiro_paragrafo = description or "This repository solves a problem a lot of people run into."
        partes = [
            "Stop paying for a tool that does this... there's a free open source repository on GitHub that solves the exact same problem.",
            f"{primeiro_paragrafo.strip().rstrip('.')}.",
            "It's open source, so you can use it, study the code, and even adapt it however you need, no license or subscription required.",
            f"Comment {gatilho} and I'll send you the repo link.",
        ]
    else:
        if not primeiro_paragrafo:
            primeiro_paragrafo = description or "Esse repositório resolve um problema que muita gente enfrenta."
        partes = [
            "Para de gastar dinheiro com ferramenta paga pra isso... existe um repositório open source no GitHub que resolve o mesmo problema, de graça.",
            f"{primeiro_paragrafo.strip().rstrip('.')}.",
            "É open source, então dá pra usar, estudar o código e até adaptar do jeito que você precisar, sem depender de licença nem mensalidade.",
            f"Comenta {gatilho} que eu te mando o link do repositório.",
        ]
    return "\n\n".join(partes)


def _call_gemini(prompt):
    key = _gemini_key()
    if not key:
        raise RuntimeError("sem_chave_gemini")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 1536,
            # gemini-2.5-flash faz "thinking" por padrão, o que consome o
            # budget de saida e corta o roteiro no meio. Desliga aqui,
            # nao precisamos de raciocinio pra escrever um roteiro curto.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read())
    texto = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    texto = texto.strip('"').strip()
    if not texto:
        raise ValueError("resposta vazia do Gemini")
    return texto


def _call_claude(prompt):
    """Chama o Claude Code CLI em modo headless (já autenticado no VPS pela
    assinatura, sem precisar de API key). Saída em texto puro."""
    if not os.path.exists(CLAUDE_BIN):
        raise RuntimeError("claude_cli_nao_encontrado")
    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude_cli_erro_{proc.returncode}:{(proc.stderr or '').strip()[:200]}")
    texto = (proc.stdout or "").strip().strip('"').strip()
    if not texto:
        raise ValueError("resposta vazia do Claude")
    return texto


def gerar_roteiro(full_name, description, readme_text, stars=None, language=None, idioma="pt", engine="gemini"):
    """Retorna (texto_roteiro, origem) onde origem é 'gemini', 'claude' ou
    'fallback_heuristico:<motivo>'."""
    idioma = idioma if idioma in IDIOMAS else "pt"
    engine = engine if engine in ENGINES else "gemini"
    gatilho = _gatilho_de(full_name)
    prompt, _ = build_prompt(full_name, description, readme_text, stars, language, idioma)

    try:
        if engine == "claude":
            texto = _call_claude(prompt)
        else:
            texto = _call_gemini(prompt)
        return texto, engine
    except Exception as e:  # noqa: BLE001
        return (
            gerar_roteiro_fallback(full_name, description, readme_text, gatilho, idioma),
            f"fallback_heuristico:erro_{engine}:{e}",
        )


def fetch_repo_info(repo_url):
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", repo_url.strip())
    if not m:
        raise ValueError("Link do GitHub inválido. Use algo como https://github.com/owner/repo")
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]

    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    req = urllib.request.Request(api_url, headers={"User-Agent": "canal-github-reels", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            info = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ValueError(f"Não achei o repositório {owner}/{repo} no GitHub (HTTP {e.code}).")

    readme_text = ""
    try:
        readme_req = urllib.request.Request(
            f"{api_url}/readme",
            headers={"User-Agent": "canal-github-reels", "Accept": "application/vnd.github.raw"},
        )
        with urllib.request.urlopen(readme_req, timeout=20) as resp:
            readme_text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        readme_text = ""

    license_info = info.get("license") or {}
    return {
        "owner": owner,
        "repo": repo,
        "full_name": info.get("full_name", f"{owner}/{repo}"),
        "description": info.get("description") or "",
        "stars": info.get("stargazers_count"),
        "forks": info.get("forks_count"),
        "language": info.get("language"),
        "license": license_info.get("name") or license_info.get("spdx_id"),
        "topics": info.get("topics") or [],
        "html_url": info.get("html_url", f"https://github.com/{owner}/{repo}"),
        "readme": readme_text,
    }


# ---------------- legenda do post + DM automática (2 passos) ----------------
#
# Mesmo motor (Gemini/Claude) usado pro roteiro, reaproveitado aqui pra gerar,
# junto com o vídeo, a legenda do Instagram + o fluxo de DM (padrão já usado
# manualmente pros primeiros vídeos do canal, ver PROJETO_CANAL_REELS_GITHUB/
# ENTREGA_FINAL/legendas_e_dm/*.txt). Padrão atual (ajustado a pedido do
# Henrique): a chamada "Comenta {gatilho}" aparece SÓ na abertura da legenda
# (junto com o hook), não repete mais perto do final.

# marcador literal que o modelo deve preservar sem tentar adivinhar a URL —
# substituímos pelo link real do repositório depois, em código, pra não
# arriscar o LLM alucinar uma URL errada.
_URL_PLACEHOLDER = "{URL_DO_REPO}"

LEGENDA_DM_FORMATO_REF = """========================================
LEGENDA DO POST (Instagram) — NoSignups
========================================

Todo site quer seu e-mail antes de te deixar trabalhar \U0001f624

Achei um repositório que nasceu porque alguém cansou disso: mais de 200 ferramentas open source que funcionam na hora, direto no navegador. Sem criar conta, sem confirmar e-mail, sem parede de cadastro no meio do caminho.

Você entra, usa e sai.

→ 2.2 mil estrelas no GitHub
→ licença GPL, mantido pela comunidade
→ zero cadastro, sempre

Segue @automatrix.ia pra mais achados open source de IA e dev \U0001f48e

#opensource #github #devbr #programacao #tecnologia


========================================
DM AUTOMÁTICA — FLUXO EM 2 PASSOS (quando comentar "NOSIGNUPS")
========================================

PASSO 1 — mensagem automática assim que a pessoa comenta a palavra-chave:

E aí, peguei o link do NoSignups pra você! Só preciso que você digite a palavra NOSIGNUPS aqui no chat.

---

PASSO 2 — quando a pessoa responde com a palavra "NOSIGNUPS" no chat, envia o botão com o link direto:

Prontinho \U0001f680 Aqui está o site, direto ao ponto:
[BOTÃO: Pegar as ferramentas → https://fucksignups.com]

Mais de 200 ferramentas prontas pra usar, sem conta e sem e-mail. Repositório aberto no GitHub também, se quiser contribuir."""

PROMPT_LEGENDA_DM_PT = """Você escreve a legenda do post de Instagram + o fluxo de DM automática (2 passos) pra reels de um canal sobre repositórios open source do GitHub (@automatrix.ia).

Aqui está um exemplo real já aprovado, pra você copiar EXATAMENTE a estrutura, formatação e tom (o conteúdo do exemplo é de outro repositório, é só referência de formato):

{formato_ref}

Agora escreva o mesmo tipo de bloco (legenda + DM), no MESMO formato acima (mesmos separadores "========================================", mesmos títulos, mesma ordem de seções), só que sobre este repositório:

Nome pra usar em {{NOME}}: {nome}
Descrição oficial: {description}
Estrelas no GitHub: {stars}
Linguagem principal: {language}
Palavra-chave/gatilho a usar (SEMPRE em maiúsculas, exatamente como está aqui): {gatilho}

Roteiro do vídeo já narrado (use pra manter o mesmo tom e os mesmos fatos, não invente nada que não esteja aqui ou na descrição/README):
{roteiro}

Trecho do README (contexto extra, pode não existir):
{readme}

REGRAS OBRIGATÓRIAS DE FORMATO (diferente do exemplo em um ponto — leia com atenção):
- A legenda abre com uma chamada pra comentar a palavra-chave JUNTO com o hook, tipo: "Comenta {gatilho} que eu te mando o link — {{hook curto de 1 linha}}." — isso fica na primeira ou segunda linha da legenda.
- Essa chamada pra comentar a palavra-chave aparece SÓ UMA VEZ, na abertura. NÃO repita "Comenta {gatilho}..." de novo perto do final da legenda (diferente do exemplo de referência, que repetia — não repita aqui).
- Depois do hook/abertura, um parágrafo curto explicando o que o repositório faz e por que importa (linguagem simples, não técnica demais).
- 3 bullets com "→ " (seta) destacando dados/features reais (número de estrelas, licença, algo técnico simples).
- Linha "Segue @automatrix.ia pra mais achados open source de IA e dev \U0001f48e".
- Linha de hashtags: #opensource #github + 1-2 hashtags relevantes ao tema/linguagem + #devbr #programacao #tecnologia.
- Depois da legenda, o bloco de DM em 2 passos, EXATAMENTE como no exemplo (títulos "PASSO 1" e "PASSO 2", separador "---").
- PASSO 1 sempre menciona o nome do repositório e pede pra digitar a palavra-chave.
- PASSO 2 usa a palavra "Prontinho \U0001f680" e o formato "[BOTÃO: Pegar o repositório → {url_placeholder}]" — IMPORTANTE: use o texto literal "{url_placeholder}" ali, EXATAMENTE assim entre chaves, NÃO tente adivinhar ou inventar a URL, isso é preenchido depois automaticamente.
- Termina com 1-2 frases de instalação rápida, baseada na descrição/README/roteiro, sem inventar comandos que não fazem sentido pro projeto.
- NÃO use markdown (sem ** ou #, exceto na linha de hashtags), escreva só o texto final.

Escreva só o bloco final (legenda + DM), pronto pra usar, nada de comentário seu antes ou depois."""

PROMPT_LEGENDA_DM_EN = """You write the Instagram post caption + the 2-step automatic DM flow for reels from a channel about open source GitHub repositories (@automatrix.ia).

Here is a real approved example, copy its structure, formatting and tone EXACTLY (the example's content is about a different repo, it's just a format reference):

{formato_ref}

Now write the same kind of block (caption + DM), in the SAME format above (same "========================================" separators, same headings, same section order), but about this repository:

Name to use as {{NAME}}: {nome}
Official description: {description}
GitHub stars: {stars}
Main language: {language}
Keyword/trigger to use (ALWAYS uppercase, exactly as given here): {gatilho}

Video script already narrated (use it to keep the same tone and facts, don't invent anything not in here or in the description/README):
{roteiro}

README excerpt (extra context, may not exist):
{readme}

MANDATORY FORMAT RULES (different from the example in one point — read carefully):
- The caption opens with a call to comment the keyword TOGETHER with the hook, like: "Comment {gatilho} and I'll send you the link — {{short 1-line hook}}." — this goes in the first or second line of the caption.
- This call to comment the keyword appears ONLY ONCE, in the opening. Do NOT repeat "Comment {gatilho}..." again near the end of the caption (unlike the reference example, which repeated it — don't repeat here).
- After the hook/opening, a short paragraph explaining what the repository does and why it matters (simple language, not overly technical).
- 3 bullets with "→ " (arrow) highlighting real data/features (star count, license, a simple technical fact).
- Line "Follow @automatrix.ia for more open source AI and dev finds \U0001f48e".
- Hashtags line: #opensource #github + 1-2 hashtags relevant to the topic/language + #devbr #programacao #tecnologia.
- After the caption, the 2-step DM block, EXACTLY like the example ("STEP 1" / "STEP 2" headings, "---" separator).
- STEP 1 always mentions the repo name and asks to type the keyword.
- STEP 2 uses "Here you go \U0001f680" and the format "[BUTTON: Get the repository → {url_placeholder}]" — IMPORTANT: use the literal text "{url_placeholder}" there, EXACTLY like that in curly braces, do NOT try to guess or invent the URL, it gets filled in automatically afterwards.
- End with 1-2 quick install sentences, based on the description/README/script, without inventing commands that don't make sense for the project.
- Do NOT use markdown (no ** or #, except in the hashtags line), write just the final text.

Write only the final block (caption + DM), ready to use, no comments before or after."""

PROMPT_LEGENDA_DM_TEMPLATES = {"pt": PROMPT_LEGENDA_DM_PT, "en": PROMPT_LEGENDA_DM_EN}


def build_prompt_legenda_dm(nome, description, roteiro, readme_text, gatilho, stars=None, language=None, idioma="pt"):
    idioma = idioma if idioma in IDIOMAS else "pt"
    template = PROMPT_LEGENDA_DM_TEMPLATES[idioma]
    return template.format(
        formato_ref=LEGENDA_DM_FORMATO_REF,
        nome=nome,
        description=description or ("(sem descrição)" if idioma == "pt" else "(no description)"),
        stars=stars if stars is not None else "?",
        language=language or "?",
        gatilho=gatilho,
        roteiro=(roteiro or "").strip()[:2500],
        readme=_strip_markdown(readme_text, 2000) or ("(sem README)" if idioma == "pt" else "(no README)"),
        url_placeholder=_URL_PLACEHOLDER,
    )


def gerar_legenda_dm_fallback(nome, description, roteiro, gatilho, idioma="pt"):
    """Heurística simples, sem LLM, seguindo o mesmo formato — usada só se o
    motor configurado falhar (mesma filosofia do fallback de roteiro)."""
    roteiro = (roteiro or "").strip()
    primeira_frase = ""
    for frase in re.split(r"(?<=[.!?])\s+", roteiro):
        frase = frase.strip()
        if len(frase) > 15:
            primeira_frase = frase.rstrip(".!?")
            break
    if not primeira_frase:
        primeira_frase = description or (nome if idioma == "en" else f"Isso aqui é o {nome}")

    corpo = description or (roteiro[:220] if roteiro else "")

    if idioma == "en":
        texto = f"""========================================
LEGENDA DO POST (Instagram) — {nome}
========================================

Comment {gatilho} and I'll send you the link — {primeira_frase}.

{corpo}

→ open source, free to use
→ active repository on GitHub
→ easy to set up

Follow @automatrix.ia for more open source AI and dev finds \U0001f48e

#opensource #github #devbr #programacao #tecnologia


========================================
DM AUTOMÁTICA — FLUXO EM 2 PASSOS (quando comentar "{gatilho}")
========================================

PASSO 1 — mensagem automática assim que a pessoa comenta a palavra-chave:

Hey, I got the {nome} repository for you! Just type the word {gatilho} here in the chat.

---

PASSO 2 — quando a pessoa responde com a palavra "{gatilho}" no chat, envia o botão com o link direto:

Here you go \U0001f680 Here's the repository, straight to the point:
[BUTTON: Get the repository → {_URL_PLACEHOLDER}]

Quick install: check the README for setup instructions."""
    else:
        texto = f"""========================================
LEGENDA DO POST (Instagram) — {nome}
========================================

Comenta {gatilho} que eu te mando o link — {primeira_frase}.

{corpo}

→ open source, de graça pra usar
→ repositório ativo no GitHub
→ fácil de configurar

Segue @automatrix.ia pra mais achados open source de IA e dev \U0001f48e

#opensource #github #devbr #programacao #tecnologia


========================================
DM AUTOMÁTICA — FLUXO EM 2 PASSOS (quando comentar "{gatilho}")
========================================

PASSO 1 — mensagem automática assim que a pessoa comenta a palavra-chave:

E aí, peguei o repositório do {nome} pra você! Só preciso que você digite a palavra {gatilho} aqui no chat.

---

PASSO 2 — quando a pessoa responde com a palavra "{gatilho}" no chat, envia o botão com o link direto:

Prontinho \U0001f680 Aqui está o repositório, direto ao ponto:
[BOTÃO: Pegar o repositório → {_URL_PLACEHOLDER}]

Instalação rápida: consulte o README do repositório pra ver o passo a passo."""
    return texto


def gerar_legenda_dm(nome, description, roteiro, readme_text, gatilho, repo_url, stars=None, language=None, idioma="pt", engine="gemini"):
    """Gera legenda do post + fluxo de DM (2 passos), reaproveitando o mesmo
    motor (gemini/claude) já usado pra gerar o roteiro. Retorna (texto, origem)
    igual ao gerar_roteiro. O placeholder {URL_DO_REPO} é sempre substituído
    aqui pela URL real antes de retornar, então quem chama não precisa se
    preocupar com isso."""
    idioma = idioma if idioma in IDIOMAS else "pt"
    engine = engine if engine in ENGINES else "gemini"
    prompt = build_prompt_legenda_dm(nome, description, roteiro, readme_text, gatilho, stars, language, idioma)

    try:
        if engine == "claude":
            texto = _call_claude(prompt)
        else:
            texto = _call_gemini(prompt)
        origem = engine
    except Exception as e:  # noqa: BLE001
        texto = gerar_legenda_dm_fallback(nome, description, roteiro, gatilho, idioma)
        origem = f"fallback_heuristico:erro_{engine}:{e}"

    url_final = repo_url or "(cole aqui o link do repositório)"
    texto = texto.replace(_URL_PLACEHOLDER, url_final)
    return texto, origem
