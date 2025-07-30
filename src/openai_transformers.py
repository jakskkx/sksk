# src/openai_transformers.py (正确版本 - 负责格式转换)

import time
import uuid
from typing import Dict, Any, List

from .models import (
    OpenAIChatCompletionRequest, OpenAIChatCompletionResponse, OpenAIChatCompletionChoice,
    OpenAIChatCompletionStreamResponse, OpenAIChatCompletionStreamChoice, OpenAIChatMessage, OpenAIDelta
)
from .config import (
    DEFAULT_SAFETY_SETTINGS,
    get_base_model_name,
    is_search_model,
    get_thinking_budget,
    should_include_thoughts
)


def openai_request_to_gemini(openai_request: OpenAIChatCompletionRequest) -> Dict[str, Any]:
    """将OpenAI格式的请求转换为Gemini格式的请求字典"""
    contents = []
    messages_copy = [msg.copy(deep=True) for msg in openai_request.messages]

    system_prompt_content = None
    if messages_copy and messages_copy[0].role == 'system':
        system_message = messages_copy.pop(0)
        system_prompt_content = system_message.content

    if system_prompt_content and messages_copy and messages_copy[0].role == 'user':
        first_user_message = messages_copy[0]
        if isinstance(first_user_message.content, str):
            first_user_message.content = f"{system_prompt_content}\n\n{first_user_message.content}"
        elif isinstance(first_user_message.content, list):
            # 将系统提示添加到多模态消息的文本部分
            text_part = next((part for part in first_user_message.content if part.get("type") == "text"), None)
            if text_part:
                text_part["text"] = f"{system_prompt_content}\n\n{text_part.get('text', '')}"
            else:
                first_user_message.content.insert(0, {"type": "text", "text": system_prompt_content})

    for message in messages_copy:
        role = "model" if message.role == "assistant" else "user"
        
        if isinstance(message.content, list):
            parts = []
            for part in message.content:
                if part.get("type") == "text":
                    parts.append({"text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    image_url_data = part.get("image_url", {}).get("url")
                    if image_url_data and ',' in image_url_data:
                        header, data = image_url_data.split(',', 1)
                        mime_type = header.split(';')[0].split(':')[1]
                        parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
            contents.append({"role": role, "parts": parts})
        else:
            contents.append({"role": role, "parts": [{"text": str(message.content)}]})

    # 构建生成配置
    generation_config = {}
    if openai_request.temperature is not None: generation_config["temperature"] = openai_request.temperature
    if openai_request.top_p is not None: generation_config["topP"] = openai_request.top_p
    if openai_request.max_tokens is not None: generation_config["maxOutputTokens"] = openai_request.max_tokens
    if openai_request.stop: generation_config["stopSequences"] = [openai_request.stop] if isinstance(openai_request.stop, str) else openai_request.stop
    if openai_request.n is not None: generation_config["candidateCount"] = openai_request.n
    if openai_request.response_format and openai_request.response_format.get("type") == "json_object":
        generation_config["responseMimeType"] = "application/json"

    # 构建最终请求体
    request_payload = {
        "contents": contents,
        "generationConfig": generation_config,
        "safetySettings": DEFAULT_SAFETY_SETTINGS,
        "model": get_base_model_name(openai_request.model)
    }

    # 处理特殊模型变体
    if is_search_model(openai_request.model):
        request_payload.setdefault("tools", []).append({"googleSearch": {}})
    
    thinking_budget = get_thinking_budget(openai_request.model)
    if thinking_budget != -1:
        thinking_config = request_payload["generationConfig"].setdefault("thinkingConfig", {})
        thinking_config["thinkingBudget"] = thinking_budget
        thinking_config["includeThoughts"] = should_include_thoughts(openai_request.model)
    
    return request_payload

def gemini_response_to_openai(gemini_response: Dict[str, Any], model: str) -> OpenAIChatCompletionResponse:
    """将Gemini的聚合响应转换为OpenAI格式的Pydantic模型"""
    choices = []
    for candidate in gemini_response.get("candidates", []):
        parts = candidate.get("content", {}).get("parts", [])
        content_text = "".join(part.get("text", "") for part in parts)
        message = OpenAIChatMessage(role="assistant", content=content_text)
        choices.append(OpenAIChatCompletionChoice(
            index=candidate.get("index", 0),
            message=message,
            finish_reason=_map_finish_reason(candidate.get("finishReason")),
        ))
    return OpenAIChatCompletionResponse(
        id="chatcmpl-" + str(uuid.uuid4()), object="chat.completion",
        created=int(time.time()), model=model, choices=choices,
    )

def gemini_stream_chunk_to_openai(gemini_chunk: Dict[str, Any], model: str, response_id: str) -> OpenAIChatCompletionStreamResponse:
    """将Gemini的流式块转换为OpenAI格式的Pydantic模型"""
    choices = []
    for candidate in gemini_chunk.get("candidates", []):
        parts = candidate.get("content", {}).get("parts", [])
        content_text = "".join(part.get("text", "") for part in parts)
        delta = OpenAIDelta(content=content_text if content_text else None)
        if delta.content or candidate.get("finishReason"):
            choices.append(OpenAIChatCompletionStreamChoice(
                index=candidate.get("index", 0),
                delta=delta,
                finish_reason=_map_finish_reason(candidate.get("finishReason")),
            ))
    return OpenAIChatCompletionStreamResponse(
        id=response_id, object="chat.completion.chunk",
        created=int(time.time()), model=model, choices=choices,
    )

def _map_finish_reason(gemini_reason: str) -> str:
    """映射Gemini的结束原因到OpenAI的格式"""
    return {"STOP": "stop", "MAX_TOKENS": "length"}.get(gemini_reason, "content_filter" if gemini_reason in ["SAFETY", "RECITATION"] else None)
