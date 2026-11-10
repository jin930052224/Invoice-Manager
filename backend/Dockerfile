FROM python:3.11-slim-bookworm

WORKDIR /app

# Use a nearby Debian mirror; these packages are needed by PaddleOCR and PDF parsing.
RUN sed -i 's@deb.debian.org/debian@mirrors.aliyun.com/debian@g; s@deb.debian.org/debian-security@mirrors.aliyun.com/debian-security@g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --timeout 120 \
    -r requirements.txt

# Copy application
COPY . .

EXPOSE 18080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080", "--reload"]
