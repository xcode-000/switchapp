"""Import / export between SwitchDoc and Excel workbooks.

The importer understands the layout of the existing "Network Switch Documentation" workbook and the
workbooks SwitchDoc exports (in any of its languages):
  * "Summary" sheet  - one row per switch (area, location, rack, designation, name, PoE, serial, IP,
                       console/telnet/config passwords, web user/password, model)
  * "VLAN" sheet     - VLAN number, name, subnet, DHCP
  * port sheets      - any sheet with "Switch" and "Port Number" headers (Server Room, Workshop, ...)
Column positions are found by header name, so extra / missing / reordered columns are tolerated.
"""
import datetime
import io
import json
import re

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import assets as assetlib
import i18n

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
EMPTY = {"", "-", "n/a", "na", "none"}
YES = {"yes", "y", "1", "true", "oui", "sí", "si", "sim"}


def _plain(_text, **kw):
    return _text % kw if kw else _text


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    v = str(v).strip()
    return "" if v.lower() in EMPTY else v


MODEL_RE = re.compile(r"((?:mikrotik|cisco|d-link|tp-link)\s+)?\b((?:WS-C|C9\d{2}|CRS|CRD|CCR|DGS-|DES-|SG\d|TL-SG|FS\d)[\w+/.-]*)", re.I)


def _model_from_text(text):
    """Pick a model name out of a free-text designation such as 'Server Room (CORE) WS-C3850-24P'."""
    m = MODEL_RE.search(text or "")
    return m.group(0).strip() if m else ""


def _norm_header(h):
    h = str(h or "").strip().lower().replace("pannel", "panel")
    h = re.sub(r"[\d.]+$", "", h).strip()  # "Port Number1", "VLAN." -> "port number", "vlan"
    return h


def _aliases(*english):
    """The given English header texts plus their translations, normalised."""
    out = set()
    for e in english:
        out.add(_norm_header(e))
        for code in i18n.LANGUAGES:
            t = i18n.catalog(code).get(e)
            if t:
                out.add(_norm_header(t))
    return out


def _norm_type(v, known):
    """Map a sheet value onto a configured port type (case-insensitive); unknown values are kept."""
    v = _s(v)
    if not v:
        return ""
    low = v.lower()
    if low in known:
        return known[low]
    if ("sfp" in low or "fiber" in low or "fibre" in low) and "sfp / fiber" in known:
        return known["sfp / fiber"]
    return v.title()


def _norm_vlan(v):
    v = _s(v)
    if v.lower() in ("vlan all", "all", "vlan-all"):
        return "ALL"
    return v


_DISABLED = None
SPEED_LABELS = {"fast": "Fast Ethernet", "gigabit": "Gigabit", "fiber": "Fiber"}


def _norm_speed(v, ptype=""):
    """Fast Ethernet / Gigabit / Fiber (in any language, or 100 / 1000 / SFP…) -> fast / gigabit / fiber."""
    low = _s(v).lower()
    for key, label in SPEED_LABELS.items():
        if low and low in _aliases(label) | {key}:
            return key
    if re.search(r"fib|sfp|optic|10g", low):
        return "fiber"
    if re.search(r"fast|^100\b|100 ?m", low):
        return "fast"
    return "gigabit"


def _norm_status(v):
    global _DISABLED
    if _DISABLED is None:
        _DISABLED = {"disabled", "disable", "down", "shutdown", "off"} | _aliases("Disabled")
    return "disabled" if _s(v).lower() in _DISABLED else "active"


