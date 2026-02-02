# Base image: Ubuntu 22.04
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies, Python, Node, Rust, and AWS SDK C++
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        build-essential \
        g++ \
        cmake \
        make \
        git \
        curl \
        python3 \
        python3-venv \
        python3-pip \
        ca-certificates \
        bash \
        libssl-dev \
        libcurl4-openssl-dev \
        libuuid1 \
        uuid-dev \
        zlib1g-dev \
        libgtest-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Rust (optional)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Build and install Google Test
RUN git clone https://github.com/google/googletest.git /tmp/googletest && \
    cd /tmp/googletest && \
    mkdir build && cd build && \
    cmake .. && make && make install && \
    rm -rf /tmp/googletest

# Setup Python virtual environment
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir boto3

# Add Python venv and Rust to PATH
ENV PATH="/opt/venv/bin:/root/.cargo/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy local app files
COPY . .

# Default entrypoint
ENTRYPOINT ["python3", "run_tests.py"]




