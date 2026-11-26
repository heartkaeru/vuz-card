FROM python:3.11-slim-bookworm

# PLAYWRIGHT_BROWSERS_PATH: браузеры в фиксированном каталоге, не зависят от
# HOME пользователя, под которым запущен контейнер.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements*.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
# МГУ и другие SPA-сайты, проверки «вы не бот» (Вузопедия) и TLS-блокировки:
# парсеру нужен настоящий браузер. --with-deps ставит системные библиотеки и
# шрифты Chromium; затем проверяем, что браузер действительно запускается.
RUN python -m playwright install --with-deps chromium \
    && python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(args=['--disable-dev-shm-usage']); b.close(); p.stop(); print('chromium ok')"
RUN curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -w "\n" >> "$(python -m certifi)"

COPY backend /app/backend
COPY supabase /app/supabase

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "run.py"]
