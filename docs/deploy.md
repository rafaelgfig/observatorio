# Publicar o site

**GitHub Pages sozinho não serve: o site precisa de um servidor Python; o código e os dados
ficam no GitHub, o app roda no Hugging Face Spaces.** O GitHub Pages só entrega arquivos
estáticos, e cada tela do observatório consulta a API (FastAPI + DuckDB) — filtros, mapas,
tiles do ITBI, exportação CSV. O caminho recomendado, sem custo:

```
GitHub ── código (este repositório)
   │
   └── Action "Dados" (mensal) ── ingere as fontes ──► Release "dados"
                                                        observatorio-dados.tar  (~590 MB)
                                                        observatorio-dados.json (índice)
                                                              │ baixado na partida
Hugging Face Space (Docker, CPU grátis) ◄─────────────────────┘
   https://<usuario>-observatorio.hf.space
```

O mesmo contêiner roda no Google Cloud Run ou em qualquer servidor com Docker (seções 4 e 5).

## 1. O pacote de dados

A ingestão baixa ~1,4 GB das fontes e leva minutos; o servidor público não faz isso. Quem já
tem os dados (a Action ou o seu computador) gera um **pacote** com `data/processed` e
`data/manifest` — nunca `data/raw` — e o servidor só baixa e instala esse pacote.

```bash
uv run observatorio bundle criar dist/observatorio-dados.tar --enxuto
uv run observatorio bundle baixar https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar
uv run observatorio bundle baixar dist/observatorio-dados.tar     # também aceita um arquivo local
```

* **Formato**: um `.tar` sem compressão (os Parquet já são ZSTD). O 1º membro, `bundle.json`, é
  o índice: data de criação, perfil e tamanho + sha256 de cada arquivo. Ao lado do `.tar` vai
  uma cópia do índice (`observatorio-dados.json`, ~30 KB) com o sha256 do próprio `.tar`.
* **Perfis**: `completo` é a cópia fiel; `--enxuto` tira do fato da ANEEL as colunas que só a
  ingestão usa (`evento`, `cod_interrupcao`, `cod_ocorrencia`) — o site responde igual (há um
  teste que compara todas as rotas da ANEEL) e o pacote fica ~26% menor. Sobre dados de um
  pacote enxuto, `ingest aneel` se recusa a rodar e explica o que fazer.
* **Tamanho** (medido em 02/11/2026 com os dados de 2015–2026 do ITBI e 2025–2026 da ANEEL):
  enxuto **586 MB**, completo 792 MB (173 arquivos); o fato da ANEEL é ~60% do completo. Criar leva ~3 s;
  conferir e instalar, ~2 s (SSD).
* **Instalação segura**: confere o sha256 do `.tar` (quando há índice ao lado) e o de cada
  arquivo, recusa caminhos fora de `processed/`/`manifest/` (`..`, absolutos, links) e extrai
  numa pasta temporária dentro de `data/`; só depois troca `processed/` e `manifest/` de uma
  vez. Se algo falhar, os dados atuais ficam intactos. No Windows, pare o servidor antes (ele
  mantém os arquivos abertos).
* Os manifestos vão sem ETag/Last-Modified: uma ingestão local feita depois baixa as fontes de
  novo em vez de confiar num bruto antigo (o sha256 evita reprocessar o que não mudou).

### O servidor e o pacote

Com `OBSERVATORIO_BUNDLE_URL` definido, o servidor, na partida:

1. sobe na hora (o `/api/health` responde) e baixa o índice publicado (poucos KB);
2. se não há dados, ou se o índice publicado é mais novo e de outro conteúdo, baixa o `.tar`
   em segundo plano (retomando após quedas de rede), confere, instala e recarrega o banco;
3. enquanto não há dados, as rotas `/api/*` e `/dados/*` respondem **503** com o andamento
   (`Baixando os dados do observatório (45% de 586 MB)…`) e o endereço principal mostra uma
   **página de espera** com esse andamento, que abre o site sozinha (no mesmo `#/tela?filtros`
   do link) quando os dados ficam prontos;
