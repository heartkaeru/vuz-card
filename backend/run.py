import uvicorn

if __name__ == "__main__":
    print("🚀 Запуск сервера ВУЗ-CRM на http://127.0.0.1:8000")
    print("📚 Документация Swagger UI доступна по адресу: http://127.0.0.1:8000/docs")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
