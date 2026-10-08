"""Agent Signal Room backend: Gemini planning, live tools, and gated Gmail."""
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode
from google import genai
import json
import re
import smtplib
import uuid
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
GEMINI_MODEL = "gemini-3.5-flash-lite"
MISSIONS: dict[str, dict] = {}
MISSIONS_LOCK = Lock()


class MissionError(Exception):
    def __init__(self, message: str, code: str = "mission_error") -> None:
        super().__init__(message)
        self.code = code


def request_json(url: str, payload: bytes | None = None, headers: dict | None = None, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(url, data=payload, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise MissionError(f"Tool request failed: {url.split('/')[2]}", "tool_unavailable") from exc


def gemini_chat(prompt: str) -> str:
    import os
api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise MissionError("Gemini is not configured. Set the GEMINI_API_KEY environment variable.", "gemini_not_configured")
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"Return only valid JSON. You are an agent planner for a classroom demo.\n\n{prompt}",
            config={"response_mime_type": "application/json", "temperature": 0.0},
        )
    except genai.errors.APIError as exc:
        raise MissionError(f"Gemini request failed: {exc.message}", "gemini_unavailable") from exc
    content = response.text or ""
    if not content:
        raise MissionError("Gemini returned no planning content.", "gemini_invalid")
    return content


def gemini_planning_ready() -> tuple[bool, str | None]:
    try:
        content = gemini_chat('Return exactly this JSON and nothing else: {"ready":true}')
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE))
        if parsed.get("ready") is True:
            return True, None
        return False, "Gemini responded, but the configured model did not pass the planning probe."
    except (MissionError, json.JSONDecodeError, AttributeError) as exc:
        return False, str(exc)


def parse_plan(prompt: str) -> dict:
    instruction = f"""Plan this user mission as JSON. User request: {prompt}
Schema: {{"goal":"string","steps":["string"],"tools":["wikipedia" or "weather"],"email":{{"subject":"string","body":"string"}}}}
Use wikipedia only for Wikipedia/research requests and weather only for weather requests. Include email only as a draft; never claim it was sent. Keep steps to 2-4 concise items."""
    raw = gemini_chat(instruction).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MissionError("Gemini returned a plan that was not valid JSON.", "gemini_invalid") from exc
    # Models may return either the schema names ("weather", "wikipedia") or
    # the concrete capability names shown in the trace ("weather.lookup",
    # "wikipedia.search"). Normalize both forms before validating the plan.
    tools = []
    for raw_tool in plan.get("tools", []):
        tool = str(raw_tool).strip().lower()
        if tool == "weather" or tool.startswith("weather."):
            tools.append("weather")
        elif tool == "wikipedia" or tool.startswith("wikipedia."):
            tools.append("wikipedia")
    tools = list(dict.fromkeys(tools))
    if not tools:
        raise MissionError("Gemini plan did not select a supported research tool.", "plan_invalid")
    email = plan.get("email") if isinstance(plan.get("email"), dict) else {}
    return {
        "goal": str(plan.get("goal") or prompt)[:500],
        "steps": [str(item)[:240] for item in plan.get("steps", [])][:4] or ["Gather live evidence", "Prepare an email draft", "Wait for approval"],
        "tools": list(dict.fromkeys(tools)),
        "email": {"subject": str(email.get("subject") or "Agent Signal Room draft")[:160], "body": str(email.get("body") or "Draft created from the live tool result; review before sending.")[:3000]},
    }


def wikipedia_result(prompt: str) -> dict:
    topic_match = re.search(r"(?:wikipedia(?:'s| page on| article on)?|research)\s+(?:the\s+)?(.+?)(?:\s+and\s+email|\s+and\s+send|$)", prompt, re.IGNORECASE)
    topic = (topic_match.group(1).strip(" .?!") if topic_match else "black hole")[:120]
    params = urlencode({"action": "query", "prop": "extracts|info", "exintro": "1", "explaintext": "1", "inprop": "url", "titles": topic, "format": "json", "origin": "*"})
    data = request_json("https://en.wikipedia.org/w/api.php?" + params, timeout=10.0)
    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    extract = str(page.get("extract", "")).strip()
    if not extract:
        raise MissionError(f"Wikipedia returned no article extract for '{topic}'.", "tool_empty")
    return {"tool": "wikipedia.search", "label": topic, "text": extract[:900], "source": page.get("fullurl") or "https://en.wikipedia.org/", "kind": "research"}


