FROM golang:1.24.4-bookworm@sha256:10f549dc8489597aa7ed2b62008199bb96717f52a8e8434ea035d5b44368f8a6 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential && rm -rf /var/lib/apt/lists/*
RUN git clone --branch v0.2.19 --depth 1 https://github.com/amnezia-vpn/amneziawg-go.git /build/go
WORKDIR /build/go
RUN test "$(git rev-parse HEAD)" = 1cc94272ca8e9e223a5fe76382f5880f09d3c12d && go mod verify && make
RUN git clone --branch v1.0.20260618 --depth 1 https://github.com/amnezia-vpn/amneziawg-tools.git /build/tools && test "$(git -C /build/tools rev-parse HEAD)" = 4cdc357c68b39a0d1e19417b7f03d604c1e1b4cf && make -C /build/tools/src

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update && apt-get install -y --no-install-recommends python3 iproute2 iptables dante-server ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/go/amneziawg-go /usr/local/bin/amneziawg-go
COPY --from=builder /build/tools/src/wg /usr/local/bin/awg
COPY deploy/vpn/entrypoint.py /app/entrypoint.py
COPY scripts/check-awg-config.py /app/check-awg-config.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LOG_LEVEL=silent
ENTRYPOINT ["python3", "/app/entrypoint.py"]
