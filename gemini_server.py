import requests
import json
import re
import os
import uvicorn
import random
import string
import hashlib
import time
import threading
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import List, Optional, Union, Any
from dotenv import load_dotenv
from random import randint

load_dotenv()
# ==========================================
# CONFIGURATION
# ==========================================
API_MODE = "native"
DEFAULT_MODEL = "1"

MODELS = {
    "1": ("Gemini 3 Flash", "56fdd199312815e2"),
    "2": ("Gemini 3 Flash Thinking", "e051ce1aa80aa576"),
    "3": ("Gemini 3.1 Pro", "e6fa609c3fa255c0")
}

AUTH_STATE_FILE = "auth_state.json"

app = FastAPI(title="Project Chimera: Gemini-to-Claude Proxy")

login_session = requests.Session()
login_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

guest_session = requests.Session()
guest_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# ==========================================
# COOKIE MANAGEMENT (Playwright-backed)
# ==========================================
_cookie_lock = threading.Lock()
_last_cookie_refresh = 0.0


def _load_auth_state() -> list:
    """Load cookies list from auth_state.json."""
    if not os.path.exists(AUTH_STATE_FILE):
        print(f"[-] {AUTH_STATE_FILE} not found. Run setup_auth.py first.")
        return []
    with open(AUTH_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    return state.get("cookies", [])


def _save_auth_state(cookies: list):
    """Persist refreshed cookies back to auth_state.json."""
    state = {"cookies": cookies, "origins": []}
    with open(AUTH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _cookies_as_dict(cookies: list) -> dict:
    """Convert cookies list to simple name→value dict for requests."""
    return {c["name"]: c["value"] for c in cookies}


def _apply_cookies_to_session(session: requests.Session, cookies: list):
    """Inject a cookies list into a requests.Session."""
    session.cookies.clear()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ".google.com"))


def refresh_cookies_playwright() -> bool:
    """
    Launch a headless Playwright browser, load existing cookies,
    visit gemini.google.com, harvest the refreshed cookies, and
    persist them. Returns True on success.
    """
    global _last_cookie_refresh
    print("[*] Playwright: starting headless cookie refresh...")
    try:
        from playwright.sync_api import sync_playwright  # lazy import
    except ImportError:
        print("[-] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return False

    try:
        with _cookie_lock:
            current_cookies = _load_auth_state()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

            # Inject existing cookies so we start authenticated
            playwright_cookies = []
            for c in current_cookies:
                pc = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".google.com"),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", True),
                    "httpOnly": c.get("httpOnly", True),
                }
                same_site = c.get("sameSite")
                if same_site == "no_restriction":
                    pc["sameSite"] = "None"
                elif same_site in ("lax", "strict", "Lax", "Strict"):
                    pc["sameSite"] = same_site.capitalize()
                else:
                    pc["sameSite"] = "None"
                playwright_cookies.append(pc)

            context.add_cookies(playwright_cookies)

            page = context.new_page()
            # Visit the app page — forces session cookie refresh server-side
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)  # let any XHR cookie-refresh calls settle

            fresh_pw_cookies = context.cookies()
            browser.close()

        # Convert Playwright cookie format back to our storage format
        fresh_cookies = []
        for c in fresh_pw_cookies:
            domain = c.get("domain", ".google.com")
            if not domain.startswith("."):
                domain = "." + domain.lstrip(".")
            same_site_raw = c.get("sameSite", "None")
            # Playwright uses "None" | "Lax" | "Strict"
            if same_site_raw == "None":
                same_site_out = "no_restriction"
            elif same_site_raw == "Lax":
                same_site_out = "lax"
            elif same_site_raw == "Strict":
                same_site_out = "strict"
            else:
                same_site_out = None

            fresh_cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", True),
                "sameSite": same_site_out,
            })

        with _cookie_lock:
            _save_auth_state(fresh_cookies)
            _apply_cookies_to_session(login_session, fresh_cookies)
            _last_cookie_refresh = time.time()

        print(f"[+] Playwright: refreshed {len(fresh_cookies)} cookies successfully.")
        return True

    except Exception as e:
        print(f"[-] Playwright cookie refresh failed: {e}")
        return False


