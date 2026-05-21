"""SpeedCore 系统代理 — 无感拦截下载，多线程加速。

架构:
  浏览器/系统HTTP流量 → 127.0.0.1:19999 (本代理)
    ├─ 网页/API请求 → 检测上游代理 → 通过上游代理转发 (或直连)
    ├─ 文件下载请求 → aria2c RPC 多线程加速 → 流式返回
    └─ HTTPS → CONNECT隧道 (可升级MITM实现HTTPS加速)

自动识别:
  - 启动时检测系统现有代理，保存为上游代理
  - 穿透(非下载)流量自动走上游代理
  - 无上游代理时直连
"""

import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

# ─── 常量 ────────────────────────────────────────────────
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 19999
ARIA2_RPC = "http://127.0.0.1:16800/jsonrpc"
CACHE_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "speedcore", "cache")
LOG_FILE = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", tempfile.gettempdir())), "speedcore_proxy.log")
BUF_SIZE = 256 * 1024  # 256KB streaming buffer
UPSTREAM_PROXY = None  # 上游外部代理 (http://host:port)
DOWNLOAD_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
                ".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk", ".ipa",
                ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                ".mp3", ".flac", ".wav", ".aac", ".ogg",
                ".pdf", ".epub", ".mobi", ".djvu",
                ".psd", ".ai", ".sketch",
                ".ttf", ".otf", ".woff2",
                ".vsix", ".crx", ".xpi", ".safariextz",
                ".bin", ".dat", ".pak", ".vpk",
                ".unitypackage", ".assetbundle",
                ".whl", ".jar", ".war", ".ear",
                ".sql", ".sqlite3", ".db", ".mdb",
                ".ova", ".vmdk", ".vdi", ".qcow2",
                ".img", ".raw", ".dmg", ".toast",
                ".cab", ".msu", ".ps1", ".nupkg"}
STREAM_TYPES = {"application/octet-stream", "application/zip",
                "application/x-msdownload", "application/x-iso9660-image",
                "application/x-rar-compressed", "application/x-7z-compressed",
                "application/x-tar", "application/gzip", "application/x-bzip2",
                "application/x-xz", "application/x-msi",
                "video/", "audio/", "application/pdf",
                "application/vnd.android.package-archive"}

# ─── aria2c JSON-RPC ────────────────────────────────────

def aria2_rpc(method: str, params: list = None) -> dict:
    """调用 aria2c JSON-RPC"""
    if params is None:
        params = []
    req_data = json.dumps({
        "jsonrpc": "2.0",
        "id": "spd",
        "method": f"aria2.{method}",
        "params": params
    }).encode()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(ARIA2_RPC, data=req_data,
                                   headers={"Content-Type": "application/json"}),
            timeout=10)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def aria2_add_url(url: str, out_dir: str, out_filename: str = None) -> str:
    """添加下载任务，返回GID"""
    opts = {"dir": out_dir}
    if out_filename:
        opts["out"] = out_filename
    result = aria2_rpc("addUri", [[url], opts])
    if "result" in result:
        return result["result"]
    return None


def aria2_status(gid: str) -> dict:
    """查询任务状态"""
    result = aria2_rpc("tellStatus", [gid, ["status", "completedLength",
                                             "totalLength", "downloadSpeed",
                                             "files", "errorMessage"]])
    return result.get("result", {})


def aria2_remove(gid: str):
    aria2_rpc("remove", [gid])


def aria2_purge(gid: str):
    aria2_rpc("removeDownloadResult", [gid])


# ─── 上游代理检测 ──────────────────────────────────────────