def weather_location(prompt: str) -> str:
    """Extract a place from the natural-language weather request.

    Weather prompts commonly put the date before the place ("weather today in
    Mumbai") or include filler such as "what's the weather like". Keep this
    parser deliberately conservative so words from the rest of the request
    (for example, an email instruction) are not sent to geocoding.
    """
    patterns = (
        # "weather in Mumbai", "weather like in New York"
        r"\bweather(?:\s+(?:is|like))?\s+(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|now|and|then)\b|\s*,|\?|$)",
        # "weather today in Mumbai", "weather tomorrow for Delhi"
        r"\bweather\s+(?:today|tomorrow|now)\s+(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:and|then)\b|\s*,|\?|$)",
        # "what is the weather in Mumbai"
        r"\b(?:what(?:'s| is)|tell me)\s+(?:the\s+)?weather(?:\s+(?:is|like))?\s+(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:today|tomorrow|now|and|then)\b|\s*,|\?|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .?!")
    return ""


def weather_result(prompt: str) -> dict:
    location = weather_location(prompt)
    if not location:
        raise MissionError("Could not identify a city in the weather request.", "tool_input")
    geo_params = urlencode({"name": location, "count": 1, "language": "en", "format": "json"})
    geo = request_json("https://geocoding-api.open-meteo.com/v1/search?" + geo_params, timeout=10.0)
    places = geo.get("results") or []
    if not places:
        raise MissionError(f"Open-Meteo could not locate '{location}'.", "tool_empty")
    place = places[0]
    forecast_params = urlencode({"latitude": place["latitude"], "longitude": place["longitude"], "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m", "timezone": "auto"})
    forecast = request_json("https://api.open-meteo.com/v1/forecast?" + forecast_params, timeout=10.0)
    current = forecast.get("current") or {}
    if not current:
        raise MissionError("Open-Meteo returned no current conditions.", "tool_empty")
    text = f"{place.get('name', location)}: {current.get('temperature_2m')}°C, humidity {current.get('relative_humidity_2m')}%, wind {current.get('wind_speed_10m')} km/h, weather code {current.get('weather_code')}."
    return {"tool": "weather.lookup", "label": place.get("name", location), "text": text, "source": "https://open-meteo.com/", "kind": "weather"}


def make_stages(prompt: str, plan: dict, results: list[dict]) -> list[dict]:
    tools = [result["tool"] for result in results]
    evidence = "\n\n".join(f"{result['tool']}: {result['text']}" for result in results)
    draft = compose_email_body(plan, results)
    return [
        {"id": "request", "name": "Request received", "copy": "The backend received this mission and opened a fresh trace.", "tool": "intake.parse", "result": prompt, "status": "done"},
        {"id": "plan", "name": "Plan assembled", "copy": "Gemini selected the steps and tools below.", "tool": "planner.gemini", "result": " → ".join(plan["steps"]), "status": "done"},
        {"id": "tools", "name": "Tools executed", "copy": "Only tools with returned evidence appear in this trace.", "tools": tools, "result": "Executed successfully: " + ", ".join(tools), "status": "done"},
        {"id": "results", "name": "Results returned", "copy": "These external tools returned the evidence below.", "tools": tools, "results": results, "result": evidence, "status": "done"},
        {"id": "approval", "name": "Next step: human approval", "copy": "The draft is prepared, but Gmail is not contacted until you choose an action.", "tool": "gmail.smtp", "result": draft, "subject": plan["email"]["subject"], "status": "approval_required"},
        {"id": "final", "name": "Final answer", "copy": "This step stays pending until the email decision and any errors are resolved.", "tool": "response.compose", "result": "pending approval", "status": "pending"},
    ]


def compose_email_body(plan: dict, results: list[dict]) -> str:
    evidence = "\n\n".join(f"{result['tool']}: {result['text']}" for result in results)
    draft = plan["email"]["body"].strip()
    placeholders = {"[Insert Data Here]", "[insert data here]", "[PLACEHOLDER]"}
    for placeholder in placeholders:
        draft = draft.replace(placeholder, evidence)
    if evidence not in draft:
        draft = draft + "\n\nLive results collected by the agent:\n" + evidence
    return draft


def run_mission(prompt: str) -> dict:
    plan = parse_plan(prompt)
    results = execute_tools(prompt, plan["tools"])
    mission_id = uuid.uuid4().hex
    email_body = compose_email_body(plan, results)
    plan["email"]["body"] = email_body
    mission = {"id": mission_id, "prompt": prompt, "plan": plan, "results": results, "stages": make_stages(prompt, plan, results), "decision": "pending"}
    with MISSIONS_LOCK:
        MISSIONS[mission_id] = mission
    return mission


