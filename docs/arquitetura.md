# Arquitetura

Este documento é a referência técnica do projeto: organização, convenções, esquemas dos
dados processados e **contratos da API** consumidos pelo frontend.

## Visão geral

```
fontes abertas ──(observatorio ingest …)──► data/raw ──► data/processed/*.parquet
                                                              │
                         FastAPI + DuckDB em memória ◄────────┘ (views / tabelas)
                                   │
                        site estático (MapLibre) em /
```

* **Ingestão** (`observatorio ingest geo|aneel|itbi|all`) é incremental: cada fonte tem uma
  chave lógica no `data/manifest.json` com URL, ETag/Last-Modified e sha256 do download e do
  último processamento. Só reprocessa o que mudou. Ao terminar, grava
  `data/processed/.version` — a API observa esse arquivo e recarrega sozinha.
* **API** abre um DuckDB **em memória** com views sobre os Parquet (`observatorio.db.Database`).
  Nunca grava nada; a ingestão substitui arquivos de forma atômica (`storage.atomic_path`).
* **Frontend** sem etapa de build: ES modules + import map com versões fixas (jsDelivr),
  MapLibre GL 6, terra-draw, estilo escuro do OpenFreeMap (sem chave de API).

## Organização do código

```
src/observatorio/
  config.py        Settings (pydantic-settings, prefixo OBSERVATORIO_)
  downloads.py     fetch() com GET condicional + Manifest
  db.py            connect() p/ ingestão; Database (views/tabelas) p/ API; macro norm_text
  storage.py       escrita atômica (COPY … TO), gzip, touch_version
  text.py          normalização de textos (busca, nomes de logradouros)
  cli.py           Typer: ingest geo|aneel|itbi|all, serve, status
  geo/ibge.py      municípios/UFs (malha simplificada, população, places)
  aneel/           sources.py, transform.py, ingest.py, queries.py, api.py
  itbi/            sources.py, parse.py, ingest.py, geosampa.py, geocode.py, queries.py, api.py
  api/             app.py (fábrica), deps.py, meta.py, search.py
  web/             index.html, css/, js/ (main.js, map.js, views/, components/)
tests/             pytest (sem rede por padrão; marcador `network` para os que acessam)
docs/
```

Cada domínio expõe em `<dominio>/api.py`:

* `DATASETS: list[Dataset]` — o que a API deve carregar de `data/processed/`;
* `router: APIRouter` — rotas sob `/api/<dominio>/`.

## Convenções

* Python 3.13, tipagem estrita (`mypy --strict`), `ruff` (lint + format), testes com `pytest`.
* Identificadores de infraestrutura em inglês; termos de domínio em português (`municipio`,
  `consumidores`, `logradouro`). Docstrings e mensagens em português.
* SQL em DuckDB **2.0**:
  * sempre `db.connect(spatial=True)` (carrega spatial e faz `SET GLOBAL geometry_always_xy = true`;
    toda geometria é **(lon, lat)**);
  * lambdas com `lambda x: …` (a seta `x -> …` foi removida);
  * relações não têm mais `fetchone()`/`fetchmany()`: use `con.execute(sql, params).fetchall()`;
  * parâmetros sempre via `?`/`$nome` — nunca interpolar valores vindos do usuário.
* Escrita de saídas: `storage.copy_query(con, sql, destino)` (Parquet ZSTD, atômico).
* Downloads: `downloads.fetch(client, key=…, url=…, dest=…, manifest=…)`; depois de processar
  com sucesso, `manifest.mark_processed(key, **extra)`; no fim de cada `run`, `touch_version`.
* **Dados pessoais**: linhas corrompidas na origem do ITBI trazem nomes de partes — são
  descartadas e só (arquivo, aba, linha) vai para o log. Nada disso é persistido.
* Busca: cada domínio grava `processed/<dominio>/places.parquet` com o esquema abaixo; a coluna
  `norm` usa a macro SQL `norm_text()` (equivalente a `text.search_key`).

