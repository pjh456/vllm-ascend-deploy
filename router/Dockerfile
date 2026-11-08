FROM python:3.12-slim

ARG ROUTER_VERSION=0.1.17
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "vllm-router==${ROUTER_VERSION}" \
    && rm -rf /root/.cache/pip

CMD ["vllm-router", "--help"]
