FROM python:3.12-slim

# Install curl for ECS container health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    boto3==1.34.0 \
    python-multipart==0.0.22 \
    starlette==0.47.2

COPY app/ ./

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
