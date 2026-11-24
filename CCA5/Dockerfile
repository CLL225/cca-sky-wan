FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /workspace

# 创建非 root 用户 (Mahimahi 要求)
RUN useradd -m -s /bin/bash testuser

COPY --chown=testuser:testuser ./mahimahi /workspace/mahimahi
COPY requirements.txt /workspace/

# 替换为清华源
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list && \
    sed -i 's@//.*security.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    python3.10-venv \
    git \
    curl \
    wget \
    ca-certificates \
    build-essential \
    iperf3 \
    iproute2 \
    iptables \
    dnsmasq \
    sudo \
    gnuplot \
    cmake \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y \
    protobuf-compiler \
    libprotobuf-dev \
    autotools-dev \
    dh-autoreconf \
    pkg-config \
    dnsmasq-base \
    apache2-bin \
    apache2-dev \
    debhelper \
    libssl-dev \
    ssl-cert \
    libxcb-present-dev \
    libcairo2-dev \
    libpango1.0-dev

# 编译安装 Mahimahi（先给脚本加执行权限）
RUN cd /workspace/mahimahi && \
    chmod +x autogen.sh && \
    ./autogen.sh && \
    ./configure && \
    make && \
    make install

# 建立软链接
RUN ln -s /usr/bin/python3 /usr/bin/python

# 安装 Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# 安装 uv (SkyDiscover 需要)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# 智能升级 pip (使用清华源)
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip3 install --upgrade pip || pip3 install --upgrade pip --break-system-packages

# 同理：尝试普通安装，如果失败则尝试强制安装
RUN pip3 install --no-cache-dir -r requirements.txt || \
    pip3 install --no-cache-dir -r requirements.txt --break-system-packages

RUN chmod +x /workspace/*.sh 2>/dev/null || true

# 验证安装
RUN python3 --version && \
    cargo --version && \
    uv --version

CMD ["/bin/bash"]