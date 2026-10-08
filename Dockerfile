FROM python:3.12-slim-bookworm

# Claude Code from Anthropic's signed apt repository (stable channel).
# Package installs don't auto-update; rebuild the image to upgrade.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
    > /etc/apt/sources.list.d/claude-code.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends claude-code \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mcp>=1.28,<2"

RUN useradd --create-home --uid 1000 bot \
 && install -d -o bot -g bot /data

WORKDIR /app
COPY server.py /app/server.py
COPY bot/ /app/
# NAS shares can hand the build context over without world-read bits, which the
# non-root bot user needs. (COPY --chmod would need BuildKit, which UGREEN lacks.)
RUN chmod 0644 /app/*

# Claude Code keeps its config and session history here (a named volume),
# so conversations survive container restarts.
ENV CLAUDE_CONFIG_DIR=/data \
    DISABLE_AUTOUPDATER=1 \
    PYTHONUNBUFFERED=1

USER bot
CMD ["python3", "/app/bot.py"]
