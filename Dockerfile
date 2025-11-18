FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application
COPY . .

# Create a directory for mounted repos (users will mount their repo here)
RUN mkdir -p /workspace

# Create entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set environment variables for config file paths
ENV APIMESH_CONFIG_PATH=/app/config.yml
ENV APIMESH_USER_CONFIG_PATH=/workspace/apimesh/config.json
ENV APIMESH_USER_REPO_PATH=/workspace
ENV APIMESH_OUTPUT_FILEPATH=/workspace/apimesh/swagger.json

# Set the entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]

# Default command - run interactively if no arguments provided
# Users can override by passing arguments: docker run ... qodexai/apimesh --help
CMD []

