"""独立 SOS 急救页面的 FastAPI 应用。

只提供两个接口：
- GET /            返回静态急救页面
- GET /api/health  返回健康检查结果

约束：
- 不导入 agent 包，不依赖主控 Agent、路由、业务模块或数据库；
- 不调用大模型、地图或任何外部服务；
- 大模型或数据库不可用时，本服务仍可独立启动和展示。
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

# 静态页面位置
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

app = FastAPI(title="SOS 急救指引", description="独立急救指引页面，不替代专业急救")


@app.get("/")
def index():
    """返回独立急救页面。"""
    return FileResponse(INDEX_FILE, media_type="text/html")


@app.get("/api/health")
def health():
    """健康检查，便于确认 SOS 服务已启动。"""
    return {"status": "ok"}