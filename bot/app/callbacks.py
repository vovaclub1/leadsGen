from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    a: str
    v: str = ""


class LeadCb(CallbackData, prefix="ld"):
    a: str
    id: int
    v: str = ""


class AdmCb(CallbackData, prefix="adm"):
    s: str
    a: str = "list"
    id: int = 0
    v: str = ""
