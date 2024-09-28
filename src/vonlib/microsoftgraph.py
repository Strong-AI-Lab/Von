import msal
import requests
import os

# Define the necessary parameters
client_id = 'YOUR_CLIENT_ID'
client_secret = 'YOUR_CLIENT_SECRET'
tenant_id = 'YOUR_TENANT_ID'
authority = f'https://login.microsoftonline.com/{tenant_id}'
scope = ['https://graph.microsoft.com/.default']
username = os.getenv('MS_GRAPH_USERNAME')
password = os.getenv('MS_GRAPH_PASSWORD')

# Check if username and password are available
if not username or not password:
    raise ValueError("Username or password environment variables not set")

# Create a confidential client application
app = msal.ConfidentialClientApplication(
    client_id,
    authority=authority,
    client_credential=client_secret
)

# Acquire a token
result = app.acquire_token_by_username_password(
    username=username,
    password=password,
    scopes=scope
)

if 'access_token' in result:
    access_token = result['access_token']
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    # Make a request to the Microsoft Graph API to list calendars
    response = requests.get('https://graph.microsoft.com/v1.0/me/calendars', headers=headers)

    if response.status_code == 200:
        calendars = response.json()
        print("Calendars:", calendars)
    else:
        print(f"Error: {response.status_code}")
        print(response.json())
else:
    print("Error acquiring token:")
    print(result.get('error'))
    print(result.get('error_description'))