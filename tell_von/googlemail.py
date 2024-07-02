import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

DRIVE_SCOPE = ['https://www.googleapis.com/auth/drive.file']
GMAIL_SCOPE = ['https://www.googleapis.com/auth/gmail.readonly']

def getGmailCredName():
    cred= os.getenv("VON_GOOGLE_GMAIL_CRED") 
    return cred
def getDriveCredName():
    cred= os.getenv("VON_GOOGLE_GDRIVE_CRED") 
    return cred

def get_service(api, scopes, token_file, creds_file):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, 'w') as token:
            token.write(creds.to_json())
    
    return build(api, 'v1', credentials=creds)

def get_drive_service():
    return get_service('drive', DRIVE_SCOPE, 'drive_token.json', getDriveCredName())

def get_gmail_service():
    return get_service('gmail', GMAIL_SCOPE, 'gmail_token.json', getGmailCredName())

def read_drive_files(drive_service):
    # Your Drive file reading code here
    pass

def read_gmail_messages(gmail_service, max_results=10):
    results = gmail_service.users().messages().list(userId='me', maxResults=max_results).execute()
    messages = results.get('messages', [])
    count=0
    for message in messages:
        msg = gmail_service.users().messages().get(userId='me', id=message['id']).execute()
        
        subject = ''
        sender = ''
        for header in msg['payload']['headers']:
            if header['name'] == 'Subject':
                subject = header['value']
            if header['name'] == 'From':
                sender = header['value']
        count+=1
        print(f"Subject: {subject}")
        print(f"From: {sender}")
        print(f"---------{count}-------------")

def main():
   # drive_service = get_drive_service()
    gmail_service = get_gmail_service()
    
    #read_drive_files(drive_service)
    read_gmail_messages(gmail_service)

if __name__ == '__main__':
    main()



############################################################################################################
# To access Google Drive as one user and Gmail as another, you'll need to manage two separate sets of credentials.


""" Key changes and considerations:

1. Separate scopes for Drive and Gmail.
2. Separate token and credentials files for each service.
3. A generalized `get_service` function that can be used for both Drive and Gmail.
4. Separate functions to get Drive and Gmail services.

To use this:

1. Create two projects in Google Cloud Console (or use two sets of credentials from the same project).
2. Download the credentials JSON file for each (rename them to `drive_credentials.json` and `gmail_credentials.json`).
3. Place these files in the same directory as your script.
4. Run the script. It will prompt you to authorize for Drive and Gmail separately in your browser.
5. After authorization, it will create `drive_token.json` and `gmail_token.json` for future use.

This setup allows you to use different Google accounts for Drive and Gmail operations. The authorization flow will run separately for each service, allowing you to log in with different accounts.
"""