# src/main.py (最终清理版 - 仅保留FastAPI核心)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- (使用相对导入，这是包内部的最佳实践) ---
from .openai_routes import router as openai_router
from .gemini_routes import router as gemini_router
from .logging_middleware import CustomLogMiddleware

# 1. 创建并配置 FastAPI 应用
app = FastAPI(title="Gemini 代理服务", version="1.1.0")

# 2. 添加中间件
app.add_middleware(CustomLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 集成路由
app.include_router(openai_router, prefix="/v1", tags=["OpenAI Compatible"])
app.include_router(gemini_router, tags=["Google Gemini Native"])

# 4. 添加根路径用于健康检查
@app.get("/", summary="Health Check", tags=["System"])
def read_root():
    return {"status": "ok", "message": "Gemini Proxy is running."}