### Esquema de `places.parquet`

| coluna | tipo | descrição |
|---|---|---|
| escopo | VARCHAR | `aneel` ou `itbi` (qual tela usa) |
| tipo | VARCHAR | `uf`, `municipio`, `distribuidora`, `logradouro`, `distrito`, `subprefeitura`, `cep` |
| id | VARCHAR | identificador usado nas rotas (ex.: código IBGE, CNPJ, nome do logradouro) |
| rotulo | VARCHAR | texto exibido (ex.: "Rua Isidoro Farina") |
| detalhe | VARCHAR | contexto curto (ex.: "SP", "312 transações") |
| norm | VARCHAR | `norm_text(...)` do texto pesquisável |
| peso | DOUBLE | relevância (população, nº de transações…) — maior primeiro |
| lon, lat | DOUBLE | ponto para centralizar/fixar marcador |
| bbox_w, bbox_s, bbox_e, bbox_n | DOUBLE | caixa para `fitBounds` (pode ser nulo) |

## Dados processados (`data/processed/`)

### geo/ (IBGE — pronto)

* `municipios.parquet`: `cod INT, nome, uf, uf_cod INT, uf_nome, regiao, populacao BIGINT,
  lon, lat (ponto interno), bbox_w, bbox_s, bbox_e, bbox_n` — 5.571 linhas.
* `ufs.parquet`: `cod INT, uf, nome, regiao, populacao, lon, lat, bbox_*`.
* `municipios.geojson(.gz)`: propriedades `cod, nome, uf`; servido em `/dados/geo/municipios.geojson`.
* `ufs.geojson(.gz)`: propriedades `cod, uf, nome`; servido em `/dados/geo/ufs.geojson`.

### aneel/

Fontes: Parquet oficiais de interrupções por ano (CKAN), indicadores oficiais DEC/FEC por
conjunto (`indicadores-continuidade-coletivos-2020-2029.parquet`) e o vínculo
conjunto↔município (`indqual-municipio.csv`).

Regras validadas contra o DEC/FEC oficial (erro < 2% em 97,5% dos conjunto-mês de 2025):

* limpeza: `SELECT DISTINCT *`; em 2026, quase-duplicatas por
  `(cnpj, CodInterrupcao, minuto(início), minuto(fim), consumidores)`;
* critério regulatório: duração ≥ 3 min **e** sem expurgo (2025 `IdeMotivoInterrupcao = 0`;
  2026 `DscMotivoExpurgo = 'Não houve Expurgo'`);
* DEC = Σ(consumidores × horas) / consumidores do conjunto; FEC = Σ consumidores / base;
* contagem de **eventos** comparável entre anos: `count(DISTINCT (cnpj, alimentador, minuto(início)))`;
* municípios: 2026 tem `CodMunicipioIBGE`; 2025 é **estimado** distribuindo cada conjunto pelos
  municípios com pesos aprendidos em 2026 (fallback IndQual × população). Erro típico ~20%:
  a UI sinaliza "estimado".

Tabelas (detalhes de colunas em `aneel/transform.py`):

* `fato/ano=YYYY.parquet` — interrupções limpas e unificadas (ambos os layouts);
* `conj_mun_pesos.parquet` — `conj, cod_mun, w, fonte, principal` (`principal`: o vínculo
  responde por ≥ 10% do conjunto ou da base do município — usado para listar conjuntos e
  interrupções de um município estimado sem incluir "transbordos" marginais);
* `mun_mes.parquet` — agregado município × mês × dimensões (programada, causa, regulatório,
  agente) com `consumidores`, `chi`, `eventos` (interrupções que atingiram o município) e
  `eventos_frac` (cada evento dividido entre os municípios que atingiu — soma sem dupla
  contagem em UF/Brasil/distribuidora);
