#!/usr/bin/env python3
"""spd — SpeedCore CLI: 系统级多线程下载加速器。

架构:
  aria2c (32线程, RPC:16800)  ← 下载引擎
       ↑ JSON-RPC
  SpeedProxy (:19999)        ← 系统代理，拦截下载请求→aria2c加速
       ↑ 系统代理/PAC
  浏览器/所有HTTP下载         ← 用户无感，自动加速

用法:
  spd install      安装系统服务 (开机自启, SYSTEM账户, 隐藏)
  spd remove       卸载服务
  spd start        立即启动
  spd stop         停止
  spd status       查看状态
  spd optimize     一键TCP+网卡极限优化
  spd proxy on     配置系统代理 (浏览器流量自动走加速)
  spd proxy off    取消系统代理
  spd get <URL>    命令行下载 (直接走aria2c多线程)
  spd speedtest    测速
"""

import os
import sys
import json
import time
import socket
import subprocess
import urllib.request

if getattr(sys, 'frozen', False):
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
ARIA2C = os.path.join(ROOT, "aria2c.exe")
ARIA2_CONF = os.path.join(ROOT, "aria2.conf")
PROXY_SCRIPT = os.path.join(ROOT, "proxy.py")
ARIA2_RPC = "http://127.0.0.1:16800/jsonrpc"


# ─── JSON-RPC ────────────────────────────────────────────

def rpc(method: str, params: list = None) -> dict:
    data = json.dumps({
        "jsonrpc": "2.0", "id": "spd",
        "method": f"aria2.{method}",
        "params": params or []
    }).encode()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(ARIA2_RPC, data=data,
                                   headers={"Content-Type": "application/json"}),
            timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


# ─── 子命令 ──────────────────────────────────────────────

def cmd_install():
    """安装系统服务"""
    from svc import install_schtask
    install_schtask()


def cmd_remove():
    """卸载系统服务"""
    from svc import remove_schtask
    remove_schtask()


