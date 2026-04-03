# 🐉 Project Chimera: The Gemini ↔ Claude Code Proxy

```text
   ____ _   _ ___ __  __ _____ ____      _    
  / ___| | | |_ _|  \/  | ____|  _ \    / \   
 | |   | |_| || || |\/| |  _| | |_) |  / _ \  
 | |___|  _  || || |  | | |___|  _ <  / ___ \ 
  \____|_| |_|___|_|  |_|_____|_| \_\/_/   \_\
```
**Project Chimera** is a high-performance, stateful API proxy that stitches two rival AI architectures together: it uses **Google Gemini** (via web session cookies) as the backend brain for **Anthropic's official `claude-code` CLI**.

By bridging Claude's powerful terminal automation with Gemini's reasoning capabilities, Chimera gives you a zero-cost, high-speed CLI coding assistant.

## 🔥 Key Features

* **Zero API Costs:** Uses your standard Gemini Web account (Free, Advanced, or Pro) via session cookies, completely bypassing paid API limits.
* **Full Local Tool Execution:** Seamlessly translates Claude Code's `<TOOL_CALL>` XML format into executable terminal actions. Gemini can read, write, grep, and execute bash commands directly on your machine.
* **"Delta Slicing" Algorithm:** Claude Code sends massive, concatenated history payloads that normally trigger Google's XSS/Spam filters. Chimera tracks Google's native stateful memory and uses a custom "Delta Slicer" to only send the *newest* text on each turn, keeping the prompt ultra-lean and completely bypassing Google's filters.
* **Auto-Healing Context:** If Google throws an internal `Error 13` (Context Desync) due to parallel CLI requests, Chimera instantly catches it, wipes the corrupted session ID, and reconstructs the timeline behind the scenes without breaking your terminal flow.
* **Aggressive JSON Repair:** Custom fallback parsers gracefully handle Gemini's escaped quotes and trailing JSON artifacts to ensure your Python scripts are written flawlessly.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
You need Python 3.8+ installed, along with Anthropic's official Claude Code CLI.

Clone this repository, then install the required Python packages:
```bash
pip install fastapi uvicorn requests pydantic python-dotenv
```

### 2. Extract Your Gemini Cookies
To connect to Google's backend, you need three specific session cookies from your active Google account.

1. Open your browser and log into [gemini.google.com](https://gemini.google.com/).
2. Open Developer Tools (Press `F12` or `Ctrl+Shift+I`).
3. Navigate to the **Application** tab (or **Storage** tab in Firefox).
4. On the left sidebar, expand **Cookies** and select `https://gemini.google.com`.
5. Find and copy the values for these three cookies:
   * `__Secure-1PSID`
   * `__Secure-1PSIDTS`
   * `__Secure-1PSIDCC`

### 3. Configure the `.env` File
Create a file named `.env` in the same directory as `gemini_server.py` and paste your cookies inside:

```env
GEMINI_PSID="your_1PSID_cookie_value_here"
GEMINI_PSIDTS="your_1PSIDTS_cookie_value_here"
GEMINI_PSIDCC="your_1PSIDCC_cookie_value_here"
```
> **⚠️ CRITICAL SECURITY WARNING:** Never commit your `.env` file to GitHub! Add `.env` to your `.gitignore` file immediately. 
> 
> *Note: These cookies eventually expire. If the proxy stops working or throws Auth Errors, repeat Step 2 to get fresh cookies.*

---

## 🚀 Running Project Chimera

### Step 1: Start the Proxy Server
Run the proxy script in your terminal. It will boot up the Chimera node and host a local API on port `8000`.

```bash
python gemini_server.py
```
*You should see the Chimera ASCII logo, followed by `[+] Native Auth Success!`*

### Step 2: Route Claude Code to Localhost
Open a **new, separate terminal window**. You need to tell the `claude-code` CLI to ignore Anthropic's official billing servers and route its traffic to your local Chimera proxy. 

Set the environment variables and launch Claude:

**On Windows (PowerShell):**
```powershell
$env:ANTHROPIC_BASE_URL="[http://127.0.0.1:8000/v1](http://127.0.0.1:8000/v1)"
$env:ANTHROPIC_API_KEY="chimera_bypass"
claude
```

**On Windows (CMD):**
```cmd
set ANTHROPIC_BASE_URL=[http://127.0.0.1:8000/v1](http://127.0.0.1:8000/v1)
set ANTHROPIC_API_KEY=chimera_bypass
claude
```

**On Mac/Linux (Bash/Zsh):**
```bash
export ANTHROPIC_BASE_URL="[http://127.0.0.1:8000/v1](http://127.0.0.1:8000/v1)"
export ANTHROPIC_API_KEY="chimera_bypass"
claude
```

---

## 🧠 Architecture: How it Works

Anthropic's `claude-code` CLI expects a **stateless API**, meaning it bundles the entire conversation history and all tool outputs into every single request it sends. 

Google Gemini's Web UI is strictly **stateful**. It tracks a specific `Chat ID` on their servers. If you repeatedly send Gemini the exact same history it already remembers, its internal sequence tracker desyncs, and it silently rejects your prompt as a spam/XSS attack. 

**Project Chimera solves this conflict by:**
1. Stripping all of Claude's hidden XML background rules.
2. Tracking the exact text string previously sent to Google.
3. Performing a "Delta Slice" when Claude sends its massive payload, extracting only the brand new user text and tool results.
4. Sending only that lean, sliced delta to Google's stateful endpoint.
5. Catching Google's raw JSON response, repairing any malformed `<TOOL_CALL>` artifacts, and feeding it seamlessly back to Claude's terminal UI.
```
