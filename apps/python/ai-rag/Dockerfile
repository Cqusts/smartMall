# 构建上下文为 apps/python/，以便同时纳入 ai-common
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装公共库，利用镜像层缓存：ai-common 不变时无需重装
COPY ai-common/ /tmp/ai-common/
RUN pip install /tmp/ai-common && rm -rf /tmp/ai-common

COPY ai-rag/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY ai-rag/app/ ./app/

EXPOSE 9001
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9001/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001"]
