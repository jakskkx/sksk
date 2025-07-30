"""
Google API Client - Handles all communication with Google's Gemini API.
This module is used by both OpenAI compatibility layer and native Gemini endpoints.
"""
import json
import logging
import requests
import time
import os
from fastapi import Response
from fastapi.responses import StreamingResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
import asyncio

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

# --- Project ID and Onboarding Logic ---
# This logic is kept here as it's tightly coupled with API requests.

def get_user_project_id(creds):
    """
    Get the user's project ID from a credential object or by making an API call.
    The project_id is cached on the credential object itself.
    """
    # 1. Check if project_id is already cached on the credential object
    if hasattr(creds, 'project_id') and creds.project_id:
        return creds.project_id

    # 2. If not, fetch it from the API
    logging.info("Project ID not found on credential, fetching from API...")
    try:
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "User-Agent": get_user_agent()
        }
        response = requests.get("https://cloudresourcemanager.googleapis.com/v1/projects", headers=headers)
        response.raise_for_status()
        projects = response.json().get("projects", [])
        
        if not projects:
            raise Exception("No Google Cloud projects found for this account.")
            
        project_id = projects[0]["projectId"]
        logging.info(f"Discovered project ID: {project_id}")
        
        # Cache the project ID on the credential object for future use
        creds.project_id = project_id
        
        # If credentials came from a file, update it with the new project ID
        # This check is a bit indirect, but effective.
        if os.getenv("GEMINI_CREDENTIALS") is None:
            save_credentials(creds, project_id)

        return project_id
    except Exception as e:
        logging.error(f"Failed to get user project ID: {e}")
        return None

def onboard_user(creds, project_id):
    """
    Onboard the user for the given project ID. A check is made to see if onboarding
    has already been done for this credential to avoid duplicate requests.
    """
    # Check if onboarding has already been completed for this credential
    if hasattr(creds, 'onboarding_complete') and creds.onboarding_complete:
        return

    logging.info(f"Performing onboarding check for project: {project_id}")
    try:
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
            "User-Agent": get_user_agent(),
        }
        data = {"clientMetadata": get_client_metadata(project_id)}
        response = requests.post(f"{CODE_ASSIST_ENDPOINT}/v1internal/projects/{project_id}:onboard", headers=headers, json=data)
        
        if response.status_code == 409: # Conflict, already onboarded
             logging.info("User already onboarded for this project.")
        elif response.ok:
            logging.info("User successfully onboarded.")
        else:
            response.raise_for_status()

        # Mark this credential as onboarded
        creds.onboarding_complete = True

    except Exception as e:
        logging.warning(f"Onboarding request failed (this may be expected if already onboarded): {e}")


def send_gemini_request(payload: dict, is_streaming: bool = False) -> Response:
    """
    Send a request to Google's Gemini API.
    
    Args:
        payload: The request payload in Gemini format
        is_streaming: Whether this is a streaming request
        
    Returns:
        FastAPI Response object
    """
    # Get the next available credential from the pool
    creds = get_next_credential()
    if not creds or not creds.token:
        error_msg = "Authentication failed. No valid credentials available in the pool."
        logging.error(error_msg)
        return Response(content=error_msg, status_code=500)

    # Get project ID and onboard user for this specific credential
    proj_id = get_user_project_id(creds)
    if not proj_id:
        return Response(content="Failed to get user project ID for the selected account.", status_code=500)
    
    # Log which credential (by project_id) is being used for this request.
    logging.info(f"Using credential for project: {proj_id}")
    
    onboard_user(creds, proj_id)

    # Build the final payload with project info
    final_payload = {
        "model": payload.get("model"),
        "project": proj_id,
        "request": payload.get("request", {})
    }

    # Determine the action and URL
    action = "streamGenerateContent" if is_streaming else "generateContent"
    target_url = f"{CODE_ASSIST_ENDPOINT}/v1internal:{action}"
    if is_streaming:
        target_url += "?alt=sse"

    # Build request headers
    request_headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
        "User-Agent": get_user_agent(),
    }

    final_post_data = json.dumps(final_payload)

    # Send the request
    try:
        if is_streaming:
            resp = requests.post(target_url, data=final_post_data, headers=request_headers, stream=True)
            return _handle_streaming_response(resp)
        else:
            resp = requests.post(target_url, data=final_post_data, headers=request_headers)
            return _handle_non_streaming_response(resp)
    except requests.exceptions.RequestException as e:
        logging.error(f"Request to Google API failed: {str(e)}")
        return Response(
            content=json.dumps({"error": {"message": f"Request failed: {str(e)}"}}),
            status_code=500,
            media_type="application/json"
        )
    except Exception as e:
        logging.error(f"Unexpected error during Google API request: {str(e)}")
        return Response(
            content=json.dumps({"error": {"message": f"Unexpected error: {str(e)}"}}),
            status_code=500,
            media_type="application/json"
        )

