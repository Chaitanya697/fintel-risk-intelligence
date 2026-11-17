# Build from the repo root:  docker build -f services/risk-engine/Dockerfile -t fintel-risk-engine .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/risk-engine/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY fintel_common ./fintel_common
COPY services/risk-engine/engine.py ./services/risk-engine/engine.py
RUN useradd -u 10001 -m fintel
USER fintel
WORKDIR /app/services/risk-engine
EXPOSE 8001
# 1 worker + threads keeps Prometheus counters in a single process
CMD ["gunicorn", "-b", "0.0.0.0:8001", "-w", "1", "--threads", "4", "--timeout", "60", "engine:app"]
