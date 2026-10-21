# --- Stage 1: Builder ---
FROM python:3.11-slim AS builder

WORKDIR /app

# Install dependencies in a separate layer for caching
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: Runtime ---
FROM python:3.11-slim

# Install gosu for proper privilege dropping in entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source (backend/ + frontend/ as siblings)
COPY --chown=appuser:appgroup backend/ ./backend/
COPY --chown=appuser:appgroup frontend/ ./frontend/

# Run the application as appuser; provision writable bind-mount storage before startup
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Expose the API port
EXPOSE 8003

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8003/health')" || exit 1

# WORKDIR is backend/ so flat imports (from llm, from search, etc.) work
# and ../frontend resolves to /app/frontend
WORKDIR /app/backend

# Run with uvicorn (no --reload in production)
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1", "--log-level", "info"]
