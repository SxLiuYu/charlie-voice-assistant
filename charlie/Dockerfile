# Charlie 语音助手 — Dockerfile
# 多阶段构建: 构建环境(含Node.js+npx) → 运行环境(精简)
FROM python:3.12-slim AS builder

# 安装系统依赖(ffmpeg + Node.js)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 验证依赖
RUN ffmpeg -version | head -1 && node --version && npx --version

WORKDIR /app

# 先装Python依赖(利用Docker缓存层)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 预装npx包(避免首次启动慢)
RUN npx -y @amap/amap-maps-mcp-server --version 2>/dev/null; \
    npx -y @modelcontextprotocol/server-filesystem --version 2>/dev/null; \
    npx -y @modelcontextprotocol/server-memory --version 2>/dev/null; \
    npx -y @modelcontextprotocol/server-sequential-thinking --version 2>/dev/null; \
    true

# ---- 运行阶段 ----
FROM python:3.12-slim AS runtime

# 安装运行时依赖(ffmpeg + Node.js + libsndfile for soundfile)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    gnupg \
    libsndfile1 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从builder复制已安装的Python包
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 从builder复制npx缓存
COPY --from=builder /root/.npm /root/.npm
COPY --from=builder /root/.npx /root/.npx

# 复制项目文件
COPY voice_agent.py voice_server.py utils.py ./
COPY magic-music.py magic-reminder.py magic-notes.py magic-system.py magic-info.py magic-life.py ./
COPY mcp_common.py mcp_ir_control.py baize_skills_mcp.py ./
COPY app/ app/
COPY web/ web/
COPY skills/ skills/

# 创建数据目录
RUN mkdir -p /app/logs /app/notes /app/data

# 环境变量默认值
ENV ARK_BASE="https://ark.cn-beijing.volces.com/api/plan/v3" \
    ARK_MODEL="ark-code-latest" \
    FINNA_BASE="https://www.finna.com.cn/v1" \
    ASSISTANT_KID_DATA_DIR="/app/data" \
    ASSISTANT_KID_LOG_DIR="/app/logs" \
    PYTHONUNBUFFERED=1 \
    SKIP_BACKGROUND=0

# 暴露端口
EXPOSE 8000
EXPOSE 1883
EXPOSE 8443
EXPOSE 8888/udp

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令
CMD ["python3", "voice_server.py"]
