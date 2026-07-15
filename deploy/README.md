# Деплой carry-бота как systemd-сервиса

Запуск бота как системного сервиса: авто-рестарт при падении, переживает
перезагрузку, логи в journald. Бот торгует **реальными деньгами** на mainnet.

## Что нужно знать

- Сервис запускает [`run_carry_testnet.py`](../scripts/run_carry_testnet.py) с
  флагом `--yes` (пропуск интерактивного подтверждения — без TTY он бы завис).
- При остановке (`systemctl stop`) позиция **остаётся открытой** (флаг
  `--flatten-on-exit` не передан) — фандинг продолжает капать между
  рестартами. Это безопасно, т.к. при старте бот видит открытую позицию.
- Логи (stdout/stderr) пишутся в journald автоматически.

## Установка

```bash
cd ~/bybit-algo-bot

# 1. Скопировать юнит в систему
sudo cp deploy/carry-bot.service /etc/systemd/system/

# 2. Перезагрузить конфигурацию systemd
sudo systemctl daemon-reload

# 3. Включить и запустить сейчас (+ автозапуск при загрузке)
sudo systemctl enable --now carry-bot
```

> ⚠️ Перед установкой откройте [`carry-bot.service`](carry-bot.service) и
> проверьте параметры в `ExecStart` (`min-funding`, `max-notional`,
> `leverage`, `equity-fraction`) — они должны совпадать с вашими настройками
> риска.

## Управление

| Действие | Команда |
|---|---|
| Статус | `sudo systemctl status carry-bot` |
| Логи в реальном времени | `journalctl -u carry-bot -f` |
| Последние 100 строк | `journalctl -u carry-bot -n 100` |
| Перезапуск | `sudo systemctl restart carry-bot` |
| Остановить (позиция остаётся) | `sudo systemctl stop carry-bot` |
| Полностью отключить | `sudo systemctl disable --now carry-bot` |

## Сначала остановите ручной запуск

Если бот уже крутится в терминале (Terminal 1), остановите его перед запуском
сервиса, чтобы не было **двух процессов** одновременно (оба пытались бы
торговать одним аккаунтом):

```bash
# В терминале с запущенным ботом: Ctrl+C
# затем запустите сервис
```

## Проверка после старта

```bash
sudo systemctl status carry-bot        # должен быть active (running)
journalctl -u carry-bot -n 20 --no-pager   # последние логи
```

Если статус `active (running)` и в логах видны строки опроса фандинга
(`· [none] funding=...`) — всё работает.

## Изменение параметров

1. Отредактируйте `ExecStart` в [`carry-bot.service`](carry-bot.service).
2. `sudo cp deploy/carry-bot.service /etc/systemd/system/`
3. `sudo systemctl daemon-reload && sudo systemctl restart carry-bot`
