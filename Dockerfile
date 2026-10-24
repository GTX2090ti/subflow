# 默认走国内镜像源（内网/NAS 构建）；CI 可传 --build-arg PYTHON_IMAGE=python:3.11-slim
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    SUBFLOW_CONFIG_FILE=/app/data/config.json

WORKDIR /app

COPY requirements.txt ./
# libarchive-tools 提供 bsdtar（libarchive 3.6，对 RAR/RAR5 解包支持最好，
# 是解字幕包的主力后端）；p7zip-full 兜底 7z/zip；unrar-free 作为最后备选。
# unrar-free 在部分镜像源里不可用，缺失不应让整个构建失败。
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && (apt-get install -y --no-install-recommends \
           tzdata libarchive-tools p7zip-full unrar-free \
        || apt-get install -y --no-install-recommends \
           tzdata libarchive-tools p7zip-full) \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
RUN mkdir -p /app/data

# 构建期门禁：导入失败即构建失败，避免把起不来的镜像推到 NAS
RUN python -c "import app.main; print('SubFlow import OK')"

EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=8s --start-period=12s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/overview',timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