def _upsert_switch(con, name, vals):
    row = con.execute("SELECT id FROM switches WHERE name=?", (name,)).fetchone()
    vals = {k: v for k, v in vals.items() if v not in (None, "")} if row else vals
    vals["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if row:
        if vals:
            con.execute(f"UPDATE switches SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?",
                        (*vals.values(), row["id"]))
        return row["id"], False
    vals["name"] = name
    cur = con.execute(f"INSERT INTO switches({', '.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                      tuple(vals.values()))
    return cur.lastrowid, True


# Summary columns: field -> (header aliases, position in the original workbook layout)
SUMMARY_COLS = {
    "area": (("Area",), 0), "location": (("Location", "Location / floor"), 1),
    "rack": (("Abreviation/Rack Location", "Rack", "Rack / abbreviation", "Rack identifier"), 2),
    "designation": (("Designation", "Office"), 3), "name": (("Switch Name",), 4), "poe": (("POE", "PoE"), 5),
    "serial": (("Serial", "Serial number"), 6), "ip": (("IP Address",), 7),
    "console": (("Console0 Password", "Console Password", "Console password"), 8),
    "telnet": (("Telnet", "Telnet Password", "Telnet password"), 9),
    "enable": (("Config Terminal Password", "Enable / config terminal password"), 10),
    "web_user": (("Web Interface Username", "Web Username", "Web username"), 11),
    "web_pass": (("Web Interface Password", "Web Password", "Web password"), 12),
    "model": (("Model",), 13), "ssh_user": (("SSH Username", "SSH username"), None),
    "ssh_pass": (("SSH Password", "SSH password"), None), "notes": (("Notes",), None),
    "cred_name": (("Credential Profile", "Credential profile"), None),
    "layer": (("Layer",), None), "sfp": (("SFP ports",), None), "ports": (("Ports", "Number of ports"), None),
    "groups": (("Groups",), None),
    "in_service": (("In service since", "In service date", "In service", "Date in service", "Commissioning date"), None),
    "monitor": (("Status check", "Monitor", "Monitored", "Monitoring"), None),
}
PORT_COLS = {
    "switch": ("Switch",), "status": ("Status",), "ptype": ("Port Type", "Type"), "vlan": ("VLAN",),
    "patch_port": ("Patch Panel Port", "Patch panel port"), "patch_label": ("Patch Panel Label", "Patch panel label"),
    "equipment": ("Connected Equipment", "Connected equipment"), "remote_switch": ("Switch Name", "Remote switch"),
    "user": ("User",), "remote_port": ("Switch to Switch Port", "Port", "Remote port"), "description": ("Label", "Description"),
    "bridge": ("Bridge",), "speed": ("Speed", "Network speed"), "eq_type": ("Equipment Type", "Equipment type"),
    "eq2_type": ("Device Behind Type", "Type of the device behind"), "equipment2": ("Device Behind", "Device behind"),
}
ASSET_COLS = {"eq_type": ("Equipment Type", "Equipment type"), "name": ("Asset Name", "Asset name", "Name"),
              "ip": ("IP Address", "IP address"), "mac": ("MAC Address", "MAC address"),
              "serial": ("Serial", "Serial number"), "location": ("Location",), "notes": ("Notes",)}


def import_workbook(path, con, enc, replace=False, add_to_groups=None, tr=_plain):
    """add_to_groups: None = user sees everything; otherwise a set of group ids every imported
    switch is also added to (so a group-restricted user can still see what they imported).
    tr: translation function for the log messages."""
    wb = openpyxl.load_workbook(path, data_only=True)
    raw = openpyxl.load_workbook(path, read_only=True)
    formula_sheets = set()
    for ws in raw.worksheets:
        for r in ws.iter_rows(min_row=2, max_row=3, values_only=True):
            if any(isinstance(c, str) and c.startswith("=") for c in r):
                formula_sheets.add(ws.title)
    raw.close()
    touched = set()
    log, stats = [], {"switches_new": 0, "switches_updated": 0, "ports": 0, "vlans": 0, "cred_profiles": 0}
    known_types = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM port_types")}
    summary_names, vlan_names = _aliases("Summary"), _aliases("VLAN", "VLANs")
    if replace:
        for t in ("change_requests", "ports", "switches", "vlans", "cred_profiles", "assets"):
            con.execute(f"DELETE FROM {t}")
        log.append(tr("Existing switches, ports, assets, VLANs and credential profiles were cleared (replace mode)."))

    # ---- VLANs
    vlan_cols = {"number": _aliases("VLAN number", "VLAN"), "name": _aliases("Vlan Name", "Name"),
                 "subnet": _aliases("Subnet", "Subnet / gateway"), "dhcp": _aliases("DHCP")}
    for ws in wb.worksheets:
        if _norm_header(ws.title) not in vlan_names:
            continue
        rows = list(ws.iter_rows(values_only=True))
        hdr = [_norm_header(h) for h in rows[0]] if rows else []
        col = {f: next((i for i, h in enumerate(hdr) if h in al), None) for f, al in vlan_cols.items()}
        if col["number"] is None:
            col["number"] = 0
        for r in rows[1:]:
            get = lambda f: _s(r[col[f]]) if col.get(f) is not None and col[f] < len(r) else ""  # noqa: E731
            num = get("number")
            if not num.isdigit():
                continue
            con.execute("""INSERT INTO vlans(number,name,subnet,dhcp) VALUES (?,?,?,?)
                           ON CONFLICT(number) DO UPDATE SET name=excluded.name, subnet=excluded.subnet,
                           dhcp=excluded.dhcp""", (int(num), get("name"), get("subnet"),
                                                   1 if get("dhcp").lower() in YES else 0))
            stats["vlans"] += 1

    # ---- Summary (switch inventory + credentials)
    profiles = {}  # tuple -> profile id
    summary = next((ws for ws in wb.worksheets if _norm_header(ws.title) in summary_names), None)
    if summary is not None:
        rows = list(summary.iter_rows(values_only=True))
        hdr = [_norm_header(h) for h in rows[0]] if rows else []
        by_header = {f: next((i for i, h in enumerate(hdr) if h in _aliases(*names)), None)
                     for f, (names, _pos) in SUMMARY_COLS.items()}
        exported_layout = by_header["name"] is not None and (
            (by_header["ip"] is not None and by_header["ip"] != 7) or by_header["layer"] is not None
            or by_header["groups"] is not None)
        col = {f: (by_header[f] if by_header[f] is not None else (None if exported_layout else pos))
               for f, (_names, pos) in SUMMARY_COLS.items()}
        width = max([c for c in col.values() if c is not None] + [14]) + 2
        area = location = ""
        for n, r in enumerate(rows[1:], start=2):
            r = list(r) + [None] * (width - len(r))
            if not any(_s(c) for c in r):
                continue
            if not exported_layout and not IP_RE.match(_s(r[7])):  # misaligned row: realign on the IP cell
                ip_pos = next((i for i, c in enumerate(r) if IP_RE.match(_s(c))), None)
                if ip_pos is None:
                    log.append(tr("Summary row %(n)s: no IP address found - skipped", n=n))
                    continue
                shift = 7 - ip_pos
                r = ([None] * shift + r) if shift > 0 else r[-shift:]
                r += [None] * (width - len(r))
                log.append(tr("Summary row %(n)s: columns were shifted - imported %(name)s; please double-check its "
                              "fields and credentials", n=n, name=_s(r[4]) or "?"))
            get = lambda f: _s(r[col[f]]) if col.get(f) is not None and col[f] < len(r) else ""  # noqa: E731
            if get("area"):
                area, location = get("area"), ""
            elif exported_layout:
                area = ""
            if get("location") or exported_layout:
                location = get("location")
            name = get("name")
            if not name:
                continue
            secrets_ = [get(k) for k in ("console", "telnet", "enable", "web_pass", "ssh_pass")]
            masked = any(v and set(v) == {"*"} for v in secrets_)
            cred_key = (get("console"), get("telnet"), get("enable"), get("web_user"), get("web_pass"),
                        get("ssh_user"), get("ssh_pass"))
            pid = None
            if get("cred_name"):  # workbook exported by SwitchDoc: re-use the profile by name
                row = con.execute("SELECT id FROM cred_profiles WHERE name=?", (get("cred_name"),)).fetchone()
                pid = row[0] if row else None
                if pid is None and not any(cred_key):  # its lines come from the Credentials sheet
                    pid = con.execute("INSERT INTO cred_profiles(name, notes) VALUES (?,?)",
                                      (get("cred_name"), tr("Created by Excel import"))).lastrowid
            if pid is None and any(cred_key) and not masked:
                if cred_key not in profiles:
                    pname = get("cred_name") or tr("Imported %(n)s (web user: %(u)s)", n=len(profiles) + 1,
                                                   u=cred_key[3] or tr("none"))
                    profiles[cred_key] = con.execute(
                        """INSERT INTO cred_profiles(name, notes, console_pass, telnet_pass, enable_pass,
                           web_user, web_pass, ssh_user, ssh_pass) VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(name) DO UPDATE SET console_pass=excluded.console_pass RETURNING id""",
                        (pname, tr("Created by Excel import - rename me"), enc(cred_key[0]), enc(cred_key[1]),
                         enc(cred_key[2]), cred_key[3], enc(cred_key[4]), cred_key[5], enc(cred_key[6]))).fetchone()[0]
                    stats["cred_profiles"] += 1
                pid = profiles[cred_key]
            vals = {
                "area": area, "location": location, "rack": get("rack"), "designation": get("designation"),
                "poe": 1 if get("poe").lower() in YES else 0, "serial": get("serial"), "ip": get("ip"),
                "model": get("model") or _model_from_text(get("designation")), "cred_profile_id": pid,
            }
            if get("notes"):
                vals["notes"] = get("notes")
            if get("in_service"):
                raw = r[col["in_service"]]
                vals["in_service"] = raw.date().isoformat() if hasattr(raw, "date") else get("in_service")[:10]
            if get("monitor"):  # Status check: Yes / No (empty = unchanged, new switches are checked)
                vals["monitor"] = 1 if get("monitor").lower() in YES else 0
            lay = get("layer").upper().replace("LAYER", "L").replace(" ", "")
            if lay in ("L1", "L2", "L3"):
                vals["layer"] = lay
            if get("sfp"):
                vals["sfp"] = 1
                vals["sfp_count"] = int(get("sfp")) if get("sfp").isdigit() else 0
            sid, new = _upsert_switch(con, name, vals)
            touched.add(sid)
            stats["switches_new" if new else "switches_updated"] += 1
            if get("ports").isdigit():  # create the missing ports (Port 1..N) so the switch can be documented
                want = min(int(get("ports")), 512)
                prefix = (con.execute("SELECT port_prefix FROM switches WHERE id=?", (sid,)).fetchone()[0] or "")
                have = {r[0] for r in con.execute("SELECT idx FROM ports WHERE switch_id=?", (sid,))}
                for i in range(1, want + 1):
                    if i not in have:
                        con.execute("INSERT INTO ports(switch_id, idx, name, ptype) VALUES (?,?,?,?)",
                                    (sid, i, f"{prefix}{i}", known_types.get("access", "Access")))
            for gname in [x.strip() for x in get("groups").split(",") if x.strip()]:
                con.execute("INSERT OR IGNORE INTO sw_groups(name) VALUES (?)", (gname,))
                con.execute("INSERT OR IGNORE INTO switch_groups(switch_id, group_id) SELECT ?, id FROM sw_groups "
                            "WHERE name=?", (sid, gname))

    # ---- Credentials sheet (profile, label, kind, value) - masked values are ignored
    cred_al = _aliases("Credentials")
    kind_al = {k: _aliases(lbl) | {k} for k, lbl in (("user", "Username"), ("secret", "Password / secret"),
                                                       ("snmp", "SNMP community"), ("text", "Other information"))}
    for ws in wb.worksheets:
        if _norm_header(ws.title) not in cred_al:
            continue
        rows = list(ws.iter_rows(values_only=True))
        hdr = [_norm_header(h) for h in rows[0]] if rows else []
        ccol = {f: next((i for i, h in enumerate(hdr) if h in _aliases(*names)), None) for f, names in
                {"profile": ("Credential Profile",), "label": ("Label",), "kind": ("Kind",), "value": ("Value",)}.items()}
        if ccol["profile"] is None or ccol["label"] is None:
            continue
        for r in rows[1:]:
            get = lambda f: _s(r[ccol[f]]) if ccol.get(f) is not None and ccol[f] < len(r) else ""  # noqa: E731
            pname, label, value = get("profile"), get("label"), get("value")
            if not pname or not label:
                continue
            kind = next((k for k, al in kind_al.items() if get("kind").lower() in al), "secret")
            pid = con.execute("SELECT id FROM cred_profiles WHERE name=?", (pname,)).fetchone()
            pid = pid[0] if pid else con.execute("INSERT INTO cred_profiles(name, notes) VALUES (?,?)",
                                                 (pname, tr("Created by Excel import"))).lastrowid
            if kind in ("secret", "snmp") and (not value or set(value) == {"*"}):
                if con.execute("SELECT 1 FROM cred_fields WHERE profile_id=? AND label=?", (pid, label)).fetchone():
                    continue  # masked in the export: keep what is stored
                value = ""
            stored = enc(value) if kind in ("secret", "snmp") else value
            row = con.execute("SELECT id FROM cred_fields WHERE profile_id=? AND label=?", (pid, label)).fetchone()
            if row:
                con.execute("UPDATE cred_fields SET kind=?, value=? WHERE id=?", (kind, stored, row[0]))
            else:
                con.execute("INSERT INTO cred_fields(profile_id, label, kind, value, sort) VALUES (?,?,?,?,"
                            "(SELECT COALESCE(MAX(sort), 0) + 1 FROM cred_fields WHERE profile_id=?))",
                            (pid, label, kind, stored, pid))
            stats["cred_profiles"] += 0

    # ---- Assets sheet (equipment type + unique asset name)
    assetlib.ensure_types(con)
    atypes = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM asset_types")}

    def asset_type(v, n):
        v = _s(v)
        if not v:
            return ""
        if v.lower() in atypes:
            return atypes[v.lower()]
        kind = assetlib.classify(v)
        con.execute("INSERT INTO asset_types(name, kind, sort) VALUES (?,?,?)",
                    (v, "hub" if kind == "switch" else kind, 50))
        atypes[v.lower()] = v
        log.append(tr("Row %(n)s: new equipment type '%(t)s' created", n=n, t=v))
        return v

    stats["assets"] = 0
    assets_al = _aliases("Assets")
    for ws in wb.worksheets:
        if _norm_header(ws.title) not in assets_al:
            continue
        rows = list(ws.iter_rows(values_only=True))
        hdr = [_norm_header(h) for h in rows[0]] if rows else []
        acol = {f: next((i for i, h in enumerate(hdr) if h in _aliases(*names)), None) for f, names in ASSET_COLS.items()}
        if acol["name"] is None or acol["eq_type"] is None:
            continue
        for n, r in enumerate(rows[1:], start=2):
            get = lambda f: _s(r[acol[f]]) if acol.get(f) is not None and acol[f] < len(r) else ""  # noqa: E731
            name = get("name")
            ty = asset_type(get("eq_type"), n)
            if not name or not ty:
                continue
            if ty == assetlib.SWITCH_TYPE:
                continue  # switches are defined on the Summary sheet
            try:
                assetlib.ensure_asset(con, ty, name)
            except ValueError as e:
                log.append(tr("Assets row %(n)s: %(name)s already exists as %(t)s - kept", n=n, name=name,
                              t=e.args[0][2] if e.args[0][0] == "type" else assetlib.SWITCH_TYPE))
                continue
            extra = {k: get(k) for k in ("ip", "mac", "serial", "location", "notes") if get(k)}
            if extra:
                con.execute(f"UPDATE assets SET {', '.join(f'{k}=?' for k in extra)} WHERE name=? COLLATE NOCASE",
                            (*extra.values(), name))
            stats["assets"] += 1
    if stats["assets"]:
        log.append(tr("%(n)s assets imported", n=stats["assets"]))

    def link_equipment(ty, name, n, sw_name, port):
        """(type, name) of a port's equipment, creating the asset if needed."""
        ty = asset_type(ty, n)
        if not name:
            return ty, ""
        if not ty:
            return "", name  # no type: guessed after the import, like the old free-text column
        if ty == assetlib.SWITCH_TYPE:
            return ty, assetlib.switch_name(con, name) or name
        try:
            return assetlib.ensure_asset(con, ty, name), name
        except ValueError as e:
            if e.args[0][0] == "switch":
                return assetlib.SWITCH_TYPE, assetlib.switch_name(con, name)
            if e.args[0][0] == "type":
                log.append(tr("%(sw)s port %(p)s: %(name)s already exists as %(t)s - that type was used",
                              sw=sw_name, p=port, name=name, t=e.args[0][2]))
                return e.args[0][2], assetlib.find_asset(con, name)["name"]
            return "", name

    # ---- Port sheets
    switch_al, portnum_al = _aliases("Switch"), _aliases("Port Number")
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows or ws is summary:
            continue
        hdr = [_norm_header(h) for h in rows[0]]
        if not (set(hdr) & switch_al) or not (set(hdr) & portnum_al):
            continue
        if ws.title in formula_sheets:
            log.append(tr("Sheet '%(s)s' is built from formulas - skipped", s=ws.title))
            continue
        col = {}
        for i, h in enumerate(hdr):
            if h in portnum_al:
                col.setdefault("port", i) if "port" not in col else col.setdefault("remote_port", i)
                continue
            for f, names in PORT_COLS.items():
                if h in _aliases(*names):
                    col.setdefault(f, i)
                    break
        sheet_ports = 0
        for r in rows[1:]:
            get = lambda k: _s(r[col[k]]) if k in col and col[k] < len(r) else ""  # noqa: E731
            sw_name, port = get("switch"), get("port")
            if not sw_name or not port or sw_name.startswith("=") or port.startswith("="):
                continue
            m = re.search(r"(\d+)$", port)
            if not m:
                continue
            idx = int(m.group(1))
            sid, new = _upsert_switch(con, sw_name, {} if con.execute(
                "SELECT 1 FROM switches WHERE name=?", (sw_name,)).fetchone() else {"area": ws.title})
            touched.add(sid)
            if new:
                stats["switches_new"] += 1
                log.append(tr("Switch %(name)s found only on sheet '%(s)s' - created in area '%(s)s'",
                              name=sw_name, s=ws.title))
            desc = get("description")
            if get("bridge"):
                desc = f"{desc} [{get('bridge')}]".strip()
            ptype = _norm_type(get("ptype"), known_types) or known_types.get("access", "Access")
            if "eq_type" in col:  # new layout: Equipment type + Connected equipment (+ device behind)
                ety, eq = link_equipment(get("eq_type"), get("equipment"), sheet_ports + 2, sw_name, port)
                e2ty, eq2 = link_equipment(get("eq2_type"), get("equipment2"), sheet_ports + 2, sw_name, port)
                rsw = eq if ety == assetlib.SWITCH_TYPE else ""
            else:  # old layout: free text, converted to assets after the import
                ety, eq, e2ty, eq2, rsw = "", get("equipment"), "", "", get("remote_switch")
            status = _norm_status(get("status"))
            if status == "disabled":  # a disabled port holds no documentation
                vals = (sid, idx, port, status, ptype, _norm_speed(get("speed"), get("ptype")), "",
                        "", "", "", "", "", "", "", "", "", "")
            else:
                vals = (sid, idx, port, status, ptype, _norm_speed(get("speed"), get("ptype")),
                        _norm_vlan(get("vlan")),
                        get("patch_port"), get("patch_label"), ety, eq, e2ty, eq2, get("user"),
                        rsw, get("remote_port"), desc)
            con.execute("""INSERT INTO ports(switch_id, idx, name, status, ptype, speed, vlan, patch_port, patch_label,
                             eq_type, equipment, eq2_type, equipment2, user, remote_switch, remote_port, description)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(switch_id, idx) DO UPDATE SET name=excluded.name, status=excluded.status,
                             ptype=excluded.ptype, speed=excluded.speed, vlan=excluded.vlan, patch_port=excluded.patch_port,
                             patch_label=excluded.patch_label, eq_type=excluded.eq_type, equipment=excluded.equipment,
                             eq2_type=excluded.eq2_type, equipment2=excluded.equipment2, user=excluded.user,
                             remote_switch=excluded.remote_switch, remote_port=excluded.remote_port,
                             description=excluded.description""", vals)
            sheet_ports += 1
        if sheet_ports:
            log.append(tr("Sheet '%(s)s': %(n)s ports", s=ws.title, n=sheet_ports))
            stats["ports"] += sheet_ports

    # one switch group per area, so access can be granted by area straight away
    created = 0
    palette = ["#0f766e", "#2563eb", "#9333ea", "#db2777", "#ea580c", "#65a30d", "#0891b2", "#ca8a04",
               "#dc2626", "#4f46e5", "#059669", "#7c3aed"]
    for (area,) in con.execute("SELECT DISTINCT area FROM switches WHERE area<>''").fetchall():
        n = con.execute("SELECT COUNT(*) FROM sw_groups").fetchone()[0]
        cur = con.execute("INSERT OR IGNORE INTO sw_groups(name, description, color) VALUES (?,?,?)",
                          (area, tr("Created from the Area column by the Excel import"), palette[n % len(palette)]))
        created += cur.rowcount
        con.execute("""INSERT OR IGNORE INTO switch_groups(switch_id, group_id)
                       SELECT s.id, g.id FROM switches s, sw_groups g WHERE s.area=? AND g.name=?""", (area, area))
    if created:
        log.append(tr("%(n)s switch groups created from the Area column (Settings › Groups)", n=created))
    if add_to_groups:
        for gid in add_to_groups:
            for sid in touched:
                con.execute("INSERT OR IGNORE INTO switch_groups(switch_id, group_id) VALUES (?,?)", (sid, gid))
    stats["groups"] = created

    # switch-to-switch links documented on one side only: fill in the other end when it is still empty
    back = 0
    for p in con.execute("""SELECT p.name, p.idx, s.name AS sw, p.remote_switch, p.remote_port FROM ports p
                            JOIN switches s ON s.id=p.switch_id WHERE p.remote_switch<>'' AND p.remote_port<>''""").fetchall():
        far = con.execute("""SELECT p.id FROM ports p JOIN switches s ON s.id=p.switch_id
                             WHERE s.name=? AND (p.name=? OR CAST(p.idx AS TEXT)=?) AND p.remote_switch=''
                             AND p.equipment='' AND p.status='active' LIMIT 1""",
                          (p["remote_switch"], p["remote_port"], p["remote_port"])).fetchone()
        if far is not None:
            con.execute("UPDATE ports SET eq_type=?, equipment=?, remote_switch=?, remote_port=? WHERE id=?",
                        (assetlib.SWITCH_TYPE, p["sw"], p["sw"], p["name"], far[0]))
            back += 1
    if back:
        log.append(tr("%(n)s links completed on the other switch", n=back))
    n_conv, created_assets = assetlib.convert_all(con)
    if n_conv:
        log.append(tr("%(n)s ports: free-text connected equipment converted to equipment type + asset (%(list)s)",
                      n=n_conv, list=", ".join(f"{k}: {v}" for k, v in sorted(created_assets.items())) or "-"))
    no_ports = [r[0] for r in con.execute(
        "SELECT name FROM switches s WHERE status='active' AND NOT EXISTS "
        "(SELECT 1 FROM ports p WHERE p.switch_id=s.id) ORDER BY name")]
    if no_ports:
        log.append(tr("%(n)s switches have no port sheet yet (set their port count): %(names)s",
                      n=len(no_ports), names=", ".join(no_ports)))
    stats["log"] = log
    return stats


# ---------------------------------------------------------------- export helpers
HEAD_FILL = PatternFill("solid", fgColor="1F2A37")
HEAD_FONT = Font(bold=True, color="FFFFFF")
KEY_FONT = Font(bold=True, color="475569")
TITLE_FONT = Font(bold=True, size=16)


def _sheet(wb, title, headers, rows, widths=None):
    ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "-", title)[:31])
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append(list(r))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(12, len(str(h)) + 4))
    return ws


