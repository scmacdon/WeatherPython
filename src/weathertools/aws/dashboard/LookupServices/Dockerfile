# Use Python with Java and Git preinstalled
FROM maven:3.9.4-eclipse-temurin-21 AS build

# Install Python, pip, git, and system tools
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y \
    python3 python3-pip git curl vim less ca-certificates \
    > /dev/null 2>&1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install required Python dependencies
RUN pip3 install --quiet --no-cache-dir \
    boto3 \
    requests \
    pyyaml \
    > /dev/null 2>&1

# Set working directory
WORKDIR /app

# Copy your Python script
COPY run_lookup.py .

# Run script on container start
CMD ["python3", "run_lookup.py"]

