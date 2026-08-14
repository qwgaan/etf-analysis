# ETF 分析工作台 - 多阶段构建
# 阶段 1:安装依赖
FROM python:3.12-slim AS builder

WORKDIR /app

# 先装依赖(利用 Docker 层缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 阶段 2:运行镜像
FROM python:3.12-slim

# 时区设为上海(影响数据日期显示与回撤按年计算)
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 从 builder 复制已装好的 site-packages
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# 复制应用代码
COPY app.py .
COPY core/ ./core/
COPY templates/ ./templates/
COPY static/ ./static/
COPY config/ ./config/
COPY warmup.py .

# 数据与配置目录(运行时用 volume 挂载持久化)
RUN mkdir -p /app/data /app/outputs

# AKShare 数据缓存也落到持久化目录
ENV AKSHARE_DATA_DIR=/app/data/akshare

EXPOSE 5001

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/etfs', timeout=5)" || exit 1

CMD ["python", "app.py"]
