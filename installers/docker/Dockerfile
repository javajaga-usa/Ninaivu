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
#
# The image model is optional: `--build-arg WITH_AI=1` adds it (about 2 GB).
# Without it the image is small and search is the light kind, as on any
# other install until somebody adds search by description.
ARG WITH_AI=0
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt && \
    if [ "$WITH_AI" = "1" ]; then \
      pip install "torch>=2.0" --index-url https://download.pytorch.org/whl/cpu && \
      pip install -r requirements-ai.txt ; \
    fi

# --- Stage 2: Runtime Image ---
FROM python:3.12-slim AS runtime

# The published image's version comes from the release workflow's labels; a
# local build says what it was given, or "dev", never a number that has gone
# stale (this said 0.1.0 for the whole of 1.0).
ARG NINAIVU_VERSION=dev
LABEL maintainer="Ninaivu Project <ninaivu@local>" \
      version="${NINAIVU_VERSION}" \
      description="Private, self-hosted, AI-indexed family media library and archive"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NINAIVU_STATE_DIR="/data/state" \
    NINAIVU_HOST="0.0.0.0" \
    NINAIVU_PORT=5000 \
    NINAIVU_ADMIN_PORT=3000

# Install runtime system libraries for OpenCV headless and networking, and
# ffmpeg: without it a guest or a share link cannot be given a video at all,
# because the copy without the place it was filmed cannot be made.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
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
# --no-mdns: a container on Docker's bridge network cannot announce a name to
# the home network anyway, and without it the family listener would also
# answer as ninaivu-admin.local — the console, on the port compose publishes
# to the whole network, when compose keeps the console's own port local.
# The console listens on the container's own network so the port mapping in
# docker-compose.yml (127.0.0.1:3000 on the host) can reach it; making the
# first administrator from outside the container asks for the setup code
# printed in `docker logs`.
CMD ["python", "-m", "ninaivu", "/data/media", "--host", "0.0.0.0", "--admin-host", "0.0.0.0", "--no-mdns"]
