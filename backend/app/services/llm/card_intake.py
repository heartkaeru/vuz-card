"""Read the institution name and official URL supplied by the employee."""

import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, HttpUrl, ValidationError

from parser.public_http import public_url

from .client import LLMError
from .models import StrictModel
from .workflow_models import WorkflowError


class CollectCard(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=160)
    website: str | None = Field(default=None, min_length=1, max_length=2048)


COLLECT_TOOL = {
    "name": "collect_card",
    "description": "Создаёт черновик и запускает сбор по официальному сайту; если адрес не указан, сервер найдёт его по названию.",
    "parameters": CollectCard.model_json_schema(),
}
COLLECT_PROMPT = """Для явной просьбы создать карточку или собрать сведения о новом вузе вызови collect_card.
Название, сайт и город бери из последнего сообщения. Если город не указан, оставь его пустым.
Используй официальный URL, только если он явно указан в сообщении. Если URL нет, оставь website пустым:
сервер найдёт сайт по названию. Если не хватает названия, воздержись от вызова.
Не запускай сбор по ссылке, приведённой как пример или в вопросе о возможностях."""


def supplied_website(args, question):
    supplied = re.findall(r"https?://[^\s<>«»\"']+", question, flags=re.IGNORECASE)
    supplied = {url.rstrip(".,;!?)") for url in supplied}

    def canonical(url):
        parts = urlsplit(str(HttpUrl(url)))
        return urlunsplit(parts._replace(path=parts.path.rstrip("/"), fragment=""))

    if len(supplied) > 1:
        raise WorkflowError("chat_collection_url_required")
    if " ".join(args.name.casefold().split()) not in " ".join(question.casefold().split()):
        raise WorkflowError("chat_collection_name_required")
    city = args.city if " ".join(args.city.casefold().split()) in " ".join(question.casefold().split()) else ""

    if not supplied:
        # Ignore any URL the model may invent; discovery uses the server-side search.
        return {"name": args.name, "city": city, "website": None}
    try:
        candidate = canonical(next(iter(supplied)))
    except ValueError:
        raise WorkflowError("chat_collection_url_required") from None
    if args.website:
        try:
            website = canonical(str(HttpUrl(args.website)))
            public_url(website)
        except ValueError:
            raise WorkflowError("public_website_required") from None
        if website != candidate:
            raise WorkflowError("chat_collection_url_required")
    else:
        website = candidate
        try:
            public_url(website)
        except ValueError:
            raise WorkflowError("public_website_required") from None
    if website != candidate:
        raise WorkflowError("chat_collection_url_required")
    return {"name": args.name, "city": city, "website": website}


def collection_arguments(choice, question):
    call = choice.message.function_call
    if choice.finish_reason != "function_call" or not call or call.name != COLLECT_TOOL["name"]:
        raise LLMError("invalid_chat_tool_call")
    try:
        return supplied_website(CollectCard.model_validate(call.arguments), question)
    except ValidationError:
        raise LLMError("invalid_chat_tool_call") from None


async def plan_collection(provider, question):
    choice = await provider.chat([
        {"role": "system", "content": COLLECT_PROMPT},
        {"role": "user", "content": question},
    ], functions=[COLLECT_TOOL], function_call="auto")
    if choice.finish_reason == "stop":
        return None
    return collection_arguments(choice, question)
