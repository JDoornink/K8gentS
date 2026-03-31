# Use a lightweight python base image
FROM python:3.11-slim

# Create generic non-root user (for security context compliance)
RUN useradd -u 1000 -m agent

# Set working directory
WORKDIR /app

# Install dependencies first (leverage docker layer caching)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/main.py .

# Switch to the non-root user
USER 1000

# Start K8gent Watcher
CMD ["python", "main.py"]