* `mun_base.parquet` — base de consumidores estimada por município × mês × agente (NumCon
  oficial quando plausível, entre 0,5× e 2× a contagem do arquivo; senão a do arquivo);
* `conj_mes.parquet` — DEC/FEC calculado vs. oficial por conjunto × mês;
* `agente_mes.parquet` — completude por distribuidora × mês (lacunas conhecidas);
* `places.parquet` — distribuidoras (escopo `aneel`).

### itbi/

* `transacoes/ref_year=YYYY/ref_month=MM/part.parquet` — uma linha por guia paga
  (esquema na seção ITBI abaixo); um mês só é regravado quando o conteúdo normalizado muda
  (impressão digital por mês no manifesto). `parse.PARSER_VERSION` no manifesto faz a
  ingestão reprocessar sozinha quando as regras de normalização mudam.
* `geocode.parquet` — `sql, logradouro, numero, cep → lon, lat, precisao, tier` (uma linha por
  chave; junção *null-safe* com `transacoes`). `precisao` ∈ `lote`, `condominio`, `quadra`,
  `setor`; `tier` 1–9:

  | tier | regra | precisão típica (p50/p90 vs. CNEFE) |
  |---|---|---|
  | 1 | lote exato (setor-quadra-lote) | 19 / 33 m |
  | 2 | condomínio da quadra pelo logradouro + número | 31 / 68 m |
  | 3 | condomínio da quadra pelo número | 30 / 69 m |
  | 4 | lote da quadra pelo logradouro + número | 24 / 56 m |
  | 5 | mesmo SQL resolvido em outra chave (herda a precisão) | 28 / 97 m |
  | 6 | único condomínio do logradouro na quadra | 55 / 229 m |
  | 7 | unidade vizinha (lote ±3) ligada a um condomínio | 45 / 143 m |
  | 8 | ponto da quadra | 76 / 196 m |
  | 9 | ponto do setor | ~700 m |

  O filtro de precisão `lote` da API inclui `lote` **e** `condominio`.
* `areas.parquet` — 96 distritos e 32 subprefeituras (GeoSampa), polígonos simplificados sem
  frestas: `tipo, id, nome, geom (GEOMETRY), bbox_*`. `id` = código oficial com 2 dígitos
  (ex.: distrito `"37"`).
* `places.parquet` — logradouros (id = nome como na fonte, ex. `R ISIDORO FARINA`), CEPs (8
  dígitos), distritos e subprefeituras (escopo `itbi`).
* `geo/` — tabelas auxiliares do GeoSampa (`lotes`, `quadras`, `setores`, `distritos`,
  `subprefeituras`), refeitas quando as camadas brutas mudam (`manifest/geosampa.json`;
  camadas rebaixadas após `geosampa_max_age_days` ou com `ingest itbi --geosampa`).

#### Esquema de `transacoes`

