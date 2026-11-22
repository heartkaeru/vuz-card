import uvicorn
import argparse
import os
from pathlib import Path
import sys
from dotenv import load_dotenv

if __name__ == "__main__":
    backend_dir = Path(__file__).resolve().parent
    os.chdir(backend_dir)
    sys.path.insert(0, str(backend_dir))
    load_dotenv(backend_dir.parent / ".env", override=False)
    load_dotenv(backend_dir / ".env", override=False)
    options = argparse.ArgumentParser(description="Local Demo 1 backend; real parser and GigaChat")
    options.add_argument("--db", type=Path, help="Optional registry/workflow SQLite file")
    args = options.parse_args()
    if args.db:
        os.environ["VUZ_MVP_DB"] = str(args.db.resolve())
    from app.api.v1.ai import get_repository
    get_repository().fail_interrupted_jobs()
    host = os.environ.get("VUZ_HOST", "127.0.0.1")
    port = int(os.environ.get("VUZ_PORT", "8000"))
    print(f"VUZ-CRM backend: http://{host}:{port}")
    print(f"API documentation: http://{host}:{port}/docs")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
