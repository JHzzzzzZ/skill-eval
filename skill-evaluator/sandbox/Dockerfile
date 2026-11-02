# T2/T3 沙箱镜像模板（非 root + 最小工具链）。
# 构建：docker build -t skill-evaluator-sandbox:local sandbox/
# 生产部署请把基础镜像与 pi/claude 版本改为 digest/精确版本并重新 pin；
# 这里用标签是为了 clone 后能直接构建（本机无 Docker 时不会在测试里构建）。
FROM node:22-bookworm-slim

# 锁版本：build 时可用 --build-arg 覆盖，但部署前必须换成精确版本
ARG PI_VERSION=latest
ARG CLAUDE_CODE_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# python3 供容器内脚本/被测 skill 使用；git 供沙箱副本使用
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
                 "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# 非 root：容器内 uid/gid 固定，rootfs 只读 + cap-drop 由 sandbox.py 的 docker run 参数保证
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox
USER sandbox

WORKDIR /work
CMD ["python3", "--version"]
