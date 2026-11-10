# Publicar o site

**GitHub Pages sozinho não serve: o site precisa de um servidor Python; o código e os dados
ficam no GitHub, o app roda no Google Cloud Run.** O GitHub Pages só entrega arquivos
estáticos, e cada tela do observatório consulta a API (FastAPI + DuckDB) — filtros, mapas,
tiles do ITBI, exportação CSV. O caminho recomendado, de custo quase zero:

```
GitHub ── código (este repositório)
   │
   └── Action "Dados" (mensal) ── ingere as fontes ──► Release "dados"
                                                        observatorio-dados.tar  (~590 MB)
                                                        observatorio-dados.json (índice)
                                                              │ embutido no build
Cloud Build ── imagem com os dados ──► Artifact Registry ──► Cloud Run (São Paulo)
```

O mesmo contêiner roda no Hugging Face Spaces (pago desde 2026) ou em qualquer servidor com
Docker (seções 4 e 5).

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
# o site no Cloud Run só pega o pacote novo ao ser reconstruído (seção 3)
gh workflow run publicar.yml
```

## 3. Google Cloud Run (recomendado)

O Cloud Run cobra só enquanto atende requisições e desliga a instância sem visitas. O nível
grátis mensal — 180 mil vCPU-segundos, 360 mil GiB-segundos e 2 milhões de requisições — é
aplicado como desconto em dinheiro pelos preços de `us-central1` e vale em qualquer região: em
São Paulo (`southamerica-east1`, ~30% mais cara e bem mais perto dos visitantes) cobre um pouco
menos de tempo, o bastante para um site com pouco tráfego.

Os dados vão **dentro da imagem** (`deploy/cloudbuild.yaml` passa `BUNDLE_URL` ao Docker): no
Cloud Run o disco é a própria RAM, e baixar o pacote na partida custaria ~1,2 GB de memória e
~1 min a cada instância nova. Arquivos da imagem não contam na memória. Medido em 10/11/2026
(1 vCPU, 2 GiB, `--cpu-boost`): uma instância nova responde em ~9 s, dos quais 4,5 s carregando
os dados; com ela ligada, as rotas respondem em 0,1–0,2 s de São Paulo. O build leva ~3 min.

### Uma vez

1. Crie um projeto em <https://console.cloud.google.com> e vincule uma conta de faturamento
   (exigida mesmo no nível grátis). Contas novas começam numa avaliação de 90 dias; ao fim,
   clique em **Ativar conta completa**, senão o serviço para.
2. Instale o [gcloud](https://cloud.google.com/sdk/docs/install) (no Windows,
   `winget install --id Google.CloudSDK -e`), entre e prepare o projeto:

   ```bash
   gcloud auth login
   gcloud config set project <projeto>
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
     artifactregistry.googleapis.com billingbudgets.googleapis.com
   gcloud artifacts repositories create observatorio --repository-format=docker \
     --location=southamerica-east1
   # guarda só as 2 imagens mais recentes (cada uma tem ~1,2 GB; 0,5 GB são grátis)
   gcloud artifacts repositories set-cleanup-policies observatorio \
     --location=southamerica-east1 --policy=deploy/limpeza-imagens.json --no-dry-run
   ```

   Logo depois de ligar o Cloud Build, o 1º `builds submit` pode responder `PERMISSION_DENIED`
   por um ou dois minutos, até a permissão se propagar; é só repetir.
3. Alerta de gastos: e-mail aos administradores do faturamento em 50%, 90% e 100% do valor, e
   quando a previsão do mês passar dele (`gcloud billing accounts list` mostra a conta):

   ```bash
   gcloud billing budgets create --billing-account=<conta> --display-name="Observatorio" \
     --budget-amount=10BRL --filter-projects=projects/<projeto> \
     --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 \
     --threshold-rule=percent=1.0 --threshold-rule=percent=1.0,basis=forecasted-spend
   ```

### Publicar (código novo ou pacote de dados novo)

Na raiz do repositório, um comando constrói no Google a imagem com o pacote publicado agora (o
`.gcloudignore` envia só ~1,5 MB) e atualiza o serviço (`observatorio`, criado na 1ª vez):

```bash
gcloud builds submit --config deploy/cloudbuild.yaml
```

Só na 1ª vez, libere o acesso público (o build não mexe nas permissões do serviço):

```bash
gcloud run services add-iam-policy-binding observatorio --region=southamerica-east1 \
  --member=allUsers --role=roles/run.invoker
```

* O Cloud Run define `PORT=8080` (a imagem obedece) e `*.run.app` já é aceito em
  `OBSERVATORIO_ALLOWED_HOSTS`.
* `--max-instances=1` (em `deploy/cloudbuild.yaml`) é o teto de gasto: num pico de acessos, as
  requisições esperam a vez em vez de abrir novas instâncias. `--min-instances=0` desliga sem
  visitas (a próxima espera a partida); manter uma instância ligada deixa de ser grátis.
* Não defina `OBSERVATORIO_BUNDLE_URL` no serviço: com os dados na imagem, ele só faria a
  instância baixar o pacote de novo para a memória.

### Publicar sozinho pelo GitHub

A Action **Publicar** (`.github/workflows/publicar.yml`) roda o mesmo `builds submit` quando a
Action **Dados** publica um pacote novo, e à mão em **Actions → Publicar → Run workflow** (código
novo, ou pacote publicado de um computador). O acesso é por **federação de identidade**: nenhuma
chave fica guardada no GitHub, e o Google só aceita tokens da `main` deste repositório. Uma vez,
com uma conta dona do projeto:

```bash
PROJ=<projeto>; NUM=$(gcloud projects describe $PROJ --format='value(projectNumber)')
REPO=<usuario>/<repo>; SA=github-publicar@$PROJ.iam.gserviceaccount.com
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
gcloud iam service-accounts create github-publicar --display-name="GitHub Actions: publicar"
# enviar o build (e o código ao bucket do Cloud Build) e rodá-lo como a conta padrão do build
gcloud projects add-iam-policy-binding $PROJ --member=serviceAccount:$SA \
  --role=roles/cloudbuild.builds.editor --condition=None
