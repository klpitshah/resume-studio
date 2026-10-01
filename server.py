"""Resume Studio: a local web wrapper around the resume-tailor skill.

Runs Claude Code headless (the CLI bundled with the Claude desktop app) with
access to the resumes folder, so the skill can save PDFs where it wants to.

    python server.py            -> http://localhost:8765
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOME = Path.home()
PORT = int(os.environ.get("RESUME_STUDIO_PORT", "8765"))
RESUMES_DIR = HOME / "Documents" / "resumes"
SKILL_DIR = HOME / ".claude" / "skills" / "resume-tailor"
# The Microsoft Store build of the desktop app keeps its AppData under
# Packages\Claude_*\LocalCache, and only the app itself sees it at the normal path.
CLAUDE_APP_DIRS = [HOME / "AppData" / "Roaming" / "Claude"] + sorted(
    (HOME / "AppData" / "Local" / "Packages").glob("Claude_*/LocalCache/Roaming/Claude")
)
WORK_ROOT = Path(__file__).parent / "work"
STATIC = Path(__file__).parent / "static"
SYNC_STATE = Path(__file__).parent / "skill-sync.json"   # agent-tab file hashes at last sync
BACKUPS = Path(__file__).parent / "skill-backups"        # every version replaced by a save


# ---------------------------------------------------------------- setup ----

def find_claude():
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    versions = sorted(
        (p for d in CLAUDE_APP_DIRS for p in d.glob("claude-code/*/claude.exe")),
        key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")],
    )
    return str(versions[-1]) if versions else None


def extra_path_dirs():
    """Folders the skill's commands need (pdflatex, pdftotext, bash)."""
    candidates = [
        HOME / "AppData" / "Local" / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
        Path("C:/Program Files/MiKTeX/miktex/bin/x64"),
        Path("C:/Program Files/Git/mingw64/bin"),
        Path("C:/Program Files/Git/usr/bin"),
        Path("C:/Program Files/Git/bin"),
    ]
    candidates += sorted(Path("C:/texlive").glob("*/bin/windows")) if Path("C:/texlive").exists() else []
    return [str(p) for p in candidates if p.exists()]


def agent_skill_copies():
    """SKILL.md files for resume-tailor made in the desktop app's agent tab."""
    pattern = "local-agent-mode-sessions/skills-plugin/*/*/skills/resume-tailor/SKILL.md"
    return [p for d in CLAUDE_APP_DIRS for p in d.glob(pattern)]


def newest_agent_skill():
    newest = None
    for skill_md in agent_skill_copies():
        src = skill_md.parent
        mtime = max(f.stat().st_mtime for f in src.rglob("*") if f.is_file())
        if newest is None or mtime > newest[0]:
            newest = (mtime, src)
    return newest[1] if newest else None


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_sync_state():
    try:
        return json.loads(SYNC_STATE.read_text())
    except (OSError, ValueError):
        return {}


def sync_skill():
    """Pull changes made in the agent tab into ~/.claude/skills/resume-tailor.

    A file is copied only when the agent tab's version changed since the last
    sync, so edits made in this app survive. If both sides changed the same
    file, the local edit is kept and the file is reported as a conflict.
    """
    src = newest_agent_skill()
    if src is None:
        return {"message": "agent-tab copy not found, using " + str(SKILL_DIR), "conflicts": []}
    state = load_sync_state()
    copied, conflicts = [], []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src).as_posix()
        dest = SKILL_DIR / rel
        src_hash = file_hash(f)
        last = state.get(rel)
        if src_hash == last and dest.exists():
            continue  # agent tab hasn't touched it; keep whatever is local
        if dest.exists():
            dest_hash = file_hash(dest)
            if dest_hash == src_hash:
                state[rel] = src_hash
                continue
            if last is not None and dest_hash != last:
                conflicts.append(rel)  # both changed; local edit wins
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        state[rel] = src_hash
        copied.append(rel)
    SYNC_STATE.write_text(json.dumps(state, indent=2))
    msg = f"synced from agent tab ({', '.join(copied)} updated)" if copied else "up to date with agent tab"
    if conflicts:
        msg += f"; kept your local edits to {', '.join(conflicts)} (agent tab changed them too)"
    return {"message": msg, "conflicts": conflicts}


