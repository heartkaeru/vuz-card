"""Supplement the LLM input with explicit institutional disclosure pages."""

from html import escape
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from parser.cleaner import HTMLCleaner
from parser.institution_context import mark_institution_context
from parser.utils import is_same_domain

from .evidence import literal_value_in_text
from .models import EntitySubject, Evidence, Fact, FactField


PROGRAM_TITLE = re.compile(r"^(?P<code>\d{2}\.\d{2}\.\d{2})\.?\s+(?P<name>\S.*)$")


def official_leadership_document(client, website, documents, *, max_chars):
    if any(urlsplit(doc.url).path.startswith("/sveden/managers") for doc in documents):
        return None
    response = client.get(urljoin(website, "/sveden/managers/"))
    if response is None or not is_same_domain(response.final_url, website):
        return None
    document = HTMLCleaner(max_chars=max_chars).to_document(response.text, url=response.final_url,
                                                           section="leadership")
    document.url = urljoin(response.final_url, document.url)
    if not is_same_domain(document.url, website):
        return None
    return document if any(f.contact_hints or f.leadership_position for f in document.fragments) else None


def official_reference_document(client, url, website, name, *, max_chars):
    if not is_same_domain(url, website):
        return None
    response = client.get(url)
    if response is None or not is_same_domain(response.final_url, website):
        return None
    cleaner = HTMLCleaner(max_chars=max_chars)
    document = cleaner.to_document(response.text, url=response.final_url, section="about")
    # The query can select a program even when the site declares a generic canonical URL.
    document.url = (response.final_url if urlsplit(response.final_url).query else
                    urljoin(response.final_url, document.url))
    if not is_same_domain(document.url, website):
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    has_anchor = mark_institution_context(document, name)
    program = PROGRAM_TITLE.fullmatch(title.partition("|")[0].strip())
    if literal_value_in_text(name, title) and (program or not has_anchor):
        source_lines = [title]
    elif urlsplit(response.final_url).path.startswith("/sveden/education/"):
        source_lines = disclosure_program_lines(soup, name)
        program = PROGRAM_TITLE.fullmatch(source_lines[-1]) if source_lines else None
    else:
        source_lines = []
    if not source_lines:
        return document
    prefix = "".join(f"<p>{escape(line)}</p>" for line in source_lines)
    html = f'<div id="vuz-reference">{prefix}{document.main_html}</div>'
    published_at = document.published_at
    document = HTMLCleaner(content_selectors=("#vuz-reference",), max_chars=max_chars).to_document(
        html, url=document.url, section="about",
    )
    document.published_at = published_at
    mark_institution_context(document, name)
    if program:
        for fragment in document.fragments:
            if fragment.institution_anchor_id and literal_value_in_text(
                program[0], document.text[fragment.start:fragment.end],
            ):
                document.metadata["program_title_fragment"] = fragment.fragment_id
                break
    return document


def disclosure_program_lines(soup, institution_name):
    code = soup.select_one('[itemprop~="eduCode"]')
    program_name = soup.select_one('[itemprop~="eduName"]')
    if code is None or program_name is None:
        return []
    code_text = code.get_text(" ", strip=True)
    name_text = program_name.get_text(" ", strip=True)
    for node in soup.select("h1, h2, h3, h4, h5, h6"):
        heading = node.get_text(" ", strip=True)
        program = PROGRAM_TITLE.fullmatch(heading)
        if not program or program["code"] != code_text or not literal_value_in_text(name_text, program["name"]):
            continue
        for footer in soup.select("footer p, [role=contentinfo] p, p.text-center"):
            line = footer.get_text(" ", strip=True)
            if line.startswith("©") and literal_value_in_text(institution_name, line):
                publisher = re.sub(r"^©\s*(?:\d{4}\s*[,–-]?\s*)?", "", line)
                return [publisher, heading]
        break
    return []


def official_program_candidates(documents, institution, existing):
    for document in documents:
        selected = document.metadata.get("program_title_fragment")
        if not selected:
            continue
        fragments = {fragment.fragment_id: fragment for fragment in document.fragments}
        fragment = fragments.get(selected)
        anchor = fragments.get(fragment.institution_anchor_id) if fragment else None
        if anchor is None:
            continue
        quote = document.text[fragment.start:fragment.end]
        program = PROGRAM_TITLE.fullmatch(quote.partition("|")[0].strip())
        if not program or not literal_value_in_text(institution.name, document.text[anchor.start:anchor.end]):
            continue
        if any(fact.field == FactField.EDUCATION_AREAS and fact.validation_status == "passed" and not fact.issues
               and any(proof.url == document.url for proof in fact.evidence)
               and literal_value_in_text(program["name"], fact.value) for fact in existing):
            continue
        yield Fact(
            fact_id=f"fact_{uuid4().hex}", institution_id=institution.id,
            subject=EntitySubject(entity_id=institution.id, entity_kind="institution", entity_name=institution.name),
            field=FactField.EDUCATION_AREAS, value=program[0], origin="parser_projection",
            evidence=[Evidence(
                source_id=document.source_id, url=document.url, quote=document.text[proof.start:proof.end],
                retrieved_at=document.retrieved_at, published_at=document.published_at,
                content_sha256=document.text_hash, chunk_id=f"parser:{proof.fragment_id}",
                fragment_id=proof.fragment_id, start_offset=proof.start, end_offset=proof.end,
            ) for proof in ([fragment] if fragment == anchor else [fragment, anchor])],
            validation_status="passed", verification_status="pending",
        )
