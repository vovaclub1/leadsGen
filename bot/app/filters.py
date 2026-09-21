from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class RoleFilter(BaseFilter):
    def __init__(self, *roles: str):
        self.roles = set(roles)

    async def __call__(self, event: TelegramObject, me: dict | None = None, **kwargs) -> bool:
        return bool(me) and me["role"] in self.roles


OWNER = RoleFilter("owner")
SENIOR_UP = RoleFilter("owner", "senior")
SELLERS = RoleFilter("owner", "senior", "sdr")
