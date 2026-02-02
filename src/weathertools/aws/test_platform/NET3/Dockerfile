# Use official .NET 8 SDK image
FROM mcr.microsoft.com/dotnet/sdk:8.0

# Install Python and other tools
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y \
        python3 \
        python3-venv \
        python3-pip \
        git \
        curl \
        vim \
        less \
        ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Set up Python virtual environment for boto3
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir boto3

# Add venv binaries to path
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy everything into container
COPY . .

# Remove the cloned repo directory before running tests to ensure fresh clone
ENTRYPOINT ["bash", "-c", "rm -rf /app/aws-doc-sdk-examples && python3 run_tests.py"]


