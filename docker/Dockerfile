FROM python:3.12-slim

LABEL maintainer="tender-monitor"

WORKDIR /app

# системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ config/
COPY src/ src/

# Volume для данных и логов (persist между запусками)
VOLUME ["/app/data", "/app/logs"]

# По умолчанию — парсинг
ENTRYPOINT ["python", "-m"]
CMD ["src.collector"]