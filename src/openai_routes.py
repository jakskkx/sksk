# src/openai_routes.py (正确版本 - 负责API路由)

import json
import uuid
import logging
from fastapi import APIRouter, Request, Response, Depends
from fastapi.responses import StreamingResponse

# --- 核心依赖 ---
from .auth import authenticate_user
from .models import OpenAIChatCompletionRequest
from .config import SUPPORTED_MODELS
from .google_api_client import send_gemini_request, build_gemini_payload_from_openai

# --- 格式转换逻辑，从正确的文件导入 ---
from .openai_transformers import (
    openai_request_to_gemini,
    gemini_response_to_openai,
    gemini_stream_chunk_to_openai
)

# --- 定义路由器，这是 main.py 需要的变量 ---
router = APIRouter()


@router.post("/chat/completions")
async def openai_chat_completions(
    request: OpenAIChatCompletionRequest, 
    http_request: Request, 
    username: str = Depends(authenticate_user)
):
    try:
        # 1. 将OpenAI请求转换为Gemini格式
        gemini_request_data = openai_request_to_gemini(request)
        # 2. 构建最终发送给Google API的payload
        gemini_payload = build_gemini_payload_from_openai(gemini_request_data)
    except Exception as e:
        logging.error(f"处理OpenAI请求时出错: {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"请求处理失败: {e}"}}), status_code=400, media_type="application/json")
    
    # 3. 发送请求给Google API
    is_streaming = request.stream
    response = await send_gemini_request(gemini_payload, is_streaming=is_streaming, request=http_request)

    # 4. 处理Google API的响应
    if response.status_code != 200:
        return response # 直接返回错误响应

    if is_streaming:
        # 5a. 如果是流式响应，包装成SSE
        return StreamingResponse(
            _openai_stream_generator(response, request.model), 
            media_type="text/event-stream"
        )
    else:
        # 5b. 如果是普通响应，解析并转换为OpenAI格式
        try:
            gemini_response_body = response.content.decode('utf-8')
            gemini_response = json.loads(gemini_response_body) if gemini_response_body else {"candidates": []}
            openai_response_model = gemini_response_to_openai(gemini_response, request.model)
            return openai_response_model
        except Exception as e:
            logging.error(f"处理非流式响应失败: {e}", exc_info=True)
            return Response(
                content=json.dumps({"error": {"message": f"处理响应失败: {e}"}}),
                status_code=500,
                media_type="application/json"
            )


async def _openai_stream_generator(response: StreamingResponse, model: str):
    """将Gemini的流式响应转换为OpenAI的流式格式"""
    response_id = "chatcmpl-" + str(uuid.uuid4())
    try:
        async for chunk in response.body_iterator:
            chunk_str = chunk.decode('utf-8', "ignore")
            for line in chunk_str.splitlines():
                if line.startswith('data: '):
                    try:
                        gemini_chunk = json.loads(line[6:])
                        actual_payload = gemini_chunk.get("response")
                        if not actual_payload or not actual_payload.get("candidates"):
                            continue
                        
                        openai_chunk_model = gemini_stream_chunk_to_openai(actual_payload, model, response_id)
                        if openai_chunk_model.choices:
                            yield f"data: {openai_chunk_model.model_dump_json()}\n\n"

                    except (json.JSONDecodeError, KeyError) as e:
                        logging.warning(f"跳过无效的流数据块: '{line}', 错误: {e}")
                        continue
        yield "data: [DONE]\n\n"
    except Exception as e:
        logging.error(f"流生成器错误: {e}", exc_info=True)


@router.get("/models")
async def openai_list_models(username: str = Depends(authenticate_user)):
    """返回兼容OpenAI格式的模型列表"""
    openai_models = []
    for model_info in SUPPORTED_MODELS:
        model_id = model_info["name"].replace("models/", "")
        openai_models.append({
            "id": model_id, "object": "model", "created": int(time.time()), "owned_by": "google",
        })
    return {"object": "list", "data": openai_models}
