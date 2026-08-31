FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CRYPTBOT_HOST=0.0.0.0 \
    CRYPTBOT_PORT=8765

WORKDIR /app
RUN addgroup --system cryptobot && adduser --system --ingroup cryptobot cryptobot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=cryptobot:cryptobot server.py index.html README.md ARCHITECTURE.md ./
COPY --chown=cryptobot:cryptobot cryptobot ./cryptobot
RUN mkdir -p /app/data && chown cryptobot:cryptobot /app/data

USER cryptobot
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"

CMD ["python", "server.py"]
