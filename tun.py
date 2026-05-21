"""SpeedCore TUN — 全局透明代理，全电脑所有程序 TCP 80/443 自动走 SpeedCore。

原理: WinDivert 双向 NAT
  出站: 拦截 SYN → 记录原始目标 → 修改 dst 到本地代理
  入站: 拦截 proxy 返回包 → 修改 src 还原为原始目标 → 客户端无感

用法:
  python tun.py start    启动 TUN 模式 (需管理员)
  python tun.py stop     停止
  python tun.py status   查看状态
"""

import os, sys, time, json, socket, struct, threading, ctypes, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(ROOT, "tun.pid")
MAP_FILE = os.path.join(ROOT, "tun_map.json")
PROXY_PORT = 19999  # Explicit proxy port (for PAC/WinINET)
TUN_PORT = 19998    # Transparent redirect port (for TUN-redirected traffic)

_map_lock = threading.Lock()
_nat_table: dict[str, dict] = {}  # key="src_ip:src_port" -> {original_dst_ip, original_dst_port, ...}


def _load_nat():
    global _nat_table
    try:
        with open(MAP_FILE, "r") as f:
            _nat_table = json.load(f)
    except Exception:
        _nat_table = {}


def _save_nat():
    try:
        with open(MAP_FILE, "w") as f:
            json.dump(_nat_table, f)
    except Exception:
        pass


def _key(ip: str, port: int) -> str:
    return f"{ip}:{port}"


def _ntohl_ip(i: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", i))


def is_tun_running() -> bool:
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


def stop_tun():
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        subprocess.run(f"taskkill /f /pid {pid}", shell=True,
                       capture_output=True, timeout=5)
        print(f"TUN (PID {pid}) terminated.")
    except FileNotFoundError:
        pass
    try:
        os.unlink(PID_FILE)
    except Exception:
        pass
    print("TUN mode stopped.")


def run_tun():
    """启动双向 NAT 重定向循环 — 阻塞运行"""
    from divert import (
        open_handle, recv, send, close, parse_ipv4_tcp,
        WINDIVERT_LAYER_NETWORK, WINDIVERT_FLAG_SNIFF, WINDIVERT_FLAG_DROP,
        calc_checksums
    )

    # Load persisted NAT table
    _load_nat()

    # Filter: capture both directions
    # Outbound: packets to TCP 80/443 (but NOT to our proxy)
    # Inbound: packets FROM our proxy
    filter_str = (
        "(tcp.DstPort == 80 or tcp.DstPort == 443 or tcp.DstPort == 8080 or tcp.DstPort == 8443) "
        "and tcp.DstPort != 19999 and tcp.DstPort != 19998 "
        "and tcp.DstPort != 7892 "  # Don't intercept upstream proxy traffic
        "or tcp.SrcPort == 19998 "  # Proxy return packets
    )

    print(f"TUN starting — redirecting TCP 80/443 → 127.0.0.1:{TUN_PORT}")
    print("All programs now route through SpeedCore.")
    print("Run 'python spd.py tun stop' to disable.")

    handle = open_handle(filter_str, WINDIVERT_LAYER_NETWORK, 0, WINDIVERT_FLAG_SNIFF)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Clean stale entries every 60s
    last_cleanup = time.time()

    try:
        while True:
            packet, addr = recv(handle, 65536)
            if packet is None:
                time.sleep(0.001)
                continue

            ip, tcp, payload_off = parse_ipv4_tcp(packet)
            if ip is None or tcp is None:
                send(handle, packet, addr)
                continue

            src_ip = _ntohl_ip(ip.SrcAddr)
            dst_ip = _ntohl_ip(ip.DstAddr)
            src_port = socket.ntohs(tcp.SrcPort)
            dst_port = socket.ntohs(tcp.DstPort)
            flags = tcp.HdrLength_Reserved_Flags & 0x3F
            is_syn = (flags & 0x02) != 0
            is_fin = (flags & 0x01) != 0
            is_rst = (flags & 0x04) != 0

            # ─── Direction: outbound (to real server) or inbound (from proxy) ───

            if dst_port == TUN_PORT:
                # INBOUND: client → proxy (already redirected)
                # Forward as-is; proxy handles it
                send(handle, packet, addr)
                continue

            if src_port == TUN_PORT:
                # OUTBOUND from proxy → client
                # Look up original destination and rewrite source
                k = _key(dst_ip, dst_port)  # dst is the client
                entry = _nat_table.get(k)
                if entry:
                    data = bytearray(packet)
                    orig_dst_ip_int = struct.unpack("!I", socket.inet_aton(entry["dst_ip"]))[0]
                    orig_dst_port = int(entry["dst_port"])
                    # Rewrite source IP/port to original destination
                    struct.pack_into("!I", data, 12, orig_dst_ip_int)   # IP src
                    struct.pack_into("!H", data, payload_off - 20 + 0, socket.htons(orig_dst_port))  # TCP src
                    # Recalculate checksums
                    calc_checksums(bytes(data), 3)  # IP + TCP
                    send(handle, bytes(data), addr)

                    # Clean on FIN/RST
                    if is_fin or is_rst:
                        _nat_table.pop(k, None)
                else:
                    send(handle, packet, addr)
                continue

            # ─── OUTBOUND: client → real server (first seen or established) ───
            if is_syn:
                # New connection — record and redirect
                k = _key(src_ip, src_port)
                _nat_table[k] = {
                    "dst_ip": dst_ip,
                    "dst_port": dst_port,
                    "time": time.time(),
                }

                # Modify destination → 127.0.0.1:TUN_PORT
                data = bytearray(packet)
                proxy_ip_int = struct.unpack("!I", socket.inet_aton("127.0.0.1"))[0]
                struct.pack_into("!I", data, 16, proxy_ip_int)  # IP dst
                struct.pack_into("!H", data, payload_off - 20 + 2, socket.htons(TUN_PORT))  # TCP dst
                calc_checksums(bytes(data), 3)  # IP + TCP
                send(handle, bytes(data), addr)
            else:
                # Established flow — continue redirecting
                k = _key(src_ip, src_port)
                if k in _nat_table:
                    data = bytearray(packet)
                    proxy_ip_int = struct.unpack("!I", socket.inet_aton("127.0.0.1"))[0]
                    struct.pack_into("!I", data, 16, proxy_ip_int)
                    struct.pack_into("!H", data, payload_off - 20 + 2, socket.htons(TUN_PORT))
                    calc_checksums(bytes(data), 3)
                    send(handle, bytes(data), addr)

                    if is_fin or is_rst:
                        _nat_table.pop(k, None)
                else:
                    send(handle, packet, addr)
                    if is_fin or is_rst:
                        _nat_table.pop(k, None)

            # Periodic cleanup
            if time.time() - last_cleanup > 60:
                stale = time.time() - 300  # 5 min
                dead = [k for k, v in _nat_table.items() if v.get("time", 0) < stale]
                for k in dead:
                    _nat_table.pop(k, None)
                if dead:
                    _save_nat()
                last_cleanup = time.time()

    except KeyboardInterrupt:
        pass
    finally:
        close(handle)
        try:
            os.unlink(PID_FILE)
        except Exception:
            pass
        print("TUN exited.")
