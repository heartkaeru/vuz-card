"""Supplement the LLM input with explicit institutional disclosure pages."""

from html import escape
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from parser.cleaner import HTMLCleaner
from parser.institution_context import mark_institution_context
from parser.utils import canonical_url, is_same_domain

from .evidence import literal_value_in_text
from .models import EntitySubject, Evidence, Fact, FactField


PROGRAM_TITLE = re.compile(r"^(?P<code>\d{2}\.\d{2}\.\d{2})\.?\s+(?P<name>\S.*)$")
PROGRAM_CODE = re.compile(r"(?<![\d.])\d{2}\.\d{2}\.\d{2}(?![\d.])")
SELF_STUDY = re.compile(r"отч[её]т\w*\s+о\s+(?:результатах\s+)?самообследовани\w*", re.I)
BRANCH_REPORT = re.compile(r"\b(?:филиал\w*|представительств\w*)\b", re.I)
PROGRAM_LIMIT = 12
BASIC_FIELDS = {"regDate": FactField.FOUNDED, "address": FactField.LOCATION, "type": FactField.TYPE}


def official_self_study_url(client, website):
    response = client.get(urljoin(website, "/sveden/document/"))
    if response is None or not is_same_domain(response.final_url, website):
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    reports = []
    for link in soup.select("a[href]"):
        url = urljoin(response.final_url, link["href"].replace("\\", "/"))
        if urlsplit(url).scheme not in {"http", "https"} or not is_same_domain(url, website):
            continue
        if not urlsplit(url).path.lower().endswith(".pdf"):
            continue
        label = link.get_text(" ", strip=True)
        if not SELF_STUDY.search(label) and sum(urlsplit(a["href"]).path.lower().endswith(".pdf")
                                                for a in link.parent.select("a[href]")) == 1:
            label = link.parent.get_text(" ", strip=True)
        if not SELF_STUDY.search(label) or BRANCH_REPORT.search(label):
            continue
        year = re.search(r"\bза\s+(20\d{2})\s*г", label, re.I)
        reports.append((int(year[1]) if year else 0, url))
    return max(reports, key=lambda item: item[0])[1] if reports else None


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
    basics = {}
    if re.match(r"^/sveden/common(?:/|$)", urlsplit(response.final_url).path, re.I):
        owner = soup.select_one('[itemprop~="fullName"]')
        full_name = owner.get_text(" ", strip=True) if owner else ""
        source_lines = [full_name] if literal_value_in_text(name, full_name) else []
        if source_lines:
            for key in ("regDate", "address"):
                node = soup.select_one(f'[itemprop~="{key}"]')
                if node is None:
                    continue
                value = node.get_text(" ", strip=True)
                if not value or len(value) > 400 or (key == "regDate" and not re.search(r"\b\d{4}\b", value)):
                    continue
                block = node.parent.get_text(" ", strip=True)
                source_lines.append(block if len(block) <= 500 else value)
                basics[key] = value
            kind = re.search(r"\b(?:образовательное учреждение|образовательная организация)\s+высшего образования\b",
                             full_name, re.I)
            if kind:
                basics["type"] = kind[0]
    elif literal_value_in_text(name, title) and (program or not has_anchor):
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
    for key, value in basics.items():
        fragment = next((f for f in document.fragments if f.institution_anchor_id
                         and literal_value_in_text(value, document.text[f.start:f.end])), None)
        if fragment:
            document.metadata[f"basic_{key}_fragment"] = fragment.fragment_id
            document.metadata[f"basic_{key}_value"] = value
    if program:
        for fragment in document.fragments:
            if fragment.institution_anchor_id and literal_value_in_text(
                program[0], document.text[fragment.start:fragment.end],
            ):
                document.metadata["program_title_fragment"] = fragment.fragment_id
                break
    return document


