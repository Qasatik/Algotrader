# 🔧 AlgoTrader — Технический чек-лист запуска

> **Цель:** выполнив все пункты по порядку, получить полностью рабочую
> систему: carry-бот торгует на Bybit, SaaS-бот принимает подписки в Telegram.

---

## 📋 Что нужно: краткий список

| Компонент | Требование |
|-----------|-----------|
| Сервер (VPS) | Ubuntu 22+, 2 vCPU, 2 GB RAM, 20 GB SSD |
| Python | 3.11+ (рекомендуется 3.13) |
| VPN | Для доступа к api.telegram.org (заблокирован в РФ) |
| Bybit аккаунт | С депозитом $200+ (для mainnet) |
| Bybit API ключ | Права: Spot + Derivatives Trade, БЕЗ Withdraw |
| Telegram бот | Токен от @BotFather |
| USDT кошелёк | TRC-20 для приёма оплат |

---

## ШАГ 1: Сервер и VPN (День 1)

### 1.1 VPS

```bash
# Проверить, что сервер подходит
uname -m          # должно быть x86_64
python3 --version # 3.11+
free -h           # минимум 2 GB RAM
df -h             # минимум 10 GB свободно
```

### 1.2 VPN для Telegram API

Telegram API (`api.telegram.org`) заблокирован в РФ. Без VPN бот не запустится.

**Вариант A — прокси через SSH-туннель (бесплатно):**
```bash
# Если есть зарубежный сервер с доступом к Telegram:
ssh -D 1080 -N user@foreign-server.com &
export ALL_PROXY=socks5://127.0.0.1:1080
```

**Вариант B — Outline VPN / Amnezia (проще):**
```bash
# Установить клиент, подключиться, проверить:
curl -s --connect-timeout 10 https://api.telegram.org/ | head -1
# Должен вернуть HTML (не таймаут)
```

**Вариант C — 3proxy/Squid на зарубежном VPS:**
```bash
# Настроить HTTP-прокси, указать в .env:
export HTTPS_PROXY=http://proxy-host:port
```

> ⚠️ **Проверка:** `curl https://api.telegram.org/bot<TOKEN>/getMe`
> должен вернуть JSON с именем бота.

---

## ШАГ 2: Установка зависимостей (День 1)

### 2.1 Системные пакеты

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### 2.2 Python-зависимости

```bash
cd ~/bybit-algo-bot
pip3 install --user -r requirements.txt
# Или если нет requirements.txt:
pip3 install --user pybit httpx structlog python-telegram-bot==21.4 \
    pydantic pydantic-settings tenacity pandas numpy
```

### 2.3 Проверка установки

```bash
python3 -c "from core.exchange import BybitExchange; print('OK')"
python3 -c "from saas.telegram_saas import SaaSTelegramBot; print('OK')"
python3 -c "from core.carry_strategy import CarryStrategy; print('OK')"
```

---

## ШАГ 3: Конфигурация .env (День 1)

### 3.1 Telegram бот

```bash
# 1. Создать бота через @BotFather в Telegram
# 2. Получить токен вида: 8255616060:AAE...
# 3. Узнать свой Telegram ID через @userinfobot
```

### 3.2 Заполнить .env

```bash
cd ~/bybit-algo-bot
cp .env.example .env  # если ещё нет
nano .env
```

**Обязательные поля:**

```ini
# Telegram
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН_ОТ_BOTFATHER
TELEGRAM_ADMIN_IDS=ВАШ_TELEGRAM_ID   # от @userinfobot

# SaaS (уже заполнено, но проверьте)
SAAS_MASTER_SECRET=уже_сгенерирован   # AES-256 ключ
SAAS_DB_PATH=data/saas.db
SAAS_ADMIN_IDS=ВАШ_TELEGRAM_ID
SAAS_USDT_WALLET=ВАШ_TRC20_АДРЕС      # для приёма оплат

# Bybit API (MAINNET для реальной торговли)
BYBIT_API_KEY=ВАШ_MAINNET_API_KEY
BYBIT_API_SECRET=ВАШ_MAINNET_API_SECRET
BYBIT_TESTNET=false                    # ← false для реальной торговли!
```

### 3.3 Сгенерировать SAAS_MASTER_SECRET (если нужно)

```bash
python3 -c "from saas.crypto import generate_master_secret; print(generate_master_secret())"
# Вставить результат в .env → SAAS_MASTER_SECRET
```

---

## ШАГ 4: API-ключ Bybit (День 1)

### 4.1 Создание ключа

1. Зайти на https://www.bybit.com → Settings → API Management
2. Нажать **Create New Key**
3. **ПРАВА (КРИТИЧНО):**

| Право | Значение |
|-------|----------|
| Spot Trade | ✅ Включить |
| Derivatives Trade | ✅ Включить |
| Withdraw | ❌ **ОТКЛЮЧИТЬ!** |
| Wallet Transfer | ❌ **ОТКЛЮЧИТЬ!** |
| IP Binding | ✅ Указать IP сервера |

