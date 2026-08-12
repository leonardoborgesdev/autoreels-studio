# Integração com o FreeCut

O botão **"Abrir no editor"** da tela de resultado gera um projeto do
[FreeCut](https://github.com/walterlow/freecut) (`.freecut.zip`) com o vídeo
de fundo, o avatar (círculo + caixa grande dos cutaways), a moldura e a
legenda já nas mesmas posições usadas por `pipeline_remoto.py`, e abre o
editor numa aba nova. O gerador do pacote é
[`../dashboard/freecut_bundle.py`](../dashboard/freecut_bundle.py) — não
depende de nada aqui além de `ffprobe` (já usado no resto do projeto).

O FreeCut em si **não está vendorizado** neste repositório (projeto de
terceiros MIT, ~50 MB de código-fonte + `node_modules` de ~2 GB depois do
`npm install`). Pra rodar a integração:

```bash
git clone https://github.com/walterlow/freecut.git
cd freecut
git apply /caminho/pro/github-reels-generator/freecut-integration/default-language-ptbr.patch
npm install
npm run build
npx vite preview --host 0.0.0.0 --port 8100 --strictPort
```

## O patch de 1 linha

[`default-language-ptbr.patch`](default-language-ptbr.patch) muda só o
idioma padrão da interface pra PT-BR (`src/i18n/languages.ts`). O FreeCut já
vem com tradução PT-BR completa e pronta (i18next) — o patch só evita ter
que trocar manualmente o idioma toda vez que abre.

## Rodar como serviço (systemd)

[`freecut.service`](freecut.service) é a unit usada em produção — copie pra
`/etc/systemd/system/freecut.service` (ajustando `WorkingDirectory` pro seu
caminho), depois:

```bash
systemctl daemon-reload
systemctl enable --now freecut.service
```

## Formato do pacote `.freecut.zip`

Reverse-engineered do código-fonte do FreeCut
(`src/features/project-bundle/`): um ZIP com `manifest.json` (lista de mídia
+ checksum SHA-256 do próprio manifesto), `project.json` (timeline com
tracks/items, mídia referenciada por `mediaRef` em vez de um ID direto) e os
arquivos de mídia em `media/<sha256>/<nome>`. O checksum é verificado
estritamente na importação — bate exatamente porque o gerador usa
serialização compacta equivalente ao `JSON.stringify` do JavaScript (sem
espaços, sem escapar acentuação, ordem de chaves por inserção).

A importação em si (selecionar o arquivo `.freecut.zip` e escolher uma pasta
de destino pros arquivos de mídia) é manual — a File System Access API do
navegador exige um gesto direto do usuário pra abrir o seletor de pasta, não
dá pra automatizar isso a partir da dashboard.
