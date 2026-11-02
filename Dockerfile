# Imagem do observatório (API + site) para Hugging Face Spaces, Cloud Run ou qualquer VPS.
# Os dados não fazem parte do código: na partida o servidor baixa o pacote publicado em
# OBSERVATORIO_BUNDLE_URL (veja docs/deploy.md). Com --build-arg BUNDLE_URL=… (e BUNDLE_REV) o
# pacote já vem dentro da imagem (sem download na partida).
#
#   docker build -t observatorio .
#   docker run --rm -p 7860:7860 \
#     -e OBSERVATORIO_BUNDLE_URL=https://github.com/<usuario>/<repo>/releases/download/dados/observatorio-dados.tar \
#     observatorio

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.7.2

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ------------------------------------------------------------------ dependências + pacote
FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# dependências primeiro: esta camada só é refeita quando o uv.lock muda
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# ------------------------------------------------------------------ execução
FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="observatorio" \
      org.opencontainers.image.description="Apagões (ANEEL) e ITBI (São Paulo) em mapa: API FastAPI + DuckDB e site estático."

# usuário sem privilégios; uid 1000 é o que o Hugging Face Spaces usa
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    PORT=7860 \
    OBSERVATORIO_HOST=0.0.0.0 \
    OBSERVATORIO_DATA_DIR=/home/app/data \
    OBSERVATORIO_ALLOWED_HOSTS='["127.0.0.1", "localhost", "*.hf.space", "*.run.app"]' \
    FORWARDED_ALLOW_IPS="*"

USER app
WORKDIR /home/app
# já do usuário app: um volume montado aqui (docker run -v …:/home/app/data) herda o dono
RUN mkdir -p /home/app/data

# extensão spatial do DuckDB instalada agora: em execução não há download de extensions.duckdb.org
RUN python -c "import duckdb; duckdb.sql('INSTALL spatial'); duckdb.sql('LOAD spatial')"

# opcional: dados embutidos na imagem (Cloud Run sem disco persistente, builds offline…).
# A URL da Release é sempre a mesma: passe também BUNDLE_REV com um valor novo a cada pacote
# (ex.: --build-arg BUNDLE_REV=$(date +%s)), senão o cache do Docker reaproveita o pacote antigo.
ARG BUNDLE_URL=""
ARG BUNDLE_REV=""
RUN if [ -n "$BUNDLE_URL" ]; then \
      echo "pacote de dados: $BUNDLE_URL ${BUNDLE_REV}" && observatorio bundle baixar "$BUNDLE_URL"; \
    fi

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '7860') + '/api/health', timeout=4)" || exit 1

# a porta vem de PORT (7860 no Hugging Face; o Cloud Run define 8080)
CMD ["observatorio", "serve"]
