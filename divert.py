"""WinDivert ctypes bindings — minimal subset needed for TCP redirection.

WinDivert (LGPL v3): https://www.reqrypt.org/windivert.html
"""

import ctypes
from ctypes import wintypes
import os
import sys

# Constants
WINDIVERT_DIRECTION_OUTBOUND = 0
WINDIVERT_DIRECTION_INBOUND = 1
WINDIVERT_LAYER_NETWORK = 0
WINDIVERT_FLAG_SNIFF = 1
WINDIVERT_FLAG_DROP = 2
WINDIVERT_PARAM_QUEUE_LEN = 0
WINDIVERT_PARAM_QUEUE_TIME = 1

# Load DLL
ROOT = os.path.dirname(os.path.abspath(__file__))
DLL_PATH = os.path.join(ROOT, "WinDivert.dll")


class WINDIVERT_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("Timestamp", wintypes.LARGE_INTEGER),
        ("Layer", wintypes.UINT),
        ("Event", wintypes.UINT),
        ("Sniffed", wintypes.UINT),
        ("Outbound", wintypes.UINT),
        ("Loopback", wintypes.UINT),
        ("Impostor", wintypes.UINT),
        ("IPv6", wintypes.UINT),
        ("IPChecksum", wintypes.UINT),
        ("TCPChecksum", wintypes.UINT),
        ("UDPChecksum", wintypes.UINT),
        ("Data", ctypes.c_void_p),
    ]


class WINDIVERT_IPHDR(ctypes.Structure):
    _fields_ = [
        ("HdrLength_Version", wintypes.UINT8),
        ("TOS", wintypes.UINT8),
        ("Length", wintypes.UINT16),
        ("Id", wintypes.UINT16),
        ("FragOff0", wintypes.UINT16),
        ("TTL", wintypes.UINT8),
        ("Protocol", wintypes.UINT8),
        ("Checksum", wintypes.UINT16),
        ("SrcAddr", wintypes.UINT32),
        ("DstAddr", wintypes.UINT32),
    ]


class WINDIVERT_TCPHDR(ctypes.Structure):
    _fields_ = [
        ("SrcPort", wintypes.UINT16),
        ("DstPort", wintypes.UINT16),
        ("SeqNum", wintypes.UINT32),
        ("AckNum", wintypes.UINT32),
        ("HdrLength_Reserved_Flags", wintypes.UINT16),
        ("Window", wintypes.UINT16),
        ("Checksum", wintypes.UINT16),
        ("UrgPtr", wintypes.UINT16),
    ]


class WINDIVERT_DATA_NETWORK(ctypes.Structure):
    _fields_ = [
        ("IfIdx", wintypes.UINT32),
        ("SubIfIdx", wintypes.UINT32),
    ]


_dll = None


def _load():
    global _dll
    if _dll is not None:
        return _dll
    _dll = ctypes.WinDLL(DLL_PATH)
    # WinDivertOpen
    _dll.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int64, wintypes.UINT64]
    _dll.WinDivertOpen.restype = ctypes.c_void_p
    # WinDivertRecv
    _dll.WinDivertRecv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(WINDIVERT_ADDRESS)]
    _dll.WinDivertRecv.restype = wintypes.BOOL
    # WinDivertSend
    _dll.WinDivertSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(WINDIVERT_ADDRESS)]
    _dll.WinDivertSend.restype = wintypes.BOOL
    # WinDivertClose
    _dll.WinDivertClose.argtypes = [ctypes.c_void_p]
    _dll.WinDivertClose.restype = wintypes.BOOL
    # WinDivertHelperCalcChecksums
    _dll.WinDivertHelperCalcChecksums.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.UINT64]
    _dll.WinDivertHelperCalcChecksums.restype = wintypes.UINT
    return _dll


def open_handle(filter_str: str, layer=WINDIVERT_LAYER_NETWORK, priority=0, flags=0):
    dll = _load()
    h = dll.WinDivertOpen(filter_str.encode(), layer, priority, flags)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise OSError("WinDivertOpen failed — driver installed? Run as admin.")
    return h


def recv(handle, bufsize=65536):
    dll = _load()
    buf = (ctypes.c_ubyte * bufsize)()
    addr = WINDIVERT_ADDRESS()
    recv_len = wintypes.UINT(0)
    ok = dll.WinDivertRecv(handle, buf, bufsize, ctypes.byref(recv_len), ctypes.byref(addr))
    if not ok:
        return None, None
    return bytes(buf[:recv_len.value]), addr


def send(handle, packet: bytes, addr):
    dll = _load()
    buf = (ctypes.c_ubyte * len(packet))(*packet)
    send_len = wintypes.UINT(0)
    ok = dll.WinDivertSend(handle, buf, len(packet), ctypes.byref(send_len), ctypes.byref(addr))
    return ok


def calc_checksums(packet: bytes, flags: int = 0):
    """flags: 1=IP, 2=TCP, 4=UDP, 0=all"""
    dll = _load()
    buf = (ctypes.c_ubyte * len(packet))(*packet)
    dll.WinDivertHelperCalcChecksums(buf, len(packet), None, flags)


def close(handle):
    dll = _load()
    dll.WinDivertClose(handle)


def parse_ipv4_tcp(packet: bytes):
    """Parse IPv4 + TCP headers from raw packet. Returns (iphdr, tcphdr, payload_offset)."""
    if len(packet) < 20:
        return None, None, 0
    ip = WINDIVERT_IPHDR.from_buffer_copy(packet[:20])
    ip_hdr_len = (ip.HdrLength_Version & 0x0F) * 4
    if ip.Protocol != 6 or len(packet) < ip_hdr_len + 20:
        return ip, None, ip_hdr_len
    tcp = WINDIVERT_TCPHDR.from_buffer_copy(packet[ip_hdr_len:ip_hdr_len + 20])
    tcp_hdr_len = ((tcp.HdrLength_Reserved_Flags >> 12) & 0xF) * 4
    return ip, tcp, ip_hdr_len + tcp_hdr_len


def modify_dst(packet: bytes, new_ip: str, new_port: int) -> bytes:
    """Modify IPv4 destination IP and TCP destination port. Returns modified packet."""
    import struct, socket
    ip, tcp, payload_off = parse_ipv4_tcp(packet)
    if ip is None or tcp is None:
        return packet
    data = bytearray(packet)
    new_ip_int = struct.unpack("!I", socket.inet_aton(new_ip))[0]
    # IP dst
    struct.pack_into("!I", data, 16, new_ip_int)
    # TCP dst port
    struct.pack_into("!H", data, payload_off - (tcp.HdrLength_Reserved_Flags and 0xF000) + 2, new_port)
    # Recalc checksums
    calc_checksums(bytes(data), 3)
    return bytes(data)
