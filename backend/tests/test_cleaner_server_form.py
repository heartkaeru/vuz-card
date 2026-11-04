from app.services.llm.fragment_models import FragmentExtractionRequest
from parser.cleaner import HTMLCleaner
from parser.llm_adapter import to_llm_request
from parser.models import UniversityData, UniversityInfo


def test_aspnet_form_keeps_leadership_and_removes_controls():
    html = """<html><body><form id="form1" runat="server">
      <input type="hidden" value="secret-view-state"><main><h1>Руководство</h1>
      <ul><li itemprop="rucovodstvo"><span itemprop="fio">Иванов Иван Иванович</span>
      <span itemprop="post">Ректор</span></li></ul></main><button>Отправить</button>
      </form></body></html>"""
    document = HTMLCleaner().to_document(html, url="https://university.example/sveden/managers", section="leadership")
    assert "Иванов Иван Иванович" in document.text and "Ректор" in document.text
    assert "secret-view-state" not in document.text and "Отправить" not in document.text
    card = UniversityData(university=UniversityInfo(name="Тест", url="https://university.example"), documents=[document])
    FragmentExtractionRequest.model_validate(to_llm_request(card, institution_id="draft:test"))


def test_small_search_form_is_still_removed():
    document = HTMLCleaner().to_document("<main><form><label>Искать на сайте</label><input></form>"
        "<p>Иванов Иван Иванович — Ректор.</p></main>", url="https://university.example/leadership")
    assert "Искать на сайте" not in document.text
    assert "Иванов Иван Иванович" in document.text


def test_phone_hint_preserves_source_format_instead_of_reformatting():
    html = "<main><h2>Приёмная ректора</h2><p>Телефон +7(3952)50-00-08, email Office@university.example</p></main>"
    document = HTMLCleaner().to_document(html, url="https://university.example/contacts", section="contacts")
    phones = [hint.value for fragment in document.fragments for hint in fragment.contact_hints if hint.field == "contact.phone"]
    assert phones == ["+7(3952)50-00-08"]
    card = UniversityData(university=UniversityInfo(name="Тест", url="https://university.example"), documents=[document])
    FragmentExtractionRequest.model_validate(to_llm_request(card, institution_id="draft:test"))