4. repete a verificação a cada `OBSERVATORIO_BUNDLE_CHECK_HOURS` (24 h). Falhas são repetidas
   com espera crescente (1 min … 1 h).

`OBSERVATORIO_BUNDLE_URL` também aceita o caminho de um pacote no disco (ex.: um volume montado
no contêiner, seção 5): o índice é lido do `.json` ao lado e um pacote novo no mesmo caminho é
instalado na verificação seguinte.

Dados ingeridos localmente (sem `processed/.bundle.json`, ou alterados por um `ingest` depois
da instalação) nunca são substituídos.

| variável | padrão | uso |
|---|---|---|
| `OBSERVATORIO_BUNDLE_URL` | — | URL do `.tar` publicado (ou caminho de um pacote local); sem ela, nada é baixado |
| `OBSERVATORIO_BUNDLE_INDEX_URL` | URL do `.tar` com `.json` | índice publicado ao lado |
| `OBSERVATORIO_BUNDLE_CHECK_HOURS` | `24` | intervalo entre verificações (`0` = só na partida) |
| `OBSERVATORIO_ALLOWED_HOSTS` | na imagem: `["127.0.0.1", "localhost", "*.hf.space", "*.run.app"]` | nomes aceitos no cabeçalho `Host` (lista JSON) |
| `OBSERVATORIO_DATA_DIR` | na imagem: `/home/app/data` | onde ficam os dados |
| `PORT` | na imagem: `7860` | porta HTTP (o Cloud Run define `8080`); `OBSERVATORIO_PORT` e `--port` têm prioridade |
| `OBSERVATORIO_HOST` | na imagem: `0.0.0.0` | interface de rede |

`OBSERVATORIO_ALLOWED_HOSTS` protege contra *DNS rebinding*: o servidor responde 400 a nomes
fora da lista. Num domínio próprio, **mantenha `127.0.0.1` e `localhost`** (o HEALTHCHECK da
imagem usa esses nomes), por exemplo `'["observatorio.exemplo.com.br", "127.0.0.1", "localhost"]'`.

## 2. Publicar os dados no GitHub

1. Suba o repositório para o GitHub como **público** (o servidor baixa os assets da Release
   sem autenticação; num repositório privado o download falha).
2. Em **Actions → Dados → Run workflow**, rode a Action (perfil `enxuto`). Ela ingere tudo do
   zero (algumas dezenas de minutos, conforme a velocidade das fontes), gera o pacote e publica na Release **`dados`** (criada na 1ª vez, sem virar
   a "Latest"). Depois disso roda sozinha todo dia 5, às 06:00 (Brasília).
3. A URL do pacote fica fixa:
   `https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar`.

Assets de Release aceitam até 2 GB por arquivo e não contam na cota do repositório; o
download é gratuito. Em repositórios públicos sem nenhum commit por 60 dias, o GitHub desliga
as Actions agendadas (avisa por e-mail): reative em **Actions → Dados → Enable workflow**.

