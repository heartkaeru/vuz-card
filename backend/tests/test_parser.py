from pathlib import Path
import unittest

try:  # запуск из backend/ (pytest)
    from parser import main as parser_main
    from parser.cleaner import HTMLCleaner
    from parser.config import SectionSpec, Settings, UNIVERSITY_PROFILES
    from parser.main import (
        SvedenTableParser,
        UniversityParser,
        _looks_like_table_name,
        diff_cards,
        load_university_list,
    )
    from parser.models import (
        Event,
        Leadership,
        Project,
        SourceDocument,
        UniversityData,
        UniversityInfo,
    )
    from parser.utils import (
        canonical_url,
        looks_like_person_name,
        normalize_phone,
        parse_date,
    )
except ImportError:  # запуск из корня репозитория
    from backend.parser import main as parser_main
    from backend.parser.cleaner import HTMLCleaner
    from backend.parser.config import SectionSpec, Settings, UNIVERSITY_PROFILES
    from backend.parser.main import (
        SvedenTableParser,
        UniversityParser,
        _looks_like_table_name,
        diff_cards,
        load_university_list,
    )
    from backend.parser.models import (
        Event,
        Leadership,
        Project,
        SourceDocument,
        UniversityData,
        UniversityInfo,
    )
    from backend.parser.utils import (
        canonical_url,
        looks_like_person_name,
        normalize_phone,
        parse_date,
    )

FIXTURES = Path(__file__).parent / "fixtures"


class SvedenParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (FIXTURES / "sveden_struct.html").read_text(encoding="utf-8")
        self.cleaner = HTMLCleaner()
        self.doc = self.cleaner.to_document(
            self.html, url="https://example.ru/sveden/struct/", section="leadership"
        )
        self.soup = parser_main._make_soup(self.html)
        self.parser = UniversityParser(Settings(fetch_bios=False))
        self.parser.profile = UNIVERSITY_PROFILES["spbu"]

    def tearDown(self) -> None:
        self.parser.close()

    def test_sveden_rows(self) -> None:
        rows = SvedenTableParser("https://example.ru/sveden/struct/").parse(self.soup)
        names = [row["name"] for row in rows]

        self.assertEqual(len(rows), 4)
        self.assertIn("Иванов Иван Иванович", names)
        self.assertIn("Петрова Мария Сергеевна", names)
        self.assertNotIn("Филимонов Олег Олегович", names)  # филиал пропущен

    def test_leadership_and_contacts(self) -> None:
        self.parser._parse_leadership(self.doc, self.soup)
        self.parser._parse_contacts(self.doc, self.soup)

        self.assertEqual(len(self.parser._leadership), 3)
        rector = next(
            item for item in self.parser._leadership if item.full_name == "Иванов Иван Иванович"
        )
        self.assertEqual(rector.position, "Ректор")
        self.assertEqual(rector.email, "rector@example.ru")

        emails = {contact.value for contact in self.parser._contacts if contact.type == "email"}
        self.assertIn("rector@example.ru", emails)
        self.assertIn("info@example.ru", emails)

        # Общий email не должен приписываться каждому руководителю.
        self.assertIsNone(
            next(
                item
                for item in self.parser._leadership
                if item.full_name == "Петрова Мария Сергеевна"
            ).email
        )

    def test_news_projects_and_events(self) -> None:
        news_html = (FIXTURES / "news_list.html").read_text(encoding="utf-8")
        doc = self.cleaner.to_document(
            news_html, url="https://example.ru/news/", section="news"
        )
        soup = parser_main._make_soup(news_html)
        section = SectionSpec(
            name="news",
            url="https://example.ru/news/",
            item_selectors=("article.news-item",),
        )

        self.parser._parse_news(doc, soup, section)

        project_titles = {item.title for item in self.parser._projects}
        event_titles = {item.title for item in self.parser._events}
        self.assertTrue(any("Приоритет-2030" in title for title in project_titles))
        self.assertTrue(any("РНФ" in title for title in project_titles))
        self.assertTrue(any("конференция" in title.lower() for title in event_titles))
        self.assertTrue(any("спортивный" in title.lower() for title in event_titles))

    def test_card_validation(self) -> None:
        self.parser._parse_leadership(self.doc, self.soup)
        self.parser._parse_contacts(self.doc, self.soup)
        card = self.parser._build_card(
            "https://example.ru", "Тестовый университет", UNIVERSITY_PROFILES["spbu"], self.doc
        )

        self.assertIsInstance(card, UniversityData)
        self.assertEqual(card.university.name, "Тестовый университет")
        self.assertGreater(len(card.leadership), 0)
        payload = card.to_json_dict()
        self.assertIn("leadership", payload)
        self.assertIn("documents_count", payload)
        # Исходные документы хранятся отдельно от карточки (раздел 4).
        self.assertNotIn("documents", payload)
        # Контракт раздела 5.
        for key in (
            "university_name",
            "official_domain",
            "grants",
            "projects",
            "last_updated",
        ):
            self.assertIn(key, payload)
        self.assertTrue(all("name" in item for item in payload["projects"]))


