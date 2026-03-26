FROM ubuntu:20.04

LABEL maintainer="routing-satellite" description="FISCO-BCOS light node routing satellite"

ENV TZ=Asia/Shanghai
ENV DEBIAN_FRONTEND=noninteractive
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

USER root

# ====== 1. 安装系统依赖 ======
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3 \
    python3-pip \
    python3-dev \
    libssl-dev \
    net-tools \
    iptables \
    iproute2 \
    iputils-ping \
    frr \
    curl \
    wget \
    vim \
    tcpdump \
    traceroute \
    iperf3 \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ====== 2. 安装 FISCO-BCOS 轻节点二进制 ======
# FISCO-BCOS v3.6.0 (支持 light node 模式)
# 通过官方发布的预编译二进制安装
ENV FISCO_VERSION=v3.6.0
RUN mkdir -p /fisco && cd /fisco \
    && curl -#LO "https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/${FISCO_VERSION}/BcosBuilder.tgz" \
    && tar -xzf BcosBuilder.tgz \
    && rm -f BcosBuilder.tgz \
    # 同时下载轻节点二进制
    && curl -#LO "https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/${FISCO_VERSION}/fisco-bcos-light-linux-x86_64.tar.gz" \
    || curl -#LO "https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/${FISCO_VERSION}/fisco-bcos-linux-x86_64.tar.gz" \
    || true
# 尝试解压轻节点二进制 (不同版本发布包命名可能不同)
RUN cd /fisco \
    && (tar -xzf fisco-bcos-light-linux-x86_64.tar.gz 2>/dev/null \
        || tar -xzf fisco-bcos-linux-x86_64.tar.gz 2>/dev/null \
        || true) \
    && rm -f *.tar.gz \
    && ([ -f /fisco/fisco-bcos ] && chmod +x /fisco/fisco-bcos || true) \
    && ([ -f /fisco/lightnode/fisco-bcos ] && cp /fisco/lightnode/fisco-bcos /fisco/fisco-bcos && chmod +x /fisco/fisco-bcos || true)

# 如果上面的预编译下载失败, 提供一个 fallback: 通过构建脚本获取
# 用户也可以在构建前将 fisco-bcos 二进制放到 ./bin/ 目录
COPY bin/fisco-bcos* /fisco/ 2>/dev/null || true
RUN chmod +x /fisco/fisco-bcos 2>/dev/null || true

# 创建轻节点运行目录
RUN mkdir -p /fisco/light_node/conf

# ====== 3. 安装 FISCO Python SDK (console_of_python_sdk) ======
RUN pip3 install --no-cache-dir \
    eth-abi==2.2.0 \
    eth-utils==1.10.0 \
    eth-account==0.5.9 \
    pycryptodome==3.18.0 \
    requests==2.31.0 \
    flask==2.3.3 \
    psutil==5.9.8 \
    Werkzeug==2.3.8

# 安装 FISCO Python SDK
RUN cd /opt && pip3 install --no-cache-dir python-fisco-sdk 2>/dev/null || true
# Fallback: 直接 clone 并安装
RUN if ! python3 -c "import client" 2>/dev/null && ! python3 -c "import fisco" 2>/dev/null; then \
        cd /opt \
        && (git clone https://github.com/FISCO-BCOS/python-sdk.git 2>/dev/null || true) \
        && (cd python-sdk && pip3 install --no-cache-dir -r requirements.txt 2>/dev/null || true) \
        && (cd python-sdk && pip3 install --no-cache-dir . 2>/dev/null || true) ; \
    fi

# ====== 4. 配置 FRR ======
COPY config/daemons /etc/frr/daemons
RUN echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf

# ====== 5. 复制应用代码 ======
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt 2>/dev/null || true
COPY app/ /app/app/
COPY contracts/ /app/contracts/
COPY config/ /app/config/

# 创建配置挂载目录
RUN mkdir -p /configuration/frr /configuration/routes /configuration/address \
    && mkdir -p /fisco/light_node/conf/accounts

ENV PYTHONPATH=/app:/opt/python-sdk
ENV PYTHONUNBUFFERED=1
ENV FISCO_BINARY=/fisco/fisco-bcos
ENV FISCO_LIGHT_NODE_DIR=/fisco/light_node

EXPOSE 18545

ENTRYPOINT ["python3", "/app/app/entrypoint.py"]
