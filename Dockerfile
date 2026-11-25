FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements*.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
RUN curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -w "\n" >> "$(python -m certifi)"

COPY backend /app/backend
COPY supabase /app/supabase

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "run.py"]
