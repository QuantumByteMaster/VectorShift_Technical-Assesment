# notion.py

import json
import secrets
import os
import base64
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
from dotenv import load_dotenv
from integrations.integration_item import IntegrationItem
from datetime import datetime

load_dotenv()
CLIENT_ID = os.getenv("NOTION_CLIENT_ID", "your_notion_client_id_here")
CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET", "your_notion_client_secret_here")
REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI", "http://localhost:8000/integrations/notion/oauth2callback")
NOTION_VERSION = "2022-06-28"
SCOPE = ""  

authorization_url = (
    f"https://api.notion.com/v1/oauth/authorize?"
    f"client_id={CLIENT_ID}"
    f"&response_type=code"
    f"&owner=user"
    f"&redirect_uri={REDIRECT_URI}"
)


class MockRedisClient:
    def __init__(self):
        self.store = {}

    async def add_key_value_redis(self, key, value, expire=None):
        self.store[key] = value

    async def get_value_redis(self, key):
        return self.store.get(key)

    async def delete_key_redis(self, key):
        if key in self.store:
            del self.store[key]

# Use this for testing
redis_client = MockRedisClient()

def encode_state(user_id, org_id):
    state_data = {
        "state": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
    return encoded_state, state_data

async def authorize_notion(user_id, org_id):
    encoded_state, _ = encode_state(user_id, org_id)
    await redis_client.add_key_value_redis(f"notion_state:{org_id}:{user_id}", encoded_state, expire=600)
    auth_url = (
        f"https://api.notion.com/v1/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={encoded_state}"
    )
    return auth_url

async def oauth2callback_notion(request: Request):
    if request.query_params.get("error"):
        raise HTTPException(status_code=400, detail=request.query_params.get("error"))
    code = request.query_params.get("code")
    encoded_state = request.query_params.get("state")
    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    try:
        state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state")

    user_id = state_data.get("user_id")
    org_id = state_data.get("org_id")
    # Validate state
    stored_state = await redis_client.get_value_redis(f"notion_state:{org_id}:{user_id}")
    if not stored_state or stored_state != encoded_state:
        raise HTTPException(status_code=400, detail="State does not match")

    token_url = "https://api.notion.com/v1/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, json=data, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to exchange code for tokens")
        token_info = response.json()

    # Store tokens in Redis
    await redis_client.add_key_value_redis(
        f"notion_tokens:{org_id}:{user_id}",
        json.dumps(token_info)
    )
    await redis_client.delete_key_redis(f"notion_state:{org_id}:{user_id}")
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_notion_credentials(user_id, org_id):
    credentials = await redis_client.get_value_redis(f"notion_tokens:{org_id}:{user_id}")
    if not credentials:
        return None
    return json.loads(credentials)

def create_integration_item_metadata_object(response_json):
    """Creates an IntegrationItem object from Notion API response"""
    item_id = response_json.get("id")
    obj_type = response_json.get("object", "unknown")
    name = None
    # Try to extract a title or name property
    if "properties" in response_json:
        for prop in response_json["properties"].values():
            if prop.get("type") == "title":
                title_arr = prop.get("title", [])
                if title_arr and isinstance(title_arr, list):
                    name = title_arr[0].get("plain_text")
                    break
    if not name:
        name = obj_type.capitalize() + " Item"
    # Parse timestamps
    creation_time = response_json.get("created_time")
    last_edited_time = response_json.get("last_edited_time")
    try:
        creation_time = datetime.fromisoformat(creation_time.replace("Z", "+00:00")) if creation_time else None
    except Exception:
        creation_time = None
    try:
        last_edited_time = datetime.fromisoformat(last_edited_time.replace("Z", "+00:00")) if last_edited_time else None
    except Exception:
        last_edited_time = None
    integration_item = IntegrationItem(
        id=str(item_id),
        type=obj_type,
        name=name,
        creation_time=creation_time,
        last_modified_time=last_edited_time,
        url=None,
        visibility=True
    )
    return integration_item

async def get_items_notion(credentials):
    """Aggregates all metadata relevant for a Notion integration"""
    if isinstance(credentials, str):
        credentials = json.loads(credentials)
    access_token = credentials.get("access_token")
    if not access_token:
        print("No access token found in credentials")
        return []
    list_of_integration_item_metadata = []
    search_url = "https://api.notion.com/v1/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Notion-Version": NOTION_VERSION,
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(search_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            for result in data.get("results", []):
                integration_item = create_integration_item_metadata_object(result)
                list_of_integration_item_metadata.append(integration_item)
            print(f"Loaded {len(data.get('results', []))} Notion items")
        else:
            print(f"Failed to fetch Notion items: {response.status_code}")
    print(f"Total Notion items loaded: {len(list_of_integration_item_metadata)}")
    for item in list_of_integration_item_metadata:
        print(f"  - {item.type}: {item.name} (ID: {item.id})")
    return list_of_integration_item_metadata