| coluna | tipo | notas |
|---|---|---|
| id | BIGINT | `ref_year*10^8 + ref_month*10^6 + row_in_sheet` |
| ref_year, ref_month | SMALLINT, TINYINT | mês de **pagamento** (aba da planilha) |
| row_in_sheet | INTEGER | linha de dados (0-based) na aba |
| sql | VARCHAR | 11 dígitos com zeros à esquerda |
| setor, quadra, lote | VARCHAR | partes do SQL (3, 3, 4 dígitos) |
| logradouro | VARCHAR | como na fonte, sem espaços extras (ex.: `R ISIDORO FARINA`) |
| numero | INTEGER | nulo para 0 / 99999 / texto |
| numero_raw | VARCHAR | texto original |
| complemento, bairro, referencia | VARCHAR | |
| cep | VARCHAR | 8 dígitos |
| natureza_cod, natureza | SMALLINT, VARCHAR | ex.: 1, "Compra e venda" |
| valor_transacao | DOUBLE | R$ declarado |
| data_transacao | DATE | |
| valor_venal_ref, proporcao_pct, valor_venal_ref_prop, base_calculo | DOUBLE | |
| financiamento_cod, financiamento, valor_financiado | SMALLINT, VARCHAR, DOUBLE | |
| cartorio_num | SMALLINT | |
| matricula | BIGINT | |
| situacao_sql | VARCHAR | |
| area_terreno_m2, testada_m, fracao_ideal, area_construida_m2 | DOUBLE | |
| uso_cod, uso | SMALLINT, VARCHAR | descrição sem espaços extras |
| padrao_cod, padrao | SMALLINT, VARCHAR | |
| acc_ano | SMALLINT | ano de construção corrigido; nulo fora de [1800, ref_year+1] |
| tipo | VARCHAR | `apartamento`, `casa`, `terreno`, `comercial`, `garagem`, `outros` (pelo uso do IPTU; nas unidades na planta, pelo complemento: AP → apartamento, SALA/CJ → comercial, VG → garagem…) |
| na_planta | BOOLEAN | unidade vendida sob o SQL de um lote ainda não desdobrado: uso terreno, ou fração < 10% de um lote cujo uso não é de unidade autônoma, com complemento de unidade |
| rs_m2 | DOUBLE | valor / (área × proporção transmitida) — área construída, ou do terreno para `terreno`; nulo para garagem, na planta, proporção fora de 10–100%, valor < R$ 5 mil ou resultado fora de R$ 100–100 mil/m² |
| possivel_duplicata | BOOLEAN | idêntica a uma linha anterior da mesma aba nas 28 colunas (a 1ª ocorrência fica `false`) |
| aba | VARCHAR | nome da aba de origem (ex.: `JAN-2026`) |

## Contratos da API

Todas as respostas são JSON (UTF-8). Valores monetários em R$, áreas em m², durações em horas,
datas ISO-8601. Erros: `{"detail": "mensagem"}`; dados ainda não ingeridos → **503**.

### Comuns

* `GET /api/health` → `{status, versao, dados: [..]}`
* `GET /api/meta` → fontes e datas de atualização.
* `GET /api/busca?q=&escopo=aneel|itbi&limite=8` →
  `[{tipo, id, rotulo, detalhe, lon, lat, bbox: [w,s,e,n] | null}]`
* `GET /dados/geo/municipios.geojson`, `GET /dados/geo/ufs.geojson` (gzip).

### ANEEL — `/api/aneel`

Parâmetros de filtro comuns (query string), modelo `FiltroAneel`:

| parâmetro | padrão | descrição |
|---|---|---|
| `ano` | último ano | 2025, 2026… |
| `mes_ini`, `mes_fim` | 1, 12 | intervalo de meses (inclusive) |
| `criterio` | `regulatorio` | `regulatorio` (≥3 min, sem expurgo) ou `todas` |
| `programadas` | `true` | incluir interrupções programadas |
| `causa` | — | repetível; filtra causas (ver `/meta`) |
| `agente` | — | CNPJ (14 dígitos) da distribuidora |
| `uf` | — | sigla, limita a uma UF |
| `mun` | — | código IBGE (7 dígitos) |

Métricas (`metrica`): `dec` (h/consumidor), `fec` (interrupções/consumidor), `chi`
(consumidor-hora), `consumidores` (consumidores afetados, soma), `eventos` (interrupções),
`duracao` (duração média por consumidor afetado, h = chi/consumidores).

* `GET /api/aneel/meta` →
  ```json
  {"anos": [{"ano": 2026, "meses": [1,2,3,4,5,6,7], "gerado_em": "2026-09-04",
             "municipio_estimado": false}],
   "causas": ["Próprias do sistema", "Meio ambiente", "..."],
   "metricas": [{"id": "dec", "nome": "DEC", "unidade": "h",
                 "descricao": "Duração equivalente de interrupção por consumidor"}],
   "lacunas": [{"agente": "COPEL-DIS", "cnpj": "...", "ano": 2025, "mes": 6,
                "cobertura": 0.0}]}
  ```