def _cookie_refresh_worker():
    """Background thread: refresh cookies at randomized intervals to evade bot detection."""
    while True:
        # 1. THE JITTER: Sleep between 20 and 40 minutes BEFORE doing anything
        sleep_seconds = random.randint(20 * 60, 40 * 60)
        print(f"[*] Cookie Watchdog sleeping for {sleep_seconds // 60} minutes...")
        time.sleep(sleep_seconds)

        # 2. AFTER sleeping, run the Playwright refresh silently
        try:
            refresh_cookies_playwright()
        except Exception as e:
            print(f"[-] Cookie refresh thread error: {e}")


# Load initial cookies from auth_state.json at startup
_initial_cookies = _load_auth_state()
if _initial_cookies:
    _apply_cookies_to_session(login_session, _initial_cookies)
    _last_cookie_refresh = time.time()
    print(f"[+] Loaded {len(_initial_cookies)} cookies from {AUTH_STATE_FILE}")
else:
    print(f"[!] No cookies loaded — requests will run unauthenticated until refresh.")

GLOBAL_AT_TOKEN = None
GLOBAL_TOKEN_REFRESH_TIME = 0.0
GLOBAL_U_PATH = "u/0/"

GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
GLOBAL_LAST_BLOCKS = []
GLOBAL_PENDING_TOOL_IDS: set = set()
GLOBAL_LAST_TOOL_USE_RESPONSE: dict = None
GLOBAL_TOOL_REDELIVERY_COUNT: int = 0
MAX_TOOL_REDELIVERY: int = 3

request_lock = threading.Lock()
_response_cache: dict = {}
CACHE_TTL = 8.0


def _cache_key(prompt: str) -> str:
    return hashlib.md5(prompt.encode()).hexdigest()


def _cache_get(key: str):
    entry = _response_cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["response"]
    return None


def _cache_set(key: str, response: dict):
    _response_cache[key] = {"response": response, "ts": time.time()}
    cutoff = time.time() - CACHE_TTL
    stale = [k for k, v in _response_cache.items() if v["ts"] < cutoff]
    for k in stale:
        del _response_cache[k]


# ==========================================
# SHARED UTILITIES
# ==========================================
def parse_gemini_response(raw_text):
    data = {"text": None, "context": (None, None, None, None)}
    try:
        for line in raw_text.split('\n'):
            line = line.strip()
            if line.startswith('[') and 'wrb.fr' in line and '"rc_' in line:
                payload = json.loads(line)
                if isinstance(payload, list) and len(payload) > 0 and isinstance(payload[0], list) and len(
                        payload[0]) > 2:
                    inner_str = payload[0][2]
                    inner_obj = json.loads(inner_str)

                    def find_rc(obj):
                        if isinstance(obj, list):
                            if len(obj) > 1 and isinstance(obj[0], str) and obj[0].startswith("rc_"): return obj
                            for item in obj:
                                res = find_rc(item)
                                if res: return res
                        return None

                    rc_array = find_rc(inner_obj)
                    if rc_array and len(rc_array) > 1 and isinstance(rc_array[1], list) and len(rc_array[1]) > 0:
                        data["text"] = rc_array[1][0]
                        break
    except Exception:
        pass

    if not data["text"]:
        match = re.search(r'\\"rc_[^"]+?\\",\s*\[\\"(.*?)(?<!\\\\)\\"[,\]]', raw_text, re.DOTALL)
        if match:
            raw_msg = match.group(1)
            try:
                data["text"] = raw_msg.encode().decode('unicode-escape').encode('latin1').decode('utf-8', 'ignore')
            except Exception:
                data["text"] = raw_msg

    if data["text"]:
        clean = data["text"]
        clean = clean.replace('\\u003d', '=').replace('\\u003c', '<').replace('\\u003e', '>')
        clean = clean.replace('\u003c', '<').replace('\u003e', '>')

        parts = re.split(r'(<TOOL_CALL>.*?</TOOL_CALL>)', clean, flags=re.DOTALL)
        for i in range(len(parts)):
            parts[i] = parts[i].replace('\\"', '"')
            if not parts[i].startswith('<TOOL_CALL>'):
                parts[i] = parts[i].replace('\\n', '\n')
        data["text"] = "".join(parts)

    c_match = re.search(r'\\"(c_[a-z0-9]+?)\\"', raw_text)
    r_match = re.search(r'\\"(r_[a-z0-9]+?)\\"', raw_text)
    rc_match = re.search(r'\\"(rc_[a-z0-9]+?)\\"', raw_text)
    token_match = re.search(r'\\"26\\":\\"(.*?)\\"', raw_text)

    if c_match and r_match and rc_match:
        data["context"] = (
            c_match.group(1), r_match.group(1), rc_match.group(1), token_match.group(1) if token_match else "")
    return data


