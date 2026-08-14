# Edge AI Person Detection - ARM64 Container
# Target: Cradlepoint routers (linux/arm64)
# Compressed image size target: <= 2 GB

# --- Builder stage: install Python packages, then strip unnecessary files ---
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && find /install -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    find /install -type d -name "tests" -exec rm -rf {} + 2>/dev/null; \
    find /install -type d -name "test" -exec rm -rf {} + 2>/dev/null; \
    find /install -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null; \
    find /install -name "*.pyc" -delete 2>/dev/null; \
    rm -rf /install/lib/python3.12/site-packages/pip 2>/dev/null; \
    rm -rf /install/bin/pip* 2>/dev/null; \
    rm -rf /install/lib/python3.12/site-packages/numpy/f2py 2>/dev/null; \
    rm -rf /install/lib/python3.12/site-packages/numpy/typing/tests 2>/dev/null; \
    exit 0

# --- Runtime stage: minimal image ---
FROM python:3.12-slim

WORKDIR /app

# Copy only the installed packages from builder
COPY --from=builder /install /usr/local

# Copy cp.py for router config store access
COPY cp.py /app/cp.py

# Copy model files
COPY models/ /app/models/

# Copy application source (includes templates/ and static/)
COPY src/ /app/src/

# Copy help file for web UI help endpoint
COPY help.md /app/help.md

# Expose web interface port
EXPOSE 8080

# Run the application
CMD ["python", "src/main.py"]
