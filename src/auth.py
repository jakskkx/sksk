import os
import json
import base64
import time
import logging
import threading
from datetime import datetime
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBasic
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleAuthRequest

from .utils import get_user_agent, get_client_metadata
from .config import (
    CLIENT_ID, CLIENT_SECRET, SCOPES, CREDENTIAL_FILE,
    CODE_ASSIST_ENDPOINT, GEMINI_AUTH_PASSWORD
)

# --- Global State for Credential Pooling ---
credential_pool = []
current_credential_index = 0
round_robin_lock = threading.Lock()
# --- Legacy Global State ---
user_project_id = None
onboarding_complete = False

security = HTTPBasic()

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
    """Authenticate the user with multiple methods."""
    # Check for API key in query parameters first (for Gemini client compatibility)
    api_key = request.query_params.get("key")
    if api_key and api_key == GEMINI_AUTH_PASSWORD:
        return "api_key_user"
    
    # Check for API key in x-goog-api-key header (Google SDK format)
    goog_api_key = request.headers.get("x-goog-api-key", "")
    if goog_api_key and goog_api_key == GEMINI_AUTH_PASSWORD:
        return "goog_api_key_user"
    
    # Check for API key in Authorization header (Bearer token format)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if bearer_token == GEMINI_AUTH_PASSWORD:
            return "bearer_user"
    
    # Check for HTTP Basic Authentication
    if auth_header.startswith("Basic "):
        try:
            encoded_credentials = auth_header[6:]
            decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8', "ignore")
            username, password = decoded_credentials.split(':', 1)
            if password == GEMINI_AUTH_PASSWORD:
                return username
        except Exception:
            pass
    
    # If none of the authentication methods work
    raise HTTPException(
        status_code=401,
        detail="Invalid authentication credentials. Use HTTP Basic Auth, Bearer token, 'key' query parameter, or 'x-goog-api-key' header.",
        headers={"WWW-Authenticate": "Basic"},
    )

def save_credentials(creds, project_id=None):
    """Saves a single credential object to the default credential file."""
    # This function is primarily for the initial OAuth flow.
    creds_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "scopes": creds.scopes if creds.scopes else SCOPES,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    
    if creds.expiry:
        if creds.expiry.tzinfo is None:
            from datetime import timezone
            expiry_utc = creds.expiry.replace(tzinfo=timezone.utc)
        else:
            expiry_utc = creds.expiry
        creds_data["expiry"] = expiry_utc.isoformat()
    
    if project_id:
        creds_data["project_id"] = project_id
    
    with open(CREDENTIAL_FILE, "w") as f:
        json.dump(creds_data, f, indent=2)
    logging.info(f"Credentials saved to {CREDENTIAL_FILE}")

def _create_credential_from_dict(raw_creds_data: dict, source_name: str) -> Credentials | None:
    """Helper to create and refresh a Credentials object from a dictionary."""
    try:
        # SAFEGUARD: Ensure a refresh token exists to load successfully.
        if "refresh_token" not in raw_creds_data or not raw_creds_data["refresh_token"]:
            logging.warning(f"Skipping credential from {source_name}: missing refresh_token.")
            return None

        creds_data = raw_creds_data.copy()
        
        # Handle format variations
        if "access_token" in creds_data and "token" not in creds_data:
            creds_data["token"] = creds_data["access_token"]
        if "scope" in creds_data and "scopes" not in creds_data:
            creds_data["scopes"] = creds_data["scope"].split()

        # Handle expiry format issues
        if "expiry" in creds_data:
            expiry_str = creds_data.get("expiry")
            if isinstance(expiry_str, str) and ("+00:00" in expiry_str or "Z" in expiry_str):
                try:
                    parsed_expiry = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
                    creds_data["expiry"] = parsed_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    logging.warning(f"Could not parse expiry format '{expiry_str}', removing field.")
                    del creds_data["expiry"]

        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        
        # Extract project_id if available
        project_id = creds_data.get("project_id")
        if project_id:
            # Attach project_id to the credentials object for later retrieval
            creds.project_id = project_id
            logging.info(f"Extracted project_id from {source_name}: {project_id}")

        # Refresh immediately if expired or no token is present
        if not creds.token or (creds.expired and creds.refresh_token):
            try:
                logging.info(f"Credential from {source_name} requires refresh. Attempting...")
                creds.refresh(GoogleAuthRequest())
                logging.info(f"Credential from {source_name} refreshed successfully.")
            except Exception as e:
                logging.error(f"Failed to refresh credential from {source_name}: {e}. It may not work.")
        
        return creds

    except Exception as e:
        logging.error(f"Failed to create credential object from {source_name}: {e}")
        return None

