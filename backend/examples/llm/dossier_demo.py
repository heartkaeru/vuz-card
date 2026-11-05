"""Synthetic, offline layout rehearsal. Contains no real university/person facts."""

import argparse
import asyncio
from pathlib import Path

from app.services.llm.client import FixtureProvider
from app.services.llm.models import Institution
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch
from app.services.meeting_pptx import build_meeting_presentation
from database.llm_store import LLMRepository
from parser.biography import mark_biography_subject
from parser.cleaner import HTMLCleaner
from parser.models import UniversityData, UniversityInfo
from .workflow_demo import draft_response


async def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    name = "Иванов Иван Иванович"
    institution = Institution(id="draft:fictional-dossier", name="Учебный университет — пример данных", card_version=1)
    paragraphs = [f"{name} — Ректор.", "В 1999 году окончил Учебный университет по специальности «Экономика».",
                  "Доктор экономических наук, профессор.", "С 2022 года назначен ректором Учебного университета.",
                  "Научные интересы: экономика образования и управление организациями.",
                  "В 2001–2010 годах работал преподавателем кафедры экономики.",
                  "В 2010–2021 годах занимал должность проректора по учебной работе."]
    biography = HTMLCleaner().to_document(f"<main><h1>{name}</h1>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</main>",
        url="https://university.example/rector", section="bio")
    mark_biography_subject(biography, name)
    profile_text = ("Учебный университет. На 1 октября 2025 года студентов очной формы головного вуза — 1200 человек. "
                    "Направления подготовки: Экономика, Менеджмент, Прикладная информатика. "
                    "Уровни образования: бакалавриат, магистратура, аспирантура. "
                    "Структура: 3 факультета, колледж; филиалов нет. Основан в 1960 году.")
    profile = HTMLCleaner().to_document(f"<main><p>{profile_text}</p></main>", url="https://university.example/about", section="about")
    card = UniversityData(university=UniversityInfo(name=institution.name, url="https://university.example/"), documents=[biography, profile])
    person_fields = ["appointment.position", "person.education", "person.degree", "appointment.since", "person.interests", "person.career", "person.career"]
    personal = [{"subject": {"person_name": name}, "field": field, "value": paragraph if i else "Ректор",
                 "fragment_ids": [biography.fragments[i + 1].fragment_id]}
                for i, (field, paragraph) in enumerate(zip(person_fields, paragraphs))]
    personal.append({"subject": {"person_name": name}, "field": "person.academic_title", "value": "профессор",
                     "fragment_ids": [biography.fragments[3].fragment_id]})
    subject = {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "f0001"}
    context = [{"subject": subject, "field": "institution.students", "value": "1200", "fragment_ids": ["f0001"],
                "metric_context": {"period": "1 октября 2025 года", "scope": "студентов очной формы головного вуза", "unit": "человек"}}]
    context.extend({"subject": subject, "field": field, "value": value, "fragment_ids": ["f0001"]} for field, value in [
        ("institution.education_areas", "Экономика, Менеджмент, Прикладная информатика"),
        ("institution.education_levels", "бакалавриат, магистратура, аспирантура"),
        ("institution.structure", "Структура: 3 факультета, колледж; филиалов нет"),
        ("institution.founded", "Основан в 1960 году")])
    repository = LLMRepository(output / "fixture.sqlite3")
    workflow = LLMWorkflow(repository, AssistantPipeline(FixtureProvider([
        {"facts": personal, "gaps": []}, {"facts": context, "gaps": []}])))
    from uuid import uuid4
    research, _ = workflow.create(institution, card, "fixture-" + uuid4().hex)
    research = await workflow.extract(research.run_id)
    assert all(f.validation_status == "passed" for f in research.facts)
    research = workflow.review(research.run_id, ReviewBatch(expected_revision=research.revision,
        decisions=[{"fact_id": f.fact_id, "decision": "accept", "source_checked": True,
                    "reason": "Synthetic layout fixture, not a real source review"} for f in research.facts]), actor="synthetic-fixture-reviewer")
    workflow.pipeline = AssistantPipeline(FixtureProvider([draft_response(research)]))
    material, draft = await workflow.generate(research.run_id, research.revision)
    path = build_meeting_presentation(draft, output / "Учебный макет.pptx", repository.material_facts(material))
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/dossier-fixture"))
    asyncio.run(run(parser.parse_args().output))
