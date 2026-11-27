import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

from app.api.v1.auth import router as auth_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.ai import router as ai_router
from logging_config import configure_logging, describe_error

configure_logging()
LOGGER = logging.getLogger("app")

app = FastAPI(
    title="ВУЗ-CRM: Интеллектуальный ассистент по работе с ВУЗ/СПО",
    description="""
    ## Ядро API платформы «Карточка ВУЗа»
    
    Сервер обеспечивает:
    * 📋 **Реестр и Карточки ВУЗов**: просмотр и ручное редактирование данных руководства.
    * 🔄 **Фоновая ИИ-актуализация**: запуск парсера и нейросети для выявления кадровых изменений («было / стало»).
    * 📊 **Генерация презентаций**: автоматическая сборка слайдов (.pptx) к встрече с ректоратом.
    """,
    version="1.0.0",
)

# Настройка CORS для подключения фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение маршрутов версии v1
app.include_router(auth_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1", deprecated=True)
app.include_router(ai_router, prefix="/api/v1")


@app.exception_handler(StarletteHTTPException)
async def log_http_error(request: Request, exc: StarletteHTTPException):
    LOGGER.warning(
        "Запрос отклонён: метод=%s путь=%s код=%s причина=%s",
        request.method,
        request.url.path,
        exc.status_code,
        describe_error(exc.detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    fields = [".".join(str(part) for part in item.get("loc", ())) for item in exc.errors()]
    LOGGER.warning(
        "Запрос не прошёл проверку: метод=%s путь=%s поля=%s",
        request.method,
        request.url.path,
        ", ".join(fields) or "неизвестно",
    )
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(Exception)
async def log_unhandled_error(request: Request, exc: Exception):
    LOGGER.exception(
        "Внутренняя ошибка запроса: метод=%s путь=%s причина=%s",
        request.method,
        request.url.path,
        describe_error(exc),
    )
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})


@app.get("/", tags=["Системные эндпоинты"])
def root():
    return {
        "service": "ВУЗ-CRM API",
        "status": "online",
        "docs_url": "/docs",
        "version": "1.0.0"
    }
