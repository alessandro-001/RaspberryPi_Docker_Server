FROM python:3.11-slim

# Upgrade OS packages to patch known CVEs in the base image
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Upgrade pip and wheel first to patch their own CVEs before installing deps
RUN pip install --upgrade pip wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Run as non-root user to limit blast radius of any exploit
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