def take_agent_version(rel):
    """Throw away the local edit of one file and use the agent tab's copy."""
    src = newest_agent_skill()
    f = (src / rel) if src else None
    if not f or not f.is_file():
        raise FileNotFoundError(rel)
    backup_skill_file(rel)
    shutil.copy2(f, skill_path(rel))
    state = load_sync_state()
    state[rel] = file_hash(f)
    SYNC_STATE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------- skill editing -----

EDITABLE = {".md", ".tex", ".py", ".txt"}


def skill_path(rel):
    p = (SKILL_DIR / rel).resolve()
    if SKILL_DIR.resolve() not in p.parents or p.suffix not in EDITABLE:
        raise PermissionError(rel)
    return p


def list_skill_files():
    out = []
    for f in sorted(SKILL_DIR.rglob("*")):
        if f.is_file() and f.suffix in EDITABLE:
            out.append({"path": f.relative_to(SKILL_DIR).as_posix(), "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime})
    # master profile first, it's the one that changes
    out.sort(key=lambda x: (x["path"] != "assets/master-profile.md", x["path"] != "SKILL.md", x["path"]))
    return out


def backup_skill_file(rel):
    p = skill_path(rel)
    if p.exists():
        dest = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)


def save_skill_file(rel, content, base_mtime):
    p = skill_path(rel)
    if p.exists() and base_mtime is not None and abs(p.stat().st_mtime - base_mtime) > 0.001:
        return {"error": "This file changed since you opened it (maybe a sync from the agent tab). "
                         "Reload it before saving."}, 409
    backup_skill_file(rel)
    p.write_text(content, encoding="utf-8", newline="\n")
    return {"ok": True, "mtime": p.stat().st_mtime}, 200


def skill_zip():
    """The skill folder as a .zip, ready to upload back into claude.ai."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in SKILL_DIR.rglob("*"):
            if f.is_file():
                z.write(f, "resume-tailor/" + f.relative_to(SKILL_DIR).as_posix())
    return buf.getvalue()


def env_for_claude():
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(extra_path_dirs() + [env.get("PATH", "")])
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if git_bash.exists():
        env.setdefault("CLAUDE_CODE_GIT_BASH_PATH", str(git_bash))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def check_setup():
    env = env_for_claude()
    claude = find_claude()
    return {
        "claude": claude,
        "pdflatex": shutil.which("pdflatex", path=env["PATH"]),
        "pdftotext": shutil.which("pdftotext", path=env["PATH"]),
        "python": shutil.which("python", path=env["PATH"]),
        "skill": SKILL_DIR.exists() or bool(agent_skill_copies()),
        "resumes_dir": str(RESUMES_DIR),
    }


# --------------------------------------------------------------- prompts ---

def clean_company(name):
    name = re.sub(r'[\\/:*?"<>|]', "", name or "").strip()
    return name.rstrip(". ")


def first_prompt(jd, company, overwrite):
    company = clean_company(company)
    folder = (
        f'Use exactly "{company}" as the company folder name.'
        if company
        else "Pick the company folder name from the posting, as the skill describes."
    )
    if overwrite:
        exists_rule = "If a resume PDF already exists in that folder, overwrite it. I already said yes."
    else:
        exists_rule = (
            "Check the destination folder before you write anything there. If a resume PDF "
            "already exists, do not overwrite it: finish the resume in the scratch folder, leave the "
            "existing files alone, and start your final report with the line "
            "`EXISTS: <full path>` so I can tell you whether to overwrite."
        )
    return f"""Use the resume-tailor skill to build my resume for the job posting below.

This run comes from a local web app, not a chat, so you cannot stop to ask me questions mid-run.
- The skill lives at {SKILL_DIR}. Save to {RESUMES_DIR}\\<Company>\\ exactly as the skill says.
- {folder}
- {exists_rule}
- Where the skill says to ask me something, make the honest call instead (leave out what you
  can't back up from the master profile), finish the run, and list those questions at the end
  of your report under "Questions for you".
- Use a scratch folder under the current working directory for compiling.
- The job-rater skill is not needed here, skip the rating.
- Follow the skill's "Reporting back" section for your final message.

JOB POSTING:
<<<
{jd.strip()}
>>>
"""


# ------------------------------------------------------------------ jobs ---

JOBS = {}
JOBS_LOCK = threading.Lock()


class Job:
    def __init__(self, prompt, session_id=None, title=""):
        self.id = uuid.uuid4().hex[:12]
        self.prompt = prompt
        self.resume_session = session_id
        self.session_id = session_id
        self.title = title
        self.events = []  # {kind, text}
        self.status = "running"
        self.result = None
        self.cost = None
        self.proc = None
        self.started = time.time()
        self.work = WORK_ROOT / self.id
        self.work.mkdir(parents=True, exist_ok=True)

    def add(self, kind, text):
        self.events.append({"kind": kind, "text": text, "t": round(time.time() - self.started, 1)})

    def to_dict(self, since=0):
        return {
            "id": self.id,
            "status": self.status,
            "session_id": self.session_id,
            "events": self.events[since:],
            "next": len(self.events),
            "result": self.result,
            "cost": self.cost,
            "elapsed": round(time.time() - self.started),
        }


def describe_tool(name, inp):
    if name in ("Bash", "PowerShell"):
        return f"{name}: {inp.get('command', '')}".strip()
    if name in ("Read", "Write", "Edit"):
        return f"{name}: {inp.get('file_path', '')}"
    if name == "Skill":
        return f"Skill: {inp.get('skill', '')}"
    if name in ("Glob", "Grep"):
        return f"{name}: {inp.get('pattern', '')}"
    return f"{name}: {json.dumps(inp)[:200]}"


def run_job(job):
    claude = find_claude()
    if not claude:
        job.status = "error"
        job.result = "Claude Code CLI not found. Set CLAUDE_BIN to the path of claude.exe."
        return
    try:
        job.add("info", "Skill " + sync_skill()["message"])
    except Exception as e:  # keep going with whatever copy is there
        job.add("info", f"Skill sync failed ({e}), using existing copy")

    cmd = [
        claude, "-p",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Bash,PowerShell,Read,Write,Edit,Glob,Grep,Skill,TodoWrite",
        "--add-dir", str(RESUMES_DIR),
        "--add-dir", str(SKILL_DIR),
    ]
    if job.resume_session:
        cmd += ["--resume", job.resume_session]

    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        job.proc = subprocess.Popen(
            cmd, cwd=job.work, env=env_for_claude(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job.proc.stdin.write(job.prompt)
        job.proc.stdin.close()
    except Exception as e:
        job.status = "error"
        job.result = f"Could not start Claude: {e}"
        return

    stderr_lines = []
    threading.Thread(target=lambda: stderr_lines.extend(job.proc.stderr), daemon=True).start()

    for line in job.proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            job.add("info", line[:500])
            continue
        t = ev.get("type")
        if ev.get("session_id"):
            job.session_id = ev["session_id"]
        if t == "system" and ev.get("subtype") == "init":
            job.add("info", f"Session started ({ev.get('model', 'claude')})")
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    job.add("text", block["text"].strip())
                elif block.get("type") == "tool_use":
                    job.add("tool", describe_tool(block.get("name", ""), block.get("input", {})))
        elif t == "user":
            for block in ev.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    job.add("error", str(content)[:600])
        elif t == "result":
            job.result = ev.get("result") or ""
            job.cost = ev.get("total_cost_usd")
            if ev.get("is_error"):
                job.status = "error"

    job.proc.wait()
    if job.status == "cancelled":
        return
    if job.status != "error":
        job.status = "done" if job.proc.returncode == 0 else "error"
    if job.status == "error" and not job.result:
        job.result = "".join(stderr_lines)[-2000:] or f"Claude exited with code {job.proc.returncode}"
    if job.result and "Not logged in" in job.result:
        job.result += (
            "\n\nOne-time fix: open a terminal and run\n"
            f'"{claude}"\n'
            "then type /login and sign in. After that, try again here."
        )


def start_job(prompt, session_id=None, title=""):
    job = Job(prompt, session_id, title)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# ------------------------------------------------------------- resumes -----

def list_resumes():
    out = []
    if not RESUMES_DIR.exists():
        return out
    for d in RESUMES_DIR.iterdir():
        if not d.is_dir():
            continue
        pdfs = sorted(d.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not pdfs:
            continue
        out.append({
            "company": d.name,
            "files": [p.name for p in pdfs],
            "modified": pdfs[0].stat().st_mtime,
        })
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


# ---------------------------------------------------------------- http -----

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def local_host(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("localhost", "127.0.0.1")

    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        if not self.local_host():  # DNS rebinding would otherwise let a site read your profile
            return self.send_json({"error": "forbidden"}, 403)
        if p == "/api/skill/files":
            try:
                sync = sync_skill()
            except Exception as e:
                sync = {"message": f"sync failed ({e})", "conflicts": []}
            return self.send_json({"dir": str(SKILL_DIR), "files": list_skill_files(), **sync})
        if p == "/api/skill/file":
            rel = parse_qs(url.query).get("path", [""])[0]
            try:
                f = skill_path(rel)
                return self.send_json({"path": rel, "content": f.read_text(encoding="utf-8"),
                                       "mtime": f.stat().st_mtime})
            except (PermissionError, OSError):
                return self.send_json({"error": "can't read that file"}, 404)
        if p == "/api/skill/zip":
            data = skill_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="resume-tailor.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if p in ("/", "/index.html"):
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if p == "/api/setup":
            return self.send_json(check_setup())
        if p == "/api/resumes":
            return self.send_json(list_resumes())
        m = re.fullmatch(r"/api/jobs/(\w+)", p)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "no such job"}, 404)
            since = int((url.query.split("since=")[-1] or 0)) if "since=" in url.query else 0
            return self.send_json(job.to_dict(since))
        m = re.fullmatch(r"/files/([^/]+)/([^/]+\.(pdf|tex))", p)
        if m:
            f = (RESUMES_DIR / unquote(m.group(1)) / unquote(m.group(2))).resolve()
            if RESUMES_DIR.resolve() in f.parents and f.exists():
                ctype = "application/pdf" if f.suffix == ".pdf" else "text/plain; charset=utf-8"
                return self.send_file(f, ctype)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        # Block other websites from driving this: a custom header forces a CORS
        # preflight we never answer, and the Host check stops DNS rebinding.
        host = (self.headers.get("Host") or "").split(":")[0]
        if self.headers.get("X-Resume-Studio") != "1" or host not in ("localhost", "127.0.0.1"):
            return self.send_json({"error": "forbidden"}, 403)
        if p == "/api/skill/file":
            body = self.read_json()
            try:
                result, code = save_skill_file(body.get("path", ""), body.get("content", ""), body.get("mtime"))
            except PermissionError:
                result, code = {"error": "that file can't be edited here"}, 403
            return self.send_json(result, code)
        if p == "/api/skill/take-agent":
            try:
                take_agent_version(self.read_json().get("path", ""))
                return self.send_json({"ok": True})
            except (PermissionError, FileNotFoundError) as e:
                return self.send_json({"error": f"no agent-tab copy of {e}"}, 404)
        if p == "/api/generate":
            body = self.read_json()
            jd = (body.get("jd") or "").strip()
            if len(jd) < 50:
                return self.send_json({"error": "Paste the full job posting."}, 400)
            job = start_job(first_prompt(jd, body.get("company", ""), bool(body.get("overwrite"))),
                            title=body.get("company", ""))
            return self.send_json({"id": job.id})
        if p == "/api/reply":
            body = self.read_json()
            if not body.get("session_id") or not (body.get("message") or "").strip():
                return self.send_json({"error": "session_id and message required"}, 400)
            job = start_job(body["message"].strip(), session_id=body["session_id"])
            return self.send_json({"id": job.id})
        m = re.fullmatch(r"/api/jobs/(\w+)/cancel", p)
        if m and m.group(1) in JOBS:
            job = JOBS[m.group(1)]
            if job.proc and job.proc.poll() is None:
                job.status = "cancelled"
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(job.proc.pid)], capture_output=True)
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/open/([^/]+)", p)
        if m:
            d = (RESUMES_DIR / unquote(m.group(1))).resolve()
            if RESUMES_DIR.resolve() in d.parents and d.exists():
                os.startfile(d)
                return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    WORK_ROOT.mkdir(exist_ok=True)
    print(f"Resume Studio on http://localhost:{PORT}")
    print("Setup:", json.dumps(check_setup(), indent=2))
    # On Windows the default lets a second server bind the same port and the two
    # silently split requests. Fail instead.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        # 127.0.0.1 only: this server can run Claude with write access to your files.
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"\nResume Studio is already running. Open http://localhost:{PORT}")
        raise SystemExit(1)
    server.serve_forever()
