from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.universities import router as universities_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.presentations import router as presentations_router
from app.api.v1.ai import router as ai_router

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
app.include_router(universities_router, prefix="/api/v1", deprecated=True)
app.include_router(jobs_router, prefix="/api/v1", deprecated=True)
app.include_router(presentations_router, prefix="/api/v1", deprecated=True)
app.include_router(ai_router, prefix="/api/v1")


@app.get("/", tags=["Системные эндпоинты"])
def root():
    return {
        "service": "ВУЗ-CRM API",
        "status": "online",
        "docs_url": "/docs",
        "version": "1.0.0"
    }