def _save(wb):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_workbook(con, dec, include_secrets=False, where="", params=(), tr=_plain):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    mask = (lambda v: dec(v)) if include_secrets else (lambda v: "********" if v else "")
    yes, no = tr("Yes"), tr("No")
    sw = con.execute(f"""SELECT s.*, c.name AS cred_name,
                        (SELECT group_concat(g.name, ', ') FROM switch_groups sg JOIN sw_groups g ON g.id=sg.group_id
                         WHERE sg.switch_id=s.id) AS grp
                        FROM switches s LEFT JOIN cred_profiles c ON c.id=s.cred_profile_id
                        LEFT JOIN models m ON m.name=s.model
                        WHERE 1=1{where} ORDER BY s.area, s.location, s.name""", params).fetchall()
    _sheet(wb, tr("Summary"),
           [tr(h) for h in ["Area", "Location", "Rack identifier", "Office", "Switch Name", "POE", "Serial", "IP Address",
                            "Model", "Layer", "SFP ports", "Groups", "Ports", "In service since", "Credential Profile", "Notes",
                            "Status check"]],
           [(r["area"], r["location"], r["rack"], r["designation"], r["name"], yes if r["poe"] else no,
             r["serial"], r["ip"], r["model"], r["layer"] or "L3", (r["sfp_count"] or tr("Yes")) if r["sfp"] else "",
             r["grp"] or "",
             con.execute("SELECT COUNT(*) FROM ports WHERE switch_id=?", (r["id"],)).fetchone()[0],
             r["in_service"] or "", r["cred_name"] or "", r["notes"],
             no if "monitor" in r.keys() and r["monitor"] == 0 else yes)
            for r in sw],
           {4: 24, 5: 16, 7: 18, 8: 15, 9: 20, 12: 20, 14: 16, 15: 24, 16: 30})
    used = {r["cred_profile_id"] for r in sw if r["cred_profile_id"]}
    cred_rows = con.execute(f"""SELECT p.name, f.label, f.kind, f.value FROM cred_fields f JOIN cred_profiles p
                                ON p.id=f.profile_id WHERE p.id IN ({','.join('?' * len(used)) or 'NULL'})
                                ORDER BY p.name, f.sort, f.id""", list(used)).fetchall()
    kinds = {"user": "Username", "secret": "Password / secret", "snmp": "SNMP community", "text": "Other information"}
    _sheet(wb, tr("Credentials"), [tr(h) for h in ["Credential Profile", "Label", "Kind", "Value"]],
           [(c["name"], tr(c["label"]), tr(kinds.get(c["kind"], c["kind"])),
             mask(c["value"]) if c["kind"] in ("secret", "snmp") else c["value"]) for c in cred_rows],
           {1: 26, 2: 32, 3: 20, 4: 30})
    _sheet(wb, tr("VLAN"), [tr(h) for h in ["VLAN number", "Vlan Name", "Subnet", "DHCP", "Notes"]],
           [(r["number"], r["name"], r["subnet"], yes if r["dhcp"] else no, r["notes"])
            for r in con.execute("SELECT * FROM vlans ORDER BY number")])
    port_rows = con.execute(f"""SELECT s.area, s.name AS sw, p.* FROM ports p JOIN switches s ON s.id=p.switch_id
                               WHERE 1=1{where} ORDER BY s.area, s.name, p.idx""", params).fetchall()
    areas = {}
    for p in port_rows:
        areas.setdefault(p["area"] or tr("No area"), []).append(p)
    headers = [tr(h) for h in PORT_IMPORT_HEADERS]
    used = {tr("Summary"), tr("VLAN"), tr("Assets"), tr("Credentials")}
    for area, plist in areas.items():
        title = re.sub(r"[\[\]:*?/\\]", "-", area)[:31]
        while title in used:
            title = title[:28] + "_2"
        used.add(title)
        _sheet(wb, title, headers,
               [(p["sw"], p["name"], tr("Enabled") if p["status"] == "active" else tr("Disabled"), p["ptype"],
                 tr(SPEED_LABELS.get(p["speed"], "Gigabit")), p["vlan"], p["patch_port"], p["patch_label"], p["eq_type"], p["equipment"], p["eq2_type"], p["equipment2"],
                 p["user"], p["remote_port"], p["description"]) for p in plist],
               {1: 14, 5: 14, 7: 18, 9: 16, 10: 28, 11: 16, 12: 22, 15: 28})
    _sheet(wb, tr("Assets"), [tr(h) for h in ASSET_HEADERS],
           [(r["t"], r["name"], r["ip"], r["mac"], r["serial"], r["location"], r["notes"]) for r in con.execute(
               """SELECT t.name AS t, a.* FROM assets a JOIN asset_types t ON t.id=a.type_id
                  ORDER BY t.sort, t.name, a.name COLLATE NOCASE""")],
           {1: 18, 2: 30, 7: 30})
    return _save(wb)


