import os
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq
import json
from datetime import datetime

app = Flask(__name__)

# ===== CONFIG =====
app.secret_key = os.getenv("SECRET_KEY", "change-this-in-production-please")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///blank.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ===== DB =====
db = SQLAlchemy(app)

class User(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    sessions = db.relationship("ChatSession", backref="user", lazy=True, cascade="all, delete-orphan")

class ChatSession(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title      = db.Column(db.String(120), default="New Chat")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages   = db.relationship("Message", backref="chat_session", lazy=True, cascade="all, delete-orphan")

class Message(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("chat_session.id"), nullable=False)
    role       = db.Column(db.String(20), nullable=False)   # "user" or "assistant"
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ===== GROQ =====
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("Missing GROQ_API_KEY environment variable")

client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = (
    "Your name is Blank_. "
    "You were created and developed by Athrv RG. "
    "Don't mention your name and creator unless asked by the user. "
    "Be a friendly assistant and assist the user."
)

# ===== HELPERS =====
def logged_in():
    return "user_id" in session

def current_user():
    if not logged_in():
        return None
    return User.query.get(session["user_id"])

# ===== AUTH ROUTES =====
@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template("index.html", username=session.get("username"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, mode="login")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif User.query.filter_by(username=username).first():
            error = "Username already taken."
        else:
            user = User(
                username=username,
                password=generate_password_hash(password)
            )
            db.session.add(user)
            db.session.commit()
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))
    return render_template("login.html", error=error, mode="signup")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ===== CHAT SESSION ROUTES =====
@app.route("/sessions", methods=["GET"])
def get_sessions():
    if not logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    user = current_user()
    sessions_list = []
    for s in sorted(user.sessions, key=lambda x: x.created_at, reverse=True):
        sessions_list.append({
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at.strftime("%b %d, %Y")
        })
    return jsonify({"sessions": sessions_list})

@app.route("/sessions/new", methods=["POST"])
def new_session():
    if not logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    user = current_user()
    s = ChatSession(user_id=user.id, title="New Chat")
    db.session.add(s)
    db.session.commit()
    return jsonify({"id": s.id, "title": s.title})

@app.route("/sessions/<int:session_id>", methods=["GET"])
def get_session(session_id):
    if not logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    s = ChatSession.query.filter_by(id=session_id, user_id=session["user_id"]).first()
    if not s:
        return jsonify({"error": "Not found"}), 404
    messages = [{"role": m.role, "content": m.content} for m in s.messages]
    return jsonify({"id": s.id, "title": s.title, "messages": messages})

@app.route("/sessions/<int:session_id>", methods=["DELETE"])
def delete_session(session_id):
    if not logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    s = ChatSession.query.filter_by(id=session_id, user_id=session["user_id"]).first()
    if not s:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(s)
    db.session.commit()
    return jsonify({"ok": True})

# ===== CHAT ROUTE =====
@app.route("/chat", methods=["POST"])
def chat():
    if not logged_in():
        return jsonify({"error": "Unauthorized"}), 401

    data       = request.get_json(force=True)
    user_msg   = data.get("message", "").strip()
    history    = data.get("history", [])
    model      = data.get("model", "llama-3.3-70b-versatile")
    session_id = data.get("session_id")

    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # Build messages for Groq
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_msg})

    try:
        res = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
        reply = res.choices[0].message.content.strip()

        # Save to DB if session_id provided
        if session_id:
            s = ChatSession.query.filter_by(id=session_id, user_id=session["user_id"]).first()
            if s:
                # Auto-title from first user message
                if len(s.messages) == 0:
                    s.title = user_msg[:60] + ("…" if len(user_msg) > 60 else "")
                db.session.add(Message(session_id=s.id, role="user",      content=user_msg))
                db.session.add(Message(session_id=s.id, role="assistant", content=reply))
                db.session.commit()

        return jsonify({"reply": reply})

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "AI request failed"}), 500

# ===== INIT DB & RUN =====
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
