# src/google_api_client.py (微调版)

"""
Google API Client - Handles all communication with Google's Gemini API.
This module is used by both OpenAI compatibility layer and native Gemini endpoints.
"""
import json
import logging
import httpx
import os
import asyncio
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .auth import get_next_credential, save_credentials
from .utils import get_user_agent, get_client_metadata
from .config import (
    CODE_ASSIST_ENDPOINT,
    DEFAULT_SAFETY_SETTINGS,
    get_base_model_name,
    is_search_model,
    get_thinking_budget,
    should_include_thoughts
)

async def get_user_project_id(creds, client: httpx.AsyncClient):
    """
    Get the user's project ID from a credential object or by making an API call.
    The project_id is cached on the credential object itself.
    """
    if hasattr(creds, 'project_id') and creds.project_id:
        return creds.project_id

    logging.info("Project ID not found on credential, fetching from API...")
    try:
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "User-Agent": get_user_agent()
        }
        response = await client.get("https://cloudresourcemanager.googleapis.com/v1/projects", headers=headers)
        response.raise_for_status()
        projects = response.json().get("projects", [])
        
        if not projects:
            raise Exception("No Google Cloud projects found for this account.")
            
        project_id = projects[0]["projectId"]
        logging.info(f"Discovered project ID: {project_id}")
        
        creds.project_id = project_id
        
        if os.getenv("GEMINI_CREDENTIALS") is None and not any(k.startswith("GEMINI_CREDENTIALS") for k in os.environ):
            save_credentials(creds, project_id)

        return project_id
    except Exception as e:
        logging.error(f"Failed to get user project ID: {e}")
        return None

async def onboard_user(creds, project_id, client: httpx.AsyncClient):
    """
    Onboard the user for the given project ID.
    """
    if hasattr(creds, 'onboarding_complete') and creds.onboarding_complete:
        return

    # --- 核心修改：将 INFO 降级为 DEBUG，以在控制台隐藏此消息 ---
    logging.debug(f"Performing onboarding check for project: {project_id}")
    try:
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
            "User-Agent": get_user_agent(),
        }
        data = {"clientMetadata": get_client_metadata(project_id)}
        response = await client.post(f"{CODE_ASSIST_ENDPOINT}/v1internal/projects/{project_id}:onboard", headers=headers, json=data)
        
        if response.status_code == 409:
             logging.debug("User already onboarded for this project.") # 也降级为 DEBUG
        elif response.is_success:
            logging.info("User successfully onboarded.")
        else:
            response.raise_for_status()

        creds.onboarding_complete = True
    except Exception as e:
        logging.debug(f"Onboarding request failed (this may be expected): {e}")

async def send_gemini_request(payload: dict, is_streaming: bool = False, request: Request = None) -> Response:
    """
    Send an async request to Google's Gemini API using httpx.
    """
    creds = get_next_credential()
    if not creds or not creds.token:
        error_msg = "Authentication failed. No valid credentials available."
        logging.error(error_msg)
        return Response(content=json.dumps({"error": {"message": error_msg}}), status_code=500, media_type="application/json")

    request_headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
        "User-Agent": get_user_agent(),
    }

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            proj_id = await get_user_project_id(creds, client)
            if not proj_id:
                return Response(content="Failed to get user project ID for the selected account.", status_code=500)
            
            # Store credential info in request.state for the logger
            if request:
                request.state.used_project_id = proj_id
                request.state.used_credential_index = getattr(creds, 'credential_source_index', None)
                request.state.used_credential_type = getattr(creds, 'credential_source_type', 'array_or_file')
            
            await onboard_user(creds, proj_id, client)
            
            final_payload = {
                "model": payload.get("model"),
                "project": proj_id,
                "request": payload.get("request", {})
            }
            final_post_data = json.dumps(final_payload)

            action = "streamGenerateContent" if is_streaming else "generateContent"
            target_url = f"{CODE_ASSIST_ENDPOINT}/v1internal:{action}"
            if is_streaming:
                target_url += "?alt=sse"

            if is_streaming:
                resp = await client.post(target_url, content=final_post_data, headers=request_headers)
                return _handle_streaming_response(resp)
            else:
                resp = await client.post(target_url, content=final_post_data, headers=request_headers)
                return _handle_non_streaming_response(resp)

    except httpx.RequestError as e:
        logging.error(f"Request to Google API failed: {e}")
        return Response(content=json.dumps({"error": {"message": f"Request failed: {e}"}}), status_code=502, media_type="application/json")
    except Exception as e:
        logging.error(f"Unexpected error during Google API request: {e}", exc_info=True)
        return Response(content=json.dumps({"error": {"message": f"Unexpected error: {e}"}}), status_code=500, media_type="application/json")


