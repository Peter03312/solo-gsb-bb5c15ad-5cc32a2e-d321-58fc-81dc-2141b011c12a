FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROUTER_DB_PATH=/data/router.db \
    ROUTER_HOST=0.0.0.0 \
    ROUTER_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 先建库建表（幂等），再启动服务
CMD ["sh", "-c", "python -m app.initdb && uvicorn app.main:app --host ${ROUTER_HOST} --port ${ROUTER_PORT}"]
