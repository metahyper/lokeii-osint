# Используем официальный образ PhoneInfoga как базовый источник бинарника
FROM sundowndev/phoneinfoga:latest AS phoneinfoga

FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем готовый бинарник PhoneInfoga из официального образа
COPY --from=phoneinfoga /app/phoneinfoga /usr/local/bin/phoneinfoga
RUN chmod +x /usr/local/bin/phoneinfoga

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
