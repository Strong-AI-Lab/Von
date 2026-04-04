"""
This module contains functions to save files to Google Drive or a local drive.
"""

import datetime
import os
import io
import subprocess

# Note: Google API imports are done lazily inside functions that actually use them

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveService:
    """Singleton class to manage Google Drive service instance."""

    _instance = None

    @staticmethod
    def get_instance(token_path):
        if DriveService._instance is None:
            creds = authenticate_google_drive(token_path)
            try:  # Lazy import to avoid mandatory dependency at import time
                from googleapiclient.discovery import build  # type: ignore

                DriveService._instance = build("drive", "v3", credentials=creds)
            except ImportError:
                raise RuntimeError(
                    "google-api-python-client not installed. Install to enable Google Drive features."
                )
        return DriveService._instance


def open_folder(path):
    """
    Opens a folder in the file explorer.
    """
    try:
        if os.name == "nt":  # For Windows
            os.startfile(path)
        elif os.name == "posix":  # For macOS and Linux
            subprocess.call(["open", path])
    except Exception:
        pass


def get_drive_cred_name():
    """
    Returns the name of the Google Drive credentials file.
    """
    cred = os.getenv("VON_GOOGLE_GDRIVE_CRED")
    if not cred:
        raise ValueError("Environment variable VON_GOOGLE_GDRIVE_CRED is not set.")
    return cred


def authenticate_google_drive(token_path):
    """Authenticates the user to Google Drive and returns the credentials."""
    # Lazy imports for google auth libraries
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as e:  # pragma: no cover - optional dep missing
        raise RuntimeError(
            "Google Drive dependencies missing. Install 'google-api-python-client google-auth google-auth-oauthlib'."
        ) from e

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not getattr(creds, "valid", False):
        if (
            creds
            and getattr(creds, "expired", False)
            and getattr(creds, "refresh_token", None)
        ):
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                get_drive_cred_name(), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return creds


def save_file_to_local_drive(path, file_name, file_content):
    """
    Saves a file to a local drive.
    """
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, file_name), "a", encoding="utf-8") as f:
            f.write(file_content)
    except Exception:
        pass


def upload_file(service, file_path, file_name, file_content, mime_type="text/plain"):
    """
    Uploads a file to Google Drive.
    """
    try:
        folder_id = create_folder_path(service, file_path)
        file_metadata = {"name": file_name, "parents": [folder_id]}
        # Lazy import for media upload helper
        from googleapiclient.http import MediaIoBaseUpload  # type: ignore

        media = MediaIoBaseUpload(io.BytesIO(file_content.encode()), mimetype=mime_type)
        file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )
        return file.get("id")
    except Exception:
        pass


def create_token_file(token_path):
    """
    Creates a Google Drive service instance.
    """
    creds_path = os.getenv("VON_GOOGLE_GDRIVE_CRED")
    if not creds_path:
        raise ValueError("Environment variable VON_GOOGLE_GDRIVE_CRED is not set.")
    # Lazy import
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-auth-oauthlib not installed. Install to create token file."
        ) from e
    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
    # return build('drive', 'v3', credentials=authenticate_google_drive(token_path))


def save_to_drive(
    location, token_path, file_path, file_name, file_content, mime_type="text/plain"
):
    """
    Saves a file to Google Drive or a local drive.
    """
    if not file_name:
        utc_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d-%H-%M-%S"
        )
        file_name = f"{utc_timestamp}.txt"

    if location == "local":
        save_file_to_local_drive(file_path, file_name, file_content)
    elif location == "drive":

        # Check if token_path exists; if not, create it using credentials.json
        if not os.path.exists(token_path):
            create_token_file(token_path)

        # TODO: have to update priodically. Just create anyway for now?
        service = DriveService.get_instance(token_path)
        file_id = upload_file(service, file_path, file_name, file_content, mime_type)

        if file_id:
            print(f"File '{file_name}' saved to Google Drive.")
            drive_file_content = read_file_from_drive(service, file_path, file_name)
            print(f"File content from Google Drive: {drive_file_content}")


def iterate_files_in_folder(token_path, folder_id=None):
    """
    Iterates through the files in a folder and returns their details.
    """
    try:
        service = DriveService.get_instance(token_path)
        query = (
            f"'{folder_id}' in parents and trashed=false"
            if folder_id
            else "trashed=false"
        )
        results = (
            service.files()
            .list(pageSize=1000, q=query, fields="files(name,id,mimeType)")
            .execute()
        )
        files = results.get("files", [])
        return files
    except Exception:
        return []


def create_folder_path(service, path):
    """
    Creates a folder path if it doesn't exist and returns the final folder ID.

    Args:
        service: Google Drive service instance
        path: String path from root (e.g. "folder1/folder2/folder3")

    Returns:
        str: Folder ID of the last folder in the path
    """
    if not path or path == "/":
        return "root"

    parts = [p for p in path.split("/") if p]
    parent_id = "root"

    for folder_name in parts:
        # Check if folder exists
        query = f"name = '{folder_name}' and "
        query += f"'{parent_id}' in parents and "
        query += "mimeType = 'application/vnd.google-apps.folder' and "
        query += "trashed = false"

        results = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )

        items = results.get("files", [])

        if items:
            # Use existing folder
            parent_id = items[0]["id"]
        else:
            # Create new folder
            folder_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }

            folder = service.files().create(body=folder_metadata, fields="id").execute()

            parent_id = folder.get("id")

    return parent_id


def read_file_from_drive(service, file_path, file_name):
    """
    Reads a file from Google Drive and returns its content.
    """
    try:
        folder_id = create_folder_path(service, file_path)
        query = f"name = '{file_name}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            file_id = files[0]["id"]
            request = service.files().get_media(fileId=file_id)
            # Lazy import for MediaIoBaseDownload
            from googleapiclient.http import MediaIoBaseDownload  # type: ignore

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            return fh.getvalue().decode()
        else:
            return None
    except Exception:
        return None


def delete_file_from_drive(service, file_path, file_name):
    """
    Deletes a file from Google Drive.
    """
    try:
        folder_id = create_folder_path(service, file_path)
        query = f"name = '{file_name}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            file_id = files[0]["id"]
            service.files().delete(fileId=file_id).execute()
            return True
        else:
            return False
    except Exception:
        return False
