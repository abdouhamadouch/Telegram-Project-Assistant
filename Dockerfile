FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/assistant.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN useradd --create-home --uid 10001 botuser && mkdir -p /data && chown -R botuser:botuser /app /data
USER botuser
CMD ["python", "bot.py"]
