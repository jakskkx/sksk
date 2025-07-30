
# src/auth.py (v3 - 实现混合加载模式)

import os
import json
import base64
import time
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBasic

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleAuthRequest

from .utils import get_user_agent, get_client_metadata
from .config import (
    CLIENT_ID, CLIENT_SECRET, SCOPES, CREDENTIAL_FILE,
    CODE_ASSIST_ENDPOINT, GEMINI_AUTH_PASSWORD
)

# --- Global State ---
credential_pool = []
current_credential_index = 0
round_robin_lock = threading.Lock()
LOADED_PROJECT_IDS = []
# 【新】用于存储最终检测到的加载模式
CREDENTIAL_MODE = "未配置"
user_project_id = None
onboarding_complete = False

security = HTTPBasic()

# ... 此处到 load_credentials_pool 函数之间的所有函数都保持不变 ...
# ... 为了简洁，这里省略，请务必保留您文件中的原始内容 ...
class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        code = query_components.get("code", [None])[0]
        if code:
            _OAuthCallbackHandler.auth_code = code
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>OAuth authentication successful!</h1><p>You can close this window. Please check the proxy server logs to verify that onboarding completed successfully. No need to restart the proxy.</p>")
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authentication failed.</h1><p>Please try again.</p>")

def authenticate_user(request: Request):
    api_key = request.query_params.get("key")
    if api_key and api_key == GEMINI_AUTH_PASSWORD: return "api_key_user"
    goog_api_key = request.headers.get("x-goog-api-key", "")
    if goog_api_key and goog_api_key == GEMINI_AUTH_PASSWORD: return "goog_api_key_user"
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if bearer_token == GEMINI_AUTH_PASSWORD: return "bearer_user"
    if auth_header.startswith("Basic "):
        try:
            encoded_credentials = auth_header[6:]
            decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8', "ignore")
            username, password = decoded_credentials.split(':', 1)
            if password == GEMINI_AUTH_PASSWORD: return username
        except Exception: pass
    raise HTTPException(status_code=401, detail="Invalid authentication credentials.", headers={"WWW-Authenticate": "Basic"})

