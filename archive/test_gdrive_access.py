from googleapiclient.discovery import build
from google.oauth2 import service_account

# === CONFIG ===
SERVICE_ACCOUNT_FILE = "gdrive2video-access-key.json"  # path to your downloaded key
FOLDER_ID = "1Z1RGGtUnSdxzRw6KHCdYEpn87R2aKjle"          # paste from your Drive URL
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# === AUTHENTICATE ===
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

service = build("drive", "v3", credentials=creds)

# === TEST LIST FILES ===
try:
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed=false",
        fields="files(id, name, mimeType)"
    ).execute()

    files = results.get("files", [])
    if not files:
        print("✅ Connected successfully, but folder is empty or no permission.")
    else:
        print("✅ Connected successfully! Found files:")
        for f in files:
            print(f" - {f['name']} ({f['mimeType']})")

except Exception as e:
    print("❌ Error accessing Drive:")
    print(e)