def generate_tool_id():
    return "toolu_01" + "".join(random.choices(string.ascii_letters + string.digits, k=15))


def repair_and_parse_tool_json(raw_json: str) -> dict:
    raw_json = raw_json.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw_json, strict=False)
    except json.JSONDecodeError:
        pass

    tool_name = None
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', raw_json)
    if name_match:
        tool_name = name_match.group(1)

    input_data = {}

    if tool_name in ["Write", "Bash"]:
        target_key = "content" if tool_name == "Write" else "command"

        if tool_name == "Write":
            path_m = re.search(r'"file_path"\s*:\s*"([^"]+)"', raw_json)
            if path_m:
                input_data["file_path"] = path_m.group(1)

        content_idx = raw_json.find(f'"{target_key}"')
        if content_idx != -1:
            val_start = raw_json.find('"', content_idx + len(target_key) + 2)
            if val_start != -1:
                val = raw_json[val_start + 1:]

                fp_idx = val.rfind('", "file_path"')
                if fp_idx != -1: val = val[:fp_idx]

                val = val.rstrip()
                for suffix in ['}}</TOOL_CALL>', '</TOOL_CALL>', '}}', '}', '"']:
                    if val.endswith(suffix):
                        val = val[:-len(suffix)].rstrip()

                val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
                input_data[target_key] = val

        return {"name": tool_name, "input": input_data}

    # === NEW: BULLETPROOF EDIT PARSER ===
    if tool_name == "Edit":
        path_m = re.search(r'"file_path"\s*:\s*"([^"]+)"', raw_json)
        if path_m:
            input_data["file_path"] = path_m.group(1)

        old_idx = raw_json.find('"old_string"')
        new_idx = raw_json.find('"new_string"')

        if old_idx != -1 and new_idx != -1:
            first_idx = min(old_idx, new_idx)
            second_idx = max(old_idx, new_idx)
            first_key = "old_string" if old_idx < new_idx else "new_string"
            second_key = "new_string" if old_idx < new_idx else "old_string"

            # Isolate the first string block
            start1 = raw_json.find('"', raw_json.find(':', first_idx)) + 1
            end1 = raw_json.rfind('"', 0, raw_json.rfind(',', 0, second_idx))
            val1 = raw_json[start1:end1]

            # Isolate the second string block
            start2 = raw_json.find('"', raw_json.find(':', second_idx)) + 1
            val2 = raw_json[start2:]

            # Clean suffixes off the final value
            val2 = val2.rstrip()
            for suffix in ['}}</TOOL_CALL>', '</TOOL_CALL>', '}}', '}', '"']:
                if val2.endswith(suffix):
                    val2 = val2[:-len(suffix)].rstrip()

            input_data[first_key] = val1.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            input_data[second_key] = val2.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

        return {"name": tool_name, "input": input_data}
    # ====================================

    # Fallback for simple tools (like Read, Grep, etc)
    kv_pattern = r'"([^"]+)"\s*:\s*"((?:\\.|[^"\\])*)"'
    for key, val in re.findall(kv_pattern, raw_json):
        if key == "name": continue
        input_data[key] = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

    if tool_name and input_data:
        return {"name": tool_name, "input": input_data}

    raise ValueError("Could not repair tool JSON")


def _format_tool_result(content: str, tool_name: str) -> str:
    limit = 12000
    if len(content) <= limit: return content
    return content[:limit] + f"\n[...truncated at {limit} chars...]"


def _normalize_block(item) -> dict:
    if isinstance(item, dict): return item
    if hasattr(item, 'model_dump'): return item.model_dump()
    if hasattr(item, '__dict__'): return dict(item.__dict__)
    return {}


