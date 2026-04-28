"""
check_folder.py
===============
Quick script to verify that OUTLOOK_INVOICE_FOLDER still exists
in the configured mailbox. Run from the project root:

    venv/Scripts/python check_folder.py
"""
from O365 import Account, FileSystemTokenBackend
from config import (
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID,
    WORKER_EMAIL, OUTLOOK_INVOICE_FOLDER, BASE_DIR,
)

credentials   = (MS_CLIENT_ID, MS_CLIENT_SECRET)
token_backend = FileSystemTokenBackend(
    token_path=str(BASE_DIR),
    token_filename="o365_token.txt",
)
account = Account(
    credentials,
    auth_flow_type="credentials",
    tenant_id=MS_TENANT_ID,
    token_backend=token_backend,
    main_resource=WORKER_EMAIL,
)
if not account.is_authenticated:
    account.authenticate()

mailbox = account.mailbox(resource=WORKER_EMAIL)
inbox   = mailbox.inbox_folder()

found = False

# Check top-level folders
for folder in mailbox.get_folders():
    if folder.name.lower() == OUTLOOK_INVOICE_FOLDER.lower():
        found = True
        print(f"✅ Found '{folder.name}' at top level  (id: {folder.folder_id})")
        break

# Check inbox subfolders
if not found:
    for folder in inbox.get_folders():
        if folder.name.lower() == OUTLOOK_INVOICE_FOLDER.lower():
            found = True
            print(f"✅ Found '{folder.name}' as inbox subfolder  (id: {folder.folder_id})")
            break

if not found:
    print(f"❌ Folder '{OUTLOOK_INVOICE_FOLDER}' not found in mailbox.")
    print("\nAvailable top-level folders:")
    for folder in mailbox.get_folders():
        print(f"   - {folder.name}")
    print("\nAvailable inbox subfolders:")
    for folder in inbox.get_folders():
        print(f"   - {folder.name}")
