FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Asia/Singapore DISABLE_AUTOUPDATER=1 CLAUDE_CONFIG_DIR=/app/data/claude-config MPLBACKEND=Agg
# curl, ca-certificates and git are for the Claude Code installer; tzdata for Asia/Singapore
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# UID matches the NAS user that owns ./data and the vault, so files stay editable by the owner
ARG UID=1000
RUN useradd -m -u ${UID} app && mkdir -p /app/data && chown -R app /app
USER app
RUN curl -fsSL https://claude.ai/install.sh | bash -s stable
ENV PATH=/home/app/.local/bin:$PATH
RUN claude --version
# Agent Reach for the film room (YouTube, read-only). From the GitHub release, never PyPI (different project there).
# Pinned to v1.6.1; bump deliberately.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ~/.local/bin/uv tool install https://github.com/Panniantong/Agent-Reach/releases/download/v1.6.1/agent-reach.zip \
 && agent-reach version
# code last: a code-only change rebuilds in seconds instead of reinstalling the Claude CLI
COPY --chown=app nba ./nba
COPY --chown=app tests ./tests

HEALTHCHECK --interval=5m --timeout=20s --start-period=10m CMD ["python", "-m", "nba", "health"]
CMD ["python", "-m", "nba", "serve"]
