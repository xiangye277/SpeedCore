"""WinDivert ctypes bindings — minimal subset needed for TCP redirection.

WinDivert (LGPL v3): https://www.reqrypt.org/windivert.html
"""

import ctypes
from ctypes import c_uint8, c_uint16, c_uint32, c_uint64, c_int, c_int64, c_void_p, c_char_p, c_bool
import os
import sys

# WinDivert constants
WINDIVERT_DIRECTION_OUTBOUND = 0
WINDIVERT_LAYER_NETWORK = 0
WINDIVERT_FLAG_SNIFF = 1
WINDIVERT_FLAG_DROP = 2

# Load DLL
ROOT = os.path.dirname(os.path.abspath(__file__))
DLL_PATH = os.path.join(ROOT, "WinDivert.dll")


class WINDIVERT_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("Timestamp", ctypes.c_longlong),
        ("Layer", c_uint32),
        ("Event", c_uint32),
        ("Sniffed", c_uint32),
        ("Outbound", c_uint32),
        ("Loopback", c_uint32),
        ("Impostor", c_uint32),
        ("IPv6", c_uint32),
        ("IPChecksum", c_uint32),
        ("TCPChecksum", c_uint32),
        ("UDPChecksum", c_uint32),
        ("Data", c_void_p),
    ]


class WINDIVERT_IPHDR(ctypes.Structure):
    _fields_ = [
        ("HdrLength_Version", c_uint8),
        ("TOS", c_uint8),
        ("Length", c_uint16),
        ("Id", c_uint16),
        ("FragOff0", c_uint16),
        ("TTL", c_uint8),
        ("Protocol", c_uint8),
        ("Checksum", c_uint16),
        ("SrcAddr", c_uint32),
        ("DstAddr", c_uint32),
    ]


class WINDIVERT_TCPHDR(ctypes.Structure):
    _fields_ = [
        ("SrcPort", c_uint16),
        ("DstPort", c_uint16),
        ("SeqNum", c_uint32),
        ("AckNum", c_uint32),
        ("HdrLength_Reserved_Flags", c_uint16),
        ("Window", c_uint16),
        ("Checksum", c_uint16),
        ("UrgPtr", c_uint16),
    ]


_dll = None


def _load():
    global _dll
    if _dll is not None:
        return _dll
    _dll = ctypes.WinDLL(DLL_PATH)
    _dll.WinDivertOpen.argtypes = [c_char_p, c_int, c_int64, c_uint64]
    _dll.WinDivertOpen.restype = c_void_p
    _dll.WinDivertRecv.argtypes = [c_void_p, c_void_p, c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(WINDIVERT_ADDRESS)]
    _dll.WinDivertRecv.restype = c_bool
    _dll.WinDivertSend.argtypes = [c_void_p, c_void_p, c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(WINDIVERT_ADDRESS)]
    _dll.WinDivertSend.restype = c_bool
    _dll.WinDivertClose.argtypes = [c_void_p]
    _dll.WinDivertClose.restype = c_bool
    _dll.WinDivertHelperCalcChecksums.argtypes = [c_void_p, c_uint32, c_void_p, c_uint64]
    _dll.WinDivertHelperCalcChecksums.restype = c_uint32
    return _dll


def open_handle(filter_str: str, layer=WINDIVERT_LAYER_NETWORK, priority=0, flags=0):
    dll = _load()
    h = dll.WinDivertOpen(filter_str.encode(), layer, priority, flags)
    if h is None or h == c_void_p(-1).value:
        raise OSError("WinDivertOpen failed — driver installed? Run as admin.")
    return h


def recv(handle, bufsize=65536):
    dll = _load()
    buf = (ctypes.c_ubyte * bufsize)()
    addr = WINDIVERT_ADDRESS()
    recv_len = c_uint32(0)
    ok = dll.WinDivertRecv(handle, buf, c_uint32(bufsize), ctypes.byref(recv_len), ctypes.byref(addr))
    if not ok:
        return None, None
    return bytes(buf[:recv_len.value]), addr


def send(handle, packet: bytes, addr):
    dll = _load()
    buf = (ctypes.c_ubyte * len(packet))(*packet)
    send_len = c_uint32(0)
    ok = dll.WinDivertSend(handle, buf, c_uint32(len(packet)), ctypes.byref(send_len), ctypes.byref(addr))
    return ok


def calc_checksums(packet: bytes, flags: int = 0):
    """flags: 1=IP, 2=TCP, 4=UDP, 0=all"""
    dll = _load()
    buf = (ctypes.c_ubyte * len(packet))(*packet)
    dll.WinDivertHelperCalcChecksums(buf, c_uint32(len(packet)), None, c_uint64(flags))


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
    struct.pack_into("!I", data, 16, new_ip_int)
    struct.pack_into("!H", data, payload_off - 20 + 2, socket.htons(new_port))
    calc_checksums(bytes(data), 3)
    return bytes(data)
