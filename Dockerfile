FROM ubuntu:20.04

LABEL maintainer="routing-satellite" description="FISCO light node routing satellite"

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

USER root

# 安装基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3 \
    python3-pip \
    python3-dev \
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
    && rm -rf /var/lib/apt/lists/*

# 启用 FRR 守护进程
COPY config/daemons /etc/frr/daemons

# 启用 IPv4 转发
RUN echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf

# 安装 Python 依赖
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ /app/app/
COPY contracts/ /app/contracts/
COPY config/ /app/config/

# 创建配置挂载目录
RUN mkdir -p /configuration/frr /configuration/routes /configuration/address

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 18545

ENTRYPOINT ["python3", "/app/app/entrypoint.py"]
