# =============================================================================
# Ninaivu — production container image (build context: the repository root)
# Multi-stage build for a lightweight, secure, non-root family media server
# =============================================================================

# --- Stage 1: Build Dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    git \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/requirements.txt requirements/requirements-ai.txt ./

# Create virtualenv and install core + extras
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Torch comes from PyTorch's own CPU index; everything else comes from PyPI.
#
# Pointing the whole AI requirements file at that index does not work: it
# carries torch and little else, so open_clip_torch resolves to nothing there
# and the build fails on it. Installing torch first also keeps the CUDA build
# out of the image -- the default PyPI wheel bundles the CUDA runtime and is
# several gigabytes of nothing in a container with no GPU.
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt && \
    pip install "torch>=2.0" --index-url https://download.pytorch.org/whl/cpu && \
    pip install -r requirements-ai.txt

# --- Stage 2: Runtime Image ---
FROM python:3.12-slim AS runtime

LABEL maintainer="Ninaivu Project <ninaivu@local>" \
      version="2.0.0" \
      description="Private, self-hosted, AI-indexed family media library and archive"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NINAIVU_STATE_DIR="/data/state" \
    NINAIVU_ROOT="/data/media" \
    NINAIVU_HOST="0.0.0.0" \
    NINAIVU_PORT=5000 \
    NINAIVU_ADMIN_PORT=3000

# Install runtime system libraries for OpenCV headless and networking
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create dedicated non-root user and directories
RUN groupadd -g 1000 ninaivu && \
    useradd -u 1000 -g ninaivu -s /bin/bash -m ninaivu && \
    mkdir -p /app /data/media /data/state && \
    chown -R ninaivu:ninaivu /app /data

WORKDIR /app

# Copy application source code
COPY --chown=ninaivu:ninaivu ninaivu ./ninaivu
COPY --chown=ninaivu:ninaivu tools ./tools
COPY --chown=ninaivu:ninaivu launcher ./launcher
COPY --chown=ninaivu:ninaivu README.md ./

# Switch to non-root user
USER ninaivu:ninaivu

# Expose Family App (5000) and Admin Console (3000)
EXPOSE 5000 3000

# Volumes for media library and persistent state (SQLite DBs, thumbnails, certs)
VOLUME ["/data/media", "/data/state"]

# Built-in health check for container orchestration
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:5000/healthz || exit 1

# Launch Ninaivu
CMD ["python", "-m", "ninaivu", "/data/media", "--host", "0.0.0.0"]
