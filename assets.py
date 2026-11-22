"""Assets: equipment types (PC, phone, printer…) and the named devices plugged into switch ports.

Also converts the old free-text "Connected equipment" / "Remote switch" columns into
Equipment type + Connected equipment (+ device behind, e.g. a PC plugged into a phone).
"""
import datetime
import re

SWITCH_TYPE = "Switch"

DEFAULT_ASSET_TYPES = [  # name, kind (topology shape), colour
    ("PC", "pc", "#2563eb"), ("Phone", "phone", "#16a34a"), ("Printer", "printer", "#9333ea"),
    ("Barcode printer", "barcode", "#c026d3"), ("Access point", "ap", "#0891b2"), ("Camera", "camera", "#ea580c"),
    ("Server", "server", "#475569"), ("Firewall", "firewall", "#dc2626"), ("Router", "router", "#ca8a04"),
    ("Unmanaged switch", "hub", "#0f766e"), ("Other", "other", "#64748b")]

MULTI_KINDS = ("server", "firewall", "router")  # types whose assets are usually cabled to several ports

KIND_RULES = [  # (kind, regex) - first match wins
    ("firewall", r"FORTIGATE|FIREWALL|PARE.?FEU|\bFW\b|\bFG\b|PFSENSE|SOPHOS"),
    ("router", r"ROUTER|ROUTEUR|CANALBOX|\bFIBER|\bFIBRE|ORANGE|STARLINK|STRALINK|MODEM|\bISP\b|INTERNET|\bWAN\b|UPLINK|\bLINK\b"),
    ("switch", r"SWITCH|\bSW[A-Z0-9]"),
    ("ap", r"\bAP\b|WIFI|WI-FI|ACCESS ?POINT|BORNE|UNIFI|\bWLAN"),
    ("camera", r"CAM|CAMERA|CAMÉRA|\bNVR\b|\bDVR\b|VIDEO|VIDÉO|CCTV"),
    ("phone", r"PHONE|\bTEL\b|TÉLÉPHONE|TELEPHONE|VOIP|IPBX|PABX|\bVOICE|\bVOIX|^\s*\d{3,5}\s*$"),
    ("barcode", r"ZEBRA|HONEYWELL|BARCODE|BAR CODE|CODE.?BARRE|ETIQUET|LABEL PRINT|DATAMAX|INTERMEC|\bTSC\b|SATO\b"),
    ("printer", r"PRINT|IMPRIM|PHOTOCOP|COPIEUR|COPIER|SCAN|\bMFP\b|TRACEUR|PLOTTER|LASER"),
    ("server", r"\bESX|\bNAS\d*|SYNOLOGY|SERVER|SERVEUR|\bSRV|-DC-|\bDC\d|HYPER-?V|\bVM\b|PROXMOX|VCENTER|BACKUP|\bSAN\b"),
]
KIND_RE = [(k, re.compile(rx, re.I)) for k, rx in KIND_RULES]
# "1114/PC0107" or "1114 / PC0107": phone extension, then the device plugged behind the phone
PHONE_CHAIN = re.compile(r"^\s*(\d{3,5})\s*/\s*(.+?)\s*$")
S2S_TEXT = re.compile(r"^\s*SWITCH\s*TO\s*SWITCH", re.I)


def classify(text, ptype=""):
    """Best-guess kind of device from its name (and the port type as a hint)."""
    for kind, rx in KIND_RE:
        if rx.search(text or ""):
            return kind
    for kind, rx in KIND_RE[:3]:
        if rx.search(ptype or ""):
            return kind
    return "pc" if any(ch.isalnum() for ch in (text or "")) else "other"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_types(con):
    """Default asset types on a new database + the built-in Switch type."""
    if not con.execute("SELECT 1 FROM asset_types WHERE system=0 LIMIT 1").fetchone():
        for j, (n, k, c) in enumerate(DEFAULT_ASSET_TYPES):
            con.execute("INSERT OR IGNORE INTO asset_types(name, kind, color, sort, multi, icon) VALUES (?,?,?,?,?,?)",
                        (n, k, c, j + 1, 1 if k in MULTI_KINDS else 0, k if k != "pc" else "pc"))
    if not con.execute("SELECT 1 FROM asset_types WHERE system=1").fetchone():
        con.execute("INSERT OR IGNORE INTO asset_types(name, kind, color, sort, system, icon) VALUES (?,?,?,?,1,?)",
                    (SWITCH_TYPE, "switch", "#0f172a", 0, "switch"))
        con.execute("UPDATE asset_types SET system=1, kind='switch' WHERE name=?", (SWITCH_TYPE,))


def type_for_kind(con, kind):
    row = con.execute("SELECT name FROM asset_types WHERE kind=? AND system=0 ORDER BY sort, id LIMIT 1",
                      (kind,)).fetchone() \
        or con.execute("SELECT name FROM asset_types WHERE kind='other' AND system=0 ORDER BY sort, id LIMIT 1").fetchone() \
        or con.execute("SELECT name FROM asset_types WHERE system=0 ORDER BY sort, id LIMIT 1").fetchone()
    return row[0] if row else ""


def find_asset(con, name):
    """(asset row with type name) or None - names are unique, case-insensitive."""
    return con.execute("""SELECT a.*, t.name AS type_name FROM assets a JOIN asset_types t ON t.id=a.type_id
                          WHERE a.name=? COLLATE NOCASE""", (name.strip(),)).fetchone()


def switch_name(con, name):
    row = con.execute("SELECT name FROM switches WHERE name=? COLLATE NOCASE ORDER BY status='active' DESC LIMIT 1",
                      (name.strip(),)).fetchone()
    return row[0] if row else None


