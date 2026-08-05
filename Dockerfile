FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Persistent volume mount point
RUN mkdir -p /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/inhouse.db

CMD ["python", "-m", "bot.main"]
