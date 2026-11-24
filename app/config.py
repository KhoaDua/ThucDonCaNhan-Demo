"""Cấu hình ứng dụng: đường dẫn, chế độ dữ liệu, AI và giới hạn tìm kiếm.

Đọc từ biến môi trường; nếu có tệp `.env` ở thư mục dự án thì nạp trước (không ghi đè
biến đã đặt). Khóa AI không bao giờ được in ra: `Settings.__repr__` che giá trị.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_LOADED = False


def load_env_file(path: Path | None = None) -> None:
    """Nạp KEY=VALUE từ `.env` (đơn giản, không cần thư viện ngoài)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = path or (BASE_DIR / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: Path
    data_mode: str = "demo"  # "demo" | "operational"
    ai_provider: str = "mock"  # "mock" | "none" | "failing" | "openai_compatible"
    ai_base_url: str = ""
    ai_model: str = ""
    ai_api_key: str = field(default="", repr=False)
    ai_timeout_s: float = 20.0
    ai_tool_max_results: int = 10
    solver: str = "builtin"  # "builtin" | "milp"
    search_timeout_s: float = 10.0
    search_max_nodes: int = 2_000_000
    admin_token: str = field(default="", repr=False)
    reports_dir: Path = BASE_DIR / "data" / "reports"
    checklist_energy_rule_set: str = ""
    checklist_sodium_rule_set: str = ""

    def __repr__(self) -> str:  # không để lộ khóa
        return (
            f"Settings(db_path={self.db_path!s}, data_mode={self.data_mode}, ai_provider={self.ai_provider}, "
            f"ai_model={self.ai_model or '-'}, ai_api_key={'***' if self.ai_api_key else '(trống)'}, "
            f"solver={self.solver}, search_timeout_s={self.search_timeout_s})"
        )

    @property
    def dataset(self) -> str:
        """Bộ công thức/giá dùng để lập thực đơn theo chế độ dữ liệu."""
        return "demo" if self.data_mode == "demo" else "operational"

    def public_dict(self) -> dict:
        return {
            "data_mode": self.data_mode,
            "dataset": self.dataset,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "solver": self.solver,
            "search_timeout_s": self.search_timeout_s,
            "search_max_nodes": self.search_max_nodes,
        }


def get_settings(**overrides) -> Settings:
    load_env_file()
    db = os.environ.get("APP_DB_PATH", str(BASE_DIR / "data" / "app.db"))
    mode = os.environ.get("APP_DATA_MODE", "demo").strip().lower()
    if mode not in ("demo", "operational"):
        mode = "demo"
    s = Settings(
        db_path=Path(db),
        data_mode=mode,
        ai_provider=os.environ.get("AI_PROVIDER", "mock").strip().lower(),
        ai_base_url=os.environ.get("AI_BASE_URL", "").strip(),
        ai_model=os.environ.get("AI_MODEL", "").strip(),
        ai_api_key=os.environ.get("AI_API_KEY", "").strip(),
        ai_timeout_s=_float("AI_TIMEOUT_S", 20.0),
        ai_tool_max_results=max(1, min(_int("AI_TOOL_MAX_RESULTS", 10), 50)),
        solver=os.environ.get("PLANNER_SOLVER", "builtin").strip().lower(),
        search_timeout_s=_float("PLANNER_TIMEOUT_S", 10.0),
        search_max_nodes=_int("PLANNER_MAX_NODES", 2_000_000),
        admin_token=os.environ.get("ADMIN_TOKEN", "").strip(),
        checklist_energy_rule_set=os.environ.get("CHECKLIST_ENERGY_RULE_SET", "").strip(),
        checklist_sodium_rule_set=os.environ.get("CHECKLIST_SODIUM_RULE_SET", "").strip(),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s
