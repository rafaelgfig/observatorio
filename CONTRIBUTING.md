# Como contribuir

Obrigado pelo interesse! Problemas e ideias entram como *issue*; mande o link da tela, que já
guarda os filtros. Correções e melhorias entram como *pull request* para `main`.

## Ambiente

```bash
uv sync                        # Python 3.13 + dependências (o uv instala o Python sozinho)
uv run pre-commit install      # ruff + mypy a cada commit
npm ci                         # só para a checagem de tipos do frontend (Node 22+)
uv run observatorio ingest all # dados reais (~1,4 GB de downloads); ou `bundle baixar <url>`
uv run observatorio serve --reload
```

## Checagens

São as mesmas do CI (`.github/workflows/ci.yml`) e precisam passar antes do *pull request*. No
Windows, rode o laço do `node` no Git Bash.

```bash
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy
npm run typecheck
for f in $(find src/observatorio/web/js -name '*.js'); do node --input-type=module --check < "$f"; done
```

* Os testes não acessam as fontes de dados. `uv run pytest -m network` roda os que consultam as
  fontes reais, e `uv run pytest --cov` mede a cobertura (o piso é 95%).
* O CI também constrói a imagem Docker (sem publicar) e confere que o servidor sobe. Para
  conferir localmente, use `docker build -t observatorio .`.

## Convenções

* **Python**: tipagem estrita (`mypy --strict`), `ruff` para lint e formatação, linhas de até
  100 colunas. Docstrings e mensagens em português; identificadores de infraestrutura em inglês
  e termos do domínio em português (`municipio`, `consumidores`, `logradouro`). O SQL segue as
  regras do DuckDB 2.0 descritas em [docs/arquitetura.md](docs/arquitetura.md#convenções), e
  valores vindos do usuário entram sempre como parâmetros (`?`/`$nome`), nunca interpolados.
* **Frontend**: ES modules sem etapa de build. Todo arquivo começa com `// @ts-check` e tipa
  com JSDoc. O DOM é montado com `el()` e os dados entram via `textContent`, nunca com
  `innerHTML`. Textos da interface em português do Brasil, tema escuro por padrão e cores pelos
  tokens de `css/app.css`. As versões do MapLibre e do terra-draw ficam no import map do
  `index.html` e no `package.json`: ao trocar uma, troque nas duas.
* **Arquivos**: UTF-8, fim de linha LF e linha final (`.editorconfig`, `.gitattributes`).
* **Documentação**: quem muda um contrato da API ou um esquema de `data/processed` atualiza
  [docs/arquitetura.md](docs/arquitetura.md). Variáveis de ambiente, comandos da CLI ou a
  imagem Docker são descritos em [docs/deploy.md](docs/deploy.md).
* **Dados pessoais**: nunca grave nem exponha nomes das partes, CPF ou CNPJ (veja "Dados
  pessoais" em `docs/arquitetura.md`).
* **Commits** no estilo `tipo(escopo): resumo`, em português: `feat(itbi): …`,
  `fix(aneel): …`, `docs: …`.

## Capturas de tela do README

Ficam em `docs/img/` e são feitas com os dados reais, em 1440×900 com escala 1, no tema
escuro, depois que o mapa termina de carregar. Os endereços de cada uma:

| arquivo | tela |
|---|---|
| `apagoes.png` | `#/apagoes` |
| `conjuntos.png` | `#/apagoes?nivel=conjunto&m=dec_oficial&uf=SP` |
| `itbi.png` | `#/itbi` |
| `itbi-ficha.png` | `#/itbi?loc=<local>`, com o `local` de `/api/itbi/endereco?logradouro=R%20INACIO%20MONTEIRO&numero=600` |

Para que cada uma fique abaixo de ~400 KB, reduza para 256 cores:

```bash
uv run --no-project --with pillow python -c "import sys; from PIL import Image; [Image.open(p).convert('RGB').quantize(256, dither=Image.Dither.FLOYDSTEINBERG).save(p, optimize=True) for p in sys.argv[1:]]" docs/img/*.png
```
