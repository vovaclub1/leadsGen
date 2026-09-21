import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Имена моделей DeepSeek, снятых с обслуживания. Старые конфиги молча падали с 400 Model Not Exist,
# поэтому подменяем их на актуальную модель и предупреждаем в логе при старте.
LEGACY_MODELS = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
# Модели DeepSeek, которые умеют читать изображения.
VISION_MODELS = {"deepseek-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-flash"}


def _int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    try:
        return float(value) if value else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "да")


def _clean_base_url(raw: str) -> str:
    """Принимает и https://api.deepseek.com, и .../v1, и случайно скопированный полный путь."""
    url = (raw or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/v1/chat/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url or "https://api.deepseek.com"


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    data_dir: Path
    db_path: Path
    tz: str
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    ai_vision_model: str
    ai_price_in: float
    ai_price_cached: float
    ai_price_out: float
    ai_timeout: int
    ai_thinking: bool
    ai_model_was_legacy: str
    tg_api_id: int
    tg_api_hash: str
    scanner_session: Path
    trustat_base: str

    @property
    def vision_possible(self) -> bool:
        return bool(self.ai_api_key and self.ai_vision_model)


def load() -> Config:
    data_dir = BASE_DIR / os.getenv("DATA_DIR", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните.")
    owner_id = _int("OWNER_ID")
    if not owner_id:
        raise SystemExit("OWNER_ID не задан в .env — без него никто не сможет войти в бота.")

    raw_model = os.getenv("AI_MODEL", "").strip() or "deepseek-flash"
    model = LEGACY_MODELS.get(raw_model, raw_model)
    legacy = raw_model if model != raw_model else ""

    raw_vision = os.getenv("AI_VISION_MODEL", "").strip()
    if raw_vision.lower() in ("off", "no", "none", "0"):
        vision_model = ""
    elif raw_vision:
        vision_model = LEGACY_MODELS.get(raw_vision, raw_vision)
    else:
        vision_model = model if model in VISION_MODELS else "deepseek-flash"

    return Config(
        bot_token=token,
        owner_id=owner_id,
        data_dir=data_dir,
        db_path=data_dir / "leadhunter.sqlite3",
        tz=os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow",
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=_clean_base_url(os.getenv("AI_BASE_URL", "https://api.deepseek.com")),
        ai_model=model,
        ai_vision_model=vision_model,
        # Цены DeepSeek-Flash за 1M токенов в пиковые часы — считаем по дороже, чтобы лимит расхода
        # срабатывал раньше, а не позже реального счёта.
        ai_price_in=_float("AI_PRICE_IN", 0.30),
        ai_price_cached=_float("AI_PRICE_CACHED", 0.006),
        ai_price_out=_float("AI_PRICE_OUT", 1.20),
        ai_timeout=_int("AI_TIMEOUT", 90),
        # У DeepSeek режим размышления включён по умолчанию: он утраивает расход выходных токенов
        # и время ответа. Для классификации он не нужен.
        ai_thinking=_bool("AI_THINKING", False),
        ai_model_was_legacy=legacy,
        tg_api_id=_int("TG_API_ID"),
        tg_api_hash=os.getenv("TG_API_HASH", "").strip(),
        scanner_session=data_dir / "scanner",
        trustat_base="https://api-public.trustat.me",
    )


config = load()
