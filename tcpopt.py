"""Windows TCP/IP 栈极限优化 — 突破单连接物理吞吐上限。

原理:
  CTCP (Compound TCP) — 微软的高BDP拥塞控制，结合Loss-based+Delay-based
  RSS — 网卡多队列分发到多CPU核心，避免单核瓶颈
  TCP Window Scaling (RFC 1323) — 高延迟网络下最大化带宽利用
  Nagle禁用 — 消灭小包40ms延迟累积
  Chimney Offload — TCP连接卸载到网卡硬件处理

需管理员权限运行。
"""

import ctypes
import os
import sys
import winreg

TCPIP = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
AFD = r"SYSTEM\CurrentControlSet\Services\AFD\Parameters"
MMEDIA = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
NETBIOS = r"SYSTEM\CurrentControlSet\Services\NetBT\Parameters"
NLA = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\DefaultMediaCost"

TWEAKS = {
    # 拥塞控制: CTCP (Compound TCP) — 高BDP场景比CUBIC好
    TCPIP: {
        "TCPCongestionControl": "ctcp",   # 0=CUBIC, 1=CTCP, 2=DCTCP
        # 时间戳+RFC1323窗口缩放 (bit0=timestamps, bit1=window_scaling, bit2=PAWS, bit3=ECN)
        "Tcp1323Opts":        3,          # 窗口缩放+时间戳
        # 全局最大TCP窗口 (1GB) — 实际受per-connection自动调优限制
        "GlobalMaxTcpWindowSize": 0x3FFFFFFF,
        "TcpTimedWaitDelay":   30,         # TIME_WAIT缩短 (默认120-240s)
        "MaxUserPort":         65534,      # 最大可用端口数
        "TcpNumConnections":   16777214,   # 最大TCP连接数
        "MaxFreeTcbs":         72000,      # TCB池大小
        "MaxHashTableSize":    65536,      # TCP连接哈希表
        "SackOpts":            1,          # 选择性确认
        "DefaultTTL":          64,
        "EnablePMTUDiscovery": 1,          # 路径MTU发现
        "EnablePMTUBHDetect":  1,          # 黑洞路由器检测
        "DisableTaskOffload":  0,          # 保持网卡卸载 (Chimney)
        "EnableTCPA":          1,          # TCP Chimney Offload
        "EnableRSS":           1,          # 接收端缩放
        "EnableTCPChimney":    1,          # TCP连接卸载到网卡
        "TCPNoDelay":          1,          # 禁用Nagle算法
        "TcpAckFrequency":     1,          # 每个包都ACK
        "TCPDelAckTicks":      0,          # 禁用延迟ACK
    },
    # AFD (Ancillary Function Driver) 参数 — 影响Winsock缓冲区
    AFD: {
        "DefaultReceiveWindow":    0x00F00000,  # ~15MB 接收窗口
        "DefaultSendWindow":       0x00F00000,  # ~15MB 发送窗口
        "LargeBufferSize":         0x00080000,  # ~512KB
        "MediumBufferSize":        0x00040000,  # ~256KB
        "SmallBufferSize":         0x00002000,  # ~8KB
        "TransmitWorker":          32,
        "FastSendDatagramThreshold": 65536,
        "PriorityBoost":           0,           # 禁用优先级提升
    },
    # 多媒体调度 — 禁用网络节流 (默认限制10%带宽给MMCSS)
    MMEDIA: {
        "NetworkThrottlingIndex": 0xFFFFFFFF,   # 完全禁用网络节流
        "SystemResponsiveness":   0,            # 禁用后台服务响应延迟
    },
    # NetBIOS over TCP — 禁用减少开销
    NETBIOS: {
        "EnableLMHOSTS":          0,
        "NoNameReleaseOnDemand":  1,
        "NodeType":               2,
    },
    # 网络位置感知 — 去除非家庭/工作网络的限制
    NLA: {
        "Default": 1,
    },
}

