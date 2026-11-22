# Развёртывание на VPS

Инструкция рассчитана на один Linux-сервер с Docker Compose, внешний Supabase и домен с HTTPS через Caddy.

## Что подготовить

1. VPS с публичным IPv4 и SSH-доступом.
2. Домен. В DNS добавьте A-запись для домена на IPv4 сервера.
3. Строку PostgreSQL `DATABASE_URL` из Supabase Dashboard → Connect.
4. Значения GigaChat и `JWT_SECRET_KEY` для backend. Секреты храните только в env-файле на сервере.

## Файлы с настройками

В корне репозитория создайте `backend/.env` из шаблона и заполните `DATABASE_URL`, `JWT_SECRET_KEY`, `GIGACHAT_CREDENTIALS`, `GIGACHAT_MODEL` и `GIGACHAT_SCOPE`:

```sh
cp backend/.env.example backend/.env
nano backend/.env
```

Укажите домен для Caddy:

```sh
cp deploy/domain.env.example deploy/domain.env
nano deploy/domain.env
```

Файл `deploy/domain.env` должен содержать публичное доменное имя:

```dotenv
DOMAIN=app.example.com
```

На сервере ограничьте доступ к файлам с секретами:

```sh
chmod 600 backend/.env deploy/domain.env
```

## Запуск

После установки Docker Engine и Compose Plugin из официальной инструкции для ОС сервера, в каталоге репозитория выполните:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

Откройте `https://<ваш-домен>`. Первый запуск Caddy получит TLS-сертификат автоматически, если DNS уже указывает на этот сервер и входящие порты 80 и 443 разрешены.

Backend, frontend и Caddy запускаются отдельными контейнерами. Backend доступен только внутри Docker-сети. SQLite-файлы находятся в постоянном именованном томе `vuz_data`; Caddy хранит сертификаты в `caddy_data` и настройки в `caddy_config`.

Не запускайте `docker compose down -v`, если хотите сохранить SQLite-файлы и сертификаты: флаг `-v` удаляет именованные тома. Перед обновлением проекта и периодически сохраняйте резервную копию тома `vuz_data`.
