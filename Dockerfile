FROM python:3.13-slim

# 非 root 运行，uid 固定 1000：与宿主机挂载目录的属主对齐，部署时不需要额外 user: 覆盖。
RUN groupadd -g 1000 adapter && useradd -u 1000 -g 1000 -m adapter

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 依赖先装、代码后拷：改代码不会让依赖层失效。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 发布版本号由 CI 构建时注入（release.yml 传 --build-arg APP_VERSION=<x.y.z>）。
#
# ⚠️ 这段放在 `pip install` **之后**是刻意的：ARG/ENV 变化会让**其后**的每一层缓存失效，
# 而上面那层 pip install 要跑好几分钟。放前面 = 每次发版都重装一遍依赖（实测多花 ~4.5 分钟）；
# 放这里 = 版本号变了只有 COPY 那几层重建。
#
# 两个 ENV 各有用途，都不是装饰：
#   ADAPTER_VERSION       —— 本服务自己读（/healthz 与 OpenAPI 的 version），
#                            回答"现在跑的是哪个镜像"；
#   LOGFIRE_SERVICE_VERSION —— logfire 认这个键，让 trace 上的 service version
#                            与镜像版本一致，而不是"未标注"。
ARG APP_VERSION=dev
ENV ADAPTER_VERSION=${APP_VERSION} \
    LOGFIRE_SERVICE_VERSION=${APP_VERSION}

# 只拷运行期需要的东西。script_store **必须**进镜像：脚本是 pinned + review 过的契约
# （ADR-005），从宿主目录挂载会让"镜像里跑的是哪份脚本"变成运行时才知道的事。
COPY adapter /app/adapter
COPY script_store /app/script_store
COPY gunicorn.conf.py /app/gunicorn.conf.py

# /app/data 是**任务表与转存产物的落点**（compose 把它挂成命名卷）。
# 必须在镜像里先建好并 chown：Docker 初始化命名卷时会继承该目录的属主，
# 否则卷会是 root 所有，非 root 进程写不进去 —— 表现为"起来就崩、日志只有一行 PermissionError"。
RUN mkdir -p /app/data && chown -R adapter:adapter /app

USER adapter

EXPOSE 8000

# 健康检查走 /healthz，并**显式绕开环境代理**：HTTP_PROXY 会把回环地址也代理走，
# 于是"容器自己活着"会被一个网关错误判成不健康（本机实测过同一个坑）。
# 端口读 $PORT（gunicorn 也读它），避免改了端口后健康检查指向一个没人听的端口。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,sys,urllib.request as u; \
op=u.build_opener(u.ProxyHandler({})); \
url='http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz'; \
sys.exit(0 if op.open(url, timeout=2).status == 200 else 1)"

# gunicorn 负责进程管理（worker 回收 / 优雅关机 / 心跳），uvicorn-worker 提供 asyncio 能力。
# 为什么不让 uvicorn 自己多进程：uvicorn 没有 worker 回收与优雅排空，容器化部署里
# 这两件事直接决定"滚动发布会不会掉请求"。配置与调参说明都在 gunicorn.conf.py。
CMD ["gunicorn", "adapter.main:app", "--config", "/app/gunicorn.conf.py"]