def _log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _detect_upstream_proxy() -> str:
    """检测系统当前是否设有外部代理（非SpeedCore自身）。

    检查顺序: 环境变量 → WinINET注册表 → WinHTTP
    返回: "http://host:port" 或 None (直连)
    """
    # 1. 环境变量 (最高优先级，urllib默认使用)
    for var in ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]:
        val = os.environ.get(var)
        if val and val.strip():
            # 如果已经是SpeedCore自己，跳过
            if ":19999" in val:
                continue
            proxy_url = val.strip()
            if not proxy_url.startswith("http"):
                proxy_url = f"http://{proxy_url}"
            _log(f"upstream from env {var}: {proxy_url}")
            return proxy_url

    # 2. WinINET 注册表
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if enabled:
                server = winreg.QueryValueEx(key, "ProxyServer")[0]
                if server and ":19999" not in str(server):
                    server = str(server).strip()
                    if "=" in server:
                        # 多协议格式: "http=1.2.3.4:80;https=1.2.3.4:443"
                        parts = server.split(";")
                        for p in parts:
                            if p.strip().lower().startswith("http="):
                                server = p.split("=", 1)[1].strip()
                                break
                    url = f"http://{server}" if "://" not in server else server
                    _log(f"upstream from WinINET: {url}")
                    return url
        except OSError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass

    # 3. WinHTTP
    try:
        r = subprocess.run(
            'netsh winhttp show proxy', shell=True,
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            line_s = line.strip()
            if ("代理服务器" in line_s or "Proxy Server" in line_s) and ":" in line_s:
                val = line_s.split(":", 1)[-1].strip()
                if val and "直接" not in val and "Direct" not in val and ":19999" not in val:
                    url = f"http://{val}" if "://" not in val else val
                    _log(f"upstream from WinHTTP: {url}")
                    return url
    except Exception:
        pass

    _log("no upstream proxy detected, using direct")
    return None


def _build_url_opener(upstream_proxy: str = None):
    """构建urllib opener: 有上游代理则走代理，否则直连"""
    handlers = []

    if upstream_proxy:
        # 通过上游代理转发
        proxy_handler = urllib.request.ProxyHandler({
            "http": upstream_proxy,
            "https": upstream_proxy,
        })
        handlers.append(proxy_handler)
    else:
        # 直连 — 显式创建空代理处理器，防止urllib读取系统代理
        proxy_handler = urllib.request.ProxyHandler({})
        handlers.append(proxy_handler)

    return urllib.request.build_opener(*handlers)


# 模块级 opener — 由 run_proxy() 初始化
_url_opener = None


def get_opener():
    global _url_opener
    if _url_opener is None:
        upstream = _detect_upstream_proxy()
        _url_opener = _build_url_opener(upstream)
        global UPSTREAM_PROXY
        UPSTREAM_PROXY = upstream
    return _url_opener


# ─── 下载检测 ────────────────────────────────────────────

DOWNLOAD_EXT_PATTERN = re.compile(
    r"\.(zip|rar|7z|tar|gz|bz2|xz|iso|exe|msi|dmg|deb|rpm|apk|ipa|"
    r"mp4|mkv|avi|mov|wmv|flv|webm|mp3|flac|wav|aac|ogg|"
    r"pdf|epub|mobi|psd|ai|ttf|otf|woff2|"
    r"vsix|crx|xpi|bin|pak|unitypackage|whl|jar|"
    r"ova|vmdk|cab|msu|nupkg|img|sqlite3|db)"
    r"(?:\?.*)?$", re.I
)


def is_download_request(url: str, content_type: str = None,
                        content_length: int = 0) -> bool:
    """判断请求是否为可加速下载"""
    # 1. URL 扩展名匹配
    if DOWNLOAD_EXT_PATTERN.search(url):
        return True

    # 2. Content-Type 是已知下载类型
    if content_type:
        ct_lower = content_type.lower()
        if ct_lower == "application/octet-stream":
            return True
        for st in STREAM_TYPES:
            if ct_lower.startswith(st):
                return True

    # 3. 大文件 (>10MB)
    if content_length and content_length > 10 * 1024 * 1024:
        return True

    return False


# ─── PAC 文件 ────────────────────────────────────────────

PAC_TEMPLATE = """function FindProxyForURL(url, host) {{
    var ext = url.match(/\\.(zip|rar|7z|tar|gz|bz2|xz|iso|exe|msi|dmg|deb|rpm|apk|ipa|mp4|mkv|avi|mov|wmv|flv|webm|mp3|flac|wav|aac|ogg|pdf|epub|bin|pak|unitypackage|vsix|crx|whl|jar|cab|msu|nupkg|img|ova|vmdk)(\\?|#|$)/i);
    if (ext) {{
        return "PROXY {host}:{port}";
    }}
    return "DIRECT";
}}"""


def get_pac() -> str:
    return PAC_TEMPLATE.format(host=PROXY_HOST, port=PROXY_PORT)


# ─── HTTP 代理处理器 ─────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    """透明加速代理 — 下载走aria2c，其他直通"""
    timeout = 60
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        pass  # 静默

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_HEAD(self):
        self._handle_request("HEAD")

    def _handle_request(self, method):
        url = self.path

        # PAC 文件请求
        if url == "/proxy.pac" or url == "/pac":
            self._serve_pac()
            return

        # 状态页
        if url == "/speedcore-status":
            self._serve_status()
            return

        # 完整URL (代理模式) vs 相对路径
        if url.startswith("http://") or url.startswith("https://"):
            parsed = urllib.parse.urlparse(url)
            target_host = parsed.hostname
            target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            target_path = parsed.path + ("?" + parsed.query if parsed.query else "")
        else:
            # 从 Host header 重建URL
            host_header = self.headers.get("Host", "")
            target_host = host_header.split(":")[0] if host_header else "unknown"
            target_port = int(host_header.split(":")[1]) if ":" in host_header else 80
            target_path = url
            url = f"http://{host_header}{target_path}"

        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", "0"))

        # 判断是否下载
        if is_download_request(url, content_type, content_length) and method == "GET":
            self._accelerated_download(url, target_path)
        else:
            self._passthrough(method, target_host, target_port, target_path)

    def do_CONNECT(self):
        """HTTPS CONNECT 隧道"""
        host_port = self.path.split(":")
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 443

        try:
            # 连接目标服务器
            remote = socket.create_connection((host, port), timeout=10)
            self.send_response(200, "Connection Established")
            self.end_headers()

            # 双向隧道
            self._tunnel(self.connection, remote)
        except Exception:
            self.send_response(502, "Bad Gateway")
            self.end_headers()
        finally:
            try:
                remote.close()
            except Exception:
                pass

    def _tunnel(self, client, remote):
        """双向字节转发 — 两个线程各负责一个方向"""
        stop = threading.Event()

        def pipe(src, dst, name):
            try:
                while not stop.is_set():
                    data = src.recv(BUF_SIZE)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=pipe, args=(client, remote, "c2r"), daemon=True)
        t2 = threading.Thread(target=pipe, args=(remote, client, "r2c"), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=self.timeout)
        t2.join(timeout=self.timeout)

    def _accelerated_download(self, url, path):
        """走aria2c多线程加速下载 → 流式返回客户端"""
        # 创建缓存目录
        task_id = str(abs(hash(url)))[:12]
        out_dir = os.path.join(CACHE_ROOT, task_id)
        os.makedirs(out_dir, exist_ok=True)

        filename = os.path.basename(urllib.parse.urlparse(url).path) or "download"
        gid = aria2_add_url(url, out_dir, filename)

        if not gid:
            # aria2c不可用，回退直通
            self._passthrough("GET", None, None, path, url)
            return

        try:
            # 等待 aria2c 开始下载（文件出现）
            downloaded_file = None
            total = 0
            deadline = time.time() + 30

            while time.time() < deadline:
                status = aria2_status(gid)
                if not status:
                    time.sleep(0.5)
                    continue

                total = int(status.get("totalLength", 0))
                files = status.get("files", [])
                if files:
                    downloaded_file = os.path.join(out_dir,
                                                   files[0].get("path", "").replace("\\", "/").split("/")[-1])
                    if os.path.exists(downloaded_file):
                        break
                time.sleep(0.3)

            if not downloaded_file or not os.path.exists(downloaded_file):
                # 回退
                aria2_remove(gid)
                self._passthrough("GET", None, None, path, url)
                return

            # 流式返回 — 边下载边发送
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if filename:
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
            if total:
                self.send_header("Content-Length", str(total))
            self.send_header("X-SpeedCore", "accelerated")
            self.end_headers()

            sent = 0
            last_speed_check = time.time()
            last_size = 0

            while True:
                current_size = os.path.getsize(downloaded_file)
                if current_size > sent:
                    with open(downloaded_file, "rb") as f:
                        f.seek(sent)
                        chunk = f.read(BUF_SIZE)
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        continue

                # 检查是否下载完成
                status = aria2_status(gid)
                st = status.get("status", "") if status else ""
                if st in ("complete", "error", "removed"):
                    # 读取剩余
                    final_size = os.path.getsize(downloaded_file)
                    if final_size > sent:
                        with open(downloaded_file, "rb") as f:
                            f.seek(sent)
                            while True:
                                chunk = f.read(BUF_SIZE)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                    break

                if st == "error":
                    break

                time.sleep(0.1)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端断开，正常
        except Exception:
            try:
                self._passthrough("GET", None, None, path, url)
            except Exception:
                pass
        finally:
            aria2_remove(gid)
            aria2_purge(gid)
            # 清理缓存
            try:
                import shutil
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass

    def _passthrough(self, method, target_host, target_port, path, fallback_url=None):
        """透明转发请求到目标服务器（自动检测上游代理或直连）"""
        url = fallback_url or self.path
        if not url.startswith("http"):
            url = f"http://{target_host}:{target_port}{path}"

        try:
            req = urllib.request.Request(
                url,
                data=self._read_body() if method in ("POST", "PUT", "PATCH") else None,
                method=method
            )

            # 复制请求头（移除代理相关头）
            skip_headers = {"host", "proxy-connection", "proxy-authorization",
                            "proxy-authenticate", "keep-alive", "transfer-encoding"}
            for key, value in self.headers.items():
                if key.lower() not in skip_headers:
                    req.add_header(key, value)

            # 使用自定义 opener — 有上游代理走代理，否则直连
            opener = get_opener()
            with opener.open(req, timeout=30) as resp:
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in {"transfer-encoding", "connection",
                                           "keep-alive", "proxy-connection"}:
                        self.send_header(key, value)
                self.end_headers()

                while True:
                    chunk = resp.read(BUF_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        except Exception as e:
            try:
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"Proxy error: {e}".encode())
            except Exception:
                pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            return self.rfile.read(length)
        return b""

    def _serve_pac(self):
        pac = get_pac()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
        self.send_header("Content-Length", str(len(pac)))
        self.end_headers()
        self.wfile.write(pac.encode())

    def _serve_status(self):
        # aria2c信息 — 非关键，短超时容错
        active_count = waiting_count = stopped_count = 0
        aria2_ver = "?"
        try:
            gs = aria2_rpc("getGlobalStat")
            if "result" in gs:
                r = gs["result"]
                active_count = int(r.get("numActive", 0))
                waiting_count = int(r.get("numWaiting", 0))
                stopped_count = int(r.get("numStoppedTotal", 0))
        except Exception:
            pass
        try:
            v = aria2_rpc("getVersion")
            if "result" in v:
                aria2_ver = v["result"].get("version", "?")
        except Exception:
            pass

        status = {
            "proxy": "running",
            "port": PROXY_PORT,
            "mode": "upstream" if UPSTREAM_PROXY else "direct",
            "upstream_proxy": UPSTREAM_PROXY,
            "aria2_rpc": ARIA2_RPC,
            "aria2_version": aria2_ver,
            "active_downloads": active_count,
            "waiting": waiting_count,
            "completed": stopped_count,
            "cache_dir": CACHE_ROOT,
        }
        body = json.dumps(status, indent=2, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())


