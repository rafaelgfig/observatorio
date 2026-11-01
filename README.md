# Observatório — apagões (ANEEL) e ITBI (São Paulo)

Site local para explorar, em mapa, dois conjuntos de dados abertos:

* **Apagões** — interrupções de energia nas redes de distribuição
  ([ANEEL](https://dadosabertos.aneel.gov.br/dataset/interrupcoes-de-energia-eletrica-nas-redes-de-distribuicao)),
  ano corrente e anterior: coroplético por município (DEC, FEC, consumidores afetados,
  interrupções…), busca com autocompletar, evolução mensal, causas, ranking e as maiores
  interrupções de cada lugar.
* **ITBI São Paulo** — guias de ITBI pagas
  ([Prefeitura de SP](https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501)),
  de 2015 até hoje, **geocodificadas** pelo cadastro de lotes do GeoSampa: pesquisa por raio
  a partir de um endereço, por polígono desenhado ou por distrito; filtros por período, tipo
  de imóvel, valor e área; mediana de R$/m², série mensal, tabela e exportação CSV.

Tema escuro por padrão, sem chaves de API (basemap do [OpenFreeMap](https://openfreemap.org)).

## Requisitos

* [uv](https://docs.astral.sh/uv/) (instala o Python 3.13 sozinho)
* Internet na primeira ingestão (~1 GB de downloads no total) e para os mapas de fundo

## Começando

```bash
uv sync
uv run observatorio ingest all
uv run observatorio serve
```

Abra <http://127.0.0.1:8000>. A documentação interativa da API fica em `/api/docs`.

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
```

## Desenvolvimento

```bash
uv run pytest                 # testes (sem rede)
uv run pytest -m network      # inclui testes que acessam as fontes reais
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pre-commit install     # roda ruff + mypy a cada commit
uv run observatorio serve --reload
```

Configuração por variáveis de ambiente com prefixo `OBSERVATORIO_` (ou arquivo `.env`), por
exemplo `OBSERVATORIO_DATA_DIR`, `OBSERVATORIO_ITBI_MIN_YEAR`, `OBSERVATORIO_PORT` — veja
`src/observatorio/config.py`.

A arquitetura, os esquemas dos dados processados e os contratos da API estão em
[docs/arquitetura.md](docs/arquitetura.md).

## Notas sobre os dados

* **DEC/FEC** são recalculados a partir das interrupções com o critério regulatório
  (duração ≥ 3 min, sem expurgos), que reproduz os indicadores oficiais da ANEEL com erro
  < 2% em ~97% dos conjuntos-mês. A tela mostra o valor oficial ao lado quando disponível.
* O arquivo de **2025** não informa o município de cada interrupção: os valores municipais
  desse ano são **estimados** distribuindo cada conjunto elétrico pelos municípios que atende
  (pesos aprendidos com os dados de 2026). A interface sinaliza esses casos.
* Algumas distribuidoras têm meses faltando nos arquivos da ANEEL; a tela lista essas lacunas.
* No **ITBI**, a localização vem do número de cadastro (SQL) cruzado com os lotes e condomínios
  do GeoSampa; ~94% das transações ficam no lote/condomínio exato e quase todo o resto no
  centro da quadra. Unidades vendidas na planta aparecem como terreno na fonte e são marcadas.
* Linhas corrompidas na origem (que expõem nomes das partes) são descartadas na ingestão.