def institutional_candidate(document, institution, field, value, proofs):
    return Fact(
        fact_id=f"fact_{uuid4().hex}", institution_id=institution.id,
        subject=EntitySubject(entity_id=institution.id, entity_kind="institution", entity_name=institution.name),
        field=field, value=value, origin="parser_projection",
        evidence=[Evidence(
            source_id=document.source_id, url=document.url, quote=document.text[proof.start:proof.end],
            retrieved_at=document.retrieved_at, published_at=document.published_at,
            content_sha256=document.text_hash, chunk_id=f"parser:{proof.fragment_id}",
            fragment_id=proof.fragment_id, start_offset=proof.start, end_offset=proof.end,
        ) for proof in proofs], validation_status="passed", verification_status="pending",
    )


def official_basic_candidates(documents, institution, existing):
    for document in documents:
        fragments = {f.fragment_id: f for f in document.fragments}
        for key, field in BASIC_FIELDS.items():
            fragment = fragments.get(document.metadata.get(f"basic_{key}_fragment"))
            value = document.metadata.get(f"basic_{key}_value")
            if not fragment or not value or not literal_value_in_text(value, document.text[fragment.start:fragment.end]):
                continue
            anchor = fragments.get(fragment.institution_anchor_id)
            if not anchor or not literal_value_in_text(institution.name, document.text[anchor.start:anchor.end]):
                continue
            if any(f.field == field and f.validation_status == "passed" and not f.issues
                   and literal_value_in_text(value, f.value) for f in existing):
                continue
            yield institutional_candidate(document, institution, field, value,
                                          [fragment] if fragment == anchor else [fragment, anchor])


def official_catalog_candidates(card, institution, existing, fields):
    """Use only parser entries that can be tied to one published row on the official site."""
    website = card.university.url
    if not website or not fields.intersection({FactField.EDUCATION_AREAS, FactField.EDUCATION_LEVELS}):
        return
    entries = {}
    for program in card.specialties:
        if PROGRAM_CODE.fullmatch(program.code) and program.name and is_same_domain(program.source_url, website):
            entries.setdefault(canonical_url(program.source_url), {}).setdefault(program.code, []).append(program)
    selected = []
    codes = set()
    levels = {f.value.casefold() for f in existing if f.field == FactField.EDUCATION_LEVELS
              and f.validation_status == "passed" and not f.issues}
    for document in card.documents:
        catalog = entries.get(canonical_url(document.url))
        if not catalog or (document.section != "programs" and not urlsplit(document.url).path.startswith("/sveden/education")):
            continue
        for fragment in document.fragments:
            if fragment.end - fragment.start > 600:
                continue
            quote = document.text[fragment.start:fragment.end]
            row_codes = set(PROGRAM_CODE.findall(quote))
            if len(row_codes) != 1:
                continue
            code = next(iter(row_codes))
            for program in catalog.get(code, ()):
                if not literal_value_in_text(program.name, quote):
                    continue
                value = f"{code} {program.name}"
                if code not in codes:
                    selected.append((value, document, fragment))
                    codes.add(code)
                level = program.level.strip()
                if FactField.EDUCATION_LEVELS in fields and level and level.casefold() not in levels and literal_value_in_text(level, quote):
                    levels.add(level.casefold())
                    yield institutional_candidate(document, institution, FactField.EDUCATION_LEVELS, level, [fragment])
                break
    if FactField.EDUCATION_AREAS not in fields:
        return
    group_counts = {}
    for value, document, fragment in sorted(selected, key=lambda item: item[0]):
        group = value.partition(".")[0]
        group_counts.setdefault(group, []).append((value, document, fragment))
    # Cover different fields of study first so a large catalog does not dominate review.
    ordered = sorted((pair for group in group_counts.values() for pair in enumerate(group)), key=lambda pair: pair[0])
    for _, (value, document, fragment) in ordered[:PROGRAM_LIMIT]:
        if not any(f.field == FactField.EDUCATION_AREAS and f.validation_status == "passed" and not f.issues
                   and literal_value_in_text(value, f.value) for f in existing):
            yield institutional_candidate(document, institution, FactField.EDUCATION_AREAS, value, [fragment])


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
        yield institutional_candidate(document, institution, FactField.EDUCATION_AREAS, program[0],
                                      [fragment] if fragment == anchor else [fragment, anchor])
