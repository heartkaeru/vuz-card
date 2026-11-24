"""Workflow questions and registry lookup without an active institution."""

from pydantic import Field, ValidationError

from .card_chat import CONTEXT_LIMIT
from .card_intake import COLLECT_PROMPT, COLLECT_TOOL, collection_arguments
from .changes import normalized
from .client import LLMError
from .models import StrictModel


class FindCards(StrictModel):
    query: str = Field(default="", max_length=300, description="Название или город. Пустая строка для списка карточек.")


REGISTRY_TOOL = {
    "name": "find_cards",
    "description": "Находит сохранённые карточки в реестре ВУЗ-CRM по названию или городу.",
    "parameters": FindCards.model_json_schema(),
}
GENERAL_PROMPT = """Ты помощник ВУЗ-CRM. Это общий чат без выбранного вуза.
На вопросы о возможностях и о том, как работать с CRM, отвечай понятным следующим шагом.
Вопрос о порядке работы не является командой на создание карточки или сбор.
Карточку можно создать по названию; официальный URL необязателен, сервер умеет искать сайт сам.
Сбор идёт в фоне, результат и ход видны в карточке.
Назови «Проверить факты» для сверки с источниками и «Принять выбранные» для подтверждения.
Для презентации по принятым сведениям предложи кнопку «Презентация» в чате открытой карточки.
Для подбора партнёрств предложи открыть чат нужного вуза и сообщить цель встречи.
При просьбе собрать или обновить сведения без названия учреждения попроси уточнить, о каком вузе речь.
Для обновления уже существующей карточки воспользуйся find_cards, если известно её название.
Предложи открыть её и запустить сбор в чате по карточке.
Для вопроса о составе реестра воспользуйся find_cards. Для вопроса о руководителях,
контактах или других сведениях учреждения предложи открыть чат по карточке. Если пользователь
назвал вуз и хочет открыть его карточку, воспользуйся find_cards.
Правки в общем чате недоступны. Для правки предложи открыть чат нужного вуза.
Не утверждай, что действие выполнено. На посторонний вопрос кратко объясни, в чём можешь помочь.
Не считай историю источником сведений о вузах. Отвечай по-русски, без Markdown."""


async def answer_general(provider, history, question):
    messages = [{"role": "system", "content": f"{GENERAL_PROMPT}\n{COLLECT_PROMPT}"}]
    for turn in history[-CONTEXT_LIMIT:]:
        answer = turn["answer"]
        cards = [f"{card['name']} ({card['city']})" if card["city"] else card["name"]
                 for card in answer.get("cards", [])]
        messages.extend([
            {"role": "user", "content": turn["question"]},
            {"role": "assistant", "content": "\n".join([answer["text"], *cards])},
        ])
    messages.append({"role": "user", "content": question})
    choice = await provider.chat(messages, functions=[REGISTRY_TOOL, COLLECT_TOOL], function_call="auto")
    call = choice.message.function_call
    if choice.finish_reason == "function_call" and call:
        if call.name == COLLECT_TOOL["name"]:
            return {"collection_request": collection_arguments(choice, question)}
        if call.name == REGISTRY_TOOL["name"]:
            try:
                return {"registry_query": FindCards.model_validate(call.arguments).query}
            except ValidationError:
                raise LLMError("invalid_chat_tool_call") from None
    if choice.finish_reason == "stop" and not call and choice.message.content.strip():
        return {"text": choice.message.content.strip()[:3000], "sources": []}
    raise LLMError("invalid_chat_tool_call")


def registry_answer(cards, query):
    query = normalized(query)
    matches = [card for card in cards if query in normalized(f"{card['name']} {card['city']}")]
    if not matches:
        text = "Подходящих карточек в реестре нет." if query else "В реестре пока нет карточек."
    elif len(matches) > 10:
        text = f"Найдено карточек: {len(matches)}. Показаны первые 10. Уточните название или город."
    else:
        text = "Выберите карточку, чтобы открыть диалог по вузу."
    return {"text": text, "sources": [], "cards": matches[:10]}
