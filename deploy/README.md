# Развёртывание на VPS

Инструкция рассчитана на один Linux-сервер с Docker Compose и внешний Supabase. Домен необязателен: Caddy может получить короткоживущий сертификат и для публичного IPv4.

## Что подготовить

1. VPS с публичным IPv4 и SSH-доступом.
2. Публичный IPv4 сервера. Если есть домен, в DNS добавьте для него A-запись на этот адрес.
3. Строку PostgreSQL `DATABASE_URL` из Supabase Dashboard → Connect.
4. Значения GigaChat и `JWT_SECRET_KEY` для backend. Секреты храните только в env-файле на сервере.

## Файлы с настройками

В корне репозитория создайте `backend/.env` из шаблона и заполните `DATABASE_URL`, `JWT_SECRET_KEY`, `GIGACHAT_CREDENTIALS`, `GIGACHAT_MODEL` и `GIGACHAT_SCOPE`:

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

## Запуск

После установки Docker Engine и Compose Plugin из официальной инструкции для ОС сервера, в каталоге репозитория выполните:

```sh
docker compose up -d --build
docker compose exec backend python -m database.postgres migrate
docker compose ps
docker compose logs --tail=100
```

`migrate` applies any new versioned Supabase schema changes. It does not remove card data; data is purged only when the authenticated API receives an explicit `DELETE /api/v1/ai/cards/{card_id}` request.

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
