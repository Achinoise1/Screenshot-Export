"""
backend/main.py

FastAPI 应用入口（单进程，同时 serve 前端静态页面）：
  - JobDatabase  — SQLite 持久化，重启后恢复历史记录
  - JobState     — 单个 job 的运行时快照
  - JobStore     — 内存 + SQLite 双层存储
  - REST / SSE 路由
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from backend.processor import DocxBuilder, ProcessParams, ProgressEvent, VideoProcessor

# 前端 index.html 路径（相对于项目根目录）
_FRONTEND_HTML = Path(__file__).parent.parent / "frontend" / "index.html"


# ──────────────────────────────────────────────────────────────────────────────
# Job 状态枚举
# ──────────────────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    READY      = "ready"
    GENERATING = "generating"
    DONE       = "done"
    ERROR      = "error"


# ──────────────────────────────────────────────────────────────────────────────
# Job 数据结构
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class JobState:
    """表示一个处理任务的完整运行时状态。"""
    job_id: str
    status: JobStatus = JobStatus.PENDING
    video_path: Path = field(default_factory=Path)
    screenshots_dir: Path = field(default_factory=Path)
    output_path: Path = field(default_factory=Path)
    screenshot_count: int = 0
    error_message: str = ""
    queue: asyncio.Queue[ProgressEvent] | None = None
    # 持久化字段
    video_filename: str = ""
    output_filename: str = "output.docx"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ──────────────────────────────────────────────────────────────────────────────
# SQLite 持久化层
# ──────────────────────────────────────────────────────────────────────────────

class JobDatabase:
    """SQLite 持久化存储，每次操作独立创建连接，线程安全。"""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id           TEXT PRIMARY KEY,
            status           TEXT NOT NULL,
            video_filename   TEXT DEFAULT '',
            video_path       TEXT DEFAULT '',
            screenshot_count INTEGER DEFAULT 0,
            output_filename  TEXT DEFAULT '',
            error_message    TEXT DEFAULT '',
            created_at       TEXT NOT NULL
        )
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        with self._conn() as conn:
            conn.execute(self._SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, state: JobState) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO jobs
                   (job_id, status, video_filename, video_path,
                    screenshot_count, output_filename, error_message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    state.job_id, state.status.value,
                    state.video_filename, str(state.video_path),
                    state.screenshot_count, state.output_filename,
                    state.error_message, state.created_at,
                ),
            )

    def update(self, state: JobState) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE jobs SET status=?, screenshot_count=?, error_message=?
                   WHERE job_id=?""",
                (state.status.value, state.screenshot_count,
                 state.error_message, state.job_id),
            )

    def load_all(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()]

    def delete(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))


# ──────────────────────────────────────────────────────────────────────────────
# JobStore 单例（内存 + SQLite）
# ──────────────────────────────────────────────────────────────────────────────

_db = JobDatabase(config.DATABASE_PATH)


class JobStore:
    _instance: "JobStore | None" = None

    def __new__(cls) -> "JobStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._jobs: dict[str, JobState] = {}
        return cls._instance

    def create(self, video_path: Path, video_filename: str = "") -> JobState:
        """创建新 job，生成时间戳文件名，持久化到 SQLite。"""
        job_id = str(uuid.uuid4())
        screenshots_dir = config.SCREENSHOTS_DIR / job_id
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        output_filename = f"截图整理_{ts}.docx"
        output_path = config.OUTPUTS_DIR / job_id / output_filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        state = JobState(
            job_id=job_id,
            video_path=video_path,
            screenshots_dir=screenshots_dir,
            output_path=output_path,
            video_filename=video_filename,
            output_filename=output_filename,
            created_at=datetime.now().isoformat(),
        )
        self._jobs[job_id] = state
        _db.save(state)
        return state

    def get(self, job_id: str) -> JobState:
        if job_id not in self._jobs:
            raise KeyError(job_id)
        return self._jobs[job_id]

    def list_all(self) -> list[JobState]:
        return sorted(self._jobs.values(), key=lambda s: s.created_at, reverse=True)

    def update_status(self, job_id: str, status: JobStatus, **kwargs) -> None:
        state = self.get(job_id)
        state.status = status
        for key, val in kwargs.items():
            setattr(state, key, val)
        _db.update(state)

    def delete(self, job_id: str) -> None:
        if job_id in self._jobs:
            del self._jobs[job_id]
        _db.delete(job_id)


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI 应用
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Screen-Export",
    root_path=config.BACKEND_ROOT_PATH,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_store = JobStore()


