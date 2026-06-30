# Деплой DEXMIND на Render — пошагово

## Шаг 1 — Залить код на GitHub

```bash
cd pump_scanner_v6
git init
git add .
git commit -m "DEXMIND bot — initial commit"
```

Создайте новый репозиторий на github.com (Private — раз там работает реальный
торговый бот, лучше не светить логику публично). Затем:

```bash
git remote add origin https://github.com/ВАШ_username/dexmind-bot.git
git branch -M main
git push -u origin main
```

Проверьте, что `.env` и `*.db` **не** попали в коммит — `.gitignore` должен их
отфильтровать:
```bash
git status   # .env и pump_scanner.db не должны быть в списке
```

## Шаг 2 — Тип сервиса на Render

**Важно: создавайте Background Worker, не Web Service.**

У бота нет HTTP-сервера — он не слушает порт, не отвечает на запросы извне.
Это чистый процесс, который держит WebSocket-соединение и шлёт сообщения в
Telegram. Web Service на Render ожидает открытый порт и будет считать сервис
нездоровым, если порта нет.

В Render Dashboard:
1. **New** → **Background Worker**
2. Подключите GitHub-репозиторий, который вы запушили на шаге 1
3. **Name**: `dexmind-bot` (или как удобно)
4. **Region**: любой (Frankfurt подойдёт, ближе к вам)
5. **Branch**: `main`
6. **Runtime**: Python 3
7. **Build Command**:
   ```
   pip install -r requirements.txt
   ```
8. **Start Command**:
   ```
   python main.py
   ```

## Шаг 3 — Переменные окружения

В разделе **Environment** добавьте три переменные (значения — ваши реальные ключи,
**не** коммитьте их в git):

| Key | Value |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `CHAT_ID` | ваш Telegram chat ID |
| `HELIUS_KEY` | ваш Helius API key |

`DB_PATH` и `LOG_LEVEL` можно не задавать — у них есть дефолты в `config.py`.

**Опционально, только для реальной автоторговли** (`/paper off` в Telegram):

| Key | Value |
|---|---|
| `WALLET_PRIVATE_KEY` | base58-приватный ключ кошелька (экспорт из Phantom) |

Без этой переменной бот прекрасно работает в paper-режиме (симуляция автоторговли)
— просто `/paper off` откажет, пока ключ не задан. Если задаёте — храните его
**только** в Render Environment, никогда не коммитьте в репозиторий.

## Шаг 4 — Персистентность SQLite (важно для self-learning и истории)

**Проблема:** файловая система на Render Background Worker по умолчанию
эфемерна. При каждом редеплое (push в main, ручной restart, перезапуск после
сбоя) файл `pump_scanner.db` создаётся с нуля — пропадает история создателей,
веса self-learning, список алертов.

**Решение — Persistent Disk:**
1. В настройках сервиса → **Disks** → **Add Disk**
2. **Mount Path**: `/data`
3. **Size**: 1 GB более чем достаточно для SQLite на старте
4. Добавьте переменную окружения `DB_PATH` = `/data/pump_scanner.db`

Без этого шага бот будет работать, но каждый редеплой откатывает всю
накопленную статистику и self-learning веса на ноль.

## Шаг 5 — Проверка после деплоя

Откройте **Logs** в Render Dashboard. Должны появиться строки:
```
DB initialised (v6)
Blacklist: N entries
WS: подключаемся к pump.fun...
WS: подписка активна
Pump Scanner v6 — starting
Started N tasks
```

Если вместо этого видите:
```
ОШИБКА: не заданы переменные окружения BOT_TOKEN / CHAT_ID / HELIUS_KEY.
```
— значит переменные из Шага 3 не сохранились или сервис не передеплоился после
их добавления. Нажмите **Manual Deploy** → **Deploy latest commit**.

## Стоимость

Background Worker на Render не имеет бесплатного тарифа (в отличие от Web
Service с автозасыпанием) — это постоянно работающий процесс, что и нужно
боту 24/7. Starter план (~$7/мес) подходит для этой нагрузки: 10 analysis
workers + WS listener + несколько фоновых тасков — это лёгкий I/O-bound
процесс, не требует много CPU/RAM.

## Обновление кода в будущем

```bash
git add .
git commit -m "описание изменений"
git push
```

Render по умолчанию настроен на **auto-deploy** при пуше в `main` — бот
передеплоится автоматически. Если временно не нужно — отключите Auto-Deploy
в настройках сервиса и деплойте вручную через **Manual Deploy**.
