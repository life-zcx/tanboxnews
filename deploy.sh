#!/bin/bash
# Tanbox News AI — Fast Server Deployment Script

echo "==========================================="
echo "🚀 Tanbox News AI — Запуск в Docker на VPS"
echo "==========================================="

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker не найден. Установите Docker командой:"
    echo "   curl -fsSL https://get.docker.com | sh"
    exit 1
fi

# Ensure data directory exists on host
mkdir -p data

# Ensure .env exists
if [ ! -f .env ]; then
    echo "⚠️ Файл .env не найден! Создаю из .env.example..."
    cp .env.example .env
    echo "❗ Пожалуйста, откройте .env (nano .env) и укажите ваши ключи перед запуском."
    exit 1
fi

echo "📦 Сборка и фоновый запуск контейнера..."
docker compose down 2>/dev/null
docker compose up -d --build

echo "✅ Контейнер успешно запущен!"
echo "📋 Для просмотра логов в реальном времени выполните:"
echo "   docker compose logs -f"
echo "==========================================="

