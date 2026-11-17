# Build from the repo root:  docker build -f services/data-processor/Dockerfile -t fintel-data-processor .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/data-processor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY fintel_common ./fintel_common
COPY services/data-processor/process.py ./services/data-processor/process.py
COPY tests ./tests
RUN useradd -u 10001 -m fintel
USER fintel
CMD ["python", "services/data-processor/process.py"]
