"""SpeedCore Setup — 一键安装到系统，注册开机自启服务。

用法:
  SpeedCore-Setup.exe              # 安装到默认目录
  SpeedCore-Setup.exe /S /D=DIR    # 静默安装到指定目录
  SpeedCore-Setup.exe /uninstall   # 卸载
"""

import os, sys, shutil, subprocess, ctypes

DEFAULT_DIR = r"C:\Program Files\SpeedCore"
FILES = ["aria2c.exe", "aria2.conf"]


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


def install(install_dir: str, silent: bool = False):
    print("=" * 50)
    print("  SpeedCore — 多线程下载加速器 安装程序")
    print("=" * 50)
    print()
    print(f"  安装目录: {install_dir}")
    print()

    # Create directory
    os.makedirs(install_dir, exist_ok=True)

    src = get_source_dir()

    # Copy files
    for fn in FILES:
        sp = os.path.join(src, fn)
        dp = os.path.join(install_dir, fn)
        if os.path.exists(sp):
            shutil.copy2(sp, dp)
            size_kb = os.path.getsize(dp) // 1024
            print(f"  [OK] {fn} ({size_kb} KB)")
        else:
            print(f"  [MISS] {fn} not found in package")

    # Copy Python scripts if running from source
    py_files = ["__init__.py", "spd.py", "svc.py", "proxy.py", "tcpopt.py"]
    for fn in py_files:
        sp = os.path.join(src, fn)
        dp = os.path.join(install_dir, fn)
        if os.path.exists(sp):
            shutil.copy2(sp, dp)

    print()

    # Stop existing service if any
    print("  停止旧服务...")
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

    # Run spd install to register service
    print("  注册系统服务...")
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

    print()
    print("=" * 50)
    print("  安装完成！")
    print()
    print("  常用命令:")
    print(f"    cd \"{install_dir}\"")
    print("    python spd.py status     查看状态")
    print("    python spd.py proxy on   启用代理加速")
    print("    python spd.py speedtest  测速")
    print("=" * 50)


def uninstall():
    print("卸载 SpeedCore...")
    subprocess.run(
        'schtasks /end /tn "SpeedCore"', shell=True,
        capture_output=True, timeout=5
    )
    subprocess.run(
        'schtasks /delete /tn "SpeedCore" /f', shell=True,
        capture_output=True, timeout=5
    )
    # Kill processes
    for name in ["aria2c.exe", "python.exe", "pythonw.exe"]:
        subprocess.run(
            f'taskkill /f /im "{name}" 2>nul', shell=True,
            capture_output=True, timeout=5
        )

    # Optionally remove install directory
    inst = get_install_dir()
    if os.path.exists(inst):
        choice = input(f"  删除安装目录 {inst}? [y/N] ")
        if choice.lower() == "y":
            shutil.rmtree(inst, ignore_errors=True)
            print(f"  [OK] 已删除 {inst}")

    print("SpeedCore 已卸载。")


def main():
    if "/uninstall" in sys.argv or "--uninstall" in sys.argv:
        if not is_admin():
            print("卸载需要管理员权限。")
            print('  右键 → 以管理员身份运行')
            input("按任意键退出...")
            sys.exit(1)
        uninstall()
        input("按任意键退出...")
        return

    silent = "/S" in sys.argv or "/silent" in sys.argv
    install_dir = get_install_dir()

    if not is_admin():
        if silent:
            print("需要管理员权限。请以管理员身份运行。")
            sys.exit(1)
        print("SpeedCore 安装需要管理员权限。")
        print('  右键 SpeedCore-Setup.exe → 以管理员身份运行')
        input("按任意键退出...")
        sys.exit(1)

    install(install_dir, silent)

    if not silent:
        input("按任意键退出...")


if __name__ == "__main__":
    main()
