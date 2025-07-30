"""
OpenAI API Routes - Handles OpenAI-compatible endpoints.
This module provides OpenAI-compatible endpoints that transform requests/responses
and delegate to the Google API client.
"""
import json
import uuid
import asyncio
import logging
import time  # <<< [修复] 添加 time 模块导入

from fastapi import APIRouter, Request, Response, Depends
from fastapi.responses import StreamingResponse

from .auth import authenticate_user
from .models import OpenAIChatCompletionRequest
from .openai_transformers import (
    openai_request_to_gemini,
    gemini_response_to_openai,
    gemini_stream_chunk_to_openai
)
from .google_api_client import send_gemini_request, build_gemini_payload_from_openai

router = APIRouter()

@router.post("/v1/chat/completions")
async def openai_chat_completions(
    request: OpenAIChatCompletionRequest, 
    http_request: Request, 
    username: str = Depends(authenticate_user)
):
    try:
        gemini_request_data = openai_request_to_gemini(request)
        gemini_payload = build_gemini_payload_from_openai(gemini_request_data)
    except Exception as e:
        logging.error(f"Error processing OpenAI request: {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"Request processing failed: {e}"}}), status_code=400, media_type="application/json")
    
    is_streaming = request.stream
    response = await send_gemini_request(gemini_payload, is_streaming=is_streaming, request=http_request)

    if response.status_code != 200:
        try:
            error_body = response.body.decode('utf-8', 'ignore')
            error_data = json.loads(error_body)
            error_message = error_data.get("error", {}).get("message", "Unknown API error")
        except:
            error_message = f"API error with status code {response.status_code}"
        
        return Response(
            content=json.dumps({"error": {"message": error_message, "type": "api_error", "code": response.status_code}}),
            status_code=response.status_code,
            media_type="application/json"
        )

    if is_streaming:
        return StreamingResponse(
            _openai_stream_generator(response, request.model), 
            media_type="text/event-stream"
        )
    else:
        try:
            gemini_response_body = response.body.decode('utf-8')
            if not gemini_response_body or gemini_response_body == 'null':
                 gemini_response = {"candidates": []}
            else:
                gemini_response = json.loads(gemini_response_body)
            
            openai_response = gemini_response_to_openai(gemini_response, request.model)
            return openai_response
        except Exception as e:
            logging.error(f"Failed to process non-streaming response: {e}", exc_info=True)
            return Response(
                content=json.dumps({"error": {"message": f"Failed to process response: {e}"}}),
                status_code=500,
                media_type="application/json"
            )

async def _openai_stream_generator(response: StreamingResponse, model: str):
    response_id = "chatcmpl-" + str(uuid.uuid4())
    try:
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', "ignore")
            
            for line in chunk.splitlines():
                if line.startswith('data: '):
                    try:
                        chunk_data = line[6:]
                        if not chunk_data:
                            continue
                        gemini_chunk = json.loads(chunk_data)

                        if "response" in gemini_chunk:
                            openai_chunk = gemini_stream_chunk_to_openai(gemini_chunk["response"], model, response_id)
                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                        elif "error" in gemini_chunk:
                             logging.error(f"Error in Gemini stream: {gemini_chunk['error']}")
                             yield f"data: {json.dumps(gemini_chunk)}\n\n"
                        
                    except (json.JSONDecodeError, KeyError) as e:
                        logging.warning(f"Skipping invalid stream chunk: {line}, error: {e}")
                        continue
        yield "data: [DONE]\n\n"
    except Exception as e:
        logging.error(f"Streaming generator error: {e}", exc_info=True)
        error_data = {"error": {"message": f"An unexpected streaming error occurred: {e}", "type": "api_error", "code": 500}}
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"

# No changes needed for openai_list_models, but it is included for completeness.
@router.get("/v1/models")
async def openai_list_models(username: str = Depends(authenticate_user)):
    try:
        from .config import SUPPORTED_MODELS
        openai_models = []
        for model in SUPPORTED_MODELS:
            model_id = model["name"].replace("models/", "")
            openai_models.append({
                "id": model_id, "object": "model", "created": int(time.time()), "owned_by": "google",
            })
        return {"object": "list", "data": openai_models}
    except Exception as e:
        logging.error(f"Failed to list models: {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"Failed to list models: {e}"}}), status_code=500, media_type="application/json")
