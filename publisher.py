#!/usr/bin/env python3
"""Local, review-before-publish uploader for one GitHub Pages repository."""

import base64
import cgi
import hashlib
import html
import json
import re
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath


OWNER = "jamiepak"
REPO = "html-publish-hub"
BRANCH = "gh-pages"
BASE_URL = f"https://{OWNER}.github.io/{REPO}/"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
PORT = 8788
MAX_BYTES = 25 * 1024 * 1024
MAX_FILES = 100
ALLOWED = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".txt", ".xml",
    ".webmanifest", ".map", ".wasm", ".mp3", ".mp4", ".webm",
}
ROOT = Path(__file__).resolve().parent
TOKEN = ""
CSRF = secrets.token_urlsafe(32)
STAGED = {}
LOCK = threading.Lock()


class PublishError(Exception):
    pass


def github(method, path, token, data=None):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(
        API + path, data=body, method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "html-publish-hub-local",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode()).get("message", "GitHub API error")
        except (ValueError, UnicodeDecodeError):
            message = "GitHub API error"
        raise PublishError(f"GitHub 返回 {exc.code}：{message}") from None
    except urllib.error.URLError as exc:
        raise PublishError(f"无法连接 GitHub：{exc.reason}") from None


def check_token(token):
    if not token:
        raise PublishError("请先输入此仓库的 GitHub Token。")
    repo = github("GET", "", token)
    if repo.get("full_name", "").lower() != f"{OWNER}/{REPO}".lower():
        raise PublishError("连接到的仓库与配置不一致。")
    if not repo.get("permissions", {}).get("push"):
        raise PublishError("这个 Token 没有该仓库的写入权限。")
    return repo


def validate_slug(value):
    slug = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?", slug):
        raise PublishError("网址名只能使用小写英文字母、数字和中划线，最多 48 个字符。")
    return slug


def safe_path(name):
    name = name.replace("\\", "/")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise PublishError(f"不允许上传此路径：{name}")
    path = PurePosixPath(name)
    if path.is_absolute() or not name or any(part.startswith(".") for part in path.parts):
        raise PublishError(f"不允许上传此路径：{name}")
    if len(name) > 240 or any(ord(ch) < 32 for ch in name):
        raise PublishError(f"文件路径不合法：{name}")
    if path.suffix.lower() not in ALLOWED:
        raise PublishError(f"不支持此文件类型：{name}")
    return path.as_posix()


def normalize_upload(fields):
    kind = fields.getfirst("kind", "")
    title = fields.getfirst("title", "").strip()
    slug = validate_slug(fields.getfirst("slug", ""))
    if not title or len(title) > 100:
        raise PublishError("标题需填写，且不能超过 100 个字符。")
    items = fields["files"] if "files" in fields else []
    if not isinstance(items, list):
        items = [items]
    if not items or len(items) > MAX_FILES:
        raise PublishError(f"请选择 1 到 {MAX_FILES} 个文件。")
    files = {}
    skipped_hidden = 0
    for item in items:
        if not item.filename:
            continue
        parts = item.filename.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise PublishError(f"不允许上传此路径：{item.filename}")
        if any(part.startswith(".") for part in parts):
            skipped_hidden += 1
            continue
        name = safe_path(item.filename)
        content = item.file.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise PublishError("单个文件过大。")
        if name in files:
            raise PublishError(f"文件名重复：{name}")
        files[name] = content
    if not files:
        raise PublishError("没有收到文件。")
    if kind == "file":
        if len(files) != 1 or Path(next(iter(files))).suffix.lower() not in (".html", ".htm"):
            raise PublishError("单文件模式只能选择一个 HTML 文件。")
        files = {"index.html": next(iter(files.values()))}
    elif kind == "folder":
        parts = [name.split("/") for name in files]
        if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1:
            files = {"/".join(p[1:]): files["/".join(p)] for p in parts}
        if "index.html" not in files:
            raise PublishError("目录根部需要 index.html；可单独选择一个 HTML 文件发布。")
    else:
        raise PublishError("请选择 HTML 文件或静态目录。")
    if sum(map(len, files.values())) > MAX_BYTES:
        raise PublishError("文件总大小超过 25 MB。")
    try:
        files["index.html"].decode("utf-8")
    except UnicodeDecodeError:
        raise PublishError("入口 index.html 需要 UTF-8 编码。") from None
    warnings = [f"已跳过 {skipped_hidden} 个隐藏文件或目录。"] if skipped_hidden else []
    page = files["index.html"].decode("utf-8")
    if re.search(r"(?:file://|(?:localhost|127\.0\.0\.1)(?::\d+)?)", page, re.I):
        warnings.append("入口页引用了本机地址，公网访问时可能失效。")
    if re.search(r"(?:src|href)\s*=\s*[\"']/(?!/)", page, re.I):
        warnings.append("入口页使用根路径资源；项目站点位于仓库子路径下，资源可能失效。")
    if kind == "file" and re.search(r"(?:src|href)\s*=\s*[\"'](?!https?:|data:|#|/)[^\"']+", page, re.I):
        warnings.append("单文件页含相对资源引用；如有 CSS、JS 或图片，请改选整个目录。")
    digest = hashlib.sha256(files["index.html"]).hexdigest()
    return {"slug": slug, "title": title, "files": files, "digest": digest, "warnings": warnings}


