from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import json
import re
import uuid
from pathlib import Path

app = Flask(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DB_DIR = Path("dbhistory")
DB_DIR.mkdir(parents=True, exist_ok=True)

FILES_DIR = Path("files")
FILES_DIR.mkdir(parents=True, exist_ok=True)

def get_history_path(session_id: str) -> Path:
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    return DB_DIR / f"{safe_id}.json"

def load_history(session_id: str) -> list:
    path = get_history_path(session_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_history(session_id: str, messages: list):
    path = get_history_path(session_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

def process_file_blocks(text: str) -> str:
    pattern1 = r"\[FILE\]\s*filename:\s*(.+?)\s*content:\s*(.*?)\s*\[/FILE\]"
    matches = re.findall(pattern1, text, re.DOTALL | re.IGNORECASE)

    if not matches:
        filename_match = re.search(r"(?:Filename|File name|file):\s*[`*]*(.+?\.\w+)[`*]*", text, re.IGNORECASE)
        content_match = re.search(r"```(?:\w+)?\n(.*?)\n```", text, re.DOTALL)
        if filename_match and content_match:
            matches = [(filename_match.group(1), content_match.group(1))]

    for filename, content in matches:
        filename = filename.strip().strip("`* ")
        content = content.strip()

        unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        file_path = FILES_DIR / unique_name

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        download_url = f"/files/{unique_name}"
        text = f"📄 File ready: **{filename}**\n\nDownload here:\n{download_url}"

    return text

def call_openrouter(messages, model="openrouter/auto"):
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY environment variable is missing")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://railway.app",
        "X-Title": "NetErrror AI"
    }

    payload = {
        "model": model,
        "messages": messages
    }

    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id", "").strip()
    model = data.get("model", "openrouter/auto")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    # If session_id is blank → create a new one
    if not session_id:
        session_id = uuid.uuid4().hex[:8]

    messages = load_history(session_id)

    system_prompt = (
        "You are a helpful AI assistant that can send files.\n\n"
        "CRITICAL RULE: When the user asks you to send, create, or give a file, "
        "you MUST reply using EXACTLY this format and nothing else:\n\n"
        "[FILE]\n"
        "filename: exact_filename_here.txt\n"
        "content:\n"
        "the full content of the file\n"
        "[/FILE]\n\n"
        "Do NOT add any extra text, roleplay, markdown, or explanations outside the [FILE] block. "
        "Only use this format when you are actually sending a file."
    )

    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": system_prompt})
    else:
        messages[0]["content"] = system_prompt

    messages.append({"role": "user", "content": user_message})

    try:
        reply = call_openrouter(messages, model)
        reply = process_file_blocks(reply)

        # Add session info at the bottom
        reply += f"\n\n──────────────\nSession: {session_id}"

        messages.append({"role": "assistant", "content": reply})
        save_history(session_id, messages)

        return jsonify({
            "reply": reply,
            "session_id": session_id
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin_send", methods=["POST", "GET"])
def admin_send():
    return jsonify({"error": "Use /chat with session_id instead"}), 400

@app.route("/files/<path:filename>")
def download_file(filename):
    return send_from_directory(FILES_DIR, filename, as_attachment=True)

@app.route("/get_history", methods=["POST"])
def get_history():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    messages = load_history(session_id)
    return jsonify({
        "history": messages,
        "count": len(messages),
        "session_id": session_id
    })

@app.route("/get_all_history", methods=["GET", "POST"])
def get_all_history():
    """Returns history from ALL sessions combined"""
    all_chats = {}
    total_messages = 0

    for file in DB_DIR.glob("*.json"):
        session_id = file.stem
        try:
            with open(file, "r", encoding="utf-8") as f:
                messages = json.load(f)
                all_chats[session_id] = messages
                total_messages += len(messages)
        except Exception:
            continue

    return jsonify({
        "sessions": all_chats,
        "total_sessions": len(all_chats),
        "total_messages": total_messages
    })

@app.route("/clear", methods=["POST"])
def clear_history():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    path = get_history_path(session_id)
    if path.exists():
        path.unlink()
    return jsonify({"status": "cleared", "session_id": session_id})

@app.route("/clear_all", methods=["POST"])
def clear_all_history():
    """Deletes ALL chat sessions"""
    count = 0
    for file in DB_DIR.glob("*.json"):
        file.unlink()
        count += 1
    return jsonify({"status": "all cleared", "deleted_sessions": count})

@app.route("/")
def home():
    return "OK", 200