def cmd_start():
    """启动 SpeedCore"""
    from svc import _write_bootstrap, save_upstream_proxy
    save_upstream_proxy()  # 保存当前代理供SYSTEM bootstrap使用
    _write_bootstrap()
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    subprocess.Popen(
        [pythonw, os.path.join(ROOT, "_bootstrap.py")],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    print("SpeedCore 启动中...")
    time.sleep(3)

    # 检查
    ok = True
    try:
        v = rpc("getVersion")
        print(f"  aria2c RPC: [OK] v{v['result']['version']}")
    except Exception:
        print(f"  aria2c RPC: [FAIL] 未启动")
        ok = False

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    proxy_ok = sock.connect_ex(("127.0.0.1", 19999)) == 0
    sock.close()
    print(f"  Proxy:      {'[OK] :19999' if proxy_ok else '[FAIL] 未启动'}")

    if ok and proxy_ok:
        print("SpeedCore 就绪。")


def cmd_stop():
    """停止 SpeedCore"""
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("停止可能需要管理员权限。")

    for name in ["aria2c.exe"]:
        subprocess.run(
            f'taskkill /f /im "{name}"', shell=True,
            capture_output=True, timeout=5
        )
    print("SpeedCore 已停止。")


def cmd_status():
    """查看运行状态"""
    from svc import _check_processes
    _check_processes()

    # 活跃任务
    active = rpc("tellActive")
    if "result" in active:
        tasks = active["result"]
        if tasks:
            print(f"\n  活跃下载: {len(tasks)}")
            for t in tasks:
                fn = t.get("files", [{}])[0].get("path", "?").split("\\")[-1]
                done = int(t.get("completedLength", 0))
                total = int(t.get("totalLength", 0))
                speed = int(t.get("downloadSpeed", 0))
                pct = f"{done * 100 / total:.0f}%" if total else "?"
                speed_str = f"{speed / 1048576:.1f}MB/s" if speed > 0 else "-"
                print(f"    [{pct}] {fn[:40]}  {speed_str}")
        else:
            print("\n  无活跃下载。")


def cmd_optimize():
    """TCP/IP 栈 + 网卡极限优化"""
    print("=" * 50)
    print("SpeedCore TCP/IP 极限优化")
    print("=" * 50)
    print()

    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("需要管理员权限。请以管理员身份运行此命令。")
        return

    from tcpopt import apply_all, apply_nic_batch
    r = apply_all()
    print(f"注册表优化: {r['ok']} 项已设置, {r['skip']} 项跳过")
    apply_nic_batch()
    print("网卡优化: 已完成")
    print()
    print("部分设置需要重启生效。")
    print("运行 'spd status' 确认或 'netsh int tcp show global' 验证。")


# ─── 代理状态检测 ──────────────────────────────────────────

PROXY_BACKUP_FILE = os.path.join(ROOT, ".proxy_backup.json")


def detect_proxy_state() -> dict:
    """全面检测当前系统代理状态。

    检查来源: WinINET注册表 / WinHTTP / 环境变量 / PAC文件
    返回:
      has_external_proxy: 是否存在非SpeedCore的外部代理
      wininet_enabled/server/pac
      winhttp_server
      env_http_proxy / env_https_proxy
      is_speedcore: 当前代理是否就是SpeedCore自己
    """
    result = {
        "has_external_proxy": False,
        "is_speedcore": False,
        "wininet_enabled": False,
        "wininet_server": None,
        "wininet_pac": None,
        "winhttp_server": None,
        "env_http_proxy": None,
        "env_https_proxy": None,
        "all_sources": [],
    }

    # 1. WinINET (IE/Edge/Chrome 等浏览器)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            result["wininet_enabled"] = bool(enabled)
        except OSError:
            pass
        try:
            result["wininet_server"] = winreg.QueryValueEx(key, "ProxyServer")[0]
            if result["wininet_server"]:
                result["all_sources"].append(f"WinINET: {result['wininet_server']}")
        except OSError:
            pass
        try:
            result["wininet_pac"] = winreg.QueryValueEx(key, "AutoConfigURL")[0]
            if result["wininet_pac"]:
                result["all_sources"].append(f"PAC: {result['wininet_pac']}")
        except OSError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass

    # 2. WinHTTP (系统服务/后台进程)
    try:
        r = subprocess.run(
            'netsh winhttp show proxy', shell=True,
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            line_s = line.strip()
            if "代理服务器" in line_s or "Proxy Server" in line_s:
                val = line_s.split(":", 1)[-1].strip()
                if val and val != "直接访问(没有代理服务器)" and "Direct" not in val:
                    result["winhttp_server"] = val
                    result["all_sources"].append(f"WinHTTP: {val}")
                    break
    except Exception:
        pass

    # 3. 环境变量
    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        val = os.environ.get(var)
        if val:
            result[f"env_{var.lower()}"] = val
            result["all_sources"].append(f"ENV {var}={val}")

    # 4. 综合判断
    # 外部代理: WinINET已启用(非19999) OR PAC非19999 OR WinHTTP OR 环境变量
    # 注意: wininet_server有值但未启用的也算（用户挂代理时自动识别）
    has_wininet_other = bool(
        (result["wininet_enabled"] and result["wininet_server"]
         and ":19999" not in str(result["wininet_server"]))
    )
    has_pac_other = bool(
        result["wininet_pac"] and ":19999" not in str(result["wininet_pac"])
    )
    result["has_external_proxy"] = bool(
        has_wininet_other
        or has_pac_other
        or result["winhttp_server"]
        or result["env_http_proxy"]
        or result["env_https_proxy"]
    )

    result["is_speedcore"] = bool(
        (result["wininet_server"] and "19999" in str(result["wininet_server"]))
        or (result["wininet_pac"] and "19999" in str(result["wininet_pac"]))
    )

    return result


def _save_proxy_state(state: dict):
    """备份当前代理状态，用于后续恢复"""
    import json
    with open(PROXY_BACKUP_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _load_proxy_backup() -> dict:
    import json
    try:
        with open(PROXY_BACKUP_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


# ─── 代理命令 ──────────────────────────────────────────────

def cmd_proxy(action: str = "status", force: bool = False):
    """配置系统代理

    spd proxy on      启用SpeedCore代理 (自动检测外部代理)
    spd proxy on -f   强制启用，不询问
    spd proxy off     恢复原始代理
    spd proxy status  查看状态
    spd proxy auto    自动检测并选择最佳模式
    spd proxy detect  仅检测并报告代理环境
    spd proxy save    保存当前代理配置 (供SYSTEM服务使用)
    """
    if action == "on":
        _proxy_on(force)
    elif action == "off":
        _proxy_off()
    elif action == "status":
        _proxy_status()
    elif action in ("auto", "detect"):
        _proxy_detect(interactive=(action == "auto"))
    elif action == "save":
        from svc import save_upstream_proxy
        save_upstream_proxy()
        print("代理配置已保存。重启 SpeedCore 生效: python svc.py restart")


def _proxy_detect(interactive: bool = True):
    """检测并报告代理环境，auto模式还会给出建议"""
    state = detect_proxy_state()

    print("=== 代理环境检测 ===")
    print()

    if state["is_speedcore"]:
        print("  当前代理: SpeedCore (本工具)")

    # WinINET
    print(f"  WinINET (浏览器): ", end="")
    if state["wininet_enabled"]:
        print(f"已启用 → {state['wininet_server']}")
    elif state["wininet_pac"]:
        print(f"PAC → {state['wininet_pac']}")
    else:
        print("未配置")

    # WinHTTP
    print(f"  WinHTTP (系统服务): ", end="")
    if state["winhttp_server"]:
        print(state["winhttp_server"])
    else:
        print("未配置 (直连)")

    # 环境变量
    if state["env_http_proxy"]:
        print(f"  HTTP_PROXY: {state['env_http_proxy']}")
    if state["env_https_proxy"]:
        print(f"  HTTPS_PROXY: {state['env_https_proxy']}")

    print()

    if state["has_external_proxy"]:
        print("  [!] 检测到外部代理:")
        for src in state["all_sources"]:
            print(f"      {src}")
        print()
        if interactive:
            print("  建议:")
            if ":19999" not in str(state.get("wininet_server", "")):
                print("    当前外部代理与SpeedCore不冲突（不同端口）。")
                print("    运行 'spd proxy on' 将覆盖为SpeedCore。")
                print("    运行 'spd proxy off' 无法自动恢复外部代理。")
                print("    如需保留外部代理: 先手动记下代理地址，或使用 'spd proxy save'")
                print()
                print("    运行 'spd proxy on -f' 强制覆盖。")
    else:
        print("  未检测到外部代理。可直接 'spd proxy on' 启用加速。")


def _proxy_on(force: bool = False):
    """启用系统代理 → 指向 SpeedCore Proxy"""
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        print("需要管理员权限来设置系统代理。")
        print('  powershell -Command "Start-Process python -ArgumentList \'spd.py proxy on\' -Verb RunAs"')
        return

    # 检测现有代理
    current = detect_proxy_state()

    if current["has_external_proxy"] and not force:
        print("[!] 检测到外部代理正在使用:")
        for src in current["all_sources"]:
            print(f"    {src}")
        print()
        print("启用SpeedCore代理将覆盖当前设置。")
        print("覆盖后 'spd proxy off' 无法恢复外部代理。")
        print()
        print("请选择:")
        print("  spd proxy on -f   强制覆盖（外部代理将被备份）")
        print("  spd proxy auto    自动适配（建议）")
        return

    # 备份当前状态（无论是否为外部代理，都备份以备恢复）
    _save_proxy_state(current)

    pac_url = "http://127.0.0.1:19999/proxy.pac"
    proxy_server = "127.0.0.1:19999"
    import winreg

    # WinINET 代理
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_server)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        winreg.CloseKey(key)
        print("[OK] WinINET 代理: 127.0.0.1:19999")
    except Exception as e:
        print(f"[FAIL] WinINET: {e}")

    # WinHTTP (系统服务)
    subprocess.run(
        f'netsh winhttp set proxy proxy-server="{proxy_server}" bypass-list="<local>"',
        shell=True, capture_output=True, timeout=5
    )

    # PAC (智能路由 — 仅下载走代理)
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, pac_url)
        winreg.CloseKey(key)
        print(f"[OK] PAC 智能路由: {pac_url}")
    except Exception as e:
        print(f"[WARN] PAC: {e}")

    if current["has_external_proxy"]:
        backup_info = "; ".join(current["all_sources"])
        print(f"[i]  原代理已备份: {backup_info}")

    print()
    print("浏览器下载流量自动通过 SpeedCore 多线程加速。")
    print("运行 'spd proxy off' 恢复到直连。")
    _flush_proxy()


def _proxy_off():
    """恢复系统代理到直连（或还原备份的外部代理）"""
    import winreg

    backup = _load_proxy_backup()

    if backup.get("has_external_proxy"):
        # 有备份的外部代理 → 还原
        print("[i] 检测到之前的外部代理备份，尝试还原...")
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                   r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            if backup.get("wininet_enabled") and backup.get("wininet_server"):
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, backup["wininet_server"])
                print(f"[OK] 已还原 WinINET: {backup['wininet_server']}")
            elif backup.get("wininet_pac"):
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, backup["wininet_pac"])
                print(f"[OK] 已还原 PAC: {backup['wininet_pac']}")
            else:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                _delete_safe(key, "ProxyServer")
                _delete_safe(key, "AutoConfigURL")
                print("[OK] 已恢复直连")
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[WARN] 还原失败 ({e})，恢复直连")
            _reset_to_direct()

        # 清除备份
        try:
            os.unlink(PROXY_BACKUP_FILE)
        except Exception:
            pass
    else:
        _reset_to_direct()

    _flush_proxy()


