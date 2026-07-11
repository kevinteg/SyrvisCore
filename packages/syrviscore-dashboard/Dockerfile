# syntax=docker/dockerfile:1
#
# Build from the REPO ROOT (context must include packages/syrviscore):
#   docker build -f packages/syrviscore-dashboard/Dockerfile \
#       -t ghcr.io/kevinteg/syrviscore-dashboard:<ver> [--build-arg WITH_L2_TOOLS=true] .

# --- stage 1: build the React SPA ------------------------------------------
FROM node:20-alpine AS web
WORKDIR /web
COPY packages/syrviscore-dashboard/frontend/package*.json ./
RUN npm ci
COPY packages/syrviscore-dashboard/frontend/ ./
RUN npm run build   # -> /web/dist

# --- stage 2: python runtime -----------------------------------------------
FROM python:3.12-slim AS runtime

# Off by default → the image is read + core-restart only (core lifecycle uses the
# Docker SDK, no compose binary). Set to "true" to enable Layer 2 mutations, which
# shell `docker compose` + `git` via ServiceManager.
ARG WITH_L2_TOOLS=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_STATIC_DIR=/app/static \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000

RUN if [ "$WITH_L2_TOOLS" = "true" ]; then \
      set -eux; \
      apt-get update; \
      apt-get install -y --no-install-recommends ca-certificates curl gnupg git; \
      install -m 0755 -d /etc/apt/keyrings; \
      curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc; \
      chmod a+r /etc/apt/keyrings/docker.asc; \
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin; \
      rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# Install the deterministic core library + the dashboard adapter.
COPY packages/syrviscore /src/syrviscore
COPY packages/syrviscore-dashboard /src/syrviscore-dashboard
RUN pip install --no-cache-dir /src/syrviscore /src/syrviscore-dashboard

# The built SPA, served by FastAPI at DASHBOARD_STATIC_DIR.
COPY --from=web /web/dist /app/static

# Runs as root to access the mounted docker socket (the Portainer model). The
# socket mount is what grants authority; auth + no-new-privileges bound the risk.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["syrviscore-dashboard"]