def ensure_asset(con, type_name, name):
    """Return the asset type the name really belongs to, creating the asset if it does not exist yet.
    Raises ValueError when the name is already used by another type or by a switch."""
    name = (name or "").strip()
    if not name or not type_name or type_name == SWITCH_TYPE:
        return type_name
    if switch_name(con, name):
        raise ValueError(("switch", name))
    row = find_asset(con, name)
    if row is not None:
        if row["type_name"].lower() != type_name.lower():
            raise ValueError(("type", name, row["type_name"]))
        return row["type_name"]
    t = con.execute("SELECT id, name FROM asset_types WHERE name=? COLLATE NOCASE", (type_name,)).fetchone()
    if t is None:
        raise ValueError(("notype", type_name))
    con.execute("INSERT INTO assets(type_id, name, created_at) VALUES (?,?,?)", (t["id"], name, now()))
    return t["name"]


def _asset_for(con, text, ptype, created):
    """Existing or new asset for a free-text device name -> (type name, asset name)."""
    text = text.strip()
    row = find_asset(con, text)
    if row is not None:
        return row["type_name"], row["name"]
    kind = classify(text, ptype)
    if kind == "switch":
        kind = "hub"  # named like a switch but not a documented one: small desk switch, typo…
    tname = type_for_kind(con, kind)
    tid = con.execute("SELECT id FROM asset_types WHERE name=?", (tname,)).fetchone()[0]
    con.execute("INSERT INTO assets(type_id, name, created_at) VALUES (?,?,?)", (tid, text, now()))
    created[tname] = created.get(tname, 0) + 1
    return tname, text


def convert_port(con, p, created):
    """New values {eq_type, equipment, eq2_type, equipment2, remote_switch, description} for a port that still uses
    the old free-text columns, or None when there is nothing to convert."""
    if p["eq_type"]:
        return None
    eq = (p["equipment"] or "").strip()
    rs = (p["remote_switch"] or "").strip()
    desc = p["description"] or ""
    out = {"eq_type": "", "equipment": "", "eq2_type": "", "equipment2": "", "remote_switch": "", "description": desc}

    def keep_text(txt):  # old text that is not the device name: keep it in the description
        if txt and not S2S_TEXT.match(txt) and txt.lower() not in desc.lower():
            out["description"] = (desc + " · " if desc else "") + txt

    if rs:
        sw = switch_name(con, rs)
        if sw or classify(rs) == "switch":  # documented switch (or a switch name with a typo - shown as unknown)
            out.update(eq_type=SWITCH_TYPE, equipment=sw or rs, remote_switch=sw or rs)
        else:  # firewall, router, AP… that used to be typed in the Remote switch column
            out["eq_type"], out["equipment"] = _asset_for(con, rs, p["ptype"], created)
        if eq.lower() != rs.lower():
            keep_text(eq)
    elif eq:
        if S2S_TEXT.match(eq) or eq in ("-", "?", "/"):
            keep_text(eq)
        elif switch_name(con, eq):
            sw = switch_name(con, eq)
            out.update(eq_type=SWITCH_TYPE, equipment=sw, remote_switch=sw)
        else:
            m = PHONE_CHAIN.match(eq)
            if m:  # phone with a PC (or another device) plugged behind it
                out["eq_type"], out["equipment"] = _asset_for(con, m.group(1), p["ptype"], created)
                behind = m.group(2).strip()
                sw = switch_name(con, behind)
                if sw:
                    out["eq2_type"], out["equipment2"] = SWITCH_TYPE, sw
                else:
                    out["eq2_type"], out["equipment2"] = _asset_for(con, behind, "", created)
            else:
                out["eq_type"], out["equipment"] = _asset_for(con, eq, p["ptype"], created)
    else:
        return None
    return out


def convert_all(con, where="", params=()):
    """Convert every port still using the old free-text columns. Returns (ports converted, assets created by type)."""
    ensure_types(con)
    created, n = {}, 0
    for p in con.execute(f"SELECT * FROM ports WHERE eq_type='' AND (equipment<>'' OR remote_switch<>''){where}",
                         params).fetchall():
        vals = convert_port(con, p, created)
        if vals is None:
            continue
        con.execute(f"UPDATE ports SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), p["id"]))
        n += 1
    return n, created


def asset_uses(con, name, exclude_port=None):
    """Ports (id, switch name, port name) where an asset is connected, as equipment or as the device behind."""
    return con.execute("""SELECT p.id, s.name AS sw, p.name FROM ports p JOIN switches s ON s.id=p.switch_id
                          WHERE (p.equipment=? COLLATE NOCASE OR p.equipment2=? COLLATE NOCASE)
                          AND s.status<>'replaced' AND p.id<>? ORDER BY s.name, p.idx""",
                       (name, name, exclude_port or -1)).fetchall()


def usage_conflict(con, type_name, name, port_id, released=()):
    """None, or ('defective', ) / ('in_use', 'SW1 port 3') when the asset may not be connected to this port.
    released: ports that are giving this asset up (same save, own pending request or same maintenance ticket)."""
    if not name or not type_name or type_name == SWITCH_TYPE:
        return None
    a = con.execute("""SELECT a.state, t.multi FROM assets a JOIN asset_types t ON t.id=a.type_id
                       WHERE a.name=? COLLATE NOCASE""", (name,)).fetchone()
    if a is None:
        return None
    uses = [u for u in asset_uses(con, name, port_id) if u["id"] not in released]
    if a["state"] == "defective" and not uses:
        return ("defective",)
    if not a["multi"] and uses:
        return ("in_use", f"{uses[0]['sw']} {uses[0]['name']}")
    return None
