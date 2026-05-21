# SpeedCore

**Windows 系统级多线程下载加速器 · System-Level Multi-Threaded Download Accelerator**

---

**作者 / Author:** Xiangye

**许可 / License:** 非商业使用 · Non-Commercial · [LICENSE](LICENSE)

**技术 / Tech:**
[Python](https://www.python.org/) ·
[aria2](https://github.com/aria2/aria2) (LGPL v2.1) ·
[WinDivert](https://www.reqrypt.org/windivert.html) (LGPL v3) ·
[WinINET](https://learn.microsoft.com/en-us/windows/win32/wininet/) ·
[CTCP](https://learn.microsoft.com/en-us/windows/win32/winsock/) ·
[PyInstaller](https://pyinstaller.org/)

---

## 目录 / TOC

- [概述](#概述)
- [架构](#架构)
- [工作模式](#工作模式)
- [核心机制](#核心机制)
- [安装](#安装)
- [命令参考](#命令参考)
- [实测性能](#实测性能)
- [项目结构](#项目结构)
- [许可](#许可)

---

## 概述

SpeedCore 是 Windows 平台上的透明下载加速系统。安装后自动拦截浏览器及**所有程序的 TCP 流量**，下载类请求交由 aria2 多线程引擎（16 连接 × 128 分片）并发加速，带宽利用率从单线程的 20~40% 提升至 90%+。

SpeedCore is a transparent download acceleration system for Windows. Once installed, TCP traffic from browsers and **all programs** is automatically intercepted, with downloads parallelized through aria2 (16 connections × 128 splits), pushing bandwidth utilization from 20-40% to 90%+.

### 它解决什么 / What It Solves

浏览器和大多数工具默认单线程下载大文件，TCP 单流窗口受限于远端限速和拥塞控制，宽带利用率极低。SpeedCore 在系统层拦截、多线程分片并发、配合 TCP/IP 栈参数调优，将带宽利用推到物理极限。

Browsers and most tools download single-threaded by default, limited by TCP single-stream window and server-side throttling. SpeedCore intercepts at the system level, parallelizes via multi-threaded chunking, and combines TCP/IP stack tuning to push bandwidth near the physical limit.

### 三层覆盖 / Three Coverage Layers

| 层 | 技术 | 范围 |
|----|------|------|
| 1 | WinINET 系统代理 + PAC | 浏览器 (Chrome, Edge, Firefox) |
| 2 | WinHTTP + 环境变量 | 系统服务, npm, pip, curl |
| 3 | **WinDivert 内核包拦截 + 双向 NAT** | **所有 TCP 程序（游戏、IM、网盘等）** |

> 注意：部分服务对非会员实施服务器端限速，此类限速与服务端有关，非 SpeedCore 能突破。
> Note: Some services enforce server-side rate limits for non-premium accounts — unrelated to SpeedCore.

---

## 架构

```
                          SpeedCore 数据流
═══════════════════════════════════════════════════════════

  浏览器 / 系统程序 / 所有 TCP 程序
           │
    ┌──────┴──────────────────┐
    │ 系统代理 (PAC/WinINET)  │
    │ 环境变量 (HTTP_PROXY)   │
    │ WinDivert 内核拦截 (TUN)│
    └──────┬──────────────────┘
           ▼
  ┌────────────────────────────────┐
  │  SpeedProxy                    │
  │  :19999 (显式代理)             │
  │  :19998 (TUN 透明代理)          │
  │                                │
  │  下载检测 — 三层判断:           │
  │  · URL 扩展名 (60+)            │
  │  · Content-Type 白名单         │
  │  · 文件 > 10MB                 │
  │                                │
  │  下载 ──→ aria2c RPC (:16800) │
  │  网页 ──→ 上游代理 或 直连     │
  └────────────────────────────────┘
           │                │
    下载流量              普通流量
           ▼                ▼
  ┌──────────────┐  ┌──────────────┐
  │  aria2c      │  │  上游代理     │
  │  16 连接     │  │  自动检测     │
  │  128 分片    │  │  直连容错     │
  │  32 并发     │  └──────────────┘
  │  128M 缓存   │
  └──────────────┘
           │
  流式回传 (256KB buffer)
           ▼
  用户 — 透明加速完成
```

---

## 工作模式

### 模式 A：系统代理加速（浏览器）

```
spd proxy on    → WinINET 代理 → :19999 → 下载走 aria2c
```
覆盖浏览器和读取系统代理的程序。

### 模式 B：TUN 全局透明代理（全电脑）

```
spd tun on      → WinDivert 内核拦截所有 TCP → :19998 → 双向 NAT
```
覆盖**所有程序**，包括不读系统代理的客户端（游戏平台、IM、网盘等）。

### 模式 C：命令行直连下载

```
spd get <URL>   → 直接 aria2c 多线程，不走代理层
```
适合单文件手动下载。

---

## 核心机制

### 1. 下载拦截

代理监听 `127.0.0.1:19999`，每个请求经三层判断：
- **URL 扩展名**: `.zip` `.exe` `.mp4` `.iso` `.pdf` 等 60+ 种
- **Content-Type**: `application/octet-stream`, `video/*`, `audio/*`
- **文件大小**: > 10MB 自动加速

非下载请求透明转发，不影响正常上网。

### 2. aria2 多线程引擎

```
URL → HEAD 获取文件大小 → 动态分片 (128 片, 最小 1MB)
→ 16 条 TCP 连接 → 各自请求不同 byte range
→ 并发下载实时拼接 → 流式回传 (256KB buffer)
```

配置: `max-connection-per-server=16` `split=128` `max-concurrent-downloads=32` `disk-cache=128M` `file-allocation=falloc`

### 3. 上游代理自动检测与容错

```
缓存文件 → WinINET 注册表 → WinHTTP → 环境变量 → TCP 可达性验证
不可达 → aria2 自动走直连，防止下载失败
```

### 4. TUN 全局透明代理

WinDivert 在内核网络层拦截所有 TCP 包，双向 NAT 重定向：

```
程序 TCP SYN (目标 internet:port)
         │
         ▼ WinDivert 网络层
┌─────────────────────┐
│ 双向 NAT             │
│ 出站: dst → :19998  │
│ 回程: src 还原      │
└─────────────────────┘
         │
         ▼
  :19998 透明代理 → NAT 表还原目标 → 直连/上游 → 下载走 aria2c
```

- 内核级包拦截，无需虚拟网卡
- 覆盖所有 TCP 端口，不限于 80/443
- HTTP Host 头 / TLS SNI 双路径还原目标

### 5. TCP/IP 协议栈优化

`spd optimize` 一键应用：
- CTCP 拥塞控制（高 BDP 最优）
- TCP 窗口缩放 + 时间戳
- 禁用 Nagle 算法
- RSS 多队列 + Chimney 硬件卸载
- 禁用 Windows 网络节流（默认限制 10% 带宽）
- 网卡极限参数（关节能、9K Jumbo、512 缓冲）

提供 `spd tcpopt --revert` 回退命令。

### 6. 看门狗自愈 / Watchdog

独立守护进程，每 30 秒检测：
- aria2c RPC :16800
- 代理 :19999
- TUN :19998

异常自动重启，连续 10 次失败记录 CRITICAL 日志。

### 7. 系统服务

`schtasks` + `NT AUTHORITY\SYSTEM` · 开机 30 秒自启 · Session 0 隔离 · 无窗口静默 · 最高权限 · 进程被杀自动恢复。

---

## 安装

### 一键安装

```
下载 SpeedCore-Setup.exe → 右键以管理员身份运行 → 确认许可协议 → 完成
```

静默安装: `SpeedCore-Setup.exe /S /D=C:\Tools\SpeedCore`

卸载: `SpeedCore-Setup.exe /uninstall`

### 手动安装

```powershell
git clone <repo> && cd speedcore
python spd.py install   # 注册开机自启服务
python spd.py start     # 立即启动
```

---

## 命令参考

```powershell
# 服务管理
spd install            # 安装系统服务 (开机自启, SYSTEM 静默)
spd remove             # 卸载
spd start / stop       # 启动 / 停止
spd status             # 状态 + 活跃任务

# 代理
spd proxy detect       # 检测当前代理环境
spd proxy on / off     # 启用 / 恢复加速代理
spd proxy status       # 查看代理状态

# TUN 全局透明代理 (需管理员)
spd tun on             # 启动全电脑 TCP 拦截
spd tun off            # 停止
spd tun status         # 状态

# 优化 (需管理员)
spd optimize           # 一键 TCP/IP + 网卡优化
spd tcpopt --revert    # 恢复默认
spd tcpopt --status    # 查看优化状态

# 工具
spd get <URL>          # CLI 多线程下载
spd speedtest          # 测速
```

---

## 实测性能

测试环境：Windows 10 · Realtek PCIe GbE · 联通 600 Mbps

| 场景 | 均值 | 峰值 | 利用率 |
|------|------|------|--------|
| 浏览器原生下载 | 18.7 MB/s | 22.4 MB/s | 30% |
| SpeedCore 单文件 | 44.0 MB/s | 47.5 MB/s | 63% |
| SpeedCore 4 并发 | 65.2 MB/s | 78.4 MB/s | 90%+ |
| TCP 优化 + 4 并发 | 71.8 MB/s | 78.4 MB/s | 95%+ |

> 78.4 MB/s ≈ 627 Mbps，接近线路签约上限。

---

## 项目结构

```
speedcore/
  spd.py              CLI 入口，子命令路由
  svc.py              Windows 服务管理（schtasks / 注册表）
  proxy.py            HTTP 透明代理 (:19999 显式 + :19998 TUN)
  tun.py              TUN 全局拦截（WinDivert 双向 NAT）
  divert.py           WinDivert ctypes 封装
  tcpopt.py           TCP/IP 协议栈 + 网卡注册表优化
  watchdog.py         看门狗守护（30s 检测 + 自动重启）
  _bootstrap.py       SYSTEM 静默启动器（自动生成）
  installer.py        一键安装器 → SpeedCore-Setup.exe
  aria2c.exe          aria2 下载引擎 (LGPL v2.1)
  aria2.conf          引擎配置 (16T × 128S)
  WinDivert.dll       内核包拦截库 (LGPL v3)
  WinDivert64.sys     内核驱动 (LGPL v3)
```

---

## 许可

SpeedCore: **非商业使用许可** — 商业使用需作者书面授权。详见 [LICENSE](LICENSE)。

本软件集成以下第三方组件，按其自身许可分发：
- [aria2](https://github.com/aria2/aria2) — GNU LGPL v2.1
- [WinDivert](https://www.reqrypt.org/windivert.html) — GNU LGPL v3

---

**© 2026 Xiangye**
