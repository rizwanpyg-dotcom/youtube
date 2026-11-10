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
    from ytengine import config
    config.load()  # reads the local .env
    secret_file = ROOT / "client_secret.json"
    if secret_file.exists():
        info = json.loads(secret_file.read_text())["installed"]
    elif config.Config.secret("YT_CLIENT_ID") and config.Config.secret("YT_CLIENT_SECRET"):
        # Re-authorising (e.g. to pick a different channel): reuse the client saved in .env.
        info = {"client_id": config.Config.secret("YT_CLIENT_ID"),
                "client_secret": config.Config.secret("YT_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"]}
    else:
        print(f"Missing {secret_file}. Download the Desktop OAuth client JSON first (see docs/DEPLOYMENT.md).")
        return 1
    flow = InstalledAppFlow.from_client_config({"installed": info}, SCOPES)
    creds = flow.run_local_server(port=0, prompt="select_account consent", access_type="offline",
                                  authorization_prompt_message=(
                                      "Opening your browser for Google sign-in...\n"
                                      "If no browser tab opens, copy this whole link into your browser:\n\n{url}\n"),
                                  success_message="Sign-in received. You can close this tab and return to the terminal.")
    if not creds.refresh_token:
        print("Google did not return a refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run again.")
        return 1
    env = ROOT / ".env"
    lines = [l for l in (env.read_text().splitlines() if env.exists() else [])
             if not l.startswith(("YT_CLIENT_ID=", "YT_CLIENT_SECRET=", "YT_REFRESH_TOKEN="))]
    lines += [f"YT_CLIENT_ID={info['client_id']}", f"YT_CLIENT_SECRET={info['client_secret']}",
              f"YT_REFRESH_TOKEN={creds.refresh_token}"]
    env.write_text("\n".join(lines) + "\n")
    print(f"Saved YouTube credentials to {env} (git-ignored).")
    try:
        from googleapiclient.discovery import build
        items = build("youtube", "v3", credentials=creds, cache_discovery=False).channels() \
            .list(part="snippet", mine=True).execute().get("items", [])
        for c in items:
            print(f"Authorised channel: {c['snippet'].get('title')} ({c['snippet'].get('customUrl')})")
        if not items:
            print("WARNING: this sign-in has no YouTube channel. Run again and pick the channel.")
    except Exception as e:
        print(f"(Could not read the channel name: {type(e).__name__})")
    print("Next: add YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN as GitHub Actions secrets.")
    print("Verify locally with:  .venv\\Scripts\\python -m ytengine watchdog   (check 'youtube_oauth')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