4. Скопировать API Key и API Secret

> ⚠️ Бот **автоматически проверит** права ключа при `/connect` и отклонит
> ключ с правами вывода. Но лучше создать правильно с самого начала.

### 4.2 Депозит

- Пополнить Bybit аккаунт на **$200+** (USDT)
- Перевести в Unified Account (Settings → Account)

---

## ШАГ 5: Запуск carry-бота (День 2)

### 5.1 Проверка на testnet (рекомендуется)

```bash
cd ~/bybit-algo-bot
# Установить BYBIT_TESTNET=true в .env
python3 scripts/run_carry_testnet.py --symbol BTCUSDT --dry-run
```

### 5.2 Запуск на mainnet (РЕАЛЬНЫЕ ДЕНЬГИ)

```bash
cd ~/bybit-algo-bot
# Установить BYBIT_TESTNET=false в .env
python3 scripts/run_carry_multi.py \
    --symbols BTCUSDT,ETHUSDT \
    --top-N 3 \
    --equity-pct 0.8 \
    --funding-min 0.0001 \
    --basis-guard 0.003 \
    --flatten-on-exit
```

**Параметры:**
- `--symbols` — список символов для сканирования
- `--top-N 3` — торговать топ-3 по фандингу
- `--equity-pct 0.8` — использовать 80% депозита
- `--funding-min 0.0001` — минимальный фандинг (0.01% за 8ч = ~11% APR)
- `--basis-guard 0.003` — защита от сквиза (0.3% basis)
- `--flatten-on-exit` — закрыть позиции при Ctrl+C

### 5.3 Проверка позиции

```bash
python3 scripts/show_position.py --symbol BTCUSDT
```

---

## ШАГ 6: Запуск SaaS Telegram-бота (День 2)

### 6.1 Инициализация БД

```bash
cd ~/bybit-algo-bot
python3 -c "from saas.database import Database; Database('data/saas.db').init()"
```

### 6.2 Запуск бота

```bash
cd ~/bybit-algo-bot
bash -c 'set -a; . ./.env; set +a; export PYTHONPATH=$(pwd); \
    python3 scripts/run_saas_bot.py'
```

### 6.3 Проверка в Telegram

1. Найти бота в Telegram (по токену от BotFather)
2. Отправить `/start` → бот должен ответить приветствием + клавиатура
3. Проверить команды:
   - `/pricing` → тарифы с кнопками
   - `/help` → список команд
   - `/connect KEY SECRET` → подключить API-ключ (бот проверит права)
   - `/start_bot` → запустить торговлю

---

## ШАГ 7: systemd-сервис (автозапуск) (День 2–3)

### 7.1 Carry-бот

```bash
sudo tee /etc/systemd/system/carry-bot.service << 'EOF'
[Unit]
Description=Carry Trading Bot
After=network.target

[Service]
Type=simple
User=pepe
WorkingDirectory=/home/pepe/bybit-algo-bot
EnvironmentFile=/home/pepe/bybit-algo-bot/.env
Environment=PYTHONPATH=/home/pepe/bybit-algo-bot
ExecStart=/usr/bin/python3 scripts/run_carry_multi.py \
    --symbols BTCUSDT,ETHUSDT,SOLUSDT --top-N 3 --equity-pct 0.8 \
    --funding-min 0.0001 --basis-guard 0.003 --flatten-on-exit
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable carry-bot
sudo systemctl start carry-bot
```

### 7.2 SaaS Telegram-бот

```bash
sudo tee /etc/systemd/system/saas-bot.service << 'EOF'
[Unit]
Description=SaaS Telegram Bot
After=network.target

[Service]
Type=simple
User=pepe
WorkingDirectory=/home/pepe/bybit-algo-bot
EnvironmentFile=/home/pepe/bybit-algo-bot/.env
Environment=PYTHONPATH=/home/pepe/bybit-algo-bot
ExecStart=/usr/bin/python3 scripts/run_saas_bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable saas-bot
sudo systemctl start saas-bot
```

### 7.3 Проверка

```bash
sudo systemctl status carry-bot
sudo systemctl status saas-bot
sudo journalctl -u carry-bot -f --no-pager   # логи в реальном времени
sudo journalctl -u saas-bot -f --no-pager
```

---

## ШАГ 8: Тестирование (День 3)

### 8.1 Чек-лист carry-бота

- [ ] Бот открывает позицию при положительном фандинге
- [ ] Short perp + Long spot = дельта ≈ 0
- [ ] Basis-guard срабатывает при сквизе
- [ ] Бот закрывает позицию при развороте фандинга
- [ ] Стоп-лосс установлен на бирже
- [ ] P&L логируется в data/carry_pnl.csv
- [ ] Сделки логируются в data/carry_trades.csv
- [ ] Telegram-уведомления приходят при open/close

### 8.2 Чек-лист SaaS-бота

