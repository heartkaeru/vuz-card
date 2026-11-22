FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements*.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "run.py"]
