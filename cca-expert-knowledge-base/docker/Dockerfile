# syntax=docker/dockerfile:1
#
# Execution environment for the CCA expert knowledge base.
# Everything (validation, indexing, retrieval, tests) runs inside this image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# poppler-utils backs the optional PDF ingestion helper.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml /app/

# CPU-only torch keeps the image ~10x smaller than the default CUDA wheel.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.1" \
    && pip install -r /app/requirements.txt

# Bake the multilingual embedding model into the image so runtime is offline-capable.
ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-small
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

COPY src /app/src
RUN pip install --no-deps -e /app

COPY . /app

CMD ["sleep", "infinity"]
