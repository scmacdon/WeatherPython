# Use exact Ruby version 3.3.7 to match Gemfile
FROM ruby:3.3.7

# Install system dependencies and Python
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y \
        git \
        curl \
        vim \
        less \
        ca-certificates \
        build-essential \
        python3 \
        python3-venv \
        python3-pip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Bundler version matching Gemfile.lock
RUN gem install bundler -v 2.3.7

# Create Python virtual environment for boto3
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir boto3

# Set working directory
WORKDIR /app

# Copy entire repo including run_tests.py
COPY . .

# Default entrypoint: run Python script
ENTRYPOINT ["python3", "run_tests.py"]