def _handle_streaming_response(resp) -> StreamingResponse:
    """Handle streaming response from Google API."""
    
    # Check for HTTP errors before starting to stream
    if resp.status_code != 200:
        logging.error(f"Google API returned status {resp.status_code}: {resp.text}")
        error_message = f"Google API error: {resp.status_code}"
        try:
            error_data = resp.json()
            if "error" in error_data:
                error_message = error_data["error"].get("message", error_message)
        except:
            pass
        
        # Return error as a streaming response
        async def error_generator():
            error_response = {
                "error": {
                    "message": error_message,
                    "type": "invalid_request_error" if resp.status_code == 404 else "api_error",
                    "code": resp.status_code
                }
            }
            yield f'data: {json.dumps(error_response)}\n\n'.encode('utf-8')
        
        response_headers = {
            "Content-Type": "text/event-stream",
            "Content-Disposition": "attachment",
            "Vary": "Origin, X-Origin, Referer",
            "X-XSS-Protection": "0",
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
            "Server": "ESF"
        }
        
        return StreamingResponse(
            error_generator(),
            media_type="text/event-stream",
            headers=response_headers,
            status_code=resp.status_code
        )
    
    async def stream_generator():
        try:
            with resp:
                for chunk in resp.iter_lines():
                    if chunk:
                        if not isinstance(chunk, str):
                            chunk = chunk.decode('utf-8', "ignore")
                            
                        if chunk.startswith('data: '):
                            chunk = chunk[len('data: '):]
                            
                            try:
                                obj = json.loads(chunk)
                                
                                if "response" in obj:
                                    response_chunk = obj["response"]
                                    response_json = json.dumps(response_chunk, separators=(',', ':'))
                                    response_line = f"data: {response_json}\n\n"
                                    yield response_line.encode('utf-8', "ignore")
                                    await asyncio.sleep(0) # Give other tasks a chance to run
                                else:
                                    obj_json = json.dumps(obj, separators=(',', ':'))
                                    yield f"data: {obj_json}\n\n".encode('utf-8', "ignore")
                            except json.JSONDecodeError:
                                continue
                
        except requests.exceptions.RequestException as e:
            logging.error(f"Streaming request failed: {str(e)}")
            error_response = {
                "error": {
                    "message": f"Upstream request failed: {str(e)}",
                    "type": "api_error",
                    "code": 502
                }
            }
            yield f'data: {json.dumps(error_response)}\n\n'.encode('utf-8', "ignore")
        except Exception as e:
            logging.error(f"Unexpected error during streaming: {str(e)}")
            error_response = {
                "error": {
                    "message": f"An unexpected error occurred: {str(e)}",
                    "type": "api_error",
                    "code": 500
                }
            }
            yield f'data: {json.dumps(error_response)}\n\n'.encode('utf-8', "ignore")

    response_headers = {
        "Content-Type": "text/event-stream",
        "Content-Disposition": "attachment",
        "Vary": "Origin, X-Origin, Referer",
        "X-XSS-Protection": "0",
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Server": "ESF"
    }
    
    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers=response_headers
    )

