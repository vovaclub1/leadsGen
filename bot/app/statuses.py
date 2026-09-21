"""Единый источник правды по статусам лида: константы, подписи, разрешённые действия.

Вместо россыпи строковых литералов (`lead["status"] not in ("NEW", "CLAIMED", …)`) —
одна карта «статус → действия». Опечатка в строке здесь = падение на импорте, а не молчаливый баг.
"""

NEW = "NEW"
CLAIMED = "CLAIMED"
CONTACTED = "CONTACTED"
REPLIED = "REPLIED"
HANDOFF = "HANDOFF"
ACCEPTED = "ACCEPTED"
WON = "WON"
LOST = "LOST"
NOT_TARGET = "NOT_TARGET"
DUPLICATE = "DUPLICATE"
ARCHIVED = "ARCHIVED"

ACTIVE = (CLAIMED, CONTACTED, REPLIED, HANDOFF, ACCEPTED)
OPEN = (NEW,) + ACTIVE
CLOSED = (WON, LOST, NOT_TARGET, DUPLICATE, ARCHIVED)

STATUS_RU = {
    NEW: "В очереди",
    CLAIMED: "Взят, ждёт первого контакта",
    CONTACTED: "Контакт установлен",
    REPLIED: "Клиент ответил",
    HANDOFF: "Передан старшему",
    ACCEPTED: "В работе у старшего",
    WON: "Сделка закрыта",
    LOST: "Потерян",
    NOT_TARGET: "Нецелевой",
    DUPLICATE: "Дубликат",
    ARCHIVED: "Архив",
}

# Какие действия карточки доступны на каждом статусе (кнопки private_card_kb и проверки в handlers).
ACTIONS: dict[str, frozenset[str]] = {
    NEW: frozenset({"claim", "nt", "dup", "dnc"}),
    CLAIMED: frozenset({"wrote", "chk", "dnc", "nt", "hand", "note"}),
    CONTACTED: frozenset({"replied", "touch", "post", "chk", "dnc", "nt", "hand", "note"}),
    REPLIED: frozenset({"hand", "chk", "post", "dnc", "nt", "note"}),
    HANDOFF: frozenset({"note"}),
    ACCEPTED: frozenset({"note"}),
    WON: frozenset(),
    LOST: frozenset(),
    NOT_TARGET: frozenset(),
    DUPLICATE: frozenset(),
    ARCHIVED: frozenset(),
}


def is_active(status: str) -> bool:
    return status in ACTIVE


def is_open(status: str) -> bool:
    return status in OPEN


def can(status: str, action: str) -> bool:
    """Разрешено ли действие над лидом в этом статусе."""
    return action in ACTIONS.get(status, frozenset())
