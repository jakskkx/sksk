"""
Gemini API Routes - Handles native Gemini API endpoints.
This module provides native Gemini API endpoints that proxy directly to Google's API
without any format transformations.
"""
import json
import logging
import time
from fastapi import APIRouter, Request, Response, Depends

from .auth import authenticate_user
from .google_api_client import send_gemini_request, build_gemini_payload_from_native
from .config import SUPPORTED_MODELS

router = APIRouter()

@router.get("/v1beta/models")
async def gemini_list_models(request: Request, username: str = Depends(authenticate_user)):
    models_response = {"models": []}
    for model in SUPPORTED_MODELS:
        m = model.copy()
        m["name"] = m["name"].replace("models/", "") # Native API also uses short names
        models_response["models"].append(m)

    return Response(content=json.dumps(models_response), status_code=200, media_type="application/json; charset=utf-8")

@router.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def gemini_proxy(request: Request, full_path: str, username: str = Depends(authenticate_user)):
    # This check prevents this catch-all route from capturing the /v1/models endpoint
    if "models" in full_path and not ("generateContent" in full_path or "countTokens" in full_path):
         if request.method == "GET":
              # Assuming it's a models list request
              if full_path in ["v1/models", "v1beta/models"]:
                   return await gemini_list_models(request, username)
              else: # Specific model GET
                   pass # continue to proxy
         else:
             pass # continue to proxy

    try:
        post_data = await request.body()
        is_streaming = "stream" in full_path.lower()
        model_name = _extract_model_from_path(full_path)
        
        if not model_name:
            return Response(content=json.dumps({"error": {"message": f"Could not extract model name from path: {full_path}"}}), status_code=400, media_type="application/json")
        
        try:
            incoming_request = json.loads(post_data) if post_data else {}
        except json.JSONDecodeError as e:
            return Response(content=json.dumps({"error": {"message": "Invalid JSON in request body"}}), status_code=400, media_type="application/json")
        
        gemini_payload = build_gemini_payload_from_native(incoming_request, f"models/{model_name}")
        
        # Pass the original request to link logging
        response = await send_gemini_request(gemini_payload, is_streaming, request=request)
        
        # The response from send_gemini_request is already a complete FastAPI Response/StreamingResponse
        return response
        
    except Exception as e:
        logging.error(f"Gemini proxy error: {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"Proxy error: {e}"}}), status_code=500, media_type="application/json")


def _extract_model_from_path(path: str) -> str:
    parts = path.split('/')
    try:
        models_index = parts.index('models')
        if models_index + 1 < len(parts):
            model_name = parts[models_index + 1]
            if ':' in model_name:
                model_name = model_name.split(':')[0]
            return model_name
    except ValueError:
        pass
    return None

@router.get("/v1/models")
async def gemini_list_models_v1(request: Request, username: str = Depends(authenticate_user)):
    return await gemini_list_models(request, username)