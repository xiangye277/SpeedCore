# SpeedCore

**系统级多线程下载加速器 · System-Level Download Accelerator**

---

**Author / 作者:** Xiangye

**License / 许可:** Non-Commercial · 非商业使用 · [LICENSE](LICENSE)

**Tech / 技术:**
[Python](https://www.python.org/) ·
[aria2](https://github.com/aria2/aria2) (LGPL) ·
[WinDivert](https://www.reqrypt.org/windivert.html) (LGPL) ·
[WinINET](https://learn.microsoft.com/en-us/windows/win32/wininet/) ·
[CTCP](https://learn.microsoft.com/en-us/windows/win32/winsock/) ·
[PyInstaller](https://pyinstaller.org/)

---

## 目录 / TOC

- [概述 / Overview](#概述--overview)
- [架构 / Architecture](#架构--architecture)
- [核心机制 / How It Works](#核心机制--how-it-works)
- [安装 / Installation](#安装--installation)
- [使用 / Usage](#使用--usage)
- [实测 / Benchmarks](#实测--benchmarks)
- [许可 / License](#许可--license)

---

## 概述 / Overview

SpeedCore 是 Windows 平台上的透明下载加速器。安装后自动拦截浏览器及所有程序的 HTTP 下载，通过 aria2 多线程引擎（16 连接 × 128 分片）并发下载，带宽利用率从单线程的 20~40% 提升至 90%+。对用户完全透明，无需改变使用习惯。

SpeedCore is a transparent download accelerator for Windows. Once installed, HTTP downloads from browsers and all programs are automatically intercepted and parallelized through aria2 (16 connections × 128 splits), pushing bandwidth utilization from 20-40% to 90%+. Fully transparent — no workflow changes needed.

### 三层覆盖 / Three Coverage Layers

| 层 | 技术 | 覆盖 |
|----|------|------|
| 浏览器 / Browsers | WinINET 系统代理 + PAC | Chrome, Edge, Firefox 等 |
| 系统服务 / System | WinHTTP 代理 + 环境变量 | Windows Update, npm, pip, curl |
| **全程序 / All Programs** | **WinDivert 内核包拦截 + 双向 NAT** | **游戏平台、IM、网盘等所有 TCP 程序** |

> 部分网盘服务对非会员实施服务器端限速，与 SpeedCore 无关。
> Some cloud drives enforce server-side rate limits for non-premium accounts — unrelated to SpeedCore.

---

## 架构 / Architecture

```
                          SpeedCore 数据流 / Data Flow
═══════════════════════════════════════════════════════════════════════

  浏览器 / 系统程序 / 所有 TCP 程序
  Browser / System / All TCP Programs
           │
    ┌──────┴──────────────────────────┐
    │  系统代理 (WinINET)    TUN 拦截   │
    │  环境变量 (HTTP_PROXY)  WinDivert │
    └──────┬──────────────────────────┘
           ▼
  ┌────────────────────────────────────┐
  │  SpeedProxy (:19999 · :19998)     │
  │                                    │
  │  · 下载检测 Download Detection    │
  │    ├ URL 扩展名 (60+ 种)          │
  │    ├ Content-Type 白名单           │
  │    └ 文件 > 10MB                   │
  │                                    │
  │  · 下载 ──→ aria2c RPC (:16800)   │
  │  · 网页 ──→ 上游代理 或 直连       │
  └────────────────────────────────────┘
           │                    │
    下载流量                    普通流量
    Download                   Normal
           ▼                    ▼
  ┌──────────────────┐  ┌──────────────┐
  │  aria2c :16800   │  │ 上游代理      │
  │  16 线程 128 分片 │  │ (自动检测     │
  │  32 并发任务      │  │  直连容错)    │
  │  disk-cache 128M │  │ Upstream      │
  └──────────────────┘  │ Auto-detect   │
           │            │ Fallback      │
           ▼            └──────────────┘
  流式回传 256KB buffer
  Streaming relay
           ▼
  用户 — 无感加速完成 / Done
```

---

## 核心机制 / How It Works

### 1. 下载拦截 / Download Interception

代理监听 `127.0.0.1:19999`。三层判断：

- **URL 扩展名**: `.zip` `.exe` `.mp4` `.iso` `.pdf` 等 60+ 种
- **Content-Type**: `application/octet-stream`, `video/*`, `audio/*`
- **文件大小**: `Content-Length > 10MB`

非下载请求透明转发，不影响正常上网。
Non-download requests forwarded transparently.

### 2. aria2c 多线程引擎 / Multi-Threaded Engine

```
URL → HEAD 获取大小 → 动态分片 (128 片 × 1MB min)
→ 16 条 TCP 并发连接 → 各自请求不同 byte range
→ 实时拼接 → 流式回传 (256KB buffer)
```

配置 / Config: `max-connection-per-server=16` `split=128` `max-concurrent-downloads=32` `disk-cache=128M` `file-allocation=falloc`

### 3. 上游代理自动检测 / Upstream Auto-Detection

```
缓存文件 → WinINET 注册表 → WinHTTP → 环境变量 → TCP 可达性验证
Cache     Registry         netsh     ENV         Reachability check

不可达 → aria2 自动切直连，防止下载失败
Unreachable → auto fallback to direct
```

### 4. TUN 全局透明代理 / Global Transparent Proxy

对于不读系统代理的程序（游戏平台、IM、网盘客户端），WinDivert 在内核网络层拦截所有 TCP 包，双向 NAT 重定向：

```
程序 TCP SYN (目标 internet:port)
         │
         ▼ WinDivert 网络层
┌─────────────────────┐
│ 双向 NAT             │
│ 出站: dst → :19998  │
│ 回程: 还原 src      │
└─────────────────────┘
         │
         ▼
  :19998 透明代理 → NAT 表还原目标 → 直连/上游 → 下载走 aria2c
```

- 无需虚拟网卡 · No virtual NIC required
- 覆盖所有 TCP 端口 · Not limited to 80/443
- HTTP Host 头 / TLS SNI 双路径还原目标

### 5. TCP/IP 优化 / Stack Tuning

`spd optimize` 一键应用：CTCP 拥塞控制 · 窗口缩放 · 禁用 Nagle · RSS 多队列 · Chimney 卸载 · 禁用网络节流 · 网卡极限参数。提供回退命令。

One-click: CTCP congestion control · Window scaling · Nagle off · RSS · Chimney offload · Network throttling disabled · NIC tuning. Revert available.

### 6. 系统服务 / System Service

`schtasks` + `NT AUTHORITY\SYSTEM` · 开机 30 秒自启 · Session 0 隔离 · 无窗口 · 最高权限 · 进程被杀自动恢复。

`schtasks` + `SYSTEM` · 30s delay autostart · Session 0 · headless · max privileges · auto-recovery.

---

## 安装 / Installation

**一键安装 / One-Click:**
```
SpeedCore-Setup.exe → 右键以管理员身份运行 → 确认许可 → 完成
SpeedCore-Setup.exe → Run as Administrator → Accept license → Done
```
静默 / Silent: `SpeedCore-Setup.exe /S /D=C:\Tools\SpeedCore`

**手动 / Manual:**
```powershell
git clone <repo> && cd speedcore
python spd.py install && python spd.py start
```

**卸载 / Uninstall:**
```
SpeedCore-Setup.exe /uninstall
```

---

## 使用 / Usage

```powershell
# 服务 / Service
spd install       # 安装开机自启 / install auto-start service
spd start         # 启动引擎 / start engine
spd stop          # 停止 / stop
spd status        # 状态 + 活跃任务 / status + active jobs

# 代理 / Proxy
spd proxy detect  # 检测环境 / detect environment
spd proxy on      # 启用加速 / enable
spd proxy off     # 恢复 / restore

# 全局 / TUN (admin)
spd tun on        # 全电脑透明代理 / all-program interception
spd tun off       # 停止 / stop
spd tun status    # 状态 / status

# 优化 / Optimize (admin)
spd optimize      # TCP/IP + 网卡优化 / stack + NIC tuning

# 工具 / Tools
spd get <URL>     # CLI 多线程下载 / multi-threaded download
spd speedtest     # 测速 / speed test
```

---

## 实测 / Benchmarks

Windows 10 · Realtek PCIe GbE · 联通 600 Mbps

| 场景 / Scenario | 均值 / Avg | 峰值 / Peak | 利用率 / Util |
|---|---|---|---|
| 浏览器原生 / Native | 18.7 MB/s | 22.4 MB/s | 30% |
| SpeedCore 单文件 / 1 file | 44.0 MB/s | 47.5 MB/s | 63% |
| SpeedCore 4 并发 / 4 concurrent | 65.2 MB/s | 78.4 MB/s | 90%+ |
| + TCP 优化 / + tuning | 71.8 MB/s | 78.4 MB/s | 95%+ |

> 78.4 MB/s ≈ 627 Mbps，接近线路物理极限 / approaching ISP physical limit.

---

## 项目结构 / Structure

```
speedcore/
  spd.py              CLI 入口 / entry point
  svc.py              服务管理 / service manager
  proxy.py            透明代理 / transparent proxy (:19999 + :19998)
  tun.py              TUN 全局拦截 / global interception (WinDivert NAT)
  divert.py           WinDivert ctypes 封装 / bindings
  tcpopt.py           TCP/IP 优化 / stack tuning
  _bootstrap.py       SYSTEM 启动器 / launcher
  installer.py        安装器 / installer → SpeedCore-Setup.exe
  aria2c.exe          下载引擎 / engine (LGPL v2.1)
  aria2.conf          引擎配置 / engine config (16T×128S)
  WinDivert.dll       包拦截库 / packet interception (LGPL v3)
  WinDivert64.sys     内核驱动 / kernel driver (LGPL v3)
```

---

## 许可 / License

SpeedCore: **非商业使用许可** — 商业使用需作者书面授权。详见 [LICENSE](LICENSE)。

SpeedCore: **Non-Commercial License** — commercial use requires explicit written authorization. See [LICENSE](LICENSE).

本软件集成 [aria2](https://github.com/aria2/aria2) (GNU LGPL v2.1) 和 [WinDivert](https://www.reqrypt.org/windivert.html) (GNU LGPL v3)，二者均按其自身许可条款分发。

This project bundles [aria2](https://github.com/aria2/aria2) (LGPL v2.1) and [WinDivert](https://www.reqrypt.org/windivert.html) (LGPL v3), both distributed under their own license terms.

---

**Author:** Xiangye
