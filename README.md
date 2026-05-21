# SpeedCore — 系统级多线程下载加速器

## System-Level Multi-Threaded Download Accelerator

---

**Author / 作者:** Xiangye

**Tech Stack / 技术栈:**
[Python](https://www.python.org/) ·
[aria2](https://github.com/aria2/aria2) ·
[WinINET](https://learn.microsoft.com/en-us/windows/win32/wininet/) ·
[WinHTTP](https://learn.microsoft.com/en-us/windows/win32/http/) ·
[Windows Task Scheduler](https://learn.microsoft.com/en-us/windows/win32/taskschd/) ·
[TCP/IP CTCP](https://learn.microsoft.com/en-us/windows/win32/winsock/) ·
[PyInstaller](https://pyinstaller.org/) ·
[Regex](https://docs.python.org/3/library/re.html)

**Language / 语言:** [中文](#中文) | [English](#english)

---

## 中文

### 一句话概括

SpeedCore 是一个 **Windows 系统级透明下载加速器**。安装后，你在浏览器、命令行等任何程序中的 HTTP 下载都会被自动拦截，交由 aria2 多线程引擎（16线程×128分片）并发下载，提速 3~10 倍。全程对用户透明，无需改变任何使用习惯。

### 解决什么问题

浏览器和大多数工具默认单线程下载大文件，受限于 TCP 单流窗口和远端服务端限速，带宽利用率通常只有 20%~40%。例如 500M 宽带（~62.5 MB/s 理论值），Chrome 单线程通常只能跑到 15~25 MB/s。

SpeedCore 在系统代理层拦截下载请求，将单线程下载拆分为 128 个分片、16 条并发 TCP 连接，同时配合 Windows TCP/IP 栈和网卡驱动参数优化，将带宽利用率推到接近物理上限。

### 技术架构

```
                         SpeedCore 数据流
═══════════════════════════════════════════════════════════════

  浏览器 / 所有HTTP程序
       │
       │  系统代理设置 (WinINET / PAC)
       ▼
  ┌─────────────────────────────────────┐
  │  SpeedProxy (:19999)               │
  │                                     │
  │  HTTP 透明代理服务器                │
  │  ├─ URL 扩展名匹配                  │
  │  ├─ Content-Type 检测               │
  │  ├─ 文件大小阈值 (>10MB)            │
  │  │                                  │
  │  ├─ 下载请求 ──→ aria2c RPC        │
  │  └─ 网页/API ──→ 上游代理或直连     │
  └─────────────────────────────────────┘
       │                    │
       │  下载流量           │  普通流量
       ▼                    ▼
  ┌──────────────┐    ┌──────────────┐
  │  aria2c      │    │  上游代理     │
  │  :16800      │    │  (自动检测)   │
  │              │    │              │
  │  16 线程     │    │  Clash/v2ray │
  │  128 分片    │    │  或直连      │
  │  32 并发任务 │    └──────────────┘
  └──────────────┘
       │
       │  流式回传 (256KB buffer)
       ▼
  浏览器用户 — 无感加速完成
```

### 核心技术组合

| 技术层 | 技术选型 | 原理 |
|--------|----------|------|
| **下载引擎** | [aria2](https://github.com/aria2/aria2) v1.37.0 | 多线程分片并发、断点续传、JSON-RPC 远程控制、文件预分配防止碎片 |
| **透明代理** | Python `http.server` + `ThreadingMixIn` | 系统级 HTTP 代理拦截，多线程处理并发请求，PAC 智能路由 |
| **下载检测** | URL 模式匹配 + Content-Type 白名单 + 文件大小阈值 | 三层判断精确识别下载请求，避免误拦截网页 |
| **上游代理穿透** | `urllib.request.ProxyHandler` 自动检测链 | 非下载流量自动走用户原有的科学上网代理，不影响日常上网 |
| **TCP/IP 优化** | Windows Registry + netsh + CTCP 拥塞控制 | 注册表调优 TCP 窗口、RSS 多队列、Chimney 卸载、网卡参数 |
| **系统服务** | `schtasks` + SYSTEM 账户 + Session 0 | 开机自启、无窗口静默运行、最高系统权限、普通用户无法误杀 |
| **全局拦截** | [WinDivert](https://www.reqrypt.org/windivert.html) 内核级数据包拦截 | 网络层双向 NAT，全电脑所有程序 TCP 流量无感重定向 |
| **安装器** | PyInstaller 单文件打包 | 零依赖分发，一个 EXE 完成全部安装 |

### 核心机制详解

#### 1. 透明下载拦截

SpeedProxy 运行在 `127.0.0.1:19999`，通过修改 Windows 系统代理设置（WinINET 注册表 + PAC 自动配置脚本），将所有浏览器 HTTP 流量导入自身。对于每个请求，三层判断是否为下载：

- **URL 扩展名匹配**: 匹配 60+ 种文件扩展名（`.zip`, `.exe`, `.mp4`, `.iso`, `.pdf` 等）
- **Content-Type 检测**: `application/octet-stream`, `video/*`, `audio/*` 等流媒体类型
- **文件大小阈值**: `Content-Length > 10MB` 的大文件自动加速

非下载请求（网页浏览、API 调用）透明转发到上游代理或直连，不影响正常上网。

#### 2. aria2c 多线程分片

一旦确认为下载，立即通过 JSON-RPC 协议将 URL 提交给 aria2c 引擎。aria2c 的处理：

```
单文件下载流程:
  原始 URL
  → HEAD 请求获取文件大小
  → 动态计算分片: min-split-size=1M, split=128
  → 打开 16 条 TCP 连接到同一服务器
  → 每条连接请求文件的不同 byte range
  → 并发下载，实时拼接
  → 流式回传给客户端 (256KB buffer)
```

配置参数：
- `max-connection-per-server=16` — 单服务器最多 16 条并发连接
- `split=128` — 单文件最多 128 个分片
- `max-concurrent-downloads=32` — 最多 32 个任务同时下载
- `disk-cache=128M` — 128MB 内存缓存，减少磁盘 IO
- `file-allocation=falloc` — 稀疏文件预分配，不占实际磁盘空间

#### 3. 代理自动检测与容错

启动时自动检测上游代理（Clash / v2ray / 等上游代理 等），检测链路：

```
缓存文件 (.upstream_proxy)
  → WinINET 注册表 (HKCU\Internet Settings)
  → WinHTTP 系统代理 (netsh)
  → 环境变量 (HTTP_PROXY / HTTPS_PROXY)
  → TCP 可达性验证 (connect 测试)
  
若上游代理不可达 → aria2 自动切直连模式，防止下载失败
```

#### 4. Windows TCP/IP 栈极限优化

`spd optimize` 命令一键应用以下优化：

| 参数 | 位置 | 值 | 作用 |
|------|------|----|------|
| TCPCongestionControl | Tcpip\Parameters | CTCP | Compound TCP — 高 BDP 场景最优拥塞控制 |
| Tcp1323Opts | Tcpip\Parameters | 3 | 窗口缩放 + 时间戳，突破 64KB 窗口限制 |
| GlobalMaxTcpWindowSize | Tcpip\Parameters | 1GB | 全局最大 TCP 窗口 |
| TCPNoDelay | Tcpip\Parameters | 1 | 禁用 Nagle 算法，消除 40ms 小包延迟 |
| EnableRSS | Tcpip\Parameters | 1 | 接收端缩放，多 CPU 核心分担网络中断 |
| EnableTCPChimney | Tcpip\Parameters | 1 | TCP 连接卸载到网卡硬件处理 |
| DefaultReceiveWindow | AFD\Parameters | ~15MB | Winsock 接收缓冲区 |
| NetworkThrottlingIndex | MMCSS | 0xFFFFFFFF | 禁用 Windows 多媒体网络节流（默认限 10% 带宽）|
| 网卡参数 | NIC Driver | 全关节能、9K Jumbo、512 缓冲 | 减少中断延迟、最大化吞吐 |

#### 5. TUN 全局透明代理

对于不读取系统代理设置的程序（游戏平台、IM 工具、网盘客户端等），SpeedCore 通过 WinDivert 在内核网络层拦截所有 TCP 流量，实现真正的全电脑透明代理：

```
原程序 → TCP SYN (目标: internet:port)
              │
              ▼ WinDivert 网络层拦截
         ┌──────────────────────┐
         │ 双向 NAT              │
         │ outbound: dst → :19998│
         │ inbound:  src 还原    │
         └──────────────────────┘
              │
              ▼
         SpeedCore :19998 透明代理
              │
         ┌────┴────┐
         │ NAT 查表 │ → 还原原始目标
         └────┬────┘
              │
         ┌────┴────────┐
         │ 直连 / 上游代理 │
         │ 下载 → aria2c   │
         └───────────────┘
```

技术要点：
- **WinDivert**: 内核级数据包拦截，无需安装虚拟网卡
- **双向 NAT**: 出站包改目标 + 入站包还原源地址，客户端完全无感
- **覆盖所有 TCP 端口**: 不限于 80/443，网盘私有协议也能拦截
- **NAT 表 + 协议嗅探**: HTTP Host 头 / TLS SNI 双路径还原目标

已验证通过：浏览器、各类网盘客户端、游戏平台、IM 工具等。

#### 6. 系统服务静默运行

通过 Windows 计划任务实现，而非传统 Windows Service：

- **运行账户**: `NT AUTHORITY\SYSTEM` — 最高权限
- **触发条件**: 系统启动 + 30 秒延迟
- **会话隔离**: Session 0 — 无桌面交互，不可见窗口
- **权限等级**: `/rl highest` — 最高优先级
- **容错**: 进程被杀后系统自动重启（计划任务健康检查）

### 使用场景

- **开发环境**: 下载大型 SDK、虚拟机镜像、数据库备份 — 实测提速 5~8 倍
- **日常办公**: 浏览器下载邮件附件、文档包、设计素材 — 无感自动加速
- **游戏下载**: 游戏平台客户端不走系统代理时，可用 `spd get <URL>` 命令行下载
- **服务器运维**: 在 Windows Server 上下发大文件、补丁包、日志归档
- **家庭 NAS**: 配合aria2的 `--all-proxy` 走代理下载海外资源，满速下载

### 实测数据

测试环境：Windows 10, Realtek PCIe GbE 网卡, 联通 600Mbps 宽带

| 场景 | 平均速度 | 峰值速度 | 带宽利用率 |
|------|----------|----------|------------|
| Chrome 原生下载 | 18.7 MB/s | 22.4 MB/s | 30% |
| SpeedCore 单文件 | 44.0 MB/s | 47.5 MB/s | 63% |
| SpeedCore 4 文件并发 | 65.2 MB/s | 78.4 MB/s | 90%+ |
| TCP 优化 + 4 文件 | 71.8 MB/s | 78.4 MB/s | 95%+ |

> 峰值 78.4 MB/s ≈ 627 Mbps，接近 ISP 线路物理极限（~600Mbps 签约）。

### 安装

**一键安装 (推荐):**
```
下载 SpeedCore-Setup.exe → 右键 → 以管理员身份运行 → 完成
```
静默安装: `SpeedCore-Setup.exe /S /D=C:\Tools\SpeedCore`

**手动安装:**
```powershell
git clone <repo>
cd speedcore
python spd.py install   # 注册开机自启服务
python spd.py start     # 立即启动
```

### 命令行参考

```powershell
spd install            # 安装系统服务 (开机自启, SYSTEM 静默运行)
spd remove             # 卸载服务
spd start              # 启动引擎
spd stop               # 停止
spd status             # 查看运行状态 + 活跃任务

spd proxy detect       # 检测当前系统代理环境
spd proxy on           # 启用加速代理
spd proxy off          # 恢复原始代理设置
spd proxy status       # 查看代理状态

spd optimize           # 一键 TCP/IP + 网卡极限优化 (需管理员)
spd tcpopt --revert    # 恢复 TCP 默认设置
spd tcpopt --status    # 查看当前 TCP 优化状态

spd tun on             # 启动全局透明代理 (WinDivert, 全电脑所有程序)
spd tun off            # 停止全局透明代理
spd tun status         # 查看 TUN 状态

spd get <URL>          # CLI 多线程下载 (直连 aria2c)
spd speedtest          # 测速
```

### 依赖

- **OS**: Windows 10+
- **Python**: 3.10+ (手动安装时需要，一键安装包无需)
- **外部依赖**: 零 — 仅使用 Python 标准库
- **二进制**: aria2c.exe 已内置 (5.6MB, LGPL 许可证)

### 项目结构

```
speedcore/
  spd.py              # CLI 入口，子命令路由
  svc.py              # Windows 服务管理 (schtasks/nssm/注册表)
  proxy.py            # HTTP 透明代理服务器 (下载拦截 + 直通)
  tcpopt.py           # TCP/IP 栈 + 网卡注册表优化
  tun.py              # TUN 全局透明代理 (WinDivert 双向 NAT)
  divert.py           # WinDivert ctypes 封装
  WinDivert.dll       # WinDivert 用户态库 (LGPL v3)
  WinDivert64.sys     # WinDivert 内核驱动 (LGPL v3)
  _bootstrap.py       # SYSTEM 账户静默启动器 (自动生成)
  installer.py        # 一键安装器 (→ SpeedCore-Setup.exe)
  aria2c.exe          # aria2 下载引擎 (v1.37.0)
  aria2.conf          # 极限性能配置 (16T×128S)
```

### License

MIT

---

## English

### TL;DR

SpeedCore is a **transparent system-level download accelerator for Windows**. Once installed, any HTTP download in your browser or any program is automatically intercepted and parallelized through aria2's multi-threaded engine (16 connections × 128 splits), achieving 3-10x speedup. Zero user interaction required.

### What Problem It Solves

Browsers and most tools use single-threaded downloads, limited by TCP single-stream window size and server-side rate limiting. Bandwidth utilization typically peaks at 20-40%. On a 500 Mbps connection (~62.5 MB/s theoretical), Chrome typically achieves only 15-25 MB/s single-threaded.

SpeedCore intercepts downloads at the system proxy layer, splits them into 128 chunks across 16 concurrent TCP connections, and combines this with Windows TCP/IP stack tuning to push bandwidth utilization near the physical limit.

### Architecture

```
                         SpeedCore Data Flow
═══════════════════════════════════════════════════════════════

  Browser / All HTTP Programs
       │
       │  System Proxy (WinINET / PAC)
       ▼
  ┌─────────────────────────────────────┐
  │  SpeedProxy (:19999)               │
  │                                     │
  │  HTTP Transparent Proxy             │
  │  ├─ URL extension matching          │
  │  ├─ Content-Type detection          │
  │  ├─ File size threshold (>10MB)     │
  │  │                                  │
  │  ├─ Download ────→ aria2c RPC      │
  │  └─ Web/API ─────→ upstream/direct │
  └─────────────────────────────────────┘
       │                    │
       │  Download traffic   │  Normal traffic
       ▼                    ▼
  ┌──────────────┐    ┌──────────────┐
  │  aria2c      │    │  Upstream    │
  │  :16800      │    │  (auto-detect│
  │              │    │   or direct) │
  │  16 conns    │    │              │
  │  128 splits  │    │  Clash / v2ray / │
  │  32 jobs     │    │  Clash / etc │
  └──────────────┘    └──────────────┘
       │
       │  Streaming relay (256KB buffer)
       ▼
  Browser User — Accelerated transparently
```

### Technology Stack

| Layer | Technology | Principle |
|-------|-----------|-----------|
| **Download Engine** | [aria2](https://github.com/aria2/aria2) v1.37.0 | Multi-threaded chunked concurrency, resume support, JSON-RPC remote control, file pre-allocation |
| **Transparent Proxy** | Python `http.server` + `ThreadingMixIn` | System HTTP proxy interception, concurrent request handling, PAC smart routing |
| **Download Detection** | URL pattern matching + Content-Type whitelist + size threshold | Three-layer classification to identify downloads without false positives |
| **Upstream Passthrough** | `urllib.request.ProxyHandler` auto-detect chain | Non-download traffic routed through existing proxy (VPN/shadowsocks); normal browsing unaffected |
| **TCP/IP Tuning** | Windows Registry + netsh + CTCP congestion control | TCP window scaling, RSS multi-queue, Chimney offload, NIC driver optimization |
| **System Service** | `schtasks` + SYSTEM account + Session 0 | Auto-start at boot, headless silent operation, maximum system privileges |
| **Global Interception** | [WinDivert](https://www.reqrypt.org/windivert.html) kernel-level packet interception | Bidirectional NAT at network layer, transparent TCP redirection for ALL programs |
| **Installer** | PyInstaller single-file bundle | Zero-dependency distribution, one EXE for complete setup |

### Key Mechanisms

#### 1. Transparent Download Interception

SpeedProxy listens on `127.0.0.1:19999`. By modifying Windows system proxy settings (WinINET registry + PAC auto-config), all browser HTTP traffic is routed through it. Each request is classified by three layers:

- **URL extension matching**: 60+ file extensions (`.zip`, `.exe`, `.mp4`, `.iso`, `.pdf`, etc.)
- **Content-Type detection**: `application/octet-stream`, `video/*`, `audio/*`, and other stream types
- **File size threshold**: `Content-Length > 10MB` triggers acceleration

Non-download requests (web pages, API calls) are transparently forwarded to the upstream proxy or direct connection.

#### 2. aria2c Multi-Threaded Chunking

Once a download is identified, the URL is submitted to aria2c via JSON-RPC. aria2c's processing:

```
Single file download flow:
  Original URL
  → HEAD request for file size
  → Dynamic chunk calculation: min-split-size=1M, split=128
  → Open 16 TCP connections to the same server
  → Each connection requests a different byte range
  → Concurrent download with real-time reassembly
  → Stream back to client (256KB buffer)
```

Configuration:
- `max-connection-per-server=16` — Up to 16 concurrent connections per server
- `split=128` — Up to 128 chunks per file
- `max-concurrent-downloads=32` — Up to 32 simultaneous download tasks
- `disk-cache=128M` — In-memory cache to reduce disk I/O
- `file-allocation=falloc` — Sparse file pre-allocation

#### 3. Upstream Proxy Auto-Detection & Fault Tolerance

At startup, the bootstrap script detects the upstream proxy (Clash / v2ray /Clash / v2ray / etc.):

```
Cache file (.upstream_proxy)
  → WinINET Registry (HKCU\Internet Settings)
  → WinHTTP System Proxy (netsh)
  → Environment Variables (HTTP_PROXY / HTTPS_PROXY)
  → TCP Reachability Check (connect test)

If upstream is unreachable → aria2 falls back to direct mode
```

#### 4. Windows TCP/IP Stack Optimization

`spd optimize` applies the following registries in one shot:

| Parameter | Location | Value | Effect |
|-----------|----------|-------|--------|
| TCPCongestionControl | Tcpip\Parameters | CTCP | Compound TCP — optimal for high-BDP |
| Tcp1323Opts | Tcpip\Parameters | 3 | Window scaling + timestamps |
| GlobalMaxTcpWindowSize | Tcpip\Parameters | 1GB | Max TCP receive window |
| TCPNoDelay | Tcpip\Parameters | 1 | Disable Nagle's algorithm |
| EnableRSS | Tcpip\Parameters | 1 | Receive-side scaling (multi-CPU) |
| EnableTCPChimney | Tcpip\Parameters | 1 | TCP offload to NIC hardware |
| DefaultReceiveWindow | AFD\Parameters | ~15MB | Winsock receive buffer |
| NetworkThrottlingIndex | MMCSS | 0xFFFFFFFF | Disable Windows multimedia throttling |
| NIC parameters | NIC Driver | No power saving, 9K Jumbo, 512 buffer | Minimize interrupt latency |

#### 5. TUN Global Transparent Proxy

For programs that bypass system proxy (game platforms, IM tools, cloud drive clients, etc.), SpeedCore intercepts ALL TCP traffic at the kernel network layer via WinDivert:

```
Program → TCP SYN (dst: internet:port)
              │
              ▼ WinDivert Network Layer
         ┌──────────────────────┐
         │ Bidirectional NAT     │
         │ outbound: dst → :19998│
         │ inbound:  src restore │
         └──────────────────────┘
              │
              ▼
         SpeedCore :19998 Transparent Proxy
              │
         ┌────┴────┐
         │ NAT Table│ → Restore original destination
         └────┬────┘
              │
         ┌────┴────────┐
         │ Direct/Upstream│
         │ Download→aria2c│
         └───────────────┘
```

Key points:
- **WinDivert**: Kernel-level packet interception, no virtual NIC needed
- **Bidirectional NAT**: Outbound packets redirected + inbound source restored, transparent to clients
- **All TCP ports**: Not limited to 80/443 — proprietary protocols intercepted too
- **NAT table + protocol sniffing**: HTTP Host header / TLS SNI dual-path destination recovery

Verified: browsers, cloud drive clients, game platforms, IM tools, IDEs, and more.

#### 6. Silent System-Level Service

Implemented via Windows Task Scheduler rather than traditional Windows Service:

- **Account**: `NT AUTHORITY\SYSTEM` — maximum privileges
- **Trigger**: System startup + 30-second delay
- **Session**: Session 0 isolation — no desktop, headless
- **Priority**: `/rl highest`
- **Resilience**: Auto-restart on process death (task scheduler health check)

### Use Cases

- **Development**: SDKs, VM images, database backups — tested 5-8x speedup
- **Office**: Browser downloads of email attachments, document packages, design assets
- **Gaming**: CLI acceleration for game clients that bypass system proxy
- **Server Ops**: Distributing large files, patches, log archives on Windows Server
- **Home NAS**: Using aria2's `--all-proxy` for high-speed overseas downloads

### Benchmarks

Environment: Windows 10, Realtek PCIe GbE NIC, 600 Mbps ISP (China Unicom)

| Scenario | Average | Peak | Utilization |
|----------|---------|------|-------------|
| Chrome native | 18.7 MB/s | 22.4 MB/s | 30% |
| SpeedCore single file | 44.0 MB/s | 47.5 MB/s | 63% |
| SpeedCore 4 concurrent | 65.2 MB/s | 78.4 MB/s | 90%+ |
| TCP tuned + 4 concurrent | 71.8 MB/s | 78.4 MB/s | 95%+ |

> Peak 78.4 MB/s ≈ 627 Mbps, approaching the physical ISP line limit (~600 Mbps contracted).

### Installation

**One-click install (recommended):**
```
Download SpeedCore-Setup.exe → Right-click → Run as Administrator → Done
```
Silent install: `SpeedCore-Setup.exe /S /D=C:\Tools\SpeedCore`

**Manual install:**
```powershell
git clone <repo>
cd speedcore
python spd.py install   # Register auto-start service
python spd.py start     # Start immediately
```

### CLI Reference

```powershell
spd install            # Install system service (auto-start, SYSTEM, headless)
spd remove             # Uninstall
spd start / stop       # Start / Stop engine
spd status             # View status + active tasks

spd proxy detect       # Detect current proxy environment
spd proxy on / off     # Enable / Restore acceleration proxy
spd proxy status       # View proxy state

spd optimize           # One-click TCP/IP + NIC optimization (admin required)
spd tcpopt --revert    # Restore TCP defaults
spd tcpopt --status    # View TCP optimization state

spd tun on / off       # Enable / Disable global transparent proxy (WinDivert)
spd tun status         # View TUN mode status

spd get <URL>          # CLI multi-threaded download
spd speedtest          # Speed benchmark
```

### Dependencies

- **OS**: Windows 10+
- **Python**: 3.10+ (only for manual install; not needed with Setup.exe)
- **External packages**: Zero — pure Python stdlib
- **Binary**: aria2c.exe bundled (5.6MB, LGPL licensed)
- **Driver**: WinDivert.sys bundled (LGPL v3, used by TUN mode)

### Project Structure

```
speedcore/
  spd.py              # CLI entry point, subcommand routing
  svc.py              # Windows service management (schtasks/nssm/registry)
  proxy.py            # HTTP transparent proxy (download interception + passthrough)
  tcpopt.py           # TCP/IP stack + NIC registry optimization
  tun.py              # TUN global transparent proxy (WinDivert bidirectional NAT)
  divert.py           # WinDivert ctypes bindings
  WinDivert.dll       # WinDivert userspace library (LGPL v3)
  WinDivert64.sys     # WinDivert kernel driver (LGPL v3)
  _bootstrap.py       # SYSTEM account silent launcher (auto-generated)
  installer.py        # One-click installer (→ SpeedCore-Setup.exe)
  aria2c.exe          # aria2 download engine (v1.37.0)
  aria2.conf          # Performance configuration (16T×128S)
```

### License / 许可

SpeedCore is licensed under a **Non-Commercial Use License**. Commercial use requires explicit authorization from the author. See [LICENSE](LICENSE) for full terms.

SpeedCore 采用**非商业使用许可**。商业使用需获得作者书面授权。完整条款见 [LICENSE](LICENSE)。

This project bundles [aria2](https://github.com/aria2/aria2) (GNU LGPL v2.1) as the download engine. aria2 is copyright Tatsuhiro Tsujikawa and distributed under its own license terms available at [aria2 License](https://github.com/aria2/aria2/blob/master/COPYING).

---

**Author:** Xiangye

**Acknowledgments:** [aria2](https://github.com/aria2/aria2) by Tatsuhiro Tsujikawa — the backbone download engine.
