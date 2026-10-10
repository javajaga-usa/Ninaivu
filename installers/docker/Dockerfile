# =============================================================================
# Ninaivu — production container image (build context: the repository root)
# Multi-stage build for a lightweight, secure, non-root family media server
# =============================================================================

# --- Stage 1: Build Dependencies ---
# The base image by digest, not by its moving tag, so a build is the image
# that was reviewed and not whatever the tag points at that day; Dependabot
# (.github/dependabot.yml) moves the digest with a pull request. Both stages
# name the same one.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

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

COPY requirements/requirements.txt requirements/requirements-ai.txt \
     requirements/constraints-tested.txt requirements/build-tools.txt ./

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
#
# The versions are the ones the tests ran with (constraints-tested.txt), and
# pip's own come from build-tools.txt; `pip freeze` in the image lists them.
ARG WITH_AI=0
RUN pip install --upgrade -c build-tools.txt pip setuptools wheel && \
    pip install -r requirements.txt -c constraints-tested.txt && \
    if [ "$WITH_AI" = "1" ]; then \
      pip install "torch>=2.0" --index-url https://download.pytorch.org/whl/cpu && \
      pip install -r requirements-ai.txt -c constraints-tested.txt ; \
    fi

# --- Stage 2: Runtime Image ---
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

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
    NINAIVU_AI_MODELS_DIR="/data/state/ai-models" \
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

# The family app only. The console (3000) is deliberately not EXPOSEd: the
# container listens on it, and docker-compose.yml publishes it on the host's
# loopback alone, but `docker run -P` and the templates that read EXPOSE
# publish every listed port on every address, past ufw, and the console was
# then on the whole network.
EXPOSE 5000

# Volumes for media library and persistent state (SQLite DBs, thumbnails, certs)
VOLUME ["/data/media", "/data/state"]

# Built-in health check for container orchestration, on whatever port the
# family app was given (NINAIVU_PORT), not always 5000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${NINAIVU_PORT:-5000}/healthz" || exit 1

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