def _reset_to_direct():
    """重置所有代理为直连"""
    import winreg
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _delete_safe(key, "ProxyServer")
        _delete_safe(key, "AutoConfigURL")
        winreg.CloseKey(key)
    except OSError:
        pass
    subprocess.run('netsh winhttp reset proxy', shell=True,
                   capture_output=True, timeout=5)
    print("[OK] 系统代理已恢复直连。")


def _delete_safe(key, name):
    try:
        winreg.DeleteValue(key, name)
    except OSError:
        pass


def _proxy_status():
    """显示详细代理状态"""
    state = detect_proxy_state()

    if state["is_speedcore"]:
        print("  加速状态: SpeedCore 运行中")
    elif state["has_external_proxy"]:
        print("  加速状态: 外部代理活跃（非SpeedCore）")
    else:
        print("  加速状态: 直连（未启用加速）")
    print()

    print("  WinINET (浏览器):")
    if state["wininet_enabled"]:
        print(f"    代理: {state['wininet_server']}")
    else:
        print(f"    代理: 关闭")
    if state["wininet_pac"]:
        print(f"    PAC:  {state['wininet_pac']}")

    print(f"  WinHTTP (服务):", state["winhttp_server"] or "直连")

    if state["env_http_proxy"]:
        print(f"  HTTP_PROXY:  {state['env_http_proxy']}")
    if state["env_https_proxy"]:
        print(f"  HTTPS_PROXY: {state['env_https_proxy']}")

    # 检查备份文件
    if os.path.exists(PROXY_BACKUP_FILE):
        backup = _load_proxy_backup()
        sources = backup.get("all_sources", [])
        if sources:
            print(f"\n  备份的外部代理: {'; '.join(sources)}")