# ── 启动恢复：从 SQLite 重建内存状态 ──────────────────

@app.on_event("startup")
async def _restore_from_db() -> None:
    """服务启动时从 SQLite 恢复所有历史 job 到内存。"""
    for row in _db.load_all():
        job_id = row["job_id"]
        status_str = row["status"]
        error_message = row.get("error_message") or ""

        # 重启时被中断的任务改标为 error
        if status_str in ("processing", "generating"):
            status_str = "error"
            error_message = "服务重启，任务中断"
            row["status"] = status_str
            row["error_message"] = error_message
            # 同步更新 DB
            tmp = JobState(
                job_id=job_id,
                status=JobStatus.ERROR,
                screenshot_count=row.get("screenshot_count", 0),
                error_message=error_message,
                video_filename=row.get("video_filename") or "",
                output_filename=row.get("output_filename") or "output.docx",
                created_at=row["created_at"],
            )
            _db.update(tmp)

        output_filename = row.get("output_filename") or "output.docx"
        state = JobState(
            job_id=job_id,
            status=JobStatus(status_str),
            video_path=Path(row["video_path"]) if row.get("video_path") else Path(),
            screenshots_dir=config.SCREENSHOTS_DIR / job_id,
            output_path=config.OUTPUTS_DIR / job_id / output_filename,
            screenshot_count=row.get("screenshot_count", 0),
            error_message=error_message,
            video_filename=row.get("video_filename") or "",
            output_filename=output_filename,
            created_at=row["created_at"],
        )
        _store._jobs[job_id] = state


# ── Pydantic 模型 ────────────────────────────────────

class ProcessRequest(BaseModel):
    sample_fps: int = 5
    change_threshold: float = 3.0
    stable_seconds: float = 2.0
    hash_threshold: int = 5


class JobResponse(BaseModel):
    job_id: str
    status: str
    screenshot_count: int
    error_message: str


class JobListItem(BaseModel):
    job_id: str
    status: str
    video_filename: str
    screenshot_count: int
    output_filename: str
    error_message: str
    created_at: str
    has_screenshots: bool
    has_docx: bool


# ── 路由 ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """返回前端单页。"""
    return HTMLResponse(_FRONTEND_HTML.read_text(encoding="utf-8"))


@app.get("/jobs", response_model=list[JobListItem])
async def list_jobs() -> list[JobListItem]:
    """返回所有 job 历史（含文件是否存在标志），按创建时间倒序。"""
    result = []
    for state in _store.list_all():
        result.append(JobListItem(
            job_id=state.job_id,
            status=state.status.value,
            video_filename=state.video_filename,
            screenshot_count=state.screenshot_count,
            output_filename=state.output_filename,
            error_message=state.error_message,
            created_at=state.created_at,
            has_screenshots=(
                state.screenshots_dir.exists()
                and any(state.screenshots_dir.glob("page_*.png"))
            ),
            has_docx=state.output_path.exists(),
        ))
    return result


@app.post("/jobs/upload")
async def upload_video(file: UploadFile) -> dict:
    """上传视频文件，返回 job_id。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        raise HTTPException(status_code=400, detail=f"不支持的视频格式：{suffix}")

    tmp_id = str(uuid.uuid4())
    video_path = config.UPLOAD_DIR / f"{tmp_id}{suffix}"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    video_path.write_bytes(content)

    safe_filename = Path(file.filename).name
    state = _store.create(video_path, video_filename=safe_filename)
    return {"job_id": state.job_id}


@app.post("/jobs/{job_id}/process")
async def start_process(
    job_id: str,
    req: ProcessRequest,
    background: BackgroundTasks,
) -> dict:
    """启动视频处理后台任务。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    if state.status not in (JobStatus.PENDING, JobStatus.ERROR):
        raise HTTPException(status_code=409, detail=f"Job 当前状态为 {state.status}，无法重新处理")

    params = ProcessParams(
        sample_fps=req.sample_fps,
        change_threshold=req.change_threshold,
        stable_seconds=req.stable_seconds,
        hash_threshold=req.hash_threshold,
    )
    state.queue = asyncio.Queue()
    _store.update_status(job_id, JobStatus.PROCESSING)
    background.add_task(_run_processor, job_id, params)
    return {"job_id": job_id, "status": state.status}


