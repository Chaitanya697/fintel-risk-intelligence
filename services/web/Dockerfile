# Build from the repo root:  docker build -f services/web/Dockerfile -t fintel-web .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/web/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/web/ ./
RUN useradd -u 10001 -m fintel && mkdir -p /data/db && chown -R fintel /data
USER fintel
ENV DB_PATH=/data/db/fintel.db
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "8", "--timeout", "60", "app:app"]
