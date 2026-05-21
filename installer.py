"""SpeedCore Setup — 一键安装到系统，注册开机自启服务。

用法:
  SpeedCore-Setup.exe              # 安装到默认目录
  SpeedCore-Setup.exe /S /D=DIR    # 静默安装到指定目录（自动接受许可）
  SpeedCore-Setup.exe /uninstall   # 卸载
"""

import os, sys, shutil, subprocess, ctypes

DEFAULT_DIR = r"C:\Program Files\SpeedCore"
FILES = ["aria2c.exe", "aria2.conf", "WinDivert.dll", "WinDivert64.sys"]

# ═══════════════════════════════════════════════════════════════
# 法律声明 / Legal Disclaimer
# ═══════════════════════════════════════════════════════════════

LICENSE_ZH = r"""
                    SpeedCore 软件许可与免责声明

  版权所有 (c) 2026 Xiangye

  本软件按非商业使用许可发布。个人、教育机构和非营利组织可在非商业
  目的下免费使用、复制、修改和分发本软件。商业使用（包括但不限于企业部署、商业捆绑、SaaS/云服务集成）须获得作者书面授权。

  本软件按「原样」提供，不作任何明示或默示的保证，包括但不限于对适销性、
  特定用途适用性和非侵权性的保证。在任何情况下，作者或版权持有人均不对
  因本软件或本软件的使用或其他交易而产生的任何索赔、损害或其他责任负责，
  无论是在合同、侵权还是其他方面。

  ─────────────────────────────────────────────────────────────

  重要提示 — 使用本软件即表示您同意以下条款：

  1. 网络使用合规
     本软件通过多线程并发下载技术提升下载速度。用户有责任确保其使用
     本软件的行为符合所在地区法律法规、网络服务提供商(ISP)的服务条款
     及目标服务器的使用政策。因违反上述规定导致的一切后果由用户自行承担。

  2. TCP/IP 优化风险
     本软件包含 Windows 注册表和网络协议栈优化功能。修改系统注册表存在
     理论风险，极端情况下可能影响网络连接稳定性。优化操作在执行前会
     提示确认，且提供回退命令（spd tcpopt --revert）。

  3. 第三方组件
     本软件集成了 aria2（GNU LGPL v2.1 许可证）。aria2 的完整许可证文本
     可在其官方网站获取：https://github.com/aria2/aria2

  4. 代理与网络流量
     本软件在安装时检测并记录当前系统代理设置，用于将非下载流量（网页
     浏览、API 请求）转发至上游代理。本软件不会修改、记录或上传您的
     网络流量数据。所有数据仅在本地处理。

  5. 数据隐私
     本软件不收集任何用户数据，不连接任何外部服务器（除您主动发起的
     下载任务和测速请求外），不包含任何遥测、广告或跟踪代码。

  6. 免责声明
     本软件不保证下载速度的提升幅度。实际效果取决于 ISP 带宽、目标服务器
     限速策略、网络拥塞状况、本地硬件性能等多种因素。作者不对因使用或
     无法使用本软件而导致的任何直接或间接损失承担责任。

  7. 管理员权限
     安装和部分优化功能需要 Windows 管理员权限，因为需要修改系统代理
     设置、注册系统服务和写入注册表。这是 Windows 操作系统的安全机制
     要求，非本软件特有。
"""

LICENSE_EN = r"""
                SpeedCore Software License & Disclaimer

  Copyright (c) 2026 Xiangye

  This software is licensed under a Non-Commercial Use License. Personal,
  educational, and non-profit use is permitted free of charge. Commercial
  use (including enterprise deployment, bundling, SaaS/cloud integration)
  requires explicit written authorization from the author.
  
  
  

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
  EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
  MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.

  ─────────────────────────────────────────────────────────────

  IMPORTANT — By using this software you agree to the following:

  1. Network Compliance
     This software accelerates downloads via multi-threaded concurrent
     connections. Users are responsible for ensuring their usage complies
     with local laws, ISP terms of service, and target server policies.
     The author assumes no liability for violations.

  2. TCP/IP Optimization Risk
     This software includes registry and network stack optimization.
     Modifying system registries carries theoretical risks. A revert
     command (spd tcpopt --revert) is provided.

  3. Third-Party Components
     This software bundles aria2 (GNU LGPL v2.1).
     Full license: https://github.com/aria2/aria2

  4. Proxy & Network Traffic
     This software detects system proxy settings during installation
     for upstream passthrough of non-download traffic. No network data
     is modified, logged, or uploaded. All processing is local.

  5. Data Privacy
     This software collects NO user data, connects to NO external
     servers (except user-initiated downloads and speed tests), and
     contains NO telemetry, advertising, or tracking code.

  6. Disclaimer
     Download speed improvement is not guaranteed. Actual performance
     depends on ISP bandwidth, server rate limits, network congestion,
     and local hardware. The author is not liable for any direct or
     indirect damages resulting from use or inability to use this software.

  7. Administrator Privileges
     Installation and certain optimization features require Windows
     administrator privileges to modify system proxy settings,
     register system services, and write to the registry. This is a
     Windows security requirement, not specific to this software.
"""