gcloud projects add-iam-policy-binding $PROJ --member=serviceAccount:$SA \
  --role=roles/serviceusage.serviceUsageConsumer --condition=None
gcloud storage buckets add-iam-policy-binding gs://${PROJ}_cloudbuild \
  --member=serviceAccount:$SA --role=roles/storage.admin
gcloud iam service-accounts add-iam-policy-binding $NUM-compute@developer.gserviceaccount.com \
  --member=serviceAccount:$SA --role=roles/iam.serviceAccountUser
# GitHub → Google: só a main deste repositório
gcloud iam workload-identity-pools create github --location=global --display-name=GitHub
gcloud iam workload-identity-pools providers create-oidc observatorio --location=global \
  --workload-identity-pool=github --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref \
  --attribute-condition="assertion.repository=='$REPO' && assertion.ref=='refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding $SA --role=roles/iam.workloadIdentityUser \
  --member=principalSet://iam.googleapis.com/projects/$NUM/locations/global/workloadIdentityPools/github/attribute.repository/$REPO
# variáveis da Action (não são segredos)
gh variable set GCP_PROJECT --body $PROJ
gh variable set GCP_SERVICE_ACCOUNT --body $SA
gh variable set GCP_WIF_PROVIDER --body projects/$NUM/locations/global/workloadIdentityPools/github/providers/observatorio
gh variable set SITE_URL --body <URL do serviço>   # opcional: a Action confere /api/health no fim
```

O build roda com a conta padrão do Cloud Build (`<número>-compute@…`), que já pode publicar no
Cloud Run; a conta do GitHub só pode enviar builds.

## 4. Hugging Face Spaces (pago)

Desde 2026, Spaces com Docker exigem a assinatura **PRO** (US$ 9/mês); só os Spaces estáticos
seguem grátis. O hardware básico (**CPU basic**: 2 vCPU, 16 GB de RAM, 50 GB de disco
temporário) sobra para o observatório (medido com os dados reais: ~0,4 GB de RAM em uso, pico
de ~1,1 GB).

1. Com a conta PRO, crie um Space em <https://huggingface.co>: **New Space → SDK: Docker →
   Blank**, hardware **CPU basic**, visibilidade **Public**. Nome sugerido: `observatorio`.
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
       allow_patterns=["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "src/*"],
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

**Limites**: no CPU basic, o Space dorme depois de 48 h sem visitas e acorda na visita
seguinte (~1–2 min); só o hardware pago (a partir de US$ 0,03/h) fica sempre ligado. O disco é
temporário: a cada partida o pacote é baixado de novo (do GitHub, ~590 MB). Para evitar,
crie também a variável `BUNDLE_URL` (a mesma URL): o Hugging Face passa as variáveis ao build
como `--build-arg`, e os dados entram na imagem; a partida passa a só conferir o índice.

**Dados novos na hora**: o Space confere o índice a cada 24 h e em toda partida. Para reiniciá-lo
assim que a Action publicar, crie no GitHub a variável `HF_SPACE` (`<usuario>/observatorio`) e
o segredo `HF_TOKEN` (token do Hugging Face com escrita): o último passo da Action usa os dois.

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

* **Automático**: a Action **Dados** roda todo dia 5. No Cloud Run, os dados estão na imagem:
  em seguida a Action **Publicar** reconstrói e publica (seção 3). Servidores com
  `OBSERVATORIO_BUNDLE_URL` (Hugging Face, VPS) pegam o pacote novo na próxima partida ou
  verificação (24 h), ou na hora com o passo opcional de reinício do Space.
* **Manual**: Actions → Dados → Run workflow, ou os comandos locais da seção 2 seguidos de
  Actions → Publicar → Run workflow (ou `gh workflow run publicar.yml`).
* **Servidor com os próprios dados** (sem pacote): rode `observatorio ingest all` nele; a API
  recarrega sozinha ao fim da ingestão.
* **Mudou a ingestão** (arquivo ou coluna nova em `data/processed`, versão de regras): o pacote
  publicado foi gerado pelo código antigo e o site novo responderia 503 nas telas que dependem
  do que falta. Rode a Action **Dados** (ou publique da sua máquina) antes de publicar o
  código novo.

## 7. Custos e limites (resumo)

| onde | custo | limites relevantes |
|---|---|---|
| GitHub (código, Actions, Release) | grátis em repositório público | asset ≤ 2 GB; job da Action ≤ 6 h |
| Google Cloud Run (São Paulo) | grátis até a cota mensal, depois por uso; imagem no Artifact Registry ~US$ 0,07/mês | desliga sem visitas (a próxima espera a partida); RAM inclui o disco gravado |
| Hugging Face Spaces, CPU basic | assinatura PRO (US$ 9/mês) | dorme após 48 h sem visitas; disco temporário |
| VPS | o do provedor (~US$ 5/mês com 2 GB de RAM) | você cuida de HTTPS e atualizações |

A API não tem limite de requisições por cliente; num endereço muito divulgado, prefira um
proxy com limitação (Caddy, Cloudflare) na frente.