def _content_to_blocks(content) -> list:
    if isinstance(content, str): return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list): return []
    return [b for b in (_normalize_block(item) for item in content if item is not None) if b]


def _block_content_to_str(content) -> str:
    if content is None: return ""
    if isinstance(content, str): return content
    if isinstance(content, list):
        return "\n".join(
            p for p in (item.get("text", str(item)) if isinstance(item, dict) else str(item) for item in content) if p)
    return str(content)


def _debug_msg_structure(messages) -> str:
    parts = []
    for i, msg in enumerate(messages):
        content = msg.content
        if isinstance(content, str):
            parts.append(f"[{i}]{msg.role}:text")
        elif isinstance(content, list):
            types = [_normalize_block(b).get("type", "?") for b in content]
            parts.append(f"[{i}]{msg.role}:[{','.join(types)}]")
        else:
            parts.append(f"[{i}]{msg.role}:{type(content).__name__}")
    return " | ".join(parts)


# ==========================================
# NATIVE MODE FUNCTIONS
# ==========================================
def get_at_token():
    global GLOBAL_AT_TOKEN, GLOBAL_U_PATH, GLOBAL_TOKEN_REFRESH_TIME
    print("[*] Fetching fresh SNlM0e token using active session cookies...")
    try:
        res = login_session.get("https://gemini.google.com/app", timeout=15)
        url_match = re.search(r'/(u/\d+)/', res.url)
        if url_match: GLOBAL_U_PATH = url_match.group(1) + "/"
        match = re.search(r'"SNlM0e":"(.*?)"', res.text)
        if not match: match = re.search(r'SNlM0e\\":\\"(.*?)\\"', res.text)
        if match:
            GLOBAL_AT_TOKEN = match.group(1)
            GLOBAL_TOKEN_REFRESH_TIME = time.time()
            print("[+] Native Auth Success!")
            return
    except Exception as e:
        print(f"[-] Network Error: {e}")
    print("[-] Native Auth Failed.")


def send_native_message(message, model_hex, context_ids):
    url = f"https://gemini.google.com/{GLOBAL_U_PATH}_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?hl=en&at={GLOBAL_AT_TOKEN}"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Same-Domain": "1",
        "x-goog-ext-525001261-jspb": f'[1,null,null,null,"{model_hex}",null,null,0,[4],null,null,2]'
    }
    c_id, r_id, rc_id, c_token = context_ids
    req_inner = [[message, 0, None, None, None, None, 0], ["en"],
                 [c_id, r_id, rc_id, None, None, None, None, None, None, c_token]]
    payload = {"f.req": json.dumps([None, json.dumps(req_inner)]), "at": GLOBAL_AT_TOKEN}
    resp = login_session.post(url, headers=headers, data=payload)
    return resp.text, resp.status_code


def send_guest_message(injected_message, model_hex):
    url = "https://gemini.google.com/u/0/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?hl=en"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Same-Domain": "1",
        "x-goog-ext-525001261-jspb": f'[1,null,null,null,"{model_hex}",null,null,0,[4],null,null,2]'
    }
    req_inner = [[injected_message, 0, None, None, None, None, 0], ["en"],
                 ["", "", "", None, None, None, None, None, None, ""]]
    payload = {"f.req": json.dumps([None, json.dumps(req_inner)])}
    resp = guest_session.post(url, headers=headers, data=payload)
    return resp.text, resp.status_code


def _send_to_gemini(injected_prompt, model_hex, sys_prompt, tools_prompt, full_msg_text):
    global GLOBAL_NATIVE_CONTEXT, GLOBAL_LAST_BLOCKS
    if API_MODE == "native":
        if not GLOBAL_AT_TOKEN or (time.time() - GLOBAL_TOKEN_REFRESH_TIME > 2700): get_at_token()
        raw_resp, status = send_native_message(injected_prompt, model_hex, GLOBAL_NATIVE_CONTEXT)
        if status in [400, 401, 403] or ("wrb.fr" in raw_resp and "[13]" in raw_resp):
            print(f"[-] Context Desync / Auth Error (status={status}). Auto-healing...")
            refresh_cookies_playwright()
            get_at_token()
            GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
            GLOBAL_LAST_BLOCKS = []
            heal_prompt = f"[SYSTEM INSTRUCTIONS]\n{sys_prompt.strip()}\n\n{tools_prompt}\n[LATEST MESSAGE]\n{full_msg_text}"
            heal_prompt = re.sub(r'\n{3,}', '\n\n', heal_prompt).strip()
            raw_resp, status = send_native_message(heal_prompt, model_hex, GLOBAL_NATIVE_CONTEXT)
    else:
        raw_resp, status = send_guest_message(injected_prompt, model_hex)
    return raw_resp, status


