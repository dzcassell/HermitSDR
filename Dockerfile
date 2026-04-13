# HermitSDR - GPU-accelerated Hermes Lite 2 Web Client
# Base: NVIDIA CUDA for CuPy FFT acceleration
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

LABEL maintainer="dzcassell" \
      description="GPU-accelerated web client for Hermes SDR compatible radios" \
      version="0.4.1"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HERMITSDR_PORT=5000

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermitsdr

# Python deps (install before code for layer caching)
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Application code
COPY hermitsdr/ hermitsdr/
COPY tests/ tests/
COPY README.md .

EXPOSE ${HERMITSDR_PORT}

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${HERMITSDR_PORT}/api/version')" || exit 1

ENTRYPOINT ["python3", "-m", "hermitsdr"]
CMD ["--host", "0.0.0.0", "--port", "5000"]
