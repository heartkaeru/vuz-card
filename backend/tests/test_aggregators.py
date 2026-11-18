try:  # запуск из backend/ (pytest)
    from parser.aggregators import PostupiParser, VuzopediaParser
    from parser.main import UniversityParser, _make_soup
    from parser.news_search import NewsSearch
    from parser.stealth import StealthResponse
except ImportError:  # запуск из корня репозитория
    from backend.parser.aggregators import PostupiParser, VuzopediaParser
    from backend.parser.main import UniversityParser, _make_soup
    from backend.parser.news_search import NewsSearch
    from backend.parser.stealth import StealthResponse


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
        }
    )
    passport = VuzopediaParser(fetcher).parse("https://vuzopedia.ru/vuz/1")

    assert passport.city == "Москва"
    assert passport.status == "государственный"
    assert passport.budget_places == 100
    assert passport.paid_places == 50
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
        }
    )
    passport = PostupiParser(fetcher).parse(
        "https://msk.postupi.online/vuz/test/"
    )

    assert passport.city == "Москва"
    assert passport.status == "государственный"
    assert passport.avg_ege == 80.5
    assert passport.budget_places == 100
    assert passport.paid_places == 50
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
