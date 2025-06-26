import os
import secrets
import json
import base64
from fastapi import Request, HTTPException, Form
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
import httpx
from dotenv import load_dotenv
from integrations.integration_item import IntegrationItem
from datetime import datetime

# Load environment variables
load_dotenv()
CLIENT_ID = os.getenv("HUBSPOT_CLIENT_ID")
CLIENT_SECRET = os.getenv("HUBSPOT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("HUBSPOT_REDIRECT_URI")
SCOPE = "crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read oauth"

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

async def authorize_hubspot(user_id, org_id):
    state_data = {
        "state": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()
    await redis_client.add_key_value_redis(f"hubspot_state:{org_id}:{user_id}", encoded_state, expire=600)
    auth_url = (
        f"https://app.hubspot.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPE}"
        f"&state={encoded_state}"
    )
    return auth_url

async def oauth2callback_hubspot(request: Request):
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

    # In a real app, validate the state from Redis here
    # stored_state = await redis_client.get_value_redis(f"hubspot_state:{state_data['org_id']}:{state_data['user_id']}")
    # if not stored_state or stored_state != encoded_state:
    #     raise HTTPException(status_code=400, detail="State does not match")

    token_url = "https://api.hubspot.com/oauth/v1/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to exchange code for tokens")
        token_info = response.json()

    # Store tokens in Redis
    await redis_client.add_key_value_redis(
        f"hubspot_tokens:{state_data['org_id']}:{state_data['user_id']}", 
        json.dumps(token_info)
    )
    return HTMLResponse(content="""
  <html>
    <head>
      <script>
        window.opener.postMessage("hubspot-auth-success", "*");
        window.close();
      </script>
    </head>
    <body>
      <p>HubSpot authentication successful. You can close this window.</p>
    </body>
  </html>
""")

async def get_hubspot_credentials(user_id, org_id):
    credentials_str = await redis_client.get_value_redis(f"hubspot_tokens:{org_id}:{user_id}")
    if not credentials_str:
        raise HTTPException(status_code=401, detail="No credentials found")
    credentials = json.loads(credentials_str)
    return credentials

def create_integration_item_metadata_object(response_json, item_type="contact"):
    """Creates an IntegrationItem object from HubSpot API response"""
    
    # Extract common properties
    item_id = response_json.get("id")
    properties = response_json.get("properties", {})
    
    # Create name based on item type
    if item_type == "contact":
        first_name = properties.get("firstname", "")
        last_name = properties.get("lastname", "")
        name = f"{first_name} {last_name}".strip() or properties.get("email", "Unknown Contact")
    elif item_type == "company":
        name = properties.get("name", "Unknown Company")
    elif item_type == "deal":
        name = properties.get("dealname", "Unknown Deal")
    else:
        name = "Unknown Item"
    
    # Parse timestamps
    creation_time = None
    last_modified_time = None
    
    if properties.get("createdate"):
        try:
            creation_time = datetime.fromtimestamp(int(properties["createdate"]) / 1000)
        except (ValueError, TypeError):
            pass
    
    if properties.get("hs_lastmodifieddate"):
        try:
            last_modified_time = datetime.fromtimestamp(int(properties["hs_lastmodifieddate"]) / 1000)
        except (ValueError, TypeError):
            pass
    
    # Create IntegrationItem
    integration_item = IntegrationItem(
        id=str(item_id),
        type=item_type,
        name=name,
        creation_time=creation_time,
        last_modified_time=last_modified_time,
        url=f"https://app.hubspot.com/contacts/{item_id}" if item_type == "contact" else None,
        visibility=True
    )
    
    return integration_item

async def get_items_hubspot(credentials):
    """Aggregates all metadata relevant for a HubSpot integration"""
    if isinstance(credentials, str):
        credentials = json.loads(credentials)
    
    access_token = credentials.get("access_token")
    if not access_token:
        print("No access token found in credentials")
        return []
        
    list_of_integration_item_metadata = []
    
    # Fetch contacts
    try:
        contacts_url = "https://api.hubapi.com/crm/v3/objects/contacts"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                contacts_url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"limit": 100}  # Limit to 100 contacts for testing
            )
            
            if response.status_code == 200:
                data = response.json()
                for contact in data.get("results", []):
                    integration_item = create_integration_item_metadata_object(contact, "contact")
                    list_of_integration_item_metadata.append(integration_item)
                print(f"Loaded {len(data.get('results', []))} contacts")
            else:
                print(f"Failed to fetch contacts: {response.status_code}")
    except Exception as e:
        print(f"Error fetching contacts: {str(e)}")
    
    # Fetch companies
    try:
        companies_url = "https://api.hubapi.com/crm/v3/objects/companies"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                companies_url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"limit": 50}  # Limit to 50 companies for testing
            )
            
            if response.status_code == 200:
                data = response.json()
                for company in data.get("results", []):
                    integration_item = create_integration_item_metadata_object(company, "company")
                    list_of_integration_item_metadata.append(integration_item)
                print(f"Loaded {len(data.get('results', []))} companies")
            else:
                print(f"Failed to fetch companies: {response.status_code}")
    except Exception as e:
        print(f"Error fetching companies: {str(e)}")
    
    try:
        deals_url = "https://api.hubapi.com/crm/v3/objects/deals"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                deals_url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"limit": 50}  
            )
            
            if response.status_code == 200:
                data = response.json()
                for deal in data.get("results", []):
                    integration_item = create_integration_item_metadata_object(deal, "deal")
                    list_of_integration_item_metadata.append(integration_item)
                print(f"Loaded {len(data.get('results', []))} deals")
            else:
                print(f"Failed to fetch deals: {response.status_code}")
    except Exception as e:
        print(f"Error fetching deals: {str(e)}")
    
    print(f"Total HubSpot items loaded: {len(list_of_integration_item_metadata)}")
    print("HubSpot Integration Items:")
    for item in list_of_integration_item_metadata:
        print(f"  - {item.type}: {item.name} (ID: {item.id})")
    
    return list_of_integration_item_metadata
