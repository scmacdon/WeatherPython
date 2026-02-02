# Use a slim Debian base
FROM debian:bullseye-slim

# Install dependencies
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y \
    curl git python3 python3-pip build-essential pkg-config libssl-dev ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install rustup and Rust 1.88
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --default-toolchain 1.88.0
ENV PATH="/root/.cargo/bin:${PATH}"

# Copy Python script
COPY run_tests.py /app/run_tests.py
WORKDIR /app

# Install boto3
RUN pip3 install boto3

# Default command
CMD ["python3", "run_tests.py"]


