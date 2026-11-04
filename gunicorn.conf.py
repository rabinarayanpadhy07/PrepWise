"""
Gunicorn configuration for Render deployment.

Render automatically provides:
- PORT: The port the application must bind to (typically 10000).
- WEB_CONCURRENCY: Optional number of worker processes.
"""

import os

# Server socket
# Render requires binding to 0.0.0.0 and the port specified by $PORT.
port = os.environ.get("PORT", "10000")
bind = f"0.0.0.0:{port}"

# Worker processes & concurrency
# Render free/starter tiers have 512MB RAM. Concurrency=2 with 2 threads per
# worker handles multiple simultaneous requests without exceeding memory limits.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("PYTHON_THREADS", "2"))
worker_class = "gthread"

# Timeouts
# AI completions and PDF extractions can take tens of seconds; 120s avoids premature 502s.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
keepalive = 5

# Logging to stdout and stderr so Render log streams capture everything
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
capture_output = True
enable_stdio_inheritance = True
