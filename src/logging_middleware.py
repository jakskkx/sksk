# src/logging_middleware.py (v2 - 动态美化版)

import time
import logging
from datetime import datetime
import pytz
from http import HTTPStatus

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from rich.console import Console

# --- Timezone & Console ---
beijing_tz = pytz.timezone('Asia/Shanghai')
console = Console()

class CustomLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        
        # 仅对核心API端点应用特殊日志逻辑
        if "/chat/completions" in request.url.path:
            # 使用 rich.status 实现动态旋转的进度条
            with console.status(f"[cyan]🌀 来自 {request.client.host} 的请求正在处理...", spinner="earth"):
                response = await call_next(request)
            
            # 请求处理完毕后（进度条自动消失），记录最终状态
            self._log_final_status(request, response)
            return response
        else:
            # 对于其他请求（如/models, /），不打印日志，直接处理
            return await call_next(request)

    def _log_final_status(self, request: Request, response: Response):
        """处理完请求后，格式化并记录最终结果。"""
        
        project_id = getattr(request.state, 'used_project_id', 'N/A')
        
        status_code = response.status_code
        try:
            status_phrase = HTTPStatus(status_code).phrase
        except ValueError:
            status_phrase = "Unknown"
        
        status_line = f"({status_code} {status_phrase})"

        if 200 <= status_code < 400:
            status_colored = f"[green]✅ 成功 {status_line}[/green]"
        else:
           status_colored = f"[red]❌ 失败 {status_line}[/red]"
        
        timestamp = datetime.now(beijing_tz).strftime('%Y-%m-%d %H:%M:%S')

        console.print(
            f"[yellow]{project_id}[/yellow] - "
            f"{status_colored} - "
            f"[blue][{timestamp}][/blue]"
        )