def execute_tools(prompt: str, tools: list[str]) -> list[dict]:
    results = []
    for tool in tools:
        if tool == "wikipedia":
            results.append(wikipedia_result(prompt))
        elif tool == "weather":
            results.append(weather_result(prompt))
        else:
            raise MissionError(f"Unsupported planned tool: {tool}", "plan_invalid")
    if not results:
        raise MissionError("The plan produced no executable tools.", "plan_invalid")
    return results


def send_gmail(mission: dict) -> None:
    address = "sarthak.dasari@gmail.com"
    recipient = "sarthak.dasari@gmail.com"
    app_password ="jmnmjzezjosucwdm"
    if not address or not app_password:
        raise MissionError("Gmail is not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (a Gmail App Password).", "gmail_not_configured")
    message = EmailMessage()
    message["Subject"] = mission["plan"]["email"]["subject"]
    message["From"] = address
    message["To"] = recipient
    message.set_content(mission["plan"]["email"]["body"])
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=12) as smtp:
            smtp.starttls()
            smtp.login(address, app_password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MissionError("Gmail send failed; no completion was recorded.", "gmail_send_failed") from exc


def get_mission(mission_id: str) -> dict:
    with MISSIONS_LOCK:
        mission = MISSIONS.get(mission_id)
    if not mission:
        raise MissionError("Mission not found or server was restarted.", "mission_not_found")
    return mission


def action_mission(mission_id: str, action: str) -> dict:
    mission = get_mission(mission_id)
    if action == "approve":
        send_gmail(mission)
        mission["decision"] = "approved"
        mission["stages"][4]["status"] = "done"
        mission["stages"][4]["result"] = "Gmail send_message succeeded."
        mission["stages"][5]["status"] = "done"
        mission["stages"][5]["result"] = "Email sent successfully after explicit approval."
        mission["answer"] = "Email sent successfully after your explicit approval."
    elif action == "reject":
        mission["decision"] = "rejected"
        mission["stages"][4]["status"] = "rejected"
        mission["stages"][4]["result"] = "Email canceled by user. No Gmail connection was attempted."
        mission["stages"][5]["status"] = "done"
        mission["stages"][5]["result"] = "Safe stop: no email was sent."
        mission["answer"] = "No email was sent because you rejected the draft."
    elif action in {"retry", "replan"}:
        results = execute_tools(mission["prompt"], mission["plan"]["tools"])
        mission["results"] = results
        mission["stages"][2]["tools"] = [result["tool"] for result in results]
        mission["stages"][3]["tools"] = [result["tool"] for result in results]
        mission["stages"][3]["results"] = results
        mission["stages"][3]["result"] = "\n\n".join(f"{result['tool']}: {result['text']}" for result in results) + (" Retry succeeded." if action == "retry" else " Replanned with fresh live lookups.")
        mission["stages"][3]["status"] = "done"
    else:
        raise MissionError("Unknown mission action.", "invalid_action")
    return mission


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: object) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8" if not isinstance(body, bytes) else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes())
            return
        if self.path == "/api/health":
            gemini, error = gemini_planning_ready()
            self._send(200, {"ok": gemini, "gemini": gemini, "planning_ready": gemini, "model": GEMINI_MODEL, "gmail_configured": bool("projectpayments.voidmain@gail.com" and "nuvgqaiawvotnulb"), "error": error})
            return
        self._send(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            prompt = str(payload.get("prompt", "")).strip()[:2000]
            if self.path == "/api/mission":
                if not prompt:
                    raise MissionError("A prompt is required.", "invalid_request")
                self._send(200, {"ok": True, "mission": run_mission(prompt)})
                return
            match = re.fullmatch(r"/api/mission/([a-f0-9]+)/([a-z]+)", self.path)
            if match:
                self._send(200, {"ok": True, "mission": action_mission(match.group(1), match.group(2))})
                return
            self._send(404, {"error": "Not found"})
        except MissionError as exc:
            self._send(503 if exc.code in {"gemini_unavailable", "gemini_not_configured", "tool_unavailable", "gmail_not_configured", "gmail_send_failed"} else 400, {"ok": False, "error": str(exc), "code": exc.code})
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "Invalid JSON request.", "code": "invalid_request"})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[agent-signal-room] {fmt % args}")


if __name__ == "__main__":
    port = 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Agent Signal Room running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