def _fmt_details(details, tr, labels, show=None):
    parts = []
    for k, v in (details or {}).items():
        label = tr(labels.get(k, k))
        if isinstance(v, list) and len(v) == 2:
            o, n = v
            fmt = lambda x: str(show(k, x)) if show else ("" if x in (None, "") else str(x))  # noqa: E731
            parts.append(f"{label}: {fmt(o)} → {fmt(n)}" if o not in (None, "") else f"{label}: {fmt(n)}")
        else:
            parts.append(f"{label}: {v}")
    return "\n".join(parts)


PORT_HEADERS = ["Port", "Status", "Type", "Speed", "VLAN", "Patch panel port", "Patch panel label", "Equipment type",
                "Connected equipment", "User", "Remote port", "Description"]
PORT_WIDTHS = [9, 12, 13, 14, 9, 18, 18, 16, 32, 16, 12, 30]
FIBER_FILL = PatternFill("solid", fgColor="DC2626")
FIBER_FONT = Font(bold=True, color="FFFFFF")
# columns of the port sheets of a workbook that can be imported back (export + template)
PORT_IMPORT_HEADERS = ["Switch", "Port Number", "Status", "Port Type", "Speed", "VLAN", "Patch Panel Port", "Patch Panel Label",
                       "Equipment Type", "Connected Equipment", "Device Behind Type", "Device Behind", "User",
                       "Remote Port", "Description"]
