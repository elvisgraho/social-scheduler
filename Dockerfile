FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      chromium chromium-driver \
      wget curl ca-certificates \
      libnss3 libgdk-pixbuf-2.0-0 libxss1 libgtk-3-0 libasound2 \
      fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/uploads /app/data/logs

EXPOSE 8501

CMD bash -lc "streamlit run main.py --server.address=0.0.0.0 --server.port=\${PORT:-8501} & python run_worker.py"
