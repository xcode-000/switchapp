"""Minimal read-only SNMP v1 / v2c client (GET, GETNEXT, GETBULK walks) - pure Python, no dependency.

Only what SwitchDoc needs to show live information about a switch: nothing is written to the device
and nothing retrieved is stored.
"""
import os
import socket
import struct
import time

# ---------------------------------------------------------------- BER encoding
INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
IPADDRESS, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB = 0x80, 0x81, 0x82
GET, GETNEXT, RESPONSE, GETBULK = 0xA0, 0xA1, 0xA2, 0xA5
ERRORS = {1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess",
          16: "authorizationError"}


class SnmpError(Exception):
    pass


class SnmpTimeout(SnmpError):
    pass


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, value):
    return bytes([tag]) + _len(len(value)) + value


def enc_int(n, tag=INTEGER):
    length = max(1, (n.bit_length() + 8) // 8)  # room for the sign bit
    return tlv(tag, n.to_bytes(length, "big", signed=True))


def enc_oid(oid):
    parts = [int(x) for x in str(oid).strip(".").split(".")]
    if len(parts) < 2:
        raise ValueError("OID too short")
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return tlv(OID, bytes(out))


def dec_tlv(data, pos):
    """-> (tag, value bytes, next position)"""
    if pos + 2 > len(data):
        raise SnmpError("truncated packet")
    tag = data[pos]
    n = data[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[pos:pos + k], "big")
        pos += k
    if pos + n > len(data):
        raise SnmpError("truncated packet")
    return tag, data[pos:pos + n], pos + n


def dec_oid(v):
    if not v:
        return ""
    parts = [v[0] // 40, v[0] % 40] if v[0] < 80 else [2, v[0] - 80]
    n = 0
    for b in v[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def dec_value(tag, v):
    if tag == INTEGER:
        return int.from_bytes(v, "big", signed=True) if v else 0
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(v, "big") if v else 0
    if tag == OCTET_STRING or tag == OPAQUE:
        return bytes(v)
    if tag == OID:
        return dec_oid(v)
    if tag == IPADDRESS:
        return ".".join(str(b) for b in v)
    if tag == NULL:
        return None
    if tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB):
        return EndOfView(tag)
    return bytes(v)


class EndOfView:
    def __init__(self, tag):
        self.tag = tag

    def __repr__(self):
        return {NO_SUCH_OBJECT: "noSuchObject", NO_SUCH_INSTANCE: "noSuchInstance"}.get(self.tag, "endOfMibView")


def build_request(version, community, pdu_type, request_id, oids, non_repeaters=0, max_repetitions=0):
    varbinds = b"".join(tlv(SEQUENCE, enc_oid(o) + tlv(NULL, b"")) for o in oids)
    if pdu_type == GETBULK:
        head = enc_int(request_id) + enc_int(non_repeaters) + enc_int(max_repetitions)
    else:
        head = enc_int(request_id) + enc_int(0) + enc_int(0)
    pdu = tlv(pdu_type, head + tlv(SEQUENCE, varbinds))
    return tlv(SEQUENCE, enc_int(version) + tlv(OCTET_STRING, community.encode()) + pdu)


def parse_response(data):
    """-> (request id, error status, error index, [(oid, tag, value), ...])"""
    tag, msg, _ = dec_tlv(data, 0)
    if tag != SEQUENCE:
        raise SnmpError("not an SNMP message")
    _t, _ver, pos = dec_tlv(msg, 0)
    _t, _community, pos = dec_tlv(msg, pos)
    ptag, pdu, _ = dec_tlv(msg, pos)
    if ptag != RESPONSE:
        raise SnmpError("unexpected PDU type 0x%02x" % ptag)
    _t, rid, p = dec_tlv(pdu, 0)
    _t, err, p = dec_tlv(pdu, p)
    _t, eidx, p = dec_tlv(pdu, p)
    _t, vbl, _ = dec_tlv(pdu, p)
    out, q = [], 0
    while q < len(vbl):
        _t, vb, q = dec_tlv(vbl, q)
        _t, oid, r = dec_tlv(vb, 0)
        vtag, val, _ = dec_tlv(vb, r)
        out.append((dec_oid(oid), vtag, dec_value(vtag, val)))
    return int.from_bytes(rid, "big", signed=True), int.from_bytes(err, "big"), int.from_bytes(eidx, "big"), out


def oid_tuple(oid):
    return tuple(int(x) for x in oid.strip(".").split(".") if x)


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, host, community="public", version="2c", port=161, timeout=2.0, retries=1, deadline=25.0):
        self.host, self.port, self.community = host, int(port), community or "public"
        self.version = 0 if str(version) == "1" else 1
        self.timeout, self.retries = timeout, retries
        self.deadline = time.monotonic() + deadline  # overall time budget for one page of data
        self.requests = 0
        try:
            info = socket.getaddrinfo(host, self.port, 0, socket.SOCK_DGRAM)[0]
        except socket.gaierror as e:
            raise SnmpError(f"cannot resolve {host}: {e}")
        self.sock = socket.socket(info[0], socket.SOCK_DGRAM)
        self.addr = info[4]

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, pdu_type, oids, max_repetitions=0):
        rid = struct.unpack(">i", os.urandom(4))[0] & 0x7FFFFFFF
        packet = build_request(self.version, self.community, pdu_type, rid, oids, 0, max_repetitions)
        for _attempt in range(self.retries + 1):
            if time.monotonic() > self.deadline:
                raise SnmpTimeout("time budget exceeded")
            self.requests += 1
            self.sock.sendto(packet, self.addr)
            end = time.monotonic() + self.timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.sock.settimeout(left)
                try:
                    data, _src = self.sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError as e:  # ICMP port unreachable etc.
                    raise SnmpError(str(e))
                try:
                    got_rid, err, eidx, vbs = parse_response(data)
                except (SnmpError, IndexError, ValueError):
                    continue
                if got_rid != rid:
                    continue  # late answer to an earlier request
                if err:
                    if err == 2 and self.version == 0:  # v1 end of the MIB
                        return []
                    raise SnmpError(f"agent error {ERRORS.get(err, err)} (index {eidx})")
                return vbs
        raise SnmpTimeout(f"no answer from {self.host}:{self.port} (check the IP address, the community "
                          "and that SNMP is enabled on the switch for this server)")

    def get(self, oids):
        """{oid: value} - missing objects are left out"""
        out = {}
        for oid, tag, val in self._request(GET, list(oids)):
            if not isinstance(val, EndOfView):
                out[oid] = val
        return out

    def walk(self, base, max_rows=5000):
        """[(oid, value)] of the sub-tree under base"""
        base_t = oid_tuple(base)
        rows, cur = [], base
        while len(rows) < max_rows:
            if self.version == 0:
                vbs = self._request(GETNEXT, [cur])
            else:
                vbs = self._request(GETBULK, [cur], max_repetitions=30)
            if not vbs:
                break
            done = False
            for oid, tag, val in vbs:
                ot = oid_tuple(oid)
                if isinstance(val, EndOfView) or ot[:len(base_t)] != base_t:
                    done = True
                    break
                if oid_tuple(cur) >= ot:  # agent not increasing: stop
                    done = True
                    break
                rows.append((oid, val))
                cur = oid
            if done:
                break
        return rows

    def table(self, columns, max_rows=5000):
        """columns {name: base oid} -> {index suffix: {name: value}}"""
        out = {}
        for name, base in columns.items():
            n = len(oid_tuple(base))
            for oid, val in self.walk(base, max_rows):
                idx = ".".join(oid.split(".")[n:])
                out.setdefault(idx, {})[name] = val
        return out


