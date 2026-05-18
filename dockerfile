FROM python:3.12-slim

# System tools: git for clones; curl + ca-certificates for HTTPS and the
# verifier agent's HTTP probes; gnupg for NodeSource repo signing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates gnupg docker.io docker-cli \
 && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS + pnpm/yarn via corepack — used by the verifier agent to
# run `pnpm install` / `pnpm dev` (and friends) against cloned repos.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable \
 && corepack prepare pnpm@9.15.0 --activate

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
