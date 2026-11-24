"""Cấu hình máy chủ và chuẩn bị danh mục sạch cho một ổ lưu trữ mới."""
from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Runtime:
    data_dir: Path
    port: int
    public_hostname: str | None = None
    invite_hashes: tuple[str, ...] = ()
    temporary_data: tempfile.TemporaryDirectory | None = None

    @property
    def bind_host(self):
        return "0.0.0.0" if self.public_hostname else "127.0.0.1"

    @property
    def allowed_hosts(self):
        return [self.public_hostname] if self.public_hostname else ["127.0.0.1", "localhost"]

    @property
    def public_origin(self):
        return f"https://{self.public_hostname}" if self.public_hostname else None


def load_runtime():
    mode = os.environ.get("TRIAL_DEPLOYMENT", "local")
    if mode not in ("local", "render", "render_free"):
        raise RuntimeError("TRIAL_DEPLOYMENT phải là local, render hoặc render_free.")
    if mode in ("render", "render_free"):
        hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.onrender\.com", hostname):
            raise RuntimeError("Thiếu tên miền onrender.com do Render cung cấp.")
        port = int(os.environ.get("PORT", "10000"))
        if mode == "render_free":
            invite_hashes = tuple(os.environ.get("TRIAL_INVITE_HASHES", "").split())
            if (
                not 1 <= len(invite_hashes) <= 10
                or len(invite_hashes) != len(set(invite_hashes))
                or not all(re.fullmatch(r"[a-f0-9]{64}", digest) for digest in invite_hashes)
            ):
                raise RuntimeError("TRIAL_INVITE_HASHES cần từ 1 đến 10 mã băm SHA-256 khác nhau, cách nhau bằng khoảng trắng.")
            # Tạo kho trống cho mỗi lần khởi động và nạp lại lời mời từ cấu hình Render.
            temporary_data = tempfile.TemporaryDirectory(prefix="ktht-render-free-")
            return Runtime(Path(temporary_data.name), port, hostname, invite_hashes, temporary_data)

        data_dir = Path(os.environ.get("TRIAL_DATA_DIR", "/var/data"))
        if not data_dir.is_absolute() or not data_dir.is_dir() or data_dir.resolve().is_relative_to(ROOT):
            raise RuntimeError("Ổ dữ liệu Render chưa sẵn sàng. Kiểm tra TRIAL_DATA_DIR và điểm gắn ổ.")
        return Runtime(data_dir.resolve(), port, hostname)
    data_dir = Path(os.environ.get("TRIAL_DATA_DIR", str(ROOT / ".local-data"))).resolve()
    return Runtime(data_dir, int(os.environ.get("PORT", "8004")))


def file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare_catalog(runtime):
    assets = ROOT / "catalog"
    expected = (assets / "catalog_seed.sha256").read_text(encoding="ascii").strip()
    runtime.data_dir.mkdir(parents=True, exist_ok=True)
    seed = runtime.data_dir / "catalog_seed.db"
    if seed.exists():
        if file_hash(seed) != expected:
            raise RuntimeError("Danh mục trên ổ dữ liệu khác bản phát hành. Cần kiểm tra trước khi cập nhật.")
        return

    # Ghi tệp tạm cùng ổ, thay tên sau khi kiểm tra checksum để tránh bản sao dang dở.
    with tempfile.TemporaryDirectory(dir=runtime.data_dir, prefix="catalog-") as temp_dir:
        staged = Path(temp_dir) / seed.name
        with gzip.open(assets / "catalog_seed.db.gz", "rb") as source, staged.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        if file_hash(staged) != expected:
            raise RuntimeError("Danh mục trong gói phát hành không khớp checksum.")
        os.replace(staged, seed)
