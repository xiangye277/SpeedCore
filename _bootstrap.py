import os, sys, time, subprocess, json, urllib.request, winreg

ROOT = r"D:\ClaudeCode\speedcore"
ARIA2C = r"D:\ClaudeCode\speedcore\aria2c.exe"
ARIA2_CONF = r"D:\ClaudeCode\speedcore\aria2.conf"
PROXY_SCRIPT = r"D:\ClaudeCode\speedcore\proxy.py"
WATCHDOG_SCRIPT = os.path.join(ROOT, "watchdog.py")
PROXY_CONFIG = os.path.join(ROOT, ".upstream_proxy")
TEMP = os.environ.get("TEMP", os.environ.get("TMP", r"C:\Windows\Temp"))
ARIA2_LOG = os.path.join(TEMP, "speedcore_aria2.log")
PROXY_LOG = os.path.join(TEMP, "speedcore_proxy.log")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_FLAGS = CREATE_NO_WINDOW | DETACHED_PROCESS

def log(msg):
    with open(os.path.join(TEMP, "speedcore_bootstrap.log"), "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def is_host_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP connect 检测主机是否可达"""
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False

def parse_proxy_url(url: str):
    """解析代理URL -> (host, port)"""
    host, port = url.split("://", 1)[-1].rsplit(":", 1)
    return host, int(port)

def detect_upstream_proxy():
    """读取缓存的代理配置；若无则尝试从注册表检测。检测到后验证可达性"""
    proxy = ""

    # 1. 先读缓存文件
    try:
        with open(PROXY_CONFIG, "r") as f:
            cached = f.read().strip()
            if cached:
                log(f"Proxy from cache: {cached}")
                proxy = cached
    except:
        pass

    # 2. 缓存未命中则尝试从注册表读取当前用户的代理
    if not proxy:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            try:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
                if enabled:
                    server = winreg.QueryValueEx(key, "ProxyServer")[0]
                    if server and ":19999" not in str(server):
                        server = str(server).strip()
                        if "=" in server:
                            for p in server.split(";"):
                                if p.strip().lower().startswith("http="):
                                    server = p.split("=", 1)[1].strip()
                                    break
                        p = f"http://{server}" if "://" not in server else server
                        log(f"Proxy from registry: {p}")
                        proxy = p
            except:
                pass
            winreg.CloseKey(key)
        except:
            pass

    # 3. 可达性验证
    if proxy:
        try:
            host, port = parse_proxy_url(proxy)
            if is_host_reachable(host, port):
                log(f"Proxy reachable: {proxy}")
                return proxy
            else:
                log(f"Proxy unreachable ({host}:{port}), fallback direct")
                return ""
        except Exception:
            log(f"Proxy parse error, fallback direct")
            return ""

    log("No upstream proxy detected")
    return ""

UPSTREAM = detect_upstream_proxy()

log("Bootstrap starting...")

# 如果已有 aria2c 在运行，跳过
try:
    r = urllib.request.urlopen("http://127.0.0.1:16800/jsonrpc",
        json.dumps({"jsonrpc":"2.0","id":"p","method":"aria2.getVersion"}).encode(), timeout=2)
    v = json.loads(r.read()).get("result", {}).get("version", "")
    if v:
        log(f"aria2c v{v} already running")
except Exception:
    # 启动 aria2c (如果检测到上游代理则传入)
    log("Starting aria2c...")
    try:
        cmd = [ARIA2C, f"--conf-path={ARIA2_CONF}",
               f"--log={ARIA2_LOG}", "--log-level=error"]
        if UPSTREAM:
            cmd.append(f"--all-proxy={UPSTREAM}")
            log(f"aria2c using proxy: {UPSTREAM}")
        subprocess.Popen(cmd, creationflags=CREATE_FLAGS, close_fds=True)
    except Exception as e:
        log(f"aria2c start error: {e}")

    # 等待 RPC 就绪
    for _ in range(30):
        time.sleep(0.5)
        try:
            r = urllib.request.urlopen("http://127.0.0.1:16800/jsonrpc",
                json.dumps({"jsonrpc":"2.0","id":"p","method":"aria2.getVersion"}).encode(), timeout=2)
            v = json.loads(r.read()).get("result", {}).get("version", "")
            if v:
                log(f"aria2c v{v} ready")
                break
        except Exception:
            continue
    else:
        log("aria2c failed to start")

# 启动代理 (传上游代理环境变量)
log("Starting proxy...")
env = os.environ.copy()
if UPSTREAM:
    env["HTTP_PROXY"] = UPSTREAM
    env["HTTPS_PROXY"] = UPSTREAM
subprocess.Popen(
    [sys.executable, PROXY_SCRIPT, "19999"],
    creationflags=CREATE_FLAGS,
    close_fds=True,
    env=env,
    stdout=open(PROXY_LOG, "a"),
    stderr=subprocess.STDOUT
)
log("Proxy started — bootstrap done")

# 启动看门狗 (自动检测 + 自动重启)
log("Starting watchdog...")
subprocess.Popen(
    [sys.executable, WATCHDOG_SCRIPT],
    creationflags=CREATE_FLAGS,
    close_fds=True
)
log("Watchdog started")
