import uvicorn
import argparse
import os
from pathlib import Path
import sys
from dotenv import load_dotenv
from logging_config import configure_logging

if __name__ == "__main__":
    backend_dir = Path(__file__).resolve().parent
    os.chdir(backend_dir)
    sys.path.insert(0, str(backend_dir))
    load_dotenv(backend_dir.parent / ".env", override=False)
    load_dotenv(backend_dir / ".env", override=False)
    configure_logging()
    options = argparse.ArgumentParser(description="Сервер приложения и сборщика данных")
    options.add_argument("--db", type=Path, help="Необязательный путь к базе данных")
    args = options.parse_args()
    if args.db:
        os.environ["VUZ_MVP_DB"] = str(args.db.resolve())
    from app.api.v1.ai import get_repository
    get_repository().fail_interrupted_jobs()
    host = os.environ.get("VUZ_HOST", "127.0.0.1")
    port = int(os.environ.get("VUZ_PORT", "8000"))
    print(f"Сервер запущен: http://{host}:{port}", flush=True)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        log_config=None,
        access_log=False,
    )