def _handle_non_streaming_response(resp) -> Response:
    """Handle non-streaming response from Google API."""
    if resp.status_code == 200:
        try:
            google_api_response = resp.text
            if google_api_response.startswith('data: '):
                google_api_response = google_api_response[len('data: '):]
            google_api_response = json.loads(google_api_response)
            standard_gemini_response = google_api_response.get("response")
            return Response(
                content=json.dumps(standard_gemini_response),
                status_code=200,
                media_type="application/json; charset=utf-8"
            )
        except (json.JSONDecodeError, AttributeError) as e:
            logging.error(f"Failed to parse Google API response: {str(e)}")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("Content-Type")
            )
    else:
        # Log the error details
        logging.error(f"Google API returned status {resp.status_code}: {resp.text}")
        
        # Try to parse error response and provide meaningful error message
        try:
            error_data = resp.json()
            if "error" in error_data:
                error_message = error_data["error"].get("message", f"API error: {resp.status_code}")
                error_response = {
                    "error": {
                        "message": error_message,
                        "type": "invalid_request_error" if resp.status_code == 404 else "api_error",
                        "code": resp.status_code
                    }
                }
                return Response(
                    content=json.dumps(error_response),
                    status_code=resp.status_code,
                    media_type="application/json"
                )
        except (json.JSONDecodeError, KeyError):
            pass
        
        # Fallback to original response if we can't parse the error
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("Content-Type")
        )

def build_gemini_payload_from_openai(openai_payload: dict) -> dict:
    """
    Build a Gemini API payload from an OpenAI-transformed request.
    This is used when OpenAI requests are converted to Gemini format.
    """
    # Extract model from the payload
    model = openai_payload.get("model")
    
    # Get safety settings or use defaults
    safety_settings = openai_payload.get("safetySettings", DEFAULT_SAFETY_SETTINGS)
    
    # Build the request portion
    request_data = {
        "contents": openai_payload.get("contents"),
        "systemInstruction": openai_payload.get("systemInstruction"),
        "cachedContent": openai_payload.get("cachedContent"),
        "tools": openai_payload.get("tools"),
        "toolConfig": openai_payload.get("toolConfig"),
        "safetySettings": safety_settings,
        "generationConfig": openai_payload.get("generationConfig", {}),
    }
    
    # Remove any keys with None values
    request_data = {k: v for k, v in request_data.items() if v is not None}
    
    return {
        "model": model,
        "request": request_data
    }

def build_gemini_payload_from_native(native_request: dict, model_from_path: str) -> dict:
    """
    Build a Gemini API payload from a native Gemini request.
    This is used for direct Gemini API calls.
    """
    native_request["safetySettings"] = DEFAULT_SAFETY_SETTINGS
    
    if "generationConfig" not in native_request:
        native_request["generationConfig"] = {}

    if "thinkingConfig" not in native_request["generationConfig"]:
        native_request["generationConfig"]["thinkingConfig"] = {}
    
    # Configure thinking based on model variant
    thinking_budget = get_thinking_budget(model_from_path)
    include_thoughts = should_include_thoughts(model_from_path)
    
    native_request["generationConfig"]["thinkingConfig"]["includeThoughts"] = include_thoughts
    native_request["generationConfig"]["thinkingConfig"]["thinkingBudget"] = thinking_budget
    
    # Add Google Search grounding for search models
    if is_search_model(model_from_path):
        if "tools" not in native_request:
            native_request["tools"] = []
        # Add googleSearch tool if not already present
        if not any(tool.get("googleSearch") for tool in native_request["tools"]):
            native_request["tools"].append({"googleSearch": {}})
    
    return {
        "model": get_base_model_name(model_from_path),  # Use base model name for API call
        "request": native_request
    }