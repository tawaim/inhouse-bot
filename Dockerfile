FROM python:3.11-slim

# Tesseract for OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Persistent volume mount point
RUN mkdir -p /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/inhouse.db

CMD ["python", "-m", "bot.main"]
