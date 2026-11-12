# Авторизация API

Скопируйте `backend/.env.example` в `backend/.env`, задайте `JWT_SECRET_KEY` и запускайте API через `python run.py`. Секрет должен содержать не менее 32 случайных байт; стабильное значение нужно сохранять между перезапусками сервера. `JWT_ACCESS_TOKEN_MINUTES` по умолчанию равен 1440 (24 часа).

Учётные записи хранятся в `backend/data/auth.sqlite3`. Путь можно переопределить переменной `VUZ_AUTH_DB`. Пароли требуют не менее 5 символов и не более 72 байт (ограничение bcrypt); сохраняется только bcrypt-хеш. Новая учётная запись всегда получает роль `user`.

## Регистрация

`POST /api/v1/auth/register`

```json
{
  "email": "employee@example.com",
  "password": "a-long-passphrase-123"
}
```

При успехе возвращает `201`, публичные данные пользователя и JWT. Повторный email возвращает `409`.

## Вход

`POST /api/v1/auth/login`

```json
{
  "email": "employee@example.com",
  "password": "a-long-passphrase-123"
}
```

Успешный вход возвращает `access_token`, `token_type` и срок действия в секундах. Неверные данные возвращают `401`.

Передавайте токен в заголовке `Authorization: Bearer <access_token>`. Middleware требует действующий JWT для `POST`, `PUT`, `PATCH` и `DELETE` по `/api/v1/universities` и `/api/v1/universities/{university_id}`. GET-запросы к карточкам и реестру остаются открытыми.