def _flush_proxy():
    """刷新代理设置 — 通知运行中的应用"""
    try:
        import ctypes
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)
    except Exception:
        pass


def cmd_get(url: str, filename: str = None):
    """命令行多线程下载 — 直接走aria2c"""
    if not filename:
        filename = os.path.basename(url.split("?")[0]) or "download"

    # 确保 aria2c 运行
    v = rpc("getVersion")
    if "error" in v:
        print("aria2c 未运行。请先执行 'spd start'")
        return

    gid = rpc("addUri", [[url], {"out": filename, "dir": os.getcwd()}])
    if "error" in gid:
        print(f"[FAIL] {gid['error']}")
        return

    gid = gid["result"]
    print(f"==> {filename}")
    print(f"   GID: {gid}")

    last = 0
    try:
        while True:
            s = rpc("tellStatus", [gid, ["status", "completedLength",
                                          "totalLength", "downloadSpeed"]])
            if "error" in s:
                print(f"\n[FAIL] 任务异常: {s['error']}")
                break

            st = s["result"]
            status = st.get("status", "?")
            done = int(st.get("completedLength", 0))
            total = int(st.get("totalLength", 0))
            speed = int(st.get("downloadSpeed", 0))

            if done != last:
                pct = f"{done * 100 / total:.1f}%" if total else "?"
                speed_str = f"{speed / 1048576:.1f}MB/s" if speed > 0 else "-"
                size_done = f"{done / 1048576:.1f}MB"
                size_total = f"{total / 1048576:.1f}MB" if total else "?"
                print(f"\r  {pct}  {size_done}/{size_total}  {speed_str}  ",
                      end="", flush=True)
                last = done

            if status in ("complete", "error", "removed"):
                print()
                if status == "complete":
                    print(f"[OK] 下载完成: {os.path.join(os.getcwd(), filename)}")
                elif status == "error":
                    err = st.get("errorMessage", "未知错误")
                    print(f"[FAIL] {err}")
                break

            time.sleep(0.3)
    except KeyboardInterrupt:
        rpc("remove", [gid])
        rpc("removeDownloadResult", [gid])
        print("\n[STOP] 已取消")


