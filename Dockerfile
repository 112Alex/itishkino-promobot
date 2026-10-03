FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.runtime.lock ./
RUN pip install setuptools==80.9.0 && pip install --no-deps -r requirements.runtime.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation . && pip check

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 promobot && useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home promobot \
    && install -d -o promobot -g promobot -m 0700 /data /data/prod /data/backups \
    && install -d -m 0755 /run/promobot /app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["promobot", "--env", "/run/promobot/.env"]
CMD ["run"]