# ---------------------------------------------------------------- value helpers
def text(v):
    if isinstance(v, bytes):
        try:
            s = v.decode("utf-8")
        except UnicodeDecodeError:
            s = v.decode("latin-1")
        return s.replace("\x00", "").strip()
    return "" if v is None else str(v)


def mac(v):
    if isinstance(v, bytes) and len(v) == 6:
        return ":".join(f"{b:02x}" for b in v)
    return text(v)


def uptime(ticks):
    s = int(ticks or 0) // 100
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}"


def port_list(v):
    """Q-BRIDGE PortList bitmap -> set of bridge port numbers"""
    out = set()
    if isinstance(v, bytes):
        for i, b in enumerate(v):
            for bit in range(8):
                if b & (0x80 >> bit):
                    out.add(i * 8 + bit + 1)
    return out


# ---------------------------------------------------------------- what SwitchDoc reads
SYSTEM = {"descr": "1.3.6.1.2.1.1.1.0", "object_id": "1.3.6.1.2.1.1.2.0", "uptime": "1.3.6.1.2.1.1.3.0",
          "contact": "1.3.6.1.2.1.1.4.0", "name": "1.3.6.1.2.1.1.5.0", "location": "1.3.6.1.2.1.1.6.0"}
IF_COLUMNS = {"descr": "1.3.6.1.2.1.2.2.1.2", "type": "1.3.6.1.2.1.2.2.1.3", "mtu": "1.3.6.1.2.1.2.2.1.4",
              "speed": "1.3.6.1.2.1.2.2.1.5", "mac": "1.3.6.1.2.1.2.2.1.6", "admin": "1.3.6.1.2.1.2.2.1.7",
              "oper": "1.3.6.1.2.1.2.2.1.8", "last_change": "1.3.6.1.2.1.2.2.1.9",
              "in_errors": "1.3.6.1.2.1.2.2.1.14", "out_errors": "1.3.6.1.2.1.2.2.1.20"}
