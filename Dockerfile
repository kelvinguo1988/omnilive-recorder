FROM python:3.11-slim

# 镜像源参数化：默认官方源(适合 GitHub 境外 runner)，本地可传阿里云源加速
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX=https://pypi.org/simple

LABEL maintainer="live-recorder"
LABEL description="多平台直播录制平台 - 抖音/Bilibili/快手"

# 使用镜像源加速安装（本地构建传 APT_MIRROR=mirrors.aliyun.com）
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装（本地构建传 PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/）
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

# 复制应用代码
COPY app/ ./app/
COPY config/ ./config/

# 创建必要目录
RUN mkdir -p /app/recordings /app/data /app/logs

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
