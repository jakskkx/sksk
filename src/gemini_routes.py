# src/gemini_routes.py (v2 - 修正路由顺序)

"""
Gemini API Routes - Handles native Gemini API endpoints.
This module provides native Gemini API endpoints that proxy directly to Google's API
without any format transformations.
"""
import json
import logging
from fastapi import APIRouter, Request, Response, Depends

from .auth import authenticate_user
from .google_api_client import send_gemini_request, build_gemini_payload_from_native
from .config import SUPPORTED_MODELS

router = APIRouter()

# --- 1. 【核心修正】将所有“精准路由”定义在前面 ---

@router.get("/v1beta/models")
async def gemini_list_models(request: Request, username: str = Depends(authenticate_user)):
    models_response = {"models": []}
    for model in SUPPORTED_MODELS:
        m = model.copy()
        m["name"] = m["name"].replace("models/", "") # Native API also uses short names
        models_response["models"].append(m)
    return Response(content=json.dumps(models_response), status_code=200, media_type="application/json; charset=utf-8")

@router.get("/v1/models")
async def gemini_list_models_v1(request: Request, username: str = Depends(authenticate_user)):
    return await gemini_list_models(request, username)

def _extract_model_from_path(path: str) -> str:
    parts = path.split('/')
    try:
        # 修正：原生API路径通常是 v1/models/gemini-pro:generateContent 或 v1beta/...
        # 所以模型名总是在 'models' 之后
        if 'models' in parts:
            models_index = parts.index('models')
            if models_index + 1 < len(parts):
                model_name = parts[models_index + 1]
                # 去掉可能的动作后缀，如 :generateContent
                if ':' in model_name:
                    model_name = model_name.split(':')[0]
                return model_name
    except ValueError:
        pass
    # 对于其他无法识别的路径，返回 None
    return None

# --- 2. 【核心修正】将“兜底路由” gemini_proxy 放在文件的最后 ---

@router.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def gemini_proxy(request: Request, full_path: str, username: str = Depends(authenticate_user)):
    # 这个兜底路由现在是最后一道防线，不会再错误地捕获 /v1/chat/completions

    try:
        post_data = await request.body()
        # 流式请求判断现在更加通用
        is_streaming = "stream" in full_path.lower() or "alt=sse" in str(request.query_params)
        model_name = _extract_model_from_path(full_path)
        
        if not model_name:
            # 现在这个错误只会对真正意图调用原生API但路径错误的请求触发
            return Response(content=json.dumps({"error": {"message": f"Could not extract model name from native Gemini API path: {full_path}"}}), status_code=400, media_type="application/json")
        
        try:
            incoming_request = json.loads(post_data) if post_data else {}
        except json.JSONDecodeError:
            return Response(content=json.dumps({"error": {"message": "Invalid JSON in request body"}}), status_code=400, media_type="application/json")
        
        # 使用 models/ 前缀来构建 payload，因为这是 config 中定义的格式
        gemini_payload = build_gemini_payload_from_native(incoming_request, f"models/{model_name}")
        
        response = await send_gemini_request(gemini_payload, is_streaming, request=request)
        
        return response
        
    except Exception as e:
        logging.error(f"Gemini proxy error on path '{full_path}': {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"Proxy error: {e}"}}), status_code=500, media_type="application/json")