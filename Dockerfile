FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    boto3==1.34.0 \
    python-multipart==0.0.9

COPY app/ ./

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
