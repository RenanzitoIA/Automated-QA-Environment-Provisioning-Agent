import os, subprocess, shutil, json, time, uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import requests

load_dotenv()

BASE = Path(os.getenv("BASE_WORKDIR", "/data/qa-envs"))
BASE.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="QA Provisioner")
STATE_DB = BASE / "state.json"
STATE = json.loads(STATE_DB.read_text()) if STATE_DB.exists() else {}

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OWNER = os.getenv("GITHUB_OWNER")
REPO = os.getenv("GITHUB_REPO")
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "qa_net")
DEFAULT_TTL = int(os.getenv("DEFAULT_TTL_MINUTES", "120"))
ALLOWED = set([s.strip() for s in os.getenv("ALLOWED_SERVICES","web,api").split(",") if s.strip()])
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN")
NGROK_REGION = os.getenv("NGROK_REGION", "us")

class ProvisionReq(BaseModel):
    branch: str
    service: str = "web"
    ttl_minutes: Optional[int] = None

class DestroyReq(BaseModel):
    env_id: str

def save_state():
    STATE_DB.write_text(json.dumps(STATE, indent=2))

def run(cmd, cwd=None, env=None):
    print("+", " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd, env=env or os.environ, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(500, f"Erro: {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()

def gh_branch_exists(branch: str) -> str:
    if not GITHUB_TOKEN or not OWNER or not REPO:
        raise HTTPException(400, "GitHub vars não configuradas")
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/git/refs/heads/{branch}"
    r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
    if r.status_code != 200:
        raise HTTPException(404, f"Branch '{branch}' não encontrada ({r.status_code})")
    return r.json()["object"]["sha"]

def ensure_network():
    try:
        run(["docker", "network", "inspect", DOCKER_NETWORK])
    except:
        run(["docker", "network", "create", DOCKER_NETWORK])

def start_ngrok(port: int) -> str:
    if not NGROK_AUTHTOKEN:
        raise HTTPException(400, "NGROK_AUTHTOKEN ausente")
    # set auth just in case
    try:
        run(["ngrok", "config", "add-authtoken", NGROK_AUTHTOKEN])
    except Exception:
        pass
    proc = subprocess.Popen(
        ["ngrok", "http", f"{port}", "--region", NGROK_REGION],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    # wait tunnel
    for _ in range(40):
        try:
            j = requests.get("http://127.0.0.1:4040/api/tunnels").json()
            tun = [t for t in j.get("tunnels",[]) if t.get("proto") == "http"]
            if tun:
                return tun[0]["public_url"]
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise HTTPException(500, "Falha ao abrir túnel ngrok")

@app.post("/provision")
def provision(req: ProvisionReq):
    if req.service not in ALLOWED:
        raise HTTPException(400, f"service inválido. Aceitos: {', '.join(ALLOWED)}")

    sha = gh_branch_exists(req.branch)
    env_id = f"{req.branch.replace('/', '-')}-{sha[:7]}-{uuid.uuid4().hex[:6]}"
    workdir = BASE / env_id
    workdir.mkdir(parents=True, exist_ok=True)

    # clone
    run(["git", "clone", f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{OWNER}/{REPO}.git", "."], cwd=workdir)
    run(["git", "checkout", req.branch], cwd=workdir)

    ensure_network()

    # Detect compose or Dockerfile
    compose = workdir / "docker-compose.qa.yml"
    if compose.exists():
        run(["docker", "compose", "-f", "docker-compose.qa.yml", "up", "-d", "--build"], cwd=workdir)
        port = 8080
    else:
        image = f"{env_id}:{req.service}"
        run(["docker", "build", "-t", image, "."], cwd=workdir)
        port = 8080
        run([
            "docker", "run", "-d", "--name", env_id,
            "--network", DOCKER_NETWORK, "-p", f"{port}:{port}",
            image
        ])

    url = start_ngrok(port)
    ttl = req.ttl_minutes or DEFAULT_TTL
    expires_at = int(time.time()) + ttl*60

    STATE[env_id] = {
        "branch": req.branch, "sha": sha, "url": url,
        "port": port, "workdir": str(workdir),
        "created_at": int(time.time()), "expires_at": expires_at
    }
    save_state()
    return {"env_id": env_id, "url": url, "expires_at": expires_at, "sha": sha}

@app.post("/destroy")
def destroy(req: DestroyReq):
    env = STATE.get(req.env_id)
    if not env: raise HTTPException(404, "env_id não encontrado")
    workdir = Path(env["workdir"])
    compose = workdir / "docker-compose.qa.yml"
    if compose.exists():
        try: run(["docker", "compose", "-f", "docker-compose.qa.yml", "down", "-v"], cwd=workdir)
        except: pass
    else:
        try: run(["docker", "rm", "-f", req.env_id])
        except: pass
    try: shutil.rmtree(workdir, ignore_errors=True)
    except: pass
    del STATE[req.env_id]
    save_state()
    return {"ok": True}

@app.get("/list")
def list_envs():
    now = int(time.time())
    items = []
    for k, v in STATE.items():
        items.append({"env_id": k, **v, "ttl_min": max(0, (v["expires_at"]-now)//60)})
    return {"environments": items}

@app.post("/gc")
def garbage_collect():
    now = int(time.time())
    killed = []
    for env_id, _ in list(STATE.items()):
        if STATE[env_id]["expires_at"] <= now:
            try:
                destroy(DestroyReq(env_id=env_id))
                killed.append(env_id)
            except Exception:
                pass
    return {"garbage_collected": killed}
