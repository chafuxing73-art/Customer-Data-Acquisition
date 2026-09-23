# 使用官方 Python 基础镜像（bookworm = Debian 12，Chromium 包名正确）
FROM python:3.11-slim-bookworm

# 设置工作目录
WORKDIR /app

# ── 安装系统依赖：Chromium + 中文字体 + CA 证书 + wget（DrissionPage 需要） ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-wqy-zenhei \
    fonts-noto-cjk \
    ca-certificates \
    wget \
    gnupg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 让 DrissionPage 能找到 Chromium（Debian bookworm 里可能是 /usr/bin/chromium）
ENV CHROMIUM_PATH=/usr/bin/chromium-browser

# 同时做个软链（有些版本是 chromium 有些是 chromium-browser）
RUN if [ -f /usr/bin/chromium ] && [ ! -f /usr/bin/chromium-browser ]; then \
        ln -sf /usr/bin/chromium /usr/bin/chromium-browser; \
    fi; \
    if [ -f /usr/bin/chromium-browser ] && [ ! -f /usr/bin/chromium ]; then \
        ln -sf /usr/bin/chromium-browser /usr/bin/chromium; \
    fi;

# ── 复制依赖文件 + 安装 Python 依赖 ──
COPY requirements.txt .

# 先更新 pip
RUN pip install --no-cache-dir --upgrade pip

# 安装 Python 依赖 + HTTPS 支持
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir pyOpenSSL cryptography

# ── 复制应用代码 ──
COPY . .

# 创建必要的目录
RUN mkdir -p logs tmp

# ── 容器环境：强制当作服务器 + headless ──
ENV FORCE_SERVER_ENV=1
ENV FORCE_HEADLESS=1
ENV PYTHONUNBUFFERED=1
ENV LANG=zh_CN.UTF-8

# 端口与 app.py 默认保持一致（3020）
EXPOSE 3020

# 启动应用（用 python app.py，不是 flask run）
CMD ["python", "app.py"]
