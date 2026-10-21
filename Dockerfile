FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Moscow

# Node 20 + Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm i -g @anthropic-ai/claude-code \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install chromium

COPY . .
CMD ["python", "-m", "app.bot"]
