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
  chave lógica (ex.: `aneel:interrupcoes:2026`, `itbi:2025`, `geosampa:lote_cidadao`) no
  manifesto do seu domínio, `data/manifest/{geo,aneel,itbi,geosampa}.json`, com URL,
  ETag/Last-Modified e sha256 do download e do último processamento. Só reprocessa o que mudou. Ao terminar, grava
  `data/processed/.version` — a API observa esse arquivo e recarrega sozinha.
* **API** abre um DuckDB **em memória** com views sobre os Parquet (`observatorio.db.Database`).
  Nunca grava nada; a ingestão substitui arquivos de forma atômica (`storage.atomic_path`, que
  repete a troca por alguns segundos quando o Windows recusa substituir um arquivo em leitura).
  Na recarga, a conexão antiga só é fechada quando a última consulta em andamento nela termina.
* **Servidor local**: só aceita os `Host` de `allowed_hosts` (`127.0.0.1`, `localhost`, `[::1]`
  e o `host` configurado; contra DNS rebinding) e corpos de até 1 MiB em `/api` (413 acima).
* **Frontend** sem etapa de build: ES modules + import map com versões fixas (jsDelivr),
  MapLibre GL 6, terra-draw, estilo escuro do OpenFreeMap (sem chave de API).

## Organização do código