def save_credentials(creds, project_id=None):
    creds_data={"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"token":creds.token,"refresh_token":creds.refresh_token,"scopes":creds.scopes if creds.scopes else SCOPES,"token_uri":"https://oauth2.googleapis.com/token",}
    if creds.expiry:
        from datetime import timezone
        expiry_utc = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry.tzinfo is None else creds.expiry
        creds_data["expiry"] = expiry_utc.isoformat()
    if project_id: creds_data["project_id"] = project_id
    with open(CREDENTIAL_FILE, "w") as f: json.dump(creds_data, f, indent=2)
    logging.info(f"凭证已保存至 {CREDENTIAL_FILE}")

def _create_credential_from_dict(raw_creds_data: dict, source_name: str) -> Credentials | None:
    try:
        if "refresh_token" not in raw_creds_data or not raw_creds_data["refresh_token"]:
            logging.warning(f"跳过凭证 {source_name}：缺少 refresh_token。")
            return None
        creds_data = raw_creds_data.copy()
        if "access_token" in creds_data and "token" not in creds_data: creds_data["token"] = creds_data["access_token"]
        if "scope" in creds_data and "scopes" not in creds_data: creds_data["scopes"] = creds_data["scope"].split()
        if "expiry" in creds_data:
            expiry_str = creds_data.get("expiry")
            if isinstance(expiry_str, str) and ("+00:00" in expiry_str or "Z" in expiry_str):
                try:
                    parsed_expiry = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
                    creds_data["expiry"] = parsed_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    logging.warning(f"无法解析过期时间格式 '{expiry_str}'，将移除该字段。")
                    del creds_data["expiry"]
        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        project_id = creds_data.get("project_id")
        if project_id:
            creds.project_id = project_id
            logging.info(f"从 {source_name} 提取 project_id: {project_id}")
            if project_id not in LOADED_PROJECT_IDS: LOADED_PROJECT_IDS.append(project_id)
        if not creds.token or (creds.expired and creds.refresh_token):
            try:
                logging.info(f"来自 {source_name} 的凭证需要刷新，正在尝试...")
                creds.refresh(GoogleAuthRequest())
                logging.info(f"来自 {source_name} 的凭证刷新成功。")
            except Exception as e:
                logging.error(f"刷新凭证 {source_name} 失败: {e}。该凭证可能无法工作。")
        return creds
    except Exception as e:
        logging.error(f"从 {source_name} 创建凭证对象失败: {e}")
        return None


# --- 【核心重构】凭证加载函数 (v4.1 - 模式检测更完善) ---


def load_credentials_pool(allow_oauth_flow=True):
    """
    【最终健壮版】加载凭证池，并直接返回检测结果（模式和ProjectID列表），
    以实现最可靠的状态传递。
    """
    global credential_pool, user_project_id, CREDENTIAL_MODE
    
    LOADED_PROJECT_IDS.clear()
    creds_list = []
    
    # --- 1. 优先从环境变量加载 ---
    is_array_mode_used, is_independent_mode_used = False, False

    # 数组模式
    env_creds_json = os.getenv("GEMINI_CREDENTIALS")
    if env_creds_json:
        try:
            creds_data_source = json.loads(env_creds_json)
            if isinstance(creds_data_source, dict): creds_data_source = [creds_data_source]
            if isinstance(creds_data_source, list):
                is_array_mode_used = True
                logging.info("检测到 '数组/单体模式' 凭证...")
                for d in creds_data_source:
                    if creds := _create_credential_from_dict(d, "数组/单体源"):
                        creds_list.append(creds)
        except json.JSONDecodeError: logging.warning("无法解析 GEMINI_CREDENTIALS, 已忽略。")

    # 独立模式
    i = 1
    while True:
        if cred_json_str := os.getenv(f"GEMINI_CREDENTIALS{i}"):
            is_independent_mode_used = True
            logging.info(f"检测到 '独立模式' 凭证 GEMINI_CREDENTIALS{i}...")
            try:
                if creds := _create_credential_from_dict(json.loads(cred_json_str), f"独立模式源 {i}"):
                    creds_list.append(creds)
            except json.JSONDecodeError as e: logging.error(f"解析 GEMINI_CREDENTIALS{i} 失败: {e}")
            i += 1
        else: break
    
    # --- 2. 回退到文件 ---
    if not creds_list and os.path.exists(CREDENTIAL_FILE):
        logging.info(f"未从环境变量加载, 回退至文件: {CREDENTIAL_FILE}")
        try:
            with open(CREDENTIAL_FILE, "r") as f:
                if creds := _create_credential_from_dict(json.load(f), f"文件源 ({CREDENTIAL_FILE})"):
                    creds_list.append(creds)
        except Exception as e: logging.error(f"读取凭证文件 {CREDENTIAL_FILE} 失败: {e}")

    # --- 3. 最终确定模式 ---
    if is_array_mode_used and is_independent_mode_used: final_mode = "混合模式"
    elif is_array_mode_used: final_mode = "数组模式"
    elif is_independent_mode_used: final_mode = "独立模式"
    elif creds_list: final_mode = "文件模式"
    else: final_mode = "未配置"
    
    # --- 4. 处理交互式授权 ---
    if not creds_list and allow_oauth_flow:
        logging.info("未找到有效凭证。启动交互式 OAuth 授权流程...")
        if creds := _run_oauth_flow():
            save_credentials(creds)
            # 递归调用以重新加载，并直接返回其结果
            return load_credentials_pool(allow_oauth_flow=False) 
            
    # --- 5. 更新全局状态并返回最终结果【核心修改】 ---
    credential_pool = creds_list
    CREDENTIAL_MODE = final_mode # 仍然设置全局变量，以备其他模块使用
    
    if credential_pool:
        user_project_id = getattr(credential_pool[0], 'project_id', None)
        logging.info(f"成功加载 {len(credential_pool)} 个凭证。最终模式: '{final_mode}'。")
    else:
        # 如果最终列表为空，确保模式是“未配置”
        CREDENTIAL_MODE = "未配置"
        logging.warning("警告: 凭证池为空。API 调用将会失败。")

    # 直接返回检测到的模式和 project_id 列表，不再依赖全局变量传递
    return CREDENTIAL_MODE, LOADED_PROJECT_IDS

        
# ... 此处 get_next_credential 和 _run_oauth_flow 函数保持不变 ...
def get_next_credential() -> Credentials | None:
    global current_credential_index
    with round_robin_lock:
        if not credential_pool:
            logging.error("凭证池为空。无法获取凭证。")
            return None
        index = current_credential_index
        creds = credential_pool[index]
        current_credential_index = (index + 1) % len(credential_pool)
    if creds.expired and creds.refresh_token:
        try:
            source_info = getattr(creds, 'project_id', f'索引 {index}')
            logging.info(f"凭证 '{source_info}' 已过期，正在后台刷新...")
            creds.refresh(GoogleAuthRequest())
            logging.info(f"凭证 '{source_info}' 刷新成功。")
        except Exception as e:
            logging.error(f"刷新凭证 '{source_info}' 失败: {e}")
    return creds

# 在 src/auth.py 中找到并替换此函数

def _run_oauth_flow():
    # 【核心修正】不再硬编码8080端口，使其可配置且默认值更安全
    OAUTH_CALLBACK_PORT = int(os.getenv("OAUTH_CALLBACK_PORT", 8085))
    redirect_uri = f"http://localhost:{OAUTH_CALLBACK_PORT}"

    client_config = {"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",}}
    
    # 使用更新后的 redirect_uri
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes='true')
    print(f"\n{'='*80}\n需要进行身份验证\n{'='*80}")
    print(f"请在浏览器中打开以下网址以进行登录:\n{auth_url}")
    print(f"{'='*80}\n")
    logging.info(f"请在浏览器中打开以下网址以进行登录: {auth_url}")
    
    try:
        # 使用更新后的端口启动临时服务器
        server = HTTPServer(("", OAUTH_CALLBACK_PORT), _OAuthCallbackHandler)
        server.handle_request()
        auth_code = _OAuthCallbackHandler.auth_code
    except OSError as e:
        if e.errno == 98: # Address already in use
            logging.error(f"❌ OAuth回调端口 {OAUTH_CALLBACK_PORT} 已被占用。")
            logging.error(f"   请检查是否有其他程序占用了此端口，或在 .env 文件中设置 OAUTH_CALLBACK_PORT 为其他端口。")
        else:
            logging.error(f"启动临时HTTP服务器失败: {e}")
        return None # 优雅地退出
    except Exception as e:
        logging.error(f"处理OAuth回调时发生未知错误: {e}")
        return None

    if not auth_code:
        logging.error("从 OAuth 回调中获取授权码失败。")
        return None
        
    try:
        flow.fetch_token(code=auth_code)
        logging.info("成功获取 OAuth 令牌。")
        return flow.credentials
    except Exception as e:
        logging.error(f"使用授权码获取令牌失败: {e}")
        return None