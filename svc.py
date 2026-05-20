"""SpeedCore Windows 服务管理 — 系统级静默常驻。

实现方式（按优先级自动选择）:
  1. sc.exe + nssm → 标准Windows服务，最可靠
  2. schtasks → 计划任务（开机自启、SYSTEM账户、隐藏窗口）
  3. Registry Run → 注册表启动项（用户登录自启）

提供:
  svc install    — 安装服务
  svc remove     — 卸载服务
  svc start      — 启动服务
  svc stop       — 停止服务
  svc status     — 查看状态
"""

import ctypes
import os
import subprocess
import sys
import time

if getattr(sys, 'frozen', False):
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
ARIA2C = os.path.join(ROOT, "aria2c.exe")
ARIA2_CONF = os.path.join(ROOT, "aria2.conf")
PROXY_SCRIPT = os.path.join(ROOT, "proxy.py")
BOOTSTRAP = os.path.join(ROOT, "_bootstrap.py")
PID_FILE = os.path.join(ROOT, "speedcore.pid")
TASK_NAME = "SpeedCore"
SERVICE_NAME = "SpeedCore"
SERVICE_DISPLAY = "SpeedCore — 多线程下载加速服务"


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def require_admin():
    if not is_admin():
        print("需要管理员权限。请以管理员身份运行。")
        print('  powershell -Command "Start-Process python -Verb RunAs"')
        sys.exit(1)


# ─── 辅助脚本生成 ────────────────────────────────────────

def _write_bootstrap():
    """生成 Python 启动器 — 自动检测用户代理并传给 aria2c"""
    script = f'''import os, sys, time, subprocess, json, urllib.request, winreg

ROOT = r"{ROOT}"
ARIA2C = r"{ARIA2C}"
ARIA2_CONF = r"{ARIA2_CONF}"
PROXY_SCRIPT = r"{PROXY_SCRIPT}"
PROXY_CONFIG = os.path.join(ROOT, ".upstream_proxy")
TEMP = os.environ.get("TEMP", os.environ.get("TMP", r"C:\\Windows\\Temp"))
ARIA2_LOG = os.path.join(TEMP, "speedcore_aria2.log")
PROXY_LOG = os.path.join(TEMP, "speedcore_proxy.log")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_FLAGS = CREATE_NO_WINDOW | DETACHED_PROCESS

def log(msg):
    with open(os.path.join(TEMP, "speedcore_bootstrap.log"), "a") as f:
        f.write(f"{{time.strftime('%H:%M:%S')}} {{msg}}\\n")

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
                log(f"Proxy from cache: {{cached}}")
                proxy = cached
    except:
        pass

    # 2. 缓存未命中则尝试从注册表读取当前用户的代理
    if not proxy:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings")
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
                        p = f"http://{{server}}" if "://" not in server else server
                        log(f"Proxy from registry: {{p}}")
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
                log(f"Proxy reachable: {{proxy}}")
                return proxy
            else:
                log(f"Proxy unreachable ({{host}}:{{port}}), fallback direct")
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
        json.dumps({{"jsonrpc":"2.0","id":"p","method":"aria2.getVersion"}}).encode(), timeout=2)
    v = json.loads(r.read()).get("result", {{}}).get("version", "")
    if v:
        log(f"aria2c v{{v}} already running")
except Exception:
    # 启动 aria2c (如果检测到上游代理则传入)
    log("Starting aria2c...")
    try:
        cmd = [ARIA2C, f"--conf-path={{ARIA2_CONF}}",
               f"--log={{ARIA2_LOG}}", "--log-level=error"]
        if UPSTREAM:
            cmd.append(f"--all-proxy={{UPSTREAM}}")
            log(f"aria2c using proxy: {{UPSTREAM}}")
        subprocess.Popen(cmd, creationflags=CREATE_FLAGS, close_fds=True)
    except Exception as e:
        log(f"aria2c start error: {{e}}")

    # 等待 RPC 就绪
    for _ in range(30):
        time.sleep(0.5)
        try:
            r = urllib.request.urlopen("http://127.0.0.1:16800/jsonrpc",
                json.dumps({{"jsonrpc":"2.0","id":"p","method":"aria2.getVersion"}}).encode(), timeout=2)
            v = json.loads(r.read()).get("result", {{}}).get("version", "")
            if v:
                log(f"aria2c v{{v}} ready")
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
'''
    with open(BOOTSTRAP, "w", encoding="utf-8") as f:
        f.write(script)
    return BOOTSTRAP


# ─── 上游代理持久化 ──────────────────────────────────────

PROXY_CACHE = os.path.join(ROOT, ".upstream_proxy")