def cmd_speedtest():
    """下载测速 — 使用已知快速CDN资源"""
    test_urls = [
        ("http://speedtest.tele2.net/100MB.zip", "100MB"),
        ("http://ipv4.download.thinkbroadband.com/100MB.zip", "100MB (Thinkbroadband)"),
    ]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    proxy_running = sock.connect_ex(("127.0.0.1", 19999)) == 0
    sock.close()

    if proxy_running:
        print("SpeedCore Proxy 已就绪。使用 'spd proxy on' 启用系统代理后可在浏览器测试。")
        print()

    v = rpc("getVersion")
    if "error" in v:
        print("aria2c 未运行。请先执行 'spd start'")
        return

    print("单线程基准测试...")
    total_speed = 0
    count = 0

    for url, label in test_urls[:1]:  # 只跑一个避免太长
        print(f"  测试资源: {label}")
        print(f"  资源URL: {url}")

        gid = rpc("addUri", [[url], {"dir": os.environ.get("TEMP", "C:\\Windows\\Temp")}])
        if "error" in gid:
            print(f"  [FAIL] {gid['error']}")
            continue

        gid = gid["result"]
        speeds = []
        start = time.time()

        try:
            while time.time() - start < 15:
                s = rpc("tellStatus", [gid, ["status", "downloadSpeed",
                                              "completedLength"]])
                if "error" in s:
                    break
                st = s.get("result", {})
                speed = int(st.get("downloadSpeed", 0))
                if speed > 0:
                    speeds.append(speed)
                if st.get("status") in ("complete", "error", "removed"):
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        rpc("remove", [gid])
        rpc("removeDownloadResult", [gid])

        if speeds:
            avg = sum(speeds) / len(speeds)
            peak = max(speeds)
            avg_mb = avg / 1048576
            peak_mb = peak / 1048576
            print(f"  平均速度: {avg_mb:.1f} MB/s")
            print(f"  峰值速度: {peak_mb:.1f} MB/s")
            total_speed += avg
            count += 1

    if count > 0:
        avg = total_speed / count
        print(f"\n整体平均: {avg / 1048576:.1f} MB/s")
        if avg < 5 * 1048576:
            print("[TIP] 建议: 运行 'spd optimize' 进行TCP优化（需管理员权限）")


# ─── 入口 ──────────────────────────────────────────────────

USAGE = """
spd — SpeedCore 系统级多线程下载加速器

  服务管理:
    spd install       安装系统服务 (开机自启, SYSTEM隐藏运行)
    spd remove        卸载服务
    spd start         立即启动 aria2c + 代理
    spd stop          停止所有进程
    spd status        查看运行状态

  优化:
    spd optimize      TCP/IP栈 + 网卡极限优化 (需管理员权限)

  代理 (自动识别外部代理):
    spd proxy detect  检测当前代理环境
    spd proxy auto    自动适配最佳模式
    spd proxy on      启用加速代理 (检测到外部代理时会警告)
    spd proxy on -f   强制启用 (跳过警告，备份原代理)
    spd proxy off     恢复 (有备份则还原外部代理，无备份则直连)
    spd proxy status  查看代理状态

  下载:
    spd get <URL>     CLI多线程下载 (直连aria2c)
    spd speedtest     测速

  示例:
    spd proxy detect  # 先检测环境
    spd start         # 启动引擎
    spd proxy on      # 启用加速（自动检测冲突）
    spd speedtest     # 验证效果
"""


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        return

    cmd = sys.argv[1].lower()

    if cmd == "install":
        cmd_install()
    elif cmd == "remove":
        cmd_remove()
    elif cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "optimize":
        cmd_optimize()
    elif cmd == "proxy":
        action = sys.argv[2] if len(sys.argv) > 2 else "status"
        force = "-f" in sys.argv or "--force" in sys.argv
        if action in ("-f", "--force"):
            action = "on"
            force = True
        cmd_proxy(action, force)
    elif cmd == "get":
        if len(sys.argv) < 3:
            print("用法: spd get <URL> [文件名]")
            return
        url = sys.argv[2]
        fn = sys.argv[3] if len(sys.argv) > 3 else None
        cmd_get(url, fn)
    elif cmd == "speedtest":
        cmd_speedtest()
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