def make_home(sites):
    rows = "\n".join(
        f'<li><a href="sites/{urllib.parse.quote(s["slug"])}/">{html.escape(s["title"])}</a></li>'
        for s in sorted(sites, key=lambda s: s["updated_at"], reverse=True)
    ) or "<li>还没有发布页面</li>"
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>HTML 发布站</title><style>body{font:16px/1.6 system-ui;max-width:760px;"
            "margin:8vh auto;padding:0 24px;color:#17243b}h1{font-size:2rem}li{margin:12px 0}"
            "a{color:#135db7}</style><h1>HTML 发布站</h1><p>本机静态页面的公开预览。</p>"
            f"<ul>{rows}</ul></html>").encode()


def publish(stage, token):
    check_token(token)
    try:
        ref = github("GET", f"/git/ref/heads/{BRANCH}", token)
    except PublishError as exc:
        raise PublishError(f"发布分支不可用；请检查 GitHub Pages 初始化状态。{exc}") from None
    head = ref["object"]["sha"]
    commit = github("GET", f"/git/commits/{head}", token)
    base_tree = commit["tree"]["sha"]
    old_tree = github("GET", f"/git/trees/{base_tree}?recursive=1", token)
    if old_tree.get("truncated"):
        raise PublishError("仓库文件列表过大，无法安全更新。")
    prefix = f'sites/{stage["slug"]}/'
    old_paths = {item["path"] for item in old_tree["tree"] if item["type"] == "blob" and item["path"].startswith(prefix)}
    new_paths = {prefix + name for name in stage["files"]}
    manifest_item = next((item for item in old_tree["tree"] if item["path"] == "sites.json" and item["type"] == "blob"), None)
    if manifest_item:
        blob = github("GET", f'/git/blobs/{manifest_item["sha"]}', token)
        try:
            sites = json.loads(base64.b64decode(blob["content"]).decode())
        except (ValueError, KeyError, UnicodeDecodeError):
            raise PublishError("远程站点清单无法读取，已停止发布。") from None
    else:
        sites = []
    if not isinstance(sites, list) or any(not isinstance(s, dict) or not {"slug", "title", "updated_at"} <= s.keys() for s in sites):
        raise PublishError("远程站点清单格式不正确，已停止发布。")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sites = [s for s in sites if s["slug"] != stage["slug"]]
    sites.append({"slug": stage["slug"], "title": stage["title"], "updated_at": now})
    files = {prefix + name: data for name, data in stage["files"].items()}
    files["sites.json"] = (json.dumps(sites, ensure_ascii=False, indent=2) + "\n").encode()
    files["index.html"] = make_home(sites)
    files[".nojekyll"] = b""
    tree_entries = [{"path": path, "mode": "100644", "type": "blob", "sha": None} for path in sorted(old_paths - new_paths)]
    for path, content in files.items():
        blob = github("POST", "/git/blobs", token, {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = github("POST", "/git/trees", token, {"base_tree": base_tree, "tree": tree_entries})
    new_commit = github("POST", "/git/commits", token, {
        "message": f'Publish {stage["slug"]}', "tree": tree["sha"], "parents": [head],
    })
    github("PATCH", f"/git/refs/heads/{BRANCH}", token, {"sha": new_commit["sha"], "force": False})
    return {"url": BASE_URL + f'sites/{stage["slug"]}/', "slug": stage["slug"], "commit": new_commit["sha"], "digest": stage["digest"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "HTMLPublisher/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), file=sys.stderr)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def valid_write(self):
        return (self.valid_host() and self.headers.get("Origin") in
                (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}") and
                secrets.compare_digest(self.headers.get("X-Publish-Session", ""), CSRF))

    def do_GET(self):
        if not self.valid_host():
            self.send_json(403, {"error": "只允许本机访问。"})
            return
        if self.path == "/":
            body = (ROOT / "web" / "index.html").read_text().replace("__CSRF__", CSRF).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self.send_json(200, {"repo": f"{OWNER}/{REPO}", "base_url": BASE_URL, "token_ready": bool(TOKEN)})
        elif self.path.startswith("/api/status?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                slug = validate_slug(query.get("slug", [""])[0])
                digest = query.get("digest", [""])[0]
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise PublishError("校验值不正确。")
                url = BASE_URL + f"sites/{slug}/index.html?v={secrets.token_hex(4)}"
                req = urllib.request.Request(url, headers={"User-Agent": "html-publish-hub-check", "Cache-Control": "no-cache"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as response:
                        actual = hashlib.sha256(response.read(MAX_BYTES + 1)).hexdigest()
                    self.send_json(200, {"live": actual == digest, "url": BASE_URL + f"sites/{slug}/"})
                except (urllib.error.URLError, TimeoutError):
                    self.send_json(200, {"live": False, "url": BASE_URL + f"sites/{slug}/"})
            except PublishError as exc:
                self.send_json(400, {"error": str(exc)})
        else:
            self.send_json(404, {"error": "未找到。"})

    def do_POST(self):
        global TOKEN
        if not self.valid_write():
            self.send_json(403, {"error": "本机请求验证失败。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BYTES + 2 * 1024 * 1024:
                raise PublishError("上传内容为空或超过 25 MB。")
            if self.path == "/api/preview":
                if not self.headers.get("Content-Type", "").startswith("multipart/form-data;"):
                    raise PublishError("上传格式不正确。")
                fields = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                          environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
                stage = normalize_upload(fields)
                stage_id = secrets.token_urlsafe(24)
                with LOCK:
                    STAGED.clear()
                    STAGED[stage_id] = stage
                self.send_json(200, {
                    "stage_id": stage_id, "title": stage["title"], "slug": stage["slug"],
                    "url": BASE_URL + f'sites/{stage["slug"]}/',
                    "files": [{"path": k, "bytes": len(v)} for k, v in sorted(stage["files"].items())],
                    "total_bytes": sum(map(len, stage["files"].values())),
                    "warnings": stage["warnings"], "digest": stage["digest"],
                })
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise PublishError("请求格式不正确。")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/connect":
                token = str(payload.get("token", "")).strip()
                check_token(token)
                TOKEN = token
                self.send_json(200, {"connected": True, "repo": f"{OWNER}/{REPO}"})
            elif self.path == "/api/publish":
                if payload.get("confirm_public") is not True:
                    raise PublishError("请先确认文件可以公开。")
                if not TOKEN:
                    raise PublishError("请先连接 GitHub。")
                stage_id = payload.get("stage_id", "")
                with LOCK:
                    stage = STAGED.get(stage_id)
                if not stage:
                    raise PublishError("预览已失效，请重新选择文件。")
                result = publish(stage, TOKEN)
                with LOCK:
                    STAGED.pop(stage_id, None)
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "未找到。"})
        except (PublishError, ValueError, KeyError) as exc:
            self.send_json(400, {"error": str(exc)})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"HTML 发布服务已启动：{url}")
    print("关闭此终端即可停止本机服务；已发布网页保持在线。")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
