"""Minimal LDAP v3 client (no external package) for signing in with Active Directory.

Supports LDAPS (636), StartTLS (389) and plain LDAP, simple bind, subtree search with RFC 4515 filters
(including AD's LDAP_MATCHING_RULE_IN_CHAIN for nested groups). Only what SwitchDoc needs: find a user,
check his password by binding as him, read his attributes and his groups.
"""
import re
import socket
import ssl

IN_CHAIN = "1.2.840.113556.1.4.1941"
USER_ATTRS = ["distinguishedName", "sAMAccountName", "userPrincipalName", "givenName", "sn", "displayName", "mail",
              "title", "memberOf", "objectGUID", "userAccountControl"]
DEFAULT_FILTER = "(&(objectCategory=person)(objectClass=user)(sAMAccountName={username}))"


class LdapError(Exception):
    """code: short machine reason (connect, tls, bind, credentials, not_found, disabled, protocol, config)."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


# ------------------------------------------------------------------ BER
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, value):
    return bytes([tag]) + _len(len(value)) + value


def ber_int(n, tag=0x02):
    if n == 0:
        return tlv(tag, b"\x00")
    b = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return tlv(tag, b)


def ber_str(s, tag=0x04):
    return tlv(tag, s.encode("utf-8") if isinstance(s, str) else s)


def ber_bool(v, tag=0x01):
    return tlv(tag, b"\xff" if v else b"\x00")


def seq(*items, tag=0x30):
    return tlv(tag, b"".join(items))


def parse(data, pos=0):
    """-> (tag, value bytes, next position)"""
    tag = data[pos]
    ln = data[pos + 1]
    pos += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + k], "big")
        pos += k
    return tag, data[pos:pos + ln], pos + ln


def children(value):
    out, pos = [], 0
    while pos < len(value):
        tag, v, pos = parse(value, pos)
        out.append((tag, v))
    return out


def as_int(v):
    return int.from_bytes(v, "big", signed=True) if v else 0


# ------------------------------------------------------------------ filters (RFC 4515)
def escape(value):
    """Escape a value to put inside a filter."""
    out = []
    for ch in value:
        if ch in "*()\\\0":
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _unescape(s):
    return re.sub(r"\\([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s).encode("utf-8") \
        if "\\" in s else s.encode("utf-8")


def encode_filter(f):
    f = f.strip()
    node, rest = _filter(f, 0)
    if f[rest:].strip():
        raise LdapError("config", f"Invalid LDAP filter: {f}")
    return node


def _filter(f, i):
    if f[i] != "(":
        raise LdapError("config", f"Invalid LDAP filter: {f}")
    i += 1
    op = f[i]
    if op in "&|":
        i += 1
        parts = []
        while f[i] == "(":
            node, i = _filter(f, i)
            parts.append(node)
        if f[i] != ")":
            raise LdapError("config", f"Invalid LDAP filter: {f}")
        return tlv(0xA0 if op == "&" else 0xA1, b"".join(parts)), i + 1
    if op == "!":
        node, i = _filter(f, i + 1)
        if f[i] != ")":
            raise LdapError("config", f"Invalid LDAP filter: {f}")
        return tlv(0xA2, node), i + 1
    j = i
    depth = 0
    while f[j] != ")" or depth:
        j += 1
    item = f[i:j]
    return _item(item), j + 1


def _item(item):
    m = re.match(r"^([\w.;-]*)((?::dn)?(?::[\w.]+)?):=(.*)$", item)
    if m and m.group(2):  # extensible match  attr:rule:=value
        attr, ext, value = m.groups()
        rule = ext.replace(":dn", "").lstrip(":")
        body = b""
        if rule:
            body += ber_str(rule, 0x81)
        if attr:
            body += ber_str(attr, 0x82)
        body += tlv(0x83, _unescape(value))
        if ":dn" in ext:
            body += ber_bool(True, 0x84)
        return tlv(0xA9, body)
    m = re.match(r"^([\w.;-]+)(>=|<=|~=|=)(.*)$", item)
    if not m:
        raise LdapError("config", f"Invalid LDAP filter item: {item}")
    attr, op, value = m.groups()
    if op == "=" and value == "*":
        return tlv(0x87, attr.encode())
    if op == "=" and "*" in value:
        parts = value.split("*")
        subs = b""
        if parts[0]:
            subs += tlv(0x80, _unescape(parts[0]))
        for p in parts[1:-1]:
            if p:
                subs += tlv(0x81, _unescape(p))
        if parts[-1]:
            subs += tlv(0x82, _unescape(parts[-1]))
        return tlv(0xA4, ber_str(attr) + seq(subs))
    tag = {"=": 0xA3, ">=": 0xA5, "<=": 0xA6, "~=": 0xA8}[op]
    return tlv(tag, ber_str(attr) + tlv(0x04, _unescape(value)))


# ------------------------------------------------------------------ connection
class Connection:
    def __init__(self, host, port, security="ldaps", verify=False, timeout=5):
        self.msgid = 0
        self.buf = b""
        try:
            raw = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise LdapError("connect", f"{host}:{port} - {e}")
        raw.settimeout(timeout)
        self.sock = raw
        self.host = host
        self.verify = verify
        if security == "ldaps":
            self._tls()
        elif security == "starttls":
            resp = self.request(tlv(0x77, ber_str("1.3.6.1.4.1.1466.20037", 0x80)), 0x78)
            code, msg = self._result(resp)
            if code != 0:
                raise LdapError("tls", f"StartTLS refused by {host}: {msg}")
            self._tls()

    def _tls(self):
        ctx = ssl.create_default_context()
        if not self.verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            self.sock = ctx.wrap_socket(self.sock, server_hostname=self.host)
        except (ssl.SSLError, OSError) as e:
            raise LdapError("tls", f"TLS with {self.host}: {e}")

    def close(self):
        try:
            self._send(seq(ber_int(self._next()), tlv(0x42, b"")))
        except (OSError, LdapError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _next(self):
        self.msgid += 1
        return self.msgid

    def _send(self, data):
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise LdapError("connect", str(e))

    def _read_message(self):
        while True:
            if len(self.buf) >= 2:
                ln = self.buf[1]
                hdr = 2
                if ln & 0x80:
                    k = ln & 0x7F
                    if len(self.buf) >= 2 + k:
                        ln = int.from_bytes(self.buf[2:2 + k], "big")
                        hdr = 2 + k
                    else:
                        ln = None
                if ln is not None and len(self.buf) >= hdr + ln:
                    msg, self.buf = self.buf[:hdr + ln], self.buf[hdr + ln:]
                    return msg
            try:
                chunk = self.sock.recv(65536)
            except (OSError, socket.timeout) as e:
                raise LdapError("connect", f"no answer from {self.host}: {e}")
            if not chunk:
                raise LdapError("connect", f"{self.host} closed the connection")
            self.buf += chunk

    def request(self, op, expect):
        mid = self._next()
        self._send(seq(ber_int(mid), op))
        while True:
            tag, value, _ = parse(self._read_message())
            parts = children(value)
            if as_int(parts[0][1]) != mid:
                continue
            if parts[1][0] != expect:
                raise LdapError("protocol", f"unexpected answer 0x{parts[1][0]:02x}")
            return parts[1][1]

    @staticmethod
    def _result(value):
        parts = children(value)
        code = as_int(parts[0][1])
        msg = parts[2][1].decode("utf-8", "replace") if len(parts) > 2 else ""
        return code, msg

    def bind(self, who, password):
        """Simple bind. An empty password is refused here: AD accepts it as an anonymous bind."""
        if not password:
            raise LdapError("credentials", "empty password")
        op = tlv(0x60, ber_int(3) + ber_str(who) + tlv(0x80, password.encode("utf-8")))
        code, msg = self._result(self.request(op, 0x61))
        if code == 49:
            raise LdapError("credentials", msg)
        if code != 0:
            raise LdapError("bind", f"LDAP error {code}: {msg}")

    def search(self, base, flt, attrs, scope=2, size=0):
        """-> [(dn, {attr lower: [values str]})]"""
        op = tlv(0x63, ber_str(base) + ber_int(scope, 0x0A) + ber_int(0, 0x0A) + ber_int(size) + ber_int(0)
                 + ber_bool(False) + encode_filter(flt) + seq(*[ber_str(a) for a in attrs]))
        mid = self._next()
        self._send(seq(ber_int(mid), op))
        out = []
        while True:
            tag, value, _ = parse(self._read_message())
            parts = children(value)
            if as_int(parts[0][1]) != mid:
                continue
            ptag, pval = parts[1]
            if ptag == 0x64:  # entry
                items = children(pval)
                dn = items[0][1].decode("utf-8", "replace")
                vals = {}
                for _t, av in children(items[1][1]):
                    a = children(av)
                    name = a[0][1].decode().lower()
                    raw = [v for _x, v in children(a[1][1])]
                    vals[name] = [r.hex() if name == "objectguid" else r.decode("utf-8", "replace") for r in raw]
                out.append((dn, vals))
            elif ptag == 0x65:  # done
                code, msg = self._result(pval)
                if code not in (0, 4):  # 4 = size limit: keep what we got
                    raise LdapError("protocol", f"search failed ({code}): {msg}")
                return out
            # 0x73 = referral: ignored


# ------------------------------------------------------------------ high level
def servers_of(cfg):
    default = 636 if cfg.get("security") == "ldaps" else 389
    out = []
    for s in re.split(r"[\s,;]+", cfg.get("servers") or ""):
        if not s:
            continue
        host, _, port = s.partition(":") if s.count(":") == 1 else (s, "", "")
        out.append((host, int(port or cfg.get("port") or default)))
    return out


def open_connection(cfg):
    errors = []
    for host, port in servers_of(cfg):
        try:
            return Connection(host, port, cfg.get("security") or "ldaps", bool(cfg.get("verify_cert")),
                              int(cfg.get("timeout") or 5))
        except LdapError as e:
            errors.append(str(e))
    if not errors:
        raise LdapError("config", "no domain controller configured")
    raise LdapError("connect", " / ".join(errors))


def split_login(cfg, login):
    """'DOMAIN\\user', 'user@domain' or 'user' -> 'user'"""
    login = login.strip()
    if "\\" in login:
        return login.split("\\", 1)[1]
    if "@" in login:
        return login.split("@", 1)[0]
    return login


def _first(vals, key):
    v = vals.get(key.lower())
    return v[0] if v else ""


def _user_info(dn, vals):
    return {"dn": dn, "sam": _first(vals, "sAMAccountName"), "upn": _first(vals, "userPrincipalName"),
            "first_name": _first(vals, "givenName"), "last_name": _first(vals, "sn"),
            "display": _first(vals, "displayName"), "email": _first(vals, "mail"), "title": _first(vals, "title"),
            "guid": _first(vals, "objectGUID"), "member_of": vals.get("memberof", []),
            "uac": int(_first(vals, "userAccountControl") or 0)}


def _find_user(con, cfg, sam):
    flt = (cfg.get("user_filter") or DEFAULT_FILTER).replace("{username}", escape(sam))
    found = con.search(cfg["base_dn"], flt, USER_ATTRS, size=2)
    if not found:
        raise LdapError("not_found", f"{sam} not found under {cfg['base_dn']}")
    return _user_info(*found[0])


def _groups(con, cfg, user):
    """DNs and names (CN) of the user's groups - nested groups included when asked (AD in-chain rule)."""
    dns = list(user["member_of"])
    if cfg.get("nested"):
        try:
            for dn, _v in con.search(cfg["base_dn"], f"(member:{IN_CHAIN}:={escape(user['dn'])})", ["cn"]):
                if dn.lower() not in {d.lower() for d in dns}:
                    dns.append(dn)
        except LdapError:
            pass  # not an Active Directory: direct groups only
    names = []
    for dn in dns:
        m = re.match(r"^\s*CN=((?:\\.|[^,])+)", dn, re.I)
        names.append(m.group(1).replace("\\", "") if m else dn)
    return dns, names


