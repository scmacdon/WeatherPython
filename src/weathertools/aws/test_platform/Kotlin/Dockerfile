# Use Gradle base image with JDK 21
FROM gradle:8.5.0-jdk21

# Install system dependencies
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y \
        python3 python3-pip git curl ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip3 install --quiet --no-cache-dir boto3

# Set working directory to /tmp
WORKDIR /tmp

# Copy Python test runner
COPY run_tests.py .

# Default command to run Python test runner
CMD ["python3", "run_tests.py"]