* `GET /api/aneel/mapa?{filtro}&metrica=dec` → valores por município para o coroplético:
  ```json
  {"metrica": "dec", "unidade": "h", "estimado": true,
   "valores": {"3550308": 5.54, "...": 0.0},
   "quebras": [0.5, 1.2, 2.0, 3.4, 5.9, 10.1],
   "pouca_base": ["1100015"],
   "n": 5521}
  ```
  `quebras` = 6 limites (quantis 1/7…6/7) → 7 classes de cor. `pouca_base` = municípios com
  base estimada < 2.000 consumidores (DEC/FEC instáveis; a UI esmaece).
* `GET /api/aneel/resumo?{filtro}` → painel do escopo (Brasil, UF, município ou distribuidora):
  ```json
  {"escopo": {"tipo": "municipio", "id": "3550308", "nome": "São Paulo – SP"},
   "estimado": false,
   "kpis": {"dec": 3.1, "fec": 1.9, "chi": 2.1e7, "consumidores": 1.3e7,
            "eventos": 48211, "duracao": 1.6, "consumidores_base": 7.1e6,
            "dec_oficial": 3.0, "fec_oficial": 1.8},
   "mensal": [{"mes": 1, "dec": 0.4, "fec": 0.3, "chi": 2.9e6, "consumidores": 2.1e6,
               "eventos": 6100}],
   "causas": [{"causa": "Meio ambiente", "chi": 9.0e6, "pct": 0.43}],
   "distribuidoras": [{"cnpj": "...", "sigla": "ENEL SP", "chi": 2.0e7, "pct": 0.95}],
   "conjuntos": [{"id": 12345, "nome": "...", "sigla": "ENEL SP", "dec": 4.2,
                  "dec_oficial": 4.1, "fec": 2.1, "fec_oficial": 2.0}]}
  ```
  `dec_oficial`/`fec_oficial` só quando houver indicador oficial comparável (nulo caso contrário);
  `distribuidoras` e `conjuntos` podem vir vazios conforme o escopo.
* `GET /api/aneel/ranking?{filtro}&metrica=dec&ordem=desc&limite=20&min_consumidores=2000` →
  `[{"cod": "3550308", "nome": "São Paulo", "uf": "SP", "valor": 3.1, "consumidores_base": 7.1e6}]`
* `GET /api/aneel/interrupcoes?{filtro}&limite=20` → maiores interrupções (por CHI) do escopo:
  ```json
  [{"inicio": "2026-03-04T17:20:00", "fim": "2026-03-05T09:00:00", "duracao": 15.7,
    "consumidores": 12000, "chi": 188400, "causa": "Meio ambiente", "detalhe": "Vento",
    "programada": false, "expurgo": "Não houve Expurgo", "sigla": "ENEL SP",
    "conjunto": "...", "alimentador": "...", "municipio_estimado": false}]
  ```

### ITBI — `/api/itbi`

Corpo comum das consultas (POST), modelo `ConsultaItbi`:

```json
{"area": null,
 "data_ini": "2025-01-01", "data_fim": "2026-12-31",
 "tipos": ["casa", "apartamento"],
 "naturezas": [1],
 "valor_min": null, "valor_max": null,
 "area_min": null, "area_max": null,
 "logradouro": null, "cep": null,
 "precisao": "quadra"}
```

`area` (opcional, um de):

* `{"tipo": "circulo", "lon": -46.6, "lat": -23.5, "raio_m": 500}` (raio 50 m … 20 km);
* `{"tipo": "poligono", "geometria": {GeoJSON Polygon|MultiPolygon}}` (desenhado);
* `{"tipo": "distrito" | "subprefeitura", "id": "..."}`.

`precisao`: `lote` (só lote/condomínio exato), `quadra` (padrão: até centroide de quadra) ou
`qualquer`. `tipos`/`naturezas` vazios ou nulos = todos. Datas filtram `data_transacao`.