def _parse_and_build_result(raw_resp, status, model_name):
    global GLOBAL_NATIVE_CONTEXT
    parsed = parse_gemini_response(raw_resp)
    if parsed.get("context") and parsed["context"][0]: GLOBAL_NATIVE_CONTEXT = parsed["context"]

    if not parsed.get("text"):
        print(f"[-] Parser failed. Status: {status}")
        response_text = "[Proxy Error: Google rejected the prompt.]"
    else:
        response_text = parsed["text"]

    content_blocks = []
    stop_reason = "end_turn"
    response_text = response_text.replace('\\u003c', '<').replace('\\u003e', '>').replace('\u003c', '<').replace(
        '\u003e', '>')
    tool_matches = list(
        re.finditer(r'<TOOL_CALL>\s*(\{.*?\})\s*</TOOL_CALL>', response_text, re.DOTALL | re.IGNORECASE))

    if tool_matches:
        pre_text = response_text[:tool_matches[0].start()].strip()
        if pre_text: content_blocks.append({"type": "text", "text": pre_text})
        for match in tool_matches:
            try:
                tool_data = repair_and_parse_tool_json(match.group(1))
                if "input" in tool_data and isinstance(tool_data["input"], dict):
                    for key in ["command", "content", "new_string", "old_string", "file_path"]:
                        if key in tool_data["input"] and isinstance(tool_data["input"][key], str):
                            tool_data["input"][key] = tool_data["input"][key].replace('\\"', '"').replace('\\n', '\n')
                content_blocks.append({
                    "type": "tool_use", "id": generate_tool_id(), "name": tool_data.get("name"),
                    "input": tool_data.get("input", {})
                })
                stop_reason = "tool_use"
                print(f"[+] Hijacked Tool Call: {tool_data.get('name')}")
            except Exception as e:
                print(f"[-] Failed to parse tool JSON: {e}")
                content_blocks.append({"type": "text", "text": "\n[Proxy Error: Malformed Tool Call]"})
    else:
        content_blocks.append({"type": "text", "text": response_text})

    return {
        "id": f"msg_{generate_tool_id()}", "type": "message", "role": "assistant",
        "content": content_blocks, "model": model_name, "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 100}
    }


# ==========================================
# FASTAPI ANTHROPIC-COMPATIBLE ENDPOINTS
# ==========================================
class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: dict


class AnthropicMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]


class AnthropicRequest(BaseModel):
    model: str
    messages: List[AnthropicMessage]
    system: Optional[Union[str, List[Any]]] = None
    tools: Optional[List[Tool]] = None
    max_tokens: Optional[int] = 1024


@app.post("/v1/messages/count_tokens")
async def count_tokens(req: Request):
    return {"input_tokens": 100}


