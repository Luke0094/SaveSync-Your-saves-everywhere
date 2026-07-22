"""
SaveSync - Embedded App Credentials
Default OAuth client IDs for zero-config sync setup.
Uses rclone's publicly shared client IDs as defaults so users can
connect with one click — no developer account required.

Users can override these with their own credentials via the Advanced
section in Sync Settings for better rate limits.

NOTE: These are PUBLIC client IDs for desktop/native apps.
They are NOT secrets — PKCE and device-code flows are designed
to work without client secrets for native apps (RFC 7636, RFC 8628).

Source: https://github.com/rclone/rclone (MIT License)
"""

# OneDrive — rclone's public Azure App (no secret needed for device-code flow)
# Override: portal.azure.com → App registrations → New registration
ONEDRIVE_CLIENT_ID = "b15665d9-eda6-4092-8539-0eec376afd59"
ONEDRIVE_TENANT = "consumers"

# Dropbox — rclone's public App Key (no secret needed for PKCE)
# Override: dropbox.com/developers → App Console → Create app
DROPBOX_APP_KEY = "5jcck7diasz0rqy"

# Google Drive — rclone's public client ID + secret
# For desktop apps, the client secret is NOT actually secret (Google documents this).
# See: https://developers.google.com/identity/protocols/oauth2/native-app
# Override: console.cloud.google.com → APIs & Services → Credentials
GOOGLE_DRIVE_CLIENT_ID = "202264815644.apps.googleusercontent.com"
GOOGLE_DRIVE_CLIENT_SECRET = "X4Z3ca8xfWDb1Voo-F9a7ZxJ"
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
