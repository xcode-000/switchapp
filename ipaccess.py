"""Allowed client addresses: single IPs, subnets (CIDR) and ranges (a-b)."""
import ipaddress
import re


def parse(rule):
    """-> (low, high) ip_address pair, or raises ValueError. Accepts 10.1.1.5 / 10.1.0.0/16 /
    10.1.1.10-10.1.1.50 / 10.1.1.10-50"""
    rule = (rule or "").strip().replace(" ", "")
    if not rule:
        raise ValueError("empty")
    if "/" in rule:
        net = ipaddress.ip_network(rule, strict=False)
        return net[0], net[-1]
    if "-" in rule:
        a, b = rule.split("-", 1)
        lo = ipaddress.ip_address(a)
        if re.fullmatch(r"\d{1,3}", b) and lo.version == 4:  # 10.1.1.10-50
            b = ".".join(a.split(".")[:3] + [b])
        hi = ipaddress.ip_address(b)
        if lo.version != hi.version or hi < lo:
            raise ValueError("range")
        return lo, hi
    ip = ipaddress.ip_address(rule)
    return ip, ip


def normalise(rule):
    lo, hi = parse(rule)
    rule = rule.strip().replace(" ", "")
    if "/" in rule:
        return str(ipaddress.ip_network(rule, strict=False))
    return str(lo) if lo == hi else f"{lo}-{hi}"


def compile_rules(rules):
    out = []
    for r in rules:
        try:
            out.append(parse(r))
        except ValueError:
            continue
    return out


def allowed(addr, compiled):
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True  # the server itself can always connect (recovery)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(lo.version == ip.version and lo <= ip <= hi for lo, hi in compiled)
