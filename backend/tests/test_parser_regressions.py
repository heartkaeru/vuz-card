"""Регрессионные тесты на ошибки из ревью парсера (ветка fix/parser-review)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from parser import public_http
from parser.aggregators import (
    PostupiParser,
    VuzopediaParser,
    _passport_matches,
    name_match_score,
    short_name_matches,
)
from parser.biography import split_biography_sentences
from parser.cleaner import HTMLCleaner
from parser.config import PROJECT_TYPE_KEYWORDS, SectionSpec, Settings
from parser.main import (
    SvedenTableParser,
    UniversityParser,
    _classify,
    _looks_like_table_name,
    _make_soup,
    _normalize_office_number,
    save_results,
)
from parser.models import DocumentFragment, Leadership, SourceDocument, UniversityData, UniversityInfo
from parser.news_search import NewsSearch, SearchHit
from parser.official_site import OfficialSiteDiscovery, host_is_excluded
from parser.stealth import StealthFetcher, StealthResponse
from parser.utils import (
    canonical_url,
    extract_phones,
    find_person_names,
    find_photo,
    is_same_domain,
    looks_like_block_page,
    looks_like_person_name,
    parse_date,
    pick_title,
    strip_name,
)


class FakeFetcher:
    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> StealthResponse:
        self.requested.append(url)
        page = self.pages.get(url, "")
        if isinstance(page, StealthResponse):
            return page
        return StealthResponse(url, 200 if page else 404, str(page), final_url=url)


# --- 1–3: телефоны, ФИО, фамилии ---------------------------------------------
def test_inn_and_requisites_are_not_phones():
    text = "ИНН 7714030726, КПП 770101001, ОГРН 1027739630401. Тел.: 8 (495) 772-95-90"
    assert extract_phones(text) == ["+7 (495) 772-95-90"]


def test_lowercase_phrase_is_not_a_person():
    assert not looks_like_person_name("подписал приказ об")
    assert find_person_names("Ректор университета подписал приказ об этом") == []
    assert find_person_names("Ректор Иванов Иван Иванович и проректор Петров П. П.") == [
        "Иванов Иван Иванович",
        "Петров П. П.",
    ]


def test_surnames_starting_with_positions_are_kept():
    assert strip_name("Ректорова Анна Ивановна") == "Ректорова Анна Ивановна"
    assert strip_name("Деканова Мария Петровна") == "Деканова Мария Петровна"
    assert strip_name("Ректор: Иванов Иван Иванович") == "Иванов Иван Иванович"
    assert looks_like_person_name("Советов Иван Петрович")
    assert looks_like_person_name("Иванов И. И.")
    assert not looks_like_person_name("Учёный Совет Университета")


# --- 4: SSRF через резервные загрузчики ---------------------------------------
def test_public_session_marks_fallback_as_public_only():
    session = public_http.public_site_session()
    client = Mock(session=session, last_error="InvalidURL", last_error_code="")
    client.get.return_value = None
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=True), client=client)
    try:
        assert parser._public_only()
        assert parser._stealth().public_only
    finally:
        parser.close()


def test_stealth_public_only_rejects_private_hosts():
    fetcher = StealthFetcher(public_only=True, min_delay=0)
    try:
        response = fetcher.get("http://127.0.0.1/")
        assert response.status_code == 0 and response.error == "public_website_required"
    finally:
        fetcher.close()


# --- 5: 404 и капча не считаются контентом ------------------------------------
def test_error_responses_are_not_ok():
    assert not StealthResponse("u", 404, "<h1>Не найдено</h1>").ok
    assert StealthResponse("u", 200, "<p>текст</p>").ok
    assert StealthFetcher.looks_blocked(200, "<p>Подтвердите, что вы не робот</p>")
    long_page = "<p>" + "Обычный текст страницы. " * 300 + "captcha cloudflare</p>"
    assert not StealthFetcher.looks_blocked(200, long_page)
    assert not looks_like_block_page(long_page, 200)


# --- 6: ректор, а не проректор/директор ----------------------------------------
def test_rector_name_ignores_prorector_and_director():
    html = """<html><body><main>
      <p>Проректор по науке Петров Пётр Петрович</p>
      <p>Директор института Сидоров Сидор Сидорович</p>
      <h1>Ректор</h1><p>Ректор Иванов Иван Иванович</p>
    </main></body></html>"""
    soup = _make_soup(html)
    text = " ".join(soup.get_text(" ", strip=True).split())
    assert OfficialSiteDiscovery._find_name(soup, text) == "Иванов Иван Иванович"


def test_director_link_is_not_rector_page():
    soup = _make_soup('<a href="/about/director/">Директор</a><a href="/rector/">Ректор</a>')
    discovery = OfficialSiteDiscovery(FakeFetcher({}))
    assert discovery._scan_page_for_rector(soup, "https://vuz.ru/") == "https://vuz.ru/rector/"
    only_director = _make_soup('<a href="/about/director/">Наш директор</a>')
    assert discovery._scan_page_for_rector(only_director, "https://vuz.ru/") == ""


def test_probe_skips_404_and_director_pages():
    fetcher = FakeFetcher({
        "https://vuz.ru/rector/": StealthResponse("https://vuz.ru/rector/", 404, "<title>Ректор</title>"),
        "https://vuz.ru/ru/rector/": "<title>Директор колледжа</title><h1>Директор</h1>",
        "https://vuz.ru/about/rector/": "<title>Ректор университета</title><h1>Ректор</h1>",
    })
    assert OfficialSiteDiscovery(fetcher)._probe_rector_paths("https://vuz.ru/") == "https://vuz.ru/about/rector/"


# --- 7: официальный сайт -------------------------------------------------------
def test_site_matching_requires_distinctive_words():
    name = "Уральский федеральный университет"
    assert not OfficialSiteDiscovery._matches_name("Казанский федеральный университет", "kpfu.ru", name)
    assert OfficialSiteDiscovery._matches_name("Уральский федеральный университет — УрФУ", "urfu.ru", name)
    assert OfficialSiteDiscovery._matches_name("Главная", "urfu.ru", name, "УрФУ")
    assert not host_is_excluded("student.mephi.ru")
    assert host_is_excluded("t.me") and host_is_excluded("m.vk.com")


def test_wikipedia_article_must_match_university():
    fetcher = FakeFetcher({})
    discovery = OfficialSiteDiscovery(fetcher)
    discovery._wiki_api = lambda params: {  # type: ignore[method-assign]
        "query": {"search": [{"title": "Казанский федеральный университет"}]}
    }
    assert discovery._wikipedia_site("Уральский федеральный университет") == ""


# --- 8: найденный ректор не подменяет официального ------------------------------
def test_discovered_rector_does_not_replace_official_one(monkeypatch):
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    parser._base_url = "https://vuz.ru/"
    parser._official_domain = "vuz.ru"
    parser._university_name = "Тестовый университет"
    parser._leadership = [Leadership(full_name="Иванов Иван Иванович", position="Ректор")]
    calls = {}

    def fake_discover(self, name, short_name="", site_url=""):
        calls["site_url"] = site_url
        return Leadership(full_name="Чужой Пётр Петрович", position="ректор"), "https://other.ru/", "https://other.ru/rector/"

    monkeypatch.setattr(OfficialSiteDiscovery, "discover", fake_discover)
    try:
        parser._enrich_rector_from_web()
    finally:
        parser.close()
    assert calls["site_url"] == "https://vuz.ru/"
    assert [leader.full_name for leader in parser._leadership] == ["Иванов Иван Иванович"]
    assert parser._official_domain == "vuz.ru"


# --- 9: пустое название не принимает случайный паспорт --------------------------
def test_empty_name_never_matches_passport():
    assert not _passport_matches("", "Любой университет")


# --- 10: агрегаторы -------------------------------------------------------------
def test_aggregator_name_matching():
    query = "Московский государственный университет"
    assert not _passport_matches(query, query + " спорта и туризма")
    assert _passport_matches(query, query + " имени М.В. Ломоносова")
    assert not _passport_matches(
        "Российский государственный университет", "Российский государственный гуманитарный университет"
    )
    assert short_name_matches("СПбГУ", "Санкт-Петербургский государственный университет")
    assert short_name_matches("НИУ ВШЭ", "Национальный исследовательский университет «Высшая школа экономики»")
    assert not short_name_matches("ВШЭ", "Уральский федеральный университет")
    assert name_match_score(query, query + " спорта и туризма") < 45


def test_postupi_negations():
    page = """<html><body><h1>Тестовый университет</h1>
      <div>Государственный. Общежитие не предоставляется. Военного учебного центра нет.</div>
    </body></html>"""
    fetcher = FakeFetcher({"https://msk.postupi.online/vuz/test/": page})
    passport = PostupiParser(fetcher).parse("https://msk.postupi.online/vuz/test/")
    assert passport.dormitory is False
    assert passport.military_department is False
    assert not hasattr(passport, "budget_places")


def test_vuzopedia_status_ignores_similar_universities_block():
    page = """<html><body><div class="card"><h1>Тестовый университет</h1>
      <p>Государственный вуз. Общежитие есть. """ + "Описание. " * 80 + """</p></div>
      <aside><p>Похожие вузы: частный институт, негосударственный вуз</p></aside>
    </body></html>"""
    fetcher = FakeFetcher({"https://vuzopedia.ru/vuz/1": page})
    passport = VuzopediaParser(fetcher).parse("https://vuzopedia.ru/vuz/1")
    assert passport.status == "государственный"


# --- 11: адрес и реквизиты -------------------------------------------------------
def test_about_requisites_and_address():
    html = """<html><body><main>
      <div><p>Инновационный университет. Адрес электронной почты: info@vuz.ru</p></div>
      <div class="card"><p>Полное наименование: ФГАОУ ВО «Тестовый университет»</p>
      <p>ИНН 7714030726</p><p>ОГРН 1027739630401</p>
      <p>Адрес места нахождения: 101000, г. Москва, ул. Мясницкая, д. 20</p></div>
    </main></body></html>"""
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    try:
        doc = HTMLCleaner().to_document(html, url="https://vuz.ru/sveden/common/", section="about")
        parser._parse_about(doc, _make_soup(html))
        card = parser._build_card("https://vuz.ru/", "Тест", None, None)
    finally:
        parser.close()
    assert card.university.inn == "7714030726"
    assert card.university.address.startswith("101000, г. Москва")
    assert all("771" not in contact.value for contact in card.contacts if contact.type == "phone")


# --- 12: кабинет ----------------------------------------------------------------
def test_office_number_requires_digits():
    assert _normalize_office_number("Иванов, к.т.н., доцент") == ""
    assert _normalize_office_number("кабинет руководителя") == ""
    assert _normalize_office_number("ул. Примерная, д. 1, каб. А308") == "каб. А308"


# --- 14: классификация ----------------------------------------------------------
def test_keywords_match_word_starts():
    assert _classify("Планирование учебного процесса", PROJECT_TYPE_KEYWORDS, "-") == "-"
    assert _classify("Новости национального исследовательского университета", PROJECT_TYPE_KEYWORDS, "-") == "-"
    assert _classify("Грант РНФ на исследование", PROJECT_TYPE_KEYWORDS, "-") == "федеральный"


# --- 15, 32: конфликты и дубликаты контактов -------------------------------------
def test_contact_conflicts_and_phone_formats():
    from parser.models import Contact

    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    try:
        parser._add_contact(Contact(type="phone", value="8 (495) 111-22-33", owner="Иванов Иван Иванович", owner_kind="person"))
        parser._add_contact(Contact(type="phone", value="+7 (495) 111-22-33", owner="Приёмная", owner_kind="office"))
        assert len(parser._contacts) == 1
        assert parser._collect_conflicts()
    finally:
        parser.close()


# --- 16: неудачный запуск не затирает снимок -------------------------------------
def test_failed_run_keeps_latest_snapshot(tmp_path):
    good = UniversityData(
        university=UniversityInfo(name="Тест", url="https://vuz.ru/"),
        leadership=[Leadership(full_name="Иванов Иван Иванович", position="Ректор")],
    )
    save_results(good, tmp_path, slug="test")
    failed = UniversityData(university=UniversityInfo(name="Тест", url="https://vuz.ru/"), errors=["сеть недоступна"])
    paths = save_results(failed, tmp_path, slug="test")
    latest = (tmp_path / "test" / "test_card.json").read_text(encoding="utf-8")
    assert "Иванов Иван Иванович" in latest
    assert "changes" not in paths


# --- 17–19: очистка HTML ------------------------------------------------------------
def test_cleaner_keeps_text_in_wrappers_and_short_contacts():
    html = """<html><head><link rel="canonical" href="/about/"></head><body><main><article>
      <header><h1>Новость о гранте</h1><time datetime="2025-03-01">1 марта</time></header>
      <a class="card" href="/x"><div><p>Карточка внутри ссылки с длинным текстом про проект</p></div></a>
      <div>Ректор: <b>Иванов Иван Иванович</b><p>Абзац текста подлиннее, чтобы был блок.</p></div>
      <div><div>+7 (495) 123-45-67</div></div>
      <div class="tab" hidden><p>Скрытая вкладка с руководством университета и контактами</p></div>
    </article></main></body></html>"""
    doc = HTMLCleaner().to_document(html, url="https://vuz.ru/about/page", section="contacts")
    assert doc.url == "https://vuz.ru/about/"
    assert doc.published_at == datetime(2025, 3, 1, tzinfo=timezone.utc)
    for expected in ("Новость о гранте", "Карточка внутри ссылки", "Ректор: Иванов Иван Иванович",
                     "+7 (495) 123-45-67", "Скрытая вкладка"):
        assert expected in doc.text


def test_canonical_to_homepage_or_other_domain_is_ignored():
    cleaner = HTMLCleaner()
    home = '<html><head><link rel="canonical" href="https://vuz.ru/"></head><body><main><p>' + "текст " * 30 + "</p></main></body></html>"
    other = home.replace("https://vuz.ru/", "https://other.ru/page")
    assert cleaner.to_document(home, url="https://vuz.ru/news/1", section="news").url == "https://vuz.ru/news/1"
    assert cleaner.to_document(other, url="https://vuz.ru/news/1", section="news").url == "https://vuz.ru/news/1"


# --- 20: таблицы /sveden/struct/ ---------------------------------------------------
def test_sveden_rowspan_and_initials():
    html = """<table>
      <tr><th>Наименование подразделения</th><th>ФИО руководителя</th><th>Должность</th><th>Телефон</th></tr>
      <tr><td rowspan="2">Ректорат</td><td>Иванов Иван Иванович</td><td>Ректор</td><td>+7 (495) 111-22-33</td></tr>
      <tr><td>Петров П. П.</td><td>Проректор</td><td>+7 (495) 111-22-44</td></tr>
    </table>"""
    rows = SvedenTableParser("https://vuz.ru/").parse(_make_soup(html))
    assert [(row["unit"], row["name"], row["position"]) for row in rows] == [
        ("Ректорат", "Иванов Иван Иванович", "Ректор"),
        ("Ректорат", "Петров П. П.", "Проректор"),
    ]
    assert _looks_like_table_name("Петров П. П.")


def test_unit_column_is_not_name_column():
    html = """<table><tr><th>ФИО</th><th>Должность</th></tr>
      <tr><td>Иванов Иван Иванович</td><td>Ректор</td></tr></table>"""
    rows = SvedenTableParser("https://vuz.ru/").parse(_make_soup(html))
    assert rows[0]["unit"] != rows[0]["name"]


# --- 21–22: даты и URL ----------------------------------------------------------------
def test_dates_and_tracking_params():
    assert parse_date("01.01.99") == "1999-01-01"
    assert parse_date("версия 1.2.10") is None
    assert parse_date("01.02.24") == "2024-02-01"
    assert canonical_url("https://x.ru/list?fromDate=2024&refId=5&utm_source=a&from=b") == (
        "https://x.ru/list?fromDate=2024&refId=5"
    )


# --- 26: фильтр новостей о Сбере ------------------------------------------------------
def test_sber_news_must_mention_this_university():
    other = SearchHit("Сбер и Казанский университет подписали соглашение", "https://n.ru/1",
                      "Соглашение о сотрудничестве", "2026-01-10", "bing", "q")
    ours = SearchHit("Сбер и УрФУ открыли лабораторию", "https://n.ru/2", "", "2026-01-10", "bing", "q")
    name = "Уральский федеральный университет"
    assert not NewsSearch._relevant(other, name, "УрФУ")
    assert NewsSearch._relevant(ours, name, "УрФУ")


def test_old_sber_news_is_dropped():
    search = NewsSearch(FakeFetcher({}), max_age_months=24)
    search._queries = lambda name, short: ["q"]  # type: ignore[method-assign]
    search._search_query = lambda query, name, short: [  # type: ignore[method-assign]
        SearchHit("Сбер и УрФУ: соглашение", "https://n.ru/old", "", "2015-01-01", "bing", "q"),
        SearchHit("Сбер и УрФУ открыли лабораторию", "https://n.ru/new", "", "2026-10-01", "bing", "q"),
    ]
    assert [item.url for item in search.search("Уральский федеральный университет", "УрФУ")] == ["https://n.ru/new"]


# --- 29: домены --------------------------------------------------------------------------
def test_regional_public_suffixes():
    assert UniversityParser._base_domain_url("https://lib.ifmo.spb.ru/x") == "https://ifmo.spb.ru/sveden/struct/"
    assert not is_same_domain("https://other.spb.ru/", "https://ifmo.spb.ru/")
    assert is_same_domain("https://spb.hse.ru/", "https://www.hse.ru/")


# --- 34: границы предложений в биографии ------------------------------------------------
def test_biography_split_respects_abbreviations():
    text = ("Работал в г. Москве с 2001 года, окончил МГУ им. Ломоносова и аспирантуру. "
            "Затем преподавал в университете и руководил кафедрой экономики много лет подряд. "
            "Учился в МГУ. Потом руководил институтом.")
    doc = SourceDocument(source_id="b", url="https://vuz.ru/rector/", text=text,
                         fragments=[DocumentFragment(fragment_id="f1", start=0, end=len(text))])
    split_biography_sentences(doc)
    parts = [doc.text[f.start:f.end] for f in doc.fragments]
    assert parts[0].startswith("Работал в г. Москве") and "им. Ломоносова" in parts[0]
    assert parts[-1] == "Потом руководил институтом."


# --- 35, 43: фото и заголовки --------------------------------------------------------------
def test_photo_fallback_requires_person_nearby():
    news = _make_soup('<main><img src="/news/photo1.jpg"><p>Новость о конференции</p></main>')
    assert find_photo(news, "https://vuz.ru/", "Иванов Иван Иванович") == ""
    portrait = _make_soup('<main><div><img src="/upload/p.jpg?v=2"><p>Иванов Иван Иванович</p></div></main>')
    assert find_photo(portrait, "https://vuz.ru/", "Иванов Иван Иванович") == "https://vuz.ru/upload/p.jpg?v=2"


def test_pick_title_keeps_all_but_site_name():
    assert pick_title("Ректор — Иванов И. И. | ВШЭ") == "Ректор — Иванов И. И."
    assert pick_title("Новости | Сайт") == "Новости"


@pytest.mark.parametrize("value", ["", "   "])
def test_parse_without_site_or_name_does_not_crawl_empty_url(value):
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False,
                                       use_aggregators=False, use_news_search=False, rector_lookup=False))
    client = Mock(last_error="")
    parser.client = client
    try:
        card = parser.parse(url=None, name=value or None, allowed_sections=("leadership",))
    finally:
        parser.close()
    client.get.assert_not_called()
    assert any("сайт" in warning.lower() for warning in card.warnings)


# --- живой прогон УрФУ: пункт меню принят за ректора ----------------------------
def test_menu_phrase_is_not_a_person_and_combined_rector_position():
    from parser.main import _is_rector_position

    assert not looks_like_person_name("Творчество Спорт Устойчивое")
    assert looks_like_person_name("Илья Николаевич Обабков")
    assert _is_rector_position("председатель, ректор")
    assert _is_rector_position("Ректор НГУ, профессор")
    assert not _is_rector_position("Проректор по науке")
    assert not _is_rector_position("Ректоров совет")


def test_undated_news_gets_date_from_article_page():
    article = '<html><head><meta property="article:published_time" content="2016-05-01T10:00:00Z"></head></html>'
    search = NewsSearch(FakeFetcher({"https://n.ru/a": article}), max_age_months=24)
    search._queries = lambda name, short: ["q"]  # type: ignore[method-assign]
    search._search_query = lambda query, name, short: [  # type: ignore[method-assign]
        SearchHit("Сбер и УрФУ: соглашение", "https://n.ru/a", "", None, "bing", "q"),
    ]
    assert search.search("Уральский федеральный университет", "УрФУ") == []


def test_default_sections_include_events_and_event_parser_uses_machine_and_text_dates():
    assert "events" in Settings().sections
    html = """<main>
      <article class="event-card"><h2><a href="/events/forum">Форум науки</a></h2>
        <time datetime="2026-12-10T10:00:00+05:00">10 декабря</time>
      </article>
      <article><h2><a href="/events/lecture">Лекция по робототехнике</a></h2></article>
      <p>15 декабря 2026 года состоится конференция молодых исследователей.</p>
    </main>"""
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, fetch_item_pages=False))
    doc = HTMLCleaner().to_document(html, url="https://vuz.ru/events/", section="events")
    try:
        parser._parse_events(
            doc,
            _make_soup(html),
            SectionSpec(name="events", url="https://vuz.ru/events/", item_selectors=("article",)),
        )
    finally:
        parser.close()

    events = {event.title: event.date for event in parser._events}
    assert events["Форум науки"] == "2026-12-10"
    assert any("конференция" in title.lower() for title in events)


def test_two_word_rector_signature_and_same_person_merge():
    from parser.main import _same_person

    html = "<main><p>Университет делают люди.</p><p>Ректор Илья Обабков</p><p>События с участием ректора</p></main>"
    soup = _make_soup(html)
    text = " ".join(soup.get_text(" ", strip=True).split())
    pytest.importorskip("pymorphy3")
    assert OfficialSiteDiscovery._find_name(soup, text) == "Илья Обабков"
    assert _same_person("Илья Обабков", "Обабков Илья Николаевич")
    assert not _same_person("Иван Петров", "Обабков Илья Николаевич")


# --- основное руководство, специальности, направления, порядок поисковиков ------
def test_card_keeps_only_key_leadership_and_their_contacts():
    from parser.models import Contact

    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    parser._leadership = [
        Leadership(full_name="Иванов Иван Иванович", position="Ректор"),
        Leadership(full_name="Петров Пётр Петрович", position="Первый проректор"),
        Leadership(full_name="Сидоров Сидор Сидорович", position="Проректор по науке"),
        Leadership(full_name="Кузнецов Кузьма Кузьмич", position="Директор центра карьеры"),
        Leadership(full_name="Смирнова Анна Олеговна", position="Декан экономического факультета"),
    ]
    for contact in (
        Contact(type="email", value="rector@vuz.ru", owner="Приёмная ректора", owner_kind="office"),
        Contact(type="email", value="ivanov@vuz.ru", owner="Иванов Иван Иванович", owner_kind="person"),
        Contact(type="email", value="career@vuz.ru", owner="Кузнецов Кузьма Кузьмич", owner_kind="person"),
        Contact(type="email", value="econ@vuz.ru", owner="Экономический факультет", owner_kind="department"),
        Contact(type="phone", value="+7 (343) 375-44-44", owner="ВУЗ", owner_kind="organization"),
    ):
        parser._add_contact(contact)
    try:
        card = parser._build_card("https://vuz.ru/", "Тест", None, None)
    finally:
        parser.close()
    assert [leader.position for leader in card.leadership] == ["Ректор", "Первый проректор", "Проректор по науке"]
    assert {contact.value for contact in card.contacts} == {"rector@vuz.ru", "ivanov@vuz.ru", "+7 (343) 375-44-44"}


def test_specialties_from_sveden_microdata_and_cooperation_directions():
    from parser.models import NewsMention
    from parser.specialties import cooperation_directions, parse_specialties

    html = """<table>
      <tr itemprop="eduOp"><td itemprop="eduCode">09.03.01</td><td itemprop="eduName">Информатика и вычислительная техника</td><td itemprop="eduLevel">высшее образование - бакалавриат</td></tr>
      <tr itemprop="eduOp"><td itemprop="eduCode">10.05.01</td><td itemprop="eduName">Компьютерная безопасность</td><td itemprop="eduLevel">высшее образование - специалитет</td></tr>
      <tr itemprop="eduOp"><td itemprop="eduCode">38.04.08</td><td itemprop="eduName">Финансы и кредит</td><td itemprop="eduLevel">высшее образование - магистратура</td></tr>
      <tr itemprop="eduOp"><td itemprop="eduCode">09.03.01</td><td itemprop="eduName">Информатика и вычислительная техника</td><td itemprop="eduLevel">бакалавриат</td></tr>
    </table>"""
    specialties = parse_specialties(html, "https://vuz.ru/sveden/education/")
    assert [(item.code, item.level) for item in specialties] == [
        ("09.03.01", "бакалавриат"), ("10.05.01", "специалитет"), ("38.04.08", "магистратура"),
    ]
    news = [NewsMention(title="Сбер и вуз открыли лабораторию искусственного интеллекта", url="https://n.ru/1", date="2026-05-01")]
    directions = cooperation_directions(specialties, ["Лаборатория машинного обучения"], news)
    areas = [item.area for item in directions]
    assert areas[0].startswith("Искусственный интеллект")
    assert directions[0].has_existing_cooperation
    assert any(area.startswith("Кибербезопасность") for area in areas)
    assert any(area.startswith("Финансы") for area in areas)
    assert not any(area.startswith("Медицина") for area in areas)


def test_web_search_prefers_yandex_and_falls_back_on_captcha():
    from parser.news_search import NewsSearch

    captcha = "<html><body>Подтвердите, что вы не робот</body></html>"
    google = '<div class="g"><a href="/url?q=https://urfu.ru/&sa=U"><h3>УрФУ — официальный сайт</h3></a></div>'
    fetcher = FakeFetcher({})
    fetcher.get = lambda url, timeout=None: StealthResponse(  # type: ignore[method-assign]
        url, 200, captcha if "yandex" in url else google if "google.com" in url else "", final_url=url
    )
    hits = NewsSearch(fetcher).web_search("УрФУ официальный сайт")
    assert [(hit.source, hit.url) for hit in hits] == [("google", "https://urfu.ru/")]


def test_rectorate_cards_and_admissions_phone_is_not_reception():
    html = """<main><h2>Ректорат</h2>
      <div>Иванов Иван Иванович</div><p>Ректор</p><p>Приемная комиссия: +7 (343) 375-44-74</p>
      <div>Петров Пётр Петрович</div><p>Первый проректор</p><p>Телефон:</p><p>+7 (343) 375-45-09</p>
      <p>Электронная почта: p.p.petrov@vuz.ru</p>
      <div>Сидоров Сидор Сидорович</div><p>Проректор по науке</p><p>Электронная почта: s.sidorov@vuz.ru</p>
    </main>"""
    doc = HTMLCleaner().to_document(html, url="https://vuz.ru/about/administration/", section="leadership")
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    try:
        leaders = parser._leadership_from_cards(doc)
    finally:
        parser.close()
    assert [(leader.full_name, leader.position) for leader in leaders] == [
        ("Иванов Иван Иванович", "Ректор"),
        ("Петров Пётр Петрович", "Первый проректор"),
        ("Сидоров Сидор Сидорович", "Проректор по науке"),
    ]
    assert leaders[1].email == "p.p.petrov@vuz.ru"
    assert OfficialSiteDiscovery._labelled_phone("Приемная комиссия: +7 (343) 375-44-74") == ""
    assert OfficialSiteDiscovery._labelled_phone("Приёмная ректора: +7 (343) 375-45-07") == "+7 (343) 375-45-07"


def test_captcha_engine_is_skipped_and_yandex_api_is_used(monkeypatch):
    import base64

    from parser import news_search
    from parser.news_search import NewsSearch

    calls: list[str] = []
    captcha = "<html><body>Подтвердите, что вы не робот</body></html>"
    bing = '<li class="b_algo"><h2><a href="https://urfu.ru/">УрФУ</a></h2><p>Официальный сайт</p></li>'

    def fake_get(url, timeout=None):
        calls.append(url)
        if "yandex" in url or "google" in url:
            return StealthResponse(url, 200, captcha, final_url=url)
        return StealthResponse(url, 200, bing if "bing.com/search" in url else "", final_url=url)

    fetcher = FakeFetcher({})
    fetcher.get = fake_get  # type: ignore[method-assign]
    search = NewsSearch(fetcher)
    assert search.web_search("УрФУ")[0].url == "https://urfu.ru/"
    yandex_calls = sum("yandex" in url for url in calls)
    search.web_search("УрФУ сайт")
    assert sum("yandex" in url for url in calls) == yandex_calls  # Яндекс на перерыве

    # С ключами Yandex Search API используется только API (v2), без обычной выдачи.
    news_search._ENGINE_BLOCKED_UNTIL.clear()
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "folder")
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "key")
    xml = ("<yandexsearch><response><results><grouping><group><doc><url>https://urfu.ru/</url>"
           "<title><hlword>УрФУ</hlword> — официальный сайт</title></doc></group></grouping>"
           "</results></response></yandexsearch>")

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"rawData": base64.b64encode(xml.encode()).decode()}

    import requests

    posted = {}
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: posted.update(url=url, **kwargs) or Response())
    calls.clear()
    hits = NewsSearch(fetcher).web_search("УрФУ")
    assert posted["url"].endswith("/v2/web/search")
    assert posted["headers"]["Authorization"] == "Api-Key key"
    assert [(hit.source, hit.title) for hit in hits] == [("yandex", "УрФУ — официальный сайт")]
    assert not any("yandex.ru/search/?" in url for url in calls)


def test_one_shared_browser_per_thread_and_global_limit(monkeypatch):
    import sys
    import threading
    import types

    from parser import stealth

    launches: list[list[str]] = []

    class Browser:
        closed = False

        def close(self):
            self.closed = True

    class Runtime:
        chromium = types.SimpleNamespace(launch=lambda headless, args: launches.append(args) or Browser())

        def start(self):
            return self

        def stop(self):
            pass

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: Runtime()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(stealth, "_BROWSER_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(stealth, "BROWSER_WAIT_SECONDS", 0.2)

    first, second = StealthFetcher(min_delay=0), StealthFetcher(min_delay=0)
    assert first._ensure_browser() is second._ensure_browser()  # один браузер на поток
    assert len(launches) == 1 and "--disable-dev-shm-usage" in launches[0]

    errors: list[str] = []
    worker = threading.Thread(target=lambda: errors.append(_try_browser()))
    worker.start()
    worker.join()
    assert errors and "browser_busy" in errors[0]  # второй поток ждёт и не запускает второй браузер

    first.close()
    second.close()
    worker = threading.Thread(target=lambda: errors.append(_try_browser()))
    worker.start()
    worker.join()
    assert errors[-1] == "ok"


def _try_browser() -> str:
    fetcher = StealthFetcher(min_delay=0)
    try:
        fetcher._ensure_browser()
        return "ok"
    except RuntimeError as exc:
        return str(exc)
    finally:
        fetcher.close()


def test_site_lookup_names_and_origin():
    from parser.official_site import _origin

    assert _passport_matches("НИУ ВШЭ", "Высшая школа экономики")
    assert not _passport_matches("НИУ ВШЭ", "Лицей НИУ ВШЭ")
    assert _passport_matches("Казанский федеральный университет", "Казанский университет")
    assert not _passport_matches("Казанский федеральный университет", "Набережночелнинский институт КФУ")
    assert _origin("https", "tpu.ru:443") == "https://tpu.ru/"
    assert _origin("http", "vuz.ru:8080") == "http://vuz.ru:8080/"


def test_list_universities_sql_casts_optional_filters():
    import inspect

    from database.postgres import api_repository

    source = inspect.getsource(api_repository.ApiRepository.list_universities) if hasattr(
        api_repository, "ApiRepository") else inspect.getsource(api_repository)
    assert "CAST(:region AS text) IS NULL" in source and "CAST(:search AS text) IS NULL" in source


def test_llm_documents_keep_only_key_leadership_fragments():
    rows = "".join(
        f"<tr><td>Центр {index}</td><td>Сотрудников Сотрудник Сотрудникович{index % 10}</td>"
        f"<td>Директор центра</td><td>+7 (343) 375-{index:02d}-{index:02d}</td></tr>"
        for index in range(10, 90)
    )
    html = f"""<html><body><main>
      <h2>Ректорат</h2><p>Ректор Иванов Иван Иванович</p><p>Приёмная ректора: +7 (343) 375-45-07</p>
      <table><tr><th>Подразделение</th><th>ФИО</th><th>Должность</th><th>Телефон</th></tr>{rows}</table>
    </main></body></html>"""
    doc = HTMLCleaner().to_document(html, url="https://vuz.ru/sveden/struct/", section="leadership")
    about = HTMLCleaner().to_document("<main><p>" + "Сведения об университете. " * 20 + "</p></main>",
                                      url="https://vuz.ru/sveden/common/", section="about")
    home = HTMLCleaner().to_document("<main><p>" + "Новости и меню сайта. " * 20 + "</p></main>",
                                     url="https://vuz.ru/", section="homepage")
    parser = UniversityParser(Settings(use_sqlite=False, use_cache=False, enable_playwright=False))
    parser._documents = {("a", "leadership"): doc, ("b", "about"): about, ("c", "homepage"): home}
    try:
        documents = parser._documents_for_llm([Leadership(full_name="Иванов Иван Иванович", position="Ректор")])
    finally:
        parser.close()
    assert [item.section for item in documents] == ["leadership", "about"]  # главная не нужна
    focused = documents[0]
    assert len(focused.text) < len(doc.text) / 3
    assert "Иванов Иван Иванович" in focused.text and "375-45-07" in focused.text
    assert "Директор центра" not in focused.text
    # Смещения пересчитаны: документ проходит контракт LLM 0.2.
    from app.services.llm.fragment_models import FragmentExtractionRequest
    from parser.llm_adapter import to_llm_request

    card = UniversityData(university=UniversityInfo(name="Тест", url="https://vuz.ru/"), documents=[focused])
    FragmentExtractionRequest.model_validate(to_llm_request(card, institution_id="t"))


def test_chromium_runs_only_when_needed(monkeypatch):
    from parser import stealth

    long_text = "<html><body><main>" + "<p>Обычная страница вуза с текстом.</p>" * 40 + "</main></body></html>"
    spa = '<!doctype html><html><body><div id="app"></div><script src="/_app/immutable/start.js"></script></body></html>'
    challenge = "<html><title>Just a moment...</title><body>Checking your browser</body></html>"
    captcha = "<html><body>Подтвердите, что вы не робот. Введите символы с картинки (captcha)</body></html>"
    short_page = "<html><body><p>Контакты: +7 (343) 375-44-44</p></body></html>"

    class Response:
        def __init__(self, url, status, text, headers=None):
            self.url, self.status_code, self.content = url, status, text.encode("utf-8")
            self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}

    pages = {
        "https://vuz.ru/": (200, long_text, None),
        "https://spa.ru/": (200, spa, None),
        "https://protected.ru/": (503, challenge, {"Server": "cloudflare"}),
        "https://captcha.ru/": (200, captcha, None),
        "https://short.ru/": (200, short_page, None),
        "https://yandex.ru/search/?text=x": (200, challenge, None),
    }

    class Session:
        def get(self, url, **kwargs):
            if url == "https://timeout.ru/":
                raise TimeoutError("Operation timed out after 20000 milliseconds")
            if url == "https://tls.ru/":
                raise OSError("SSL handshake failed: connection reset")
            status, text, headers = pages[url]
            return Response(url, status, text, headers)

        def close(self):
            pass

    rendered: list[str] = []
    fetcher = StealthFetcher(enable_playwright=True, min_delay=0)
    fetcher._session = Session()
    monkeypatch.setattr(
        fetcher, "_playwright_get",
        lambda url, timeout, error="": rendered.append(url) or StealthResponse(url, 0, "", engine="playwright"),
    )
    for url in ("https://vuz.ru/", "https://spa.ru/", "https://protected.ru/", "https://captcha.ru/",
                "https://short.ru/", "https://yandex.ru/search/?text=x", "https://timeout.ru/", "https://tls.ru/"):
        fetcher.get(url)
    assert rendered == ["https://spa.ru/", "https://protected.ru/", "https://tls.ru/"]

    assert stealth.looks_like_spa_shell(spa)
    assert not stealth.looks_like_spa_shell(short_page)
    assert not stealth.network_error_needs_browser("Connection timed out")
    assert stealth.network_error_needs_browser("SSL: CERTIFICATE_VERIFY_FAILED")