class InputListTests(unittest.TestCase):
    def test_load_university_list(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universities.json"
            path.write_text(
                json.dumps(
                    [
                        {"name": "Первый вуз", "url": "https://one.ru"},
                        "https://two.ru",
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            targets = load_university_list(path)

        self.assertEqual(targets[0], ("https://one.ru", "Первый вуз", None))
        self.assertEqual(targets[1], ("https://two.ru", None, None))


class BioContactTests(unittest.TestCase):
    def test_bio_contacts_are_saved_to_leader(self) -> None:
        doc = SourceDocument(
            source_id="bio",
            url="https://example.ru/staff/ivanov/",
            text=(
                "Иванов Иван Иванович\n\n"
                "Контакты\n\n"
                "Телефон: +7 (495) 531-00-22\n\n"
                "Факс: +7 (495) 531-00-30\n\n"
                "Адрес: ул. Примерная, д. 1, каб. А308\n\n"
                "Email: office@example.ru\n"
            ),
        )
        leader = Leadership(
            full_name="Иванов Иван Иванович",
            position="Ректор",
            email="office@example.ru",
        )
        parser = UniversityParser(Settings(fetch_bios=False))
        try:
            parser._extract_bio_text(doc, leader.full_name, leader)
        finally:
            parser.close()

        self.assertEqual(leader.phone, "+7 (495) 531-00-22")
        self.assertEqual(leader.office_phone, "+7 (495) 531-00-22")
        self.assertEqual(leader.fax, "+7 (495) 531-00-30")
        self.assertEqual(leader.office_number, "каб. А308")
        # Служебный email не должен выдаваться за личный.
        self.assertIsNone(leader.email)


class UtilsTests(unittest.TestCase):
    def test_dates(self) -> None:
        self.assertEqual(parse_date("15 октября 2026"), "2026-10-15")
        self.assertEqual(parse_date("15.10.2026"), "2026-10-15")
        self.assertEqual(parse_date("2026-10-15T10:00:00"), "2026-10-15")
        self.assertEqual(parse_date("21 сентября — 10 ноября 2026"), "2026-09-21")

    def test_phone_and_names(self) -> None:
        self.assertEqual(normalize_phone("8 (812) 328-20-00"), "+7 (812) 328-20-00")
        self.assertTrue(looks_like_person_name("Иванов Иван Иванович"))
        self.assertFalse(looks_like_person_name("Санкт-Петербургский государственный университет"))
        self.assertTrue(_looks_like_table_name("Анна Силюнас"))
        self.assertFalse(_looks_like_table_name("неприменимо"))

    def test_canonical_url(self) -> None:
        self.assertEqual(
            canonical_url("/news/?utm_source=test#top", "https://example.ru/"),
            "https://example.ru/news/",
        )


class ChangeTrackingTests(unittest.TestCase):
    def test_diff(self) -> None:
        previous = {
            "university": {"scraped_at": "2026-10-01T10:00:00+00:00"},
            "leadership": [
                {"full_name": "Иванов Иван Иванович", "position": "Ректор"},
                {"full_name": "Старый Иван Иванович", "position": "Проректор"},
            ],
            "projects": [{"title": "Старый проект"}],
            "events": [],
        }
        current = UniversityData(
            university=UniversityInfo(name="Тест", url="https://example.ru"),
            leadership=[
                Leadership(full_name="Иванов Иван Иванович", position="Исполняющий обязанности ректора"),
                Leadership(full_name="Новый Пётр Петрович", position="Проректор"),
            ],
            projects=[Project(title="Новый проект")],
            events=[Event(title="Новое мероприятие")],
        )

        report = diff_cards(previous, current)

        self.assertIsNotNone(report)
        assert report is not None
        self.assertIn("Новый Пётр Петрович", report.new_leadership)
        self.assertIn("Старый Иван Иванович", report.removed_leadership)
        self.assertIn("Иванов Иван Иванович", report.changed_positions)
        self.assertIn("Новый проект", report.new_projects)
        self.assertIn("Новое мероприятие", report.new_events)


if __name__ == "__main__":
    unittest.main()
