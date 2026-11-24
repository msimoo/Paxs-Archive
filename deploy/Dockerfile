# Stateless PAXS node (roadmap #47). Nodes share PostgreSQL + object storage (S3/MinIO)
# and are load-balanced by HAProxy — see deploy/LOAD-BALANCING.md.
FROM python:3.13-slim

WORKDIR /app

# System deps for pydicom/pillow wheels on slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY run_paxs.py .

# Each node reports its own instance id (HAProxy round-robin visibility).
ENV PAXS_INSTANCE_ID=node-1 \
    ENVIRONMENT=production \
    STORAGE_BACKEND=s3 \
    S3_ENDPOINT_URL=http://minio:9000 \
    DICOM_RECEIVER_ENABLED=true \
    WORKER_ENABLED=true

EXPOSE 8000 10400

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://0.0.0.0:8000/api/v1/health/ready', timeout=3).status==200 else 1)"

CMD ["python", "run_paxs.py"]