IFX_COLUMNS = {"name": "1.3.6.1.2.1.31.1.1.1.1", "in_octets": "1.3.6.1.2.1.31.1.1.1.6",
               "out_octets": "1.3.6.1.2.1.31.1.1.1.10", "high_speed": "1.3.6.1.2.1.31.1.1.1.15",
               "alias": "1.3.6.1.2.1.31.1.1.1.18"}
DOT1D_BASEPORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
DOT1Q_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"
DOT1Q_VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"
DOT1Q_VLAN_EGRESS = "1.3.6.1.2.1.17.7.1.4.3.1.2"
DOT1Q_VLAN_UNTAGGED = "1.3.6.1.2.1.17.7.1.4.3.1.4"
CISCO_VTP_VLAN_NAME = "1.3.6.1.4.1.9.9.46.1.3.1.1.4.1"
CISCO_VLAN_MEMBER = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"  # vmVlan.ifIndex
CISCO_TRUNK_STATUS = "1.3.6.1.4.1.9.9.46.1.6.1.1.14"  # vlanTrunkPortDynamicStatus: 1 trunking
LLDP_REM = {"chassis": "1.0.8802.1.1.2.1.4.1.1.5", "port_id": "1.0.8802.1.1.2.1.4.1.1.7",
            "port_desc": "1.0.8802.1.1.2.1.4.1.1.8", "sys_name": "1.0.8802.1.1.2.1.4.1.1.9"}
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
CDP_CACHE = {"device": "1.3.6.1.4.1.9.9.23.1.2.1.1.6", "port": "1.3.6.1.4.1.9.9.23.1.2.1.1.7",
             "platform": "1.3.6.1.4.1.9.9.23.1.2.1.1.8", "address": "1.3.6.1.4.1.9.9.23.1.2.1.1.4"}
FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"  # dot1qTpFdbPort: index = vlan . mac(6)
FDB_PORT_V1 = "1.3.6.1.2.1.17.4.3.1.2"  # dot1dTpFdbPort: index = mac(6)
IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}


def read_system(c):
    v = c.get(SYSTEM.values())
    inv = {oid: k for k, oid in SYSTEM.items()}
    out = {inv[o]: val for o, val in v.items() if o in inv}
    return {"descr": text(out.get("descr")), "object_id": text(out.get("object_id")),
            "uptime": uptime(out.get("uptime")), "contact": text(out.get("contact")),
            "name": text(out.get("name")), "location": text(out.get("location"))}


