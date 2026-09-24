# 使用 Python 官方 bookworm 镜像（非 slim，包更全，Chromium 装得上）
FROM python:3.11-bookworm

WORKDIR /app

# ── 1. 先配置 apt 源（Debian bookworm 默认 slim 镜像只启 main，Chromium 在 contrib 里） ──
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && echo "deb http://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list \
    && echo "deb http://security.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list

# ── 2. 安装系统依赖：Chromium + 字体 + 工具 ──
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-wqy-zenhei \
        fonts-noto-cjk \
        ca-certificates \
        wget \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Chromium 可能叫 chromium 或 chromium-browser，两个都软链一下
RUN if [ -f /usr/bin/chromium ] && [ ! -f /usr/bin/chromium-browser ]; then \
        ln -sf /usr/bin/chromium /usr/bin/chromium-browser; \
    fi \
    && if [ -f /usr/bin/chromium-browser ] && [ ! -f /usr/bin/chromium ]; then \
        ln -sf /usr/bin/chromium-browser /usr/bin/chromium; \
    fi

ENV CHROMIUM_PATH=/usr/bin/chromium-browser

# ── 3. 安装 Python 依赖 ──
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir pyOpenSSL cryptography

# ── 4. 复制应用代码 ──
COPY . .
RUN mkdir -p logs tmp

# ── 5. 容器环境变量（强制服务器 headless 模式） ──
ENV FORCE_SERVER_ENV=1
ENV PYTHONUNBUFFERED=1
ENV LANG=zh_CN.UTF-8

# ── 6. 端口 + 启动 ──
EXPOSE 3020
CMD ["python", "app.py"]
