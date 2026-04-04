# 🐉 Project Chimera: The Gemini ↔ Claude Code Proxy

```text
   ____ _   _ ___ __  __ _____ ____      _    
  / ___| | | |_ _|  \/  | ____|  _ \    / \   
 | |   | |_| || || |\/| |  _| | |_) |  / _ \  
 | |___|  _  || || |  | | |___|  _ <  / ___ \ 
  \____|_| |_|___|_|  |_|_____|_| \_\/_/   \_\
```

**Project Chimera** is a high-performance, stateful API proxy that stitches two rival AI architectures together: it uses **Google Gemini** (via web session cookies) as the backend brain for **Anthropic's official `claude-code` CLI**.

By bridging Claude's powerful terminal automation with Gemini's reasoning capabilities, Chimera gives you a zero-cost, high-speed CLI coding assistant that can read, write, and execute code directly on your local machine.

---

## 🔥 Key Features

* **Zero API Costs:** Uses your standard Gemini Web account via session cookies, completely bypassing paid API limits.
* **Playwright Cookie Watchdog:** Includes a background headless browser that silently wakes up at randomized intervals (20-40 minutes) to ping Google, rotate your session tokens, and keep your authentication alive indefinitely without triggering bot detection.
* **"Delta Slicing" Algorithm:** Claude Code sends massive, concatenated history payloads that normally trigger Google's XSS/Spam filters. Chimera tracks Google's native stateful memory and uses a custom "Delta Slicer" to only send the *newest* text on each turn, keeping the prompt ultra-lean.
* **Bulletproof Tool Parsing:** Custom fallback parsers gracefully handle Gemini's escaped quotes, hallucinatory JSON formatting, and identical-string `Edit` bugs to ensure your Python scripts and terminal commands are executed flawlessly.
* **Auto-Healing Context:** If Google throws an internal `Error 13` (Context Desync), Chimera instantly catches it, fires a Playwright cookie refresh, and reconstructs the timeline behind the scenes without breaking your terminal flow.

---

## ⚠️ CRITICAL: The "Evil Clone" Logout Problem

Google's security backend is highly sensitive to concurrent sessions. If you extract cookies from your **main personal Chrome profile** and run this proxy, Google will see two identical sessions doing different things at the exact same time (one from your real browser, one from the headless Playwright browser). 

Google will assume your session was hijacked by a hacker and will immediately hit the kill switch, **logging you out of your Google account across all devices.**

**THE SOLUTION:** You must create a **Burner Chrome Profile** (or use an entirely separate burner Google account) specifically for this proxy. 
1. Open Chrome, click your Profile icon in the top right, and click **Add**.
2. Set up a new profile.
3. Log into Gemini on this new profile. Keep this profile closed when you aren't actively getting cookies.

---

## 🛠️ Installation & Setup

### 1. Install Dependencies
You need Python 3.8+ installed, along with Anthropic's official Claude Code CLI. Clone this repository, then install the required Python packages and the Playwright headless browser:

```bash
pip install fastapi uvicorn requests pydantic python-dotenv playwright
playwright install chromium
```

### 2. Extract Your Gemini Cookies (The Right Way)
We use the **Cookie-Editor** extension to grab a clean JSON export of your session.

1. Install the **Cookie-Editor** extension in your Burner Chrome Profile ([Chrome Web Store Link](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)).
2. Go to [gemini.google.com](https://gemini.google.com/) and ensure you are logged in.
3. Click the Cookie-Editor extension icon in your browser toolbar.
4. Click **Export** -> **Export as JSON** (This copies the data to your clipboard).
5. In your project directory, create a file named `cookies_raw.json` and paste the clipboard contents into it.

### 3. Generate the Auth State
Run the setup script. This script acts as a sanitizer—it reads `cookies_raw.json`, cleans up any invalid formatting (like bad `sameSite` tags), and generates a perfect `auth_state.json` file for Playwright to use.

```bash
python setup.py
```
*You should see: `[+] Saved to auth_state.json`*

## 🚀 Running Project Chimera

### Step 1: Start the Proxy Server
Run the proxy script in your terminal. It will load your `auth_state.json`, start the background Cookie Watchdog, and host a local API on port `8000`.

```bash
python gemini_server.py
```
*You should see the Chimera ASCII logo, followed by `[*] Cookie Watchdog started`.*

### Step 2: Route Claude Code to Localhost
Open a **new, separate terminal window**. You need to tell the `claude-code` CLI to ignore Anthropic's official billing servers and route its traffic to your local Chimera proxy. 

Set the environment variables and launch Claude:

**On Windows (PowerShell):**
```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8000/v1"
$env:ANTHROPIC_API_KEY="chimera_bypass"
claude
```

**On Windows (CMD):**
```cmd
set ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1
set ANTHROPIC_API_KEY=chimera_bypass
claude
```

**On Mac/Linux (Bash/Zsh):**
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8000/v1"
export ANTHROPIC_API_KEY="chimera_bypass"
claude
```

---

## 🧠 Architecture: How it Works

Anthropic's `claude-code` CLI expects a **stateless API**, meaning it bundles the entire conversation history and all tool outputs into every single request it sends. 

Google Gemini's Web UI is strictly **stateful**. It tracks a specific `Chat ID` on their servers. If you repeatedly send Gemini the exact same history it already remembers, its internal sequence tracker desyncs, and it silently rejects your prompt as a spam/XSS attack. 

**Project Chimera solves this conflict by:**
1. **Delta Slicing:** Tracking the exact array of blocks sent to Google, comparing it against Claude's incoming payload, and extracting *only* the new messages and tool results to send forward.
2. **Watchdog Persistence:** Playwright runs silently in the background, injecting your `auth_state.json` into a headless Chromium instance, visiting Gemini to force Google to issue fresh `TS` (Timestamp) cookies, and saving them back to the JSON file so your session never dies.
3. **Regex Repair:** Gemini occasionally hallucinates unescaped quotes inside JSON strings. Chimera intercepts the raw text stream and surgically repairs malformed `<TOOL_CALL>` blocks before passing them to Claude for local execution.