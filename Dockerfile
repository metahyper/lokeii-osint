FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libffi-dev \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Клонируем и настраиваем Wiwok
RUN git clone https://github.com/Kirozaku/Wiwok /app/wiwok && \
    cd /app/wiwok && \
    python3 wiwok.py --setup || true

COPY . .

CMD ["python", "main.py"]