def save_upstream_proxy():
    """保存当前用户代理到缓存文件，供 SYSTEM 账户下的 bootstrap 读取"""
    try:
        from spd import detect_proxy_state
        state = detect_proxy_state()
        proxy = None
        if state["wininet_enabled"] and state["wininet_server"]:
            server = state["wininet_server"].strip()
            if "=" in server:
                for p in server.split(";"):
                    if p.strip().lower().startswith("http="):
                        server = p.split("=", 1)[1].strip()
                        break
            proxy = f"http://{server}" if "://" not in server else server
        elif state["env_http_proxy"]:
            proxy = state["env_http_proxy"]
        elif state["env_https_proxy"]:
            proxy = state["env_https_proxy"]

        if proxy:
            with open(PROXY_CACHE, "w") as f:
                f.write(proxy)
            print(f"[i]  上游代理已保存: {proxy}")
            return proxy
        else:
            # 清除缓存
            try:
                os.unlink(PROXY_CACHE)
            except Exception:
                pass
            print("[i]  无上游代理，直连模式")
            return None
    except Exception as e:
        print(f"[WARN] 代理保存失败: {e}")
        return None


# ─── schtasks 方式 ───────────────────────────────────────

def install_schtask():
    require_admin()
    save_upstream_proxy()  # 保存用户代理供 SYSTEM bootstrap 使用
    _write_bootstrap()

    # 用 pythonw.exe 避免 SYSTEM 账户下无 .py 关联
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    task_cmd = f'"{pythonw}" "{BOOTSTRAP}"'

    # 删除旧任务
    subprocess.run(
        f'schtasks /delete /tn "{TASK_NAME}" /f',
        shell=True, capture_output=True, timeout=5
    )

    # 创建新任务 — SYSTEM账户、开机自启、隐藏运行
    result = subprocess.run(
        f'schtasks /create /tn "{TASK_NAME}" /tr "{task_cmd}" '
        f'/sc onstart /ru SYSTEM /rl highest /f /delay 0000:30',
        shell=True, capture_output=True, text=True, timeout=10
    )

    if result.returncode == 0:
        print(f"[OK] 计划任务已安装: {TASK_NAME}")
        print(f"   触发: 开机自启 (30秒延迟)")
        print(f"   账户: SYSTEM (最高权限, 交互桌面不可见)")
        print(f"   脚本: {task_cmd}")

        # 立即启动
        subprocess.run(
            f'schtasks /run /tn "{TASK_NAME}"',
            shell=True, capture_output=True, timeout=5
        )
        print(f"   状态: 已启动")
    else:
        print(f"[FAIL] 安装失败:\n{result.stderr}")
        return False
    return True


def remove_schtask():
    require_admin()
    # 先杀进程
    _kill_all()
    subprocess.run(
        f'schtasks /delete /tn "{TASK_NAME}" /f',
        shell=True, capture_output=True, timeout=5
    )
    print(f"[OK] 计划任务已删除: {TASK_NAME}")