- [ ] `/start` → регистрация + 7 дней триала
- [ ] `/pricing` → тарифы с инлайн-кнопками
- [ ] `/subscribe basic` → создаётся счёт
- [ ] `/pay` → инструкция по оплате USDT
- [ ] `/admin_pay <id>` → подтверждение оплаты (только админ)
- [ ] `/connect KEY SECRET` → ключ проверяется, шифруется, сообщение удаляется
- [ ] `/start_bot` → бот запускается (нужен ключ + подписка)
- [ ] `/stop_bot` → бот останавливается
- [ ] `/status` → статус, тариф, последний цикл
- [ ] Кнопки меню работают (Тарифы, Мой тариф, Оплатить, и т.д.)
- [ ] Реферальная ссылка работает

### 8.3 Полный E2E тест

```
1. /start (новый юзер) → триал 7 дней
2. /connect API_KEY API_SECRET → ключ принят
3. /start_bot → торговля запущена
4. Подождать 8ч → проверить сбор фандинга
5. /status → виден последний цикл
6. /stop_bot → торговля остановлена
7. /pricing → выбрать Pro
8. /subscribe pro → счёт создан
9. /pay → USDT-инструкция
10. /admin_pay <id> → подписка активирована
11. /myplan → тариф Pro, 30 дней
```

---

## ШАГ 9: Мониторинг (постоянно)

### 9.1 Команды для проверки

```bash
# Статус ботов
sudo systemctl status carry-bot saas-bot

# Позиции на Bybit
python3 scripts/show_position.py --symbol BTCUSDT

# История сделок
python3 scripts/show_trades.py --local

# P&L (USDT и BTC)
python3 -c "from core.pnl_tracker import load_history, summary; \
    h = load_history(); print(summary(h))"

# Статистика по периодам
python3 -c "from core.carry_stats import load_stats, format_stats; \
    s = load_stats(); print(format_stats('day', s))"

# Логи carry-бота
tail -50 data/carry_multi.log

# Логи SaaS-бота
tail -50 data/saas_bot.log
```

### 9.2 Алерты

Бот отправляет Telegram-уведомления при:
- ✅ Открытии позиции (символ, размер, фандинг)
- ✅ Закрытии позиции (P&L за период)
- ✅ Ребалансировке (выравнивание дельты)
- ⚠️ Приближении к ликвидации
- ❌ Ошибке (network, API, etc.)

---

## ШАГ 10: Бэкапы (ежедневно)

### 10.1 Что бэкапить

```bash
# База данных SaaS (юзеры, подписки, инвойсы)
cp data/saas.db data/backups/saas-$(date +%Y%m%d).db

# История P&L
cp data/carry_pnl.csv data/backups/carry_pnl-$(date +%Y%m%d).csv

# История сделок
cp data/carry_trades.csv data/backups/carry_trades-$(date +%Y%m%d).csv

# .env (секреты!)
cp .env data/backups/env-$(date +%Y%m%d).backup
```

### 10.2 Cron для автоматических бэкапов

```bash
crontab -e
# Добавить:
0 3 * * * cd /home/pepe/bybit-algo-bot && \
    cp data/saas.db data/backups/saas-$(date +\%Y\%m\%d).db && \
    find data/backups/ -mtime +30 -delete
```

---

## 🚨 Устранение неполадок

| Проблема | Решение |
|----------|---------|
| `TimedOut` при запуске бота | Telegram API заблокирован → включить VPN |
| `ModuleNotFoundError: No module named 'saas'` | `export PYTHONPATH=$(pwd)` |
| `Conflict: terminated by other getUpdates` | Другой процесс использует тот же токен. Убить старый: `pkill -f run_saas` |
| Бот не открывает позиции | Проверить фандинг: `python3 scripts/scan_funding.py`. Минимум: `--funding-min 0.0001` |
| `Insufficient balance` | Пополнить Bybit аккаунт, проверить Unified Account |
| API ключ отклонён `/connect` | Убрать права Withdraw/Wallet в настройках Bybit |
| Позиция в убытке | Нормально для carry: P&L колеблется, фандинг компенсирует за 8ч |

---

## ✅ Финальный чек-лист готовности

- [ ] VPN работает (`curl https://api.telegram.org/` отвечает)
- [ ] .env заполнен (токен, ключи, admin ID, USDT кошелёк)
- [ ] Bybit API ключ: Trade only, без Withdraw, с IP-binding
- [ ] Carry-бот торгует на mainnet (позиция открыта)
- [ ] SaaS-бот отвечает на `/start` в Telegram
- [ ] systemd-сервисы включены (`enable`)
- [ ] Бэкапы настроены в cron
- [ ] Самозанятость зарегистрирована
- [ ] Оферта и политика конфиденциальности на сайте
- [ ] Минимум 2 недели track-record перед запуском рекламы

> **Готово!** Система работает: carry-бот собирает фандинг 24/7,
> SaaS-бот принимает новых подписчиков и платежи.
