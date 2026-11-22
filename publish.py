#!/usr/bin/env python3
"""JOZOOR Studios Instagram publisher (Instagram API with Instagram Login).

Runs in GitHub Actions. Publishes every approved, due item in queue/ exactly once.

queue/<id>/meta.json   {"id", "publish_at": "2026-11-23T15:00:00Z", "approved": true,
                        "type": "REELS", "video": "video.mp4", "cover": "cover.jpg",
                        "caption": "caption.txt", "share_to_feed": true}
state/ledger.json      per-item state machine, committed after every transition:
                        HOSTED -> CONTAINER -> PUBLISHING -> PUBLISHED   (or FAILED)

Duplicate safety: media_publish is only ever called for a container whose
status_code is FINISHED. If a run dies after publishing but before saving,
the next run sees status_code == PUBLISHED and records it instead of posting again.

env: TOKEN_KEY (decrypts state/token.enc), PAGES_BASE (https://owner.github.io/repo),
     DRY_RUN=1 for a no-post test.
"""
import base64, datetime as dt, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
QUEUE, LEDGER = ROOT / "queue", ROOT / "state" / "ledger.json"
GRAPH = "https://graph.facebook.com/v24.0"   # Unified login: system-user token, never expires
IG_USER_ID = os.environ.get("IG_USER_ID", "17841480898193113")   # @jozoor.om
def _token():
    """System-user token lives encrypted in state/token.enc (AES-256, key in the TOKEN_KEY secret)."""
    if os.environ.get("IG_TOKEN"):
        return os.environ["IG_TOKEN"]
    enc = ROOT / "state" / "token.enc"
    if enc.exists() and os.environ.get("TOKEN_KEY"):
        return subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a", "-A",
                               "-in", str(enc), "-pass", "env:TOKEN_KEY"], check=True, capture_output=True,
                              text=True).stdout.strip()
    return ""


TOKEN = _token()
DRY = os.environ.get("DRY_RUN") == "1"
MAX_ATTEMPTS = 4


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(*a):
    print(f"[{now():%H:%M:%S}]", *a, flush=True)


def http(method, url, params=None, body=None, headers=None, timeout=120):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def graph(method, path, **params):
    params["access_token"] = TOKEN
    if method == "POST":
        code, res = http("POST", f"{GRAPH}/{path}", body=urllib.parse.urlencode(params).encode(),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        code, res = http("GET", f"{GRAPH}/{path}", params=params)
    if code >= 400:
        raise RuntimeError(f"Graph {method} {path} -> {code}: {res}")
    return res


# ---------- ledger (committed to git so state survives runs) ----------
def load_ledger():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}