LICENSE_FOOTER_ZH = """
  输入 Y 或按下回车键即表示您已阅读、理解并同意以上全部条款。
  输入 N 将退出安装程序。
"""

LICENSE_FOOTER_EN = """
  Enter Y or press Enter to indicate you have read, understood,
  and agree to all the above terms.
  Enter N to exit the installer.
"""


def safe_input(prompt: str = "") -> str | None:
    """input() with EOFError guard — returns None on EOF"""
    try:
        return input(prompt)
    except EOFError:
        return None


def show_license() -> bool:
    """显示许可协议，返回用户是否同意"""
    print()
    print("=" * 60)
    print("  SpeedCore — 多线程下载加速器 安装程序")
    print("  SpeedCore — Multi-Threaded Download Accelerator Setup")
    print("=" * 60)

    # Print license in pages (20 lines at a time)
    full_text = LICENSE_ZH + "\n" + LICENSE_EN
    lines = full_text.strip().splitlines()

    page_size = 20
    total_pages = (len(lines) + page_size - 1) // page_size
    current_page = 0

    while current_page < total_pages:
        start = current_page * page_size
        end = min(start + page_size, len(lines))
        for line in lines[start:end]:
            print(line)
        current_page += 1
        if current_page < total_pages:
            print()
            print(f"  --- 第 {current_page}/{total_pages} 页, 按回车继续 ---")
            print(f"  --- Page {current_page}/{total_pages}, press Enter to continue ---")
            if safe_input() is None:
                # Non-interactive — show all pages at once
                pass

    print()
    print("=" * 60)
    print(LICENSE_FOOTER_ZH)
    print(LICENSE_FOOTER_EN)
    print("=" * 60)
    print()

    while True:
        choice = safe_input("  [Y/N] ")
        if choice is None:
            print()
            print("  Non-interactive mode — license acceptance required.")
            print("  非交互模式 — 必须手动接受许可协议。")
            print("  Use /S for silent install with auto-accept.")
            print("  使用 /S 参数进行静默安装（自动接受许可）。")
            return False
        choice = choice.strip().lower()
        if choice in ("y", "yes", ""):
            return True
        elif choice in ("n", "no"):
            return False
        print("  请输入 Y (同意/Agree) 或 N (拒绝/Decline)")


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_install_dir():
    if "/D=" in sys.argv:
        for a in sys.argv:
            if a.startswith("/D="):
                return a[3:].strip('"')
    if len(sys.argv) > 2 and sys.argv[1] not in ("/S", "/silent", "/uninstall"):
        d = sys.argv[2]
        if os.path.isabs(d):
            return d
    return DEFAULT_DIR


