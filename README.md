# Agent Signal Room

No-build teaching UI for a real Gemini-planned agent trace. The browser renders mission data returned by the Python backend; it does not fabricate tool results or completion messages.

## Local setup

1. Install Python 3.10+.
2. Install the Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Set your Gemini API key. Since the key was pasted into chat, revoke it if it is real, create a replacement in Google AI Studio, and keep the replacement out of source control:

   ```powershell
   $env:GEMINI_API_KEY = "your-gemini-api-key"
   ```

   The default model is `gemini-3.5-flash-lite`. Override it with `MODEL` or `GEMINI_MODEL` if needed.

4. Configure Gmail only if you want the approval action to actually send mail:

   ```powershell
   $env:GMAIL_ADDRESS = "you@gmail.com"
   $env:GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
   ```

   For explicit sender and test recipient settings, you may use `SENDER_GMAIL` and `TEST_RECIPIENT`.

   `GMAIL_APP_PASSWORD` must be a Google App Password for an account with 2-Step Verification enabled. Do not use your normal Gmail password and do not commit either value.

5. Run the app:

   ```powershell
   cd path\to\agentic_ui\ui
   python server.py
   ```

Open <http://127.0.0.1:8000>. The health indicator reports whether Gemini is reachable. If Gemini is unavailable or not configured, the mission is blocked with an explicit error; there is no canned fallback. Wikipedia uses the Wikimedia API, weather uses Open-Meteo, and Gmail is contacted only after **Approve & send**.

Optional settings:

```powershell
$env:PORT = "8000"
$env:MODEL = "gemini-3.5-flash-lite"
```

Use **Ctrl+Enter** in the composer to launch a mission.
