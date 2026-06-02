import os
from pathlib import Path

# ── 端口 ──────────────────────────────────────────────
BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8001"))

# ── 子路径（nginx proxy_pass 挂载点）─────────────────
# 本地开发留空；部署到 nginx 子路径时通过环境变量设置：
#   export BACKEND_ROOT_PATH=/screen-export
BACKEND_ROOT_PATH: str = os.getenv("BACKEND_ROOT_PATH", "")

# ── 数据目录 ──────────────────────────────────────────
# 所有路径均可通过环境变量独立指定，脱离项目代码目录。
# 只需设置 SCREEN_EXPORT_DATA_DIR 即可统一迁移数据根目录，
# 也可单独覆盖某个子目录。
#
# 示例（将数据放到项目外）：
#   export SCREEN_EXPORT_DATA_DIR=/var/screen-export-data
#
BASE_DIR: Path = Path(__file__).parent
DATA_DIR: Path = Path(os.getenv("SCREEN_EXPORT_DATA_DIR", BASE_DIR / "data"))

UPLOAD_DIR: Path      = Path(os.getenv("SCREEN_EXPORT_UPLOAD_DIR",      DATA_DIR / "uploads"))
SCREENSHOTS_DIR: Path = Path(os.getenv("SCREEN_EXPORT_SCREENSHOTS_DIR", DATA_DIR / "screenshots"))
OUTPUTS_DIR: Path     = Path(os.getenv("SCREEN_EXPORT_OUTPUTS_DIR",     DATA_DIR / "outputs"))
DATABASE_PATH: Path   = Path(os.getenv("SCREEN_EXPORT_DATABASE_PATH",   DATA_DIR / "jobs.db"))

for _d in (UPLOAD_DIR, SCREENSHOTS_DIR, OUTPUTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
