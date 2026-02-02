# ---- Build stage: still needed if you later want to build Go binaries ----
FROM golang:1.20 AS build

WORKDIR /app
COPY . .

# ---- Final stage: Python + Go ----
FROM python:3.9-slim

# Install Go, git, and dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        wget \
        tar \
        gcc \
        g++ \
        make \
        libc6-dev \
        build-essential \
        pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Install Go manually (Debian slim doesn’t include it)
ENV GOLANG_VERSION=1.20.14
RUN wget https://go.dev/dl/go${GOLANG_VERSION}.linux-amd64.tar.gz && \
    tar -C /usr/local -xzf go${GOLANG_VERSION}.linux-amd64.tar.gz && \
    rm go${GOLANG_VERSION}.linux-amd64.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"

# 🔑 Fix Go proxy issues
ENV GOPROXY=direct
# ENV GOSUMDB=off   # Uncomment if checksum DB blocked

# Copy repo (if needed) and test runner
COPY --from=build /app /app
COPY run_tests.py /app/

WORKDIR /app

# Install Python deps
RUN pip install --quiet boto3

# Default: run the Python test runner
CMD ["python", "run_tests.py"]

