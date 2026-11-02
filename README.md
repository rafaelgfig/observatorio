# Observatório — apagões (ANEEL) e ITBI (São Paulo)

Onde falta energia no Brasil e por quanto se vendem os imóveis em São Paulo — em mapa, a partir
de dados abertos oficiais, sem cadastro e sem chave de API.

<!-- Capturas de tela: adicionar docs/img/apagoes.png e docs/img/itbi.png (ex.: 1600×1000). -->
![Apagões: DEC por município, com painel do escopo e evolução mensal](docs/img/apagoes.png)
![ITBI: transações num raio a partir de um endereço, com mediana de R$/m²](docs/img/itbi.png)

## Funcionalidades

* **Apagões** — interrupções de energia nas redes de distribuição
  ([ANEEL](https://dadosabertos.aneel.gov.br/dataset/interrupcoes-de-energia-eletrica-nas-redes-de-distribuicao)),
  ano corrente e anterior: coroplético por município **ou por conjunto elétrico** (DEC, FEC,
  consumidores afetados, interrupções…; por conjunto também o DEC/FEC oficial, sem
  estimativa), busca com autocompletar, evolução mensal, calculado × oficial, causas, ranking e
  as maiores interrupções de cada lugar.
* **ITBI São Paulo** — guias de ITBI pagas
  ([Prefeitura de SP](https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501)),
  de 2015 até hoje, **geocodificadas** pelo cadastro de lotes do GeoSampa: pesquisa por raio
  a partir de um endereço, por polígono desenhado ou por distrito; filtros por período, tipo
  de imóvel, valor e área; mediana de R$/m², série mensal, tabela e exportação CSV.

* **Para todos**: links compartilháveis (o estado da tela vai no endereço), tema escuro por
  padrão, basemap do [OpenFreeMap](https://openfreemap.org) (sem chaves de API), tabelas como
  alternativa acessível aos gráficos e API JSON documentada em `/api/docs`.
* **Dados em dia**: ingestão incremental (só baixa e reprocessa o que mudou nas fontes) e um
  pacote de dados publicado no GitHub para o site público, atualizado todo mês por uma Action.

## Requisitos

* [uv](https://docs.astral.sh/uv/) (instala o Python 3.13 sozinho) — ou só o Docker
* Internet na primeira ingestão (~1,4 GB de downloads, ~4 min de processamento) e para os
  mapas de fundo
* ~2 GB de RAM livres para o servidor (o ITBI é carregado em memória e comprimido)

## Começando

```bash
uv sync
uv run observatorio ingest all
uv run observatorio serve
```

Abra <http://127.0.0.1:8000>. A documentação interativa da API fica em `/api/docs`.

Sem ingerir (usa o pacote de dados já publicado, ~590 MB):

```bash
uv run observatorio bundle baixar https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar
uv run observatorio serve
```

### Com Docker

```bash
docker build -t observatorio .
docker run --rm -p 7860:7860 \
  -e OBSERVATORIO_BUNDLE_URL=https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar \
  observatorio
```

Abra <http://localhost:7860>: o contêiner sobe na hora e baixa os dados em segundo plano; até
lá, uma página de espera mostra o andamento e abre o site sozinha quando os dados chegam.

### Atualizar os dados

`ingest` é **incremental**: guarda ETag/Last-Modified e o hash de cada arquivo em
`data/manifest/` e só reprocessa o que mudou. No ITBI, cada mês é uma partição — quando a
prefeitura publica um novo mês no arquivo do ano corrente, só esse mês é gravado. Rodar de novo
sem novidades leva segundos. Com o servidor no ar, ele recarrega os dados sozinho ao fim da
ingestão.

```bash
uv run observatorio ingest itbi          # só o ITBI
uv run observatorio ingest aneel --year 2026
uv run observatorio ingest all --force   # reprocessa tudo
uv run observatorio status               # o que foi baixado/processado e quando
uv run observatorio bundle criar dist/observatorio-dados.tar --enxuto   # pacote para publicar
```

## Publicar o site

O GitHub Pages sozinho não serve: o site precisa de um servidor Python. O código e os dados
ficam no GitHub (os dados como asset da Release `dados`, gerado pela Action
[`dados.yml`](.github/workflows/dados.yml)) e o app roda num contêiner — de graça no
[Hugging Face Spaces](https://huggingface.co/spaces), ou no Cloud Run ou num servidor
qualquer. Passo a passo, custos e limites em [docs/deploy.md](docs/deploy.md).

## Desenvolvimento

```bash
uv run pytest                 # testes (sem acessar as fontes de dados)
uv run pytest -m network      # só os testes que consultam as fontes reais (CKAN, Prefeitura)
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pre-commit install     # roda ruff + mypy a cada commit
uv run observatorio serve --reload
```

Configuração por variáveis de ambiente com prefixo `OBSERVATORIO_` (ou arquivo `.env`), por
exemplo `OBSERVATORIO_DATA_DIR`, `OBSERVATORIO_ITBI_MIN_YEAR`, `OBSERVATORIO_PORT`,
`OBSERVATORIO_ANEEL_CONJUNTOS=false` (pula os polígonos dos conjuntos) — veja
`src/observatorio/config.py`. O servidor só responde a `127.0.0.1`/`localhost`; para abrir na
rede local (`serve --host 0.0.0.0`), liste os nomes ou IPs usados em
`OBSERVATORIO_ALLOWED_HOSTS='["127.0.0.1", "192.168.0.10"]'`. Os testes ignoram essas variáveis
e o `.env`.

Na primeira execução, os testes (como a ingestão e o servidor) baixam a extensão `spatial` do
DuckDB (dezenas de MB, de `extensions.duckdb.org`, guardada em `~/.duckdb/extensions`); depois
disso `uv run pytest` roda sem internet.

A arquitetura, os esquemas dos dados processados e os contratos da API estão em
[docs/arquitetura.md](docs/arquitetura.md).

## Notas sobre os dados

* **DEC/FEC** são recalculados a partir das interrupções com o critério regulatório
  (duração ≥ 3 min, sem expurgos), que reproduz os indicadores oficiais da ANEEL com erro
  < 2% em ~97% dos conjuntos-mês. A tela mostra o valor oficial ao lado quando disponível.
* A **base de consumidores** de cada município (o denominador do DEC/FEC municipal) é estimada
  pelo tamanho dos alimentadores que o atendem, medido nas interrupções de 2026 — não por
  quantas vezes faltou energia ali —, limitada a 1,5 unidade consumidora por habitante.
* O arquivo de **2025** não informa o município de cada interrupção: os valores municipais
  desse ano são **estimados** distribuindo cada conjunto elétrico pelos municípios que atende,
  com os mesmos pesos. A interface sinaliza esses casos.
* O **mapa por conjunto elétrico** (o recorte em que a ANEEL publica o DEC/FEC) não tem
  estimativa: cada conjunto mostra o calculado das próprias interrupções e o indicador oficial.
  Os polígonos vêm da BDGD (base geográfica das distribuidoras, no ArcGIS Online da ANEEL; a
  ingestão lê por faixas HTTP só a camada dos conjuntos — ~80 MB em vez de 31 GB) e, para os
  poucos que faltam, do SIGEL (base 2020). Cobrem 100% dos consumidores de 2025 e 99,5% dos de
  2026 (conjuntos criados em 2026 ainda não têm polígono). Códigos antigos que ainda aparecem em
  poucos meses ao lado do sucessor de mesmo nome ficam fora do mapa daquele ano (não se
  sobrepõem a ele), mas continuam na busca e no painel. Sem acesso ao ArcGIS, a ingestão segue
  sem eles e a tela fica só com o mapa por município.
* Algumas distribuidoras têm meses faltando nos arquivos da ANEEL. Esses meses ficam fora das
  contas (não viram DEC 0): a tela lista as lacunas, marca os municípios e meses afetados e os
  tira do ranking.
* No **ITBI**, a localização vem do número de cadastro (SQL) cruzado com os lotes e condomínios
  do GeoSampa; ~94% das transações ficam no lote/condomínio exato e quase todo o resto no
  centro da quadra. Unidades vendidas na planta aparecem como terreno na fonte e são marcadas;
  a área do IPTU delas é a do lote inteiro e não entra nos filtros nem na mediana de área.
  Todas as guias de um condomínio caem no mesmo ponto: o mapa mostra um ponto por lote ou
  condomínio, e ao abri-lo aparecem as transações dele com unidade, bloco/torre e vagas (lidos
  do complemento, do bairro ou da referência, onde o cadastro os digita).
* O **valor** mostrado é o declarado pelo contribuinte, levado a 100% do imóvel quando só uma
  parte foi transmitida (partilhas, cessões de 50%…). Declarações muito abaixo ou acima do valor
  venal de referência da Prefeitura (menos da metade ou mais de 5×; vagas de garagem: menos de
  1/5) ou abaixo de R$ 5 mil — cessões de saldo de financiamento da COHAB, valores simbólicos —
  são marcadas como **atípicas** (~8% das guias) e ficam fora das medianas por padrão; dá para
  incluí-las ou vê-las sozinhas.
* O último mês de pagamento ainda recebe guias atrasadas: o período padrão termina no último
  mês completo, e os meses parciais aparecem marcados nos gráficos.
* Linhas corrompidas na origem (que expõem nomes das partes) são descartadas na ingestão, e
  CPFs/CNPJs digitados em campos de texto livre são removidos.

## Contribuindo

Sugestões e correções são bem-vindas — abra uma *issue* descrevendo o problema (com o link da
tela, que guarda os filtros) ou um *pull request*:

1. `uv sync` e `uv run pre-commit install`;
2. mantenha o estilo do código: tipagem estrita (`mypy`), `ruff`, docstrings e mensagens em
   português; no frontend, ES modules sem etapa de build, `// @ts-check` e dados sempre como
   texto (nunca `innerHTML`);
3. inclua testes (`uv run pytest`; eles não acessam as fontes de dados) e atualize
   [docs/arquitetura.md](docs/arquitetura.md) quando mudar um contrato da API ou um esquema;
4. nunca grave nem exponha dados pessoais (nomes das partes, CPF/CNPJ).

## Licença

A definir. Os dados vêm de fontes públicas (ANEEL, Prefeitura de São Paulo/GeoSampa, IBGE) e
seguem os termos de cada uma.
