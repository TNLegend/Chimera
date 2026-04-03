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

load_dotenv()
# ==========================================
# CONFIGURATION
# ==========================================
API_MODE = "native"
DEFAULT_MODEL = "1"

PSID = os.getenv("GEMINI_PSID","")
PSIDTS = os.getenv("GEMINI_PSIDTS", "")
PSIDCC = os.getenv("GEMINI_PSIDCC", "")

MODELS = {
    "1": ("Gemini 3 Flash", "56fdd199312815e2"),
    "2": ("Gemini 3 Flash Thinking", "e051ce1aa80aa576"),
    "3": ("Gemini 3.1 Pro", "e6fa609c3fa255c0")
}

app = FastAPI(title="Project Chimera: Gemini-to-Claude Proxy")

login_session = requests.Session()
login_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
login_session.cookies.update({"__Secure-1PSID": PSID, "__Secure-1PSIDTS": PSIDTS, "__Secure-1PSIDCC": PSIDCC})

guest_session = requests.Session()
guest_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

GLOBAL_AT_TOKEN = None
GLOBAL_TOKEN_REFRESH_TIME = 0.0
GLOBAL_U_PATH = "u/0/"

GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
GLOBAL_LAST_BLOCKS = []

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
    except Exception as e:
        pass

    if not data["text"]:
        match = re.search(r'\\"rc_[^"]+?\\",\s*\[\\"(.*?)(?<!\\\\)\\"[,\]]', raw_text, re.DOTALL)
        if match:
            raw_msg = match.group(1)
            try:
                data["text"] = raw_msg.encode().decode('unicode-escape').encode('latin1').decode('utf-8', 'ignore')
            except:
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

    # Super-safe greedy extraction specifically to prevent the "}}" bug on file writes!
    if tool_name in ["Write", "Bash"]:
        target_key = "content" if tool_name == "Write" else "command"

        if tool_name == "Write":
            path_m = re.search(r'"file_path"\s*:\s*"([^"]+)"', raw_json)
            if path_m: input_data["file_path"] = path_m.group(1)

        # Grab everything after the key, straight to the end of the string
        content_m = re.search(rf'"{target_key}"\s*:\s*"(.*)', raw_json, re.DOTALL)
        if content_m:
            val = content_m.group(1)

            # CRITICAL FIX: If Gemini put "file_path" AFTER "content", slice it off!
            fp_idx = val.rfind('", "file_path"')
            if fp_idx != -1:
                val = val[:fp_idx]

            # Surgically strip the trailing JSON garbage (}}, }, and ")
            val = val.rstrip()
            if val.endswith('}}'):
                val = val[:-2].rstrip()
            elif val.endswith('}'):
                val = val[:-1].rstrip()
            if val.endswith('"'): val = val[:-1]

            val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            input_data[target_key] = val

        return {"name": tool_name, "input": input_data}

    # Fallback for simpler tools
    kv_pattern = r'"([^"]+)"\s*:\s*"((?:\\.|[^"\\])*)"'
    for key, val in re.findall(kv_pattern, raw_json):
        if key == "name": continue
        input_data[key] = val.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

    if tool_name and input_data:
        print(f"[~] Repaired malformed tool JSON for: {tool_name}")
        return {"name": tool_name, "input": input_data}

    raise ValueError("Could not repair tool JSON")


