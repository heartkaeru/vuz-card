# Развёртывание на VPS

Инструкция рассчитана на один Linux-сервер с Docker Compose и SQLite в постоянном Docker-томе. Домен необязателен: Caddy может получить короткоживущий сертификат и для публичного IPv4.

## Что подготовить

1. VPS с публичным IPv4 и SSH-доступом.
2. Публичный IPv4 сервера. Если есть домен, в DNS добавьте для него A-запись на этот адрес.
3. Значения GigaChat и `JWT_SECRET_KEY` для backend. Секреты храните только в env-файле на сервере.

## Файлы с настройками

В корне репозитория создайте `backend/.env` из шаблона и заполните `JWT_SECRET_KEY`, `GIGACHAT_CREDENTIALS`, `GIGACHAT_MODEL` и `GIGACHAT_SCOPE`:

```sh
cp backend/.env.example backend/.env
nano backend/.env
```

Укажите адрес, по которому будет открываться приложение:

```sh
cp deploy/site.env.example deploy/site.env
nano deploy/site.env
```

Файл `deploy/site.env` содержит публичный IPv4 или доменное имя:

```dotenv
SITE_ADDRESS=203.0.113.10
```

На сервере ограничьте доступ к файлам с секретами:

```sh
chmod 600 backend/.env deploy/site.env
```

## Ресурсы сервера и Chromium

Парсер использует headless-Chromium (Playwright) для SPA-сайтов вузов и
проверок «вы не бот» на агрегаторах. Ориентиры для сервера 1 vCPU / 2 ГБ / 100 ГБ:

| Что | Память | Диск |
| --- | --- | --- |
| Система и Docker | ~250–350 МБ | — |
| Backend (FastAPI, парсер, словари pymorphy3) | ~250–400 МБ | образ ~1,5 ГБ, из них Chromium с библиотеками ~0,6 ГБ |
| Chromium во время рендеринга | ~250–400 МБ | — |
| Frontend (Next.js) | ~150–250 МБ | ~0,5 ГБ |
| Caddy | ~30 МБ | ~0,1 ГБ |

Итого в пике около 1–1,4 ГБ из 2 ГБ: Chromium помещается, если одновременно
работает **один** браузер. Это задано по умолчанию (`PARSER_MAX_BROWSERS=1`):
параллельные задания ждут очереди, а не запускают второй браузер. Картинки,
видео и шрифты в браузере не загружаются. При сборке образа нужно ещё
~2–3 ГБ свободного диска и ~1 ГБ памяти, поэтому собирайте, когда на сервере
не идёт сбор.

Перед запуском проверьте свободные ресурсы:

```sh
free -h
df -h /
docker system df
```

На 2 ГБ памяти рекомендуется файл подкачки на 2 ГБ: он страхует сборку образа и
пиковые моменты от остановки процессов из-за нехватки памяти.

```sh
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Проверить, что Chromium в контейнере запускается (при сборке это делается
автоматически — в логе должна быть строка `chromium ok`):

```sh
docker compose exec backend python -c "from parser.stealth import StealthFetcher; f = StealthFetcher(enable_playwright=True); print(f._playwright_get('https://example.com', 30).status_code); f.close()"
```

## Запуск

После установки Docker Engine и Compose Plugin из официальной инструкции для ОС сервера, в каталоге репозитория выполните:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

Откройте `https://<публичный-ip>` или `https://<домен>`. Caddy использует профиль Let's Encrypt `shortlived` и автоматически обновляет сертификат. Публичный IPv4 должен быть постоянным, а входящие порты 80 и 443 — доступны снаружи. Let’s Encrypt выдаёт такие сертификаты на 160 часов, поэтому автоматическое продление важно. См. [объявление Let’s Encrypt](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) и [документацию Caddy](https://caddyserver.com/docs/caddyfile/directives/tls#issuers).

Backend, frontend и Caddy запускаются отдельными контейнерами. Backend доступен только внутри Docker-сети. SQLite-файлы находятся в постоянном именованном томе `vuz_data`; Caddy хранит сертификаты в `caddy_data` и настройки в `caddy_config`.

Не запускайте `docker compose down -v`, если хотите сохранить SQLite-файлы и сертификаты: флаг `-v` удаляет именованные тома. Перед обновлением проекта и периодически сохраняйте резервную копию тома `vuz_data`.

## Временная бесплатная ссылка без домена и выделенного IP

Для демонстрации можно запустить Cloudflare Quick Tunnel. Он создаёт временный HTTPS-адрес `trycloudflare.com`; домен и входящие порты VPS не нужны. Из корня репозитория запустите backend, frontend и туннель:

```sh
docker compose --profile quick-tunnel up -d --build backend frontend cloudflared
docker compose logs -f cloudflared
```

Скопируйте адрес `https://…trycloudflare.com` из логов и откройте его в браузере. Ссылка действует, пока работает этот туннель, и меняется после его пересоздания или перезапуска. Cloudflare не обещает доступность для Quick Tunnels; официально они предназначены для проверки и разработки, а не для постоянного сайта. Ограничение — до 200 одновременных запросов; Server-Sent Events не поддерживаются. [Документация Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).
