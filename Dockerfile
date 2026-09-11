# Используем компактный образ Python на базе Alpine Linux
FROM python:3.11-alpine

# Устанавливаем системные зависимости для сборки lxml и компиляции C-расширений
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libxml2-dev \
    libxslt-dev

# Задаём рабочую директорию
WORKDIR /app

# Отключаем буферизацию вывода Python и запись .pyc файлов
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/checker.db

# Копируем списки зависимостей
COPY requirements.txt .

# Доставляем lxml в requirements и устанавливаем все пакеты без кэширования
RUN echo "lxml>=4.9.0" >> requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения
COPY main.py .

# Создаём директорию под SQLite базу данных
RUN mkdir -p /data

# Указываем монтируемый том для сохранения базы данных при перезапусках контейнера
VOLUME ["/data"]

# Запускаем бота
CMD ["python", "main.py"]