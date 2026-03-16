# Интеграция TG Account Service

## Деплой на Railway

1. Создай новый проект на Railway
2. Загрузи файлы из папки tg_service/
3. Добавь переменные окружения:
   TG_API_ID         = получи на my.telegram.org
   TG_API_HASH       = получи на my.telegram.org
   API_SECRET        = придумай_секрет
   MAIN_APP_URL      = https://твой-основной-сервис.up.railway.app
   WA_WEBHOOK_SECRET = тот_же_что_в_основном_сервисе

## Переменные в ОСНОВНОМ сервисе

   TG_SERVICE_URL     = https://tg-service-xxx.up.railway.app
   TG_API_SECRET      = тот_же_секрет
   TG_WEBHOOK_SECRET  = тот_же_что_в_tg_сервисе

## Получение API_ID и API_HASH

1. Зайди на https://my.telegram.org
2. Log in → API development tools
3. Создай приложение (любое название)
4. Скопируй App api_id и App api_hash

## TikTok пиксель

Настройки → Meta Pixel & CAPI → TikTok Pixel & Events API
Вставь Pixel ID и Access Token из TikTok Events Manager
