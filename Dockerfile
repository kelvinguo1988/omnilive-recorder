FROM python:3.11.9-slim-bookworm

# P1-25: 固定容器时区。全项目时间口径是「DB 列存 naive UTC + 目录/文件名用本地时区」，
# 两者必须一致才不会在跨 8h 边界时把同一天的录像分到不同日期目录。
# 只靠 compose 传 TZ 不够：裸 `docker run`（README 的示例未带 -e TZ）会退回 UTC，
# 导致日期目录整体偏 8 小时。这里在镜像层固定默认值，compose / -e TZ 仍可覆盖。
# 注意：Debian **slim** 镜像默认不含 tzdata，缺它时 TZ 会静默退回 UTC，
# 因此下面 apt 安装清单里必须带上 tzdata（仅设 ENV 是不够的）。
ENV TZ=Asia/Shanghai

# 镜像源参数化：默认官方源(适合 GitHub 境外 runner)，本地可传阿里云源加速
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX=https://pypi.org/simple

LABEL maintainer="live-recorder"
LABEL description="多平台直播录制平台 - 抖音/Bilibili/快手"

# 使用镜像源加速安装（本地构建传 APT_MIRROR=mirrors.aliyun.com）
# DEBIAN_FRONTEND=noninteractive: 下面装的 tzdata 会弹 debconf「选择地理区域」交互提示，
# 无 TTY 的构建环境下可能挂起或取错默认值；非交互模式直接使用镜像内已有的 TZ 默认值。
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    tzdata \
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
#
# P1-23: requirements.lock 的目标解释器必须与本镜像的 Python 3.11 一致。
# 该 lock 原先在 Python 3.13 / macOS arm64 上生成，SQLAlchemy 2.0.36 对 greenlet 的
# 依赖带 `python_version < "3.13"` 环境标记，因此在生成宿主机上不出现、在 3.11 镜像里
# 却是必需依赖 —— 这类「lock 缺包」正是被第二遍 pip install 静默补上的。
# 现已按 Python 3.11 / linux 重新解析（详见 requirements.lock 头部说明）。
# 改动 requirements.txt 后务必重新生成 lock，且必须指定 3.11/linux 目标。
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
