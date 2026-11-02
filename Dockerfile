FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    TIKTOK_HEADLESS=true

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps --only-shell chromium && \
    rm -rf /var/lib/apt/lists/*

COPY . .
RUN mkdir -p /app/data/uploads /app/data/logs

EXPOSE 8501

CMD bash -lc "streamlit run main.py --server.address=0.0.0.0 --server.port=\${PORT:-8501} & python run_worker.py"