BATCH_CMD = r"""@echo off
rem 网卡高级设置 — 批量优化所有网络适配器
for /f "tokens=*" %%i in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}" /f "PCI" /k 2^>nul ^| find "HKEY"') do (
  rem 中断裁决 (Interrupt Moderation) → 关闭 (减少延迟)
  reg add "%%i" /v "*InterruptModeration" /t REG_SZ /d "0" /f >nul 2>&1
  rem 接收缓冲区 → 最大
  reg add "%%i" /v "*ReceiveBuffers" /t REG_SZ /d "4096" /f >nul 2>&1
  rem 发送缓冲区 → 最大
  reg add "%%i" /v "*TransmitBuffers" /t REG_SZ /d "4096" /f >nul 2>&1
  rem RSS 队列 → 最大
  reg add "%%i" /v "*RSSProfile" /t REG_SZ /d "4" /f >nul 2>&1
  rem 大量发送卸载 → 启用
  reg add "%%i" /v "*LsoV2IPv4" /t REG_SZ /d "1" /f >nul 2>&1
  rem 接收端合并 → 关闭 (减少延迟)
  reg add "%%i" /v "*RSCIPv4" /t REG_SZ /d "0" /f >nul 2>&1
  rem 流控制 → 关闭
  reg add "%%i" /v "*FlowControl" /t REG_SZ /d "0" /f >nul 2>&1
  rem 节能以太网 → 关闭
  reg add "%%i" /v "*EEELinkAdvertisement" /t REG_SZ /d "0" /f >nul 2>&1
  rem Jumbo包 → 9K
  reg add "%%i" /v "*JumboPacket" /t REG_SZ /d "9014" /f >nul 2>&1
  rem 校验和卸载 → 全部启用
  reg add "%%i" /v "*TCPChecksumOffloadIPv4" /t REG_SZ /d "3" /f >nul 2>&1
  reg add "%%i" /v "*UDPChecksumOffloadIPv4" /t REG_SZ /d "3" /f >nul 2>&1
  rem VLAN 硬件卸载 → 启用
  reg add "%%i" /v "*PriorityVLANTag" /t REG_SZ /d "3" /f >nul 2>&1
  rem NS 卸载 → 启用
  reg add "%%i" /v "*PMNSOffload" /t REG_SZ /d "1" /f >nul 2>&1
  rem ARP 卸载 → 启用
  reg add "%%i" /v "*PMARPOffload" /t REG_SZ /d "1" /f >nul 2>&1
)
rem DNS 缓存扩大
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Dnscache\Parameters" /v "MaxCacheTtl" /t REG_DWORD /d 86400 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Dnscache\Parameters" /v "MaxNegativeCacheTtl" /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\Dnscache\Parameters" /v "CacheHashTableBucketSize" /t REG_DWORD /d 16 /f >nul 2>&1

echo TCP/IP optimization batch complete.
netsh int tcp set global autotuninglevel=normal >nul 2>&1
netsh int tcp set global chimney=enabled >nul 2>&1
netsh int tcp set global rss=enabled >nul 2>&1
netsh int tcp set global initialRto=2000 >nul 2>&1
netsh int tcp set supplemental custom congestionprovider=ctcp >nul 2>&1
echo netsh settings applied.
"""


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def apply_tweak(path: str, name: str, value):
    """设置注册表 DWORD/SZ 值，自动判断类型"""
    if isinstance(value, str):
        vtype = winreg.REG_SZ
    elif isinstance(value, int):
        vtype = winreg.REG_DWORD
    elif isinstance(value, list):
        vtype = winreg.REG_MULTI_SZ
    else:
        vtype = winreg.REG_SZ

    try:
        key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, path)
        winreg.SetValueEx(key, name, 0, vtype, value)
        winreg.CloseKey(key)
        return True
    except OSError as e:
        print(f"  SKIP {path}\\{name}: {e}")
        return False


