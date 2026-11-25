"""Offline checks only; public network access is denied by an audit hook."""
from pathlib import Path
import ipaddress
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor_deps"))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

def network_guard(event, args):
    if event in {"socket.connect", "socket.sendto"}:
        address = args[-1]
        host = address[0] if isinstance(address, tuple) else ""
    elif event == "socket.getaddrinfo":
        host = args[0]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host)).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("离线检查禁止外部网络访问")

if __name__ == "__main__":
    sys.addaudithook(network_guard)
    print("原版定时优化 · 离线检查（不会登录或向平台报名）")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("通过离线检查不代表真实抢中率或 Windows 定时精度。")
    raise SystemExit(0 if result.wasSuccessful() else 1)
