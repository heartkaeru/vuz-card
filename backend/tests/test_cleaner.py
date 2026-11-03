from pathlib import Path
import unittest

try:  # запуск из backend/ (pytest)
    from parser.cleaner import HTMLCleaner, clean_html, clean_text
    from parser.models import SourceDocument
    from parser.utils import clean_email, clean_phone
except ImportError:  # запуск из корня репозитория
    from backend.parser.cleaner import HTMLCleaner, clean_html, clean_text
    from backend.parser.models import SourceDocument
    from backend.parser.utils import clean_email, clean_phone

FIXTURES = Path(__file__).parent / "fixtures"


class CleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (FIXTURES / "sveden_struct.html").read_text(encoding="utf-8")
        self.cleaner = HTMLCleaner()

    def test_removes_scripts_styles_and_navigation(self) -> None:
        text = clean_html(self.html)

        self.assertNotIn("Секретный скрипт", text)
        self.assertNotIn("display: none", text)
        self.assertNotIn("Главная", text)
        self.assertNotIn("Все права защищены", text)
        self.assertIn("Иванов Иван Иванович", text)
        self.assertIn("rector@example.ru", text)

    def test_keeps_structure_and_contacts(self) -> None:
        result = self.cleaner.clean(self.html)
        kinds = {block.kind for block in result.blocks}

        self.assertIn("heading", kinds)
        self.assertIn("paragraph", kinds)
        self.assertIn("list_item", kinds)
        self.assertIn("table_row", kinds)
        self.assertEqual(result.title, "Структура и органы управления")

    def test_document_fragments_are_valid(self) -> None:
        doc = self.cleaner.to_document(self.html, url="https://example.ru/sveden/struct/")

        self.assertIsInstance(doc, SourceDocument)
        self.assertTrue(doc.text)
        self.assertEqual(doc.text_hash, doc.text_hash)
        self.assertGreater(len(doc.fragments), 3)

        previous_end = 0
        for fragment in doc.fragments:
            self.assertGreater(fragment.end, fragment.start)
            self.assertLessEqual(fragment.end, len(doc.text))
            self.assertGreaterEqual(fragment.start, previous_end)
            self.assertEqual(doc.text[fragment.start:fragment.end], doc.text[fragment.start:fragment.end].strip())
            previous_end = fragment.end

    def test_contact_hints_have_owner(self) -> None:
        doc = self.cleaner.to_document(self.html, url="https://example.ru/sveden/struct/")
        hints = [hint for fragment in doc.fragments for hint in fragment.contact_hints]

        self.assertTrue(hints)
        emails = [hint.value for hint in hints if hint.field == "contact.email"]
        phones = [hint.value for hint in hints if hint.field == "contact.phone"]
        self.assertIn("rector@example.ru", emails)
        self.assertIn("+7 (495) 123-45-67", phones)
        self.assertTrue(all(hint.owner.kind != "unknown" for hint in hints))

    def test_document_llm_contract(self) -> None:
        doc = self.cleaner.to_document(
            self.html, url="https://example.ru/news/", section="news"
        )

        self.assertEqual(doc.category, "project_news")
        self.assertEqual(doc.language, "ru")
        record = doc.to_llm_record("Тестовый вуз", "example.ru")

        self.assertEqual(record["university"]["official_domain"], "example.ru")
        document = record["document"]
        for key in (
            "category",
            "title",
            "url",
            "published_at",
            "collected_at",
            "language",
            "content",
            "content_hash",
            "content_type",
            "fragments",
        ):
            self.assertIn(key, document)
        self.assertEqual(document["content"], doc.text)
        self.assertEqual(document["content_hash"], doc.text_hash)
        self.assertTrue(document["fragments"])
        self.assertIn("university", document["fragments"][0])

    def test_clean_text_normalizes_whitespace(self) -> None:
        self.assertEqual(clean_text("  Привет,\n\n мир!  "), "Привет, мир!")

    def test_contacts_helpers(self) -> None:
        self.assertEqual(clean_email("mailto:Rector@Example.RU, "), "Rector@example.ru")
        self.assertEqual(clean_phone("тел. 8 (812) 328-20-00"), "+7 (812) 328-20-00")


if __name__ == "__main__":
    unittest.main()
