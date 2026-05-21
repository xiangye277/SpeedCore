"""SpeedCore Watchdog — 自动检测运行状态，异常自动重启。

作为 _bootstrap.py 最后一步启动，每 30 秒检测一次：
  - aria2c 进程 + RPC 可达
  - 代理端口 :19999 监听
  - 发现异常 → 自动重启对应组件
  - 记录所有事件到日志
"""

import os, sys, time, socket, json, subprocess, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ARIA2C = os.path.join(ROOT, "aria2c.exe")
ARIA2_CONF = os.path.join(ROOT, "aria2.conf")
PROXY_SCRIPT = os.path.join(ROOT, "proxy.py")
TEMP = os.environ.get("TEMP", os.environ.get("TMP", r"C:\Windows\Temp"))
WATCHDOG_LOG = os.path.join(TEMP, "speedcore_watchdog.log")
INTERVAL = 30  # seconds between checks
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_FLAGS = CREATE_NO_WINDOW | DETACHED_PROCESS


def log(msg: str):
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def check_port(port: int) -> bool:
    """TCP connect 检测端口是否在监听"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        r = s.connect_ex(("127.0.0.1", port))
        return r == 0
    except Exception:
        return False
    finally:
        s.close()


def check_aria2_rpc() -> bool:
    """检测 aria2 RPC 是否可达"""
    try:
        data = json.dumps({"jsonrpc": "2.0", "id": "w", "method": "aria2.getVersion"}).encode()
        r = urllib.request.urlopen("http://127.0.0.1:16800/jsonrpc", data, timeout=3)
        resp = json.loads(r.read())
        return "result" in resp
    except Exception:
        return False


def start_aria2(upstream_proxy: str = ""):
    """启动 aria2c 进程"""
    log("Starting aria2c...")
    try:
        cmd = [ARIA2C, f"--conf-path={ARIA2_CONF}",
               f"--log={os.path.join(TEMP, 'speedcore_aria2.log')}",
               "--log-level=error"]
        if upstream_proxy:
            cmd.append(f"--all-proxy={upstream_proxy}")
        subprocess.Popen(cmd, creationflags=CREATE_FLAGS, close_fds=True)
        log("aria2c started")
        return True
    except Exception as e:
        log(f"aria2c start failed: {e}")
        return False


def start_proxy(upstream_proxy: str = ""):
    """启动代理进程"""
    log("Starting proxy...")
    try:
        env = os.environ.copy()
        if upstream_proxy:
            env["HTTP_PROXY"] = upstream_proxy
            env["HTTPS_PROXY"] = upstream_proxy
        subprocess.Popen(
            [sys.executable, PROXY_SCRIPT, "19999"],
            creationflags=CREATE_FLAGS,
            close_fds=True,
            env=env,
            stdout=open(os.path.join(TEMP, "speedcore_proxy.log"), "a"),
            stderr=subprocess.STDOUT,
        )
        log("proxy started")
        return True
    except Exception as e:
        log(f"proxy start failed: {e}")
        return False


def get_upstream_proxy() -> str:
    """读取上游代理缓存"""
    try:
        cfg = os.path.join(ROOT, ".upstream_proxy")
        with open(cfg, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def run():
    """启动看门狗 — 阻塞运行，每 INTERVAL 秒循环检测"""
    log("=" * 40)
    log("Watchdog started")
    log(f"  PID: {os.getpid()}")
    log(f"  Interval: {INTERVAL}s")

    upstream = get_upstream_proxy()
    consecutive_failures = 0

    while True:
        aria2_ok = check_aria2_rpc()
        proxy_ok = check_port(19999)

        if not aria2_ok:
            log(f"[!] aria2c DOWN — restarting (failure #{consecutive_failures + 1})")
            start_aria2(upstream)
            time.sleep(5)  # Wait for startup
            if check_aria2_rpc():
                log("  -> aria2c recovered")
                consecutive_failures = 0
            else:
                consecutive_failures += 1

        if not proxy_ok:
            log(f"[!] proxy :19999 DOWN — restarting")
            start_proxy(upstream)
            time.sleep(3)
            if check_port(19999):
                log("  -> proxy recovered")
            else:
                log("  -> proxy still down")

        # Log periodic status
        if aria2_ok and proxy_ok:
            if consecutive_failures > 0:
                log("Both services healthy — resetting failure counter")
                consecutive_failures = 0

        # If too many consecutive failures, escalate
        if consecutive_failures >= 10:
            log("[CRITICAL] 10 consecutive failures — giving up this cycle, system may need intervention")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    print("SpeedCore Watchdog — auto-detect + auto-restart")
    print(f"  PID: {os.getpid()}")
    print(f"  Log: {WATCHDOG_LOG}")
    run()