def _handle_streaming_response(resp: httpx.Response) -> StreamingResponse:
    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
                await asyncio.sleep(0)
        except Exception as e:
            logging.error(f"Error during streaming: {e}")
            error_response = {"error": {"message": f"Streaming error: {e}", "code": 500}}
            yield f'data: {json.dumps(error_response)}\n\n'.encode('utf-8')

    # Pass through headers from the original response
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in ['content-length', 'transfer-encoding', 'content-encoding']}
    
    return StreamingResponse(stream_generator(), status_code=resp.status_code, headers=headers, media_type=resp.headers.get("Content-Type"))


def _handle_non_streaming_response(resp: httpx.Response) -> Response:
    if resp.is_success:
        try:
            google_api_response_text = resp.text
            # It might be prefixed with 'data: '
            if google_api_response_text.strip().startswith('data: '):
                google_api_response_text = google_api_response_text.strip()[len('data: '):]
            
            google_api_response = json.loads(google_api_response_text)
            # The actual content is nested inside a "response" key.
            standard_gemini_response = google_api_response.get("response")
            
            # If the key is missing or value is null, return the full object for debugging
            if standard_gemini_response is None:
                 logging.warning(f"Google API response is missing 'response' field. Full response: {google_api_response_text}")
                 # Return an empty but valid structure to avoid downstream errors
                 standard_gemini_response = {"candidates": []}

            return Response(
                content=json.dumps(standard_gemini_response),
                status_code=200,
                media_type="application/json; charset=utf-8"
            )
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse Google API response: {e}. Raw text: {resp.text}")
            # Fallback to returning raw content if parsing fails
            return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("Content-Type"))
    else:
        logging.error(f"Google API returned error {resp.status_code}: {resp.text}")
        return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("Content-Type"))

# --- Payload Builders (No changes needed) ---

def build_gemini_payload_from_openai(openai_payload: dict) -> dict:
    model = openai_payload.get("model")
    safety_settings = openai_payload.get("safetySettings", DEFAULT_SAFETY_SETTINGS)
    request_data = {
        "contents": openai_payload.get("contents"),
        "systemInstruction": openai_payload.get("systemInstruction"),
        "cachedContent": openai_payload.get("cachedContent"),
        "tools": openai_payload.get("tools"),
        "toolConfig": openai_payload.get("toolConfig"),
        "safetySettings": safety_settings,
        "generationConfig": openai_payload.get("generationConfig", {}),
    }
    request_data = {k: v for k, v in request_data.items() if v is not None}
    return {"model": model, "request": request_data}

def build_gemini_payload_from_native(native_request: dict, model_from_path: str) -> dict:
    native_request["safetySettings"] = native_request.get("safetySettings", DEFAULT_SAFETY_SETTINGS)
    if "generationConfig" not in native_request:
        native_request["generationConfig"] = {}
    if "thinkingConfig" not in native_request["generationConfig"]:
        native_request["generationConfig"]["thinkingConfig"] = {}
    
    thinking_budget = get_thinking_budget(model_from_path)
    include_thoughts = should_include_thoughts(model_from_path)
    
    native_request["generationConfig"]["thinkingConfig"]["includeThoughts"] = include_thoughts
    if thinking_budget != -1:
        native_request["generationConfig"]["thinkingConfig"]["thinkingBudget"] = thinking_budget
    
    if is_search_model(model_from_path):
        if "tools" not in native_request:
            native_request["tools"] = []
        if not any(tool.get("googleSearch") for tool in native_request["tools"]):
            native_request["tools"].append({"googleSearch": {}})
    
    return {
        "model": get_base_model_name(model_from_path),
        "request": native_request
    }