"""SpeedCore Watchdog — 自动检测运行状态，异常自动重启。

作为 _bootstrap.py 最后一步启动，每 30 秒检测一次：
  - aria2c RPC :16800
  - 代理端口 :19999 + :19998 (TUN)
  - TUN 进程
  - 发现异常 → 自动重启对应组件
"""

import os, sys, time, socket, json, subprocess, urllib.request, ctypes

ROOT = os.path.dirname(os.path.abspath(__file__))
ARIA2C = os.path.join(ROOT, "aria2c.exe")
ARIA2_CONF = os.path.join(ROOT, "aria2.conf")
PROXY_SCRIPT = os.path.join(ROOT, "proxy.py")
TUN_SCRIPT = os.path.join(ROOT, "tun.py")
PID_FILE = os.path.join(ROOT, "tun.pid")
TEMP = os.environ.get("TEMP", os.environ.get("TMP", r"C:\Windows\Temp"))
WATCHDOG_LOG = os.path.join(TEMP, "speedcore_watchdog.log")
INTERVAL = 30
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
    try:
        data = json.dumps({"jsonrpc": "2.0", "id": "w", "method": "aria2.getVersion"}).encode()
        r = urllib.request.urlopen("http://127.0.0.1:16800/jsonrpc", data, timeout=3)
        return "result" in json.loads(r.read())
    except Exception:
        return False


def check_tun_process() -> bool:
    """Check if TUN process is alive via PID file"""
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(0x0400, False, pid)
        if h:
            kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def start_aria2(upstream: str = ""):
    log("Starting aria2c...")
    try:
        cmd = [ARIA2C, f"--conf-path={ARIA2_CONF}",
               f"--log={os.path.join(TEMP, 'speedcore_aria2.log')}",
               "--log-level=error"]
        if upstream:
            cmd.append(f"--all-proxy={upstream}")
        subprocess.Popen(cmd, creationflags=CREATE_FLAGS, close_fds=True)
        log("aria2c started")
    except Exception as e:
        log(f"aria2c start failed: {e}")


def start_proxy(upstream: str = ""):
    log("Starting proxy...")
    try:
        env = os.environ.copy()
        if upstream:
            env["HTTP_PROXY"] = upstream
            env["HTTPS_PROXY"] = upstream
        subprocess.Popen(
            [sys.executable, PROXY_SCRIPT, "19999"],
            creationflags=CREATE_FLAGS, close_fds=True, env=env,
            stdout=open(os.path.join(TEMP, "speedcore_proxy.log"), "a"),
            stderr=subprocess.STDOUT,
        )
        log("proxy started")
    except Exception as e:
        log(f"proxy start failed: {e}")


def start_tun():
    """Start TUN transparent proxy process"""
    log("Starting TUN...")
    try:
        subprocess.Popen(
            [sys.executable, TUN_SCRIPT, "start"],
            creationflags=CREATE_FLAGS, close_fds=True,
            stdout=open(os.path.join(TEMP, "speedcore_tun.log"), "a"),
            stderr=subprocess.STDOUT,
        )
        log("TUN started")
    except Exception as e:
        log(f"TUN start failed: {e}")


def get_upstream_proxy() -> str:
    try:
        cfg = os.path.join(ROOT, ".upstream_proxy")
        with open(cfg, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def run():
    log("=" * 40)
    log("Watchdog started")
    log(f"  PID: {os.getpid()}  Interval: {INTERVAL}s")
    log(f"  Checks: aria2c :16800 | proxy :19999 | TUN :19998")

    upstream = get_upstream_proxy()
    failures = {"aria2": 0, "proxy": 0, "tun": 0}

    while True:
        aria2_ok = check_aria2_rpc()
        proxy_ok = check_port(19999)
        tun_ok = check_port(19998) or check_tun_process()

        # ── aria2c ──
        if not aria2_ok:
            failures["aria2"] += 1
            log(f"[!] aria2c DOWN (#{failures['aria2']}) — restarting")
            start_aria2(upstream)
            time.sleep(5)
            if check_aria2_rpc():
                log("  aria2c recovered")
                failures["aria2"] = 0

        # ── proxy ──
        if not proxy_ok:
            failures["proxy"] += 1
            log(f"[!] proxy :19999 DOWN (#{failures['proxy']}) — restarting")
            start_proxy(upstream)
            time.sleep(3)
            if check_port(19999):
                log("  proxy recovered")
                failures["proxy"] = 0

        # ── TUN ──
        if not tun_ok:
            failures["tun"] += 1
            log(f"[!] TUN :19998 DOWN (#{failures['tun']}) — restarting")
            # Kill stale TUN process if PID file exists
            try:
                subprocess.run(f"taskkill /f /fi \"IMAGENAME eq python.exe\" /fi \"WINDOWTITLE eq *tun*\"",
                               shell=True, capture_output=True, timeout=3)
            except Exception:
                pass
            start_tun()
            time.sleep(5)
            if check_port(19998) or check_tun_process():
                log("  TUN recovered")
                failures["tun"] = 0

        # ── periodic status ──
        if aria2_ok and proxy_ok and tun_ok:
            reset = [k for k, v in failures.items() if v > 0]
            if reset:
                log(f"All healthy — reset: {reset}")
                for k in reset:
                    failures[k] = 0

        # ── escalation ──
        for name, count in failures.items():
            if count >= 10:
                log(f"[CRITICAL] {name} — 10 consecutive failures")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    print("SpeedCore Watchdog")
    print(f"  PID: {os.getpid()}  Log: {WATCHDOG_LOG}")
    print(f"  Guarding: aria2c :16800 | proxy :19999 | TUN :19998")
    run()