def _format_tool_result(content: str, tool_name: str) -> str:
    # Since we are using Delta Slicing, we can send the full result without worrying
    # about flooding Google's memory, because it gets sliced off on the next turn!
    limit = 12000
    if len(content) <= limit:
        return content
    if tool_name == "Read":
        return content[:limit] + f"\n[...truncated at {limit} chars...]"
    else:
        return f"[...truncated...]\n" + content[-limit:]


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
    global GLOBAL_NATIVE_CONTEXT, GLOBAL_LAST_BLOCKS
    model_name, model_hex = MODELS[DEFAULT_MODEL]

    req_dump = ""
    try:
        req_dump = json.dumps(req.model_dump())
    except:
        req_dump = str(req.messages) + str(req.system)

    if "Generate a concise, sentence-case title" in req_dump:
        print("[*] Caught and bypassed Claude Code Title Generation Request.")
        return {"id": "msg_title_gen", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": '{"title": "Claude Code Session"}'}], "model": model_name,
                "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}

    with request_lock:
        print(f"\n[*] Processing request from Claude Code.")

        # Detect if Claude Code cleared the terminal/history so we can reset Google too
        if len(req.messages) == 1 and GLOBAL_NATIVE_CONTEXT[0]:
            print("[*] Claude Code started a new chat. Resetting Google Context...")
            GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
            GLOBAL_LAST_BLOCKS = []

        # 1. EXTRACT CLAUDE'S NATIVE SYSTEM PROMPT
        sys_prompt = ""
        if req.system:
            if isinstance(req.system, str):
                sys_prompt = req.system
            elif isinstance(req.system, list):
                sys_prompt = "\n".join([b.get("text", "") for b in req.system if isinstance(b, dict) and b.get("type") == "text"])

        # 2. INJECT TOOLS
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
        # 🌟 THE ARRAY DELTA SLICER (100% BULLETPROOF) 🌟
        # -------------------------------------------------------
        last_msg = req.messages[-1]
        current_blocks_raw = []

        if isinstance(last_msg.content, str):
            current_blocks_raw = [{"type": "text", "text": last_msg.content}]
        elif isinstance(last_msg.content, list):
            current_blocks_raw = last_msg.content

        # Create string signatures for prefix matching
        current_block_sigs = [str(b) for b in current_blocks_raw]

        # Find how many blocks we've already sent to Google
        match_idx = 0
        if GLOBAL_NATIVE_CONTEXT[0]:
            for i in range(min(len(GLOBAL_LAST_BLOCKS), len(current_block_sigs))):
                if GLOBAL_LAST_BLOCKS[i] == current_block_sigs[i]:
                    match_idx += 1
                else:
                    break

        # Slice off everything we've already sent!
        new_blocks = current_blocks_raw[match_idx:]
        GLOBAL_LAST_BLOCKS = current_block_sigs

        # 3. BUILD THE DELTA MESSAGE (Only the new stuff)
        msg_to_send = ""
        has_user_text = False

        for block in new_blocks:
            if not isinstance(block, dict): continue
            btype = block.get("type")
            if btype == "text":
                text_val = block.get("text", "")
                if text_val.strip(): has_user_text = True
                msg_to_send += text_val + "\n"
            elif btype == "tool_use":
                msg_to_send += f"\n[You requested Tool: {block.get('name')}]\n"
            elif btype == "tool_result":
                raw_content = str(block.get('content', ''))
                msg_to_send += f"\n[Tool Output Result]:\n{_format_tool_result(raw_content, block.get('name', ''))}\n"

        # 3.5 BUILD FULL MESSAGE (Just in case we need to Auto-Heal)
        full_msg_text = ""
        for block in current_blocks_raw:
            if not isinstance(block, dict): continue
            btype = block.get("type")
            if btype == "text":
                full_msg_text += block.get("text", "") + "\n"
            elif btype == "tool_use":
                full_msg_text += f"\n[You requested Tool: {block.get('name')}]\n"
            elif btype == "tool_result":
                raw_content = str(block.get('content', ''))
                full_msg_text += f"\n[Tool Output Result]:\n{_format_tool_result(raw_content, block.get('name', ''))}\n"

        # Scrub hidden bloat
        sys_prompt = re.sub(r'<system-reminder>.*?</system-reminder>', '', sys_prompt, flags=re.DOTALL)
        msg_to_send = re.sub(r'<system-reminder>.*?</system-reminder>', '', msg_to_send, flags=re.DOTALL).strip()
        full_msg_text = re.sub(r'<system-reminder>.*?</system-reminder>', '', full_msg_text, flags=re.DOTALL).strip()

        # Stop signal to prevent autonomous looping
        if not has_user_text and msg_to_send:
            stop_signal = "\n\n[Tools executed successfully. Await next user instruction before taking further actions.]"
            msg_to_send += stop_signal
            full_msg_text += stop_signal
            print("[~] Pure tool-result message — added stop signal to prevent Gemini looping.")

        # Check Cache
        cache_key_full = _cache_key(str(current_block_sigs))
        if not msg_to_send:
            cached_full = _cache_get(cache_key_full)
            if cached_full:
                print(f"[~] No new array delta detected. Returning cached response.")
                return cached_full
            msg_to_send = "[Awaiting next instructions]"
            full_msg_text = "[Awaiting next instructions]"

        # 4. CONSTRUCT PROMPT
        if not GLOBAL_NATIVE_CONTEXT[0] or match_idx == 0:
            injected_prompt = f"[SYSTEM INSTRUCTIONS]\n{sys_prompt.strip()}\n\n{tools_prompt}\n[LATEST MESSAGE]\n{full_msg_text}"
            print(f"[*] Sending Full Context block ({len(new_blocks)} new items).")
        else:
            lean_reminder = "Reminder: To execute a tool, reply EXACTLY with: <TOOL_CALL>{\"name\": \"...\", \"input\": {...}}</TOOL_CALL>\n\n"
            injected_prompt = f"{lean_reminder}[NEW EVENT]\n{msg_to_send}"
            print(f"[*] Sending DELTA ARRAY SLICE ({len(new_blocks)} new items) to Chat ID: {GLOBAL_NATIVE_CONTEXT[0][:10]}...")

        injected_prompt = re.sub(r'\n{3,}', '\n\n', injected_prompt).strip()

        # 5. SEND TO GOOGLE
        if API_MODE == "native":
            if not GLOBAL_AT_TOKEN or (time.time() - GLOBAL_TOKEN_REFRESH_TIME > 2700):
                print("[*] Token refresh needed...")
                get_at_token()

            raw_resp, status = send_native_message(injected_prompt, model_hex, GLOBAL_NATIVE_CONTEXT)

            if status in [400, 401, 403] or ("wrb.fr" in raw_resp and "[13]" in raw_resp):
                print(f"[-] Context Desync / Auth Error (status={status}). Auto-healing...")
                get_at_token()
                GLOBAL_NATIVE_CONTEXT = ("", "", "", "")
                GLOBAL_LAST_BLOCKS = []

                # CRITICAL FIX: If we heal, we MUST send the FULL history, not just the delta!
                heal_prompt = f"[SYSTEM INSTRUCTIONS]\n{sys_prompt.strip()}\n\n{tools_prompt}\n[LATEST MESSAGE]\n{full_msg_text}"
                heal_prompt = re.sub(r'\n{3,}', '\n\n', heal_prompt).strip()
                raw_resp, status = send_native_message(heal_prompt, model_hex, GLOBAL_NATIVE_CONTEXT)
        else:
            raw_resp, status = send_guest_message(injected_prompt, model_hex)

        parsed = parse_gemini_response(raw_resp)

        if parsed.get("context") and parsed["context"][0]:
            GLOBAL_NATIVE_CONTEXT = parsed["context"]

        if not parsed.get("text"):
            print(f"[-] Parser failed. Status: {status}")
            dump_filename = f"google_error_dump_{int(time.time())}.txt"
            try:
                with open(dump_filename, "w", encoding="utf-8") as f:
                    f.write(raw_resp)
            except:
                pass
            response_text = f"[Proxy Error: Google rejected the prompt. Check {dump_filename}]"
        else:
            response_text = parsed["text"]

        # 6. PARSE RESPONSE FOR CLAUDE TOOLS
        content_blocks = []
        stop_reason = "end_turn"

        response_text = response_text.replace('\\u003c', '<').replace('\\u003e', '>').replace('\u003c', '<').replace('\u003e', '>')

        tool_matches = list(re.finditer(r'<TOOL_CALL>\s*(\{.*?\})\s*</TOOL_CALL>', response_text, re.DOTALL | re.IGNORECASE))

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
                        "type": "tool_use",
                        "id": generate_tool_id(),
                        "name": tool_data.get("name"),
                        "input": tool_data.get("input", {})
                    })
                    stop_reason = "tool_use"
                    print(f"[+] Hijacked Tool Call: {tool_data.get('name')}")
                except Exception as e:
                    print(f"[-] Failed to parse tool JSON: {e}")
                    content_blocks.append({"type": "text", "text": "\n[Proxy Error: Malformed Tool Call]"})
        else:
            content_blocks.append({"type": "text", "text": response_text})

        result = {
            "id": "msg_gemini_proxy_123",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model_name,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 100}
        }

        _cache_set(cache_key_full, result)
        return result


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
[90m             [ Stateful Delta Active ]            [0m
[90m==================================================[0m
"""

if __name__ == "__main__":
    # Enable ANSI colors on Windows just in case
    if os.name == 'nt':
        os.system('color')

    print(CHIMERA_LOGO)

    if API_MODE == "native":
        get_at_token()

    print("\n[*] Starting Chimera Node on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)