"""Spike checks 1 and 2: headless Codex resume (incl. after kill) and concurrency on one login.

Never prints tokens: only auth.json metadata (mtime, last_refresh, token lifetime).
"""
import base64, json, os, signal, subprocess, sys, time, threading
from concurrent.futures import ThreadPoolExecutor

W = sys.argv[1]
AUTH = os.path.expanduser("~/.codex/auth.json")


def parse(lines):
    out = []
    for line in lines.splitlines() if isinstance(lines, str) else lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def agent_text(events):
    texts = [e["item"].get("text", "") for e in events
             if e.get("type") == "item.completed" and e.get("item", {}).get("type") in ("agent_message", "assistant_message")]
    return texts[-1] if texts else None


def usage(events):
    u = [e.get("usage") for e in events if e.get("type") == "turn.completed"]
    return u[-1] if u else None


def errors(events):
    return [e.get("message") or e.get("error") for e in events if e.get("type") in ("error", "turn.failed")]


def run(args, timeout=300):
    p = subprocess.run(args, cwd=W, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, parse(p.stdout)


def auth_meta():
    st = os.stat(AUTH)
    d = json.load(open(AUTH))
    tok = (d.get("tokens") or {}).get("access_token", "")
    exp = iat = None
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        j = json.loads(base64.urlsafe_b64decode(payload))
        exp, iat = j.get("exp"), j.get("iat")
    except Exception:
        pass
    now = time.time()
    return {
        "mtime_age_min": round((now - st.st_mtime) / 60, 1),
        "last_refresh": d.get("last_refresh"),
        "token_lifetime_h": round((exp - iat) / 3600, 1) if exp and iat else None,
        "token_expires_in_h": round((exp - now) / 3600, 1) if exp else None,
    }


def log(*a):
    print(*a, flush=True)


# ---- 1a. basic headless resume -------------------------------------------
log("== 1a basic resume")
rc, ev = run(["codex", "exec", "--json", "--skip-git-repo-check",
              "Remember the code word PAPAYA. Reply with exactly: noted"])
tid = next((e.get("thread_id") for e in ev if e.get("type") == "thread.started"), None)
log("start rc", rc, "thread", tid, "reply", agent_text(ev), "errors", errors(ev))
rc, ev = run(["codex", "exec", "resume", tid, "--json", "--skip-git-repo-check",
              "What was the code word? Reply with the word only."])
log("resume rc", rc, "reply", agent_text(ev), "usage", usage(ev), "errors", errors(ev))

# ---- 1b. resume after SIGKILL mid-turn ------------------------------------
log("== 1b resume after kill mid-turn")
p = subprocess.Popen(["codex", "exec", "--json", "--skip-git-repo-check",
                      "Remember the code word MANGO. Then run the shell command `sleep 30`. "
                      "When it has finished, reply with exactly: slept"],
                     cwd=W, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     text=True, start_new_session=True)
tid2, saw_cmd, t0 = None, False, time.time()
for line in p.stdout:
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("type") == "thread.started":
        tid2 = e.get("thread_id")
    if e.get("type") == "item.started" and e.get("item", {}).get("type") == "command_execution":
        saw_cmd = True
        break
    if time.time() - t0 > 180:
        break
time.sleep(3)
os.killpg(p.pid, signal.SIGKILL)
p.wait()
log("killed after", round(time.time() - t0, 1), "s; thread", tid2, "saw command start", saw_cmd)
rc, ev = run(["codex", "exec", "resume", tid2, "--json", "--skip-git-repo-check",
              "You were interrupted. In one short line: what was the code word, and did your sleep command finish?"])
log("resume-after-kill rc", rc, "reply", agent_text(ev), "usage", usage(ev), "errors", errors(ev))

# ---- 2a. concurrent exec runs on one login ---------------------------------
log("== 2a four concurrent codex exec runs")
before = auth_meta()
log("auth before", before)


def one(n):
    t = time.time()
    rc, ev = run(["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                  f"Reply with exactly: run {n}"])
    return n, rc, agent_text(ev), errors(ev), round(time.time() - t, 1)


with ThreadPoolExecutor(4) as ex:
    for res in ex.map(one, range(1, 5)):
        log("run", res)
log("auth after", auth_meta())

# ---- 2b. one app-server, two threads, turns at the same time ---------------
log("== 2b one app-server, two concurrent turns")
srv = subprocess.Popen(["codex", "app-server"], cwd=W, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True)
lock = threading.Lock()


def send(msg):
    with lock:
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()


def recv_until(pred, deadline):
    while time.time() < deadline:
        line = srv.stdout.readline()
        if not line:
            return None
        try:
            m = json.loads(line)
        except Exception:
            continue
        if pred(m):
            return m
        recv_until.seen.append(m)
    return None


recv_until.seen = []
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "spike", "version": "0"}}})
recv_until(lambda m: m.get("id") == 1, time.time() + 30)
send({"jsonrpc": "2.0", "method": "initialized"})
threads = []
for i in (2, 3):
    send({"jsonrpc": "2.0", "id": i, "method": "thread/start", "params": {"cwd": W, "ephemeral": True}})
    r = recv_until(lambda m, i=i: m.get("id") == i, time.time() + 60)
    res = (r or {}).get("result", {})
    threads.append((res.get("thread") or {}).get("id") or res.get("threadId"))
log("threads", threads, "start result keys", list(((r or {}).get("result") or {}).keys()))
starts, done, texts = {}, {}, {}
t0 = time.time()
for n, th in enumerate(threads, start=1):
    send({"jsonrpc": "2.0", "id": 10 + n, "method": "turn/start",
          "params": {"threadId": th, "input": [{"type": "text", "text": f"Count from 1 to 40 in words, then say: thread {n} done"}]}})
deadline = time.time() + 240
while time.time() < deadline and len(done) < 2:
    line = srv.stdout.readline()
    if not line:
        break
    try:
        m = json.loads(line)
    except Exception:
        continue
    meth, params = m.get("method"), m.get("params") or {}
    th = params.get("threadId") or (params.get("turn") or {}).get("threadId")
    if meth == "turn/started":
        starts[th] = round(time.time() - t0, 1)
    elif meth == "turn/completed":
        done[th] = round(time.time() - t0, 1)
    elif meth == "item/completed" and (params.get("item") or {}).get("type") in ("agentMessage", "agent_message"):
        texts[th] = (params["item"].get("text") or "")[-40:]
    elif "error" in m:
        log("error", m["error"])
log("turn started at (s)", starts)
log("turn completed at (s)", done)
log("last words", texts)
srv.kill()
log("auth at end", auth_meta())