def status_schtask():
    result = subprocess.run(
        f'schtasks /query /tn "{TASK_NAME}" /fo list',
        shell=True, capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        print("状态: 未安装")
        return

    status = "未知"
    for line in result.stdout.splitlines():
        if line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
    print(f"状态: {status}")

    # 检查进程
    _check_processes()


# ─── nssm 方式 ────────────────────────────────────────────

def _find_nssm() -> str:
    """查找 nssm.exe"""
    for path in os.environ.get("PATH", "").split(";"):
        nssm = os.path.join(path, "nssm.exe")
        if os.path.exists(nssm):
            return nssm

    # 检查 speedcore 目录
    local = os.path.join(ROOT, "nssm.exe")
    if os.path.exists(local):
        return local
    return None


def install_nssm():
    require_admin()
    nssm = _find_nssm()
    if not nssm:
        print("nssm.exe 未找到。下载中...")
        import urllib.request
        nssm_url = "https://nssm.cc/release/nssm-2.24.zip"
        zip_path = os.path.join(ROOT, "nssm.zip")
        urllib.request.urlretrieve(nssm_url, zip_path)
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                if name.endswith("win64/nssm.exe"):
                    z.extract(name, ROOT)
                    os.rename(os.path.join(ROOT, name),
                              os.path.join(ROOT, "nssm.exe"))
                    nssm = os.path.join(ROOT, "nssm.exe")
                    break
        os.unlink(zip_path)
        if not nssm:
            print("[FAIL] 下载nssm失败，请手动放置到speedcore目录")
            return False

    _write_bootstrap()
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    # 安装服务
    cmds = [
        [nssm, "install", SERVICE_NAME, pythonw, BOOTSTRAP],
        [nssm, "set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY],
        [nssm, "set", SERVICE_NAME, "Description",
         "Multi-threaded download accelerator — aria2c + system proxy"],
        [nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"],
        [nssm, "set", SERVICE_NAME, "AppExit", "Default", "Restart"],
        [nssm, "set", SERVICE_NAME, "AppNoConsole", "1"],
        [nssm, "set", SERVICE_NAME, "AppPriority", "NORMAL_PRIORITY_CLASS"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, capture_output=True, timeout=5)

    subprocess.run([nssm, "start", SERVICE_NAME],
                   capture_output=True, timeout=10)
    print(f"[OK] Windows 服务已安装: {SERVICE_NAME}")
    print(f"   管理: nssm <start|stop|restart|remove> {SERVICE_NAME}")


def remove_nssm():
    require_admin()
    nssm = _find_nssm()
    if nssm:
        subprocess.run([nssm, "stop", SERVICE_NAME],
                       capture_output=True, timeout=5)
        subprocess.run([nssm, "remove", SERVICE_NAME, "confirm"],
                       capture_output=True, timeout=5)
    print(f"[OK] 服务已删除: {SERVICE_NAME}")


# ─── Registry Run 方式 (fallback) ────────────────────────

def install_registry():
    _write_bootstrap()

    import winreg
    key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")
    winreg.SetValueEx(key, SERVICE_NAME, 0, winreg.REG_SZ, BOOTSTRAP)
    winreg.CloseKey(key)

    # 立即执行一次
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    subprocess.Popen(
        [pythonw, BOOTSTRAP],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    print(f"[OK] 启动项已安装: HKLM\\Run\\{SERVICE_NAME}")
    print(f"   注意: 仅在用户登录后启动，非系统级服务。")


def remove_registry():
    require_admin()
    _kill_all()
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, SERVICE_NAME)
        winreg.CloseKey(key)
        print(f"[OK] 启动项已删除")
    except OSError:
        pass


# ─── 进程管理 ────────────────────────────────────────────

def _kill_all():
    for name in ["aria2c.exe", "python.exe", "pythonw.exe"]:
        subprocess.run(
            f'taskkill /f /im "{name}" /fi "WINDOWTITLE eq speedcore*" 2>nul',
            shell=True, capture_output=True, timeout=5
        )


def _check_processes():
    """检查 aria2c 和 proxy 是否在运行"""
    r1 = subprocess.run(
        'tasklist /fi "imagename eq aria2c.exe" /fo csv /nh',
        shell=True, capture_output=True, text=True, timeout=5
    )
    aria2_running = "aria2c.exe" in r1.stdout
    print(f"  aria2c:  {'[OK] 运行中' if aria2_running else '[FAIL] 未运行'}")

    # 检查代理端口
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    proxy_running = sock.connect_ex(("127.0.0.1", 19999)) == 0
    sock.close()
    print(f"  proxy:   {'[OK] 运行中 (port 19999)' if proxy_running else '[FAIL] 未运行'}")

    # 检查 aria2c RPC
    try:
        import urllib.request, json
        r = urllib.request.urlopen(
            "http://127.0.0.1:16800/jsonrpc",
            json.dumps({"jsonrpc": "2.0", "id": "c", "method": "aria2.getVersion"}).encode(),
            timeout=3
        )
        version = json.loads(r.read()).get("result", {}).get("version", "")
        print(f"  RPC:     [OK] aria2 v{version}")
    except Exception:
        print(f"  RPC:     [FAIL] 不可达")


# ─── CLI ──────────────────────────────────────────────────

def print_usage():
    print("""
SpeedCore 服务管理

用法: python svc.py <命令>

命令:
  install         安装计划任务 (开机自启, SYSTEM账户, 隐藏)
  install --nssm  使用nssm安装为标准Windows服务
  install --reg   安装为注册表启动项 (用户登录启动)

  remove          卸载
  start           立即启动
  stop            停止所有进程
  status          查看运行状态

示例:
  python svc.py install      # 推荐: 计划任务方式
  python svc.py status       # 检查运行状态
""")


def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    cmd = sys.argv[1].lower()
    mode = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "install":
        if mode == "--nssm":
            install_nssm()
        elif mode == "--reg":
            install_registry()
        else:
            install_schtask()

    elif cmd == "remove":
        require_admin()
        remove_schtask()
        _kill_all()
        try:
            remove_registry()
        except Exception:
            pass

    elif cmd == "start":
        _write_bootstrap()
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        subprocess.Popen(
            [pythonw, BOOTSTRAP],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        print("[OK] SpeedCore 已启动")
        time.sleep(3)
        _check_processes()

    elif cmd == "stop":
        require_admin()
        _kill_all()
        # 也停止计划任务
        subprocess.run(
            f'schtasks /end /tn "{TASK_NAME}"',
            shell=True, capture_output=True, timeout=5
        )
        print("[OK] SpeedCore 已停止")

    elif cmd == "status":
        status_schtask()

    elif cmd == "restart":
        require_admin()
        _kill_all()
        time.sleep(1)
        _write_bootstrap()
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        subprocess.Popen(
            [pythonw, BOOTSTRAP],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        print("[OK] SpeedCore 已重启")
        time.sleep(3)
        _check_processes()

    else:
        print_usage()


if __name__ == "__main__":
    main()
