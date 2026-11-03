"""Offline test guard: permit local fixtures, reject external network sockets."""
import ipaddress
import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_resolve = socket.getaddrinfo


def _local(host):
    if isinstance(host, bytes):
        host = host.decode('ascii', 'strict')
    if host is None or host in {'localhost', ''}:
        return True
    try:
        return ipaddress.ip_address(str(host).split('%', 1)[0]).is_loopback
    except ValueError:
        return False


def _guarded_connect(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6} and not _local(address[0]):
        raise AssertionError('External network is forbidden in offline validation')
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6} and not _local(address[0]):
        raise AssertionError('External network is forbidden in offline validation')
    return _connect_ex(self, address)


def _guarded_resolve(host, *args, **kwargs):
    if not _local(host):
        raise AssertionError('External DNS is forbidden in offline validation')
    return _resolve(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_resolve
