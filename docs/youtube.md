# Connect YouTube

1. Go to console.cloud.google.com and create a project.
2. Enable "YouTube Data API v3".
3. OAuth consent screen: External, add yourself as a test user.
4. Credentials: Create credentials, OAuth client ID, type Desktop app. Download the JSON.
5. Save it in this folder as `client_secret.json` (or leave it in Downloads, `connect_youtube.bat` picks up the newest `client_secret_*.json`).
6. Run `connect_youtube.bat`. A browser opens, you sign in and approve. This creates `yt_token.json`.

Notes:
- Uploads from an unaudited project are private. Request an API audit with Google, or switch videos to public in YouTube Studio.
- The API quota is about 6 uploads a day on the default allowance.
- Never share `client_secret.json` or `yt_token.json`.