class ThreadingProxyServer(ThreadingMixIn, HTTPServer):
    """多线程代理服务器 — 支持并发请求"""
    allow_reuse_address = True
    daemon_threads = True


# ─── TUN 透明代理 ──────────────────────────────────────────

TUN_PORT = 19998
TUN_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tun_map.json")


def _load_tun_map() -> dict:
    try:
        import json as _json
        with open(TUN_MAP_FILE, "r") as f:
            return _json.load(f)
    except Exception:
        return {}


def _tun_relay(client_sock, client_addr):
    """透明代理转发 — 查 NAT 表还原原始目标，双向字节隧道"""
    import json as _json
    src_ip, src_port = client_addr[0], client_addr[1]

    # Look up original destination
    tun_map = _load_tun_map()
    key = f"{src_ip}:{src_port}"
    entry = tun_map.get(key)
    if entry is None:
        # Try recently added (re-read after short delay)
        time.sleep(0.1)
        tun_map = _load_tun_map()
        entry = tun_map.get(key)

    if entry is None:
        # Can't find destination — try reading Host from HTTP header or SNI
        client_sock.settimeout(3)
        try:
            peek = client_sock.recv(4096, socket.MSG_PEEK)
            # Try HTTP Host header
            host_match = re.search(rb"Host:\s*([^\r\n]+)", peek)
            if host_match:
                host_str = host_match.group(1).decode("ascii", errors="ignore").strip()
                if ":" in host_str:
                    host, port_str = host_str.rsplit(":", 1)
                    dst_host, dst_port = host, int(port_str)
                else:
                    dst_host, dst_port = host_str, 80
            else:
                # Try TLS SNI
                if peek[:3] == b"\x16\x03\x01" or peek[:3] == b"\x16\x03\x03":
                    # TLS ClientHello — parse SNI
                    try:
                        sni_len = int.from_bytes(peek[5:7], "big")
                        if len(peek) > 7 + sni_len:
                            sni_data = peek[7:7 + sni_len]
                            # Find SNI extension (type 0x00 0x00)
                            i = 0
                            while i < len(sni_data) - 4:
                                if sni_data[i:i+2] == b"\x00\x00":
                                    sni_name_len = int.from_bytes(sni_data[i+2:i+4], "big")
                                    sni_name = sni_data[i+4:i+4+sni_name_len].decode("ascii", errors="ignore")
                                    dst_host, dst_port = sni_name, 443
                                    break
                                i += 2
                if dst_host is None:
                    client_sock.close()
                    return
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass
            return

        dst_host, dst_port = entry[0], int(entry[1]) if entry else (host_str, 80)

    dst_host, dst_port = entry[0], int(entry[1])

    # Connect to original destination
    try:
        remote = socket.create_connection((dst_host, dst_port), timeout=10)
    except Exception:
        try:
            client_sock.close()
        except Exception:
            pass
        return

    # Bidirectional relay
    def pipe(src, dst):
        try:
            while True:
                data = src.recv(BUF_SIZE)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass

    t1 = threading.Thread(target=pipe, args=(client_sock, remote), daemon=True)
    t2 = threading.Thread(target=pipe, args=(remote, client_sock), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=300)
    t2.join(timeout=300)

    try:
        client_sock.close()
    except Exception:
        pass
    try:
        remote.close()
    except Exception:
        pass


