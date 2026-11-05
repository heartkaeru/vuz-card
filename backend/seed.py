"""
Seed script: наполнение базы данных реальными вузами для CRM.
Запускается для создания каталога карточек ВУЗов в базе SQLite.
"""

from pathlib import Path
import sys

# Добавляем путь к backend
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database.demo1_store import DemoRepository

UNIVERSITIES = [
    ("Байкальский государственный университет", "Иркутск", "https://bgu.ru"),
    ("Московский государственный технический университет имени Н.Э. Баумана", "Москва", "https://bmstu.ru"),
    ("Санкт-Петербургский государственный университет", "Санкт-Петербург", "https://spbu.ru"),
    ("Национальный исследовательский университет «Высшая школа экономики»", "Москва", "https://hse.ru"),
    ("Московский физико-технический институт", "Долгопрудный, Московская область", "https://mipt.ru"),
    ("Московский государственный университет имени М.В. Ломоносова", "Москва", "https://msu.ru"),
    ("Национальный исследовательский ядерный университет «МИФИ»", "Москва", "https://mephi.ru"),
]

def seed():
    db_path = Path(__file__).resolve().parent / "output/mvp/registry.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    repo = DemoRepository(db_path)
    print(f"Инициализация базы данных: {db_path}")
    
    for name, city, website in UNIVERSITIES:
        card = repo.create_card(name, city, website)
        print(f" [OK] Добавлена карточка: {name} ({city}) -> id: {card['id']}")
    
    total = len(repo.cards())
    print(f"\nВсего карточек в базе данных: {total}")

if __name__ == "__main__":
    seed()