```
src/observatorio/
  config.py        Settings (pydantic-settings, prefixo OBSERVATORIO_), configure_logging()
  downloads.py     fetch() com GET condicional + Manifest (um por domínio), file_sha256()
  db.py            connect() p/ ingestão; Database (views/tabelas) p/ API; macro norm_text;
                   sql_path() (caminho como literal SQL)
  storage.py       escrita atômica (COPY … TO), gzip, touch_version
  text.py          normalização de textos (search_key, street_label/expand_street)
  cli.py           Typer: ingest geo|aneel|itbi|all, serve, status, bundle criar|baixar
  bundle.py        pacote de dados (.tar + índice com sha256) p/ publicar; download na partida
                   (BundleUpdater) e 503 enquanto instala (BundleGate) — ver docs/deploy.md
  geo/ibge.py      municípios/UFs (malha simplificada, população, places)
  aneel/           sources.py, transform.py, ingest.py, conjuntos.py (polígonos), queries.py,
                   api.py
  itbi/            sources.py, parse.py, ingest.py, geosampa.py, geocode.py, tabela.py,
                   queries.py, api.py
  api/             app.py (fábrica), deps.py (DB, require, run_query), meta.py, search.py
  web/             index.html, css/, js/ (main.js, map.js, views/, components/)
tests/             pytest (sem rede por padrão; `-m network` roda os que consultam as fontes)
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
  descartadas e só (arquivo, aba, linha) vai para o log. CPFs (com dígitos verificadores
  válidos) e CNPJs formatados nos textos livres de linhas íntegras viram `[removido]`, também
  só com (aba, linha) no log. Nada disso é persistido; a rota de detalhe devolve uma lista
  fechada de colunas (`queries.COLUNAS_TRANSACAO`).
* Busca: cada domínio grava `processed/<dominio>/places.parquet` com o esquema abaixo; a coluna
  `norm` usa a macro SQL `norm_text()`, que dá o mesmo resultado de `text.search_key` (usada nos
  termos da busca): minúsculas, sem acentos (NFD), tudo fora de `[0-9a-z]` vira espaço —
  inclusive `º`, `ª` e `²`. `dom.normalizeText` (destaque no autocompletar) segue a mesma regra.

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
  municípios com os pesos abaixo (cada município herda o DEC/FEC do conjunto). Erro típico de
  25–30% no CHI municipal: a UI sinaliza "estimado";
* pesos conjunto→município (a **base** de cada município): soma, por município, do tamanho de
  cada alimentador — o maior nº de consumidores de uma só interrupção regulatória, de origem
  interna, que não derrubou ≥ 3 alimentadores no mesmo minuto, limitado ao tamanho do conjunto.
  Não depende de quantas vezes o alimentador caiu (somar os afetados faria o FEC municipal
  repetir o do conjunto). Conjunto sem esse dado: IndQual × população; sem IndQual: os demais
  registros. Base implícita acima de 1,5× a população é cortada e o excesso vai para os outros
  municípios do conjunto (aviso no log);
* distribuidora-mês com **lacuna** (`agente_mes.lacuna`) sai do numerador e da base nas consultas;
  mês com mais de 20% da base do recorte em lacuna fica sem valor (não conta como 0);
* `transform.TRANSFORM_VERSION` fica no manifesto: mudou, a ingestão refaz o fato de todos os
  anos em disco (a partir dos brutos) e os agregados.

Tabelas (detalhes de colunas em `aneel/transform.py`):

* `fato/ano=YYYY.parquet` — interrupções limpas e unificadas (ambos os layouts);
* `conj_mun_pesos.parquet` — `conj, cod_mun, w, fonte, principal` (`fonte`: `observado` ou
  `indqual_populacao`, ver os pesos acima; `principal`: o vínculo
  responde por ≥ 10% do conjunto ou da base do município — usado para listar conjuntos e
  interrupções de um município estimado sem incluir "transbordos" marginais);
* `mun_mes.parquet` — agregado município × mês × dimensões (programada, causa, regulatório,
  agente) com `consumidores`, `chi`, `eventos` (interrupções que atingiram o município) e
  `eventos_frac` (cada evento dividido entre os municípios que atingiu — soma sem dupla
  contagem em UF/Brasil/distribuidora);
* `mun_base.parquet` — base de consumidores estimada por município × mês × agente (NumCon
  oficial quando plausível, entre 0,5× e 2× a contagem do arquivo; senão a do arquivo);
* `conj_mes.parquet` — DEC/FEC calculado vs. oficial por conjunto × mês;
* `conj_dim.parquet` — agregado conjunto × mês × dimensões (programada, causa, regulatório) com
  `consumidores`, `chi` e `eventos` (eventos distintos que atingiram o conjunto): o numerador do
  mapa por conjunto com qualquer filtro;
* `agente_mes.parquet` — completude por distribuidora × mês (lacunas conhecidas);
* `places.parquet` — distribuidoras e conjuntos (escopo `aneel`; `transform.write_places`, que
  a etapa dos polígonos também chama). Conjunto: `id` = código ANEEL, `rotulo` = nome oficial,
  `detalhe` = sigla da distribuidora (com os anos quando o mesmo nome tem dois códigos),
  `norm` inclui o código, `peso` = base; caixa do polígono ou, sem ele, dos municípios.

#### Polígonos dos conjuntos (`aneel/conjuntos.py`)

Etapa separada dentro de `ingest aneel`, depois das tabelas; incremental e **nunca derruba a
ingestão** (ArcGIS/SIGEL fora do ar: aviso e as saídas anteriores ficam). Desligue com
`OBSERVATORIO_ANEEL_CONJUNTOS=false`.

* **BDGD** (ArcGIS Online, dono `aneel_aneel`, um *File Geodatabase* zipado por distribuidora,
  título `<Nome>_<código>_<data-base>_V11_<gerado>`): vale a data-base mais recente de cada
  distribuidora. O zip (até 4 GB) é lido por **HTTP Range** (`RangeReader` + `zipfile`): fim do
  zip, tabelas de sistema do geodatabase e só os arquivos da camada `CONJ` (4 requisições,
  ~0,5–3 MB por distribuidora); o GDAL do DuckDB (`ST_Read`, OpenFileGDB) lê a pasta extraída.
  Extrato em `raw/aneel/conjuntos/bdgd/<código>.parquet` (`conj, nome, dist, base, item, geom`),
  refeito só quando o `modified` do item muda (`raw/aneel/conjuntos/index.json`).
* **SIGEL** (`SRD/Conjuntos`, base 2020): reserva, baixada só se algum conjunto dos dados não tem
  polígono de mesmo código na BDGD; renovada a cada `aneel_sigel_max_age_days` (90).
* **Vínculo** com `conj_mes`: código; senão, conjunto renumerado = mesmo nome normalizado na mesma
  distribuidora (código DIST ↔ CNPJ aprendido dos vínculos por código), desde que os dois códigos
  não apareçam no mesmo ano; ordem BDGD/código > BDGD/nome > SIGEL/código > SIGEL/nome.
* **Código substituído**: alguns códigos antigos ainda aparecem em 1–3 meses depois da
  renumeração, ao lado do sucessor de mesmo nome (ex.: 8 da Equatorial AL em 2025, 4 da COPEL em
  2026 com base de 1 consumidor); os dois teriam polígono (o antigo, do SIGEL) e se sobreporiam.
  Fica fora do mapa daquele ano o código da mesma distribuidora, mesmo nome e polígono
  sobreposto (≥ 50% do menor) presente em menos meses (empate: o de código menor). Os dados dele
  continuam na busca, no painel e no ranking.
* **Saídas**: `conjuntos.geojson(.gz)` — um polígono por código dos dados, `ST_CoverageSimplify`
  por distribuidora (tolerância 0,002°), **do maior para o menor** (um conjunto dentro de outro —
  enclaves, áreas de distribuidoras que se sobrepõem na BDGD — fica por cima e continua
  clicável), feature `id` = código e propriedades `nome, sigla, uf, anos` (texto, ex.
  `"2025 2026"`: anos em que aparece no mapa; o mapa filtra pelo ano, porque o código antigo e o
  novo podem ter o mesmo polígono) e `fonte` (`bdgd`|`sigel`); `conjuntos.parquet` — `id, nome,
  sigla, cnpj, uf, ufs, anos, anos_mapa, fonte, vinculo, data_base, lon, lat, bbox_*`, uma linha
  por conjunto dos dados (sem polígono: `fonte` e geometria nulas; `anos` = anos com dados,
  `anos_mapa` = anos em que aparece no mapa, sem os de código substituído).
* **Em dia** quando a impressão digital (versão, itens da BDGD, SIGEL, `conj_mes`,
  `conj_mun_pesos`, municípios) bate com a do manifesto (chave `aneel:conjuntos`, com
  `feicoes`, `cobertura` por ano — inclusive `substituidos` —, `baixado_bytes` e `falhas`).
  `--force` remonta sem baixar.

Medido (nov/2026, base 2025-12-31): 114 distribuidoras, **80 MB baixados** (de 31,6 GB de zips) em
~98 s; 3.170 polígonos no mapa; cobertura 2025: 3.093/3.101 conjuntos (100% dos consumidores; os
8 restantes são códigos substituídos); 2026: 3.084/3.123 (99,5% dos consumidores; 77 renumerados
ligados pelo nome; 4 substituídos; os 35 sem polígono são conjuntos criados em 2026). Os 12
polígonos do SIGEL eram todos de códigos substituídos. GeoJSON 11,5 MB (3,2 MB gzip). Reexecução
sem novidades: só a busca no ArcGIS (~2 s); a montagem sozinha leva ~7 s.

### itbi/

* `transacoes/ref_year=YYYY/ref_month=MM/part.parquet` — uma linha por guia paga
  (esquema na seção ITBI abaixo); um mês só é regravado quando o conteúdo normalizado muda
  (impressão digital por mês no manifesto). `parse.PARSER_VERSION` (4: unidade, bloco, vagas,
  edifício, valor integral/referência/atípico; 5: números de porta `E 1341`/`A 1806` não viram
  torre nem unidade) no manifesto faz a ingestão reprocessar sozinha
  quando as regras de normalização mudam.
* `geocode.parquet` — `sql, logradouro, numero, cep → lon, lat, precisao, tier` (uma linha por
  chave; junção *null-safe* com `transacoes`). `geocode.GEOCODE_VERSION` (2: tabela da API) fica
  em `itbi/.geocode-version`; versão diferente refaz a geocodificação e a tabela da API.
  `precisao` ∈ `lote`, `condominio`, `quadra`, `setor`; `tier` 1–9:

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
* `tabela.parquet` — a tabela que a API carrega como `itbi` (`itbi/tabela.py`, ~2 s; refeita
  junto com a geocodificação, ou sozinha com `tabela.build_from_outputs`): uma linha por guia de
  `transacoes` com o ponto de `geocode` (junção *null-safe*), só as colunas da API, na ordem da
  curva de Hilbert sobre a cidade (sem coordenada no fim). A API não junta nem ordena nada na
  carga (0,3 s; ~106 MiB em memória). Colunas (`tabela.COLUNAS`):

  | coluna | tipo | notas |
  |---|---|---|
  | id, sql | BIGINT | `sql` como inteiro (um por unidade: conta unidades distintas) |
  | ref_year, ref_month, data_transacao, tipo, na_planta, natureza_cod, natureza | | como em `transacoes` |
  | valor_transacao, valor_integral, valor_referencia, proporcao_pct, base_calculo | DOUBLE | `valor_integral` com 2 casas |
  | valor_atipico | BOOLEAN | nunca nulo (nulo na origem → `true`) |
  | rs_m2 | DOUBLE | 2 casas |
  | area_construida_m2, area_terreno_m2, area_m2 | DOUBLE | construída nula quando a área do IPTU é a do lote (na planta, proporção < 10%); `area_m2` = construída, ou do terreno p/ `terreno` |
  | logradouro, numero_txt, complemento, bairro, cep | VARCHAR | `numero_txt`: número para exibição (`s/n` para 0/99999/S-N) |
  | unidade, bloco, vagas, edificio | | como em `transacoes` |
  | uso, padrao, financiamento, acc_ano | | |
  | lon, lat | DOUBLE | ponto com **6 casas**; nulos sem geocodificação |
  | precisao, tier | VARCHAR, TINYINT | |
  | local | BIGINT | chave do ponto (ver "ITBI — /api/itbi"); igual para todas as guias do ponto |
  | distrito, subprefeitura | VARCHAR | código da área que contém o ponto (divisa: o menor código); 24 dos 282 mil locais ficam fora dos distritos simplificados |
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
| unidade | VARCHAR | nº da unidade no complemento (`AP 44` → `44`, `APTO 1205`, `SALA 217`, `CASA Nº 12`, `UNID RESID 9`, `AP 9A` → `9A`; zeros à esquerda removidos); também `511 TORRE 1` → `511`, `BL9-12` → `12`, `1603 A` → `1603`. Números de porta não são unidade: `E 1341` (conjunção, "1337 e 1341"; nem torre E) e `A 1806` com o nº do imóvel a até 300 ("1802 a 1806"; `A 512` no nº 4228 continua torre A, unidade 512). 98,9% dos apartamentos |
| bloco | VARCHAR | bloco/torre: do complemento (`BL 1`, `BLOCO B`, `TORRE 2`, `T 9`/`T9`/`TA` com unidade, `TR 3`, `BL. T2` → `T2`, `BLOCO II`), senão do bairro (`BLOCO 14`), senão da referência (`ED AUTUMN BL A`); só o identificador, sem zeros à esquerda; `TORR` cortado sem identificador → nulo. 38% dos apartamentos (47% desde 2022) |
| vagas | SMALLINT | nº de vagas declarado: `E 4VGS` → 4, `AP 101 4 VG` → 4, `E 2 V` → 2, `E VG`/`E BOX`/`VG 45` → 1; plural sem número (`E VGS`) ou > 20 → nulo |
| edificio | VARCHAR | nome do prédio/condomínio da referência, por extenso: prefixos `ED`/`EDIF`/`EDIFICIO` → Edifício, `COND`/`CD` → Condomínio, `RES`/`RESID` → Residencial, `CJ HAB`, `CJ RES`, `PQ RES`, `C R` (em cadeia: `CD RES AIRY` → "Condomínio Residencial Airy"), ou nomes com palavra típica (`RESIDENCE`, `TOWER`, `PARK`, `SPAZIO`…); nulo para bairros (`SAO PAULO`, `STO AMARO`), lote/matrícula e só bloco. 33% dos apartamentos |
| valor_integral | DOUBLE | declarado ÷ proporção (10–100%; em branco = 100%); proporção < 10% → o declarado (preço da unidade); > 100% ou ≤ 0 → nulo |
| valor_referencia | DOUBLE | `valor_venal_ref` (100%) quando a referência é do próprio imóvel (nem na planta, nem proporção < 10%) |
| razao_referencia | DOUBLE | declarado ÷ `valor_venal_ref_prop`, nas mesmas condições |
| valor_atipico | BOOLEAN | razão < 0,5 (garagem: < 0,2) ou > 5, ou valor integral < R$ 5 mil ou nulo |
| rs_m2 | DOUBLE | valor integral / área — área construída, ou do terreno para `terreno`; nulo para atípico, garagem, na planta, proporção fora de 10–100% ou resultado fora de R$ 100–100 mil/m² |
| possivel_duplicata | BOOLEAN | idêntica a uma linha anterior da mesma aba nas 28 colunas (a 1ª ocorrência fica `false`) |
| aba | VARCHAR | nome da aba de origem (ex.: `JAN-2026`) |

## Contratos da API

Todas as respostas são JSON (UTF-8). Valores monetários em R$, áreas em m², durações em horas,
datas ISO-8601. Erros: `{"detail": "mensagem"}`; dados ainda não ingeridos → **503**. Com
`OBSERVATORIO_BUNDLE_URL`, enquanto o primeiro pacote é baixado/instalado, `/api/*` e `/dados/*`
(menos `/api/health` e a documentação) respondem 503 com o andamento no `detail` e `Retry-After`,
e `GET /` devolve uma página de espera (200) que consulta `/api/meta` a cada 3 s e recarrega o
site quando a resposta deixa de ser esse 503.

### Comuns

* `GET /api/health` → `{status, versao, dados: [..]}`
* `GET /api/meta` → fontes e datas de atualização:
  ```json
  {"dados": ["aneel_fato", "itbi", "municipios", "..."],
   "fontes": {"itbi:2026": {"url": "https://...", "baixado_em": "2026-10-01T09:12:44+00:00",
                            "processado": true, "linhas": 155903, "meses": [1, 2, 3],
                            "snapshot": "2026-09-30"},
              "geosampa:lote_cidadao": {"url": "...", "baixado_em": "...",
                                        "processado": true, "feicoes": 1688271}},
   "carregado_em": "2026-10-01T09:20:03+00:00"}
  ```
  Uma entrada por chave dos manifestos. `processado`: o último download já foi processado.
  `linhas`, `meses`, `snapshot` e `feicoes` só aparecem nas fontes que os registram.
  `carregado_em`: quando a API carregou os dados em uso (nulo antes da 1ª carga).
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
| `conj` | — | código ANEEL do conjunto (resumo, interrupções e nível conjunto; ignorado no nível município) |

Métricas (`metrica`): `dec` (h/consumidor), `fec` (interrupções/consumidor), `chi`
(consumidor-hora), `consumidores` (consumidores afetados, soma), `eventos` (interrupções),
`duracao` (duração média por consumidor afetado, h = chi/consumidores) e, **só no nível
conjunto**, `dec_oficial`/`fec_oficial` (soma dos indicadores oficiais mensais; exigem
`criterio=regulatorio`, `programadas=true` e nenhuma `causa` — senão **422**).

Nível (`nivel`, em `mapa` e `ranking`): `municipio` (padrão) ou `conjunto`. Por conjunto nada é
estimado: numerador do `conj_dim` sobre a base do `conj_mes`, mês a mês, com as mesmas regras de
lacuna (um conjunto-mês está todo em lacuna ou não; o oficial não depende das lacunas). O
oficial do período só sai se todos os meses do conjunto o têm.

* `GET /api/aneel/meta` →
  ```json
  {"anos": [{"ano": 2026, "meses": [1,2,3,4,5,6,7], "gerado_em": "2026-09-04",
             "municipio_estimado": false}],
   "causas": ["Próprias do sistema", "Meio ambiente", "..."],
   "metricas": [{"id": "dec", "nome": "DEC", "unidade": "h",
                 "descricao": "Duração equivalente de interrupção por consumidor",
                 "niveis": ["municipio", "conjunto"]}],
   "lacunas": [{"agente": "COPEL-DIS", "cnpj": "...", "ano": 2025, "mes": 6,
                "cobertura": 0.0}],
   "conjuntos": {"poligonos": 3170, "sigel": 0, "sem_poligono": 35, "substituidos": 12,
                 "data_base": "2025-12-31"}}
  ```
  `conjuntos` é nulo sem `conjuntos.parquet` (a UI desliga o mapa por conjunto). `poligonos` e
  `sigel` contam os conjuntos que aparecem no mapa de algum ano; `substituidos`, os que têm
  polígono mas ficam fora do mapa (código substituído, ver "Polígonos dos conjuntos").
* `GET /api/aneel/mapa?{filtro}&metrica=dec&nivel=municipio|conjunto` → valores por município
  (código IBGE) ou por conjunto (código ANEEL) para o coroplético:
  ```json
  {"metrica": "dec", "unidade": "h", "estimado": true,
   "valores": {"3550308": 5.54, "...": 0.0},
   "quebras": [0.5, 1.2, 2.0, 3.4, 5.9, 10.1],
   "pouca_base": ["1100015"],
   "lacuna": ["1100023"],
   "n": 5521,
   "nivel": "municipio",
   "sem_geometria": []}
  ```
  `lacuna` = municípios (conjuntos) com mais de 20% da base do período em meses sem dados da
  distribuidora (valor parcial: a UI marca, ficam fora das quebras e do ranking; vazio nas
  métricas oficiais). `quebras` = 6 limites (quantis 1/7…6/7) → 7 classes de cor.
  `pouca_base` = base < 2.000 consumidores (DEC/FEC instáveis; a UI esmaece). `sem_geometria`
  (nível conjunto) = conjuntos com valor que não aparecem no mapa do ano: sem polígono ou com o
  código substituído (`anos_mapa` sem o ano). `estimado` é sempre `false` por conjunto. O nível
  conjunto exige `conjuntos.parquet` (503 sem ele).
* `GET /api/aneel/conjuntos.geojson` → polígonos dos conjuntos (gzip; `Cache-Control:
  no-cache` + `ETag`: com `If-None-Match` igual, **304** sem corpo — o arquivo tem ~3 MB
  comprimido; 503 antes da 1ª ingestão com polígonos).
* `GET /api/aneel/resumo?{filtro}&serie=periodo|ano` → painel do escopo (Brasil, UF, município,
  distribuidora ou conjunto — `conj`); com `serie=ano`, `mensal` traz o ano inteiro (os KPIs
  seguem o período):
  ```json
  {"escopo": {"tipo": "municipio", "id": "3550308", "nome": "São Paulo – SP", "uf": "SP",
              "bbox": [-46.83, -24.01, -46.36, -23.36]},
   "estimado": false,
   "kpis": {"dec": 3.1, "fec": 1.9, "chi": 2.1e7, "consumidores": 1.3e7,
            "eventos": 48211, "duracao": 1.6, "consumidores_base": 7.1e6,
            "dec_oficial": 3.0, "fec_oficial": 1.8},
   "meses_lacuna": [],
   "mensal": [{"mes": 1, "dec": 0.4, "fec": 0.3, "chi": 2.9e6, "consumidores": 2.1e6,
               "eventos": 6100}],
   "causas": [{"causa": "Meio ambiente", "chi": 9.0e6, "pct": 0.43}],
   "distribuidoras": [{"cnpj": "...", "sigla": "ENEL SP", "chi": 2.0e7, "pct": 0.95}],
   "conjuntos": [{"id": 12345, "nome": "...", "sigla": "ENEL SP", "dec": 4.2,
                  "dec_oficial": 4.1, "fec": 2.1, "fec_oficial": 2.0}]}
  ```
  `meses_lacuna`: meses do período com > 20% da base em lacuna — ficam com valores nulos em
  `mensal` e os KPIs somam só os demais (DEC/FEC parciais; o oficial continua o do período).
  `dec_oficial`/`fec_oficial` só quando houver indicador oficial comparável (nulo caso contrário);
  em `mensal` eles aparecem por mês no conjunto e em Brasil/UF/distribuidora (média ponderada
  pela base; nulos no município). `escopo.uf`: UF do município/da UF/principal do conjunto;
  `escopo.bbox` (`[w, s, e, n]`): caixa para enquadrar (polígono do conjunto, município ou UF;
  nula nos demais). `distribuidoras` e `conjuntos` podem vir vazios conforme o escopo (no
  conjunto, `conjuntos` é vazio e `causas`/`distribuidoras` vêm do `conj_dim`).
* `GET /api/aneel/ranking?{filtro}&metrica=dec&ordem=desc&limite=20&min_consumidores=2000&nivel=municipio`
  (sem os municípios/conjuntos em `lacuna`) →
  `[{"cod": "3550308", "nome": "São Paulo", "uf": "SP", "valor": 3.1, "consumidores_base": 7.1e6,
  "sigla": null}]` — com `nivel=conjunto`, `cod` = código do conjunto, `nome` = nome oficial e
  `sigla` = distribuidora (não precisa dos polígonos).
* `GET /api/aneel/interrupcoes?{filtro}&limite=20` → maiores interrupções (por CHI) do escopo
  (com `conj`, só as do conjunto):
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
 "precisao": "quadra",
 "atipicos": "excluir"}
```

`area` (opcional, um de):

* `{"tipo": "circulo", "lon": -46.6, "lat": -23.5, "raio_m": 500}` (raio 50 m … 20 km);
* `{"tipo": "poligono", "geometria": {GeoJSON Polygon|MultiPolygon}}` (desenhado; ≤ 5.000
  vértices e ≤ 20 partes, também depois da correção de autointerseções);
* `{"tipo": "distrito" | "subprefeitura", "id": "..."}`.

`precisao`: `lote` (só lote/condomínio exato), `quadra` (padrão: até centroide de quadra) ou
`qualquer`. `tipos`/`naturezas` vazios ou nulos = todos. Datas filtram `data_transacao`.
`valor_min`/`valor_max` filtram o **valor integral** (abaixo). `area_min`/`area_max` usam a área
construída (terrenos: a do terreno); nas unidades na planta e nas frações < 10% de um lote a
área do IPTU é a do lote inteiro, então a guia fica sem área (fora do filtro, da mediana e no
fim da ordenação por área).

`atipicos` (padrão `excluir`): `excluir` tira as guias com `valor_atipico`, `incluir` mantém
todas, `somente` mostra só elas. Vale em **todas** as saídas: resumo, pontos, local,
transações, exportação, distritos e tiles (parâmetro `atipicos=` na query string).

Valores (colunas de `transacoes`, ver o esquema):

* `valor` = **declarado** pelo contribuinte (`valor_transacao`);
* `valor_integral` = declarado levado a 100% do imóvel (÷ proporção transmitida de 10–100%;
  proporção em branco = 100%; abaixo de 10% o declarado já é o preço da unidade; proporção
  acima de 100% → nulo). Medianas de valor e o filtro de valor usam este;
* `valor_referencia` = valor venal de referência da Prefeitura (100%), nulo quando a referência
  é a do lote-mãe (na planta ou proporção < 10%);
* `valor_atipico` = declarado < 0,5× (vagas de garagem: < 0,2×) ou > 5× a referência
  proporcional, ou valor integral < R$ 5 mil ou nulo; sem referência só valem as duas últimas
  regras. Ex.: R. Inácio Monteiro, 600 (referência ~R$ 125–141 mil): R$ 27.213,63, R$ 11 mil,
  R$ 0,01 e R$ 7 mil são atípicos (cessões de saldo de COHAB, valores simbólicos). 7,6% das guias
  (8,2% das compras e vendas com proporção de 10–100%; 4,5% dos apartamentos, 10,6% das vagas);
* `rs_m2` = valor integral ÷ área; nulo para atípicos, garagem, na planta, proporção fora de
  10–100% e fora de R$ 100–100 mil/m².

`local` (chave de um ponto geocodificado: lote ou condomínio) = `dx * 2^21 + dy`, com `dx`/`dy`
em milionésimos de grau a partir de (-48°, -25°) sobre `lon`/`lat` com 6 casas — inteiro
< 2^42 (exato em JavaScript); `tabela.ponto_do_local` decodifica. Todas as guias de um
condomínio têm o mesmo `local` (as 64 da R. Inácio Monteiro, 600, por exemplo). É estável entre
recargas enquanto a geocodificação do ponto não mudar.

Observações para a implementação: `natureza` código 5 tem dois rótulos ao longo dos anos
("Excesso de meação…" até 2020, "Adjudicação compulsória" a partir de 2021) — agrupe por
(código, rótulo); `data_transacao` começa em 1994 (guias pagas em 2015+ com escritura antiga).

* `GET /api/itbi/meta` →
  `{"periodo": {"min": "1994-06-24", "max": "2026-09-07", "pago_ate": "2026-08-31",
                 "completo_ate": "2026-07-31"}, "atualizado_em": "...",
    "tipos": [{"id": "apartamento", "nome": "Apartamento"}],
    "naturezas": [{"cod": 1, "nome": "Compra e venda", "n": 1490420}],
    "total": 1672712,
    "atipicos": {"n": 127079, "razao_min": 0.5, "razao_min_garagem": 0.2, "razao_max": 5.0,
                 "valor_min": 5000.0}}`

  `min`/`max` = datas de transação; `pago_ate` = fim do último mês de pagamento publicado;
  `completo_ate` = fim do mês anterior a ele (~20% das guias são pagas no mês seguinte ao da
  transação: os meses depois de `completo_ate` ainda vão crescer e a UI os marca como parciais).
  `atipicos` = quantas guias são atípicas no total e os limites da regra (texto de ajuda).
* `POST /api/itbi/resumo` →
  ```json
  {"total": 1234, "valor_total": 6.1e8, "valor_mediano": 420000, "rs_m2_mediano": 7850,
   "area_mediana": 62, "atipicos_ocultos": 98,
   "precisao": {"lote": 1100, "quadra": 120, "aproximado": 10, "sem_coordenada": 4},
   "mensal": [{"mes": "2025-01", "n": 98, "valor_mediano": 400000, "rs_m2_mediano": 7600}],
   "por_tipo": [{"tipo": "apartamento", "n": 900, "valor_mediano": 450000,
                 "rs_m2_mediano": 8100}],
   "periodo": {"min": "2025-01-03", "max": "2026-08-30"},
   "comparacao": {"cidade_rs_m2_mediano": 3917.79, "variacao_12m": 0.04,
                  "rs_m2_12m": 4830.04, "rs_m2_12m_anterior": 4644.27,
                  "variacao_fim": "2026-07-31"}}
  ```
  `valor_mediano` = mediana do valor integral; `valor_total` = soma do **declarado**.
  `atipicos_ocultos` = guias que atendem aos filtros mas ficaram de fora por serem atípicas
  (só com `atipicos: "excluir"`; 0 nos outros modos). `mensal`/`por_tipo` só trazem grupos com
  `n > 0`. `comparacao`:
  * `cidade_rs_m2_mediano` = mediana de R$/m² com os mesmos filtros **sem a área** (sem área,
    é a própria `rs_m2_mediano`; em cache até a próxima recarga);
  * `variacao_12m` = `rs_m2_12m / rs_m2_12m_anterior − 1` (fração: 0,04 = +4%), medianas de
    R$/m² da seleção (com a área) nos 12 meses completos que terminam em `variacao_fim` contra
    os 12 anteriores; `variacao_fim` = `completo_ate`, ou o último fim de mês ≤ `data_fim` se
    ela for anterior; `data_ini` **não** limita as janelas. Nula com menos de 20 guias com
    R$/m² em uma das janelas (as duas medianas continuam saindo).
* `POST /api/itbi/pontos?limite=20000` → GeoJSON `FeatureCollection` com **uma feição por
  local** (`limite` 1–50.000 conta locais):
  ```json
  {"type": "FeatureCollection", "total": 18314, "locais": 1085, "truncado": false,
   "features": [{"type": "Feature", "id": 3333934147165,
                 "geometry": {"type": "Point", "coordinates": [-46.410257, -23.564771]},
                 "properties": {"n": 46, "m2": 2072.58, "v": 128500.0, "d": "2026-08-04",
                                "t": "apartamento", "p": "condominio", "u": 40,
                                "e": "Residencial Jardim dos Ypes"}}]}
  ```
  `id` = `local`; `n` = guias no local; `m2` = mediana de R$/m²; `v` = mediana do valor
  integral; `d` = data da guia mais recente; `t` = tipo mais frequente (empate: qualquer um);
  `p` = precisão; `u` = unidades distintas (SQLs; nas guias na planta, que estão todas sob o SQL
  do lote-mãe, bloco + unidade do complemento — `queries.UNIDADE_SQL`); `e` = edifício/condomínio mais frequente
  (pode ser nulo). `total` = guias filtradas **com coordenada**; `locais` = locais com ao menos
  uma guia; `truncado` = havia mais locais que `limite` — vêm os de guia mais recente
  (ordem: `d` desc, `local` desc).
* `POST /api/itbi/local` com `{"consulta": {...}, "local": 3333934147165, "pagina": 1,
  "por_pagina": 50, "ordem": "data|valor|rs_m2|area|unidade|bloco", "direcao": "desc|asc"}` →
  todas as guias do local com os filtros da consulta **menos a área** (o local já é a área):
  ```json
  {"local": 3333934147165, "lon": -46.410257, "lat": -23.564771, "precisao": "condominio",
   "endereco": "Rua Inacio Monteiro, 600", "edificio": "Residencial Jardim dos Ypes",
   "distrito": {"id": "25", "nome": "Cidade Tiradentes"},
   "subprefeitura": {"id": "31", "nome": "Cidade Tiradentes"}, "guias": 64,
   "resumo": {"n": 46, "valor_mediano": 128500.0, "rs_m2_mediano": 2072.58,
              "periodo": {"min": "2010-09-08", "max": "2026-08-04"},
              "unidades": 40, "blocos": 17, "atipicos_ocultos": 18},
   "total": 46,
   "linhas": [{"id": 202608002839, "data": "2026-08-04", "endereco": "Rua Inacio Monteiro, 600",
               "complemento": "AP 21", "unidade": "21", "bloco": "14", "vagas": null,
               "tipo": "apartamento", "area": 62.0, "valor": 141000.0,
               "valor_integral": 141000.0, "proporcao": 100.0, "valor_referencia": 140782.0,
               "base_calculo": 141000.0, "rs_m2": 2274.19, "natureza": "Compra e venda",
               "financiamento": null, "valor_atipico": false, "na_planta": false}]}
  ```
  `guias` = todas as guias do local, sem filtros; `endereco`/`edificio` = os mais frequentes do
  local (sem filtros); `resumo` segue os filtros e `resumo.valor_mediano` usa só guias
  **típicas** (mesmo com `atipicos: "incluir"`); `unidades` = unidades distintas como em `u` de
  `/pontos` (um lançamento na planta de 698 apartamentos não sai "1"); `total` = linhas
  paginadas. `area` = área usada no R$/m² (construída; terreno p/ terrenos; nula na planta).
  Ordem `unidade`/`bloco` é natural (2 < 11 < 101 < 101A < B), nulos no fim, empate pelo `id`.
  Local inexistente → 404; `local` fora de 0…2^42−1 → 422.
* `GET /api/itbi/endereco?logradouro=R%20INACIO%20MONTEIRO&numero=600` → o local de um número
  da rua ("Rua X, 600" na busca), entre **todas** as guias geocodificadas dela (sem filtros):
  ```json
  {"local": 3333934147165, "lon": -46.410257, "lat": -23.564771, "numero": 600,
   "exato": true, "guias": 64, "endereco": "Rua Inacio Monteiro, 600"}
  ```
  Número sem guias: o mais próximo com guias (`exato: false`), até 300 de distância (empate: o
  de mais guias); nada perto → 404. `logradouro` como na fonte (id da busca; maiúsculas e
  espaços normalizados); `numero` 1–99.999. ~10 ms.
* `POST /api/itbi/distritos` com `{"consulta": {...}, "nivel": "distrito|subprefeitura"}`
  (corpo opcional; a área da consulta é ignorada) → coroplético:
  ```json
  {"nivel": "distrito",
   "areas": [{"id": "45", "nome": "Jardim Paulista", "n": 42673, "rs_m2_mediano": 6611.99,
              "valor_mediano": 600000.0, "variacao_12m": 0.0838}],
   "quebras": [2587.63, 2856.85, 3187.49, 3324.02, 3746.2, 4717.97],
   "n_min": 20}
  ```
  Todas as áreas do nível (ordem de `id`), com `n` = 0 e medianas nulas onde não há guias.
  `variacao_12m` como no resumo (janelas por `data_fim`, mínimo de 20 guias por janela).
  `quebras` = 6 limites (quantis k/7) de `rs_m2_mediano` das áreas com `n ≥ n_min` → 7 classes
  (vazio com menos de 7 áreas). A área de cada guia é a do seu ponto (calculada na ingestão).
* `GET /api/itbi/areas.geojson?tipo=distrito|subprefeitura` (padrão `distrito`) →
  `FeatureCollection` das áreas: `id` (texto, ex. `"37"`) também como *feature id*,
  `properties: {"id", "nome"}`, coordenadas com 6 casas. Vai comprimido (`Content-Encoding:
  gzip`: distritos 0,84 MB → 226 KB, subprefeituras 0,58 MB → 143 KB) quando o cliente aceita
  gzip; `Cache-Control:
  public, max-age=86400`; montado uma vez por carga de dados. Use `promoteId: "id"` no MapLibre.
* `POST /api/itbi/transacoes` com `{"consulta": {...}, "pagina": 1, "por_pagina": 50,
  "ordem": "data|valor|rs_m2|area", "direcao": "desc|asc"}` (`valor` ordena pelo valor
  integral) →
  `{"total": 1234, "linhas": [{"id", "data", "endereco", "edificio", "bloco", "unidade",
  "vagas", "complemento", "bairro", "cep", "tipo", "na_planta", "uso", "padrao",
  "area_construida", "area_terreno", "valor", "proporcao", "valor_integral",
  "valor_referencia", "valor_atipico", "rs_m2", "natureza", "financiamento", "acc_ano",
  "precisao", "local", "lon", "lat"}]}` (`local` nulo sem coordenada).
* `POST /api/itbi/exportar` → CSV (`;`, vírgula decimal, UTF-8 com BOM) com as linhas
  filtradas, colunas: `ID; Data da transação; Endereço; Edifício/condomínio; Bloco/torre;
  Unidade; Vagas; Complemento; Bairro; CEP; Tipo; Na planta; Uso (IPTU); Padrão (IPTU); Área
  construída (m²); Área do terreno (m²); Valor declarado (R$); Proporção transmitida (%); Valor
  integral (R$, 100%); Valor venal de referência (R$); Valor atípico; R$/m²; Natureza da
  transação; Financiamento; Ano de construção; Precisão da localização; Longitude; Latitude`.
  No máximo 500 mil linhas (as mais recentes); cabeçalhos `X-Total-Linhas`,
  `X-Linhas-Exportadas`, `X-Limite-Linhas`, `X-Truncado`. CEP sai como `01452-000`, `Na planta`
  e `Valor atípico` como `sim`/`não`, a área construída fica vazia quando a do IPTU é a do lote,
  e textos que começam com `= + - @` ganham apóstrofo (proteção contra fórmulas).
* `GET /api/itbi/transacao/{id}` → as colunas do esquema de `transacoes` de uma guia (lista
  fechada `COLUNAS_TRANSACAO`, inclusive `unidade`, `bloco`, `vagas`, `edificio`,
  `valor_integral`, `valor_referencia`, `razao_referencia`, `valor_atipico`), com `lon`, `lat`,
  `precisao`, `tier`, `local`, `distrito`, `subprefeitura` (códigos) e `endereco`.
* `GET /api/itbi/area/{tipo}/{id}` → `{"tipo", "id", "nome", "geometria": GeoJSON, "bbox"}`.
* `GET /api/itbi/tiles/{z}/{x}/{y}.mvt?data_ini=&data_fim=&tipo=&natureza=&valor_min=&valor_max=&area_min=&area_max=&logradouro=&cep=&precisao=&atipicos=`
  → vector tile (z 9–18; `tipo`/`natureza` repetíveis; mesmos filtros de `ConsultaItbi`, sem
  área). Camada `itbi_agg` (z < 14): **quadrados** de uma grade de 128 × 128 unidades do tile
  (32 × 32 células; ~150 m em z13, ~2,4 km em z9), polígonos que cobrem a célula inteira, com
  `n` (guias) e `m2` (mediana de R$/m²) — desenhe como `fill` (mosaico tipo raster). Camada
  `itbi` (z ≥ 14): uma feição (ponto) por **local** com `local`, `n` (guias), `m2` (mediana do
  local) e `id`, `v` (valor integral), `t`, `d` da guia mais recente. Propriedades nulas são
  omitidas; não há *feature id* (use `local`). Corpo vazio = nada no tile.

Tempos medidos nos dados reais (1,67 mi de guias, 282 mil locais; servidor local, cache frio
dos caches por carga): resumo da cidade 150 ms (círculo de 500 m 28 ms, distrito 45 ms),
pontos da cidade 194 ms (círculo de 1 km 51 ms), local 30 ms, distritos 66–78 ms,
`areas.geojson` 5 ms, tiles 15–63 ms, transações 12–19 ms. Carga da tabela: 0,3 s (pico de
memória 1,0 GB contra 1,45 GB da junção na carga que havia antes), ~106 MiB em memória.

## Frontend

* `web/index.html` com import map (MapLibre 6.16.0, terra-draw 1.39.0 + adaptador 1.5.0).
* `js/main.js` — roteamento por hash (`#/apagoes`, `#/itbi`), mapa único compartilhado; cada
  view exporta `mount(ctx)` / `unmount()` e reage a `ctx.onStyle(fn)` (troca de tema recria o estilo).
* `views/apagoes.js` — "Mapa por: Municípios | Conjuntos elétricos" (`nivel=conjunto` e
  `conj=<código>` na URL; um `conj` abre o mapa por conjunto, salvo `nivel=municipio`). Os
  polígonos dos conjuntos (`/api/aneel/conjuntos.geojson`) só são carregados quando o nível
  conjunto é usado (link aberto direto nele: as camadas esperam o estilo do mapa, via
  `onStyle`); as camadas dos dois níveis têm a mesma estrutura
  (preenchimento, respiro, tracejado de instável, hover, seleção) e só as do nível atual ficam
  visíveis; nos conjuntos, um filtro pela propriedade `anos` mostra só os do ano escolhido. As
  métricas oficiais só aparecem no nível conjunto e travam critério/programadas/causas na
  definição oficial. O enquadramento de um escopo aberto por link usa `escopo.bbox` do resumo
  (em zoom baixo os tiles omitem polígonos pequenos).
* `views/itbi.js` — sem região, o mapa muda com o zoom: abaixo de z12 o coroplético dos distritos
  (`/areas.geojson` + `/distritos`, feature-state; "Cor do mapa": R$/m² ou transações, `cor=n`),
  de z12 a z14 a grade `itbi_agg` (nº de guias, escala log. calibrada pelo p90 por zoom), de z14
  em diante um ponto por endereço (camada `itbi`: raio ∝ √n com teto, rótulo com n, cor pelo R$/m²
  em faixas de ½× a 2× a mediana da cidade); transição cruzada de ±0,35 zoom, legenda por nível.
  Com região, os pontos de `/pontos` (faixas por quantil da região). Clique num endereço (ou numa
  linha da tabela) abre a ficha (`components/location-sheet.js`, `POST /local`, `loc=<local>` na
  URL); "Rua X, 600" na busca procura o número entre as guias da rua (`/endereco`) e abre a
  ficha do endereço. Filtro de atípicos `at=incluir|somente`.
* Componentes: `components/autocomplete.js`, `charts.js` (SVG: colunas, linha, barras horizontais,
  com tooltip), `legend.js`, `table.js` (`render` devolve texto ou nó), `location-sheet.js` (ficha
  do endereço do ITBI: painel lateral, folha inferior no celular); utilitários em `dom.js` (`el`, `fmt`, `field`, `kpiTile`,
  `showError`, `debounce`…), `api.js` (`api`, `latest`, `isAbort`). Nomes de métricas (ANEEL) e de
  tipos de imóvel (ITBI) vêm dos `/meta`; o frontend guarda só os ids, a formatação e os textos de
  ajuda.
* Tema escuro padrão; tokens CSS em `css/app.css`. Cores de dados: rampa sequencial única por
  métrica (`--seq-outage` laranja para apagões, `--seq-price` azul para preços), "sem dados" em
  cinza; status nunca carrega significado sozinho.
* Acessibilidade: textos de dados via `textContent`, foco visível, combobox ARIA; filtros
  recriados devolvem o foco ao mesmo controle (`dom.preserveFocus`); um `role="status"` por view
  anuncia só o resumo de cada atualização (o painel não é região viva) e erros saem com
  `role="alert"`; gráficos de colunas com um só ponto de tabulação (setas, Home/End,
  Enter/Espaço) e valor anunciado; tabela como alternativa aos gráficos.
