FROM python:3.11-slim

# Устанавливаем системные зависимости, которые нужны для сборки некоторых питоновских пакетов
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Ставим зависимости. Если maigret тянет тяжелые пакеты, тут может быть долго
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
