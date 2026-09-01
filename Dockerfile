FROM python:3.11.9-slim-bookworm

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
# P2-7: 先用 pip-compile 生成的 requirements.lock 锁定传递依赖（提升复现性），
# 再装一遍 requirements.txt 补齐 lock 中可能遗漏的直接依赖。
#
# 背景：曾出现 lock 漏掉 gmssl，导致「构建成功、容器启动即 ModuleNotFoundError」，
# 属于典型的依赖清单不同步。两遍安装 + check_deps.py 显式校验，可把这类问题
# 拦截在构建阶段（构建失败）而不是运行时（服务起不来）。
COPY requirements.txt requirements.lock* ./
COPY scripts/check_deps.py ./scripts/
RUN set -eux; \
    if [ -f requirements.lock ]; then \
      pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.lock; \
    fi; \
    pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt; \
    python scripts/check_deps.py requirements.txt

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
