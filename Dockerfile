FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libffi-dev \
    curl \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Скачиваем PhoneInfoga (Go-бинарник для Linux)
RUN curl -sSL https://github.com/sundowndev/phoneinfoga/releases/latest/download/phoneinfoga_Linux_x86_64.tar.gz \
    | tar -xz -C /usr/local/bin/ phoneinfoga && \
    chmod +x /usr/local/bin/phoneinfoga

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