Observações para a implementação: `natureza` código 5 tem dois rótulos ao longo dos anos
("Excesso de meação…" até 2020, "Adjudicação compulsória" a partir de 2021) — agrupe por
(código, rótulo); `data_transacao` começa em 1994 (guias pagas em 2015+ com escritura antiga).

* `GET /api/itbi/meta` →
  `{"periodo": {"min": "2015-01-02", "max": "2026-09-07"}, "atualizado_em": "...",
    "tipos": [{"id": "apartamento", "nome": "Apartamento"}],
    "naturezas": [{"cod": 1, "nome": "Compra e venda", "n": 1490420}],
    "total": 1672712}`
* `POST /api/itbi/resumo` →
  ```json
  {"total": 1234, "valor_total": 6.1e8, "valor_mediano": 420000, "rs_m2_mediano": 7850,
   "area_mediana": 62,
   "precisao": {"lote": 1100, "quadra": 120, "aproximado": 10, "sem_coordenada": 4},
   "mensal": [{"mes": "2025-01", "n": 98, "valor_mediano": 400000, "rs_m2_mediano": 7600}],
   "por_tipo": [{"tipo": "apartamento", "n": 900, "valor_mediano": 450000,
                 "rs_m2_mediano": 8100}],
   "periodo": {"min": "2025-01-03", "max": "2026-08-30"}}
  ```
* `POST /api/itbi/pontos?limite=20000` → GeoJSON `FeatureCollection` com
  `properties: {"v": valor, "m2": rs_m2, "t": tipo, "d": "2025-03-01", "p": precisao}`,
  `id` = id da transação, e membros extras `"total"` e `"truncado"`.
* `POST /api/itbi/transacoes` com `{"consulta": {...}, "pagina": 1, "por_pagina": 50,
  "ordem": "data|valor|rs_m2|area", "direcao": "desc|asc"}` →
  `{"total": 1234, "linhas": [{"id", "data", "endereco", "complemento", "bairro", "cep",
  "tipo", "uso", "padrao", "area_construida", "area_terreno", "valor", "rs_m2", "natureza",
  "financiamento", "acc_ano", "precisao", "lon", "lat"}]}`
* `POST /api/itbi/exportar` → CSV (`;`, UTF-8 com BOM) com as linhas filtradas.
* `GET /api/itbi/transacao/{id}` → todas as colunas de uma transação.
* `GET /api/itbi/area/{tipo}/{id}` → `{"tipo", "id", "nome", "geometria": GeoJSON, "bbox"}`.
* `GET /api/itbi/tiles/{z}/{x}/{y}.mvt?data_ini=&data_fim=&tipo=&natureza=` → vector tile:
  camada `itbi_agg` (z < 14; pontos de grade com `n`, `m2`) ou `itbi` (z ≥ 14; `id`, `v`, `m2`, `t`).

## Frontend

* `web/index.html` com import map (MapLibre 6.16.0, terra-draw 1.39.0 + adaptador 1.5.0).
* `js/main.js` — roteamento por hash (`#/apagoes`, `#/itbi`), mapa único compartilhado; cada
  view exporta `mount(ctx)` / `unmount()` e reage a `ctx.onStyle(fn)` (troca de tema recria o estilo).
* Componentes: `components/autocomplete.js`, `charts.js` (SVG: colunas, linha, barras horizontais,
  com tooltip), `legend.js`, `table.js`; utilitários em `dom.js` (`el`, `fmt`), `api.js`.
* Tema escuro padrão; tokens CSS em `css/app.css`. Cores de dados: rampa sequencial única por
  métrica (`--seq-outage` laranja para apagões, `--seq-price` azul para preços), "sem dados" em
  cinza; status nunca carrega significado sozinho.
* Acessibilidade: textos de dados via `textContent`, foco visível, combobox ARIA, tabela como
  alternativa aos gráficos.
