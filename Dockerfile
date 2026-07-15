# ---- Multi-stage build for a lean GPU runtime image ----------------
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04 AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3-pip python3.10-distutils curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/bin/python3 \
    && ln -sf /usr/bin/python3 /usr/bin/python

# ---- Builder: install deps once ------------------------------------
FROM runtime-base AS builder
WORKDIR /install
COPY requirements.txt .
# CPU torch wheel is fine for the bot logic; GPU torch is installed via
# the extra index below so CUDA inference works on the runtime host.
RUN pip3 install --upgrade pip \
    && pip3 install --prefix=/install -r requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cu121

# ---- Final image ---------------------------------------------------
FROM runtime-base
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . .

# Pre-create model dir (mount your trained checkpoint here)
RUN mkdir -p models

# Healthcheck: metrics endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:9090/metrics || exit 1

EXPOSE 9090

# Graceful shutdown for SIGTERM (Docker / k8s)
STOPSIGNAL SIGTERM

CMD ["python3", "-u", "main.py"]
