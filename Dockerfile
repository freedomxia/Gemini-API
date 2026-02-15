FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 缓存
COPY pyproject.toml ./
COPY src/ ./src/

# setuptools-scm 在没有 .git 时无法推断版本，用环境变量指定
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0

# 安装项目本体 + web 额外依赖
RUN pip install --no-cache-dir "." && \
    pip install --no-cache-dir aiohttp aiohttp-cors

# 复制应用代码
COPY web/ ./web/

# 创建数据目录（运行时可挂载）
RUN mkdir -p /app/data

# 复制默认配置（如果挂载了 volume 会被覆盖）
COPY data/ ./data/

EXPOSE 8000

ENV PORT=8000

CMD ["python", "web/app.py"]