**Se a Action falhar na ingestão**: as fontes da Prefeitura de São Paulo (página do ITBI e WFS
do GeoSampa) às vezes recusam máquinas de nuvem. A Release anterior continua valendo; publique
do seu computador (precisa do [GitHub CLI](https://cli.github.com/) com `gh auth login`):

```bash
uv run observatorio ingest all
uv run observatorio bundle criar dist/observatorio-dados.tar --enxuto
# só na primeira vez:
gh release create dados --title "Dados processados" --notes "Dados do observatório" --latest=false
# o .tar antes do índice
gh release upload dados dist/observatorio-dados.tar --clobber
gh release upload dados dist/observatorio-dados.json --clobber
```

## 3. Hugging Face Spaces (recomendado)

O plano grátis (**CPU basic**: 2 vCPU, 16 GB de RAM, 50 GB de disco temporário) sobra para o
observatório (medido com os dados reais: ~0,4 GB de RAM em uso, pico de ~1,1 GB).

1. Crie uma conta em <https://huggingface.co> e um Space: **New Space → SDK: Docker → Blank**,
   hardware **CPU basic (free)**, visibilidade **Public**. Nome sugerido: `observatorio`.
2. Em **Settings → Variables and secrets → New variable**:
   `OBSERVATORIO_BUNDLE_URL` = a URL do pacote (seção 2). `*.hf.space` já é aceito pela imagem.
3. Envie o código com a biblioteca do Hugging Face (`huggingface_hub`, rodada pelo `uv` sem
   entrar no projeto). Ela sobe só o que a imagem usa, num commit, sem o histórico do git — um
   `git push` do repositório levaria o histórico inteiro, e o Hugging Face recusa pushes com
   arquivos binários (como capturas de tela em `docs/img/`) fora do Xet/LFS. O `README.md` que
   o Space cria sozinho (com `sdk: docker`; a porta padrão, 7860, é a da imagem) fica como
   está — edite título e descrição pela página do Space se quiser. Na raiz do repositório (no
   Windows, pelo Git Bash):

   ```bash
   # uma vez: cole um token com permissão de escrita (Settings → Access Tokens);
   # ou, só nesta sessão do terminal: export HF_TOKEN=<token>
   uvx --from huggingface_hub hf auth login

   uv run --no-project --with huggingface_hub python - <<'EOF'
   from huggingface_hub import upload_folder

   upload_folder(
       repo_id="<usuario>/observatorio",
       repo_type="space",
       folder_path=".",
       allow_patterns=["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "src/*"],
       ignore_patterns=["*__pycache__*"],
       delete_patterns=["src/*"],  # apaga do Space o que saiu de src/
       commit_message="Código do observatório",
   )
   EOF
   ```

   Para atualizar o código depois, rode o mesmo `upload_folder`; cada envio reconstrói a
   imagem.
4. O Space constrói a imagem (alguns minutos; aba **Logs**) e sobe. Na 1ª visita aparece a
   página de espera ("Preparando os dados do observatório…", com o andamento) por cerca de um
   minuto, e o site abre sozinho em seguida, em `https://<usuario>-observatorio.hf.space` (ou
   embutido em `https://huggingface.co/spaces/<usuario>/observatorio`).

**Limites do plano grátis**: o Space dorme depois de 48 h sem visitas e acorda na visita
seguinte (~1–2 min). O disco é temporário: a cada partida o pacote é baixado de novo (do
GitHub, ~590 MB). Com **armazenamento persistente** (pago) montado em `/data`, defina
`OBSERVATORIO_DATA_DIR=/data/observatorio` e a partida passa a só conferir o índice.

**Dados novos na hora**: o Space confere o índice a cada 24 h e em toda partida. Para reiniciá-lo
assim que a Action publicar, crie no GitHub a variável `HF_SPACE` (`<usuario>/observatorio`) e
o segredo `HF_TOKEN` (token do Hugging Face com escrita): o último passo da Action usa os dois.

## 4. Google Cloud Run (alternativa)

Precisa de um projeto com faturamento ativo (o uso pequeno cabe na cota grátis) e do `gcloud`.

```bash
gcloud run deploy observatorio --source . --region southamerica-east1 \
  --memory 4Gi --cpu 2 --min-instances 0 --max-instances 2 --allow-unauthenticated \
  --set-env-vars OBSERVATORIO_BUNDLE_URL=https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar
```

* O Cloud Run define `PORT=8080` (a imagem obedece) e `*.run.app` já é aceito.
* O disco do Cloud Run fica na memória: o pacote instalado (~0,6 GB) conta na RAM, por isso
  `--memory 4Gi`.
* Com `--min-instances 0`, cada partida a frio baixa o pacote (~1 min). Para evitar, embuta os
  dados na imagem: `docker build --build-arg BUNDLE_URL=<url do .tar> --build-arg BUNDLE_REV=$(date +%s) -t <região>-docker.pkg.dev/<projeto>/observatorio/app .`,
  `docker push …` e `gcloud run deploy observatorio --image …` (refaça a cada pacote novo; o
  `BUNDLE_REV` diferente impede o Docker de reaproveitar do cache o pacote anterior).
* **Custos**: a cota grátis mensal (cobrança por requisição) é de 180 mil vCPU-segundos,
  360 mil GiB-segundos e 2 milhões de requisições — com 2 vCPU e 4 GiB, ~25 h de instância
  ativa por mês. Acima disso, cobra-se pelo uso; o Artifact Registry guarda a imagem (~550 MB)
  com 0,5 GB grátis. Confira os preços atuais em <https://cloud.google.com/run/pricing>.

## 5. Docker local ou num servidor (VPS)

```bash
docker build -t observatorio .

# local, com o pacote publicado
docker run --rm -p 7860:7860 \
  -e OBSERVATORIO_BUNDLE_URL=https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar \
  observatorio
# abra http://localhost:7860

# local, com um pacote gerado aqui (sem download): monte a pasta do pacote
uv run observatorio bundle criar dist/observatorio-dados.tar --enxuto
docker run --rm -p 7860:7860 -v "$PWD/dist:/pacote:ro" \
  -e OBSERVATORIO_BUNDLE_URL=/pacote/observatorio-dados.tar \
  observatorio
```

Num servidor, com volume para os dados (sobrevivem a reinícios) e um proxy HTTPS na frente
(ex.: [Caddy](https://caddyserver.com) com `observatorio.exemplo.com.br { reverse_proxy 127.0.0.1:8000 }`):

```bash
docker run -d --name observatorio --restart unless-stopped -p 127.0.0.1:8000:7860 \
  -v observatorio-dados:/home/app/data \
  -e OBSERVATORIO_BUNDLE_URL=https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar \
  -e OBSERVATORIO_ALLOWED_HOSTS='["observatorio.exemplo.com.br", "127.0.0.1", "localhost"]' \
  observatorio
```

Detalhes da imagem: Python 3.13 *slim* em duas etapas (dependências instaladas com `uv sync
--locked --no-dev`), usuário sem privilégios (uid 1000), extensão `spatial` do DuckDB já
instalada (nada é baixado de `extensions.duckdb.org` em execução), `HEALTHCHECK` em
`/api/health` e cabeçalhos `X-Forwarded-*` aceitos do proxy da plataforma. A cada *pull
request* e a cada push na `main`, o CI (job "Imagem Docker" de `.github/workflows/ci.yml`)
constrói a imagem, sem publicá-la, e confere que o servidor sobe.

## 6. Atualizar os dados

* **Automático**: a Action **Dados** roda todo dia 5; o servidor pega o pacote novo na próxima
  partida ou verificação (24 h), ou na hora com o passo opcional de reinício do Space.
* **Manual**: Actions → Dados → Run workflow, ou os comandos locais da seção 2.
* **Servidor com os próprios dados** (sem pacote): rode `observatorio ingest all` nele; a API
  recarrega sozinha ao fim da ingestão.
* **Mudou a ingestão** (arquivo ou coluna nova em `data/processed`, versão de regras): o pacote
  publicado foi gerado pelo código antigo e o site novo responderia 503 nas telas que dependem
  do que falta. Rode a Action **Dados** (ou publique da sua máquina) junto com a atualização do
  código no Space.

## 7. Custos e limites (resumo)

| onde | custo | limites relevantes |
|---|---|---|
| GitHub (código, Actions, Release) | grátis em repositório público | asset ≤ 2 GB; job da Action ≤ 6 h |
| Hugging Face Spaces, CPU basic | grátis | dorme após 48 h sem visitas; disco temporário (pacote baixado a cada partida) |
| Google Cloud Run | grátis até a cota mensal, depois por uso | RAM inclui o disco; partida a frio baixa o pacote |
| VPS | o do provedor (~US$ 5/mês com 2 GB de RAM) | você cuida de HTTPS e atualizações |

A API não tem limite de requisições por cliente; num endereço muito divulgado, prefira um
proxy com limitação (Caddy, Cloudflare) na frente.