def save_ledger(ledger, msg):
    LEDGER.parent.mkdir(exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    if os.environ.get("GITHUB_ACTIONS"):
        subprocess.run(["git", "add", str(LEDGER)], check=True, cwd=ROOT)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
            subprocess.run(["git", "commit", "-q", "-m", msg], check=True, cwd=ROOT)
            for _ in range(3):
                if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
                    return
                subprocess.run(["git", "pull", "-q", "--rebase"], cwd=ROOT)
            raise RuntimeError("could not push ledger; stopping before any further API call")


# ---------- media hosting ----------
# This repo is public with GitHub Pages serving the main branch, so every queued file
# already has a public URL. After publishing, the video is removed from the branch.
def public_url(item_dir, name):
    rel = (item_dir / name).relative_to(ROOT).as_posix()
    return f"{os.environ['PAGES_BASE'].rstrip('/')}/{rel}"


def unhost(item_dir):
    if os.environ.get("GITHUB_ACTIONS"):
        for f in ("video.mp4",):
            if (item_dir / f).exists():
                subprocess.run(["git", "rm", "-q", str(item_dir / f)], cwd=ROOT)


def wait_public(url, size, limit=600):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200 and int(r.headers.get("Content-Length", "0")) == size:
                    return True
        except Exception:
            pass
        time.sleep(15)
    raise RuntimeError(f"{url} not publicly reachable after {limit}s")


# ---------- one item ----------
def process(item_dir, meta, ledger, ig_id):
    iid = meta["id"]
    st = ledger.setdefault(iid, {"status": "NEW", "attempts": 0})
    if st["status"] in ("PUBLISHED", "FAILED"):
        return
    video, cover = item_dir / meta["video"], item_dir / meta.get("cover", "cover.jpg")
    caption = (item_dir / meta.get("caption", "caption.txt")).read_text().strip()
    if st["status"] in ("NEW", "HOSTED"):
        st["video_url"] = public_url(item_dir, meta["video"])
        st["cover_url"] = public_url(item_dir, meta.get("cover", "cover.jpg")) if cover.exists() else None
        wait_public(st["video_url"], video.stat().st_size)
        st["status"] = "HOSTED"; save_ledger(ledger, f"{iid}: hosted")
        if DRY:
            log(f"DRY RUN {iid}: media public at {st['video_url']}; skipping Instagram calls")
            st["status"] = "NEW"; return
        params = {"media_type": meta.get("type", "REELS"), "video_url": st["video_url"], "caption": caption,
                  "share_to_feed": str(meta.get("share_to_feed", True)).lower()}
        if st["cover_url"]:
            params["cover_url"] = st["cover_url"]
        st["container_id"] = graph("POST", f"{ig_id}/media", **params)["id"]
        st["status"] = "CONTAINER"; st["attempts"] += 1
        save_ledger(ledger, f"{iid}: container {st['container_id']}")

    # poll container (Meta: about once a minute, up to 5 min; this run waits up to 8)
    for _ in range(16):
        sc = graph("GET", st["container_id"], fields="status_code,status")
        code = sc.get("status_code")
        log(f"{iid}: container {code}")
        if code == "PUBLISHED":           # an earlier run published it but died before saving
            st["status"] = "PUBLISHED"; break
        if code == "FINISHED":
            if st["status"] == "PUBLISHING":
                # previous publish call outcome unknown; FINISHED means it did not go out. Safe to retry.
                pass
            st["status"] = "PUBLISHING"; save_ledger(ledger, f"{iid}: publishing")
            st["media_id"] = graph("POST", f"{ig_id}/media_publish", creation_id=st["container_id"])["id"]
            st["status"] = "PUBLISHED"; break
        if code in ("ERROR", "EXPIRED"):
            st["last_error"] = sc.get("status"); st.pop("container_id", None)
            st["status"] = "FAILED" if st["attempts"] >= MAX_ATTEMPTS else "HOSTED"
            save_ledger(ledger, f"{iid}: container {code}")
            if st["status"] == "FAILED":
                raise RuntimeError(f"{iid} failed after {st['attempts']} attempts: {st['last_error']}")
            return  # next run recreates the container
        time.sleep(30)
    else:
        save_ledger(ledger, f"{iid}: still processing"); return

    if st["status"] == "PUBLISHED":
        if st.get("media_id"):
            try:
                st["permalink"] = graph("GET", st["media_id"], fields="permalink")["permalink"]
            except Exception as e:
                log("permalink lookup failed:", e)
        st["published_at"] = now().isoformat(timespec="seconds")
        unhost(item_dir)
        save_ledger(ledger, f"{iid}: PUBLISHED {st.get('permalink', '')}")
        log(f"{iid}: PUBLISHED {st.get('permalink', '')}")


def main():
    if not TOKEN:
        sys.exit("IG_TOKEN missing")
    me = graph("GET", IG_USER_ID, fields="id,username")
    ig_id = me["id"]
    log(f"account @{me['username']} ({ig_id})")
    if me["username"].lower() != "jozoor.om":
        sys.exit(f"token belongs to @{me['username']}, expected @jozoor.om; refusing to publish")
    ledger = load_ledger()
    errors = []
    for meta_file in sorted(QUEUE.glob("*/meta.json")):
        meta = json.loads(meta_file.read_text())
        due = dt.datetime.fromisoformat(meta["publish_at"].replace("Z", "+00:00")) <= now()
        if not meta.get("approved") or not due:
            continue
        if ledger.get(meta["id"], {}).get("status") in ("PUBLISHED", "FAILED"):
            continue
        try:
            limit = graph("GET", f"{ig_id}/content_publishing_limit", fields="quota_usage,config")
            log("publishing limit:", limit.get("data"))
        except Exception as e:
            log("limit check skipped:", e)
        try:
            process(meta_file.parent, meta, ledger, ig_id)
        except Exception as e:
            log(f"ERROR {meta['id']}: {e}")
            ledger.setdefault(meta["id"], {})["last_error"] = str(e)[:500]
            save_ledger(ledger, f"{meta['id']}: error")
            errors.append(f"{meta['id']}: {e}")
        break  # one post per run: keeps spacing natural and runs short
    if errors:
        pathlib.Path(os.environ.get("RUNNER_TEMP", "/tmp"), "publish_errors.txt").write_text("\n".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()
