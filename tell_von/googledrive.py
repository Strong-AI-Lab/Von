from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import datetime
import os
import io
from googleapiclient.http import MediaIoBaseDownload

SCOPES = [
    'https://www.googleapis.com/auth/drive.file'
]
def getDriveCredName():
    cred= os.getenv("VON_GOOGLE_GDRIVE_CRED") 
    return cred

def get_service():
    # persist successful service to function attribute
    if not hasattr(get_service,"service"):
        try:
            creds = authenticate_google_drive()
            get_service.service  = build('drive', 'v3', credentials=creds)
            return get_service.service
        except Exception as e:
            print(f"An error occurred authenticating to Drive: {e}")
            return None
    elif not hasattr(get_service,"serviceexistremainder"):  # don't print this message again after the first time
        print("Google Service exists already, returning it.")
        get_service.serviceexistremainder = True

    return get_service.service

def get_default_folder_id():
    if hasattr(get_default_folder_id, 'cached_result'):
        return get_default_folder_id.cached_result
    else:
        get_default_folder_id.cached_result = os.getenv("VON_ROOT_FOLDER_ID") 
        return get_default_folder_id.cached_result

def authenticate_google_drive():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first time.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(getDriveCredName(), SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

def upload_file_to_drive(service, file_name, file_content,folder_id=get_default_folder_id()):
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]  # Specify the folder ID here
        }
    fh = io.BytesIO(file_content.encode())
    media = MediaIoBaseUpload(fh, mimetype='text/plain')
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print('File ID:', file.get('id'))
    print('File Name:', file_name)
    return file.get('id')

def submit_text(text):
    if text:
        utc_timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d-%H-%M-%S')
        file_name = f"{utc_timestamp}.txt"
        folder_id=get_default_folder_id()
        creds = authenticate_google_drive()
        service = build('drive', 'v3', credentials=creds)
        upload_file_to_drive(service, file_name, text)

def save_to_drive(file_content, file_name=None, folder_id=get_default_folder_id()):
    if file_name==None:
        utc_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')
        file_name = f"{utc_timestamp}.txt"
   
    upload_file_to_drive(get_service(), file_name, file_content, folder_id)
    print(f"Saved {file_content} to {file_name} in GoogleDrive.")
    upload_file_to_drive(get_service(), file_name, file_content)

def iterate_files_in_folder(folder_id=get_default_folder_id()):
    # Call the Drive API to list the files in the folder
    results = get_service().files().list(q=f"'{folder_id}' in parents and trashed=false", fields="files(id, name)").execute()
    files = results.get('files', [])
    return files

def update_all_file_content(summaryFileName="ALL_FILE_CONTENT.txt"):
    # Iterate through the files and print their names       
    # Call the function with the folder ID
    content=""
    for file in iterate_files_in_folder():
        fname=file.get('name')
        if (fname == summaryFileName): 
            print("Skipping: ", fname)
            continue
        fid=file.get('id')
        thisc= get_file_content(fid)
        print('File Name:', fname, ' File ID:', fid ,'---', thisc)
        content=content+thisc+"\n"
    save_to_drive(content, file_name=summaryFileName)
    return content

def get_file_content(file_id):
    request = get_service().files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    file_content = fh.getvalue().decode()
    return file_content


# Function to move a file to trash
def trash_file(file_id, service=None):

        # Update the 'trashed' field of the file to True
        update_body = {'trashed': True}
        response = get_service().files().update(
            fileId=file_id,
            body=update_body
        ).execute()
        print("File moved to bin successfully.")
        return response


# # Call the function with the file ID
# file_id = 'your_file_id_here'
# file_content = get_file_content(file_id)
# print('File Content:', file_content)

def get_file_description(file_id, service=None ):
    try:
        if service is None:
            creds = authenticate_google_drive()
            service = build('drive', 'v3', credentials=creds)
        # Request to get the 'description' field of the file
        file = service.files().get(fileId=file_id, fields='description').execute()
        description = file.get('description', 'No description provided.')
        print(f"Description: {description}")
        return description
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
    
def update_file_description( file_id, new_description, service=None ):
    try:
        if service is None:
            creds = authenticate_google_drive()
            service = build('drive', 'v3', credentials=creds)
        # File metadata to update
        file_metadata = {'description': new_description}
        
        # Update the file
        updated_file = service.files().update(fileId=file_id, body=file_metadata, fields='description').execute()
        print(f"Updated Description: {updated_file.get('description')}")
        return updated_file
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

print(getDriveCredName())
#print(get_default_folder_id())
#get_service()
#print("Content:",update_all_file_content())
