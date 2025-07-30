# src/main.py 【终极完整版】

import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

# 现在这些导入可以正常工作了，因为 run.py 已经设置好了路径
from src.gemini_routes import router as gemini_router
from src.openai_routes import router as openai_router
from src.auth import load_credentials_pool
from src.logging_middleware import CustomLogMiddleware

# 加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# !!! 注意：logging.basicConfig 和 getLogger 的配置已移至 run.py 进行统一管理 !!!
# !!! 这是为了确保使用 `python run.py` 启动时，自定义日志能完全生效 !!!

app = FastAPI(title="Gemini API Proxy", version="1.2.0-fixed")

# 添加自定义中文日志中间件
app.add_middleware(CustomLogMiddleware)

# 添加 CORS 中间件，保持原有逻辑
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 【重要】恢复您原来的、更详细的启动事件日志
@app.on_event("startup")
async def startup_event():
    try:
        logging.info("Starting Gemini proxy server and loading credentials...")
        load_credentials_pool(allow_oauth_flow=True)
        logging.info("Gemini proxy server started successfully.")
        logging.info("Authentication required - Password: see .env file or config.")
    except Exception as e:
        logging.error(f"Fatal startup error: {str(e)}")
        logging.warning("Server may not function properly.")

# 【重要】恢复您原来的、功能更完整的 OPTIONS 预检请求处理器
@app.options("/{full_path:path}")
async def handle_preflight(request: Request, full_path: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Credentials": "true",
        }
    )

# 【重要】恢复您原来的、信息非常详细的根路径信息
@app.get("/")
async def root():
    return {
        "name": "geminicli2api",
        "description": "OpenAI-compatible API proxy for Google's Gemini models via gemini-cli",
        "purpose": "Provides both OpenAI-compatible endpoints (/v1/chat/completions) and native Gemini API endpoints for accessing Google's Gemini models with multi-account round-robin support.",
        "version": "1.2.0-fixed",
        "endpoints": {
            "openai_compatible": {
                "chat_completions": "/v1/chat/completions",
                "models": "/v1/models"
            },
            "native_gemini": {
                "models": "/v1beta/models",
                "generate": "/v1beta/models/{model}/generateContent",
                "stream": "/v1beta/models/{model}/streamGenerateContent"
            },
            "health": "/health"
        },
        "authentication": "Required for all endpoints except root and health",
    }

# 添加健康检查点
@app.get("/health", tags=["Health Check"])
async def health_check():
    return {"status": "ok"}

# 包含路由，保持原有结构
app.include_router(openai_router)
app.include_router(gemini_router)