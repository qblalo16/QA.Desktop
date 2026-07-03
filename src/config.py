import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(env_path: Path) -> None:
    """Parsea y carga un archivo .env en os.environ sin sobrescribir variables ya presentes."""
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=False)
        return
    except ImportError:
        pass

    # Fallback manual: parsea KEY=VALUE ignorando comentarios y líneas vacías
    content = env_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        # Quitar comillas envolventes si existen
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        # Solo establece si la variable no está ya en el entorno
        if key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).parent.parent / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    api_base_url: str = field(default_factory=lambda: os.getenv("API_BASE_URL", "https://iaopt-atm-testing-services-dev.azurewebsites.net/api/v1"))

    # Azure OpenAI configuration
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", ""))
    openai_api_version: str = field(default_factory=lambda: os.getenv("OPENAI_API_VERSION", "2024-12-01-preview"))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))

    # 2Captcha para resolución automática de captchas
    two_captcha_api_key: str = field(default_factory=lambda: os.getenv("TWO_CAPTCHA_API_KEY", ""))
    two_captcha_timeout: int = field(default_factory=lambda: int(os.getenv("TWO_CAPTCHA_TIMEOUT", "180")))

    # Playwright runtime
    playwright_headless: bool = field(default_factory=lambda: _env_bool("PLAYWRIGHT_HEADLESS", False))
    playwright_slow_mo_ms: int = field(default_factory=lambda: int(os.getenv("PLAYWRIGHT_SLOW_MO_MS", "200")))


settings = Settings()