@app.get("/jobs/{job_id}/progress")
async def stream_progress(job_id: str) -> StreamingResponse:
    """SSE 端点，实时推送处理进度。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    return StreamingResponse(
        _sse_generator(state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    """查询 job 状态。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    return JobResponse(
        job_id=state.job_id,
        status=state.status.value,
        screenshot_count=state.screenshot_count,
        error_message=state.error_message,
    )


@app.get("/jobs/{job_id}/screenshots")
async def list_screenshots(job_id: str) -> dict:
    """返回截图文件名列表。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    files = sorted(state.screenshots_dir.glob("page_*.png"))
    return {"screenshots": [f.name for f in files]}


@app.get("/jobs/{job_id}/screenshots/{filename}")
async def get_screenshot(job_id: str, filename: str) -> FileResponse:
    """返回单张截图文件。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    safe_name = Path(filename).name
    img_path = state.screenshots_dir / safe_name
    if not img_path.exists() or not img_path.is_relative_to(state.screenshots_dir):
        raise HTTPException(status_code=404, detail="截图文件不存在")

    return FileResponse(img_path, media_type="image/png")


@app.post("/jobs/{job_id}/generate-docx")
async def generate_docx(job_id: str, background: BackgroundTasks) -> dict:
    """启动 DOCX 生成后台任务。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    if state.status != JobStatus.READY:
        raise HTTPException(status_code=409, detail=f"Job 状态为 {state.status}，需先完成视频处理")

    _store.update_status(job_id, JobStatus.GENERATING)
    background.add_task(_run_docx_builder, job_id)
    return {"job_id": job_id, "status": JobStatus.GENERATING}


@app.get("/jobs/{job_id}/download")
async def download_docx(job_id: str) -> FileResponse:
    """下载生成的 DOCX 文件，使用时间戳文件名。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    if state.status != JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"DOCX 尚未生成，当前状态：{state.status}")

    if not state.output_path.exists():
        raise HTTPException(status_code=404, detail="输出文件不存在")

    return FileResponse(
        state.output_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=state.output_filename,
    )


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    """删除 job 及其全部磁盘文件。"""
    try:
        state = _store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job 不存在")

    shutil.rmtree(state.screenshots_dir, ignore_errors=True)
    shutil.rmtree(state.output_path.parent, ignore_errors=True)
    state.video_path.unlink(missing_ok=True)
    _store.delete(job_id)
    return {"deleted": job_id}


# ──────────────────────────────────────────────────────────────────────────────
# 后台任务
# ──────────────────────────────────────────────────────────────────────────────

async def _run_processor(job_id: str, params: ProcessParams) -> None:
    state = _store.get(job_id)
    processor = VideoProcessor(state.video_path, state.screenshots_dir, params)
    try:
        count = await processor.run(state.queue)
        _store.update_status(job_id, JobStatus.READY, screenshot_count=count)
    except Exception as exc:
        _store.update_status(job_id, JobStatus.ERROR, error_message=str(exc))


async def _run_docx_builder(job_id: str) -> None:
    state = _store.get(job_id)
    builder = DocxBuilder(state.screenshots_dir, state.output_path)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, builder.build)
        _store.update_status(job_id, JobStatus.DONE)
    except Exception as exc:
        _store.update_status(job_id, JobStatus.ERROR, error_message=str(exc))


async def _sse_generator(state: JobState) -> AsyncGenerator[str, None]:
    for _ in range(50):
        if state.queue is not None:
            break
        await asyncio.sleep(0.1)

    if state.queue is None:
        yield 'data: {"type":"error","message":"Queue 未初始化"}\n\n'
        return

    while True:
        try:
            event: ProgressEvent = await asyncio.wait_for(state.queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
            continue

        yield f"data: {event.to_json()}\n\n"

        if event.type in ("done", "error"):
            break


# ──────────────────────────────────────────────────────────────────────────────
# 启动入口
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=config.BACKEND_PORT,
        reload=False,
    )
