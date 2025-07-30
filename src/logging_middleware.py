# src/logging_middleware.py (已修改)

import time
import logging
from datetime import datetime
import pytz
from http import HTTPStatus
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

# --- ANSI Color Codes ---
class Colors:
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    ENDC = "\033[0m"

# --- Timezone ---
beijing_tz = pytz.timezone('Asia/Shanghai')

class CustomLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        
        try:
            # 日志记录在响应之后执行，以获取状态码和由API调用设置的状态变量
            self._log_request(request, response)
        except Exception as e:
            # 这是一个安全措施，防止日志中间件自身出错导致服务崩溃
            logging.error(f"Error within CustomLogMiddleware: {e}")
            
        return response

    def _log_request(self, request: Request, response: Response):
        """处理完请求后，格式化并记录请求详情。"""
        
        # 从请求状态中获取由 google_api_client.py 设置的 project_id
        project_id = getattr(request.state, 'used_project_id', None)
        
        # --- 核心变更：只记录实际使用了凭证的API请求 ---
        # 这样可以有效过滤掉OPTIONS预检请求、/health、/等非核心请求的日志。
        if not project_id:
            return

        cred_index = getattr(request.state, 'used_credential_index', None)
        cred_type = getattr(request.state, 'used_credential_type', None)

        # 为 "Numbered" 类型的凭证格式化日志前缀
        log_prefix = ""
        if cred_type == 'numbered' and cred_index is not None:
            log_prefix = f"[{cred_index}] "
        
        # 获取状态码和对应的HTTP原因短语 (例如 "OK", "Too Many Requests")
        status_code = response.status_code
        try:
            status_phrase = HTTPStatus(status_code).phrase
        except ValueError:
            status_phrase = "Unknown Status"
        
        status_line = f"({status_code} {status_phrase})"

        # 根据状态码为成功或失败信息着色
        if 200 <= status_code < 400: # 2xx 和 3xx 都视为“成功”类别
            status_colored = f"{Colors.GREEN}✅ 成功 {status_line}{Colors.ENDC}"
        else:
            status_colored = f"{Colors.RED}❌ 失败 {status_line}{Colors.ENDC}"
        
        # 获取北京时区的当前时间
        timestamp = datetime.now(beijing_tz).strftime('%Y-%m-%d %H:%M:%S')

        # 组装并打印最终的格式化日志消息
        logging.info(
            f"{log_prefix}{Colors.YELLOW}{project_id}{Colors.ENDC} - "
            f"{status_colored} - "
            f"{Colors.BLUE}[{timestamp}]{Colors.ENDC}"
        )
