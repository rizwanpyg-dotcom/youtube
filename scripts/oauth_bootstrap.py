"""One-time YouTube authorisation, run on YOUR PC (Phase 5).

1. In Google Cloud Console create an OAuth client of type "Desktop app" and download its JSON
   as client_secret.json into this project folder (it's git-ignored).
2. Run:  .venv\\Scripts\\python scripts\\oauth_bootstrap.py
3. A browser window opens. Sign in with the account that owns the channel and approve.
4. The script writes YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN into .env (git-ignored).
   Copy each value into GitHub → Settings → Secrets and variables → Actions, then delete
   client_secret.json and the .env lines if you don't need local uploads.

The refresh token is printed nowhere and never sent anywhere except Google's token endpoint.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ytengine.youtube.auth import SCOPES  # noqa: E402


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    secret_file = ROOT / "client_secret.json"
    if not secret_file.exists():
        print(f"Missing {secret_file}. Download the Desktop OAuth client JSON first (see docs/DEPLOYMENT.md).")
        return 1
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Opening your browser for Google sign-in...")
    if not creds.refresh_token:
        print("Google did not return a refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run again.")
        return 1
    info = json.loads(secret_file.read_text())["installed"]
    env = ROOT / ".env"
    lines = [l for l in (env.read_text().splitlines() if env.exists() else [])
             if not l.startswith(("YT_CLIENT_ID=", "YT_CLIENT_SECRET=", "YT_REFRESH_TOKEN="))]
    lines += [f"YT_CLIENT_ID={info['client_id']}", f"YT_CLIENT_SECRET={info['client_secret']}",
              f"YT_REFRESH_TOKEN={creds.refresh_token}"]
    env.write_text("\n".join(lines) + "\n")
    print(f"Saved YouTube credentials to {env} (git-ignored).")
    print("Next: add YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN as GitHub Actions secrets.")
    print("Verify locally with:  .venv\\Scripts\\python -m ytengine watchdog   (check 'youtube_oauth')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
