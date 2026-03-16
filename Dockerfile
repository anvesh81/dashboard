FROM python:3.12-slim

# Pull latest OS security patches including sqlite3 fix
RUN apt-get update && apt-get upgrade -y && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade pip==25.3 && \
    pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    boto3==1.34.0 \
    python-multipart==0.0.22 \
    starlette==0.47.2

COPY app/ ./

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