def service_bind(con, cfg, bind_password):
    who = cfg.get("bind_user") or ""
    if "@" not in who and "=" not in who and "\\" not in who and cfg.get("upn_suffix"):
        who = f"{who}@{cfg['upn_suffix']}"
    con.bind(who, bind_password)


def authenticate(cfg, login, password, bind_password=""):
    """Check login / password against the directory. -> user info dict with 'groups' (DNs) and 'group_names'."""
    if not password:
        raise LdapError("credentials", "empty password")
    if not cfg.get("base_dn"):
        raise LdapError("config", "base DN missing")
    sam = split_login(cfg, login)
    if not sam:
        raise LdapError("credentials", "empty username")
    con = open_connection(cfg)
    try:
        if cfg.get("bind_user"):
            try:
                service_bind(con, cfg, bind_password)
            except LdapError as e:
                raise LdapError("bind", f"service account: {e}")
            user = _find_user(con, cfg, sam)
            if user["uac"] & 2:
                raise LdapError("disabled", f"{sam} is disabled in the directory")
            con.bind(user["dn"], password)  # the user's own password
            service_bind(con, cfg, bind_password)  # back to the service account to read the groups
        else:
            who = f"{sam}@{cfg['upn_suffix']}" if cfg.get("upn_suffix") else login
            con.bind(who, password)
            user = _find_user(con, cfg, sam)
            if user["uac"] & 2:
                raise LdapError("disabled", f"{sam} is disabled in the directory")
        user["groups"], user["group_names"] = _groups(con, cfg, user)
        return user
    finally:
        con.close()


def test_connection(cfg, bind_password=""):
    """-> (server used, number of users visible) - raises LdapError."""
    con = open_connection(cfg)
    try:
        if cfg.get("bind_user"):
            service_bind(con, cfg, bind_password)
        found = con.search(cfg["base_dn"], "(&(objectCategory=person)(objectClass=user))", ["sAMAccountName"],
                           size=50) if cfg.get("bind_user") else []
        return con.host, len(found)
    finally:
        con.close()


def matches(group_rule, dns, names):
    """An AD group of the mapping (its name or its full DN) is among the user's groups."""
    g = (group_rule or "").strip().lower()
    if not g:
        return False
    if "=" in g:
        return g in {d.lower() for d in dns}
    return g in {n.lower() for n in names}