def _start_tun_listener():
    """启动透明代理监听 (端口 19998) — 后台线程"""
    tun_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tun_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tun_sock.bind(("127.0.0.1", TUN_PORT))
    tun_sock.listen(128)
    tun_sock.settimeout(1)
    _log(f"TUN transparent proxy listening on 127.0.0.1:{TUN_PORT}")

    while True:
        try:
            client, addr = tun_sock.accept()
            t = threading.Thread(target=_tun_relay, args=(client, addr), daemon=True)
            t.start()
        except socket.timeout:
            continue
        except Exception:
            break


# ─── 服务管理 ────────────────────────────────────────────

def run_proxy(port: int = PROXY_PORT):
    """启动代理服务器（阻塞）— 自动检测上游代理"""
    # 初始化上游代理检测 + URL opener
    global _url_opener, UPSTREAM_PROXY
    upstream = _detect_upstream_proxy()
    UPSTREAM_PROXY = upstream
    _url_opener = _build_url_opener(upstream)

    # 启动 TUN 透明代理监听 (:19998)
    tun_thread = threading.Thread(target=_start_tun_listener, daemon=True)
    tun_thread.start()

    server = ThreadingProxyServer((PROXY_HOST, port), ProxyHandler)
    print(f"SpeedCore Proxy -> http://{PROXY_HOST}:{port}")
    if upstream:
        print(f"  [!] 检测到上游代理: {upstream}")
        print(f"      网页/API流量将通过上游代理转发")
        print(f"      下载流量: aria2c多线程加速 (直连)")
    else:
        print(f"  模式: 直连 (无上游代理)")
    print(f"  PAC: http://{PROXY_HOST}:{port}/proxy.pac")
    print(f"  Status: http://{PROXY_HOST}:{port}/speedcore-status")
    print(f"  TUN:  tcp://{PROXY_HOST}:{TUN_PORT} (transparent)")

    _log(f"Proxy started on port {port}, upstream={upstream or 'none'}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PROXY_PORT
    run_proxy(port)
