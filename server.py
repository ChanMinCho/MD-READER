#!/usr/bin/env python3
"""로컬 마크다운 뷰어. 표준 라이브러리만 사용한다.

사용법:
    python3 server.py                 # 이 PC 에서만 (127.0.0.1)
    python3 server.py FILE.md         # 특정 파일을 열면서 실행
    python3 server.py --no-open       # 브라우저 자동 실행 안 함
    python3 server.py --lan DIR       # 같은 네트워크의 다른 PC 에도 공개

기본은 127.0.0.1 에만 바인딩하므로 외부에서 접근할 수 없다.

--lan 을 쓰면 외부에 열리는데, 이 서버의 /api/read 는 임의 경로를 읽으므로
그대로 열면 같은 네트워크의 누구나 이 PC 의 파일을 가져갈 수 있다. 그래서
--lan 은 (1) 공개할 디렉터리를 반드시 인자로 받고 그 밖은 읽지 않으며,
(2) 매 실행마다 임의 토큰을 만들어 토큰 없는 API 요청을 거부한다.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".mdown", ".mkd", ""}
MAX_BYTES = 20 * 1024 * 1024
START_PORT = 7654

TOKEN = None      # --lan 일 때만 생성된다
SHARE_ROOT = None  # --lan 일 때 읽기를 허용할 디렉터리

# 최근 파일·테마는 서버에 둔다. localStorage 는 127.0.0.1:포트 단위로 격리되는데
# 포트는 자동으로 바뀌므로, 브라우저에 저장하면 포트가 달라질 때마다 초기화된다.
STATE_FILE = ROOT / "state.json"
STATE_LOCK = threading.Lock()
MAX_RECENT = 15

# 실행 중인 인스턴스를 찾을 수 있도록 포트와 pid 를 남긴다 (md-reader 스킬이 읽는다)
PORT_FILE = ROOT / ".port"

# 다른 PC 공개 토큰은 한 번 만들어 두고 다시 켤 때도 같은 것을 쓴다(주소가 안 바뀌게).
# 뷰어 폴더 밖(~/.config)에 둬서 정적 파일로 새어 나가지 않게 한다.
TOKEN_FILE = Path.home() / ".config" / "mdview" / "lan-token"


def lan_token():
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(16)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    return t


# 드래그앤드롭한 파일의 원본 경로를 되찾을 때 뒤지지 않을 디렉터리
SKIP_DIRS = {
    "Library", "Applications", "node_modules", ".git", ".Trash", ".gradle",
    ".npm", ".cache", "venv", ".venv", "build", "dist", "Pods", "target",
    "__pycache__", ".idea", "Photos Library.photoslibrary",
}


def locate_file(name, size, mtime_ms, budget=2.5):
    """이름·크기·수정시각이 맞는 파일을 디스크에서 찾는다.

    브라우저는 드롭한 파일의 절대경로를 주지 않는다. 세 값이 모두 맞으면
    사실상 그 파일이므로, 흔한 위치를 시간 제한을 두고 훑어 경로를 되찾는다.

    size 가 None 이면 이름만으로 찾는다(최근 목록의 낡은 항목 복구용).

    한글 파일명 주의: macOS 브라우저는 file.name 을 NFD(자모 분리)로 주는데
    디스크에는 NFC 로 저장돼 있는 경우가 많다. 파일 열기는 APFS 가 정규화를
    무시해 주지만 파이썬 문자열 비교는 깨지므로, 양쪽을 NFC 로 맞춰 비교한다.
    """
    if not name:
        return None
    want = unicodedata.normalize("NFC", name)
    home = Path.home()
    # (뒤질 위치, 최대 깊이) — 자주 쓰는 곳을 먼저, 홈 전체는 얕게만
    roots = [
        (home / "Downloads", 6), (home / "Desktop", 6), (home / "Documents", 6),
        (home / ".claude", 6), (home / "Developer", 5), (home, 2),
    ]
    deadline = time.time() + budget
    fallback = None
    seen = set()

    for root, max_depth in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if time.time() > deadline:
                return fallback
            d = Path(dirpath)
            if d in seen:
                dirnames[:] = []
                continue
            seen.add(d)
            if len(d.parts) - base_depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    x for x in dirnames
                    if x not in SKIP_DIRS and not (x.startswith(".") and x != ".claude")
                ]
            if want in filenames:
                real = want
            else:
                real = next(
                    (f for f in filenames if unicodedata.normalize("NFC", f) == want),
                    None,
                )
                if real is None:
                    continue
            hit = d / real
            try:
                st = hit.stat()
            except OSError:
                continue
            if size is not None and st.st_size != size:
                continue
            if size is not None and mtime_ms and abs(st.st_mtime * 1000 - mtime_ms) < 2000:
                return str(hit)      # 이름·크기·수정시각 일치 — 확실하다
            fallback = fallback or str(hit)   # 일부만 일치 — 후보로 남긴다
    return fallback


def load_state():
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"recent": s.get("recent", []), "theme": s.get("theme", ""), "buddy": s.get("buddy", {}), "bg": s.get("bg", "field"), "bgAlpha": s.get("bgAlpha", 0.12), "bgMotion": s.get("bgMotion", True), "editor": s.get("editor", {}), "pos": s.get("pos", {}), "pins": s.get("pins", []), "tabs": s.get("tabs", {}), "read": s.get("read", {}), "accent": s.get("accent", {})}
    except (OSError, ValueError):
        return {"recent": [], "theme": "", "buddy": {}, "bg": "field"}


def save_state(state):
    with STATE_LOCK:
        cur = load_state()
        cur.update({k: v for k, v in state.items() if k in ("recent", "theme", "buddy", "bg", "bgAlpha", "bgMotion", "editor", "pos", "pins", "tabs", "read", "accent")})
        cur["recent"] = cur["recent"][:MAX_RECENT]
        try:
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except OSError:
            pass
        return cur


def expand(raw):
    """사용자가 입력한 경로를 실제 경로로 편다.

    따옴표, 백슬래시 이스케이프(Finder 에서 드래그하면 붙는다), ~ 를 처리한다.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    s = s.replace("\\ ", " ").replace("\\~", "~")
    if s.startswith("file://"):
        from urllib.parse import unquote

        s = unquote(s[7:])
    return Path(os.path.expandvars(os.path.expanduser(s)))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # 뷰어 파일을 고쳐도 브라우저가 옛 버전을 계속 쓰지 않도록 캐시를 끈다.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, q):
        """--lan 모드에서는 토큰이 맞아야 API 를 쓸 수 있다."""
        if TOKEN is None:
            return True
        given = (q.get("t") or [""])[0] or self.headers.get("X-Token", "")
        return secrets.compare_digest(given, TOKEN)

    def _allowed(self, p):
        """--lan 모드에서는 공개 디렉터리 밖의 경로를 거부한다."""
        if SHARE_ROOT is None:
            return True
        try:
            return SHARE_ROOT == p or SHARE_ROOT in p.resolve().parents
        except OSError:
            return False

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            q = parse_qs(url.query)
            if not self._authorized(q):
                return self._json({"error": "토큰이 필요합니다."}, 401)
            if url.path == "/api/read":
                return self.api_read(q)
            if url.path == "/api/complete":
                return self.api_complete(q)
            if url.path == "/api/initial":
                return self._json({"path": INITIAL_FILE})
            if url.path == "/api/state":
                return self._json(load_state())
            if url.path == "/api/locate":
                return self.api_locate(q)
            if url.path == "/api/file":
                return self.api_file(q)
            if url.path == "/api/search":
                return self.api_search(q)
            if url.path == "/api/dirs":
                return self.api_dirs(q)
            return self._json({"error": "없는 API"}, 404)
        if TOKEN is not None and not (url.path in ("/", "/index.html") or url.path.startswith("/vendor/")):
            return self._json({"error": "없는 경로"}, 404)
        # If-Modified-Since 가 있으면 304 가 나가 옛 파일이 재사용된다. 떼고 처리한다.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        if "If-None-Match" in self.headers:
            del self.headers["If-None-Match"]
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ("/api/state", "/api/save-html", "/api/save-md", "/api/save-pdf"):
            return self._json({"error": "없는 API"}, 404)
        if not self._authorized(parse_qs(url.query)):
            return self._json({"error": "토큰이 필요합니다."}, 401)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "잘못된 요청"}, 400)
        if not isinstance(body, dict):
            body = {}
        if url.path == "/api/save-html":
            return self.api_save_html(body)
        if url.path == "/api/save-md":
            return self.api_save_md(body)
        if url.path == "/api/save-pdf":
            return self.api_save_pdf(body)
        return self._json(save_state(body))

    def api_save_html(self, body):
        """열린 md 옆에 같은 이름의 .html 을 쓴다. 다른 PC 공개(--lan) 중에는 쓰기를 막는다."""
        if TOKEN is not None:
            return self._json({"error": "다른 PC 공개 중에는 파일을 저장하지 않습니다."}, 403)
        html = body.get("html")
        if not isinstance(html, str) or not html:
            return self._json({"error": "내용이 비어 있습니다."}, 400)
        out = self._pick_out(body, (".html", ".htm"))
        if isinstance(out, tuple):
            return self._json(*out)
        if out is None:
            src = Path(str(body.get("path") or ""))
            if not src.is_absolute() or src.suffix.lower() not in TEXT_SUFFIXES - {""} or not src.is_file():
                return self._json({"error": f"원본 마크다운 파일이 없습니다: {src}"}, 404)
            out = src.with_suffix(".html")
        if out.exists() and not body.get("overwrite"):
            return self._json({"exists": True, "path": str(out)}, 409)
        try:
            out.write_text(html, encoding="utf-8")
        except OSError as e:
            return self._json({"error": f"저장하지 못했습니다: {e}"}, 400)
        return self._json({"path": str(out), "size": out.stat().st_size})

    BROWSERS = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    @classmethod
    def find_browser(cls):
        for c in cls.BROWSERS:
            if Path(c).is_file():
                return c
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge", "chrome"):
            found = shutil.which(n)
            if found:
                return found
        return None

    def _pick_out(self, body, suffixes):
        """저장 창에서 고른 경로(out). 없으면 None, 잘못되면 (오류, 코드)."""
        raw = body.get("out")
        if not raw:
            return None
        out = Path(os.path.abspath(expand(str(raw))))
        if out.suffix.lower() not in suffixes:
            out = out.with_name(out.name + suffixes[0])
        if not out.parent.is_dir():
            return ({"error": f"폴더가 없습니다: {out.parent}"}, 400)
        if not self._allowed(out.parent):
            return ({"error": "공개 범위 밖의 경로입니다."}, 403)
        if out.is_dir():
            return ({"error": "같은 이름의 폴더가 있습니다."}, 400)
        return out

    def api_save_pdf(self, body):
        """인쇄 창 없이 PDF 를 만든다: 받은 HTML 을 크롬(또는 엣지) 헤드리스로 출력한다. md 옆, 경로가 없으면 다운로드 폴더."""
        if TOKEN is not None:
            return self._json({"error": "다른 PC 공개 중에는 파일을 저장하지 않습니다."}, 403)
        html = body.get("html")
        if not isinstance(html, str) or not html:
            return self._json({"error": "내용이 비어 있습니다."}, 400)
        out = self._pick_out(body, (".pdf",))
        if isinstance(out, tuple):
            return self._json(*out)
        src = body.get("path")
        if out is None and isinstance(src, str) and src:
            sp = Path(src)
            if sp.is_absolute() and sp.is_file() and sp.suffix.lower() in TEXT_SUFFIXES - {""}:
                out = sp.with_suffix(".pdf")
        if out is None:
            name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", str(body.get("name") or "문서")).strip()[:80] or "문서"
            out = Path.home() / "Downloads" / (name + ".pdf")
        if out.exists() and not body.get("overwrite"):
            return self._json({"exists": True, "path": str(out)}, 409)
        browser = self.find_browser()
        if not browser:
            return self._json({"error": "크롬이나 엣지를 찾지 못해 PDF 를 만들 수 없습니다. 인쇄 창(⌘P)에서 PDF로 저장해 주세요."}, 500)
        with tempfile.TemporaryDirectory(prefix="mdview-pdf-") as d:
            page = Path(d) / "doc.html"
            pdf = Path(d) / "out.pdf"
            page.write_text(html, encoding="utf-8")
            cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--no-pdf-header-footer", f"--user-data-dir={Path(d) / 'profile'}", f"--print-to-pdf={pdf}",
                   "--virtual-time-budget=5000", page.as_uri()]
            try:
                subprocess.run(cmd, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return self._json({"error": "PDF 를 만드는 데 너무 오래 걸려 멈췄습니다."}, 500)
            except OSError as e:
                return self._json({"error": f"브라우저를 실행하지 못했습니다: {e}"}, 500)
            if not pdf.is_file() or pdf.stat().st_size == 0:
                return self._json({"error": "PDF 를 만들지 못했습니다."}, 500)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pdf, out)
            except OSError as e:
                return self._json({"error": f"저장하지 못했습니다: {e}"}, 400)
        return self._json({"path": str(out), "size": out.stat().st_size})

    DIRS_LIMIT = 400

    def api_dirs(self, q):
        """저장할 곳을 고르는 창용: 폴더 하나의 하위 폴더와 글 파일 이름을 돌려준다."""
        raw = (q.get("path") or [""])[0].strip() or "~"
        p = expand(raw)
        if not p.is_absolute():
            p = Path.home() / p
        p = Path(os.path.abspath(p))
        while not p.is_dir() and p.parent != p:      # 없는 폴더면 있는 윗폴더까지 올라간다
            p = p.parent
        if not self._allowed(p):
            return self._json({"error": f"공개 범위({SHARE_ROOT}) 밖의 경로입니다."}, 403)
        dirs, files = [], []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    dirs.append(child.name)
                elif child.suffix.lower() in TEXT_SUFFIXES - {""}:
                    files.append(child.name)
        except PermissionError:
            return self._json({"error": f"읽기 권한이 없습니다: {p}"}, 403)
        except OSError as e:
            return self._json({"error": f"읽지 못했습니다: {e}"}, 400)
        return self._json({
            "path": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "home": str(Path.home()),
            "dirs": dirs[: self.DIRS_LIMIT],
            "files": files[: self.DIRS_LIMIT],
            "more": len(dirs) > self.DIRS_LIMIT or len(files) > self.DIRS_LIMIT,
        })

    def api_save_md(self, body):
        """마크다운 글을 파일에 쓴다. create 면 새 파일, 아니면 이미 있는 파일을 고쳐 쓴다.

        고쳐 쓸 때는 화면이 읽어 간 수정시각(mtime)을 함께 받아, 그 사이 파일이 바뀌었으면
        덮어쓰지 않고 409 를 돌려준다. 다른 PC 공개(--lan) 중에는 쓰기를 막는다.
        """
        if TOKEN is not None:
            return self._json({"error": "다른 PC 공개 중에는 파일을 저장하지 않습니다."}, 403)
        raw = str(body.get("path") or "")
        text = body.get("text")
        if not raw.strip() or not isinstance(text, str):
            return self._json({"error": "경로나 내용이 비어 있습니다."}, 400)
        p = expand(raw)
        if not p.is_absolute():
            p = Path.home() / p
        create = bool(body.get("create"))
        if create and p.suffix == "":
            p = p.with_name(p.name + ".md")
        if p.suffix.lower() not in TEXT_SUFFIXES - {""}:
            return self._json({"error": f"마크다운·텍스트 파일만 저장합니다: {p.name}"}, 400)
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:
            return self._json({"error": "내용이 너무 큽니다."}, 400)
        try:
            if create:
                if p.exists() and not body.get("overwrite"):
                    return self._json({"exists": True, "path": str(p), "error": f"이미 있는 파일입니다: {p}"}, 409)
                if not p.parent.is_dir():
                    return self._json({"error": f"폴더가 없습니다: {p.parent}"}, 400)
            else:
                if not p.is_file():
                    return self._json({"error": f"파일이 없습니다: {p}"}, 404)
                base = body.get("mtime")
                if isinstance(base, (int, float)) and not body.get("force") and abs(p.stat().st_mtime - base) > 0.0005:
                    return self._json({"changed": True, "mtime": p.stat().st_mtime, "error": "그 사이 파일이 바뀌었습니다."}, 409)
            p.write_bytes(data)
            st = p.stat()
        except PermissionError:
            return self._json({"error": f"쓰기 권한이 없습니다: {p}"}, 403)
        except OSError as e:
            return self._json({"error": f"저장하지 못했습니다: {e}"}, 400)
        return self._json({"name": p.name, "path": str(p), "dir": str(p.parent), "mtime": st.st_mtime, "size": st.st_size})

    # 문서 옆의 이미지·첨부(스크린샷 png, 응답 json 등)를 그대로 내려준다. 읽기 허용 범위는 api_read 와 같다.
    FILE_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
        ".csv": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
        ".mjs": "text/plain; charset=utf-8", ".js": "text/plain; charset=utf-8",
        ".py": "text/plain; charset=utf-8", ".sh": "text/plain; charset=utf-8",
        ".sql": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8", ".yaml": "text/plain; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".pdf": "application/pdf",
        ".webm": "video/webm", ".mp4": "video/mp4",
    }
    # 브라우저가 화면에 못 여는 첨부(엑셀 등)는 내려받기로 준다.
    DOWNLOAD_TYPES = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".zip": "application/zip",
    }

    def api_file(self, q):
        raw = (q.get("path") or [""])[0]
        if not raw.strip():
            return self._json({"error": "경로가 비어 있습니다."}, 400)
        p = expand(raw)
        if not p.is_absolute():
            p = (Path.home() / p).resolve()
        if not self._allowed(p):
            return self._json({"error": f"공개 범위({SHARE_ROOT}) 밖의 경로입니다."}, 403)
        download = p.suffix.lower() in self.DOWNLOAD_TYPES
        content_type = self.DOWNLOAD_TYPES.get(p.suffix.lower()) or self.FILE_TYPES.get(p.suffix.lower())
        if content_type is None:
            return self._json({"error": f"내려줄 수 없는 파일 형식입니다: {p.suffix}"}, 415)
        try:
            if not p.is_file():
                return self._json({"error": f"파일이 없습니다: {p}"}, 404)
            if p.stat().st_size > MAX_BYTES:
                return self._json({"error": "파일이 너무 큽니다."}, 400)
            data = p.read_bytes()
        except PermissionError:
            return self._json({"error": f"읽기 권한이 없습니다: {p}"}, 403)
        except OSError as e:
            return self._json({"error": f"읽지 못했습니다: {e}"}, 400)
        # 영상 탐색(seek)은 브라우저가 Range 로 일부만 요청한다. 단일 구간만 지원한다.
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            total = len(data)
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
            else:
                start = max(total - int(m.group(2)), 0)
                end = total - 1
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            data = data[start:end + 1]
        else:
            self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(p.name)}")
        self.end_headers()
        self.wfile.write(data)

    def api_read(self, q):
        raw = (q.get("path") or [""])[0]
        if not raw.strip():
            return self._json({"error": "경로가 비어 있습니다."}, 400)

        p = expand(raw)
        if not p.is_absolute():
            p = (Path.home() / p).resolve()

        if not self._allowed(p):
            return self._json({"error": f"공개 범위({SHARE_ROOT}) 밖의 경로입니다."}, 403)

        try:
            if p.is_dir():
                return self._json({"error": f"디렉터리입니다: {p}"}, 400)
            if not p.exists():
                return self._json({"error": f"파일이 없습니다: {p}"}, 404)
            size = p.stat().st_size
            if size > MAX_BYTES:
                mb = size / 1024 / 1024
                return self._json({"error": f"파일이 너무 큽니다 ({mb:.1f}MB)."}, 400)
            # 줄바꿈은 화면용으로 \n 으로 맞추되, 원본이 CRLF 였는지 알려 줘서 편집 저장 때 그대로 되돌린다
            text = p.read_bytes().decode("utf-8", errors="replace")
            crlf = "\r\n" in text
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        except PermissionError:
            return self._json({"error": f"읽기 권한이 없습니다: {p}"}, 403)
        except OSError as e:
            return self._json({"error": f"읽지 못했습니다: {e}"}, 400)

        return self._json(
            {
                "crlf": crlf,
                "name": p.name,
                "path": str(p),
                "dir": str(p.parent),
                "text": text,
                "mtime": p.stat().st_mtime,
                "size": size,
            }
        )

    SEARCH_HITS_PER_FILE = 50
    SNIPPET_LEN = 160

    def api_search(self, q):
        """최근 파일 목록의 문서 본문을 검색한다. 공백으로 나눈 단어가 한 줄에 모두 있으면 일치."""
        query = unicodedata.normalize("NFC", (q.get("q") or [""])[0]).strip()
        only = (q.get("path") or [""])[0]
        terms = [t.lower() for t in query.split() if t]
        if not terms:
            return self._json({"query": query, "files": []})

        files = []
        st = load_state()
        seen = set()
        entries = []
        for entry in list(st.get("recent") or []) + list(st.get("pins") or []):   # 즐겨찾기도 함께 찾는다
            k = entry.get("path") or ("#" + str(entry.get("name")))
            if k in seen:
                continue
            seen.add(k)
            entries.append(entry)
        for entry in entries:
            path = entry.get("path")
            if only and path != only:
                continue
            text = None
            if path:
                p = Path(path)
                if not self._allowed(p):
                    continue
                try:
                    if p.is_file() and p.stat().st_size <= MAX_BYTES:
                        text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = None
            elif entry.get("text") is not None:
                text = entry["text"]
            if text is None:
                continue

            result = self._search_text(unicodedata.normalize("NFC", text), terms)
            name_hit = all(t in unicodedata.normalize("NFC", entry.get("name", "")).lower() for t in terms)
            if result["count"] or name_hit:
                files.append({
                    "name": entry.get("name"),
                    "path": path,
                    "dir": entry.get("dir"),
                    "nameHit": name_hit,
                    **result,
                })
        return self._json({"query": query, "files": files})

    def _search_text(self, text, terms):
        first = terms[0]
        hits, count, heading, before, in_fence = [], 0, "", 0, False
        for no, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            elif not in_fence and stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
            if all(t in low for t in terms):
                count += 1
                if len(hits) < self.SEARCH_HITS_PER_FILE:
                    hits.append({
                        "line": no,
                        "text": self._snippet(stripped, low.strip().find(first)),
                        "plain": stripped,
                        "heading": heading,
                        "nth": before,
                    })
            before += low.count(first)
        return {"count": count, "hits": hits}

    def _snippet(self, line, pos):
        if len(line) <= self.SNIPPET_LEN:
            return line
        start = max(0, pos - self.SNIPPET_LEN // 3)
        end = start + self.SNIPPET_LEN
        return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")

    def api_locate(self, q):
        """드롭·선택으로 연 파일의 원본 경로를 되찾는다."""
        name = (q.get("name") or [""])[0]
        raw_size = (q.get("size") or [""])[0]
        try:
            # size 를 주지 않으면 이름만으로 찾는다 (낡은 최근 항목 복구)
            size = int(raw_size) if raw_size != "" else None
            mtime = float((q.get("mtime") or ["0"])[0])
        except ValueError:
            return self._json({"path": None})

        if (size is not None and size < 0) or "/" in name or name in ("", ".", ".."):
            return self._json({"path": None})

        found = locate_file(name, size, mtime)
        if found and not self._allowed(Path(found)):
            found = None
        if not found:
            return self._json({"path": None})
        p = Path(found)
        return self._json({"path": str(p), "name": p.name, "dir": str(p.parent)})

    def api_complete(self, q):
        """경로 입력창 자동완성. 접두사에 맞는 디렉터리/마크다운 파일을 돌려준다."""
        raw = (q.get("path") or [""])[0]
        if not raw.strip():
            return self._json({"items": []})

        p = expand(raw)
        if raw.rstrip().endswith("/"):
            base, stem = p, ""
        else:
            base, stem = p.parent, p.name

        if not base.is_dir() or not self._allowed(base):
            return self._json({"items": []})

        items = []
        try:
            for child in sorted(base.iterdir()):
                if child.name.startswith(".") and not stem.startswith("."):
                    continue
                if stem and not child.name.lower().startswith(stem.lower()):
                    continue
                is_dir = child.is_dir()
                if not is_dir and child.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                items.append(
                    {
                        "name": child.name + ("/" if is_dir else ""),
                        "path": str(child) + ("/" if is_dir else ""),
                        "dir": is_dir,
                    }
                )
                if len(items) >= 60:
                    break
        except PermissionError:
            pass

        items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
        return self._json({"items": items})


def pick_port(start=START_PORT, tries=25):
    for port in range(start, start + tries):
        with socket.socket() as s:
            # 서버와 같은 조건으로 확인해야 한다. SO_REUSEADDR 이 없으면 방금 닫힌
            # 포트가 TIME_WAIT 상태라 사용 중으로 오판하고 포트가 계속 밀려 올라간다.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{start}~{start + tries - 1} 사이에 빈 포트가 없습니다.")


INITIAL_FILE = None


def lan_ip():
    """이 PC 의 LAN IP 를 찾는다. 실제로 패킷을 보내지는 않는다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global INITIAL_FILE, TOKEN, SHARE_ROOT

    ap = argparse.ArgumentParser(description="로컬 마크다운 뷰어")
    ap.add_argument("file", nargs="?", help="열면서 바로 표시할 md 파일")
    ap.add_argument("--port", type=int, default=None, help="포트 지정")
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동 실행 안 함")
    ap.add_argument(
        "--lan",
        metavar="DIR",
        help="같은 네트워크의 다른 PC 에도 공개한다. 공개할 디렉터리를 반드시 지정해야 하며, "
        "그 안의 파일만 읽는다. 접속에는 실행할 때 출력되는 토큰이 필요하다.",
    )
    args = ap.parse_args()

    if args.file:
        INITIAL_FILE = str(expand(args.file))

    if args.lan:
        SHARE_ROOT = expand(args.lan).resolve()
        if not SHARE_ROOT.is_dir():
            raise SystemExit(f"  공개할 디렉터리가 없습니다: {SHARE_ROOT}")
        if SHARE_ROOT == Path.home() or SHARE_ROOT == Path("/"):
            raise SystemExit(
                f"  {SHARE_ROOT} 전체 공개는 막아 두었습니다. 하위 폴더를 지정하세요."
            )
        if INITIAL_FILE and SHARE_ROOT not in Path(INITIAL_FILE).resolve().parents:
            INITIAL_FILE = None
        TOKEN = lan_token()
        host, shown = "0.0.0.0", lan_ip()
    else:
        host = shown = "127.0.0.1"

    port = args.port or pick_port()
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{shown}:{port}/" + (f"?t={TOKEN}" if TOKEN else "")

    if TOKEN:
        print(f"\n  다른 PC 공개 모드 — 공개 폴더: {SHARE_ROOT}")
        print("  아래 주소를 다른 PC 브라우저에 그대로 붙여 넣으세요.")
        print("  주소의 토큰이 열쇠입니다. 외부에 흘리지 마세요.\n")
        print(f"    {url}\n")
        host = socket.gethostname()
        if host and not host.replace(".", "").isdigit():
            print(f"    (이름 주소) http://{host if host.endswith('.local') else host + '.local'}:{port}/?t={TOKEN}\n")
        print("  같은 네트워크에 있어야 하고, 이 창을 닫으면 공개도 끝납니다.\n")
    else:
        print(f"\n  마크다운 뷰어 실행 중 → {url}")
        print("  이 PC 에서만 열립니다. 종료하려면 Ctrl+C\n")

    if not args.no_open:
        local = f"http://127.0.0.1:{port}/" + (f"?t={TOKEN}" if TOKEN else "")
        threading.Thread(
            target=lambda: (time.sleep(0.4), webbrowser.open(local)), daemon=True
        ).start()

    try:
        PORT_FILE.write_text(f"{port}\n{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료했습니다.")
        server.shutdown()
    finally:
        try:
            if PORT_FILE.exists() and PORT_FILE.read_text().split()[0] == str(port):
                PORT_FILE.unlink()
        except (OSError, IndexError):
            pass


if __name__ == "__main__":
    main()