def get_source_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def install(install_dir: str):
    print()
    print("=" * 50)
    print("  SpeedCore 安装中...")
    print("  SpeedCore Installing...")
    print("=" * 50)
    print()
    print(f"  Install Dir: {install_dir}")
    print()

    os.makedirs(install_dir, exist_ok=True)
    src = get_source_dir()

    # Copy binary files
    for fn in FILES:
        sp = os.path.join(src, fn)
        dp = os.path.join(install_dir, fn)
        if os.path.exists(sp):
            shutil.copy2(sp, dp)
            size_kb = os.path.getsize(dp) // 1024
            print(f"  [OK] {fn} ({size_kb} KB)")
        else:
            print(f"  [MISS] {fn} not found in package")

    # Copy Python scripts
    py_files = ["__init__.py", "spd.py", "svc.py", "proxy.py", "tcpopt.py", "divert.py", "tun.py"]
    for fn in py_files:
        sp = os.path.join(src, fn)
        dp = os.path.join(install_dir, fn)
        if os.path.exists(sp):
            shutil.copy2(sp, dp)

    print()

    # Stop old service
    print("  Stopping old service...")
    subprocess.run(
        'schtasks /end /tn "SpeedCore"', shell=True,
        capture_output=True, timeout=5
    )
    subprocess.run(
        'schtasks /delete /tn "SpeedCore" /f', shell=True,
        capture_output=True, timeout=5
    )
    subprocess.run(
        'taskkill /f /im aria2c.exe 2>nul', shell=True,
        capture_output=True, timeout=5
    )

    # Register service
    print("  Registering system service...")
    spd = os.path.join(install_dir, "spd.py")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    result = subprocess.run(
        [pythonw, spd, "install"],
        capture_output=True, text=True, timeout=30,
        cwd=install_dir
    )
    if result.returncode != 0:
        if result.stderr:
            print(f"  [WARN] {result.stderr.strip()}")

    # Write a copy of the license to the install directory
    license_path = os.path.join(install_dir, "LICENSE.txt")
    with open(license_path, "w", encoding="utf-8") as f:
        f.write("SpeedCore — System-Level Multi-Threaded Download Accelerator\n")
        f.write("Copyright (c) 2026 Xiangye\n\n")
        f.write(LICENSE_ZH.strip() + "\n\n")
        f.write(LICENSE_EN.strip() + "\n")

    print()
    print("=" * 50)
    print("  Install Complete / 安装完成！")
    print()
    print("  Commands / 常用命令:")
    print(f'    cd "{install_dir}"')
    print("    python spd.py status       Status / 查看状态")
    print("    python spd.py proxy on      Enable acceleration / 启用加速")
    print("    python spd.py speedtest     Speed test / 测速")
    print()
    print(f"  License saved to: {license_path}")
    print("=" * 50)


def uninstall():
    print()
    print("=" * 50)
    print("  SpeedCore Uninstall / 卸载")
    print("=" * 50)
    print()

    subprocess.run(
        'schtasks /end /tn "SpeedCore"', shell=True,
        capture_output=True, timeout=5
    )
    subprocess.run(
        'schtasks /delete /tn "SpeedCore" /f', shell=True,
        capture_output=True, timeout=5
    )
    for name in ["aria2c.exe", "python.exe", "pythonw.exe"]:
        subprocess.run(
            f'taskkill /f /im "{name}" 2>nul', shell=True,
            capture_output=True, timeout=5
        )

    inst = get_install_dir()
    if os.path.exists(inst):
        choice = input(f"  Delete installation directory? / 删除安装目录? {inst} [y/N] ")
        if choice.lower() == "y":
            shutil.rmtree(inst, ignore_errors=True)
            print(f"  [OK] Deleted / 已删除: {inst}")

    print()
    print("  SpeedCore has been uninstalled. / SpeedCore 已卸载。")


def main():
    if "/uninstall" in sys.argv or "--uninstall" in sys.argv:
        if not is_admin():
            print("Uninstall requires administrator privileges.")
            print("卸载需要管理员权限。右键 -> 以管理员身份运行。")
            safe_input("Press Enter / 按回车退出...")
            sys.exit(1)
        uninstall()
        safe_input("Press Enter / 按回车退出...")
        return

    silent = "/S" in sys.argv or "/silent" in sys.argv
    install_dir = get_install_dir()

    if not is_admin():
        if silent:
            print("Administrator privileges required. / 需要管理员权限。")
            sys.exit(1)
        print("=" * 50)
        print("  SpeedCore requires administrator privileges.")
        print("  SpeedCore 需要管理员权限。")
        print()
        print("  Right-click SpeedCore-Setup.exe -> Run as administrator")
        print("  右键 SpeedCore-Setup.exe -> 以管理员身份运行")
        print("=" * 50)
        safe_input("Press Enter / 按回车退出...")
        sys.exit(1)

    # Show license (skip if silent)
    if not silent:
        agreed = show_license()
        if not agreed:
            print()
            print("=" * 50)
            print("  You have declined the license agreement.")
            print("  您已拒绝许可协议。")
            print()
            print("  Installation cancelled. / 安装已取消。")
            print("=" * 50)
            safe_input("Press Enter / 按回车退出...")
            sys.exit(0)
    else:
        print("[SILENT] License accepted automatically. / 静默安装：自动接受许可。")

    install(install_dir)

    if not silent:
        safe_input("Press Enter / 按回车退出...")


if __name__ == "__main__":
    main()