@app.post("/v1/messages")
def anthropic_messages(req: AnthropicRequest):
    global GLOBAL_NATIVE_CONTEXT, GLOBAL_LAST_BLOCKS, GLOBAL_PENDING_TOOL_IDS
    global GLOBAL_LAST_TOOL_USE_RESPONSE, GLOBAL_TOOL_REDELIVERY_COUNT
    model_name, model_hex = MODELS[DEFAULT_MODEL]

    req_dump = ""
    try:
        req_dump = json.dumps(req.model_dump())
    except Exception:
        req_dump = str(req.messages) + str(req.system)

    if "Generate a concise, sentence-case title" in req_dump:
        print("[*] Caught and bypassed Claude Code Title Generation Request.")
        return {"id": "msg_title_gen", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": '{"title": "Claude Code Session"}'}],
                "model": model_name, "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10}}

    with request_lock:
        print(f"\n[*] Processing request from Claude Code.")
        print(f"[D] Msg structure: {_debug_msg_structure(req.messages)}")

        if len(req.messages) == 1 and GLOBAL_NATIVE_CONTEXT[0]:
            print("[*] Claude Code started a new chat. Resetting Google Context...")
            GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
            GLOBAL_LAST_BLOCKS = []
            GLOBAL_PENDING_TOOL_IDS.clear()
            GLOBAL_LAST_TOOL_USE_RESPONSE = None
            GLOBAL_TOOL_REDELIVERY_COUNT = 0

        sys_prompt = ""
        if req.system:
            if isinstance(req.system, str):
                sys_prompt = req.system
            elif isinstance(req.system, list):
                sys_prompt = "\n".join(
                    [b.get("text", "") for b in req.system if isinstance(b, dict) and b.get("type") == "text"])
        sys_prompt = re.sub(r'<system-reminder>.*?</system-reminder>', '', sys_prompt, flags=re.DOTALL)

        tools_prompt = ""
        if req.tools:
            tools_prompt += "AVAILABLE TOOLS:\n"
            for t in req.tools:
                props = list(t.input_schema.get("properties", {}).keys())
                tools_prompt += f"- {t.name}({', '.join(props)})\n"
            tools_prompt += (
                "\nCRITICAL: To execute a tool, reply ONLY with this exact format:\n"
                "<TOOL_CALL>{\"name\": \"ToolName\", \"input\": {\"param\": \"value\"}}</TOOL_CALL>\n\n"
                "IMPORTANT RULES FOR TOOL CALLS:\n"
                "- The entire tool call including all content MUST fit inside ONE <TOOL_CALL> block.\n"
                "- When writing Python code, NEVER leave expression incomplete.\n"
                "- Use 4-space indentation encoded as spaces (not tabs).\n\n"
            )

        # -------------------------------------------------------
        # 🌟 THE UNIFIED ARRAY DELTA SLICER 🌟
        # -------------------------------------------------------
        last_msg = req.messages[-1]
        current_blocks_raw = _content_to_blocks(last_msg.content)
        current_block_sigs = [str(b) for b in current_blocks_raw]

        match_idx = 0
        if GLOBAL_NATIVE_CONTEXT[0]:
            for i in range(min(len(GLOBAL_LAST_BLOCKS), len(current_block_sigs))):
                if GLOBAL_LAST_BLOCKS[i] == current_block_sigs[i]:
                    match_idx += 1
                else:
                    break

        new_blocks = current_blocks_raw[match_idx:]
        GLOBAL_LAST_BLOCKS = current_block_sigs

        msg_to_send = ""
        has_user_text = False
        has_tool_result = False

        for block in new_blocks:
            btype = block.get("type")
            if btype == "text":
                text_val = block.get("text", "")
                if text_val.strip(): has_user_text = True
                msg_to_send += text_val + "\n"
            elif btype == "tool_use":
                msg_to_send += f"\n[You requested Tool: {block.get('name')}]\n"
            elif btype == "tool_result":
                has_tool_result = True
                raw_content = _block_content_to_str(block.get("content", ""))
                msg_to_send += f"\n[Tool Output Result]:\n{_format_tool_result(raw_content, '')}\n"

        full_msg_text = ""
        for block in current_blocks_raw:
            btype = block.get("type")
            if btype == "text":
                full_msg_text += block.get("text", "") + "\n"
            elif btype == "tool_use":
                full_msg_text += f"\n[You requested Tool: {block.get('name')}]\n"
            elif btype == "tool_result":
                raw_content = _block_content_to_str(block.get("content", ""))
                full_msg_text += f"\n[Tool Output Result]:\n{_format_tool_result(raw_content, '')}\n"

        msg_to_send = re.sub(r'<system-reminder>.*?</system-reminder>', '', msg_to_send, flags=re.DOTALL).strip()
        full_msg_text = re.sub(r'<system-reminder>.*?</system-reminder>', '', full_msg_text, flags=re.DOTALL).strip()

        if has_tool_result and not has_user_text:
            if not msg_to_send.strip(): msg_to_send = "[All tools completed with no stdout output.]\n"
            msg_to_send += "\n\n[Tools completed successfully. Provide a brief confirmation to the user of what was done.]"
            full_msg_text += "\n\n[Tools completed successfully.]"
            print(f"[*] Forwarding {len(new_blocks)} Tool Result(s) to Gemini.")
        elif not has_user_text and msg_to_send:
            stop_signal = "\n\n[Tools executed successfully. Await next user instruction before taking further actions.]"
            msg_to_send += stop_signal
            full_msg_text += stop_signal

        cache_key_full = _cache_key(str(current_block_sigs))

        if not new_blocks:
            if GLOBAL_PENDING_TOOL_IDS and GLOBAL_LAST_TOOL_USE_RESPONSE is not None:
                if GLOBAL_TOOL_REDELIVERY_COUNT < MAX_TOOL_REDELIVERY:
                    GLOBAL_TOOL_REDELIVERY_COUNT += 1
                    print(
                        f"[~] Intermediate request. Re-delivering tool_use response ({GLOBAL_TOOL_REDELIVERY_COUNT}/{MAX_TOOL_REDELIVERY}).")
                    return GLOBAL_LAST_TOOL_USE_RESPONSE
                else:
                    GLOBAL_PENDING_TOOL_IDS.clear()
                    GLOBAL_LAST_TOOL_USE_RESPONSE = None
                    GLOBAL_TOOL_REDELIVERY_COUNT = 0

            cached_full = _cache_get(cache_key_full)
            if cached_full and cached_full.get("stop_reason") != "tool_use":
                print(f"[~] No new delta. Returning cached response.")
                return cached_full

            print(f"[~] Empty delta, no pending tools. Returning idle.")
            return {
                "id": f"msg_{generate_tool_id()}", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "Standing by."}],
                "model": model_name, "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}
            }

        if not GLOBAL_NATIVE_CONTEXT[0] or match_idx == 0:
            injected_prompt = f"[SYSTEM INSTRUCTIONS]\n{sys_prompt.strip()}\n\n{tools_prompt}\n[LATEST MESSAGE]\n{full_msg_text}"
            print(f"[*] Sending Full Context block ({len(new_blocks)} new items).")
        else:
            lean_reminder = "Reminder: To execute a tool, reply EXACTLY with: <TOOL_CALL>{\"name\": \"...\", \"input\": {...}}</TOOL_CALL>\n\n"
            injected_prompt = f"{lean_reminder}[NEW EVENT]\n{msg_to_send}"
            print(
                f"[*] Sending DELTA ARRAY SLICE ({len(new_blocks)} new items) to Chat ID: {GLOBAL_NATIVE_CONTEXT[0][:10]}...")

        injected_prompt = re.sub(r'\n{3,}', '\n\n', injected_prompt).strip()
        raw_resp, status = _send_to_gemini(injected_prompt, model_hex, sys_prompt, tools_prompt, full_msg_text)
        result = _parse_and_build_result(raw_resp, status, model_name)

        if result["stop_reason"] == "tool_use":
            GLOBAL_PENDING_TOOL_IDS = {b["id"] for b in result["content"] if b.get("type") == "tool_use"}
            GLOBAL_LAST_TOOL_USE_RESPONSE = result
            GLOBAL_TOOL_REDELIVERY_COUNT = 0
        else:
            GLOBAL_PENDING_TOOL_IDS.clear()
            GLOBAL_LAST_TOOL_USE_RESPONSE = None
            _cache_set(cache_key_full, result)

        return result


if __name__ == "__main__":
    if os.name == 'nt': os.system('color')

    CHIMERA_LOGO = r"""
[96m
   ____ _   _ ___ __  __ _____ ____      _    
  / ___| | | |_ _|  \/  | ____|  _ \    / \   
 | |   | |_| || || |\/| |  _| | |_) |  / _ \  
 | |___|  _  || || |  | | |___|  _ <  / ___ \ 
  \____|_| |_|___|_|  |_|_____|_| \_\/_/   \_\
[0m
[90m==================================================[0m
[92m   PROJECT CHIMERA: Gemini -> Claude API Proxy    [0m
[90m          [ Stateful Array Slicer Active ]        [0m
[90m==================================================[0m
"""
    print(CHIMERA_LOGO)
    if API_MODE == "native": get_at_token()

    cookie_thread = threading.Thread(target=_cookie_refresh_worker, daemon=True, name="CookieWatchdog")
    cookie_thread.start()
    print("\n[*] Starting Chimera Node on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)