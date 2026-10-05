FROM python:3.12-slim

WORKDIR /app

# 依赖安装随镜像构建完成
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV DB_PATH=/data/app.db \
    PORT=8000 \
    PYTHONUNBUFFERED=1

# SQLite 数据目录（挂载卷以持久化，重启后数据与链接保留）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 启动时自动建库建表（幂等）
CMD ["python", "app.py"]