ASSET_HEADERS = ["Equipment Type", "Asset Name", "IP Address", "MAC Address", "Serial", "Location", "Notes"]
THIN = Side(style="thin", color="CBD5E1")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
OK_FILL, OFF_FILL = PatternFill("solid", fgColor="DCFCE7"), PatternFill("solid", fgColor="FEE2E2")


def _tint(hex_color, amount=0.75):
    """Light version of a #rrggbb colour for cell backgrounds."""
    h = (hex_color or "#64748b").lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "E2E8F0"
    return "".join(f"{int(c + (255 - c) * amount):02X}" for c in (r, g, b))


def _port_sheet(wb, title, sw, ports, groups, model, tr, type_colors, total_ports=None, status_label=None):
    """A sheet laid out like the switch page: header block with the switch details, then the port table
    with the same columns, colours and order as on screen."""
    ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "-", title)[:31])
    ws.append([sw["name"] + (f" — {sw['designation']}" if sw["designation"] else "")])
    ws["A1"].font = TITLE_FONT
    left = [(tr("IP address"), sw["ip"]), (tr("Model"), f"{sw['model'] or ''}  ·  {sw['layer'] or 'L3'}" + (f"  ·  {sw['sfp_count'] or ''} SFP" if sw["sfp"] else "")),
            (tr("Serial number"), sw["serial"]), (tr("PoE"), tr("Yes") if sw["poe"] else tr("No")),
            (tr("In service since"), sw["in_service"] if "in_service" in sw.keys() else "")]
    right = [(tr("Area / location"), " · ".join(x for x in (sw["area"], sw["location"], sw["rack"]) if x)),
             (tr("Office"), sw["designation"]),
             (tr("Groups"), ", ".join(groups)), (tr("Status"), status_label or ""),
             (tr("Export date"), datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))]
    left.append((tr("Layer"), sw["layer"] or "L3"))
    from itertools import zip_longest
    for (k1, v1), (k2, v2) in zip_longest(left, right, fillvalue=("", "")):
        ws.append([k1, None, v1, None, None, None, k2, v2])
        r = ws.max_row
        for col in (1, 7):
            ws.cell(r, col).font = KEY_FONT
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=5)
        ws.merge_cells(start_row=r, start_column=8, end_row=r, end_column=12)
    active = sum(1 for p in ports if p["status"] == "active")
    summary = tr("Ports (active / total)") + f": {active} / {len(ports)}"
    if total_ports is not None and total_ports != len(ports):
        summary += "   ·   " + tr("Filtered: %(n)s of %(t)s ports", n=len(ports), t=total_ports)
    ws.append([summary])
    ws.cell(ws.max_row, 1).font = Font(italic=True, color="64748B")
    ws.append([])
    head = ws.max_row + 1
    ws.append([tr(h) for h in PORT_HEADERS])
    for c in ws[head]:
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for p in ports:
        eq = p["equipment"] or ""
        if p["equipment2"]:
            eq += f"\n↳ {p['equipment2']}" + (f" ({p['eq2_type']})" if p["eq2_type"] else "")
        speed = p["speed"] if "speed" in p.keys() else "gigabit"
        ws.append([p["name"], tr("Active") if p["status"] == "active" else tr("Disabled"), p["ptype"],
                   tr(SPEED_LABELS.get(speed, "Gigabit")), p["vlan"],
                   p["patch_port"], p["patch_label"], p["eq_type"], eq, p["user"], p["remote_port"],
                   p["description"]])
        r = ws.max_row
        for c in ws[r]:
            c.border = BORDER
            c.alignment = Alignment(vertical="center", wrap_text=bool(p["equipment2"]) and c.column == 9)
        ws.cell(r, 1).font = Font(bold=True)
        ws.cell(r, 2).fill = OK_FILL if p["status"] == "active" else OFF_FILL
        ws.cell(r, 3).fill = PatternFill("solid", fgColor=_tint(type_colors.get(p["ptype"])))
        if p["status"] != "active":
            for col in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12):
                ws.cell(r, col).font = Font(color="94A3B8", bold=(col == 1))
        if speed == "fiber":  # fiber, and every switch-to-switch link: red label, white text
            ws.cell(r, 4).fill, ws.cell(r, 4).font = FIBER_FILL, FIBER_FONT
        if p["remote_switch"]:
            ws.cell(r, 3).fill, ws.cell(r, 3).font = FIBER_FILL, FIBER_FONT
    for i, w in enumerate(PORT_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(head + 1, 1)
    if ports:
        ws.auto_filter.ref = f"A{head}:{get_column_letter(len(PORT_HEADERS))}{ws.max_row}"
    ws.print_title_rows = f"{head}:{head}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return ws


def export_switch(sw, ports, cred, groups, model, incoming, history, include_secrets, dec, tr, labels, actions,
                  status_labels, all_labels, show=None, type_colors=None, total_ports=None, cred_fields=None):
    """One switch: the port table as on screen, then details / credentials, links and change history."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _port_sheet(wb, sw["name"], sw, ports, groups, model, tr, type_colors or {}, total_ports,
                tr(status_labels.get(sw["status"], sw["status"])))
    ws = wb.create_sheet(tr("Details")[:31])
    info = [(tr("Switch name"), sw["name"]), (tr("Status"), tr(status_labels.get(sw["status"], sw["status"]))),
            (tr("IP address"), sw["ip"]), (tr("Serial number"), sw["serial"]), (tr("Model"), sw["model"]),
            (tr("In service since"), sw["in_service"] if "in_service" in sw.keys() else "")]
    info += [(tr("Layer"), sw["layer"] or "L3"), (tr("SFP ports"), (sw["sfp_count"] or tr("Yes")) if sw["sfp"] else tr("No"))]
    if model:
        info += [(tr("Vendor"), model["vendor"])]
    info += [(tr("PoE"), tr("Yes") if sw["poe"] else tr("No")), (tr("Area"), sw["area"]),
             (tr("Location"), sw["location"]), (tr("Rack identifier"), sw["rack"]),
             (tr("Office"), sw["designation"]), (tr("Groups"), ", ".join(groups)),
             (tr("Number of ports"), total_ports if total_ports is not None else len(ports)),
             (tr("Last updated"), sw["updated_at"]), (tr("Notes"), sw["notes"])]
    if sw["status"] != "active" and sw["status_reason"]:
        info.append((tr("Reason"), sw["status_reason"]))
    if cred is not None:
        info.append((tr("Credential profile"), cred["name"]))
        for f in cred_fields or []:
            if f["value"]:
                v = f["value"] if f["kind"] not in ("secret", "snmp") else (dec(f["value"]) if include_secrets else "********")
                info.append((tr(f["label"]), v))
    for k, v in info:
        ws.append([k, v])
        ws.cell(ws.max_row, 1).font = KEY_FONT
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 50
    links = [(p["name"], p["remote_switch"], p["remote_port"], p["ptype"], p["patch_port"]) for p in ports if p["remote_switch"]]
    links += [(p["remote_port"], p["switch_name"], p["name"], p["ptype"], p["patch_port"]) for p in incoming]
    seen, uniq = set(), []
    for l in links:
        k = (str(l[0]), l[1], str(l[2]))
        if k not in seen:
            seen.add(k)
            uniq.append(l)
    if uniq:
        _sheet(wb, tr("Links"), [tr(h) for h in ["Port", "Connected switch", "Remote port", "Type", "Patch panel port"]],
               sorted(uniq, key=lambda l: (int(re.sub(r"\D", "", str(l[0])) or 0), l[1])), {2: 20})
    if history:
        _sheet(wb, tr("History"), [tr(h) for h in ["Date", "User", "Action", "Details"]],
               [(h["ts"], h["username"], tr(actions.get(h["action"], h["action"])),
                 _fmt_details(h["details"], tr, all_labels, show)) for h in history],
               {1: 20, 2: 16, 3: 26, 4: 90})
    return _save(wb)


def export_switches(items, tr, type_colors, status_labels):
    """Several switches: an index sheet, then one sheet per switch laid out like the switch page.
    items: list of (switch_row, ports, groups, model)."""
    wb = openpyxl.Workbook()
    idx = wb.active
    idx.title = tr("Switches")[:31]
    headers = [tr(h) for h in ["Switch", "Office", "Area / location", "IP address", "Model", "Serial number",
                               "Ports (active / total)", "Groups"]]
    idx.append(headers)
    for c in idx[1]:
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
    used = {idx.title}
    for sw, ports, groups, model in items:
        title = re.sub(r"[\[\]:*?/\\]", "-", sw["name"])[:31]
        n = 2
        while title in used:
            title = f"{title[:28]}_{n}"
            n += 1
        used.add(title)
        _port_sheet(wb, title, sw, ports, groups, model, tr, type_colors, None,
                    tr(status_labels.get(sw["status"], sw["status"])))
        active = sum(1 for p in ports if p["status"] == "active")
        idx.append([sw["name"], sw["designation"], " · ".join(x for x in (sw["area"], sw["location"]) if x), sw["ip"],
                    sw["model"], sw["serial"], f"{active} / {len(ports)}", ", ".join(groups)])
        cell = idx.cell(idx.max_row, 1)
        cell.hyperlink = f"#'{title}'!A1"
        cell.font = Font(color="0F766E", underline="single", bold=True)
        for c in idx[idx.max_row]:
            c.border = BORDER
    for i, w in enumerate([18, 24, 26, 15, 22, 18, 16, 30], start=1):
        idx.column_dimensions[get_column_letter(i)].width = w
    idx.freeze_panes = "A2"
    idx.auto_filter.ref = idx.dimensions
    return _save(wb)


def export_audit(rows, tr, categories, actions, labels, show=None):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = _sheet(wb, tr("Audit log"),
                [tr(h) for h in ["Date", "User", "IP", "Category", "Action", "Object", "Details"]],
                [(r["ts"], r["username"], r["ip"], tr(categories.get(r["category"], r["category"])),
                  tr(actions.get(r["action"], r["action"])), r["target_name"],
                  _fmt_details(json.loads(r["details"] or "{}"), tr, labels, show)) for r in rows],
                {1: 20, 2: 16, 3: 15, 4: 22, 5: 26, 6: 24, 7: 90})
    for row in ws.iter_rows(min_row=2):
        row[6].alignment = Alignment(wrap_text=True, vertical="top")
    return _save(wb)


# ---------------------------------------------------------------- import template
NOTE_FILL = PatternFill("solid", fgColor="F1F5F9")
REQ_FILL = PatternFill("solid", fgColor="0F766E")


def export_template(con, tr, n_rows=500):
    """Empty workbook ready to fill and import: Instructions, Summary, Ports, Assets, VLAN and Lists sheets.
    The drop-downs (status, port type, VLAN, equipment type, connected equipment filtered by type, switches)
    come from the current settings."""
    from openpyxl.workbook.defined_name import DefinedName  # noqa: F401  (kept for older openpyxl)
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = openpyxl.Workbook()
    ins = wb.active
    ins.title = tr("Instructions")[:31]
    sum_h = ["Area", "Location", "Rack identifier", "Office", "Switch Name", "POE", "Serial", "IP Address", "Model",
             "Layer", "SFP ports", "Groups", "Ports", "In service since", "Credential Profile", "Notes", "Status check"]
    summary = _sheet(wb, tr("Summary"), [tr(h) for h in sum_h], [], {4: 20, 5: 18, 8: 15, 9: 18, 12: 22, 14: 20, 15: 30})
    ports = _sheet(wb, tr("Ports"), [tr(h) for h in PORT_IMPORT_HEADERS], [],
                   {1: 16, 2: 12, 5: 14, 7: 16, 8: 16, 9: 16, 10: 28, 11: 18, 12: 22, 14: 13, 15: 30})
    assets = _sheet(wb, tr("Assets"), [tr(h) for h in ASSET_HEADERS], [], {1: 18, 2: 30, 7: 30})
    vlan = _sheet(wb, tr("VLAN"), [tr(h) for h in ["VLAN number", "Vlan Name", "Subnet", "DHCP", "Notes"]],
                  [(r["number"], r["name"], r["subnet"], tr("Yes") if r["dhcp"] else tr("No"), r["notes"])
                   for r in con.execute("SELECT * FROM vlans ORDER BY number")], {2: 22, 3: 20})
    for ws, req in ((summary, (5,)), (ports, (1, 2)), (assets, (1, 2)), (vlan, (1,))):
        for c in req:
            ws.cell(1, c).fill = REQ_FILL
        ws.auto_filter.ref = None

    # ---- Lists (source of the drop-downs)
    lists = wb.create_sheet(tr("Lists")[:31])
    types = [dict(r) for r in con.execute("SELECT * FROM asset_types ORDER BY system DESC, sort, name COLLATE NOCASE")]
    switches = [r[0] for r in con.execute("SELECT name FROM switches WHERE status='active' ORDER BY name COLLATE NOCASE")]
    statuses = [tr("Enabled"), tr("Disabled")]
    ptypes = [r[0] for r in con.execute("SELECT name FROM port_types ORDER BY sort, name")]
    speeds = [tr(v) for v in SPEED_LABELS.values()]
    vlans = ["ALL"] + [str(r[0]) for r in con.execute("SELECT number FROM vlans ORDER BY number")]
    layers = ["L1", "L2", "L3"]
    yesno = [tr("Yes"), tr("No")]
    fixed = [(tr("Status"), statuses), (tr("Port Type"), ptypes), (tr("Speed"), speeds), (tr("VLAN"), vlans), (tr("Layer"), layers),
             (tr("POE"), yesno), (tr("Switches"), switches), (tr("Equipment Type"), [t["name"] for t in types])]
    col = 1
    ranges = {}
    for title, values in fixed:
        lists.cell(1, col, title).font = HEAD_FONT
        lists.cell(1, col).fill = HEAD_FILL
        for i, v in enumerate(values, start=2):
            lists.cell(i, col, v)
        letter = get_column_letter(col)
        ranges[title] = f"'{lists.title}'!${letter}$2:${letter}${max(2, len(values) + 1)}"
        lists.column_dimensions[letter].width = 18
        col += 1
    # one column per equipment type with its assets (switches for the Switch type) + a lookup table type -> range
    col += 1
    lookup_col = col
    lists.cell(1, lookup_col, tr("Equipment Type")).font = HEAD_FONT
    lists.cell(1, lookup_col).fill = HEAD_FILL
    lists.cell(1, lookup_col + 1, tr("Range")).font = HEAD_FONT
    lists.cell(1, lookup_col + 1).fill = HEAD_FILL
    col = lookup_col + 3
    for j, t in enumerate(types, start=2):
        if t["system"]:
            names = switches
        else:
            names = [r[0] for r in con.execute("SELECT name FROM assets WHERE type_id=? ORDER BY name COLLATE NOCASE",
                                               (t["id"],))]
        letter = get_column_letter(col)
        lists.cell(1, col, t["name"]).font = HEAD_FONT
        lists.cell(1, col).fill = PatternFill("solid", fgColor=(t["color"] or "#64748b").lstrip("#").upper())
        for i, v in enumerate(names, start=2):
            lists.cell(i, col, v)
        lists.cell(j, lookup_col, t["name"])
        lists.cell(j, lookup_col + 1, f"'{lists.title}'!${letter}$2:${letter}${max(2, len(names) + 201)}")
        lists.column_dimensions[letter].width = 22
        col += 1
    lk = get_column_letter(lookup_col)
    lk2 = get_column_letter(lookup_col + 1)
    lookup = f"'{lists.title}'!${lk}$2:${lk2}${len(types) + 1}"
    lists.column_dimensions[lk].width = 18
    lists.column_dimensions[lk2].width = 30

    def dv(ws, rng, formula, strict=True, prompt=None):
        v = DataValidation(type="list", formula1=formula, allow_blank=True)
        v.showErrorMessage = True
        v.errorStyle = "stop" if strict else "warning"
        v.error = tr("Choose a value from the list (Lists sheet).") if strict else \
            tr("This name is not in the list yet - it will be created as a new asset of the chosen type.")
        if prompt:
            v.promptTitle, v.prompt, v.showInputMessage = tr("Tip")[:32], prompt[:255], True
        ws.add_data_validation(v)
        v.add(rng)

    last = n_rows + 1
    dv(summary, f"F2:F{last}", f"{ranges[tr('POE')]}")
    dv(summary, f"J2:J{last}", f"{ranges[tr('Layer')]}")
    dv(summary, f"Q2:Q{last}", f"{ranges[tr('POE')]}")
    dv(ports, f"A2:A{last}", f"{ranges[tr('Switches')]}", strict=False,
       prompt=tr("Switch name - must also be on the Summary sheet (or already exist in SwitchDoc)."))
    dv(ports, f"C2:C{last}", f"{ranges[tr('Status')]}")
    dv(ports, f"D2:D{last}", f"{ranges[tr('Port Type')]}")
    dv(ports, f"E2:E{last}", f"{ranges[tr('Speed')]}")
    dv(ports, f"F2:F{last}", f"{ranges[tr('VLAN')]}", strict=False)
    dv(ports, f"I2:I{last}", f"{ranges[tr('Equipment Type')]}")
    dv(ports, f"K2:K{last}", f"{ranges[tr('Equipment Type')]}")
    # connected equipment: only the assets of the equipment type chosen in the column before
    dv(ports, f"J2:J{last}", f"INDIRECT(VLOOKUP($I2,{lookup},2,FALSE))", strict=False,
       prompt=tr("Choose the Equipment Type first: only the assets of that type are listed."))
    dv(ports, f"L2:L{last}", f"INDIRECT(VLOOKUP($K2,{lookup},2,FALSE))", strict=False)
    dv(assets, f"A2:A{last}", f"{ranges[tr('Equipment Type')]}")

    # ---- Instructions
    ins.column_dimensions["A"].width = 28
    ins.column_dimensions["B"].width = 100
    ins.append([tr("SwitchDoc import template")])
    ins["A1"].font = TITLE_FONT
    ins.append([tr("Fill the sheets below, save the file, then upload it in Import / Export. "
                   "Dark green column headers are required. Other columns can stay empty.")])
    ins.append([tr("Existing switches are updated (matched by Switch Name), new ones are created. "
                   "Ports are matched by switch + port number. Nothing is deleted unless you choose "
                   "'replace everything' when importing.")])
    for r in (2, 3):
        ins.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ins.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
        ins.row_dimensions[r].height = 32

    def section(title, rows):
        ins.append([])
        ins.append([title])
        ins.cell(ins.max_row, 1).font = Font(bold=True, size=13, color="0F766E")
        ins.append([tr("Column"), tr("How to fill it")])
        for c in ins[ins.max_row]:
            c.fill, c.font = HEAD_FILL, HEAD_FONT
        for k, v in rows:
            ins.append([tr(k), v])
            ins.cell(ins.max_row, 1).font = KEY_FONT
            ins.cell(ins.max_row, 2).alignment = Alignment(wrap_text=True, vertical="top")
            ins.cell(ins.max_row, 1).alignment = Alignment(vertical="top")

    section(tr("Sheet “%(s)s” - one row per switch", s=tr("Summary")), [
        ("Area", tr("Site or building. A switch group with the same name is created so access can be given per area.")),
        ("Location", tr("Floor / room - from Settings › Locations, racks & offices (new values are added).")),
        ("Rack identifier", tr("Rack where the switch is mounted, e.g. RCK1.")),
        ("Office", tr("Office / department served by the switch.")),
        ("Switch Name", tr("Required. Unique name, e.g. SWCORE1. It must not be the name of an asset.")),
        ("POE", tr("Yes or No.")),
        ("Serial", tr("Serial number.")), ("IP Address", tr("Management IP address (leave empty for a Layer 1 switch).")),
        ("Model", tr("Switch model, e.g. WS-C2960X-48FPD-L.")),
        ("Layer", tr("L1, L2 or L3 (default L3).")),
        ("SFP ports", tr("Number of SFP ports (they are the last ports of the switch).")),
        ("Groups", tr("Switch groups, separated by commas.")),
        ("Ports", tr("Number of ports: ports 1..N are created so you can document them later in SwitchDoc.")),
        ("In service since", tr("Date the switch was put in service for the first time (YYYY-MM-DD).")),
        ("Credential Profile", tr("Name of an existing credential profile (Credentials menu). Passwords are not imported from this template.")),
        ("Notes", tr("Free text.")),
        ("Status check", tr("Yes or No: ping this switch in the background to show online / offline (default Yes).")),
    ])
    section(tr("Sheet “%(s)s” - one row per port", s=tr("Ports")), [
        ("Switch", tr("Required. Switch name (drop-down).")),
        ("Port Number", tr("Required. Port name, e.g. 1, Gi1/0/1 - the number at the end is the port position.")),
        ("Status", tr("%(a)s or %(b)s. A disabled port should have no patch panel, equipment or user.", a=tr("Enabled"), b=tr("Disabled"))),
        ("Port Type", tr("From Settings › Port types: %(list)s.", list=", ".join(ptypes))),
        ("Speed", tr("%(list)s (empty = %(d)s).", list=", ".join(speeds), d=tr("Gigabit"))),
        ("VLAN", tr("A VLAN number from Settings › VLANs, or ALL for a trunk.")),
        ("Patch Panel Port", tr("e.g. P1-12.")), ("Patch Panel Label", tr("Label written on the wall socket.")),
        ("Equipment Type", tr("What is plugged in: %(list)s. Choose %(sw)s for a link to another switch.",
                              list=", ".join(t["name"] for t in types if not t["system"]), sw=assetlib.SWITCH_TYPE)),
        ("Connected Equipment", tr("Asset name, from the list of the chosen Equipment Type (asset names are unique). "
                                   "A new name is created as a new asset of that type. For type %(sw)s: the other switch.",
                                   sw=assetlib.SWITCH_TYPE)),
        ("Device Behind Type", tr("Optional: type of a device plugged behind the equipment, e.g. PC behind a Phone.")),
        ("Device Behind", tr("Optional: name of that device, e.g. PC0107 for “1114 → PC0107”.")),
        ("User", tr("Person using the equipment.")),
        ("Remote Port", tr("Port on the other switch (type %(sw)s) or on the firewall / router. "
                           "Both switches are linked automatically.", sw=assetlib.SWITCH_TYPE)),
        ("Description", tr("Free text.")),
    ])
    section(tr("Sheet “%(s)s” - optional, one row per asset", s=tr("Assets")), [
        ("Equipment Type", tr("Required. Type of the asset (drop-down). Switches are not listed here: they are on the Summary sheet.")),
        ("Asset Name", tr("Required. Unique name (PC name, phone extension, printer name…).")),
        ("IP Address", tr("Optional.")), ("MAC Address", tr("Optional.")), ("Serial", tr("Optional.")),
        ("Location", tr("Optional.")), ("Notes", tr("Optional.")),
    ])
    section(tr("Sheet “%(s)s” - optional", s=tr("VLAN")), [
        ("VLAN number", tr("Required. Already contains your current VLANs - add rows for new ones.")),
        ("Vlan Name", tr("Name.")), ("Subnet", tr("Subnet / gateway.")), ("DHCP", tr("Yes or No.")),
    ])
    # example
    ins.append([])
    ins.append([tr("Example (Ports sheet)")])
    ins.cell(ins.max_row, 1).font = Font(bold=True, size=13, color="0F766E")
    ex_head = ins.max_row + 1
    ins.append([tr(h) for h in PORT_IMPORT_HEADERS])
    for c in ins[ex_head]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
    pc = next((t["name"] for t in types if t["kind"] == "pc"), "PC")
    ph = next((t["name"] for t in types if t["kind"] == "phone"), "Phone")
    pr = next((t["name"] for t in types if t["kind"] == "printer"), "Printer")
    gb, fb = tr("Gigabit"), tr("Fiber")
    for row in (["SWEXAMPLE1", "1", tr("Enabled"), "Access", gb, "100", "P1-01", "1-01", ph, "1114", pc, "PC0107", "John", "", ""],
                ["SWEXAMPLE1", "2", tr("Enabled"), "Access", gb, "100", "P1-02", "1-02", pr, "PRINTER IT", "", "", "", "", ""],
                ["SWEXAMPLE1", "3", tr("Disabled"), "Access", gb, "", "", "", "", "", "", "", "", "", ""],
                ["SWEXAMPLE1", "48", tr("Enabled"), "Trunk", fb, "ALL", "", "", assetlib.SWITCH_TYPE, "SWEXAMPLE2", "", "", "", "24",
                 tr("Uplink")]):
        ins.append(row)
        for c in ins[ins.max_row]:
            c.fill, c.border = NOTE_FILL, BORDER
    ins.append([tr("Rows of this example are not imported: the Instructions sheet is ignored.")])
    ins.cell(ins.max_row, 1).font = Font(italic=True, color="64748B")
    wb.move_sheet(lists, offset=0)
    return _save(wb)


def export_tickets(items, tr, users, statuses, kinds, field_labels):
    """Maintenance tickets: items = [(ticket, [assignee names], switch rows, change request rows)]"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _sheet(wb, tr("Tickets"), [tr(h) for h in ["Ticket", "Title", "Type of work", "Status", "Planned start", "Planned end",
                                               "Actual start", "Actual end", "Assigned technicians", "Switches",
                                               "Created", "Validated", "Technician comment", "Supervisor comment",
                                               "Archived"]],
           [(t["id"], t["title"], tr(kinds.get(t["kind"], t["kind"])), tr(statuses.get(t["status"], t["status"])),
             t["planned_start"], t["planned_end"], t["actual_start"], t["actual_end"], ", ".join(a), len(sws),
             f"{t['created_at']} · {users.get(t['created_by'], '')}",
             f"{t['validated_at'] or ''} · {users.get(t['validated_by'], '')}" if t["validated_by"] else "",
             t["tech_comment"], t["validation_comment"], tr("Yes") if t["archived"] else "")
            for t, a, sws, _c in items],
           {2: 34, 3: 22, 5: 17, 6: 17, 7: 17, 8: 17, 9: 30, 11: 26, 12: 26, 13: 40, 14: 40})
    _sheet(wb, tr("Switches"), [tr(h) for h in ["Ticket", "Switch", "Verified", "Verified by", "Date", "Note"]],
           [(t["id"], s["name"], tr("Yes") if s["done"] else tr("No"), users.get(s["done_by"], ""), s["done_at"] or "",
             s["note"]) for t, _a, sws, _c in items for s in sws], {2: 18, 4: 22, 5: 17, 6: 40})
    rows = []
    for t, _a, _s, chg in items:
        for c in chg:
            new, old = json.loads(c["fields"] or "{}"), json.loads(c["old"] or "{}")
            rows.append((t["id"], c["sw"], c["port"] or "", "; ".join(
                f"{tr(field_labels.get(k, k))}: {old.get(k) or '—'} → {v or '—'}" for k, v in new.items() if k != "remote_switch"),
                users.get(c["requested_by"], ""), c["requested_at"], c["status"]))
    _sheet(wb, tr("Changes"), [tr(h) for h in ["Ticket", "Switch", "Port", "Changes", "By", "Date", "Status"]], rows,
           {2: 18, 4: 80, 5: 22, 6: 17})
    return _save(wb)


def export_switch_list(rows, tr, columns):
    """One sheet: one line per switch (no ports)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = _sheet(wb, tr("Switches"), [tr(label) for _k, label in columns],
                [tuple((tr("Yes") if r[k] else tr("No")) if k == "poe" else (r[k] if r[k] is not None else "")
                       for k, _l in columns) for r in rows],
                {1: 22, 2: 26, 3: 20, 4: 16, 5: 22, 6: 16, 7: 14, 8: 8, 9: 16})
    ws.print_title_rows = "1:1"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    for c in ws[1]:
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.border = BORDER
    return _save(wb)
