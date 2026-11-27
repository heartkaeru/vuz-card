try:  # запуск из backend/ (pytest)
    from parser.aggregators import PostupiParser, VuzopediaParser
    from parser.main import UniversityParser, _make_soup
    from parser.news_search import NewsSearch
    from parser.official_site import OfficialSiteDiscovery
    from parser.stealth import StealthResponse
    from parser.utils import normalize_whitespace
except ImportError:  # запуск из корня репозитория
    from backend.parser.aggregators import PostupiParser, VuzopediaParser
    from backend.parser.main import UniversityParser, _make_soup
    from backend.parser.news_search import NewsSearch
    from backend.parser.official_site import OfficialSiteDiscovery
    from backend.parser.stealth import StealthResponse
    from backend.parser.utils import normalize_whitespace


class FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str, timeout: float | None = None) -> StealthResponse:
        return StealthResponse(url, 200, self.pages.get(url, ""))


def test_vuzopedia_passport() -> None:
    page = """
    <html><body>
      <h1>Тестовый университет</h1>
      <a href="/region/city/59">Москва</a>
      <div>Учредитель гос. Общежитие есть Воен. уч. центр есть</div>
      <article class="vxStatCard">
        <span class="vxStatCard__label">Бюджетных мест</span>
        <strong class="vxStatCard__value">100</strong>
      </article>
      <article class="vxStatCard">
        <span class="vxStatCard__label">Платных мест</span>
        <strong class="vxStatCard__value">50</strong>
      </article>
    </body></html>
    """
    programs = """<html><body><ul>
      <li><a href="/p/1">09.03.01 Информатика и вычислительная техника</a></li>
      <li><a href="/p/2">38.03.01 Экономика</a></li>
    </ul></body></html>"""
    faculties = """
    <html><body>
      <div class="itemVuzTitle">Факультет тестовый</div>
      <div class="itemVuzTitle">Институт тестовый</div>
    </body></html>
    """
    fetcher = FakeFetcher(
        {
            "https://vuzopedia.ru/vuz/1": page,
            "https://vuzopedia.ru/vuz/1/podrazdeleniya": faculties,
            "https://vuzopedia.ru/vuz/1/programs/bakispec": programs,
        }
    )
    passport = VuzopediaParser(fetcher).parse("https://vuzopedia.ru/vuz/1")

    assert passport.city == "Москва"
    assert passport.status == "государственный"
    assert [item.code for item in passport.specialties] == ["09.03.01", "38.03.01"]
    assert passport.specialties[0].level == "бакалавриат"
    assert passport.specialties[0].group_name == "Информатика и вычислительная техника"
    assert passport.dormitory is True
    assert passport.military_department is True
    assert passport.faculties == ["Факультет тестовый", "Институт тестовый"]


def test_postupi_passport() -> None:
    page = """
    <html><body>
      <h1>Тестовый университет</h1>
      <div>Государственный, Общежитие, Воен. уч. центр</div>
      <div>100 бюджет. мест 50 платных мест</div>
      <div class="swiper-score__slide">
        <span>2024</span><span>80.5</span><span>70.1</span>
      </div>
    </body></html>
    """
    faculties = """
    <html><body>
      <h2>Факультет тестовый</h2>
      <h2>Институт тестовый</h2>
    </body></html>
    """
    fetcher = FakeFetcher(
        {
            "https://msk.postupi.online/vuz/test/": page,
            "https://msk.postupi.online/vuz/test/podrazdeleniya/": faculties,
            "https://msk.postupi.online/vuz/test/specialnosti/magistr/": (
                "<table><tr><td>38.04.08</td><td>Финансы и кредит</td></tr></table>"
            ),
        }
    )
    passport = PostupiParser(fetcher).parse(
        "https://msk.postupi.online/vuz/test/"
    )

    assert passport.city == "Москва"
    assert passport.status == "государственный"
    assert [(item.code, item.name, item.level) for item in passport.specialties] == [
        ("38.04.08", "Финансы и кредит", "магистратура")
    ]
    assert passport.dormitory is True
    assert passport.military_department is True
    assert passport.faculties == ["Факультет тестовый", "Институт тестовый"]


def test_bing_news_rss() -> None:
    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss><channel><item>
      <title>Сбербанк и МГУ заключили соглашение</title>
      <link>https://example.ru/news/1</link>
      <description>Соглашение о сотрудничестве и совместных проектах</description>
      <pubDate>Mon, 23 Mar 2026 14:49:00 GMT</pubDate>
    </item></channel></rss>"""
    search = NewsSearch(FakeFetcher({"https://www.bing.com/news/search?q=x&format=rss": rss}))
    hits = search._bing_news("x")

    assert len(hits) == 1
    assert hits[0].date == "2026-03-23"
    assert hits[0].title.startswith("Сбербанк и МГУ")
    assert search._relevant(hits[0], "Московский государственный университет", "МГУ")


def test_rector_photo_from_background() -> None:
    html = """
    <html><body>
      <div style="background-image: url('https://example.ru/rector_900.jpg')"></div>
    </body></html>
    """
    soup = _make_soup(html)
    photo = UniversityParser._find_photo(soup, "https://example.ru/", "Иванов Иван")

    assert photo == "https://example.ru/rector_900.jpg"


def test_rector_name_and_photo_auto() -> None:
    html = """
    <html><body>
      <h1>Ректор</h1>
      <p>Ректор Илья Николаевич Обабков</p>
      <img alt="Илья Николаевич Обабков"
           src="/upload/Obabkov.jpg" width="300" height="400">
    </body></html>
    """
    soup = _make_soup(html)
    text = normalize_whitespace(soup.get_text(" ", strip=True))
    name = OfficialSiteDiscovery._find_name(soup, text)

    assert OfficialSiteDiscovery._normalize_name(name) == "Обабков Илья Николаевич"
    assert OfficialSiteDiscovery._normalize_name("Анисимов Никита Юрьевич") == (
        "Анисимов Никита Юрьевич"
    )