def read_interfaces(c):
    t = c.table(IF_COLUMNS)
    try:
        x = c.table(IFX_COLUMNS)
    except SnmpError:
        x = {}
    # VLAN of each port: Q-BRIDGE PVID (bridge port -> ifIndex) or Cisco vmVlan
    pvid = {}
    try:
        base = {idx: v for idx, v in c.table({"if": DOT1D_BASEPORT_IFINDEX}).items()}
        bp_if = {idx: r.get("if") for idx, r in base.items()}
        for idx, r in c.table({"pvid": DOT1Q_PVID}).items():
            ifi = bp_if.get(idx)
            if ifi is not None:
                pvid[str(ifi)] = r["pvid"]
    except SnmpError:
        pass
    trunk = {}
    try:
        for idx, r in c.table({"vlan": CISCO_VLAN_MEMBER}).items():
            pvid.setdefault(idx, r["vlan"])
        for idx, r in c.table({"st": CISCO_TRUNK_STATUS}).items():
            trunk[idx] = r["st"] == 1
    except SnmpError:
        pass
    rows = []
    for idx, r in t.items():
        e = x.get(idx, {})
        speed = e.get("high_speed") or ((r.get("speed") or 0) // 1_000_000)
        rows.append({"index": int(idx) if idx.isdigit() else idx, "name": text(e.get("name")) or text(r.get("descr")),
                     "descr": text(r.get("descr")), "alias": text(e.get("alias")), "type": r.get("type"),
                     "admin": IF_STATUS.get(r.get("admin"), r.get("admin")),
                     "oper": IF_STATUS.get(r.get("oper"), r.get("oper")), "speed_mbps": speed,
                     "mac": mac(r.get("mac")), "mtu": r.get("mtu"), "vlan": pvid.get(idx),
                     "trunk": trunk.get(idx), "in_octets": e.get("in_octets"), "out_octets": e.get("out_octets"),
                     "in_errors": r.get("in_errors"), "out_errors": r.get("out_errors")})
    rows.sort(key=lambda r: (r["index"] if isinstance(r["index"], int) else 10 ** 9))
    return rows


def read_interfaces_names(c):
    names = c.table({"name": IFX_COLUMNS["name"]})
    if not names:
        names = c.table({"name": IF_COLUMNS["descr"]})
    return [{"index": idx, "name": text(r.get("name"))} for idx, r in names.items()]


def read_vlans(c):
    names = {idx: text(r.get("name")) for idx, r in c.table({"name": DOT1Q_VLAN_NAME}).items()}
    members = {}
    if names:
        try:
            for idx, r in c.table({"egress": DOT1Q_VLAN_EGRESS, "untagged": DOT1Q_VLAN_UNTAGGED}).items():
                members[idx] = (len(port_list(r.get("egress"))), len(port_list(r.get("untagged"))))
        except SnmpError:
            pass
    else:  # Cisco without Q-BRIDGE VLAN names
        try:
            names = {idx: text(r.get("name")) for idx, r in c.table({"name": CISCO_VTP_VLAN_NAME}).items()}
        except SnmpError:
            names = {}
    out = [{"id": int(i) if i.isdigit() else i, "name": n, "ports": members.get(i, (None, None))[0],
            "untagged": members.get(i, (None, None))[1]} for i, n in names.items()]
    return sorted(out, key=lambda v: v["id"] if isinstance(v["id"], int) else 10 ** 9)


def read_neighbors(c, ifnames=None):
    ifnames = ifnames or {}
    out = []
    try:
        loc = {idx: text(r.get("d")) or text(r.get("i"))
               for idx, r in c.table({"d": LLDP_LOC_PORT_DESC, "i": LLDP_LOC_PORT_ID}).items()}
        for idx, r in c.table(LLDP_REM).items():
            parts = idx.split(".")  # timeMark . localPortNum . index
            lp = parts[1] if len(parts) >= 3 else ""
            out.append({"proto": "LLDP", "local": loc.get(lp) or ifnames.get(lp) or lp,
                        "device": text(r.get("sys_name")) or mac(r.get("chassis")),
                        "port": text(r.get("port_desc")) or text(r.get("port_id")), "platform": ""})
    except SnmpError:
        pass
    try:
        for idx, r in c.table(CDP_CACHE).items():
            ifi = idx.split(".")[0]
            addr = r.get("address")
            out.append({"proto": "CDP", "local": ifnames.get(ifi) or ifi, "device": text(r.get("device")),
                        "port": text(r.get("port")), "platform": text(r.get("platform")),
                        "address": ".".join(str(b) for b in addr) if isinstance(addr, bytes) and len(addr) == 4 else ""})
    except SnmpError:
        pass
    return out


def read_macs(c, ifnames=None, limit=4000):
    ifnames = ifnames or {}
    bp_if = {}
    try:
        bp_if = {idx: str(r.get("if")) for idx, r in c.table({"if": DOT1D_BASEPORT_IFINDEX}).items()}
    except SnmpError:
        pass
    out = []
    rows = c.walk(FDB_PORT, limit)
    q = True
    if not rows:
        rows, q = c.walk(FDB_PORT_V1, limit), False
    base = len(oid_tuple(FDB_PORT if q else FDB_PORT_V1))
    for oid, port in rows:
        parts = oid.split(".")[base:]
        vlan = parts[0] if q else ""
        m = ":".join(f"{int(b):02x}" for b in parts[-6:])
        ifi = bp_if.get(str(port), str(port))
        out.append({"mac": m, "vlan": vlan, "bridge_port": port, "port": ifnames.get(ifi) or ifi})
    return out