def apply_all(dry_run: bool = False) -> dict:
    """应用所有 TCP 优化，返回结果统计"""
    results = {"ok": 0, "skip": 0}

    for path, params in TWEAKS.items():
        if dry_run:
            print(f"[{path}]")
        for name, value in params.items():
            if dry_run:
                print(f"  {name} = {value}")
                results["ok"] += 1
            else:
                if apply_tweak(path, name, value):
                    results["ok"] += 1
                else:
                    results["skip"] += 1

    # netsh 命令
    if not dry_run:
        import subprocess
        cmds = [
            'netsh int tcp set global autotuninglevel=normal',
            'netsh int tcp set global chimney=enabled',
            'netsh int tcp set global rss=enabled',
            'netsh int tcp set global initialRto=2000',
            'netsh int tcp set supplemental custom congestionprovider=ctcp',
        ]
        for cmd in cmds:
            try:
                subprocess.run(cmd, shell=True, check=False,
                               capture_output=True, timeout=10)
            except Exception:
                pass

    return results


def apply_nic_batch() -> bool:
    """运行网卡批量优化批处理"""
    import subprocess
    import tempfile
    bat_path = os.path.join(tempfile.gettempdir(), "_spd_nic_opt.cmd")
    with open(bat_path, "w") as f:
        f.write(BATCH_CMD)
    try:
        subprocess.run([bat_path], shell=True, check=False,
                       capture_output=True, timeout=30)
        return True
    except Exception:
        return False
    finally:
        try:
            os.unlink(bat_path)
        except Exception:
            pass


def status() -> list:
    """检查当前 TCP 优化状态"""
    import subprocess
    lines = []
    try:
        r = subprocess.run("netsh int tcp show global", shell=True,
                           capture_output=True, text=True, timeout=5)
        lines.extend(r.stdout.strip().splitlines())
    except Exception:
        pass

    r2 = subprocess.run(
        'reg query "HKLM\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters" /v TCPCongestionControl 2>nul',
        shell=True, capture_output=True, text=True, timeout=5)
    if r2.stdout.strip():
        lines.append(f"CongestionControl: {r2.stdout.strip().split()[-1]}")

    return lines


def revert():
    """恢复 TCP 设置为默认"""
    import subprocess
    tweaks = {
        TCPIP: ["TCPCongestionControl", "Tcp1323Opts", "GlobalMaxTcpWindowSize",
                "TcpTimedWaitDelay", "MaxUserPort", "TcpNumConnections",
                "EnableTCPA", "EnableRSS", "EnableTCPChimney", "TCPNoDelay"],
        AFD: ["DefaultReceiveWindow", "DefaultSendWindow"],
        MMEDIA: ["NetworkThrottlingIndex", "SystemResponsiveness"],
    }
    for path, names in tweaks.items():
        for name in names:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path,
                                     0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, name)
                winreg.CloseKey(key)
            except OSError:
                pass

    cmds = [
        'netsh int tcp set global autotuninglevel=normal',
        'netsh int tcp set global chimney=default',
        'netsh int tcp set global rss=default',
        'netsh int tcp set global initialRto=1000',
    ]
    for cmd in cmds:
        subprocess.run(cmd, shell=True, check=False,
                       capture_output=True, timeout=5)
    return True


if __name__ == "__main__":
    if not is_admin():
        print("需要管理员权限。请以管理员身份运行。")
        print("  powershell -Command \"Start-Process python -ArgumentList 'tcpopt.py' -Verb RunAs\"")
        sys.exit(1)

    if "--dry-run" in sys.argv:
        apply_all(dry_run=True)
    elif "--revert" in sys.argv:
        revert()
        print("TCP 设置已恢复默认。重启生效。")
    elif "--status" in sys.argv:
        for line in status():
            print(line)
    else:
        r = apply_all()
        apply_nic_batch()
        print(f"\nTCP 优化完成: {r['ok']} 项已设置, {r['skip']} 项跳过")
        print("部分设置需重启生效。")