def load_credentials_pool(allow_oauth_flow=True):
    """Loads all credentials from environment or file into a pool."""
    global credential_pool, user_project_id
    
    creds_list = []
    
    # 1. Prioritize numbered GEMINI_CREDENTIALS{i} environment variables
    # This supports multi-line JSON values.
    numbered_creds_found = False
    i = 1
    while True:
        env_var_name = f"GEMINI_CREDENTIALS{i}"
        cred_json_str = os.getenv(env_var_name)
        if cred_json_str is None:
            break
        
        numbered_creds_found = True
        try:
            # json.loads can handle multi-line strings directly
            cred_data = json.loads(cred_json_str)
            logging.info(f"Found {env_var_name}. Processing...")
            creds = _create_credential_from_dict(cred_data, f"env ({env_var_name})")
            if creds:
                # Attach metadata for logging
                creds.credential_source_type = 'numbered'
                creds.credential_source_index = i
                creds_list.append(creds)
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse JSON from {env_var_name}: {e}")
        
        i += 1
    
    # 2. If no numbered creds are found, check for the single GEMINI_CREDENTIALS array
    if not numbered_creds_found:
        env_creds_json_array = os.getenv("GEMINI_CREDENTIALS")
        if env_creds_json_array:
            try:
                creds_data_list = json.loads(env_creds_json_array)
                if not isinstance(creds_data_list, list):
                    creds_data_list = [creds_data_list]
                
                logging.info(f"Found {len(creds_data_list)} credential(s) in GEMINI_CREDENTIALS array.")
                for idx, cred_data in enumerate(creds_data_list):
                    creds = _create_credential_from_dict(cred_data, f"env (array, index {idx})")
                    if creds:
                         # Attach metadata for logging
                        creds.credential_source_type = 'array_or_file'
                        creds_list.append(creds)
            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse GEMINI_CREDENTIALS JSON array: {e}. Falling back to file.")

    # 3. Fallback to credential file if no env vars are used
    if not creds_list and os.path.exists(CREDENTIAL_FILE):
        try:
            with open(CREDENTIAL_FILE, "r") as f:
                cred_data = json.load(f)
            logging.info(f"Loading credential from file: {CREDENTIAL_FILE}")
            creds = _create_credential_from_dict(cred_data, f"file ({CREDENTIAL_FILE})")
            if creds:
                creds.credential_source_type = 'array_or_file'
                creds_list.append(creds)
        except Exception as e:
            logging.error(f"Failed to read or process credentials file {CREDENTIAL_FILE}: {e}")

    # 4. If no credentials found, trigger interactive OAuth flow if allowed
    if not creds_list and allow_oauth_flow:
        logging.info("No valid credentials found. Starting interactive OAuth flow.")
        creds = _run_oauth_flow()
        if creds:
            save_credentials(creds)
            creds.credential_source_type = 'oauth_flow'
            creds_list.append(creds)

    credential_pool = creds_list
    if credential_pool:
        first_creds = credential_pool[0]
        user_project_id = getattr(first_creds, 'project_id', None)
        logging.info(f"Successfully loaded {len(credential_pool)} credential(s). Pool is ready.")
    else:
        logging.warning("Credential pool is empty. API calls will fail until credentials are provided.")

def get_next_credential() -> Credentials | None:
    """Gets the next available credential from the pool in a round-robin fashion."""
    global current_credential_index
    
    with round_robin_lock:
        if not credential_pool:
            logging.error("Credential pool is empty. Cannot get a credential.")
            return None

        index = current_credential_index
        creds = credential_pool[index]
        
        current_credential_index = (index + 1) % len(credential_pool)

    if creds.expired and creds.refresh_token:
        try:
            source_type = getattr(creds, 'credential_source_type', 'unknown')
            source_index = getattr(creds, 'credential_source_index', index)
            logging.info(f"Credential from source '{source_type}' index {source_index} is expired. Refreshing...")
            creds.refresh(GoogleAuthRequest())
            logging.info(f"Credential refreshed successfully.")
        except Exception as e:
            logging.error(f"Failed to refresh credential: {e}")

    return creds

def _run_oauth_flow():
    """Runs the interactive command-line OAuth flow."""
    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri="http://localhost:8080"
    )
    
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes='true'
    )
    print(f"\n{'='*80}\nAUTHENTICATION REQUIRED\n{'='*80}")
    print(f"Please open this URL in your browser to log in:\n{auth_url}")
    print(f"{'='*80}\n")
    logging.info(f"Please open this URL in your browser to log in: {auth_url}")
    
    server = HTTPServer(("", 8080), _OAuthCallbackHandler)
    server.handle_request()
    
    auth_code = _OAuthCallbackHandler.auth_code
    if not auth_code:
        logging.error("Failed to get authorization code from OAuth callback.")
        return None

    try:
        flow.fetch_token(code=auth_code)
        logging.info("Successfully fetched OAuth token.")
        return flow.credentials
    except Exception as e:
        logging.error(f"Failed to fetch token with auth code: {e}")
        return None