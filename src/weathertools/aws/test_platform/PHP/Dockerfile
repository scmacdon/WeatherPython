FROM php:8.2-cli

# Install deps
RUN set -ex && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git curl unzip vim less ca-certificates \
        libzip-dev libxml2-dev libcurl4-openssl-dev libssl-dev zlib1g-dev pkg-config autoconf build-essential \
        libonig-dev python3 python3-pip python3-venv && \
    docker-php-ext-configure zip && \
    docker-php-ext-install -j$(nproc) mbstring xml curl zip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Composer
RUN curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

# Install PHPUnit
RUN curl -Ls https://phar.phpunit.de/phpunit-9.phar -o /usr/local/bin/phpunit && chmod +x /usr/local/bin/phpunit

# Install boto3 (safe with --break-system-packages)
RUN python3 -m pip install boto3 --break-system-packages

WORKDIR /app

# Copy files
COPY composer.json composer.lock* ./ 
COPY run_tests.py ./ 

# Install PHP deps
RUN composer install --no-interaction --prefer-dist --no-progress || true

# Default command
CMD ["python3", "./run_tests.py"]