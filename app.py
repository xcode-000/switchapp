"""SwitchDoc - network switch documentation tool.

Run:  python app.py               (development server on 0.0.0.0:8080)
      python app.py import FILE [--company NAME]  (import an Excel workbook into a company)
      python app.py reset-password USERNAME
      python app.py i18n-check    (list texts missing from the translation files)
      python app.py ip-access off|on|show   (network access restriction - recovery if locked out)
Production: waitress-serve --listen=0.0.0.0:8080 app:app
"""
import datetime
import difflib
import functools
import json
import os
import secrets
import sqlite3
import sys
import threading

from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, abort, flash, g, has_request_context, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import assets as assetlib
import ipaccess
import i18n
import ldapauth
from i18n import LANGUAGES, N_

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SWITCHDOC_DATA", os.path.join(BASE, "data"))
os.makedirs(DATA, exist_ok=True)
DB_PATH = os.path.join(DATA, "switchdoc.db")


def _load_key(filename, env):
    val = os.environ.get(env)
    if val:
        return val.encode()
    path = os.path.join(DATA, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    with open(path, "rb") as f:
        return f.read().strip()


FERNET = Fernet(_load_key("secret.key", "SWITCHDOC_KEY"))

app = Flask(__name__)
app.secret_key = _load_key("session.key", "SWITCHDOC_SESSION_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)


# ---------------------------------------------------------------- language
def lang():
    if not has_request_context():
        return "en"
    if "lang" not in g:
        u = g.get("user")
        code = (u["lang"] if u is not None and "lang" in u.keys() else "") or session.get("lang") \
            or i18n.best_match(request.accept_languages)
        g.lang = code if code in LANGUAGES else "en"
    return g.lang


def _(_text, **kw):
    return i18n.translate(lang(), _text, **kw)


# ---------------------------------------------------------------- permissions model
LEVELS = {"none": 0, "view": 1, "edit": 2, "modify": 3}
LEVEL_LABELS = [("none", N_("No access")), ("view", N_("View")), ("edit", N_("Edit")), ("modify", N_("Modify"))]
MENUS = [
    ("switches", N_("Switches"), N_("Switch inventory and Settings › Switches. Edit = add / change switches, "
                                    "Modify = also disable, re-enable, replace and delete.")),
    ("ports", N_("Ports"), N_("Port status, type, VLAN and connection details. Edit = changes are sent for approval, "
                              "Modify = changes apply directly and can approve others' changes.")),
    ("credentials", N_("Credentials"), N_("View = see the credential profiles and usernames, Edit = create / change "
                                          "profiles, Modify = also delete. Passwords need the Passwords line below.")),
    ("passwords", N_("Passwords"), N_("View = show and copy the passwords (switch page, Credentials) and export them "
                                      "to Excel. No access = passwords always stay hidden.")),
    ("topology", N_("Topology"), N_("View = see the switch interconnection diagram, Edit / Modify = also save the diagram layout.")),
    ("maintenance", N_("Maintenance"), N_("Maintenance / verification tickets. View = see tickets, Edit = technician "
                                          "(work on the tickets assigned to you), Modify = supervisor (create, assign, "
                                          "validate the work of others).")),
    ("snmp", N_("SNMP (live)"), N_("Read live information from a switch over SNMP with its read-only community "
                                  "(nothing is written to the switch, nothing is stored). View = can query switches.")),
    ("import_export", N_("Import / Export"), N_("View = export to Excel, Edit = import, Modify = import in 'replace everything' mode.")),
    ("groups", N_("Settings › Groups"), N_("Switch groups. Edit = create / change, Modify = also delete.")),
    ("vlans", N_("Settings › VLANs"), N_("Edit = create / change, Modify = also delete.")),
    ("port_types", N_("Settings › Port types"), N_("Edit = create / change, Modify = also delete.")),
    ("models", N_("Settings › Switch models"), N_("Edit = create / change, Modify = also delete.")),
    ("assets", N_("Assets"), N_("Asset types and assets (PCs, phones, printers…) shown in the Connected equipment "
                              "drop-down. View = see, Edit = create / change, Modify = also delete.")),
    ("places", N_("Settings › Locations, racks & offices"), N_("Edit = create / change, Modify = also delete.")),
    ("ip_access", N_("Settings › Security"), N_("Session timeout and which IP addresses, subnets or ranges may open "
                                                "SwitchDoc. View = see, Edit / Modify = change them.")),
    ("users", N_("Settings › Users"), N_("Edit = create / change users, Modify = also delete.")),
    ("profiles", N_("Settings › Authorisation profiles"), N_("Edit = create / change, Modify = also delete.")),
    ("audit", N_("Settings › Audit log"), N_("View = read the audit log, Edit = also export it to Excel, Modify = also purge old entries.")),
]
MENU_KEYS = [m[0] for m in MENUS]
SETTINGS_MENUS = ["switches", "assets", "places", "ip_access", "groups", "vlans", "port_types", "models", "users", "profiles", "audit"]

STATUSES = [("active", N_("Active")), ("disabled", N_("Disabled"))]
SWITCH_STATUSES = [("active", N_("Active")), ("disabled", N_("Disabled")), ("replaced", N_("Replaced"))]
SPEEDS = [("fast", N_("Fast Ethernet")), ("gigabit", N_("Gigabit")), ("fiber", N_("Fiber"))]
DEFAULT_SPEED = "gigabit"
PORT_FIELDS = ["name", "status", "ptype", "speed", "vlan", "patch_port", "patch_label", "eq_type", "equipment",
               "eq2_type", "equipment2", "user", "remote_switch", "remote_port", "description"]
# fields emptied automatically when a port is set to Disabled
CLEARED_WHEN_DISABLED = ["vlan", "patch_port", "patch_label", "eq_type", "equipment", "eq2_type", "equipment2", "user",
                         "remote_switch", "remote_port", "description"]
SWITCH_TYPE = assetlib.SWITCH_TYPE  # built-in asset type: its assets are the documented switches
PORT_FIELD_LABELS = {"name": N_("Port name"), "status": N_("Status"), "ptype": N_("Type"), "speed": N_("Speed"),
                     "vlan": N_("VLAN"),
                     "patch_port": N_("Patch panel port"), "patch_label": N_("Patch panel label"),
                     "equipment": N_("Connected equipment"), "eq_type": N_("Equipment type"),
                     "eq2_type": N_("Type of the device behind"), "equipment2": N_("Device behind (e.g. PC behind a phone)"),
                     "user": N_("User"), "remote_switch": N_("Remote switch"),
                     "remote_port": N_("Remote port"), "description": N_("Description")}
SWITCH_FIELD_LABELS = {"name": N_("Switch name"), "ip": N_("IP address"), "serial": N_("Serial number"),
                       "model": N_("Model"), "area": N_("Area"), "location": N_("Location"),
                       "rack": N_("Rack identifier"), "designation": N_("Office"), "poe": N_("PoE"),
                       "layer": N_("Layer"), "sfp": N_("SFP ports"), "sfp_count": N_("Number of SFP ports"),
                       "port_prefix": N_("Port name prefix"), "cred_profile_id": N_("Credential profile"),
                       "notes": N_("Notes"), "ports": N_("Number of ports"), "groups": N_("Groups"),
                       "status": N_("Status"), "in_service": N_("In service since"),
                       "monitor": N_("Status check")}
SECRET_FIELDS = ["ssh_pass", "telnet_pass", "console_pass", "enable_pass", "web_pass", "snmp_community"]
CRED_FIELDS = ["ssh_user", "telnet_user", "web_user"] + SECRET_FIELDS
CRED_LABELS = {
    "ssh_user": N_("SSH username"), "ssh_pass": N_("SSH password"),
    "telnet_user": N_("Telnet username"), "telnet_pass": N_("Telnet password"),
    "console_pass": N_("Console password"), "enable_pass": N_("Enable / config terminal password"),
    "web_user": N_("Web username"), "web_pass": N_("Web password"),
    "snmp_community": N_("SNMP community"),
}
# credential profiles have any number of fields: label + kind + value (secrets are encrypted)
CRED_KINDS = [("user", N_("Username")), ("secret", N_("Password / secret")), ("snmp", N_("SNMP community")),
              ("text", N_("Other information"))]
SECRET_KINDS = ("secret", "snmp")
CRED_TEMPLATES = [  # name, [(label, kind)] - offered when building a profile
    ("Cisco IOS", [(N_("Console password"), "secret"), (N_("Enable password"), "secret"),
                   (N_("Configuration terminal password"), "secret"), (N_("SSH username"), "user"),
                   (N_("SSH password"), "secret"), (N_("Web username"), "user"), (N_("Web password"), "secret"),
                   (N_("SNMP community"), "snmp")]),
    ("D-Link / TP-Link (web smart)", [(N_("Web username"), "user"), (N_("Web password"), "secret"),
                                      (N_("SNMP community"), "snmp")]),
    ("HPE / Aruba", [(N_("Manager username"), "user"), (N_("Manager password"), "secret"),
                     (N_("Operator username"), "user"), (N_("Operator password"), "secret"), (N_("SNMP community"), "snmp")]),
    ("MikroTik", [(N_("Winbox / SSH username"), "user"), (N_("Winbox / SSH password"), "secret"),
                  (N_("SNMP community"), "snmp")]),
    ("FortiSwitch", [(N_("Admin username"), "user"), (N_("Admin password"), "secret"), (N_("SNMP community"), "snmp")]),
    (N_("Generic"), [(N_("Username"), "user"), (N_("Password"), "secret")]),
]
OLD_CRED_KIND = {"ssh_user": "user", "telnet_user": "user", "web_user": "user", "snmp_community": "snmp"}


def migrate_legacy_creds(con):
    """Fixed credential columns (older versions, or a profile just created by the Excel import) -> dynamic fields."""
    cols = ["console_pass", "enable_pass", "ssh_user", "ssh_pass", "telnet_user", "telnet_pass", "web_user", "web_pass",
            "snmp_community"]
    for prof in con.execute(f"""SELECT * FROM cred_profiles p WHERE NOT EXISTS (SELECT 1 FROM cred_fields f WHERE
                                f.profile_id=p.id) AND ({' OR '.join(f"{c}<>''" for c in cols)})""").fetchall():
        for j, k in enumerate(cols):
            if prof[k]:
                con.execute("INSERT INTO cred_fields(profile_id, label, kind, value, sort) VALUES (?,?,?,?,?)",
                            (prof["id"], CRED_LABELS[k], OLD_CRED_KIND.get(k, "secret"), prof[k], j))
        con.execute(f"UPDATE cred_profiles SET {', '.join(f'{c}=' + chr(39) * 2 for c in cols)} WHERE id=?", (prof["id"],))


def cred_fields_of(con, pid):
    return con.execute("SELECT * FROM cred_fields WHERE profile_id=? ORDER BY sort, id", (pid,)).fetchall()


def snmp_community_of(con, pid):
    row = con.execute("SELECT value FROM cred_fields WHERE profile_id=? AND kind='snmp' AND value<>'' ORDER BY sort, id LIMIT 1",
                      (pid,)).fetchone() if pid else None
    return dec(row[0]) if row else ""


LAYERS = [("L1", N_("Layer 1 (unmanaged / hub)")), ("L2", N_("Layer 2 (managed)")), ("L3", N_("Layer 3 (routing)"))]
DEFAULT_PORT_TYPES = [("Access", "#22c55e"), ("Trunk", "#3b82f6"), ("Hybrid", "#f59e0b"), ("Native", "#a855f7"),
                      ("SFP / Fiber", "#06b6d4"), ("Uplink", "#ec4899"), ("Unused", "#64748b")]
GROUP_COLORS = ["#0f766e", "#2563eb", "#9333ea", "#db2777", "#ea580c", "#65a30d", "#0891b2", "#ca8a04",
                "#dc2626", "#4f46e5", "#059669", "#7c3aed"]

AUDIT_CATEGORIES = [("auth", N_("Sign-in")), ("access", N_("Access to sensitive data")),
                    ("security", N_("Authorisations")), ("documentation", N_("Documentation")),
                    ("settings", N_("Settings"))]
AUDIT_ACTIONS = {
    "setup": N_("First administrator created"), "login": N_("Signed in"), "login_failed": N_("Failed sign-in"),
    "logout": N_("Signed out"), "password_change": N_("Changed own password"), "language": N_("Changed language"),
    "reveal_credentials": N_("Viewed passwords"), "export_workbook": N_("Exported workbook"),
    "export_switch": N_("Exported switch"), "export_audit": N_("Exported audit log"),
    "user_create": N_("User created"), "user_update": N_("User changed"), "user_delete": N_("User deleted"),
    "profile_create": N_("Profile created"), "profile_update": N_("Profile changed"), "profile_delete": N_("Profile deleted"),
    "group_create": N_("Group created"), "group_update": N_("Group changed"), "group_delete": N_("Group deleted"),
    "switch_create": N_("Switch created"), "switch_update": N_("Switch changed"), "switch_delete": N_("Switch deleted"),
    "switch_disable": N_("Switch disabled"), "switch_enable": N_("Switch re-enabled"), "switch_replace": N_("Switch replaced"),
    "port_update": N_("Port changed"), "port_request": N_("Port change requested"),
    "request_approve": N_("Change approved"), "request_reject": N_("Change rejected"), "request_cancel": N_("Change cancelled"),
    "cred_create": N_("Credential profile created"), "cred_update": N_("Credential profile changed"),
    "cred_delete": N_("Credential profile deleted"), "import": N_("Excel import"), "topology_layout": N_("Topology layout saved"),
    "vlan_create": N_("VLAN created"), "vlan_update": N_("VLAN changed"), "vlan_delete": N_("VLAN deleted"),
    "port_type_create": N_("Port type created"), "port_type_update": N_("Port type changed"),
    "port_type_delete": N_("Port type deleted"), "model_create": N_("Model created"),
    "model_update": N_("Model changed"), "model_delete": N_("Model deleted"),
    "audit_purge": N_("Audit log purged"), "export_switches": N_("Exported switches"),
    "place_create": N_("Location / rack / office created"), "place_update": N_("Location / rack / office changed"),
    "place_delete": N_("Location / rack / office deleted"),
    "asset_type_create": N_("Asset type created"), "asset_type_update": N_("Asset type changed"),
    "asset_type_delete": N_("Asset type deleted"), "asset_create": N_("Asset created"),
    "asset_update": N_("Asset changed"), "asset_delete": N_("Asset deleted"),
    "assets_converted": N_("Connected equipment converted to assets"), "asset_move": N_("Asset moved"),
    "ticket_delete": N_("Maintenance ticket deleted"),
    "ticket_create": N_("Maintenance ticket created"), "ticket_update": N_("Maintenance ticket changed"),
    "ticket_start": N_("Maintenance started"), "ticket_switch_done": N_("Switch verified (maintenance)"),
    "ticket_submit": N_("Maintenance submitted for validation"), "ticket_validate": N_("Maintenance validated"),
    "ticket_reject": N_("Maintenance sent back"), "ticket_cancel": N_("Maintenance ticket cancelled"),
    "tickets_archive": N_("Maintenance tickets archived"), "tickets_delete": N_("Maintenance tickets deleted"),
    "export_tickets": N_("Exported maintenance tickets"), "export_switch_list": N_("Exported the switch list"),
    "snmp_query": N_("SNMP query"), "monitoring_settings": N_("Monitoring settings changed"),
    "monitor_mail_failed": N_("Status e-mail could not be sent"), "print_switch": N_("Printed switch layout"), "session_timeout": N_("Signed out (no activity)"),
    "security_settings": N_("Security settings changed"), "ip_access_update": N_("Network access rules changed"),
    "ip_access_denied": N_("Access refused (IP address not allowed)"),
}
# asset type -> shape used in the topology
ASSET_KINDS = [("pc", N_("PC / workstation")), ("phone", N_("Phone")), ("printer", N_("Printer / copier")),
               ("barcode", N_("Barcode / label printer")), ("ap", N_("WiFi access point")), ("camera", N_("Camera")),
               ("server", N_("Server / NAS")), ("firewall", N_("Firewall")), ("router", N_("Router / internet link")),
               ("hub", N_("Unmanaged switch / hub")), ("switch", N_("Switch")), ("other", N_("Other device"))]
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS auth_profiles(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', perms TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sw_groups(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', color TEXT DEFAULT '#0f766e');
CREATE TABLE IF NOT EXISTS user_groups(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES sw_groups(id) ON DELETE CASCADE, PRIMARY KEY(user_id, group_id));
CREATE TABLE IF NOT EXISTS cred_profiles(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, notes TEXT DEFAULT '',
  ssh_user TEXT DEFAULT '', ssh_pass TEXT DEFAULT '',
  telnet_user TEXT DEFAULT '', telnet_pass TEXT DEFAULT '',
  console_pass TEXT DEFAULT '', enable_pass TEXT DEFAULT '',
  web_user TEXT DEFAULT '', web_pass TEXT DEFAULT '', snmp_community TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS vlans(
  id INTEGER PRIMARY KEY, number INTEGER UNIQUE NOT NULL, name TEXT DEFAULT '',
  subnet TEXT DEFAULT '', dhcp INTEGER DEFAULT 0, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS port_types(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, color TEXT DEFAULT '#64748b',
  description TEXT DEFAULT '', sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS models(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, vendor TEXT DEFAULT '', layer TEXT DEFAULT 'L2',
  poe INTEGER DEFAULT 0, sfp INTEGER DEFAULT 0, sfp_count INTEGER DEFAULT 0, port_count INTEGER DEFAULT 0,
  notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS switches(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, ip TEXT DEFAULT '', serial TEXT DEFAULT '',
  model TEXT DEFAULT '', area TEXT DEFAULT '', location TEXT DEFAULT '', rack TEXT DEFAULT '',
  designation TEXT DEFAULT '', poe INTEGER DEFAULT 0, port_prefix TEXT DEFAULT '',
  cred_profile_id INTEGER REFERENCES cred_profiles(id) ON DELETE SET NULL,
  notes TEXT DEFAULT '', updated_at TEXT);
CREATE TABLE IF NOT EXISTS switch_groups(
  switch_id INTEGER NOT NULL REFERENCES switches(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES sw_groups(id) ON DELETE CASCADE, PRIMARY KEY(switch_id, group_id));
CREATE TABLE IF NOT EXISTS ports(
  id INTEGER PRIMARY KEY, switch_id INTEGER NOT NULL REFERENCES switches(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL, name TEXT NOT NULL, status TEXT DEFAULT 'active', ptype TEXT DEFAULT 'Access',
  vlan TEXT DEFAULT '', patch_port TEXT DEFAULT '', patch_label TEXT DEFAULT '',
  equipment TEXT DEFAULT '', user TEXT DEFAULT '', remote_switch TEXT DEFAULT '',
  remote_port TEXT DEFAULT '', description TEXT DEFAULT '', UNIQUE(switch_id, idx));
CREATE INDEX IF NOT EXISTS ix_ports_switch ON ports(switch_id);
CREATE TABLE IF NOT EXISTS change_requests(
  id INTEGER PRIMARY KEY, port_id INTEGER REFERENCES ports(id) ON DELETE CASCADE,
  switch_id INTEGER REFERENCES switches(id) ON DELETE CASCADE,
  requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL, requested_at TEXT,
  fields TEXT NOT NULL, old TEXT NOT NULL, status TEXT DEFAULT 'pending',
  reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL, reviewed_at TEXT, comment TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_cr_status ON change_requests(status);
CREATE TABLE IF NOT EXISTS topo_layout(node TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, username TEXT DEFAULT '', ip TEXT DEFAULT '',
  category TEXT NOT NULL, action TEXT NOT NULL, target_type TEXT DEFAULT '', target_id INTEGER,
  target_name TEXT DEFAULT '', details TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit(target_type, target_id);
CREATE TABLE IF NOT EXISTS locations(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, area TEXT DEFAULT '', description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS racks(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, location TEXT DEFAULT '', description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS asset_types(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, kind TEXT DEFAULT 'other',
  color TEXT DEFAULT '#64748b', sort INTEGER DEFAULT 0, system INTEGER DEFAULT 0, description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL REFERENCES asset_types(id),
  name TEXT UNIQUE NOT NULL COLLATE NOCASE, ip TEXT DEFAULT '', mac TEXT DEFAULT '', serial TEXT DEFAULT '',
  location TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_assets_type ON assets(type_id);
CREATE TABLE IF NOT EXISTS cred_fields(
  id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES cred_profiles(id) ON DELETE CASCADE,
  label TEXT NOT NULL, kind TEXT DEFAULT 'secret', value TEXT DEFAULT '', sort INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_cred_fields ON cred_fields(profile_id);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT DEFAULT 'maintenance', description TEXT DEFAULT '',
  status TEXT DEFAULT 'open', planned_start TEXT DEFAULT '', planned_end TEXT DEFAULT '', actual_start TEXT DEFAULT '',
  actual_end TEXT DEFAULT '', scope TEXT DEFAULT '', created_by INTEGER, created_at TEXT, submitted_by INTEGER,
  tech_comment TEXT DEFAULT '', validated_by INTEGER, validated_at TEXT, validation_comment TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ticket_assignees(
  ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, seen INTEGER DEFAULT 0, PRIMARY KEY(ticket_id, user_id));
CREATE TABLE IF NOT EXISTS ticket_switches(
  ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  switch_id INTEGER NOT NULL REFERENCES switches(id) ON DELETE CASCADE, done INTEGER DEFAULT 0,
  done_by INTEGER, done_at TEXT, note TEXT DEFAULT '', PRIMARY KEY(ticket_id, switch_id));
CREATE INDEX IF NOT EXISTS ix_ticket_status ON tickets(status);
CREATE TABLE IF NOT EXISTS ip_rules(
  id INTEGER PRIMARY KEY, rule TEXT NOT NULL, description TEXT DEFAULT '', created_at TEXT, created_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS switch_status(
  switch_id INTEGER PRIMARY KEY REFERENCES switches(id) ON DELETE CASCADE, state TEXT DEFAULT 'unknown', since TEXT,
  last_check TEXT, rtt REAL, loss INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, uptime INTEGER, rebooted_at TEXT,
  detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS status_events(
  id INTEGER PRIMARY KEY, switch_id INTEGER REFERENCES switches(id) ON DELETE CASCADE, ts TEXT NOT NULL,
  state TEXT NOT NULL, prev TEXT DEFAULT '', detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_status_events ON status_events(switch_id, ts);
CREATE TABLE IF NOT EXISTS company_settings(key TEXT PRIMARY KEY, value TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS offices(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, location TEXT DEFAULT '', description TEXT DEFAULT '');
"""

ADDED_COLUMNS = {
    "users": {
        "first_name": "TEXT DEFAULT ''", "last_name": "TEXT DEFAULT ''", "email": "TEXT DEFAULT ''",
        "title": "TEXT DEFAULT ''", "profile_id": "INTEGER REFERENCES auth_profiles(id) ON DELETE SET NULL",
        "is_admin": "INTEGER DEFAULT 0", "all_groups": "INTEGER DEFAULT 0", "active": "INTEGER DEFAULT 1",
        "last_login": "TEXT", "lang": "TEXT DEFAULT ''", "must_change": "INTEGER DEFAULT 0", "pw_changed_at": "TEXT",
        "auth_source": "TEXT DEFAULT 'local'", "directory_id": "INTEGER", "ext_id": "TEXT DEFAULT ''",
        "last_sync": "TEXT DEFAULT ''",
    },
    "switches": {
        "status": "TEXT DEFAULT 'active'", "replaced_by": "INTEGER", "status_changed_at": "TEXT",
        "status_reason": "TEXT DEFAULT ''", "layer": "TEXT DEFAULT 'L3'", "sfp": "INTEGER DEFAULT 0",
        "sfp_count": "INTEGER DEFAULT 0", "in_service": "TEXT DEFAULT ''",
        "monitor": "INTEGER DEFAULT 1",
    },
    "ports": {
        "eq_type": "TEXT DEFAULT ''", "eq2_type": "TEXT DEFAULT ''", "equipment2": "TEXT DEFAULT ''",
        "speed": "TEXT DEFAULT 'gigabit'",
    },
    "asset_types": {"multi": "INTEGER DEFAULT 0", "icon": "TEXT DEFAULT ''"},
    "change_requests": {"ticket_id": "INTEGER"},
    "tickets": {"archived": "INTEGER DEFAULT 0", "closed_at": "TEXT DEFAULT ''"},
    "assets": {"state": "TEXT DEFAULT 'stock'"},
}
# icon library shipped in static/icons (index.json: file name -> label)
try:
    with open(os.path.join(BASE, "static", "icons", "index.json"), encoding="utf-8") as _f:
        ICONS = json.load(_f)
except OSError:
    ICONS = {"other": "Other device"}
KIND_ICONS = {"pc": "pc", "phone": "phone", "printer": "printer", "barcode": "barcode", "ap": "ap", "camera": "camera",
              "server": "server", "firewall": "firewall", "router": "router", "hub": "hub", "switch": "switch",
              "other": "other"}
ASSET_STATES = [("in_use", N_("In use")), ("stock", N_("In stock")), ("defective", N_("Defective"))]


# ---------------------------------------------------------------- database
# Several companies: data/core.db holds what is shared (users, authorisation profiles, companies and the role of each
# user in each company, directories, security settings); every company has its own database file with its whole
# documentation. A request works on the current company's file with core.db attached as "core"; a temporary view
# "users" shows each user with his role (profile, all switches) in that company, so the rest of the code is unchanged.
CORE_PATH = os.path.join(DATA, "core.db")
CORE_TABLES = ("users", "auth_profiles", "ip_rules", "app_settings")
USER_FK_TABLES = ("user_groups", "ticket_assignees", "change_requests")
FIRST_COMPANY = os.environ.get("SWITCHDOC_FIRST_COMPANY", "My company")
CORE_EXTRA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, code TEXT DEFAULT '', dbfile TEXT UNIQUE NOT NULL,
  active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS memberships(
  user_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  profile_id INTEGER REFERENCES auth_profiles(id) ON DELETE SET NULL, all_groups INTEGER DEFAULT 0,
  source TEXT DEFAULT 'manual', PRIMARY KEY(user_id, company_id));
CREATE TABLE IF NOT EXISTS directories(
  id INTEGER PRIMARY KEY, company_id INTEGER UNIQUE NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  enabled INTEGER DEFAULT 0, label TEXT DEFAULT '', servers TEXT DEFAULT '', port INTEGER DEFAULT 0,
  security TEXT DEFAULT 'ldaps', verify_cert INTEGER DEFAULT 0, base_dn TEXT DEFAULT '', bind_user TEXT DEFAULT '',
  bind_pass TEXT DEFAULT '', upn_suffix TEXT DEFAULT '', netbios TEXT DEFAULT '', user_filter TEXT DEFAULT '',
  nested INTEGER DEFAULT 1, default_profile_id INTEGER, timeout INTEGER DEFAULT 5, updated_at TEXT);
CREATE TABLE IF NOT EXISTS dir_groups(
  id INTEGER PRIMARY KEY, directory_id INTEGER NOT NULL REFERENCES directories(id) ON DELETE CASCADE,
  ad_group TEXT NOT NULL, profile_id INTEGER NOT NULL REFERENCES auth_profiles(id) ON DELETE CASCADE,
  all_groups INTEGER DEFAULT 0, sw_groups TEXT DEFAULT '', sort INTEGER DEFAULT 0);
"""


def _split_schema():
    import re as _r
    core, comp = [], []
    for stmt in SCHEMA.split(";"):
        st = stmt.strip()
        if not st:
            continue
        m = _r.search(r"(?:TABLE IF NOT EXISTS|ON)\s+(\w+)", st)
        if m is None:  # PRAGMA
            continue
        name = m.group(1)
        if name in CORE_TABLES:  # in core.db the users table is called accounts (users = per-company view)
            core.append(st.replace("TABLE IF NOT EXISTS users(", "TABLE IF NOT EXISTS accounts(", 1)
                        .replace("REFERENCES users(id)", "REFERENCES accounts(id)"))
        else:
            comp.append(_r.sub(r"\s*REFERENCES users\(id\)(?: ON DELETE (?:CASCADE|SET NULL))?", "", st))
            if name == "audit":  # the core keeps its own audit log (sign-ins, users, security, companies)
                core.append(st)
    return ";\n".join(core) + ";\n" + CORE_EXTRA, ";\n".join(comp) + ";\n"


class Conn(sqlite3.Connection):
    cid = None


_COMPANIES = {"rows": None}


def companies_all(reload=False):
    """Every company (cached; reloaded after a change)."""
    if reload or _COMPANIES["rows"] is None:
        c = sqlite3.connect(CORE_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            _COMPANIES["rows"] = [dict(r) for r in c.execute("SELECT * FROM companies ORDER BY sort, name COLLATE NOCASE")]
        except sqlite3.Error:
            _COMPANIES["rows"] = []
        c.close()
    return _COMPANIES["rows"]


def company_by_id(cid):
    for c in companies_all():
        if c["id"] == cid:
            return c
    return None


def _user_view(con, cid):
    """TEMP view users = core.users + the role of each user in company cid (writes go back to core)."""
    info = con.execute("PRAGMA core.table_info(accounts)").fetchall()
    base = [r[1] for r in info if r[1] not in ("profile_id", "all_groups")]
    dflt = {r[1]: r[4] for r in info}
    cid = int(cid or 0)
    sel = ", ".join(f"u.{c}" for c in base)
    con.execute(f"""CREATE TEMP VIEW users AS SELECT {sel}, m.profile_id AS profile_id,
                    COALESCE(m.all_groups, 0) AS all_groups, m.source AS role_source
                    FROM core.accounts u LEFT JOIN core.memberships m ON m.user_id=u.id AND m.company_id={cid}""")
    vals = ", ".join("NEW.id" if c == "id" else
                     (f"COALESCE(NEW.{c}, {dflt[c]})" if dflt[c] is not None else f"NEW.{c}") for c in base)
    con.execute(f"""CREATE TEMP TRIGGER users_ins INSTEAD OF INSERT ON users BEGIN
                    INSERT INTO accounts({', '.join(base)}) VALUES ({vals});
                    INSERT OR REPLACE INTO memberships(user_id, company_id, profile_id, all_groups, source)
                      SELECT id, {cid}, NEW.profile_id, COALESCE(NEW.all_groups, 0), 'manual' FROM accounts
                      WHERE username=NEW.username AND {cid}>0 AND (NEW.profile_id IS NOT NULL OR NEW.all_groups=1);
                    END""")
    sets = ", ".join(f"{c}=NEW.{c}" for c in base if c != "id")
    con.execute(f"CREATE TEMP TRIGGER users_upd INSTEAD OF UPDATE ON users BEGIN "
                f"UPDATE accounts SET {sets} WHERE id=OLD.id; END")
    con.execute(f"""CREATE TEMP TRIGGER users_role INSTEAD OF UPDATE ON users
                    WHEN {cid}>0 AND (NEW.profile_id IS NOT OLD.profile_id OR NEW.all_groups IS NOT OLD.all_groups)
                    BEGIN
                      DELETE FROM memberships WHERE user_id=OLD.id AND company_id={cid}
                        AND NEW.profile_id IS NULL AND COALESCE(NEW.all_groups, 0)=0;
                      INSERT OR REPLACE INTO memberships(user_id, company_id, profile_id, all_groups, source)
                        SELECT OLD.id, {cid}, NEW.profile_id, COALESCE(NEW.all_groups, 0), 'manual'
                        WHERE NEW.profile_id IS NOT NULL OR COALESCE(NEW.all_groups, 0)=1;
                    END""")
    con.execute("CREATE TEMP TRIGGER users_del INSTEAD OF DELETE ON users BEGIN DELETE FROM accounts WHERE id=OLD.id; END")


def _raw(path):
    con = sqlite3.connect(path, timeout=10, factory=Conn)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def connect(cid=None, company=True):
    """Connection to a company's documentation (cid, or the first active company) with core.db attached.
    company=False (or no company yet): only the core (users, profiles, settings)."""
    comp = None
    if company:
        comp = company_by_id(cid) if cid else None
        if comp is None:
            comp = next((c for c in companies_all() if c["active"]), None)
    con = _raw(os.path.join(DATA, comp["dbfile"]) if comp else ":memory:")
    con.execute("ATTACH DATABASE ? AS core", (CORE_PATH,))
    con.cid = comp["id"] if comp else None
    _user_view(con, con.cid)
    return con


def db():
    if "db" not in g:
        cid = g.get("cid")
        if cid is None and has_request_context():
            cid = session.get("cid")
        g.db = connect(cid)
    return g.db


def current_company():
    return company_by_id(db().cid)


@app.teardown_appcontext
def _close(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def ensure_reference_data(con):
    """Make sure every port type / model used by ports and switches exists in the settings tables."""
    if not con.execute("SELECT 1 FROM port_types LIMIT 1").fetchone():
        for j, (n, c) in enumerate(DEFAULT_PORT_TYPES):
            con.execute("INSERT OR IGNORE INTO port_types(name,color,sort) VALUES (?,?,?)", (n, c, j))
    nxt = con.execute("SELECT COALESCE(MAX(sort),0)+1 FROM port_types").fetchone()[0]
    for (name,) in con.execute("SELECT DISTINCT ptype FROM ports WHERE ptype<>'' AND ptype NOT IN "
                               "(SELECT name FROM port_types)").fetchall():
        con.execute("INSERT OR IGNORE INTO port_types(name,color,sort) VALUES (?,?,?)", (name, "#64748b", nxt))
        nxt += 1
    con.execute("""INSERT OR IGNORE INTO models(name, poe)
                   SELECT model, MAX(poe) FROM switches WHERE model<>'' GROUP BY model""")
    # locations / racks / offices used by switches (racks and offices remember the location they were seen in)
    con.execute("""INSERT OR IGNORE INTO locations(name, area)
                   SELECT location, MIN(area) FROM switches WHERE location<>'' GROUP BY location""")
    con.execute("""INSERT OR IGNORE INTO racks(name, location)
                   SELECT rack, MIN(location) FROM switches WHERE rack<>'' GROUP BY rack""")
    con.execute("""INSERT OR IGNORE INTO offices(name, location)
                   SELECT designation, MIN(location) FROM switches WHERE designation<>'' GROUP BY designation""")


CORE_SCHEMA, COMPANY_SCHEMA = _split_schema()


def init_core():
    """Create / upgrade core.db. On the first start of version 12, the single database of version 11 becomes the
    first company: its users, authorisation profiles and security settings move to core.db."""
    legacy = not os.path.exists(CORE_PATH) and os.path.exists(DB_PATH)
    con = _raw(CORE_PATH)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(CORE_SCHEMA)
    core_side = True
    upgraded_users = False
    for table, cols in ADDED_COLUMNS.items():
        if (table in CORE_TABLES) != core_side:
            continue
        tname = "accounts" if table == "users" else table
        have = {r[1] for r in con.execute(f"PRAGMA main.table_info({tname})")}
        for col, ddl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {tname} ADD COLUMN {col} {ddl}")
                if table == "users" and col == "is_admin":
                    upgraded_users = True
                if table == "tickets" and col == "closed_at":
                    con.execute("""UPDATE tickets SET closed_at=COALESCE(NULLIF(validated_at, ''), created_at)
                                   WHERE status IN ('validated', 'cancelled')""")
                if table == "asset_types" and col == "icon":
                    for kind, icon in KIND_ICONS.items():
                        con.execute("UPDATE asset_types SET icon=? WHERE kind=? AND icon=''", (icon, kind))
                if table == "asset_types" and col == "multi":  # devices that usually have several cables
                    con.execute("UPDATE asset_types SET multi=1 WHERE kind IN ('server', 'firewall', 'router')")
                if table == "switches" and col == "sfp_count":
                    con.execute("""UPDATE switches SET sfp=1, sfp_count=(SELECT m.sfp_count FROM models m
                                   WHERE m.name=switches.model) WHERE model IN (SELECT name FROM models WHERE sfp=1)""")
    upgraded_users = False  # a new core.db: nothing to upgrade (version 1 is handled by the migration)
    if legacy:
        _migrate_single_db(con)
    if upgraded_users:  # users created by version 1 had full rights
        con.execute("UPDATE accounts SET is_admin=1, all_groups=1")
    if not con.execute("SELECT 1 FROM auth_profiles LIMIT 1").fetchone():
        full = {k: "modify" for k in MENU_KEYS}
        tech = {"switches": "view", "ports": "edit", "credentials": "view", "passwords": "view", "maintenance": "edit",
                "topology": "view",
                "import_export": "view", "groups": "view", "vlans": "view", "port_types": "view",
                "models": "view", "places": "view", "assets": "view", "snmp": "view", "ip_access": "none", "users": "none", "profiles": "none", "audit": "none"}
        supervisor = dict(tech, switches="modify", ports="modify", vlans="edit", port_types="edit", models="edit",
                          places="edit", topology="edit", assets="modify", maintenance="modify")
        ro = {k: "view" for k in MENU_KEYS}
        ro.update(snmp="none", ip_access="none", passwords="none", credentials="none", users="none", profiles="none", import_export="none", audit="none")
        for name, desc, perms in [
            ("Administrator", "Full control of every menu", full),
            ("Network supervisor", "Manages switches, applies port changes and approves technicians' changes", supervisor),
            ("Technician", "Proposes port changes (need approval), can read credentials", tech),
            ("Technician (no passwords)", "Like Technician, but passwords stay hidden", dict(tech, passwords="none")),
            ("Read only", "Can look at switches, ports and topology - no passwords", ro),
        ]:
            con.execute("INSERT INTO auth_profiles(name, description, perms) VALUES (?,?,?)",
                        (name, desc, json.dumps(perms)))
    else:  # profiles from an older version: give the Administrator profile the new menus
        for row in con.execute("SELECT id, name, perms FROM auth_profiles").fetchall():
            perms = json.loads(row["perms"] or "{}")
            missing = [k for k in MENU_KEYS if k not in perms]
            if missing:
                for k in missing:
                    if row["name"] == "Administrator":
                        perms[k] = "modify"
                    elif k == "maintenance":
                        perms[k] = {"modify": "modify", "edit": "edit"}.get(perms.get("ports"), "view" if perms.get("switches", "none") != "none" else "none")
                    elif k == "passwords":  # until now, seeing credentials meant seeing the passwords
                        perms[k] = "view" if perms.get("credentials", "none") != "none" else "none"
                    elif k == "snmp":
                        perms[k] = "view" if perms.get("ports") in ("edit", "modify") else "none"
                    elif k == "assets":  # whoever can change ports directly may also manage assets
                        perms[k] = "edit" if perms.get("ports") == "modify" else ("view" if perms.get("ports", "none") != "none" else "none")
                    else:
                        perms[k] = "view" if k == "places" else "none"
                con.execute("UPDATE auth_profiles SET perms=? WHERE id=?", (json.dumps(perms), row["id"]))
    if not con.execute("SELECT 1 FROM auth_profiles WHERE name='Technician (no passwords)'").fetchone():
        tech = con.execute("SELECT perms FROM auth_profiles WHERE name='Technician'").fetchone()
        if tech:
            perms = dict(json.loads(tech["perms"] or "{}"), passwords="none")
            con.execute("INSERT INTO auth_profiles(name, description, perms) VALUES (?,?,?)",
                        ("Technician (no passwords)", "Like Technician, but passwords stay hidden", json.dumps(perms)))
    con.commit()
    con.close()
    companies_all(reload=True)


def _initials(name):
    words = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words).upper()[:6] if len(words) > 1 else name[:6].upper()


def _migrate_single_db(core):
    import shutil
    old = _raw(DB_PATH)
    old_tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "users" not in old_tables:  # an empty / new file: nothing to move
        old.close()
        return False
    old.close()
    shutil.copy2(DB_PATH, os.path.join(DATA, "switchdoc-before-v12.db"))  # safety copy
    old = _raw(DB_PATH)
    for table in ("auth_profiles", "users", "ip_rules", "app_settings"):
        if table not in old_tables:
            continue
        dest = "accounts" if table == "users" else table
        have = [r[1] for r in old.execute(f"PRAGMA table_info({table})")]
        mine = {r[1] for r in core.execute(f"PRAGMA main.table_info({dest})")}
        cols = [c for c in have if c in mine]
        rows = old.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        core.executemany(f"INSERT OR REPLACE INTO {dest}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [tuple(r) for r in rows])
    ucols = {r[1] for r in old.execute("PRAGMA table_info(users)")}
    if "is_admin" not in ucols:  # version 1: every user had full rights
        core.execute("UPDATE accounts SET is_admin=1")
    cid = core.execute("INSERT INTO companies(name, code, dbfile, sort, created_at) VALUES (?,?,?,1,?)",
                       (FIRST_COMPANY, _initials(FIRST_COMPANY), os.path.basename(DB_PATH),
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
    for u in old.execute("SELECT * FROM users").fetchall():
        prof = u["profile_id"] if "profile_id" in ucols else None
        allg = u["all_groups"] if "all_groups" in ucols else 1
        admin = u["is_admin"] if "is_admin" in ucols else 1
        if prof is not None or allg or admin:
            core.execute("INSERT OR REPLACE INTO memberships(user_id, company_id, profile_id, all_groups, source) "
                         "VALUES (?,?,?,?, 'manual')", (u["id"], cid, prof, 1 if allg else 0))
    core.execute("UPDATE app_settings SET key=? WHERE key='disabled_ports_cleaned'", (f"disabled_ports_cleaned:{cid}",))
    core.commit()
    old.execute("PRAGMA foreign_keys = OFF")
    for table in CORE_TABLES:
        old.execute(f"DROP TABLE IF EXISTS {table}")
    old.commit()
    old.close()
    return True


def _rebuild_user_fk(con):
    """Company tables cannot reference users any more (they live in core.db): rebuild older tables without that link."""
    import re as _r
    done = False
    for t in USER_FK_TABLES:
        row = con.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not row or "REFERENCES users" not in row[0]:
            continue
        if not done:
            con.commit()
            con.execute("PRAGMA foreign_keys = OFF")
            done = True
        new_sql = _r.sub(r"\s*REFERENCES users\(id\)(?: ON DELETE (?:CASCADE|SET NULL))?", "", row[0])
        con.execute(f"ALTER TABLE main.{t} RENAME TO {t}__old")
        con.execute(new_sql.replace(f"CREATE TABLE {t}", f"CREATE TABLE main.{t}", 1))
        con.execute(f"INSERT INTO main.{t} SELECT * FROM main.{t}__old")
        con.execute(f"DROP TABLE main.{t}__old")
    if done:
        con.commit()
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(COMPANY_SCHEMA)  # indexes of the rebuilt tables


def init_company(comp):
    con = connect(comp["id"])
    con.execute("PRAGMA main.journal_mode = WAL")
    con.executescript(COMPANY_SCHEMA)
    _rebuild_user_fk(con)
    core_side = False
    done_key = f"disabled_ports_cleaned:{comp['id']}"
    upgraded_users = False
    upgraded_users = False
    for table, cols in ADDED_COLUMNS.items():
        if (table in CORE_TABLES) != core_side:
            continue
        tname = table
        have = {r[1] for r in con.execute(f"PRAGMA main.table_info({tname})")}
        for col, ddl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {tname} ADD COLUMN {col} {ddl}")
                if table == "users" and col == "is_admin":
                    upgraded_users = True
                if table == "tickets" and col == "closed_at":
                    con.execute("""UPDATE tickets SET closed_at=COALESCE(NULLIF(validated_at, ''), created_at)
                                   WHERE status IN ('validated', 'cancelled')""")
                if table == "asset_types" and col == "icon":
                    for kind, icon in KIND_ICONS.items():
                        con.execute("UPDATE asset_types SET icon=? WHERE kind=? AND icon=''", (icon, kind))
                if table == "asset_types" and col == "multi":  # devices that usually have several cables
                    con.execute("UPDATE asset_types SET multi=1 WHERE kind IN ('server', 'firewall', 'router')")
                if table == "switches" and col == "sfp_count":
                    con.execute("""UPDATE switches SET sfp=1, sfp_count=(SELECT m.sfp_count FROM models m
                                   WHERE m.name=switches.model) WHERE model IN (SELECT name FROM models WHERE sfp=1)""")
    migrate_legacy_creds(con)
    # a disabled port holds no documentation (one-time clean-up of older data, then enforced on every change)
    if not con.execute("SELECT 1 FROM app_settings WHERE key=?", (done_key,)).fetchone():
        cond = " OR ".join(f"{c}<>''" for c in CLEARED_WHEN_DISABLED)
        rows = con.execute(f"""SELECT s.name, p.name FROM ports p JOIN switches s ON s.id=p.switch_id
                               WHERE p.status='disabled' AND ({cond})""").fetchall()
        if rows:
            con.execute(f"UPDATE ports SET {', '.join(f'{c}=' + chr(39) * 2 for c in CLEARED_WHEN_DISABLED)} "
                        f"WHERE status='disabled' AND ({cond})")
            con.execute("""INSERT INTO audit(ts, username, category, action, target_type, target_name, details)
                           VALUES (?,?,?,?,?,?,?)""", (assetlib.now(), "system", "documentation", "port_update", "port", "",
                                                      json.dumps({"rows": [None, len(rows)], "status": [None, "disabled"],
                                                                  "comment": [None, "Disabled ports emptied: " + ", ".join(
                                                                      f"{a} {b}" for a, b in rows[:200])]})))
        con.execute("INSERT INTO app_settings(key, value) VALUES (?, '1')", (done_key,))
    ensure_reference_data(con)
    convert_equipment(con)
    con.commit()
    con.close()


def create_company(name, code="", notes=""):
    """New company with an empty documentation (default port types and equipment types)."""
    core = _raw(CORE_PATH)
    cid = core.execute("INSERT INTO companies(name, code, dbfile, notes, sort, created_at) VALUES (?,?,?,?,"
                       "(SELECT COALESCE(MAX(sort),0)+1 FROM companies),?)",
                       (name, code or _initials(name), f"pending-{secrets.token_hex(4)}", notes,
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
    core.execute("UPDATE companies SET dbfile=? WHERE id=?", (f"company-{cid}.db", cid))
    core.commit()
    core.close()
    companies_all(reload=True)
    init_company(company_by_id(cid))
    return cid


def init_db():
    init_core()
    for comp in companies_all():
        init_company(comp)


def convert_equipment(con, username="system"):
    """Old free-text Connected equipment / Remote switch -> Equipment type + asset (+ device behind)."""
    n, created = assetlib.convert_all(con)
    if n:
        con.execute("""INSERT INTO audit(ts, username, category, action, target_type, target_name, details)
                       VALUES (?,?,?,?,?,?,?)""",
                    (assetlib.now(), username, "documentation", "assets_converted", "asset", "",
                     json.dumps({"rows": [None, n], "result": [None, created]})))
    return n, created


init_db()


def enc(value):
    return FERNET.encrypt(value.encode()).decode() if value else ""


def dec(value):
    if not value:
        return ""
    try:
        return FERNET.decrypt(value.encode()).decode()
    except InvalidToken:
        return "<cannot decrypt - wrong secret.key?>"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def now_s():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_port_type(con):
    row = con.execute("SELECT name FROM port_types ORDER BY sort, name LIMIT 1").fetchone()
    return row[0] if row else "Access"


def set_port_count(con, switch_id, count, prefix, default_type="Access"):
    count = max(0, min(int(count), 512))
    cur = con.execute("SELECT COALESCE(MAX(idx),0) FROM ports WHERE switch_id=?", (switch_id,)).fetchone()[0]
    for i in range(cur + 1, count + 1):
        con.execute("INSERT OR IGNORE INTO ports(switch_id, idx, name, ptype) VALUES (?,?,?,?)",
                    (switch_id, i, f"{prefix}{i}", default_type))
    con.execute("DELETE FROM ports WHERE switch_id=? AND idx>?", (switch_id, count))


# ---------------------------------------------------------------- audit log
# events that are not about one company's documentation go to the audit log of core.db
GLOBAL_AUDIT = {"setup", "login", "login_failed", "logout", "session_timeout", "password_change", "language",
                "user_create", "user_update", "user_delete", "profile_create", "profile_update", "profile_delete",
                "security_settings", "ip_access_update", "ip_access_denied", "company_create", "company_update",
                "company_delete", "directory_update", "directory_test", "company_switch"}


def audit(category, action, target_type="", target_id=None, target_name="", details=None, user=None, username=None):
    u = user if user is not None else g.get("user")
    ip = ""
    if has_request_context():
        ip = (request.access_route[0] if request.access_route else request.remote_addr) or ""
    table = "core.audit" if action in GLOBAL_AUDIT or db().cid is None else "main.audit"
    db().execute(f"""INSERT INTO {table}(ts, user_id, username, ip, category, action, target_type, target_id, target_name,
                    details) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (now_s(), u["id"] if u is not None else None,
                  username if username is not None else (u["username"] if u is not None else ""),
                  ip, category, action, target_type, target_id, target_name or "",
                  json.dumps(details or {}, ensure_ascii=False, default=str)))


def diff(old, new, keys=None):
    """{field: [old, new]} for every field whose value changed."""
    out = {}
    for k in keys or new.keys():
        o = old.get(k) if isinstance(old, dict) else (old[k] if old is not None and k in old.keys() else None)
        n = new.get(k)
        if (o if o is not None else "") != (n if n is not None else ""):
            out[k] = [o, n]
    return out


# ---------------------------------------------------------------- current user & permissions
def current_user():
    return g.get("user")


def perms_of(user):
    if user is None:
        return {}
    if user["is_admin"]:
        return {k: 3 for k in MENU_KEYS}
    if "perms" not in g:
        row = db().execute("SELECT perms FROM auth_profiles WHERE id=?", (user["profile_id"],)).fetchone()
        raw = json.loads(row["perms"]) if row else {}
        g.perms = {k: LEVELS.get(raw.get(k, "none"), 0) for k in MENU_KEYS}
    return g.perms


def perm(menu):
    return perms_of(current_user()).get(menu, 0)


def can(menu, level="view"):
    return perm(menu) >= LEVELS[level]


def require(menu, level="view"):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not can(menu, level):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def sees_all():
    u = current_user()
    return bool(u and (u["is_admin"] or u["all_groups"]))


def vis(alias="s", all_status=False):
    """SQL fragment restricting switches to the ones the current user may see (via groups).
    Disabled / replaced switches are hidden from the documentation unless all_status is True."""
    clause, params = "", ()
    if not all_status:
        clause = f" AND {alias}.status='active'"
    if not sees_all():
        clause += (f" AND {alias}.id IN (SELECT sg.switch_id FROM switch_groups sg JOIN user_groups ug "
                   f"ON ug.group_id=sg.group_id WHERE ug.user_id=?)")
        params = (current_user()["id"],)
    return clause, params


def can_see_switch(sid, all_status=False):
    clause, params = vis("s", all_status)
    return db().execute(f"SELECT 1 FROM switches s WHERE s.id=?{clause}", (sid, *params)).fetchone() is not None


def my_group_ids():
    if sees_all():
        return None
    return {r[0] for r in db().execute("SELECT group_id FROM user_groups WHERE user_id=?", (current_user()["id"],))}


def visible_groups():
    con = db()
    if sees_all():
        return con.execute("SELECT * FROM sw_groups ORDER BY name COLLATE NOCASE").fetchall()
    return con.execute("""SELECT g.* FROM sw_groups g JOIN user_groups ug ON ug.group_id=g.id
                          WHERE ug.user_id=? ORDER BY g.name COLLATE NOCASE""", (current_user()["id"],)).fetchall()


def display_name(u):
    if not u:
        return "—"
    full = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip()
    return full or u["username"]


# ---------------------------------------------------------------- request guards
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


# ---------------------------------------------------------------- session timeout
DEFAULT_SESSION_TIMEOUT = 15  # minutes without activity before the user is signed out
PASSIVE_ENDPOINTS = {"api_notifications"}  # background polling does not count as activity


def get_setting(key, default=""):
    try:
        row = db().execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row and row[0] != "" else default


def set_setting(con, key, value):
    con.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def session_timeout_min():
    try:
        return max(1, min(1440, int(get_setting("session_timeout", DEFAULT_SESSION_TIMEOUT))))
    except ValueError:
        return DEFAULT_SESSION_TIMEOUT


# ---------------------------------------------------------------- password policy
PW_DEFAULTS = {"pw_min": "10", "pw_upper": "1", "pw_lower": "1", "pw_digit": "1", "pw_special": "1"}


def pw_policy():
    out = {k: get_setting(k, v) for k, v in PW_DEFAULTS.items()}
    try:
        out["pw_min"] = max(6, min(64, int(out["pw_min"])))
    except ValueError:
        out["pw_min"] = 10
    for k in ("pw_upper", "pw_lower", "pw_digit", "pw_special"):
        out[k] = out[k] == "1"
    return out


def pw_rules():
    """[(key, text, JS regex or None)] of the current policy - used by the server check and the live hint."""
    p = pw_policy()
    rules = [("len", _("At least %(n)s characters", n=p["pw_min"]), None)]
    if p["pw_upper"]:
        rules.append(("upper", _("An uppercase letter (A-Z)"), "[A-Z]"))
    if p["pw_lower"]:
        rules.append(("lower", _("A lowercase letter (a-z)"), "[a-z]"))
    if p["pw_digit"]:
        rules.append(("digit", _("A digit (0-9)"), "[0-9]"))
    if p["pw_special"]:
        rules.append(("special", _("A special character (! @ # $ % …)"), "[^A-Za-z0-9]"))
    rules.append(("user", _("Different from the username"), None))
    return rules, p["pw_min"]


def password_problems(pw, username=""):
    import re as _r
    rules, minimum = pw_rules()
    bad = []
    for key, text, rx in rules:
        ok = (len(pw) >= minimum) if key == "len" else \
            (not username or pw.lower() != username.lower()) if key == "user" else bool(_r.search(rx, pw))
        if not ok:
            bad.append(text)
    return bad


# ---------------------------------------------------------------- network access (allowed IPs)
_IP = {"loaded": False, "on": False, "rules": []}
_DENIED_LOGGED = {}


def ip_rules_reload(con=None):
    con = con or connect()
    on = con.execute("SELECT value FROM app_settings WHERE key='ip_restrict'").fetchone()
    _IP["rules"] = ipaccess.compile_rules([r[0] for r in con.execute("SELECT rule FROM ip_rules")])
    _IP["on"] = bool(on and on[0] == "1" and _IP["rules"])
    _IP["loaded"] = True


def _client_ip():
    return request.remote_addr or ""


def _ip_guard():
    if not _IP["loaded"]:
        ip_rules_reload()
    if not _IP["on"] or ipaccess.allowed(_client_ip(), _IP["rules"]):
        return None
    ip = _client_ip()
    last = _DENIED_LOGGED.get(ip, 0)
    if datetime.datetime.now().timestamp() - last > 600:  # log each refused address at most every 10 minutes
        _DENIED_LOGGED[ip] = datetime.datetime.now().timestamp()
        try:
            con = connect()
            con.execute("""INSERT INTO core.audit(ts, username, ip, category, action, target_type, target_name, details)
                           VALUES (?,?,?,?,?,?,?,?)""", (now_s(), "", ip, "security", "ip_access_denied", "", request.path[:200], "{}"))
            con.commit()
            con.close()
        except sqlite3.Error:
            pass
    return (f"<!doctype html><meta charset=utf-8><title>403</title><body style='font-family:system-ui;padding:40px'>"
            f"<h1>403</h1><p>{_('Access refused: your IP address %(ip)s is not allowed to open SwitchDoc.', ip=ip)}</p>"), 403


app.before_request_funcs.setdefault(None, []).insert(0, _ip_guard)


@app.before_request
def _guard():
    if request.endpoint in ("static",):
        return None
    con = db()
    has_users = con.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if not has_users and request.endpoint not in ("setup", "set_lang"):
        return redirect(url_for("setup"))
    if request.method == "POST":
        token = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not token or token != session.get("csrf"):
            abort(400, _("Invalid or missing security token - reload the page and try again."))
    if "uid" in session:
        g.user = con.execute("SELECT * FROM users WHERE id=? AND active=1", (session["uid"],)).fetchone()
        if g.user is None:
            session.clear()
        else:
            stamp = datetime.datetime.now().timestamp()
            if stamp - session.get("seen", stamp) > session_timeout_min() * 60:
                audit("auth", "session_timeout", "user", g.user["id"], g.user["username"])
                con.commit()
                keep = session.get("lang")
                session.clear()
                if keep:
                    session["lang"] = keep
                g.user = None
                if request.path.startswith("/api/"):
                    abort(401)
                flash(_("You were signed out after %(n)s minutes without activity.", n=session_timeout_min()), "info")
                return redirect(url_for("login", next=request.path if request.method == "GET" else None))
            if request.endpoint not in PASSIVE_ENDPOINTS:
                session["seen"] = stamp
            if g.user["must_change"] and g.user["auth_source"] != "directory" and request.endpoint not in ("password_change", "logout", "set_lang", "api_keepalive"):
                if request.path.startswith("/api/"):
                    abort(401)
                return redirect(url_for("password_change"))
            # the company the user works in: the one chosen in the session if he has a role there, else his first one
            ids = [c["id"] for c in allowed_companies(g.user)]
            if con.cid not in ids:
                if ids:
                    g.pop("db").close()
                    g.cid = session["cid"] = ids[0]
                    con = db()
                    g.user = con.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
                    g.pop("perms", None)
                elif request.endpoint not in ("logout", "set_lang", "account", "password_change", "api_keepalive",
                                              "api_notifications"):
                    if request.path.startswith("/api/"):
                        abort(403)
                    return render_template("error.html", code=403, message=_(
                        "Your account has no role in any company yet. Ask an administrator to give you access."))
            else:
                session["cid"] = con.cid
    if request.endpoint not in ("login", "setup", "set_lang") and has_users and not g.get("user"):
        if request.path.startswith("/api/"):
            abort(401)
        return redirect(url_for("login", next=request.path))
    return None


def offline_count():
    """Switches of the current company seen offline (monitoring on, not in a maintenance ticket in progress)."""
    try:
        con = db()
        if con.cid is None or mon_cfg(con)["mon_enabled"] != "1":
            return 0
        clause, params = vis("s")
        return con.execute(f"""SELECT COUNT(*) FROM switch_status st JOIN switches s ON s.id=st.switch_id
                               WHERE st.state='offline' AND s.ip<>'' AND COALESCE(s.layer, '')<>'L1'
                               AND COALESCE(s.monitor, 1)<>0{clause}
                               AND s.id NOT IN (SELECT ts.switch_id FROM ticket_switches ts JOIN tickets t
                                                ON t.id=ts.ticket_id WHERE t.status='in_progress')""",
                           params).fetchone()[0]
    except sqlite3.Error:
        return 0


def allowed_companies(user):
    """Active companies the user may open: all for an administrator, else those where he has a role."""
    if user is None:
        return []
    comps = [c for c in companies_all() if c["active"]]
    if user["is_admin"]:
        return comps
    con = db()
    mine = {r[0] for r in con.execute("SELECT company_id FROM core.memberships WHERE user_id=? AND profile_id IS NOT NULL",
                                      (user["id"],))}
    return [c for c in comps if c["id"] in mine]


@app.context_processor
def _globals():
    u = current_user()
    pending = 0
    if u and can("ports", "view"):
        clause, params = vis("s")
        if can("ports", "modify"):
            pending = db().execute(f"""SELECT COUNT(*) FROM change_requests c JOIN switches s ON s.id=c.switch_id
                                       WHERE c.status='pending' AND c.ticket_id IS NULL{clause}""", params).fetchone()[0]
        else:
            pending = db().execute("""SELECT COUNT(*) FROM change_requests WHERE status='pending' AND requested_by=?
                                      AND ticket_id IS NULL""", (u["id"],)).fetchone()[0]
    js = {s: i18n.translate(lang(), s) for s in i18n.JS_STRINGS}
    return {"app_name": "SwitchDoc", "can": can, "me": u, "display_name": display_name, "_": _,
            "company": current_company() if u else None, "my_companies": allowed_companies(u) if u else [],
            "pending_count": pending, "csrf_token": csrf_token, "lang": lang(), "languages": LANGUAGES,
            "js_i18n": js, "any_settings": any(can(m) for m in SETTINGS_MENUS) or can("maintenance", "modify"),
            "session_timeout": session_timeout_min() if u else 0, "pw_rules": pw_rules(),
            "tickets_note": my_ticket_counts(db()) if u else {"todo": 0, "validate": 0, "new": []},
            "offline_count": offline_count() if u and can("switches") else 0}


@app.errorhandler(403)
def _forbidden(_e):
    return render_template("error.html", code=403,
                           message=_("You don't have permission to open this page. Ask an administrator to "
                                     "change your authorisation profile or switch groups.")), 403


@app.errorhandler(404)
def _notfound(_e):
    return render_template("error.html", code=404,
                           message=_("This page or item does not exist, is disabled, or belongs to a switch group "
                                     "you are not a member of.")), 404


@app.route("/api/keepalive", methods=["POST"])
def api_keepalive():
    """The page is being used (typing, clicking): keep the session alive."""
    return jsonify({"ok": True, "timeout": session_timeout_min()})


@app.route("/lang/<code>")
def set_lang(code):
    if code in LANGUAGES:
        session["lang"] = code
        if g.get("user"):
            old = g.user["lang"]
            db().execute("UPDATE users SET lang=? WHERE id=?", (code, g.user["id"]))
            audit("auth", "language", "user", g.user["id"], g.user["username"], {"lang": [old, code]})
            db().commit()
    nxt = request.referrer or url_for("dashboard")
    return redirect(nxt)


# ---------------------------------------------------------------- auth
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db().execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        u, p = f["username"].strip(), f["password"]
        bad = password_problems(p, u)
        if not u or bad:
            flash(_("Username required.") if not u else _("The password does not respect the policy: %(list)s",
                                                          list=", ".join(bad)), "error")
        else:
            if not companies_all():  # first company of a new installation
                cname = (f.get("company") or "").strip()[:120] or FIRST_COMPANY
                g.pop("db").close()
                g.cid = create_company(cname)
            admin_profile = db().execute("SELECT id FROM auth_profiles WHERE name='Administrator'").fetchone()
            db().execute("""INSERT INTO users(username,pw_hash,first_name,last_name,email,title,profile_id,
                            is_admin,all_groups,active,lang) VALUES (?,?,?,?,?,?,?,1,1,1,?)""",
                         (u, generate_password_hash(p), f.get("first_name", "").strip(),
                          f.get("last_name", "").strip(), f.get("email", "").strip(), f.get("title", "").strip(),
                          admin_profile["id"] if admin_profile else None, lang()))
            uid = db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
            audit("auth", "setup", "user", uid, u, username=u)
            db().commit()
            flash(_("Administrator account created - please sign in."), "ok")
            return redirect(url_for("login"))
    return render_template("setup.html", need_company=not companies_all())


def sign_in_directories():
    """Enabled directories of active companies (for the sign-in page)."""
    active = {c["id"]: c for c in companies_all() if c["active"]}
    rows = db().execute("SELECT * FROM core.directories WHERE enabled=1 ORDER BY id").fetchall()
    return [dict(d, company=active[d["company_id"]]["name"]) for d in rows if d["company_id"] in active]


def _pick_directory(login, choice, dirs):
    """The directory to use: the one chosen, else the one whose NetBIOS name (DOMAIN\\user) or UPN suffix
    (user@domain) the login names; None = local account."""
    if choice == "local":
        return None
    if choice and choice.isdigit():
        return next((d for d in dirs if d["id"] == int(choice)), None)
    low = login.lower()
    for d in dirs:
        if d["netbios"] and low.startswith(d["netbios"].lower() + "\\"):
            return d
        if d["upn_suffix"] and low.endswith("@" + d["upn_suffix"].lower()):
            return d
    return None


def directory_username(d, info):
    suffix = (d["upn_suffix"] or d["netbios"] or f"dir{d['id']}").lower()
    return f"{info['sam'].lower()}@{suffix}"


def provision_directory_user(con, d, info):
    """Create or refresh the SwitchDoc account of a domain user (names, email, title come from the directory)."""
    uname = directory_username(d, info)
    row = con.execute("""SELECT * FROM core.accounts WHERE auth_source='directory' AND directory_id=?
                         AND ((ext_id<>'' AND ext_id=?) OR lower(username)=lower(?))""",
                      (d["id"], info["guid"], uname)).fetchone()
    vals = {"first_name": info["first_name"] or info["display"], "last_name": info["last_name"],
            "email": info["email"], "title": info["title"], "ext_id": info["guid"], "last_sync": now()}
    if row is None:
        if con.execute("SELECT 1 FROM core.accounts WHERE lower(username)=lower(?)", (uname,)).fetchone():
            raise ValueError(uname)
        con.execute(f"""INSERT INTO core.accounts(username, pw_hash, auth_source, directory_id, active, must_change,
                        is_admin, {', '.join(vals)}) VALUES (?, '!', 'directory', ?, 1, 0, 0,
                        {', '.join('?' * len(vals))})""", (uname, d["id"], *vals.values()))
        uid = con.execute("SELECT id FROM core.accounts WHERE username=?", (uname,)).fetchone()[0]
        audit("security", "user_create", "user", uid, uname, {"auth_source": [None, d["label"] or d["company"]]},
              username="system")
    else:
        uid = row["id"]
        con.execute(f"UPDATE core.accounts SET username=?, {', '.join(f'{k}=?' for k in vals)} WHERE id=?",
                    (uname, *vals.values(), uid))
    return uid


def sync_directory_roles(con, d, uid, info):
    """Role in the directory's company from the AD group mapping (first match in the list), or the default role.
    A role an administrator set by hand in SwitchDoc is never changed. Returns the role applied (or None)."""
    m = con.execute("SELECT * FROM core.memberships WHERE user_id=? AND company_id=?", (uid, d["company_id"])).fetchone()
    if m is not None and m["source"] == "manual":
        return {"profile_id": m["profile_id"], "source": "manual"}
    hit = None
    for rule in con.execute("SELECT * FROM core.dir_groups WHERE directory_id=? ORDER BY sort, id", (d["id"],)):
        if ldapauth.matches(rule["ad_group"], info["groups"], info["group_names"]):
            hit = rule
            break
    if hit is None and d["default_profile_id"]:
        want = {"profile_id": d["default_profile_id"], "all_groups": 0, "sw_groups": ""}
    elif hit is not None:
        want = {"profile_id": hit["profile_id"], "all_groups": hit["all_groups"], "sw_groups": hit["sw_groups"] or ""}
    else:
        want = None
    if want is None:
        con.execute("DELETE FROM core.memberships WHERE user_id=? AND company_id=?", (uid, d["company_id"]))
        return None
    con.execute("INSERT OR REPLACE INTO core.memberships(user_id, company_id, profile_id, all_groups, source) "
                "VALUES (?,?,?,?, 'ad')", (uid, d["company_id"], want["profile_id"], want["all_groups"]))
    names = [x.strip().lower() for x in want["sw_groups"].split(",") if x.strip()]
    if names:  # switch groups given by the mapping, in that company's documentation
        other = con if con.cid == d["company_id"] else connect(d["company_id"])
        gids = [r[0] for r in other.execute("SELECT id, name FROM sw_groups") if r[1].lower() in names]
        other.execute("DELETE FROM user_groups WHERE user_id=?", (uid,))
        other.executemany("INSERT OR IGNORE INTO user_groups(user_id, group_id) VALUES (?,?)", [(uid, gi) for gi in gids])
        if other is not con:
            other.commit()
            other.close()
    return dict(want, source="ad", rule=hit["ad_group"] if hit else None)


def _directory_cfg(d):
    return dict(d)


@app.route("/login", methods=["GET", "POST"])
def login():
    dirs = sign_in_directories()
    if request.method == "POST":
        ident = request.form["username"].strip()
        password = request.form["password"]
        d = _pick_directory(ident, request.form.get("dir", ""), dirs)
        con = db()
        row, reason = None, ""
        # a local account with this name is always tried first (e.g. the local administrator)
        cand = con.execute("SELECT * FROM users WHERE (username=? OR (email<>'' AND lower(email)=lower(?))) "
                           "AND active=1 AND COALESCE(auth_source, 'local')='local'", (ident, ident)).fetchone()
        try:
            ok = cand is not None and check_password_hash(cand["pw_hash"], password)
        except ValueError:
            ok = False
        row = cand if ok else None
        if row is not None:
            d = None
        elif d is None:  # "Local" chosen but DOMAIN\\user or user@domain typed: try that domain
            d = _pick_directory(ident, "", dirs)
        if d is not None and row is None:
            try:
                info = ldapauth.authenticate(_directory_cfg(d), ident, password, dec(d["bind_pass"]))
                uid = provision_directory_user(con, d, info)
                role = sync_directory_roles(con, d, uid, info)
                con.commit()
                row = con.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
                if row is not None and not row["is_admin"] and not allowed_companies(row):
                    reason = "no role in SwitchDoc (no mapped AD group)"
                    flash(_("Your domain account is valid, but it has no role in SwitchDoc. Ask an administrator "
                            "to add you to the right AD group."), "error")
                    audit("auth", "login_failed", "user", uid, row["username"], {"reason": [None, reason]},
                          user=None, username=row["username"])
                    con.commit()
                    return render_template("login.html", dirs=dirs, last=str(d["id"]))
                if role is None and row is not None and not allowed_companies(row):
                    row = None
            except ldapauth.LdapError as e:
                reason = f"{d['label'] or d['company']}: {e.code} - {e}"
            except ValueError as e:
                reason = f"username {e} already used by a local account"
        if row is not None:
            session.clear()
            session.permanent = True
            session["uid"] = row["id"]
            session["seen"] = datetime.datetime.now().timestamp()
            if d is not None:
                session["cid"] = d["company_id"]
            if row["lang"]:
                session["lang"] = row["lang"]
            con.execute("UPDATE users SET last_login=? WHERE id=?", (now(), row["id"]))
            audit("auth", "login", "user", row["id"], row["username"], user=row,
                  details={"via": [None, d["label"] or d["company"]]} if d is not None else None)
            con.commit()
            nxt = request.args.get("next", "")
            resp = redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
            if row["must_change"] and d is None:
                flash(_("Your password is temporary: choose your own password to continue."), "info")
                resp = redirect(url_for("password_change"))
            resp.set_cookie("sd_dir", str(d["id"]) if d is not None else "local", max_age=3600 * 24 * 365,
                            samesite="Lax", httponly=True)
            return resp
        audit("auth", "login_failed", "user", None, ident, {"reason": [None, reason]} if reason else None,
              user=None, username=ident[:100])
        con.commit()
        flash(_("Wrong username / email or password, or the account is disabled."), "error")
        return render_template("login.html", dirs=dirs, last=request.form.get("dir", ""))
    return render_template("login.html", dirs=dirs, last=request.cookies.get("sd_dir", ""))


@app.route("/logout")
def logout():
    timed_out = request.args.get("timeout") == "1"
    if g.get("user"):
        audit("auth", "session_timeout" if timed_out else "logout", "user", g.user["id"], g.user["username"])
        db().commit()
    keep = session.get("lang")
    session.clear()
    if keep:
        session["lang"] = keep
    if timed_out:
        flash(_("You were signed out after %(n)s minutes without activity.", n=session_timeout_min()), "info")
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
def password_change():
    """Forced change of a temporary password (also usable any time)."""
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    if u["auth_source"] == "directory":
        return redirect(url_for("account"))
    con = db()
    if request.method == "POST":
        new = request.form.get("new", "")
        bad = password_problems(new, u["username"])
        if new != request.form.get("confirm", ""):
            flash(_("The two new passwords are different."), "error")
        elif bad:
            flash(_("The password does not respect the policy: %(list)s", list=", ".join(bad)), "error")
        elif check_password_hash(u["pw_hash"], new):
            flash(_("The new password must be different from the current one."), "error")
        else:
            con.execute("UPDATE users SET pw_hash=?, must_change=0, pw_changed_at=? WHERE id=?",
                        (generate_password_hash(new), now(), u["id"]))
            audit("security", "password_change", "user", u["id"], u["username"])
            con.commit()
            flash(_("Password changed."), "ok")
            return redirect(url_for("dashboard"))
    return render_template("password_change.html", forced=bool(u["must_change"]))


@app.route("/account", methods=["GET", "POST"])
def account():
    con = db()
    u = current_user()
    if request.method == "POST" and u["auth_source"] == "directory":
        abort(400)
    if request.method == "POST":
        new = request.form["new"]
        bad = password_problems(new, u["username"])
        if not check_password_hash(u["pw_hash"], request.form["current"]):
            flash(_("Current password is wrong."), "error")
        elif new != request.form.get("confirm", new):
            flash(_("The two new passwords are different."), "error")
        elif bad:
            flash(_("The password does not respect the policy: %(list)s", list=", ".join(bad)), "error")
        elif check_password_hash(u["pw_hash"], new):
            flash(_("The new password must be different from the current one."), "error")
        else:
            con.execute("UPDATE users SET pw_hash=?, must_change=0, pw_changed_at=? WHERE id=?",
                        (generate_password_hash(new), now(), u["id"]))
            audit("security", "password_change", "user", u["id"], u["username"])
            con.commit()
            flash(_("Password changed."), "ok")
        return redirect(url_for("account"))
    profile = con.execute("SELECT * FROM auth_profiles WHERE id=?", (u["profile_id"],)).fetchone()
    return render_template("account.html", profile=profile, groups=visible_groups(),
                           perms=perms_of(u), menus=MENUS, level_names=dict(LEVEL_LABELS),
                           level_keys=[k for k, _l in LEVEL_LABELS])


# ---------------------------------------------------------------- dashboard
@app.route("/")
def dashboard():
    con = db()
    if not can("switches"):
        if can("topology"):
            return redirect(url_for("topology"))
        abort(403)
    q = request.args.get("q", "").strip()
    area = request.args.get("area", "")
    group = request.args.get("group", "")
    clause, params = vis("s")
    rows = con.execute(f"""
        SELECT s.*, c.name AS cred_name,
          (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id) AS n_ports,
          (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id AND p.status='active') AS n_active,
          (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id AND p.ptype='Trunk') AS n_trunk,
          (SELECT group_concat(g.id || ':' || g.name || ':' || g.color, '|') FROM switch_groups sg
             JOIN sw_groups g ON g.id=sg.group_id WHERE sg.switch_id=s.id) AS grp
        FROM switches s LEFT JOIN cred_profiles c ON c.id=s.cred_profile_id
        LEFT JOIN models m ON m.name=s.model
        WHERE 1=1{clause}
        ORDER BY s.area COLLATE NOCASE, s.location COLLATE NOCASE, s.name COLLATE NOCASE""", params).fetchall()
    rows = [dict(r, groups=split_groups(r["grp"])) for r in rows]
    areas = sorted({r["area"] for r in rows if r["area"]}, key=str.lower)

    dup_ip, dup_serial = {}, {}
    for r in rows:
        if r["ip"]:
            dup_ip.setdefault(r["ip"], []).append(r["name"])
        if r["serial"]:
            dup_serial.setdefault(r["serial"].upper(), []).append(r["name"])
    dup_ip = {k: v for k, v in dup_ip.items() if len(v) > 1}
    dup_serial = {k: v for k, v in dup_serial.items() if len(v) > 1}

    def match(r):
        if area and r["area"] != area:
            return False
        if group and group not in {x["id"] for x in r["groups"]}:
            return False
        if not q:
            return True
        hay = " ".join(str(r[k] or "") for k in ("name", "ip", "serial", "model", "area", "location",
                                                  "rack", "designation", "notes")).lower()
        return q.lower() in hay

    mcfg = mon_cfg(con)
    mon_on = mcfg["mon_enabled"] == "1"
    mon = status_info(con, rows, mcfg) if mon_on else {}
    st_filter = request.args.get("st", "")
    switches = [r for r in rows if match(r) and (not st_filter or mon.get(r["id"], {}).get("state") == st_filter)]
    mon_hist = status_history(con, [r["id"] for r in switches]) if mon_on else {}
    mon_counts = {}
    for v in mon.values():
        mon_counts[v["state"]] = mon_counts.get(v["state"], 0) + 1
    port_hits = []
    if q and can("ports"):
        like = f"%{q}%"
        port_hits = con.execute(f"""
            SELECT p.*, s.name AS switch_name, s.id AS sid FROM ports p JOIN switches s ON s.id=p.switch_id
            WHERE (p.equipment LIKE ? OR p.patch_port LIKE ? OR p.patch_label LIKE ? OR p.user LIKE ?
               OR p.description LIKE ? OR p.remote_switch LIKE ? OR p.equipment2 LIKE ?){clause}
            ORDER BY s.name, p.idx LIMIT 200""", (like,) * 7 + params).fetchall()

    stats = {"sw": len(rows), "ports": 0, "active": 0, "disabled": 0, "trunks": 0}
    if rows:
        st = con.execute(f"""SELECT COUNT(*) ports, SUM(p.status='active') active, SUM(p.status='disabled') disabled,
                             SUM(p.ptype='Trunk') trunks FROM ports p JOIN switches s ON s.id=p.switch_id
                             WHERE 1=1{clause}""", params).fetchone()
        stats.update({k: st[k] or 0 for k in ("ports", "active", "disabled", "trunks")})
    return render_template("dashboard.html", switches=switches, areas=areas, area=area, q=q, group=group,
                           groups=visible_groups(), stats=stats, dup_ip=dup_ip, dup_serial=dup_serial,
                           port_hits=port_hits, mon_on=mon_on, mon=mon, mon_hist=mon_hist, mon_counts=mon_counts,
                           st_filter=st_filter, mon_labels=MON_STATES)


def split_groups(grp):
    return [dict(zip(("id", "name", "color"), x.split(":", 2))) for x in (grp or "").split("|") if x]


# ---------------------------------------------------------------- switches
def switch_or_404(sid, all_status=False):
    clause, params = vis("s", all_status)
    row = db().execute(f"SELECT * FROM switches s WHERE s.id=?{clause}", (sid, *params)).fetchone()
    if not row:
        abort(404)
    return row


def form_lists():
    con = db()
    return dict(
        creds=con.execute("SELECT id, name FROM cred_profiles ORDER BY name").fetchall() if can("credentials") else [],
        areas=[r[0] for r in con.execute("SELECT DISTINCT area FROM switches WHERE area<>'' ORDER BY area")],
        locations=[r[0] for r in con.execute("SELECT DISTINCT location FROM switches WHERE location<>'' ORDER BY location")],
        models=con.execute("SELECT * FROM models ORDER BY name COLLATE NOCASE").fetchall(),
        groups=visible_groups(),
        loc_list=con.execute("SELECT * FROM locations ORDER BY area COLLATE NOCASE, name COLLATE NOCASE").fetchall(),
        rack_list=con.execute("SELECT * FROM racks ORDER BY name COLLATE NOCASE").fetchall(),
        office_list=con.execute("SELECT * FROM offices ORDER BY name COLLATE NOCASE").fetchall(),
        layers=LAYERS,
    )


def back_to(default):
    nxt = request.values.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


def _date_or_blank(v):
    v = (v or "").strip()[:10]
    try:
        return datetime.date.fromisoformat(v).isoformat()
    except ValueError:
        return ""


@app.route("/switch/new", methods=["GET", "POST"])
@app.route("/switch/<int:sid>/edit", methods=["GET", "POST"])
@require("switches", "edit")
def switch_edit(sid=None):
    con = db()
    sw = switch_or_404(sid, all_status=True) if sid else None
    if sw and sw["status"] == "replaced":
        flash(_("This is archived hardware of a replaced switch and cannot be edited."), "error")
        return redirect(url_for("switch_view", sid=sid))
    n_ports = con.execute("SELECT COUNT(*) FROM ports WHERE switch_id=?", (sid,)).fetchone()[0] if sid else ""
    member = {r[0] for r in con.execute("SELECT group_id FROM switch_groups WHERE switch_id=?", (sid,))} if sid else set()
    if request.method == "POST":
        f = request.form
        vals = {k: f.get(k, "").strip() for k in ("name", "ip", "serial", "model", "area", "location",
                                                   "rack", "designation", "port_prefix", "notes")}
        vals["in_service"] = _date_or_blank(f.get("in_service"))
        vals["poe"] = 1 if f.get("poe") else 0
        vals["layer"] = f.get("layer") if f.get("layer") in dict(LAYERS) else "L3"
        vals["sfp"] = 1 if f.get("sfp") else 0
        vals["monitor"] = 1 if f.get("monitor") else 0
        try:
            vals["sfp_count"] = max(0, min(64, int(f.get("sfp_count") or 0))) if vals["sfp"] else 0
        except ValueError:
            vals["sfp_count"] = 0
        if vals["layer"] == "L1":
            vals["ip"] = ""  # unmanaged switch: no IP address
        if can("credentials"):
            vals["cred_profile_id"] = int(f["cred_profile_id"]) if f.get("cred_profile_id") else None
        try:
            count = int(f.get("port_count") or 0)
        except ValueError:
            count = 0
        chosen = {int(x) for x in f.getlist("group_ids")}
        allowed = my_group_ids()
        if allowed is not None:
            chosen &= allowed
        err = None
        if not vals["name"]:
            err = _("Switch name is required.")
        elif allowed is not None and not chosen:
            err = _("Choose at least one of your groups, otherwise you will not be able to see this switch.")
        elif assetlib.find_asset(con, vals["name"]) is not None:
            err = _("%(name)s is already the name of an asset - asset and switch names must be unique.", name=vals["name"])
        if err:
            flash(err, "error")
            return render_template("switch_form.html", sw=f, n_ports=count, member=chosen, **form_lists())
        changes = diff(sw, vals) if sw else {}
        vals["updated_at"] = now()
        try:
            if sw:
                cols = ", ".join(f"{k}=?" for k in vals)
                con.execute(f"UPDATE switches SET {cols} WHERE id=?", (*vals.values(), sid))
                if vals["name"] != sw["name"]:  # keep the links of other switches pointing to it
                    n = con.execute("UPDATE ports SET remote_switch=? WHERE remote_switch=?", (vals["name"], sw["name"])).rowcount
                    rename_switch_equipment(con, sw["name"], vals["name"])
                    if n:
                        changes["links_renamed"] = [None, n]
                if vals["port_prefix"] != (sw["port_prefix"] or ""):
                    for p in con.execute("SELECT id, idx, name FROM ports WHERE switch_id=?", (sid,)).fetchall():
                        if p["name"] == f"{sw['port_prefix'] or ''}{p['idx']}":
                            con.execute("UPDATE ports SET name=? WHERE id=?", (f"{vals['port_prefix']}{p['idx']}", p["id"]))
            else:
                vals["status"] = "active"
                cols = ", ".join(vals)
                sid = con.execute(f"INSERT INTO switches({cols}) VALUES ({','.join('?' * len(vals))})",
                                  tuple(vals.values())).lastrowid
            if vals["model"]:
                con.execute("INSERT OR IGNORE INTO models(name, poe) VALUES (?,?)", (vals["model"], vals["poe"]))
            editable = {gr["id"]: gr["name"] for gr in visible_groups()}
            before_groups = sorted(editable[x] for x in member if x in editable)
            for gid in editable:
                if gid in chosen:
                    con.execute("INSERT OR IGNORE INTO switch_groups(switch_id, group_id) VALUES (?,?)", (sid, gid))
                else:
                    con.execute("DELETE FROM switch_groups WHERE switch_id=? AND group_id=?", (sid, gid))
            after_groups = sorted(editable[x] for x in chosen if x in editable)
            if before_groups != after_groups:
                changes["groups"] = [", ".join(before_groups), ", ".join(after_groups)]
            if sw and count != n_ports:
                changes["ports"] = [n_ports, count]
            set_port_count(con, sid, count, vals["port_prefix"], default_port_type(con))
            if sw:
                if changes:
                    audit("documentation", "switch_update", "switch", sid, vals["name"], changes)
            else:
                audit("documentation", "switch_create", "switch", sid, vals["name"],
                      {k: [None, v] for k, v in vals.items() if v not in ("", None, 0) and k != "updated_at"}
                      | {"ports": [None, count], "groups": [None, ", ".join(after_groups)]})
            con.commit()
        except sqlite3.IntegrityError:
            con.rollback()
            flash(_("A switch named %(name)s already exists.", name=vals["name"]), "error")
            return render_template("switch_form.html", sw=f, n_ports=count, member=chosen, **form_lists())
        flash(_("Switch %(name)s saved.", name=vals["name"]), "ok")
        return redirect(back_to(url_for("switch_view", sid=sid)))
    return render_template("switch_form.html", sw=sw, n_ports=n_ports, member=member, **form_lists())


@app.route("/switch/<int:sid>/delete", methods=["POST"])
@require("switches", "modify")
def switch_delete(sid):
    sw = switch_or_404(sid, all_status=True)
    n = db().execute("SELECT COUNT(*) FROM ports WHERE switch_id=?", (sid,)).fetchone()[0]
    audit("documentation", "switch_delete", "switch", sid, sw["name"],
          {"ip": [sw["ip"], None], "serial": [sw["serial"], None], "model": [sw["model"], None], "ports": [n, None]})
    db().execute("DELETE FROM switches WHERE id=?", (sid,))
    db().commit()
    flash(_("Switch %(name)s deleted.", name=sw["name"]), "ok")
    return redirect(back_to(url_for("settings_switches")))


@app.route("/switch/<int:sid>/disable", methods=["POST"])
@require("switches", "modify")
def switch_disable(sid):
    con = db()
    sw = switch_or_404(sid, all_status=True)
    if sw["status"] != "active":
        abort(400)
    reason = request.form.get("reason", "").strip()[:500]
    con.execute("UPDATE switches SET status='disabled', status_changed_at=?, status_reason=? WHERE id=?",
                (now(), reason, sid))
    n = con.execute("""UPDATE change_requests SET status='cancelled', reviewed_by=?, reviewed_at=?, comment=?
                       WHERE switch_id=? AND status='pending'""",
                    (current_user()["id"], now(), "Switch disabled", sid)).rowcount
    audit("documentation", "switch_disable", "switch", sid, sw["name"],
          {"status": ["active", "disabled"], "reason": [None, reason]} | ({"cancelled_requests": [None, n]} if n else {}))
    con.commit()
    flash(_("Switch %(name)s disabled. It is hidden from the documentation and can be re-enabled from Settings › Switches.",
            name=sw["name"]), "ok")
    return redirect(back_to(url_for("settings_switches")))


@app.route("/switch/<int:sid>/enable", methods=["POST"])
@require("switches", "modify")
def switch_enable(sid):
    con = db()
    sw = switch_or_404(sid, all_status=True)
    if sw["status"] != "disabled":
        abort(400)
    con.execute("UPDATE switches SET status='active', status_changed_at=?, status_reason='' WHERE id=?", (now(), sid))
    audit("documentation", "switch_enable", "switch", sid, sw["name"], {"status": ["disabled", "active"]})
    con.commit()
    flash(_("Switch %(name)s re-enabled.", name=sw["name"]), "ok")
    return redirect(back_to(url_for("settings_switches")))


@app.route("/switch/<int:sid>/replace", methods=["GET", "POST"])
@require("switches", "modify")
def switch_replace(sid):
    """Swap the hardware of a switch. The switch keeps its name, groups, credentials, links and history;
    the old hardware (with a copy of its port documentation) is archived as a 'replaced' record."""
    con = db()
    sw = switch_or_404(sid, all_status=True)
    if sw["status"] != "active":
        flash(_("Only active switches can be replaced."), "error")
        return redirect(url_for("settings_switches"))
    ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,)).fetchall()
    documented = [p for p in ports if any(p[k] for k in ("equipment", "patch_port", "remote_switch", "user", "description"))]
    if request.method == "POST":
        f = request.form
        new = {"name": f.get("name", "").strip() or sw["name"], "model": f.get("model", "").strip(),
               "serial": f.get("serial", "").strip(), "ip": f.get("ip", "").strip(), "poe": 1 if f.get("poe") else 0,
               "layer": f.get("layer") if f.get("layer") in dict(LAYERS) else "L3", "sfp": 1 if f.get("sfp") else 0}
        try:
            new["sfp_count"] = max(0, min(64, int(f.get("sfp_count") or 0))) if new["sfp"] else 0
        except ValueError:
            new["sfp_count"] = 0
        if new["layer"] == "L1":
            new["ip"] = ""
        try:
            count = max(0, min(512, int(f.get("port_count") or 0)))
        except ValueError:
            count = len(ports)
        keep = f.get("ports_mode", "keep") == "keep"
        reason = f.get("reason", "").strip()[:500]
        if new["name"] != sw["name"] and con.execute("SELECT 1 FROM switches WHERE name=?", (new["name"],)).fetchone():
            flash(_("A switch named %(name)s already exists.", name=new["name"]), "error")
            return redirect(request.url)
        day = datetime.date.today().isoformat()
        archive_name, n = f"{sw['name']} [{day}]", 2
        while con.execute("SELECT 1 FROM switches WHERE name=?", (archive_name,)).fetchone():
            archive_name, n = f"{sw['name']} [{day} #{n}]", n + 1
        # 1. archive the old hardware with a snapshot of its ports
        cols = [c for c in sw.keys() if c not in ("id", "name", "status", "replaced_by", "status_changed_at", "status_reason")]
        aid = con.execute(f"INSERT INTO switches(name, status, replaced_by, status_changed_at, status_reason, {', '.join(cols)}) "
                          f"VALUES (?,?,?,?,?,{','.join('?' * len(cols))})",
                          (archive_name, "replaced", sid, now(), reason, *[sw[c] for c in cols])).lastrowid
        con.execute(f"""INSERT INTO ports(switch_id, idx, {', '.join(PORT_FIELDS)})
                        SELECT ?, idx, {', '.join(PORT_FIELDS)} FROM ports WHERE switch_id=?""", (aid, sid))
        con.execute("INSERT INTO switch_groups(switch_id, group_id) SELECT ?, group_id FROM switch_groups WHERE switch_id=?",
                    (aid, sid))
        # 2. put the new hardware on the live record
        new["in_service"] = _date_or_blank(f.get("in_service")) or datetime.date.today().isoformat()
        con.execute("UPDATE switches SET name=?, model=?, serial=?, ip=?, poe=?, layer=?, sfp=?, sfp_count=?, updated_at=?, "
                    "in_service=? WHERE id=?", (new["name"], new["model"], new["serial"], new["ip"], new["poe"], new["layer"],
                                                new["sfp"], new["sfp_count"], now(), new["in_service"], sid))
        if new["model"]:
            con.execute("INSERT OR IGNORE INTO models(name, poe) VALUES (?,?)", (new["model"], new["poe"]))
        cancelled = 0
        if not keep:
            cancelled = con.execute("""UPDATE change_requests SET status='cancelled', reviewed_by=?, reviewed_at=?,
                                       comment='Switch replaced' WHERE switch_id=? AND status='pending'""",
                                    (current_user()["id"], now(), sid)).rowcount
            con.execute(f"""UPDATE ports SET status='active', ptype=?, vlan='', patch_port='', patch_label='', equipment='',
                            eq_type='', eq2_type='', equipment2='', user='', remote_switch='', remote_port='', description='',
                            name=(SELECT COALESCE(s.port_prefix,'') FROM switches s WHERE s.id=ports.switch_id) || idx
                            WHERE switch_id=?""", (default_port_type(con), sid))
        set_port_count(con, sid, count, sw["port_prefix"] or "", default_port_type(con))
        renamed = 0
        if new["name"] != sw["name"] and f.get("update_links"):
            renamed = con.execute("UPDATE ports SET remote_switch=? WHERE remote_switch=? AND switch_id<>?",
                                  (new["name"], sw["name"], aid)).rowcount
            rename_switch_equipment(con, sw["name"], new["name"], aid)
        details = diff(sw, new) | {"ports": [len(ports), count], "port_documentation": [None, "kept" if keep else "cleared"],
                                   "archived_as": [None, archive_name], "reason": [None, reason]}
        if renamed:
            details["links_renamed"] = [None, renamed]
        if cancelled:
            details["cancelled_requests"] = [None, cancelled]
        audit("documentation", "switch_replace", "switch", sid, new["name"], details)
        con.commit()
        flash(_("Switch replaced. The old hardware is archived as %(name)s.", name=archive_name), "ok")
        return redirect(url_for("switch_view", sid=sid))
    lost = [p for p in documented]
    return render_template("switch_replace.html", today=datetime.date.today().isoformat(), sw=sw, ports=ports, documented=documented, lost=lost, layers=LAYERS,
                           models=con.execute("SELECT * FROM models ORDER BY name COLLATE NOCASE").fetchall())


def pending_for_ports(con, port_ids):
    """{port_id: {field: {"value":..., "by":..., "req": id}}} for pending change requests."""
    out = {}
    if not port_ids:
        return out
    rows = con.execute(f"""SELECT c.*, u.username, u.first_name, u.last_name FROM change_requests c
                           LEFT JOIN users u ON u.id=c.requested_by
                           WHERE c.status='pending' AND c.port_id IN ({','.join('?' * len(port_ids))})
                           ORDER BY c.id""", list(port_ids)).fetchall()
    for r in rows:
        who = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or r["username"] or "?"
        for k, v in json.loads(r["fields"]).items():
            out.setdefault(r["port_id"], {})[k] = {"value": v, "by": who, "req": r["id"], "at": r["requested_at"]}
    return out


def switch_history(con, sid, limit=200):
    return [dict(r, details=json.loads(r["details"] or "{}")) for r in con.execute(
        "SELECT * FROM audit WHERE target_type='switch' AND target_id=? ORDER BY id DESC LIMIT ?", (sid, limit))]


@app.route("/switch/<int:sid>")
@require("switches", "view")
def switch_view(sid):
    con = db()
    sw = switch_or_404(sid, all_status=can("switches", "edit"))
    active = sw["status"] == "active"
    show_ports = can("ports")
    ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,)).fetchall() if show_ports else []
    cred = con.execute("SELECT * FROM cred_profiles WHERE id=?", (sw["cred_profile_id"],)).fetchone() \
        if sw["cred_profile_id"] and can("credentials") and active else None
    clause, params = vis("s")
    names = {r["name"]: r["id"] for r in con.execute(f"SELECT s.id, s.name FROM switches s WHERE 1=1{clause}", params)}
    vlans = con.execute("SELECT number, name FROM vlans ORDER BY number").fetchall()
    ptypes = con.execute("SELECT * FROM port_types ORDER BY sort, name").fetchall()
    model = con.execute("SELECT * FROM models WHERE name=?", (sw["model"],)).fetchone() if sw["model"] else None
    groups = con.execute("""SELECT g.* FROM sw_groups g JOIN switch_groups sg ON sg.group_id=g.id
                            WHERE sg.switch_id=? ORDER BY g.name""", (sid,)).fetchall()
    incoming = con.execute(f"""SELECT p.*, s.name AS switch_name, s.id AS sid FROM ports p
        JOIN switches s ON s.id=p.switch_id WHERE p.remote_switch=? AND s.id<>?{clause} ORDER BY s.name, p.idx""",
                           (sw["name"], sid, *params)).fetchall() if show_ports and active else []
    pending = pending_for_ports(con, [p["id"] for p in ports]) if active else {}
    sfp_cnt = (sw["sfp_count"] or (model["sfp_count"] if model and model["sfp"] else 0)) if sw["sfp"] else 0
    sfp_n = sfp_cnt if sfp_cnt and sfp_cnt < len(ports) else 0
    archived = con.execute("SELECT * FROM switches WHERE replaced_by=? ORDER BY id DESC", (sid,)).fetchall() \
        if can("switches", "edit") else []
    successor = con.execute("SELECT id, name FROM switches WHERE id=?", (sw["replaced_by"],)).fetchone() \
        if sw["replaced_by"] else None
    history = switch_history(con, sid, 30) if can("audit") or can("switches", "edit") else []
    ticket, ticket_sw = None, None
    if request.args.get("ticket", type=int) and active:
        ticket = work_ticket(con, request.args.get("ticket", type=int))
        if ticket is not None:
            ticket_sw = con.execute("SELECT * FROM ticket_switches WHERE ticket_id=? AND switch_id=?",
                                    (ticket["id"], sid)).fetchone()
            if ticket_sw is None:
                ticket = None
    atypes = con.execute("SELECT * FROM asset_types ORDER BY system DESC, sort, name").fetchall()
    assets_js = {}
    if show_ports and active and can("ports", "edit"):
        used = {}
        tk = request.args.get("ticket", type=int)
        tk = tk if tk and work_ticket(con, tk) is not None else None
        gone = released_pairs(con, tk, current_user()["id"]) if (tk or perm("ports") < LEVELS["modify"]) else set()
        for u in con.execute("""SELECT p.id, p.equipment, p.equipment2, s.name AS sw, p.name FROM ports p
                                JOIN switches s ON s.id=p.switch_id WHERE s.status<>'replaced'
                                AND (p.equipment<>'' OR p.equipment2<>'')"""):
            for n in (u["equipment"], u["equipment2"]):
                if n and (u["id"], n.lower()) not in gone:
                    used.setdefault(n.lower(), []).append([u["id"], f"{u['sw']} {u['name']}"])
        for r in con.execute("""SELECT a.name, a.ip, a.state, t.name AS t FROM assets a JOIN asset_types t ON t.id=a.type_id
                                ORDER BY a.name COLLATE NOCASE"""):
            # [name, ip, [[port id, "SW port"], ...], state]
            assets_js.setdefault(r["t"], []).append([r["name"], r["ip"] or "", used.get(r["name"].lower(), []),
                                                     r["state"] or "stock"])
        assets_js[SWITCH_TYPE] = [[n, ""] for n in sorted(names, key=str.lower) if n != sw["name"]]
    # ports of every switch the user can see, for the switch-to-switch link picker
    link_ports = {}
    if show_ports and active and can("ports", "edit"):
        for r in con.execute(f"""SELECT s.name AS sw, p.name, p.remote_switch, p.remote_port, p.equipment, p.status
                                 FROM ports p JOIN switches s ON s.id=p.switch_id WHERE s.id<>?{clause}
                                 ORDER BY s.name, p.idx""", (sid, *params)):
            info = f"↔ {r['remote_switch']} {r['remote_port'] or ''}".strip() if r["remote_switch"] else (r["equipment"] or "")
            if r["status"] == "disabled":
                info = (info + " · " if info else "") + _("disabled")
            occupied = 1 if r["status"] == "disabled" or (r["equipment"] and not r["remote_switch"]) else 0
            link_ports.setdefault(r["sw"], []).append([r["name"], info, r["remote_switch"] or "", r["remote_port"] or "",
                                                       occupied])
    # connections of this switch, checked against the other end
    connections = []
    if show_ports and active:
        local_names = {}
        for p in ports:
            local_names[p["name"]] = p
            local_names[str(p["idx"])] = p
        for p in ports:
            if not p["remote_switch"]:
                continue
            rs, rp = p["remote_switch"], p["remote_port"] or ""
            state = "device"
            if rs in names:
                far = _find_port(con, rs, rp)
                if far is None:
                    state = "missing"
                elif far["remote_switch"] == sw["name"] and (far["remote_port"] or "") in (p["name"], str(p["idx"])):
                    state = "ok"
                else:
                    state = "conflict"
            connections.append({"port": p["name"], "pid": p["id"], "remote": rs, "rsid": names.get(rs), "rport": rp,
                                "type": p["ptype"], "status": p["status"], "state": state, "speed": p["speed"]})
        for q in incoming:  # other switches pointing here, not documented on this side
            mine = local_names.get(q["remote_port"] or "")
            if mine is None or mine["remote_switch"] != q["switch_name"]:
                connections.append({"port": q["remote_port"] or "?", "pid": mine["id"] if mine else None,
                                    "remote": q["switch_name"], "rsid": q["sid"], "rport": q["name"], "type": q["ptype"],
                                    "status": q["status"], "state": "incoming", "speed": q["speed"]})
        connections.sort(key=lambda c: (int("".join(ch for ch in c["port"] if ch.isdigit()) or 0), c["port"]))
    mon, mon_day, mon_avail, mon_outages, mon_events, mon_links, mon_interval = None, [], None, 0, [], [], 60
    mcfg = mon_cfg(con)
    if mcfg["mon_enabled"] == "1" and active:
        mon = status_info(con, [sw], mcfg)[sw["id"]]
        mon_interval = mon_int(mcfg, "mon_interval", 30, 3600)
        if mon["state"] != "off":
            mon_day, mon_avail, mon_outages = status_timeline(con, sw["id"])
            mon_events = con.execute("SELECT * FROM status_events WHERE switch_id=? ORDER BY ts DESC LIMIT 8",
                                     (sw["id"],)).fetchall()
            links = neighbour_links(con, sw["id"])
            if links:
                linfo = status_info(con, con.execute(f"SELECT id, ip, layer, status, monitor FROM switches WHERE id IN "
                                                     f"({','.join(str(int(l['sid'])) for l in links)})").fetchall(), mcfg)
                mon_links = [dict(l, state=linfo.get(l["sid"], {}).get("state", "off")) for l in links]
    return render_template("switch_view.html", mon=mon, mon_day=mon_day, mon_avail=mon_avail, mon_outages=mon_outages,
                           mon_events=mon_events, mon_links=mon_links, mon_interval=mon_interval,
                           mon_labels=MON_STATES, link_ports=link_ports, connections=connections, sw=sw, ports=ports, cred=cred, switch_ids=names, vlans=vlans,
                           incoming=incoming, port_types=ptypes, type_colors={t["name"]: t["color"] for t in ptypes},
                           statuses=STATUSES, status_labels=dict(STATUSES),
                           cred_fields=cred_fields_of(con, cred["id"]) if cred else [], secret_kinds=SECRET_KINDS,
                           ssh_user=next((f["value"] for f in (cred_fields_of(con, cred["id"]) if cred else [])
                                          if f["kind"] == "user" and "ssh" in f["label"].lower()), ""),
                           model=model, active=active,
                           groups=groups, pending=pending, sfp_n=sfp_n, field_labels=PORT_FIELD_LABELS,
                           layers=dict(LAYERS), archived=archived, successor=successor, history=history,
                           actions=AUDIT_ACTIONS, all_labels=all_field_labels(),
                           asset_types=atypes, atype_colors={t["name"]: t["color"] for t in atypes},
                           asset_types_js=[{"name": t["name"], "kind": t["kind"], "color": t["color"],
                                            "multi": t["multi"], "icon": t["icon"] or "other"} for t in atypes],
                           atype_icons={t["name"]: t["icon"] or KIND_ICONS.get(t["kind"], "other") for t in atypes},
                           assets_js=assets_js, switch_type=SWITCH_TYPE, vlan_numbers={str(v["number"]) for v in vlans},
                           cleared_when_disabled=CLEARED_WHEN_DISABLED, speeds=SPEEDS, ticket=ticket,
                           ticket_sw=ticket_sw, ticket_progress=ticket_progress(con, ticket["id"]) if ticket else None,
                           active_tickets=con.execute("""SELECT t.id, t.title, t.status FROM tickets t JOIN ticket_switches ts
                               ON ts.ticket_id=t.id WHERE ts.switch_id=? AND t.status IN ('open','in_progress','pending_validation')""",
                                                      (sid,)).fetchall() if can("maintenance") else [])


BOOL_FIELDS = {"multi", "active", "is_admin", "all_groups", "poe", "dhcp", "sfp", "secrets"}
VALUE_LABELS = {"kept": N_("kept"), "cleared": N_("cleared"), "merge": N_("merge"), "replace": N_("replace everything"),
                "active": N_("Active"), "disabled": N_("Disabled"), "replaced": N_("Replaced"),
                "none": N_("No access"), "view": N_("View"), "edit": N_("Edit"), "modify": N_("Modify"),
                "open": N_("Open"), "in_progress": N_("In progress"), "pending_validation": N_("Pending validation"),
                "validated": N_("Validated (closed)"), "cancelled": N_("Cancelled"),
                "stock": N_("In stock"), "defective": N_("Defective"), "in_use": N_("In use"),
                "fast": N_("Fast Ethernet"), "gigabit": N_("Gigabit"), "fiber": N_("Fiber")}


@app.context_processor
def _value_helpers():
    def show(field, v):
        """Human, translated rendering of a value stored in the audit log."""
        if v is None or v == "":
            return ""
        if field in BOOL_FIELDS or isinstance(v, bool):
            return _("Yes") if v in (1, True, "1") else _("No")
        if isinstance(v, str) and v in VALUE_LABELS and (field in ("status", "port_documentation", "mode", "speed", "state")
                                                         or field.startswith("perm_")):
            return _(VALUE_LABELS[v])
        if field == "lang" and v in LANGUAGES:
            return LANGUAGES[v]
        if isinstance(v, dict):
            return ", ".join(f"{k}: {x}" for k, x in v.items())
        return v
    return {"show": show}


def all_field_labels():
    return {**PORT_FIELD_LABELS, **SWITCH_FIELD_LABELS, **CRED_LABELS,
            "reason": N_("Reason"), "archived_as": N_("Archived as"), "port_documentation": N_("Port documentation"),
            "links_renamed": N_("Links renamed on other switches"), "cancelled_requests": N_("Pending requests cancelled"),
            "comment": N_("Comment"), "port": N_("Port"), "profile": N_("Authorisation profile"),
            "is_admin": N_("Administrator"), "all_groups": N_("Can see all switches"), "active": N_("Account active"),
            "first_name": N_("First name"), "last_name": N_("Last name"), "email": N_("Email"), "title": N_("Title"),
            "username": N_("Username"), "password": N_("Password"), "lang": N_("Language"), "switches": N_("Switches"),
            "color": N_("Colour"), "number": N_("VLAN"), "subnet": N_("Subnet / gateway"), "dhcp": N_("DHCP"),
            "vendor": N_("Vendor"), "layer": N_("Layer"), "sfp": N_("SFP ports"), "sfp_count": N_("Number of SFP ports"),
            "port_count": N_("Default number of ports"), "sort": N_("Order"), "file": N_("File"), "mode": N_("Mode"),
            "result": N_("Result"), "nodes": N_("Switches"), "secrets": N_("Including passwords"),
            "filters": N_("Filters"), "rows": N_("Rows"), "perms": N_("Permissions"), "before": N_("Before"),
            "link_sync": N_("Updated automatically from the other end of the link"), "name": N_("Name"),
            "description": N_("Description"), "mac": N_("MAC address"), "ticket": N_("Maintenance ticket"),
            "assignees": N_("Assigned technicians"), "planned_start": N_("Planned start"), "planned_end": N_("Planned end"),
            "title": N_("Title"),
            "session_timeout": N_("Session timeout (minutes)"), "pw_min": N_("Minimum password length"),
            "pw_upper": N_("Uppercase letter required"), "pw_lower": N_("Lowercase letter required"),
            "pw_digit": N_("Digit required"), "pw_special": N_("Special character required"),
            "must_change": N_("Must change the password at next sign-in"), "kind": N_("Shape in the topology"),
            "type_id": N_("Equipment type"), "state": N_("Status"), "icon": N_("Logo"),
            "multi": N_("Can be connected to several ports"),
            **{f"perm_{k}": label for k, label, _h in MENUS}}


@app.route("/switch/<int:sid>/export.xlsx")
@require("switches", "view")
def switch_export(sid):
    import importer
    con = db()
    sw = switch_or_404(sid, all_status=can("switches", "edit"))
    include = request.args.get("secrets") == "1" and can("passwords")
    ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,)).fetchall() if can("ports") else []
    total = len(ports)
    only = {int(x) for x in request.args.get("ports", "").split(",") if x.strip().isdigit()}
    if only:  # export only the rows shown by the filter on the switch page
        ports = [p for p in ports if p["id"] in only]
    cred = con.execute("SELECT * FROM cred_profiles WHERE id=?", (sw["cred_profile_id"],)).fetchone() \
        if sw["cred_profile_id"] and can("credentials") else None
    groups = [r[0] for r in con.execute("""SELECT g.name FROM sw_groups g JOIN switch_groups sg ON sg.group_id=g.id
                                           WHERE sg.switch_id=? ORDER BY g.name""", (sid,))]
    model = con.execute("SELECT * FROM models WHERE name=?", (sw["model"],)).fetchone() if sw["model"] else None
    clause, params = vis("s")
    incoming = con.execute(f"""SELECT p.*, s.name AS switch_name FROM ports p JOIN switches s ON s.id=p.switch_id
                               WHERE p.remote_switch=? AND s.id<>?{clause} ORDER BY s.name, p.idx""",
                           (sw["name"], sid, *params)).fetchall()
    history = switch_history(con, sid, 1000) if can("audit") or can("switches", "edit") else []
    type_colors = {t["name"]: t["color"] for t in con.execute("SELECT name, color FROM port_types")}
    buf = importer.export_switch(sw, ports, cred, groups, model, incoming, history, include, dec, _,
                                 labels={**CRED_LABELS, **PORT_FIELD_LABELS, **SWITCH_FIELD_LABELS},
                                 actions=AUDIT_ACTIONS, status_labels=dict(SWITCH_STATUSES + STATUSES),
                                 all_labels=all_field_labels(), show=_value_helpers()["show"],
                                 type_colors=type_colors, total_ports=total,
                                 cred_fields=cred_fields_of(con, cred["id"]) if cred else [])
    audit("access", "export_switch", "switch", sid, sw["name"],
          {"secrets": [None, include]} | ({"rows": [None, f"{len(ports)} / {total}"]} if only else {}))
    con.commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sw["name"])
    return send_file(buf, as_attachment=True, download_name=f"{safe}-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/switch/<int:sid>/print")
@require("switches", "view")
def switch_print(sid):
    """Printable layout of a switch (front panel + port table + verification boxes), for checks on site."""
    con = db()
    sw = switch_or_404(sid, all_status=can("switches", "edit"))
    ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,)).fetchall() if can("ports") else []
    total = len(ports)
    only = {int(x) for x in request.args.get("ports", "").split(",") if x.strip().isdigit()}
    if only:
        ports = [p for p in ports if p["id"] in only]
    groups = [r[0] for r in con.execute("""SELECT g.name FROM sw_groups g JOIN switch_groups sg ON sg.group_id=g.id
                                           WHERE sg.switch_id=? ORDER BY g.name""", (sid,))]
    ptypes = {t["name"]: t["color"] for t in con.execute("SELECT name, color FROM port_types")}
    model = con.execute("SELECT * FROM models WHERE name=?", (sw["model"],)).fetchone() if sw["model"] else None
    sfp_cnt = (sw["sfp_count"] or (model["sfp_count"] if model and model["sfp"] else 0)) if sw["sfp"] else 0
    ticket = request.args.get("ticket", type=int)
    audit("access", "print_switch", "switch", sid, sw["name"], {"rows": [None, f"{len(ports)} / {total}"]})
    con.commit()
    return render_template("switch_print.html", sw=sw, ports=ports, total=total, groups=groups, type_colors=ptypes,
                           sfp_n=sfp_cnt if sfp_cnt and sfp_cnt < total else 0, all_ports=len(ports) == total,
                           speeds=dict(SPEEDS), printed=now(), ticket=ticket)


# ---------------------------------------------------------------- switch list (summary without ports): Excel + print
LIST_COLUMNS = [("name", N_("Switch name")), ("model", N_("Model")), ("serial", N_("Serial number")),
                ("ip", N_("IP address")), ("location", N_("Location")), ("rack", N_("Rack identifier")),
                ("n_ports", N_("Number of ports")), ("poe", N_("PoE")), ("in_service", N_("In service since"))]


def switch_list_rows():
    """Selected switches (ids=…) or the switches of the current filter of the Switches page (q, area, group)."""
    con = db()
    clause, params = vis("s")
    where, args = "1=1", []
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    if ids:
        where += f" AND s.id IN ({','.join('?' * len(ids))})"
        args += ids
    else:
        if request.args.get("area"):
            where += " AND s.area=?"
            args.append(request.args["area"])
        if request.args.get("group", "").isdigit():
            where += " AND EXISTS (SELECT 1 FROM switch_groups sg WHERE sg.switch_id=s.id AND sg.group_id=?)"
            args.append(int(request.args["group"]))
        q = request.args.get("q", "").strip()
        if q:
            like = f"%{q}%"
            where += " AND (s.name LIKE ? OR s.ip LIKE ? OR s.serial LIKE ? OR s.model LIKE ? OR s.location LIKE ? OR s.designation LIKE ?)"
            args += [like] * 6
    return con.execute(f"""SELECT s.*, (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id) AS n_ports
                           FROM switches s WHERE {where}{clause} ORDER BY s.area COLLATE NOCASE, s.name COLLATE NOCASE""",
                       (*args, *params)).fetchall()


@app.route("/switches/list.xlsx")
@require("switches", "view")
def switches_list_export():
    import importer
    rows = switch_list_rows()
    buf = importer.export_switch_list(rows, _, LIST_COLUMNS)
    audit("access", "export_switch_list", "workbook", None, "", {"rows": [None, len(rows)]})
    db().commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, as_attachment=True, download_name=f"switch-list-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/switches/list/print")
@require("switches", "view")
def switches_list_print():
    rows = switch_list_rows()
    return render_template("switch_list_print.html", rows=rows, cols=LIST_COLUMNS, printed=now(),
                           filtered=bool(request.args.get("ids") or request.args.get("q") or request.args.get("area")
                                         or request.args.get("group")))


@app.route("/switches/export.xlsx")
@require("switches", "view")
def switches_export():
    """Several switches selected on the Switches page: one sheet per switch, laid out like the switch page."""
    import importer
    con = db()
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()][:500]
    clause, params = vis("s", all_status=can("switches", "edit"))
    rows = con.execute(f"SELECT * FROM switches s WHERE s.id IN ({','.join('?' * len(ids)) or 'NULL'}){clause} "
                       "ORDER BY s.area COLLATE NOCASE, s.name COLLATE NOCASE", (*ids, *params)).fetchall()
    if not rows:
        flash(_("Select at least one switch to export."), "error")
        return redirect(url_for("dashboard"))
    items = []
    for sw in rows:
        ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sw["id"],)).fetchall() if can("ports") else []
        groups = [r[0] for r in con.execute("""SELECT g.name FROM sw_groups g JOIN switch_groups sg ON sg.group_id=g.id
                                               WHERE sg.switch_id=? ORDER BY g.name""", (sw["id"],))]
        model = con.execute("SELECT * FROM models WHERE name=?", (sw["model"],)).fetchone() if sw["model"] else None
        items.append((sw, ports, groups, model))
    type_colors = {t["name"]: t["color"] for t in con.execute("SELECT name, color FROM port_types")}
    buf = importer.export_switches(items, _, type_colors, dict(SWITCH_STATUSES + STATUSES))
    audit("access", "export_switches", "workbook", None, "", {"switches": [None, ", ".join(r["name"] for r in rows)]})
    con.commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, as_attachment=True, download_name=f"switches-{len(rows)}-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- port API (with approval workflow)
class PortError(Exception):
    pass


def asset_type_names(con):
    return {r[0].lower(): r[0] for r in con.execute("SELECT name FROM asset_types")}


def _clean_port_values(data, port=None):
    """Validate the posted values and derive the dependent fields:
    - a Disabled port has no patch panel, equipment, user or link documented
    - Equipment type Switch -> the equipment is a switch: it is also the remote end of a switch-to-switch link
    - the equipment must exist as an asset of the chosen type (new names are created when the change is applied)"""
    con = db()
    types = {r[0] for r in con.execute("SELECT name FROM port_types")}
    atypes = asset_type_names(con)
    out = {}
    for k, v in data.items():
        if k not in PORT_FIELDS or k == "remote_switch":
            continue
        v = (str(v) if v is not None else "").strip()[:500]
        if k == "status" and v not in dict(STATUSES):
            continue
        if k == "ptype" and v not in types:
            continue
        if k == "speed" and v not in dict(SPEEDS):
            continue
        if k == "name" and not v:
            continue
        if k in ("eq_type", "eq2_type") and v:
            if v.lower() not in atypes:
                raise PortError(_("Unknown equipment type: %(t)s", t=v))
            v = atypes[v.lower()]
        out[k] = v
    if port is None:
        return out
    cur = {k: (port[k] or "") for k in PORT_FIELDS}
    new = {**cur, **out}
    if new["status"] == "disabled" and ("status" in out or cur["status"] == "disabled"):
        for k in CLEARED_WHEN_DISABLED:
            new[k] = ""
    for t, e in (("eq_type", "equipment"), ("eq2_type", "equipment2")):
        if new[e] and not new[t]:  # a name typed without a type: use the asset's own type (or guess it)
            a = assetlib.find_asset(con, new[e])
            sw = assetlib.switch_name(con, new[e])
            new[t] = SWITCH_TYPE if sw else (a["type_name"] if a else assetlib.type_for_kind(con, _kind_guess(new[e])))
        if not new[e] and t == "eq2_type":
            new[t] = ""
        if new[t] == SWITCH_TYPE and new[e]:
            sw = assetlib.switch_name(con, new[e])
            if sw:
                new[e] = sw
        elif new[t] and new[e]:
            a = assetlib.find_asset(con, new[e])
            if assetlib.switch_name(con, new[e]):
                raise PortError(_("%(name)s is a switch: choose the equipment type Switch.", name=new[e]))
            if a is not None and a["type_name"] != new[t]:
                raise PortError(_("%(name)s already exists as %(t)s - asset names are unique.", name=new[e], t=a["type_name"]))
            if a is not None:
                new[e] = a["name"]
            if new[e].lower() != cur[e].lower() or new[t] != cur[t]:
                check_asset_free(con, new[t], new[e], port["id"])
    if new["eq_type"] == SWITCH_TYPE:
        new["remote_switch"] = new["equipment"]
        new["eq2_type"] = new["equipment2"] = ""
    else:
        new["remote_switch"] = ""
    if new["remote_switch"] and new["remote_port"] and \
            (new["remote_switch"], new["remote_port"]) != (cur["remote_switch"], cur["remote_port"]):
        check_link_free(con, port["sw_name"], {**new, "idx": port["idx"]}, port["id"])
    return {k: v for k, v in new.items() if k in out or v != cur[k]}


def check_link_free(con, sw_name, vals, port_id):
    """A port of another switch can only be at the end of one link."""
    rs, rp = vals.get("remote_switch") or "", vals.get("remote_port") or ""
    if not rs or not rp:
        return
    me = {vals.get("name") or "", str(vals.get("idx") or "")} - {""}
    if port_id:
        row = con.execute("SELECT name, idx FROM ports WHERE id=?", (port_id,)).fetchone()
        if row:
            me |= {row["name"], str(row["idx"])}
    far = _find_port(con, rs, rp)
    if far is None:
        return
    where = f"{rs} {_('port')} {far['name']}"
    if far["remote_switch"] and not (far["remote_switch"] == sw_name and (far["remote_port"] or "") in me):
        raise PortError(_("%(where)s is already linked to %(other)s - a port can only be used by one link. "
                          "Free it first.", where=where, other=f"{far['remote_switch']} {far['remote_port']}".strip()))
    if far["equipment"] and far["eq_type"] != SWITCH_TYPE:
        raise PortError(_("%(where)s is already used by %(other)s - a port can only be used once. Free it first.",
                          where=where, other=far["equipment"]))
    if far["status"] == "disabled":
        raise PortError(_("%(where)s is disabled - enable it before linking it.", where=where))
    other = con.execute("""SELECT s.name AS sw, p.name FROM ports p JOIN switches s ON s.id=p.switch_id
                           WHERE p.remote_switch=? AND p.remote_port IN (?, ?) AND p.id<>? AND s.status='active'
                           AND NOT (s.name=? AND p.id=?) LIMIT 1""",
                        (rs, far["name"], str(far["idx"]), port_id or -1, sw_name, port_id or -1)).fetchone()
    if other is not None:
        raise PortError(_("%(where)s is already claimed by %(other)s - a port can only be used by one link.",
                          where=where, other=f"{other['sw']} {_('port')} {other['name']}"))


def _pending_scope_sql(ticket_id, user_id):
    if ticket_id:
        return "c.ticket_id=?", (ticket_id,)
    return "c.ticket_id IS NULL AND c.requested_by=?", (user_id,)


def releasing_requests(con, name, ticket_id=None, user_id=None):
    """Pending change requests (same maintenance ticket, or the same person's own requests) that take the asset
    `name` off the port it is connected to now: {port id: request row}."""
    where, params = _pending_scope_sql(ticket_id, user_id)
    out = {}
    for r in con.execute(f"""SELECT c.*, p.equipment AS cur_eq, p.equipment2 AS cur_eq2, s.name AS sw_name, p.name AS port_name
                             FROM change_requests c JOIN ports p ON p.id=c.port_id JOIN switches s ON s.id=c.switch_id
                             WHERE c.status='pending' AND {where}
                             AND (p.equipment=? COLLATE NOCASE OR p.equipment2=? COLLATE NOCASE)""",
                         (*params, name, name)).fetchall():
        fields = json.loads(r["fields"] or "{}")
        disabled = fields.get("status") == "disabled"
        new_eq = "" if disabled else fields.get("equipment", r["cur_eq"]) or ""
        new_eq2 = "" if disabled else fields.get("equipment2", r["cur_eq2"]) or ""
        if name.lower() not in (new_eq.lower(), new_eq2.lower()):
            out[r["port_id"]] = r
    return out


def released_pairs(con, ticket_id=None, user_id=None):
    """{(port id, asset name lower)} given up by the pending requests of a ticket / of one person."""
    where, params = _pending_scope_sql(ticket_id, user_id)
    out = set()
    for r in con.execute(f"""SELECT c.fields, c.port_id, p.equipment, p.equipment2 FROM change_requests c
                             JOIN ports p ON p.id=c.port_id WHERE c.status='pending' AND {where}""", params):
        f = json.loads(r["fields"] or "{}")
        dis = f.get("status") == "disabled"
        new = {("" if dis else f.get("equipment", r["equipment"]) or "").lower(),
               ("" if dis else f.get("equipment2", r["equipment2"]) or "").lower()}
        for cur in (r["equipment"], r["equipment2"]):
            if cur and cur.lower() not in new:
                out.add((r["port_id"], cur.lower()))
    return out


def released_ports(con, name):
    """Ports giving this asset up in the current operation: the ports changed in the same save, plus pending
    requests of the same ticket / of the same person (they will be applied together)."""
    ctx = g.get("asset_ctx") or {}
    out = {pid for pid, (eq, eq2) in ctx.get("batch", {}).items() if name.lower() not in (eq.lower(), eq2.lower())}
    u = current_user()
    if ctx.get("ticket") or (u is not None and perm("ports") < LEVELS["modify"]):
        out |= set(releasing_requests(con, name, ctx.get("ticket"), u["id"] if u else None))
    return out


def set_batch_context(con, changes, ticket_id=None):
    """Remember the new equipment of every port saved together, so an asset can move between them."""
    batch = {}
    for pid, vals in changes.items():
        row = con.execute("SELECT equipment, equipment2, status FROM ports WHERE id=?", (int(pid),)).fetchone()
        if row is None or not isinstance(vals, dict):
            continue
        dis = (vals.get("status", row["status"]) == "disabled")
        batch[int(pid)] = ("" if dis else str(vals.get("equipment", row["equipment"]) or ""),
                           "" if dis else str(vals.get("equipment2", row["equipment2"]) or ""))
    g.asset_ctx = {"batch": batch, "ticket": ticket_id}


def check_asset_free(con, type_name, name, port_id, released=None):
    if released is None:
        released = released_ports(con, name)
    c = assetlib.usage_conflict(con, type_name, name, port_id, released)
    if c and c[0] == "defective":
        raise PortError(_("%(name)s is marked Defective in Assets - it cannot be connected.", name=name))
    if c:
        raise PortError(_("%(name)s is already connected to %(where)s - an asset of type %(t)s can only be used once "
                          "(remove it there first, or allow several ports for this type in Assets).",
                          name=name, where=c[1], t=type_name))


def _kind_guess(text):
    k = assetlib.classify(text)
    return "hub" if k == "switch" else k


def create_assets_for(con, vals, sw_name, port_name):
    """Create the assets named on a port that do not exist yet (when the change is applied)."""
    for t, e in (("eq_type", "equipment"), ("eq2_type", "equipment2")):
        if vals.get(e) and vals.get(t) and vals[t] != SWITCH_TYPE and assetlib.find_asset(con, vals[e]) is None:
            try:
                assetlib.ensure_asset(con, vals[t], vals[e])
            except ValueError:
                continue
            row = assetlib.find_asset(con, vals[e])
            audit("documentation", "asset_create", "asset", row["id"], row["name"],
                  {"eq_type": [None, vals[t]], "port": [None, f"{sw_name} {port_name}"]})


def _find_port(con, switch_name, port_ref):
    """Port of an active switch, by port name or number."""
    if not switch_name or not port_ref:
        return None
    return con.execute("""SELECT p.*, s.name AS sw_name FROM ports p JOIN switches s ON s.id=p.switch_id
                          WHERE s.name=? AND s.status='active' AND (p.name=? OR CAST(p.idx AS TEXT)=?)
                          ORDER BY p.name=? DESC LIMIT 1""", (switch_name, port_ref, port_ref, port_ref)).fetchone()


def rename_switch_equipment(con, old, new, skip_switch=None):
    """A switch was renamed: ports that name it as connected equipment follow."""
    for e, t in (("equipment", "eq_type"), ("equipment2", "eq2_type")):
        con.execute(f"UPDATE ports SET {e}=? WHERE {e}=? AND {t}=? AND switch_id<>?",
                    (new, old, SWITCH_TYPE, skip_switch or -1))


CLEAR_LINK_SQL = f"""UPDATE ports SET remote_switch='', remote_port='',
    equipment=CASE WHEN eq_type='{SWITCH_TYPE}' THEN '' ELSE equipment END,
    eq_type=CASE WHEN eq_type='{SWITCH_TYPE}' THEN '' ELSE eq_type END WHERE id=?"""


def sync_link(con, sw_name, before, after):
    """Keep both ends of a switch-to-switch link documented.
    before / after: dicts with name, remote_switch, remote_port of the local port.
    - the port at the old far end that pointed back to us is cleared
    - the port at the new far end is set to point back to us"""
    old = (before.get("remote_switch") or "", before.get("remote_port") or "")
    new = (after.get("remote_switch") or "", after.get("remote_port") or "")
    me_names = {before.get("name") or "", after.get("name") or "", str(before.get("idx") or "")} - {""}
    if old == new and before.get("name") == after.get("name"):
        return
    linked = g.setdefault("linked", [])
    if old[0] and old != new:
        far = _find_port(con, old[0], old[1])
        if far is not None and far["remote_switch"] == sw_name and (far["remote_port"] or "") in me_names:
            con.execute(CLEAR_LINK_SQL, (far["id"],))
            audit("documentation", "port_update", "switch", far["switch_id"], far["sw_name"],
                  {"port": [None, far["name"]], "remote_switch": [sw_name, ""], "remote_port": [far["remote_port"], ""],
                   "link_sync": [None, f"{sw_name} {after.get('name')}"]})
            linked.append(f"{far['sw_name']} {_('port')} {far['name']} ({_('link removed')})")
    if new[0] and new[1]:
        far = _find_port(con, new[0], new[1])
        if far is not None and (far["remote_switch"] != sw_name or (far["remote_port"] or "") != after.get("name")):
            # the far port was linked to a third switch: that third end no longer has a partner
            if far["remote_switch"] and far["remote_switch"] != sw_name:
                third = _find_port(con, far["remote_switch"], far["remote_port"] or "")
                if third is not None and third["remote_switch"] == far["sw_name"] and \
                        (third["remote_port"] or "") in (far["name"], str(far["idx"])):
                    con.execute(CLEAR_LINK_SQL, (third["id"],))
                    audit("documentation", "port_update", "switch", third["switch_id"], third["sw_name"],
                          {"port": [None, third["name"]], "remote_switch": [far["sw_name"], ""],
                           "remote_port": [third["remote_port"], ""], "link_sync": [None, f"{sw_name} {after.get('name')}"]})
                    linked.append(f"{third['sw_name']} {_('port')} {third['name']} ({_('link removed')})")
            con.execute("""UPDATE ports SET remote_switch=?, remote_port=?, eq_type=?, equipment=?, eq2_type='',
                           equipment2='' WHERE id=?""", (sw_name, after.get("name"), SWITCH_TYPE, sw_name, far["id"]))
            con.execute("UPDATE switches SET updated_at=? WHERE id=?", (now(), far["switch_id"]))
            audit("documentation", "port_update", "switch", far["switch_id"], far["sw_name"],
                  {"port": [None, far["name"]], "remote_switch": [far["remote_switch"], sw_name],
                   "remote_port": [far["remote_port"], after.get("name")], "link_sync": [None, f"{sw_name} {after.get('name')}"]})
            linked.append(f"{far['sw_name']} {_('port')} {far['name']}")


def submit_port_change(con, port, vals, sw_name, ticket_id=None):
    """Apply directly (Modify level) or store / merge a pending change request (Edit level).
    Inside a maintenance ticket every change is held until the ticket is validated."""
    if perm("ports") >= LEVELS["modify"] and not ticket_id:
        changed = {k: v for k, v in vals.items() if (port[k] or "") != v}
        if changed:
            full = {k: changed.get(k, port[k] or "") for k in PORT_FIELDS}
            create_assets_for(con, full, sw_name, full["name"])
            con.execute(f"UPDATE ports SET {', '.join(f'{k}=?' for k in changed)} WHERE id=?",
                        (*changed.values(), port["id"]))
            con.execute("UPDATE switches SET updated_at=? WHERE id=?", (now(), port["switch_id"]))
            audit("documentation", "port_update", "switch", port["switch_id"], sw_name,
                  {"port": [None, port["name"]]} | {k: [port[k], v] for k, v in changed.items()})
            if {"remote_switch", "remote_port", "name"} & set(changed):
                after = {k: changed.get(k, port[k]) for k in ("name", "remote_switch", "remote_port")}
                sync_link(con, sw_name, dict(port), after)
        return "applied"
    uid = current_user()["id"]
    req = con.execute("""SELECT * FROM change_requests WHERE port_id=? AND requested_by=? AND status='pending'
                         AND COALESCE(ticket_id, 0)=?""", (port["id"], uid, ticket_id or 0)).fetchone()
    fields = json.loads(req["fields"]) if req else {}
    old = json.loads(req["old"]) if req else {}
    newly = {}
    for k, v in vals.items():
        if (port[k] or "") == v:
            fields.pop(k, None)  # user reverted to the current value
            old.pop(k, None)
        else:
            if fields.get(k) != v:
                newly[k] = [port[k] or "", v]
            fields[k] = v
            old.setdefault(k, port[k] or "")
    if req and not fields:
        con.execute("DELETE FROM change_requests WHERE id=?", (req["id"],))
    elif req:
        con.execute("UPDATE change_requests SET fields=?, old=?, requested_at=? WHERE id=?",
                    (json.dumps(fields), json.dumps(old), now(), req["id"]))
    elif fields:
        con.execute("""INSERT INTO change_requests(port_id, switch_id, requested_by, requested_at, fields, old, ticket_id)
                       VALUES (?,?,?,?,?,?,?)""", (port["id"], port["switch_id"], uid, now(),
                                                   json.dumps(fields), json.dumps(old), ticket_id))
    if newly:
        audit("documentation", "port_request", "switch", port["switch_id"], sw_name, {"port": [None, port["name"]]} | newly
              | ({"ticket": [None, f"#{ticket_id}"]} if ticket_id else {}))
    return "ticket" if ticket_id else "pending"


def _port_payload(con, pid):
    p = dict(con.execute("SELECT * FROM ports WHERE id=?", (pid,)).fetchone())
    p["pending"] = pending_for_ports(con, [pid]).get(pid, {})
    return p


def _visible_port(con, pid):
    port = con.execute("SELECT p.*, s.name AS sw_name FROM ports p JOIN switches s ON s.id=p.switch_id WHERE p.id=?",
                       (pid,)).fetchone()
    if not port or not can_see_switch(port["switch_id"]):
        abort(404)
    return port


@app.route("/api/port/<int:pid>", methods=["POST"])
@require("ports", "edit")
def api_port(pid):
    con = db()
    port = _visible_port(con, pid)
    try:
        vals = _clean_port_values(request.get_json(force=True) or {}, port)
    except PortError as e:
        return jsonify({"error": str(e)}), 400
    mode = submit_port_change(con, port, vals, port["sw_name"])
    con.commit()
    return jsonify({"mode": mode, "port": _port_payload(con, pid)})


@app.route("/api/ports/bulk", methods=["POST"])
@require("ports", "edit")
def api_ports_bulk():
    con = db()
    data = request.get_json(force=True) or {}
    fields = dict(data.get("fields", {}))
    fields.pop("name", None)
    ids = [int(i) for i in data.get("ids", [])]
    mode = "applied"
    for pid in ids:
        port = _visible_port(con, pid)
        try:
            vals = _clean_port_values(fields, port)
        except PortError as e:
            con.rollback()
            return jsonify({"error": str(e)}), 400
        if vals:
            mode = submit_port_change(con, port, vals, port["sw_name"])
    con.commit()
    return jsonify({"mode": mode, "ports": [_port_payload(con, pid) for pid in ids]})


@app.route("/api/ports/save", methods=["POST"])
@require("ports", "edit")
def api_ports_save():
    """Save every port change made in edit mode in one go: {"changes": {port_id: {field: value}}}."""
    con = db()
    body = request.get_json(force=True) or {}
    changes = body.get("changes") or {}
    ticket = None
    if body.get("ticket"):
        ticket = work_ticket(con, int(body["ticket"]))
        if ticket is None:
            return jsonify({"error": _("This maintenance ticket is not in progress or not assigned to you.")}), 403
    mode, out = "applied", []
    set_batch_context(con, changes, ticket["id"] if ticket else None)
    for pid, vals in changes.items():
        port = _visible_port(con, int(pid))
        if not port["switch_id"] or not isinstance(vals, dict):
            continue
        try:
            clean = _clean_port_values(vals, port)
        except PortError as e:
            con.rollback()
            return jsonify({"error": f"{_('Port')} {port['name']}: {e}"}), 400
        if ticket is not None and not con.execute("SELECT 1 FROM ticket_switches WHERE ticket_id=? AND switch_id=?",
                                                  (ticket["id"], port["switch_id"])).fetchone():
            return jsonify({"error": _("This switch is not part of the maintenance ticket.")}), 403
        if clean:
            mode = submit_port_change(con, port, clean, port["sw_name"], ticket["id"] if ticket else None)
        out.append(int(pid))
    con.commit()
    return jsonify({"mode": mode, "ports": [_port_payload(con, pid) for pid in out], "linked": g.get("linked", [])})


# ---------------------------------------------------------------- approvals
def _requests(where, params, limit=500):
    clause, vparams = vis("s")
    return db().execute(f"""
        SELECT c.*, s.name AS switch_name, s.id AS sid, p.name AS port_name, p.idx,
               ru.username AS r_user, ru.first_name AS r_first, ru.last_name AS r_last,
               vu.username AS v_user, vu.first_name AS v_first, vu.last_name AS v_last
        FROM change_requests c JOIN switches s ON s.id=c.switch_id LEFT JOIN ports p ON p.id=c.port_id
        LEFT JOIN users ru ON ru.id=c.requested_by LEFT JOIN users vu ON vu.id=c.reviewed_by
        WHERE c.ticket_id IS NULL AND {where}{clause} ORDER BY c.id DESC LIMIT {int(limit)}""", (*params, *vparams)).fetchall()


@app.route("/approvals")
@require("ports", "edit")
def approvals():
    con = db()
    approver = can("ports", "modify")
    if approver:
        pending = _requests("c.status='pending'", ())
        history = _requests("c.status<>'pending'", (), 150)
    else:
        uid = current_user()["id"]
        pending = _requests("c.status='pending' AND c.requested_by=?", (uid,))
        history = _requests("c.status<>'pending' AND c.requested_by=?", (uid,), 150)
    current = {}
    ids = [r["port_id"] for r in pending if r["port_id"]]
    if ids:
        for p in con.execute(f"SELECT * FROM ports WHERE id IN ({','.join('?' * len(ids))})", ids):
            current[p["id"]] = p

    def decorate(rows):
        out = []
        for r in rows:
            d = dict(r)
            d["fields"], d["old"] = json.loads(r["fields"]), json.loads(r["old"])
            d["requester"] = f"{r['r_first'] or ''} {r['r_last'] or ''}".strip() or r["r_user"] or "?"
            d["reviewer"] = f"{r['v_first'] or ''} {r['v_last'] or ''}".strip() or r["v_user"] or ""
            cur = current.get(r["port_id"])
            d["stale"] = [k for k in d["fields"] if cur is not None and (cur[k] or "") != d["old"].get(k, "")]
            out.append(d)
        return out

    return render_template("approvals.html", pending=decorate(pending), history=decorate(history),
                           approver=approver, labels=PORT_FIELD_LABELS, statuses=dict(STATUSES),
                           req_status={"pending": N_("pending"), "approved": N_("approved"),
                                       "rejected": N_("rejected"), "cancelled": N_("cancelled")})


def apply_change_request(con, r, comment=""):
    """Apply one pending change request (row with sw_name, port_name). Returns an error text or None."""
    st = con.execute("SELECT status FROM change_requests WHERE id=?", (r["id"],)).fetchone()
    if st is None or st[0] != "pending":
        return None  # already applied together with another change (asset move)
    fields = {k: v for k, v in json.loads(r["fields"]).items() if k in PORT_FIELDS}
    old = json.loads(r["old"])
    details = {"port": [None, r["port_name"]]} | {k: [old.get(k), v] for k, v in fields.items()}
    if comment:
        details["comment"] = [None, comment]
    if r["ticket_id"]:
        details["ticket"] = [None, f"#{r['ticket_id']}"]
    if r["port_id"] and fields:
        before = con.execute("SELECT * FROM ports WHERE id=?", (r["port_id"],)).fetchone()
        if before is not None:
            full = {k: fields.get(k, before[k] or "") for k in PORT_FIELDS}
            if full["status"] == "disabled":
                for k in CLEARED_WHEN_DISABLED:
                    if full[k]:
                        fields[k] = full[k] = ""
            try:
                for t_, e_ in (("eq_type", "equipment"), ("eq2_type", "equipment2")):
                    if e_ in fields and full[e_]:
                        # the asset comes from another port whose change (same ticket / same person) is still
                        # pending: apply that release first, so the move is applied as a whole
                        rel = releasing_requests(con, full[e_], r["ticket_id"], r["requested_by"])
                        busy = g.setdefault("_applying", set())
                        busy.add(r["id"])
                        for pid_, rr in rel.items():
                            if pid_ == r["port_id"] or rr["id"] in busy:
                                continue
                            row = con.execute("""SELECT c.*, s.name AS sw_name, p.name AS port_name FROM change_requests c
                                                 JOIN switches s ON s.id=c.switch_id LEFT JOIN ports p ON p.id=c.port_id
                                                 WHERE c.id=? AND c.status='pending'""", (rr["id"],)).fetchone()
                            if row is not None:
                                err = apply_change_request(con, row, comment)
                                if err:
                                    return err
                        check_asset_free(con, full[t_], full[e_], r["port_id"], released=set())
                if "remote_port" in fields or "equipment" in fields:
                    check_link_free(con, r["sw_name"], full, r["port_id"])
            except PortError as e:
                return f"{r['sw_name']} {_('port')} {r['port_name']}: {e}"
            create_assets_for(con, full, r["sw_name"], r["port_name"])
        con.execute(f"UPDATE ports SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                    (*fields.values(), r["port_id"]))
        con.execute("UPDATE switches SET updated_at=? WHERE id=?", (now(), r["switch_id"]))
        if before is not None and {"remote_switch", "remote_port", "name"} & set(fields):
            sync_link(con, r["sw_name"], dict(before),
                      {k: fields.get(k, before[k]) for k in ("name", "remote_switch", "remote_port")})
    con.execute("UPDATE change_requests SET status='approved', reviewed_by=?, reviewed_at=?, comment=? WHERE id=?",
                (current_user()["id"], now(), comment, r["id"]))
    audit("documentation", "request_approve", "switch", r["switch_id"], r["sw_name"], details)
    return None


@app.route("/approvals/act", methods=["POST"])
@require("ports", "edit")
def approvals_act():
    con = db()
    action = request.form.get("action")
    ids = [int(x) for x in request.form.getlist("ids")]
    comment = request.form.get("comment", "").strip()[:500]
    if action in ("approve", "reject") and not can("ports", "modify"):
        abort(403)
    done = 0
    for rid in ids:
        r = con.execute("""SELECT c.*, s.name AS sw_name, p.name AS port_name FROM change_requests c
                           JOIN switches s ON s.id=c.switch_id LEFT JOIN ports p ON p.id=c.port_id
                           WHERE c.id=? AND c.status='pending' AND c.ticket_id IS NULL""", (rid,)).fetchone()
        if not r or not can_see_switch(r["switch_id"]):
            continue
        fields = {k: v for k, v in json.loads(r["fields"]).items() if k in PORT_FIELDS}
        old = json.loads(r["old"])
        details = {"port": [None, r["port_name"]]} | {k: [old.get(k), v] for k, v in fields.items()}
        if comment:
            details["comment"] = [None, comment]
        if action == "cancel":
            if r["requested_by"] != current_user()["id"]:
                continue
            con.execute("UPDATE change_requests SET status='cancelled', reviewed_by=?, reviewed_at=? WHERE id=?",
                        (current_user()["id"], now(), rid))
            audit("documentation", "request_cancel", "switch", r["switch_id"], r["sw_name"], details)
        elif action == "approve":
            err = apply_change_request(con, r, comment)
            if err:
                flash(err, "error")
                continue
        elif action == "reject":
            con.execute("UPDATE change_requests SET status='rejected', reviewed_by=?, reviewed_at=?, comment=? "
                        "WHERE id=?", (current_user()["id"], now(), comment, rid))
            audit("documentation", "request_reject", "switch", r["switch_id"], r["sw_name"], details)
        else:
            continue
        done += 1
    con.commit()
    if g.get("linked"):
        flash(_("The other end of the link was updated too: %(list)s", list=", ".join(g.linked)), "ok")
    msg = {"approve": N_("%(n)s change requests approved."), "reject": N_("%(n)s change requests rejected."),
           "cancel": N_("%(n)s change requests cancelled.")}.get(action, N_("%(n)s change requests processed."))
    flash(_(msg, n=done), "ok" if done else "error")
    return redirect(request.referrer or url_for("approvals"))


# ---------------------------------------------------------------- credentials
@app.route("/api/creds/<int:cid>")
@require("credentials", "view")
def api_creds(cid):
    row = db().execute("SELECT * FROM cred_profiles WHERE id=?", (cid,)).fetchone()
    if not row:
        abort(404)
    sw = request.args.get("switch", "")[:100]
    fields = cred_fields_of(db(), cid)
    if not can("passwords"):  # usernames / text only
        return jsonify({f"f{f['id']}": f["value"] for f in fields if f["kind"] not in SECRET_KINDS})
    audit("access", "reveal_credentials", "credentials", cid, row["name"], {"switches": [None, sw]} if sw else {})
    db().commit()
    return jsonify({f"f{f['id']}": (dec(f["value"]) if f["kind"] in SECRET_KINDS else f["value"]) for f in fields})


@app.route("/credentials")
@require("credentials", "view")
def creds_list():
    con = db()
    clause, params = vis("s")
    profiles = con.execute("SELECT * FROM cred_profiles ORDER BY name").fetchall()
    usage = {}
    for r in con.execute(f"SELECT s.id, s.name, s.cred_profile_id FROM switches s WHERE 1=1{clause} ORDER BY s.name",
                         params):
        usage.setdefault(r["cred_profile_id"], []).append(r)
    return render_template("creds.html", profiles=profiles, usage=usage,
                           fields={p["id"]: cred_fields_of(con, p["id"]) for p in profiles}, secret_kinds=SECRET_KINDS)


@app.route("/credentials/new", methods=["GET", "POST"])
@app.route("/credentials/<int:cid>/edit", methods=["GET", "POST"])
@require("credentials", "edit")
def cred_edit(cid=None):
    con = db()
    prof = con.execute("SELECT * FROM cred_profiles WHERE id=?", (cid,)).fetchone() if cid else None
    if cid and not prof:
        abort(404)
    clause, params = vis("s")
    switches = con.execute(f"SELECT s.id, s.name, s.area, s.cred_profile_id FROM switches s WHERE 1=1{clause} "
                           "ORDER BY s.area, s.name", params).fetchall()
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash(_("Profile name is required."), "error")
            return redirect(request.url)
        vals = {"name": name, "notes": f.get("notes", "").strip()}
        changes = diff(prof, vals) if prof else {k: [None, v] for k, v in vals.items() if v}
        try:
            if prof:
                con.execute("UPDATE cred_profiles SET name=?, notes=? WHERE id=?", (name, vals["notes"], cid))
            else:
                cid = con.execute("INSERT INTO cred_profiles(name, notes) VALUES (?,?)", (name, vals["notes"])).lastrowid
        except sqlite3.IntegrityError:
            flash(_("A profile with that name already exists."), "error")
            return redirect(request.url)
        old = {r["id"]: r for r in cred_fields_of(con, cid)}
        kept = set()

        def shown(k, v):
            return ("••••" if v else "") if k in SECRET_KINDS else v
        clear = set(f.getlist("f_clear"))
        rows = zip(f.getlist("f_id"), f.getlist("f_label"), f.getlist("f_kind"), f.getlist("f_value"))
        for sort, (fid, label, kind, value) in enumerate(rows):
            label, kind = label.strip()[:80], kind if kind in dict(CRED_KINDS) else "secret"
            if not label:
                continue
            o = old.get(int(fid)) if fid.isdigit() else None
            secret = kind in SECRET_KINDS
            if o is not None and secret and o["kind"] in SECRET_KINDS and value == "" and fid not in clear:
                stored = o["value"]  # blank = keep the saved secret
            else:
                stored = enc(value) if secret else value.strip()
            if o is None:
                con.execute("INSERT INTO cred_fields(profile_id, label, kind, value, sort) VALUES (?,?,?,?,?)",
                            (cid, label, kind, stored, sort))
                if stored:
                    changes[label] = [None, shown(kind, stored)]
            else:
                kept.add(o["id"])
                con.execute("UPDATE cred_fields SET label=?, kind=?, value=?, sort=? WHERE id=?",
                            (label, kind, stored, sort, o["id"]))
                if o["label"] != label:
                    changes[label] = [o["label"], label]
                elif stored != o["value"] or o["kind"] != kind:
                    changes[label] = [shown(o["kind"], o["value"]), shown(kind, stored)]
        for oid, o in old.items():
            if oid not in kept:
                con.execute("DELETE FROM cred_fields WHERE id=?", (oid,))
                changes[o["label"]] = [shown(o["kind"], o["value"]), None]
        assigned = {int(x) for x in f.getlist("switch_ids")}
        before = sorted(s["name"] for s in switches if s["cred_profile_id"] == cid)
        for s in switches:
            if s["id"] in assigned:
                con.execute("UPDATE switches SET cred_profile_id=? WHERE id=?", (cid, s["id"]))
            elif s["cred_profile_id"] == cid:
                con.execute("UPDATE switches SET cred_profile_id=NULL WHERE id=?", (s["id"],))
        after = sorted(s["name"] for s in switches if s["id"] in assigned)
        if before != after:
            changes["switches"] = [", ".join(before), ", ".join(after)]
        if changes or not prof:
            audit("documentation", "cred_update" if prof else "cred_create", "credentials", cid, name, changes)
        con.commit()
        flash(_("Credential profile %(name)s saved.", name=name), "ok")
        return redirect(url_for("creds_list"))
    fields = cred_fields_of(con, cid) if prof else []
    suggestions = sorted({lab for _n, fl in CRED_TEMPLATES for lab, _k in fl} |
                         {r[0] for r in con.execute("SELECT DISTINCT label FROM cred_fields")})
    return render_template("cred_form.html", prof=prof, switches=switches, fields=fields, kinds=CRED_KINDS,
                           kind_labels={k: _(lbl) for k, lbl in CRED_KINDS}, label_map={lab: _(lab) for lab in suggestions},
                           secret_kinds=SECRET_KINDS, templates=CRED_TEMPLATES,
                           suggestions=sorted({lab for _n, fl in CRED_TEMPLATES for lab, _k in fl} |
                                              {r[0] for r in con.execute("SELECT DISTINCT label FROM cred_fields")}))


@app.route("/credentials/<int:cid>/delete", methods=["POST"])
@require("credentials", "modify")
def cred_delete(cid):
    row = db().execute("SELECT name FROM cred_profiles WHERE id=?", (cid,)).fetchone()
    if row:
        audit("documentation", "cred_delete", "credentials", cid, row["name"])
        db().execute("DELETE FROM cred_profiles WHERE id=?", (cid,))
        db().commit()
    flash(_("Credential profile deleted."), "ok")
    return redirect(url_for("creds_list"))


# ---------------------------------------------------------------- settings
@app.route("/settings")
def settings():
    if not any(can(m) for m in SETTINGS_MENUS) and not can("maintenance", "modify"):
        abort(403)
    con = db()
    clause, params = vis("s", all_status=True)
    counts = {
        "switches": con.execute(f"SELECT COUNT(*) FROM switches s WHERE s.status<>'replaced'{clause}", params).fetchone()[0],
        "groups": con.execute("SELECT COUNT(*) FROM sw_groups").fetchone()[0],
        "vlans": con.execute("SELECT COUNT(*) FROM vlans").fetchone()[0],
        "port_types": con.execute("SELECT COUNT(*) FROM port_types").fetchone()[0],
        "models": con.execute("SELECT COUNT(*) FROM models").fetchone()[0],
        "assets": con.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        "places": con.execute("SELECT (SELECT COUNT(*) FROM locations)+(SELECT COUNT(*) FROM racks)+(SELECT COUNT(*) FROM offices)").fetchone()[0],
        "users": con.execute("SELECT COUNT(*) FROM users WHERE profile_id IS NOT NULL OR is_admin=1").fetchone()[0],
        "profiles": con.execute("SELECT COUNT(*) FROM auth_profiles").fetchone()[0],
        "audit": con.execute(f"SELECT COUNT(*) FROM {audit_source()}").fetchone()[0],
        "ip_access": con.execute("SELECT COUNT(*) FROM ip_rules").fetchone()[0],
        "maintenance": con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
        "companies": len(companies_all()),
        "monitoring": mon_cfg(con)["mon_enabled"] == "1",
    }
    return render_template("settings.html", counts=counts)


# -- switches (all, including disabled and replaced)
@app.route("/settings/switches")
@require("switches")
def settings_switches():
    con = db()
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip().lower()
    clause, params = vis("s", all_status=True)
    rows = con.execute(f"""
        SELECT s.*, (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id) AS n_ports,
          (SELECT group_concat(g.id || ':' || g.name || ':' || g.color, '|') FROM switch_groups sg
             JOIN sw_groups g ON g.id=sg.group_id WHERE sg.switch_id=s.id) AS grp,
          (SELECT r.name FROM switches r WHERE r.id=s.replaced_by) AS successor
        FROM switches s LEFT JOIN models m ON m.name=s.model WHERE 1=1{clause}
        ORDER BY s.status='replaced', s.area COLLATE NOCASE, s.name COLLATE NOCASE""", params).fetchall()
    counts = {k: sum(1 for r in rows if r["status"] == k) for k, _l in SWITCH_STATUSES}
    rows = [dict(r, groups=split_groups(r["grp"])) for r in rows
            if (not status or r["status"] == status)
            and (not q or q in " ".join(str(r[k] or "") for k in ("name", "ip", "serial", "model", "area",
                                                                   "designation", "location", "rack")).lower())]
    return render_template("settings_switches.html", switches=rows, status=status, q=q, counts=counts,
                           sw_statuses=SWITCH_STATUSES)


# -- locations, racks, offices
PLACE_KINDS = {  # kind -> (table, switch column, label)
    "location": ("locations", "location", N_("Location")),
    "rack": ("racks", "rack", N_("Rack identifier")),
    "office": ("offices", "designation", N_("Office")),
}


@app.route("/settings/places", methods=["GET", "POST"])
@require("places")
def places():
    con = db()
    if request.method == "POST":
        if not can("places", "edit"):
            abort(403)
        f = request.form
        kind = f.get("kind")
        if kind not in PLACE_KINDS:
            abort(400)
        table, col, label = PLACE_KINDS[kind]
        name = f.get("name", "").strip()
        if not name:
            flash(_("The name is required."), "error")
            return redirect(url_for("places", _anchor=kind))
        vals = {"name": name, "description": f.get("description", "").strip()}
        if kind == "location":
            vals["area"] = f.get("area", "").strip()
        else:
            vals["location"] = f.get("location", "").strip()
        try:
            if f.get("id"):
                old = con.execute(f"SELECT * FROM {table} WHERE id=?", (int(f["id"]),)).fetchone()
                con.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), int(f["id"])))
                ch = diff(old, vals)
                if old and old["name"] != name:  # rename everywhere it is used
                    n = con.execute(f"UPDATE switches SET {col}=? WHERE {col}=?", (name, old["name"])).rowcount
                    if kind == "location":
                        con.execute("UPDATE racks SET location=? WHERE location=?", (name, old["name"]))
                        con.execute("UPDATE offices SET location=? WHERE location=?", (name, old["name"]))
                    if n:
                        ch["switches"] = [None, n]
                if ch:
                    audit("settings", "place_update", kind, int(f["id"]), name, ch)
            else:
                pid = con.execute(f"INSERT INTO {table}({', '.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                                  tuple(vals.values())).lastrowid
                audit("settings", "place_create", kind, pid, name, {k: [None, v] for k, v in vals.items() if v})
            con.commit()
            flash(_("%(what)s %(name)s saved.", what=_(label), name=name), "ok")
        except sqlite3.IntegrityError:
            flash(_("%(what)s %(name)s already exists.", what=_(label), name=name), "error")
        return redirect(url_for("places", _anchor=kind))
    data = {}
    for kind, (table, col, _l) in PLACE_KINDS.items():
        data[kind] = con.execute(f"""SELECT t.*, (SELECT COUNT(*) FROM switches s WHERE s.{col}=t.name AND s.status<>'replaced')
                                    AS used FROM {table} t ORDER BY {'t.area COLLATE NOCASE, ' if kind == 'location' else ''}
                                    t.name COLLATE NOCASE""").fetchall()
    areas = [r[0] for r in con.execute("SELECT DISTINCT area FROM switches WHERE area<>'' ORDER BY area")]
    return render_template("places.html", data=data, areas=areas)


@app.route("/settings/places/<kind>/<int:pid>/delete", methods=["POST"])
@require("places", "modify")
def place_delete(kind, pid):
    if kind not in PLACE_KINDS:
        abort(404)
    con = db()
    table, col, label = PLACE_KINDS[kind]
    row = con.execute(f"SELECT * FROM {table} WHERE id=?", (pid,)).fetchone()
    if row:
        used = con.execute(f"SELECT COUNT(*) FROM switches WHERE {col}=? AND status<>'replaced'", (row["name"],)).fetchone()[0]
        if used:
            flash(_("%(n)s switches still use %(name)s - change them first.", n=used, name=row["name"]), "error")
            return redirect(url_for("places", _anchor=kind))
        con.execute(f"DELETE FROM {table} WHERE id=?", (pid,))
        audit("settings", "place_delete", kind, pid, row["name"])
        con.commit()
        flash(_("%(what)s %(name)s deleted.", what=_(label), name=row["name"]), "ok")
    return redirect(url_for("places", _anchor=kind))


# ---------------------------------------------------------------- assets
def asset_usage(con, names_lower=None):
    """{asset name (lower): [(switch id, switch name, port id, port name, behind?)]} for the switches the user can see."""
    clause, params = vis("s")
    out = {}
    for r in con.execute(f"""SELECT p.id, p.name, p.equipment, p.eq_type, p.equipment2, p.eq2_type, s.id AS sid,
                                    s.name AS sw FROM ports p JOIN switches s ON s.id=p.switch_id
                             WHERE (p.equipment<>'' OR p.equipment2<>''){clause} ORDER BY s.name, p.idx""", params):
        for col, behind in (("equipment", False), ("equipment2", True)):
            n = (r[col] or "").lower()
            if n and (names_lower is None or n in names_lower):
                out.setdefault(n, []).append((r["sid"], r["sw"], r["id"], r["name"], behind))
    return out


USED_NAMES_SQL = """SELECT lower(equipment) FROM ports p JOIN switches s ON s.id=p.switch_id
                    WHERE p.equipment<>'' AND s.status<>'replaced'
                    UNION SELECT lower(equipment2) FROM ports p JOIN switches s ON s.id=p.switch_id
                    WHERE p.equipment2<>'' AND s.status<>'replaced'"""
STATE_SQL = f"""CASE WHEN lower(a.name) IN ({USED_NAMES_SQL}) THEN 'in_use'
                     WHEN a.state='defective' THEN 'defective' ELSE 'stock' END"""


@app.route("/assets")
@require("assets")
def assets_page():
    con = db()
    types = con.execute(f"""SELECT t.*, COUNT(a.id) AS n,
                                   SUM(CASE WHEN {STATE_SQL}='in_use' THEN 1 ELSE 0 END) AS used,
                                   SUM(CASE WHEN {STATE_SQL}='defective' THEN 1 ELSE 0 END) AS defective
                            FROM asset_types t LEFT JOIN assets a ON a.type_id=t.id
                            GROUP BY t.id ORDER BY t.system DESC, t.sort, t.name COLLATE NOCASE""").fetchall()
    q = request.args.get("q", "").strip()
    state = request.args.get("state", "")
    state = state if state in dict(ASSET_STATES) or state == "free" else ""
    try:
        tid = int(request.args.get("type") or 0)
    except ValueError:
        tid = 0
    cur = next((t for t in types if t["id"] == tid), None)
    if cur is None and not q and not state:
        cur = next((t for t in types if not t["system"]), types[0] if types else None)
    switch_rows, rows = [], []
    if cur is not None and cur["system"] and not q:
        clause, params = vis("s")
        switch_rows = con.execute(f"""SELECT s.* FROM switches s WHERE 1=1{clause}
                                      ORDER BY s.name COLLATE NOCASE""", params).fetchall()
    else:
        sql = f"""SELECT * FROM (SELECT a.*, t.name AS type_name, t.color, t.kind, t.multi, {STATE_SQL} AS st
                  FROM assets a JOIN asset_types t ON t.id=a.type_id) a WHERE 1=1"""
        args = []
        if q:
            like = f"%{q}%"
            sql += " AND (a.name LIKE ? OR a.ip LIKE ? OR a.mac LIKE ? OR a.serial LIKE ? OR a.location LIKE ? OR a.notes LIKE ?)"
            args += [like] * 6
        elif cur is not None:
            sql += " AND a.type_id=?"
            args.append(cur["id"])
        if state == "free":
            sql += " AND a.st<>'in_use'"
        elif state:
            sql += " AND a.st=?"
            args.append(state)
        rows = con.execute(sql + " ORDER BY a.st='in_use', a.name COLLATE NOCASE LIMIT 3000", args).fetchall()
    usage = asset_usage(con, {r["name"].lower() for r in rows}) if rows and can("ports") else {}
    totals = con.execute(f"""SELECT COUNT(*) AS n, SUM({STATE_SQL}='in_use') AS used,
                                    SUM({STATE_SQL}='stock') AS stock, SUM({STATE_SQL}='defective') AS defective
                             FROM assets a""").fetchone()
    kinds = [(k, _(l)) for k, l in ASSET_KINDS if k != "switch"]
    return render_template("assets.html", icons=ICONS, types=types, cur=None if (q or (state and not tid)) else cur, rows=rows,
                           usage=usage, q=q, state=state, states=ASSET_STATES + [("free", N_("Free"))], totals=totals,
                           switch_rows=switch_rows, kinds=kinds, kind_labels=dict(kinds), switch_type=SWITCH_TYPE)


@app.route("/api/switch/<int:sid>/ports")
@require("ports")
def api_switch_ports(sid):
    """Ports of a switch with what is connected (destination picker of an asset move)."""
    switch_or_404(sid)
    return jsonify([{"id": p["id"], "name": p["name"], "status": p["status"], "eq_type": p["eq_type"],
                     "equipment": p["equipment"], "equipment2": p["equipment2"]}
                    for p in db().execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,))])


@app.route("/assets/<int:aid>/move", methods=["GET", "POST"])
@require("ports", "edit")
def asset_move(aid):
    """Move an asset from the port it is connected to, to another port - in one action."""
    con = db()
    a = con.execute("""SELECT a.*, t.name AS type_name, t.multi FROM assets a JOIN asset_types t ON t.id=a.type_id
                       WHERE a.id=?""", (aid,)).fetchone()
    if a is None:
        abort(404)
    u = current_user()
    clause, params = vis("s")
    sources = con.execute(f"""SELECT p.*, s.name AS sw_name, s.id AS sid FROM ports p JOIN switches s ON s.id=p.switch_id
                              WHERE (p.equipment=? COLLATE NOCASE OR p.equipment2=? COLLATE NOCASE)
                              AND s.status='active'{clause} ORDER BY s.name, p.idx""", (a["name"], a["name"], *params)).fetchall()
    sws = con.execute(f"SELECT s.id, s.name, s.area FROM switches s WHERE 1=1{clause} ORDER BY s.name COLLATE NOCASE",
                      params).fetchall()
    tickets = con.execute("""SELECT t.* FROM tickets t JOIN ticket_assignees ta ON ta.ticket_id=t.id
                             WHERE t.status='in_progress' AND ta.user_id=? ORDER BY t.id""", (u["id"],)).fetchall()
    back = request.values.get("next") or url_for("assets_page", q=a["name"])
    if request.method == "POST":
        f = request.form
        src = next((p for p in sources if str(p["id"]) == f.get("from")), None)
        dst = con.execute("SELECT p.*, s.name AS sw_name FROM ports p JOIN switches s ON s.id=p.switch_id WHERE p.id=?",
                          (f.get("to", type=int),)).fetchone() if f.get("to", "").isdigit() else None
        ticket = work_ticket(con, int(f["ticket"])) if f.get("ticket", "").isdigit() else None
        err = None
        if src is None and sources:
            err = _("Choose the port the asset comes from.")
        elif dst is None or not can_see_switch(dst["switch_id"]):
            err = _("Choose the destination port.")
        elif src is not None and dst["id"] == src["id"]:
            err = _("The destination is the port where the asset already is.")
        elif dst["status"] == "disabled":
            err = _("The destination port is disabled - enable it first.")
        elif f.get("ticket") and ticket is None:
            err = _("This maintenance ticket is not in progress or not assigned to you.")
        elif ticket is not None and not all(con.execute("SELECT 1 FROM ticket_switches WHERE ticket_id=? AND switch_id=?",
                                                        (ticket["id"], x)).fetchone()
                                            for x in {dst["switch_id"], *( [src["switch_id"]] if src else [])}):
            err = _("Both switches must be part of the maintenance ticket.")
        elif (dst["equipment"] or dst["remote_switch"]) and not f.get("replace"):
            err = _("The destination port already has %(x)s - tick “replace” to free it.", x=dst["equipment"] or dst["remote_switch"])
        if err:
            flash(err, "error")
            return redirect(url_for("asset_move", aid=aid, next=back))
        as_behind = src is not None and (src["equipment2"] or "").lower() == a["name"].lower() and \
            (src["equipment"] or "").lower() != a["name"].lower()
        with_behind = src is not None and not as_behind and src["equipment2"] and f.get("with_behind")
        changes = {}
        if src is not None:
            changes[src["id"]] = {"eq2_type": "", "equipment2": ""} if as_behind else \
                ({"eq_type": "", "equipment": "", "remote_port": ""} | ({"eq2_type": "", "equipment2": ""} if with_behind else {}))
        changes[dst["id"]] = {"eq_type": a["type_name"], "equipment": a["name"], "remote_port": ""} | \
            ({"eq2_type": src["eq2_type"], "equipment2": src["equipment2"]} if with_behind else {"eq2_type": "", "equipment2": ""})
        set_batch_context(con, changes, ticket["id"] if ticket else None)
        mode = "applied"
        try:
            for pid in ([src["id"]] if src is not None else []) + [dst["id"]]:
                port = _visible_port(con, pid)
                clean = _clean_port_values(changes[pid], port)
                if clean:
                    mode = submit_port_change(con, port, clean, port["sw_name"], ticket["id"] if ticket else None)
        except PortError as e:
            con.rollback()
            flash(str(e), "error")
            return redirect(url_for("asset_move", aid=aid, next=back))
        audit("documentation", "asset_move", "asset", aid, a["name"],
              {"rows": [None, f"{src['sw_name']} {src['name']} → {dst['sw_name']} {dst['name']}" if src else
                        f"→ {dst['sw_name']} {dst['name']}"], "result": [None, mode]}
              | ({"ticket": [None, f"#{ticket['id']}"]} if ticket else {}))
        con.commit()
        flash({"applied": _("%(name)s moved to %(sw)s port %(p)s.", name=a["name"], sw=dst["sw_name"], p=dst["name"]),
               "pending": _("Move of %(name)s sent for approval (both ports change together when approved).", name=a["name"]),
               "ticket": _("Move of %(name)s held in ticket #%(n)s until it is validated.", name=a["name"],
                           n=ticket["id"] if ticket else "")}[mode], "ok")
        return redirect(back)
    return render_template("asset_move.html", a=a, sources=sources, sws=sws, tickets=tickets, back=back,
                           direct=can("ports", "modify"))


@app.route("/assets/types", methods=["POST"])
@require("assets", "edit")
def asset_type_save():
    con = db()
    f = request.form
    name = f.get("name", "").strip()[:60]
    vals = {"name": name, "kind": f.get("kind") if f.get("kind") in dict(ASSET_KINDS) else "other",
            "multi": 1 if f.get("multi") else 0, "icon": f.get("icon") if f.get("icon") in ICONS else "",
            "color": f.get("color", "#64748b")[:9], "description": f.get("description", "").strip()[:300]}
    try:
        vals["sort"] = int(f.get("sort")) if f.get("sort") not in (None, "") else \
            con.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM asset_types").fetchone()[0]
    except ValueError:
        vals["sort"] = 0
    if not name:
        flash(_("The name is required."), "error")
        return redirect(url_for("assets_page"))
    try:
        if f.get("id"):
            tid = int(f["id"])
            old = con.execute("SELECT * FROM asset_types WHERE id=?", (tid,)).fetchone()
            if old is None:
                abort(404)
            if old["system"]:  # the Switch type keeps its name and shape
                vals.update(name=old["name"], kind=old["kind"], multi=old["multi"])
            con.execute(f"UPDATE asset_types SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), tid))
            ch = diff(old, vals)
            if old["name"] != vals["name"]:
                n = con.execute("UPDATE ports SET eq_type=? WHERE eq_type=?", (vals["name"], old["name"])).rowcount
                n += con.execute("UPDATE ports SET eq2_type=? WHERE eq2_type=?", (vals["name"], old["name"])).rowcount
                if n:
                    ch["rows"] = [None, n]
            if ch:
                audit("settings", "asset_type_update", "asset_type", tid, vals["name"], ch)
        else:
            tid = con.execute("INSERT INTO asset_types(name, kind, color, description, sort, multi, icon) VALUES (?,?,?,?,?,?,?)",
                              (vals["name"], vals["kind"], vals["color"], vals["description"], vals["sort"],
                               vals["multi"], vals["icon"] or KIND_ICONS.get(vals["kind"], "other"))).lastrowid
            audit("settings", "asset_type_create", "asset_type", tid, name, {k: [None, v] for k, v in vals.items() if v})
        con.commit()
        flash(_("Asset type %(name)s saved.", name=vals["name"]), "ok")
    except sqlite3.IntegrityError:
        flash(_("Asset type %(name)s already exists.", name=name), "error")
        return redirect(url_for("assets_page"))
    return redirect(url_for("assets_page", type=tid))


@app.route("/assets/types/<int:tid>/delete", methods=["POST"])
@require("assets", "modify")
def asset_type_delete(tid):
    con = db()
    row = con.execute("SELECT * FROM asset_types WHERE id=?", (tid,)).fetchone()
    if row is None or row["system"]:
        abort(404)
    n = con.execute("SELECT COUNT(*) FROM assets WHERE type_id=?", (tid,)).fetchone()[0]
    used = con.execute("SELECT COUNT(*) FROM ports WHERE eq_type=? OR eq2_type=?", (row["name"], row["name"])).fetchone()[0]
    if n or used:
        flash(_("%(name)s still has %(n)s assets / %(p)s ports - move or delete them first.", name=row["name"], n=n, p=used), "error")
        return redirect(url_for("assets_page", type=tid))
    con.execute("DELETE FROM asset_types WHERE id=?", (tid,))
    audit("settings", "asset_type_delete", "asset_type", tid, row["name"])
    con.commit()
    flash(_("Asset type %(name)s deleted.", name=row["name"]), "ok")
    return redirect(url_for("assets_page"))


ASSET_FIELDS = ["name", "type_id", "ip", "mac", "serial", "location", "notes"]


@app.route("/assets/save", methods=["POST"])
@require("assets", "edit")
def asset_save():
    con = db()
    f = request.form
    back = f.get("next") or url_for("assets_page")
    try:
        t = con.execute("SELECT * FROM asset_types WHERE id=? AND system=0", (int(f.get("type_id") or 0),)).fetchone()
    except ValueError:
        t = None
    if t is None:
        flash(_("Choose an asset type."), "error")
        return redirect(back)
    # several names at once (one per line) when adding
    names = [n.strip()[:120] for n in (f.get("names") or f.get("name") or "").splitlines() if n.strip()]
    if not names:
        flash(_("The name is required."), "error")
        return redirect(back)
    extra = {k: f.get(k, "").strip()[:300] for k in ("ip", "mac", "serial", "location", "notes")}
    extra["state"] = "defective" if f.get("state") == "defective" else "stock"
    if f.get("id"):
        aid = int(f["id"])
        old = con.execute("SELECT a.*, t.name AS type_name FROM assets a JOIN asset_types t ON t.id=a.type_id WHERE a.id=?",
                          (aid,)).fetchone()
        if old is None:
            abort(404)
        name = names[0]
        if assetlib.switch_name(con, name):
            flash(_("%(name)s is already the name of a switch - asset and switch names must be unique.", name=name), "error")
            return redirect(back)
        vals = {"name": name, "type_id": t["id"], **extra}
        try:
            con.execute(f"UPDATE assets SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), aid))
        except sqlite3.IntegrityError:
            flash(_("An asset named %(name)s already exists - asset names are unique.", name=name), "error")
            return redirect(back)
        ch = diff(old, vals)
        ch.pop("type_id", None)
        if old["type_id"] != t["id"]:
            ch["eq_type"] = [old["type_name"], t["name"]]
        n = 0
        for e, ty in (("equipment", "eq_type"), ("equipment2", "eq2_type")):  # ports follow the rename / new type
            n += con.execute(f"UPDATE ports SET {e}=?, {ty}=? WHERE {e}=? COLLATE NOCASE AND {ty}=?",
                             (name, t["name"], old["name"], old["type_name"])).rowcount
        if n:
            ch["rows"] = [None, n]
        if ch:
            audit("documentation", "asset_update", "asset", aid, name, ch)
        con.commit()
        flash(_("Asset %(name)s saved.", name=name), "ok")
        return redirect(back)
    added, skipped = [], []
    for name in names:
        if assetlib.switch_name(con, name) or assetlib.find_asset(con, name) is not None:
            skipped.append(name)
            continue
        aid = con.execute("INSERT INTO assets(type_id, name, ip, mac, serial, location, notes, state, created_at) VALUES "
                          "(?,?,?,?,?,?,?,?,?)", (t["id"], name, *extra.values(), now_s())).lastrowid
        audit("documentation", "asset_create", "asset", aid, name,
              {"eq_type": [None, t["name"]]} | {k: [None, v] for k, v in extra.items() if v})
        added.append(name)
    con.commit()
    if added:
        flash(_("%(n)s assets added to %(t)s.", n=len(added), t=t["name"]), "ok")
    if skipped:
        flash(_("Already used (asset names are unique): %(list)s", list=", ".join(skipped)), "error")
    return redirect(back)


@app.route("/assets/<int:aid>/delete", methods=["POST"])
@require("assets", "modify")
def asset_delete(aid):
    con = db()
    row = con.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
    back = request.form.get("next") or url_for("assets_page")
    if row is None:
        abort(404)
    used = con.execute("""SELECT COUNT(*) FROM ports WHERE equipment=? COLLATE NOCASE OR equipment2=? COLLATE NOCASE""",
                       (row["name"], row["name"])).fetchone()[0]
    if used:
        flash(_("%(name)s is still connected to %(n)s ports - remove it from the ports first.", name=row["name"], n=used), "error")
        return redirect(back)
    con.execute("DELETE FROM assets WHERE id=?", (aid,))
    audit("documentation", "asset_delete", "asset", aid, row["name"])
    con.commit()
    flash(_("Asset %(name)s deleted.", name=row["name"]), "ok")
    return redirect(back)


@app.route("/assets/delete-unused", methods=["POST"])
@require("assets", "modify")
def assets_delete_unused():
    """Delete the assets of one type that are not connected to any port."""
    con = db()
    tid = int(request.form.get("type_id") or 0)
    rows = con.execute("""SELECT a.id, a.name FROM assets a WHERE a.type_id=? AND NOT EXISTS
                          (SELECT 1 FROM ports p WHERE p.equipment=a.name COLLATE NOCASE OR p.equipment2=a.name COLLATE NOCASE)""",
                       (tid,)).fetchall()
    for r in rows:
        con.execute("DELETE FROM assets WHERE id=?", (r["id"],))
    if rows:
        audit("documentation", "asset_delete", "asset", None, "", {"rows": [None, len(rows)],
                                                                    "name": [None, ", ".join(r["name"] for r in rows[:50])]})
    con.commit()
    flash(_("%(n)s unused assets deleted.", n=len(rows)), "ok")
    return redirect(url_for("assets_page", type=tid))


# -- groups
@app.route("/settings/groups")
@require("groups")
def groups():
    con = db()
    rows = con.execute("""SELECT g.*, (SELECT COUNT(*) FROM switch_groups sg JOIN switches s ON s.id=sg.switch_id
                          WHERE sg.group_id=g.id AND s.status='active') AS n_sw,
                          (SELECT COUNT(*) FROM user_groups ug WHERE ug.group_id=g.id) AS n_users
                          FROM sw_groups g ORDER BY g.name COLLATE NOCASE""").fetchall()
    mine = my_group_ids()
    if mine is not None:
        rows = [r for r in rows if r["id"] in mine]
    return render_template("groups.html", groups=rows)


@app.route("/settings/groups/new", methods=["GET", "POST"])
@app.route("/settings/groups/<int:gid>", methods=["GET", "POST"])
@require("groups", "edit")
def group_edit(gid=None):
    con = db()
    grp = con.execute("SELECT * FROM sw_groups WHERE id=?", (gid,)).fetchone() if gid else None
    if gid and (not grp or (my_group_ids() is not None and gid not in my_group_ids())):
        abort(404)
    clause, params = vis("s")
    switches = con.execute(f"SELECT s.id, s.name, s.area FROM switches s WHERE 1=1{clause} ORDER BY s.area, s.name",
                           params).fetchall()
    members = {r[0] for r in con.execute("SELECT switch_id FROM switch_groups WHERE group_id=?", (gid,))} if gid else set()
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash(_("Group name is required."), "error")
            return redirect(request.url)
        vals = {"name": name, "description": f.get("description", "").strip(), "color": f.get("color", "#0f766e")}
        changes = diff(grp, vals) if grp else {k: [None, v] for k, v in vals.items() if v}
        try:
            if grp:
                con.execute("UPDATE sw_groups SET name=?, description=?, color=? WHERE id=?", (*vals.values(), gid))
            else:
                gid = con.execute("INSERT INTO sw_groups(name, description, color) VALUES (?,?,?)",
                                  tuple(vals.values())).lastrowid
                if not sees_all():  # creator keeps access to the group they made
                    con.execute("INSERT OR IGNORE INTO user_groups(user_id, group_id) VALUES (?,?)",
                                (current_user()["id"], gid))
        except sqlite3.IntegrityError:
            flash(_("A group with that name already exists."), "error")
            return redirect(request.url)
        chosen = {int(x) for x in f.getlist("switch_ids")}
        before = sorted(s["name"] for s in switches if s["id"] in members)
        for s in switches:
            if s["id"] in chosen:
                con.execute("INSERT OR IGNORE INTO switch_groups(switch_id, group_id) VALUES (?,?)", (s["id"], gid))
            else:
                con.execute("DELETE FROM switch_groups WHERE switch_id=? AND group_id=?", (s["id"], gid))
        after = sorted(s["name"] for s in switches if s["id"] in chosen)
        if before != after:
            added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
            changes["switches"] = [("− " + ", ".join(removed)) if removed else "", ("+ " + ", ".join(added)) if added else ""]
        if changes or not grp:
            audit("security", "group_update" if grp else "group_create", "group", gid, name, changes)
        con.commit()
        flash(_("Group %(name)s saved.", name=name), "ok")
        return redirect(url_for("groups"))
    users = con.execute("""SELECT u.* FROM users u JOIN user_groups ug ON ug.user_id=u.id WHERE ug.group_id=?
                           ORDER BY u.username""", (gid,)).fetchall() if gid else []
    n = con.execute("SELECT COUNT(*) FROM sw_groups").fetchone()[0]
    return render_template("group_form.html", grp=grp, switches=switches, members=members, users=users,
                           default_color=GROUP_COLORS[n % len(GROUP_COLORS)])


@app.route("/settings/groups/<int:gid>/delete", methods=["POST"])
@require("groups", "modify")
def group_delete(gid):
    row = db().execute("SELECT name FROM sw_groups WHERE id=?", (gid,)).fetchone()
    if row:
        audit("security", "group_delete", "group", gid, row["name"])
        db().execute("DELETE FROM sw_groups WHERE id=?", (gid,))
        db().commit()
    flash(_("Group deleted. Its switches still exist but users who only saw them through this group lose access."), "ok")
    return redirect(url_for("groups"))


# -- VLANs
@app.route("/settings/vlans", methods=["GET", "POST"])
@require("vlans")
def vlans():
    con = db()
    if request.method == "POST":
        if not can("vlans", "edit"):
            abort(403)
        f = request.form
        try:
            number = int(f["number"])
            if not 1 <= number <= 4094:
                raise ValueError
        except (KeyError, ValueError):
            flash(_("VLAN number must be between 1 and 4094."), "error")
            return redirect(url_for("vlans"))
        vals = {"number": number, "name": f.get("name", "").strip(), "subnet": f.get("subnet", "").strip(),
                "dhcp": 1 if f.get("dhcp") else 0, "notes": f.get("notes", "").strip()}
        try:
            if f.get("id"):
                old = con.execute("SELECT * FROM vlans WHERE id=?", (int(f["id"]),)).fetchone()
                con.execute("UPDATE vlans SET number=?, name=?, subnet=?, dhcp=?, notes=? WHERE id=?",
                            (*vals.values(), int(f["id"])))
                ch = diff(old, vals)
                if ch:
                    audit("settings", "vlan_update", "vlan", int(f["id"]), str(number), ch)
            else:
                vid = con.execute("INSERT INTO vlans(number,name,subnet,dhcp,notes) VALUES (?,?,?,?,?)",
                                  tuple(vals.values())).lastrowid
                audit("settings", "vlan_create", "vlan", vid, str(number), {k: [None, v] for k, v in vals.items() if v})
            con.commit()
            flash(_("VLAN %(n)s saved.", n=number), "ok")
        except sqlite3.IntegrityError:
            flash(_("VLAN %(n)s already exists.", n=number), "error")
        return redirect(url_for("vlans"))
    rows = con.execute("""SELECT v.*, (SELECT COUNT(*) FROM ports p WHERE p.vlan = CAST(v.number AS TEXT)) AS used
                          FROM vlans v ORDER BY number""").fetchall()
    return render_template("vlans.html", vlans=rows)


@app.route("/settings/vlans/<int:vid>/delete", methods=["POST"])
@require("vlans", "modify")
def vlan_delete(vid):
    row = db().execute("SELECT * FROM vlans WHERE id=?", (vid,)).fetchone()
    if row:
        audit("settings", "vlan_delete", "vlan", vid, str(row["number"]), {"name": [row["name"], None]})
        db().execute("DELETE FROM vlans WHERE id=?", (vid,))
        db().commit()
    flash(_("VLAN deleted."), "ok")
    return redirect(url_for("vlans"))


# -- port types
@app.route("/settings/port-types", methods=["GET", "POST"])
@require("port_types")
def port_types():
    con = db()
    if request.method == "POST":
        if not can("port_types", "edit"):
            abort(403)
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash(_("Port type name is required."), "error")
            return redirect(url_for("port_types"))
        try:
            sort = int(f.get("sort") or 0)
        except ValueError:
            sort = 0
        vals = {"name": name, "color": f.get("color", "#64748b"), "description": f.get("description", "").strip(),
                "sort": sort}
        try:
            if f.get("id"):
                old = con.execute("SELECT * FROM port_types WHERE id=?", (int(f["id"]),)).fetchone()
                con.execute("UPDATE port_types SET name=?, color=?, description=?, sort=? WHERE id=?",
                            (*vals.values(), int(f["id"])))
                if old and old["name"] != name:
                    n = con.execute("UPDATE ports SET ptype=? WHERE ptype=?", (name, old["name"])).rowcount
                    flash(_("Renamed on %(n)s ports.", n=n), "ok")
                ch = diff(old, vals)
                if ch:
                    audit("settings", "port_type_update", "port_type", int(f["id"]), name, ch)
            else:
                tid = con.execute("INSERT INTO port_types(name,color,description,sort) VALUES (?,?,?,?)",
                                  tuple(vals.values())).lastrowid
                audit("settings", "port_type_create", "port_type", tid, name, {k: [None, v] for k, v in vals.items()})
            con.commit()
            flash(_("Port type %(name)s saved.", name=name), "ok")
        except sqlite3.IntegrityError:
            flash(_("Port type %(name)s already exists.", name=name), "error")
        return redirect(url_for("port_types"))
    rows = con.execute("""SELECT t.*, (SELECT COUNT(*) FROM ports p WHERE p.ptype=t.name) AS used
                          FROM port_types t ORDER BY sort, name""").fetchall()
    return render_template("port_types.html", types=rows)


@app.route("/settings/port-types/<int:tid>/delete", methods=["POST"])
@require("port_types", "modify")
def port_type_delete(tid):
    con = db()
    t = con.execute("SELECT * FROM port_types WHERE id=?", (tid,)).fetchone()
    if t:
        used = con.execute("SELECT COUNT(*) FROM ports WHERE ptype=?", (t["name"],)).fetchone()[0]
        repl = request.form.get("replace", "")
        if used and not repl:
            flash(_("%(n)s ports use '%(name)s'. Choose a replacement type to delete it.", n=used, name=t["name"]), "error")
            return redirect(url_for("port_types"))
        if used:
            con.execute("UPDATE ports SET ptype=? WHERE ptype=?", (repl, t["name"]))
        con.execute("DELETE FROM port_types WHERE id=?", (tid,))
        audit("settings", "port_type_delete", "port_type", tid, t["name"], {"ptype": [t["name"], repl or None]})
        con.commit()
        flash(_("Port type %(name)s deleted.", name=t["name"]), "ok")
    return redirect(url_for("port_types"))


# -- switch models
@app.route("/settings/models")
@require("models")
def models():
    rows = db().execute("""SELECT m.*, (SELECT COUNT(*) FROM switches s WHERE s.model=m.name AND s.status='active') AS used
                           FROM models m ORDER BY m.vendor COLLATE NOCASE, m.name COLLATE NOCASE""").fetchall()
    return render_template("models.html", models=rows, layers=dict(LAYERS))


@app.route("/settings/models/new", methods=["GET", "POST"])
@app.route("/settings/models/<int:mid>", methods=["GET", "POST"])
@require("models", "edit")
def model_edit(mid=None):
    con = db()
    m = con.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone() if mid else None
    if mid and not m:
        abort(404)
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash(_("Model name is required."), "error")
            return redirect(request.url)

        def num(k):
            try:
                return max(0, int(f.get(k) or 0))
            except ValueError:
                return 0
        sfp = 1 if f.get("sfp") else 0
        vals = {"name": name, "vendor": f.get("vendor", "").strip(),
                "layer": f.get("layer") if f.get("layer") in dict(LAYERS) else "L2",
                "poe": 1 if f.get("poe") else 0, "sfp": sfp, "sfp_count": num("sfp_count") if sfp else 0,
                "port_count": num("port_count"), "notes": f.get("notes", "").strip()}
        try:
            if m:
                con.execute(f"UPDATE models SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), mid))
                if m["name"] != name:
                    con.execute("UPDATE switches SET model=? WHERE model=?", (name, m["name"]))
                ch = diff(m, vals)
                if ch:
                    audit("settings", "model_update", "model", mid, name, ch)
            else:
                mid = con.execute(f"INSERT INTO models({', '.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                                  tuple(vals.values())).lastrowid
                audit("settings", "model_create", "model", mid, name, {k: [None, v] for k, v in vals.items() if v})
            con.commit()
        except sqlite3.IntegrityError:
            flash(_("A model with that name already exists."), "error")
            return redirect(request.url)
        flash(_("Model %(name)s saved.", name=name), "ok")
        return redirect(url_for("models"))
    clause, params = vis("s")
    used_by = con.execute(f"SELECT s.id, s.name FROM switches s WHERE s.model=?{clause} ORDER BY s.name",
                          (m["name"], *params)).fetchall() if m else []
    return render_template("model_form.html", m=m, layers=LAYERS, used_by=used_by)


@app.route("/settings/models/<int:mid>/delete", methods=["POST"])
@require("models", "modify")
def model_delete(mid):
    row = db().execute("SELECT name FROM models WHERE id=?", (mid,)).fetchone()
    if row:
        audit("settings", "model_delete", "model", mid, row["name"])
        db().execute("DELETE FROM models WHERE id=?", (mid,))
        db().commit()
    flash(_("Model deleted (switches keep the model name as text)."), "ok")
    return redirect(url_for("models"))


# -- authorisation profiles
@app.route("/settings/profiles")
@require("profiles")
def profiles():
    rows = db().execute("""SELECT a.*, (SELECT COUNT(*) FROM core.memberships m WHERE m.profile_id=a.id) AS n_users
                           FROM auth_profiles a ORDER BY a.name""").fetchall()
    rows = [dict(r, perms=json.loads(r["perms"] or "{}")) for r in rows]
    return render_template("profiles.html", profiles=rows, menus=MENUS, level_names=dict(LEVEL_LABELS))


@app.route("/settings/profiles/new", methods=["GET", "POST"])
@app.route("/settings/profiles/<int:aid>", methods=["GET", "POST"])
@require("profiles", "edit")
def profile_edit(aid=None):
    con = db()
    prof = con.execute("SELECT * FROM auth_profiles WHERE id=?", (aid,)).fetchone() if aid else None
    if aid and not prof:
        abort(404)
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            flash(_("Profile name is required."), "error")
            return redirect(request.url)
        perms = {k: f.get(f"perm_{k}") if f.get(f"perm_{k}") in LEVELS else "none" for k in MENU_KEYS}
        if not current_user()["is_admin"]:  # nobody can grant more than they have
            mine = perms_of(current_user())
            inv = {v: k for k, v in LEVELS.items()}
            perms = {k: inv[min(LEVELS[v], mine[k])] for k, v in perms.items()}
        old_perms = json.loads(prof["perms"]) if prof else {}
        changes = {f"perm_{k}": [old_perms.get(k, "none") if prof else None, v] for k, v in perms.items()
                   if old_perms.get(k, "none") != v or not prof}
        changes.update(diff(prof, {"name": name, "description": f.get("description", "").strip()}) if prof else
                       {"name": [None, name]})
        try:
            if prof:
                con.execute("UPDATE auth_profiles SET name=?, description=?, perms=? WHERE id=?",
                            (name, f.get("description", "").strip(), json.dumps(perms), aid))
            else:
                aid = con.execute("INSERT INTO auth_profiles(name, description, perms) VALUES (?,?,?)",
                                  (name, f.get("description", "").strip(), json.dumps(perms))).lastrowid
            if changes:
                audit("security", "profile_update" if prof else "profile_create", "profile", aid, name, changes)
            con.commit()
        except sqlite3.IntegrityError:
            flash(_("A profile with that name already exists."), "error")
            return redirect(request.url)
        flash(_("Authorisation profile %(name)s saved.", name=name), "ok")
        return redirect(url_for("profiles"))
    perms = json.loads(prof["perms"]) if prof else {}
    users = con.execute("SELECT * FROM users WHERE profile_id=? ORDER BY username", (aid,)).fetchall() if aid else []
    return render_template("profile_form.html", prof=prof, perms=perms, menus=MENUS, levels=LEVEL_LABELS, users=users)


@app.route("/settings/profiles/<int:aid>/delete", methods=["POST"])
@require("profiles", "modify")
def profile_delete(aid):
    row = db().execute("SELECT name FROM auth_profiles WHERE id=?", (aid,)).fetchone()
    if row:
        audit("security", "profile_delete", "profile", aid, row["name"])
        db().execute("DELETE FROM auth_profiles WHERE id=?", (aid,))
        db().commit()
    flash(_("Profile deleted. Users that had it now have no access until a profile is assigned."), "ok")
    return redirect(url_for("profiles"))


# -- users
@app.route("/settings/users")
@require("users")
def users():
    scope = request.args.get("scope", "company")
    rows = db().execute(f"""SELECT u.*, a.name AS profile_name,
            (SELECT group_concat(g.name, ', ') FROM user_groups ug JOIN sw_groups g ON g.id=ug.group_id
             WHERE ug.user_id=u.id) AS group_names,
            (SELECT group_concat(c.code || ' ' || p.name, ' · ') FROM core.memberships m
               JOIN core.companies c ON c.id=m.company_id JOIN auth_profiles p ON p.id=m.profile_id
             WHERE m.user_id=u.id) AS roles,
            (SELECT d.label FROM core.directories d WHERE d.id=u.directory_id) AS dir_label
            FROM users u LEFT JOIN auth_profiles a ON a.id=u.profile_id
            {"" if scope == "all" else "WHERE u.profile_id IS NOT NULL OR u.is_admin=1"}
            ORDER BY u.active DESC, u.last_name COLLATE NOCASE, u.username""").fetchall()
    return render_template("users.html", users=rows, scope=scope)


@app.route("/settings/users/new", methods=["GET", "POST"])
@app.route("/settings/users/<int:uid>", methods=["GET", "POST"])
@require("users", "edit")
def user_edit(uid=None):
    con = db()
    usr = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    if uid and not usr:
        abort(404)
    me = current_user()
    if usr and usr["is_admin"] and not me["is_admin"]:
        abort(403)
    member = {r[0] for r in con.execute("SELECT group_id FROM user_groups WHERE user_id=?", (uid,))} if uid else set()
    profiles_ = con.execute("SELECT * FROM auth_profiles ORDER BY name").fetchall()
    domain = bool(usr and usr["auth_source"] == "directory")
    # roles in the other companies (administrators only)
    others = []
    if me["is_admin"]:
        held = {r["company_id"]: r for r in con.execute("SELECT * FROM core.memberships WHERE user_id=?", (uid or 0,))}
        others = [dict(c, role=held.get(c["id"])) for c in companies_all() if c["id"] != con.cid]
    if request.method == "POST":
        f = request.form
        username = f.get("username", "").strip()
        pw = f.get("password", "")
        if domain:
            username, pw = usr["username"], ""  # managed by the directory
        errors = []
        if not username:
            errors.append(_("Username is required."))
        if not usr and not pw:
            errors.append(_("A password is required."))
        if pw:
            bad = password_problems(pw, username)
            if bad:
                errors.append(_("The password does not respect the policy: %(list)s", list=", ".join(bad)))
        vals = {"username": username, "first_name": f.get("first_name", "").strip(),
                "last_name": f.get("last_name", "").strip(), "email": f.get("email", "").strip(),
                "title": f.get("title", "").strip(),
                "profile_id": int(f["profile_id"]) if f.get("profile_id") else None,
                "active": 1 if f.get("active") else 0,
                "all_groups": 1 if f.get("all_groups") and sees_all() else 0,
                "lang": f.get("lang") if f.get("lang") in LANGUAGES else ""}
        if me["is_admin"]:
            vals["is_admin"] = 1 if f.get("is_admin") else 0
        if usr and usr["id"] == me["id"]:
            vals["active"] = 1
            if me["is_admin"]:
                vals["is_admin"] = 1  # can't demote yourself
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("user_form.html", usr=f, member={int(x) for x in f.getlist("group_ids")},
                                   profiles=profiles_, groups=visible_groups(), others=others, domain=domain)
        pnames = {p["id"]: p["name"] for p in profiles_}
        changes = diff(usr, vals) if usr else {k: [None, v] for k, v in vals.items() if v not in ("", None, 0)}
        if "profile_id" in changes:
            o, n = changes.pop("profile_id")
            changes["profile"] = [pnames.get(o), pnames.get(n)]
        if pw:
            vals["pw_hash"] = generate_password_hash(pw)
            vals["must_change"] = 0 if f.get("pw_mode") == "permanent" else 1
            vals["pw_changed_at"] = now()
            changes["password"] = [None, _("temporary - to change at next sign-in") if vals["must_change"] else _("permanent")]
        try:
            if usr:
                con.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), uid))
            else:
                con.execute(f"INSERT INTO users({', '.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                            tuple(vals.values()))
                uid = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        except sqlite3.IntegrityError:
            flash(_("That username already exists."), "error")
            return redirect(request.url)
        chosen = {int(x) for x in f.getlist("group_ids")}
        gnames = {gr["id"]: gr["name"] for gr in visible_groups()}
        for gid in gnames:
            if gid in chosen:
                con.execute("INSERT OR IGNORE INTO user_groups(user_id, group_id) VALUES (?,?)", (uid, gid))
            else:
                con.execute("DELETE FROM user_groups WHERE user_id=? AND group_id=?", (uid, gid))
        before = sorted(gnames[x] for x in member if x in gnames)
        after = sorted(gnames[x] for x in chosen if x in gnames)
        if before != after:
            changes["groups"] = [", ".join(before), ", ".join(after)]
        for c in others:  # roles in the other companies
            key = f"role_{c['id']}"
            if key not in f:
                continue
            new_p = int(f[key]) if f[key] else None
            new_all = 1 if f.get(f"allg_{c['id']}") else 0
            old = c["role"]
            if (old["profile_id"] if old else None) == new_p and (old["all_groups"] if old else 0) == new_all:
                continue
            if new_p is None:
                con.execute("DELETE FROM core.memberships WHERE user_id=? AND company_id=?", (uid, c["id"]))
            else:
                con.execute("INSERT OR REPLACE INTO core.memberships(user_id, company_id, profile_id, all_groups, source) "
                            "VALUES (?,?,?,?, 'manual')", (uid, c["id"], new_p, new_all))
            changes[f"role {c['code'] or c['name']}"] = [pnames.get(old["profile_id"]) if old else None, pnames.get(new_p)]
        if changes:
            audit("security", "user_update" if usr else "user_create", "user", uid, username, changes)
        con.commit()
        flash(_("User %(name)s saved.", name=username), "ok")
        return redirect(url_for("users"))
    return render_template("user_form.html", usr=usr, member=member, profiles=profiles_, groups=visible_groups(),
                           others=others, domain=domain)


@app.route("/settings/users/<int:uid>/delete", methods=["POST"])
@require("users", "modify")
def user_delete(uid):
    con = db()
    usr = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not usr or uid == current_user()["id"]:
        flash(_("You cannot delete your own account."), "error")
    elif usr["is_admin"] and not current_user()["is_admin"]:
        abort(403)
    elif usr["is_admin"] and con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] <= 1:
        flash(_("This is the last administrator - it cannot be deleted."), "error")
    else:
        audit("security", "user_delete", "user", uid, usr["username"],
              {"first_name": [usr["first_name"], None], "last_name": [usr["last_name"], None], "email": [usr["email"], None]})
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        con.execute("DELETE FROM user_groups WHERE user_id=?", (uid,))
        con.execute("DELETE FROM ticket_assignees WHERE user_id=?", (uid,))
        con.commit()
        for c in companies_all():  # his switch groups and ticket assignments in the other companies
            if c["id"] != con.cid:
                other = connect(c["id"])
                other.execute("DELETE FROM user_groups WHERE user_id=?", (uid,))
                other.execute("DELETE FROM ticket_assignees WHERE user_id=?", (uid,))
                other.commit()
                other.close()
        flash(_("User %(name)s deleted.", name=usr["username"]), "ok")
    return redirect(url_for("users"))


# ---------------------------------------------------------------- companies & directories (administrators)
def require_admin():
    if not (current_user() and current_user()["is_admin"]):
        abort(403)


@app.route("/company/<int:cid>")
def company_switch(cid):
    if cid not in [c["id"] for c in allowed_companies(current_user())]:
        abort(403)
    session["cid"] = cid
    flash(_("You are now working in %(c)s.", c=company_by_id(cid)["name"]), "ok")
    return redirect(url_for("dashboard"))


def _company_stats():
    out = {}
    con = db()
    for c in companies_all():
        try:
            other = con if c["id"] == con.cid else connect(c["id"])
            n_sw = other.execute("SELECT COUNT(*) FROM switches WHERE status='active'").fetchone()[0]
            if other is not con:
                other.close()
        except sqlite3.Error:
            n_sw = None
        n_users = con.execute("SELECT COUNT(*) FROM core.memberships WHERE company_id=? AND profile_id IS NOT NULL",
                              (c["id"],)).fetchone()[0]
        d = con.execute("SELECT * FROM core.directories WHERE company_id=?", (c["id"],)).fetchone()
        out[c["id"]] = {"switches": n_sw, "users": n_users, "directory": d}
    return out


@app.route("/settings/companies", methods=["GET", "POST"])
def companies_page():
    require_admin()
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        code = request.form.get("code", "").strip()[:12]
        if not name:
            flash(_("The name is required."), "error")
        elif any(c["name"].lower() == name.lower() for c in companies_all()):
            flash(_("A company named %(name)s already exists.", name=name), "error")
        else:
            cid = create_company(name, code)
            audit("settings", "company_create", "company", cid, name, {"code": [None, code or _initials(name)]})
            db().commit()
            flash(_("Company %(name)s created with an empty documentation. Give roles to its users, then switch to it "
                    "to add its switches (or import its workbook).", name=name), "ok")
            return redirect(url_for("company_edit", cid=cid))
    return render_template("companies.html", companies=companies_all(), stats=_company_stats())


@app.route("/settings/companies/<int:cid>", methods=["GET", "POST"])
def company_edit(cid):
    require_admin()
    con = db()
    comp = company_by_id(cid)
    if comp is None:
        abort(404)
    d = con.execute("SELECT * FROM core.directories WHERE company_id=?", (cid,)).fetchone()
    profiles_ = con.execute("SELECT * FROM auth_profiles ORDER BY name").fetchall()
    pnames = {p["id"]: p["name"] for p in profiles_}
    test = None
    if request.method == "POST":
        f = request.form
        act = f.get("action")
        if act == "company":
            vals = {"name": f.get("name", "").strip()[:120], "code": f.get("code", "").strip()[:12],
                    "notes": f.get("notes", "").strip()[:500], "active": 1 if f.get("active") else 0}
            if not vals["name"]:
                flash(_("The name is required."), "error")
            elif cid == con.cid and not vals["active"]:
                flash(_("You cannot deactivate the company you are working in - switch to another company first."), "error")
            else:
                try:
                    con.execute("UPDATE core.companies SET name=?, code=?, notes=?, active=? WHERE id=?",
                                (*vals.values(), cid))
                except sqlite3.IntegrityError:
                    flash(_("A company named %(name)s already exists.", name=vals["name"]), "error")
                    return redirect(request.url)
                ch = diff(comp, vals)
                if ch:
                    audit("settings", "company_update", "company", cid, vals["name"], ch)
                con.commit()
                companies_all(reload=True)
                flash(_("Company saved."), "ok")
            return redirect(url_for("company_edit", cid=cid))
        if act == "delete":
            if f.get("confirm", "").strip() != comp["name"]:
                flash(_("Type the exact name of the company to delete it."), "error")
            elif cid == con.cid:
                flash(_("You cannot delete the company you are working in - switch to another company first."), "error")
            else:
                con.execute("DELETE FROM core.companies WHERE id=?", (cid,))
                audit("settings", "company_delete", "company", cid, comp["name"], {"dbfile": [comp["dbfile"], None]})
                con.commit()
                companies_all(reload=True)
                path = os.path.join(DATA, comp["dbfile"])
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                for ext in ("", "-wal", "-shm"):  # kept aside, not destroyed
                    if os.path.exists(path + ext):
                        os.replace(path + ext, os.path.join(DATA, f"deleted-{stamp}-{comp['dbfile']}{ext}"))
                flash(_("Company %(name)s deleted. Its database file was kept in the data folder as deleted-….db.",
                        name=comp["name"]), "ok")
                return redirect(url_for("companies_page"))
            return redirect(url_for("company_edit", cid=cid))
        if act in ("directory", "test", "test_user"):
            vals = {"enabled": 1 if f.get("enabled") else 0, "label": f.get("label", "").strip()[:60],
                    "servers": f.get("servers", "").strip()[:300], "security": f.get("security") if f.get("security") in
                    ("ldaps", "starttls", "plain") else "ldaps", "port": int(f.get("port") or 0) if (f.get("port") or "0").isdigit() else 0,
                    "verify_cert": 1 if f.get("verify_cert") else 0, "base_dn": f.get("base_dn", "").strip()[:300],
                    "bind_user": f.get("bind_user", "").strip()[:200], "upn_suffix": f.get("upn_suffix", "").strip().lstrip("@")[:120],
                    "netbios": f.get("netbios", "").strip().upper()[:30], "user_filter": f.get("user_filter", "").strip()[:400],
                    "nested": 1 if f.get("nested") else 0,
                    "default_profile_id": int(f["default_profile_id"]) if f.get("default_profile_id") else None,
                    "timeout": max(1, min(30, int(f.get("timeout") or 5))) if (f.get("timeout") or "5").isdigit() else 5}
            secret = f.get("bind_pass", "")
            if f.get("clear_bind_pass"):
                vals["bind_pass"] = ""
            elif secret:
                vals["bind_pass"] = enc(secret)
            if vals["user_filter"] and "{username}" not in vals["user_filter"]:
                flash(_("The user filter must contain {username}."), "error")
                return redirect(url_for("company_edit", cid=cid) + "#directory")
            if vals["user_filter"]:
                try:
                    ldapauth.encode_filter(vals["user_filter"].replace("{username}", "x"))
                except (ldapauth.LdapError, IndexError):
                    flash(_("The user filter is not a valid LDAP filter."), "error")
                    return redirect(url_for("company_edit", cid=cid) + "#directory")
            vals["updated_at"] = now()
            # save (the tests too, so a password typed is kept), then test the saved settings
            if d is None:
                con.execute(f"INSERT INTO core.directories(company_id, {', '.join(vals)}) VALUES (?, "
                            f"{', '.join('?' * len(vals))})", (cid, *vals.values()))
            else:
                con.execute(f"UPDATE core.directories SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?",
                            (*vals.values(), d["id"]))
            ch = diff(d, vals, [k for k in vals if k not in ("updated_at", "bind_pass")]) if d else \
                {k: [None, v] for k, v in vals.items() if v and k not in ("updated_at", "bind_pass")}
            if "bind_pass" in vals:
                ch["bind_pass"] = [None, "••••••" if vals["bind_pass"] else ""]
            if ch:
                audit("security", "directory_update", "company", cid, comp["name"], ch)
            con.commit()
            if act == "directory":
                flash(_("Domain sign-in settings saved."), "ok")
                return redirect(url_for("company_edit", cid=cid) + "#directory")
            d = con.execute("SELECT * FROM core.directories WHERE company_id=?", (cid,)).fetchone()
            cfg = dict(d)
            bind_pw = dec(d["bind_pass"])
            try:
                if act == "test":
                    host, n = ldapauth.test_connection(cfg, bind_pw)
                    test = {"ok": True, "text": _("Connected to %(h)s.", h=host) + " " + (
                        _("The service account can read the directory (%(n)s users seen in the first page).", n=n)
                        if cfg.get("bind_user") else _("No service account: users bind with their own password."))}
                else:
                    info = ldapauth.authenticate(cfg, f.get("t_user", ""), f.get("t_pass", ""), bind_pw)
                    rules = con.execute("SELECT * FROM core.dir_groups WHERE directory_id=? ORDER BY sort, id",
                                        (d["id"] if d else 0,)).fetchall()
                    hit = next((r for r in rules if ldapauth.matches(r["ad_group"], info["groups"], info["group_names"])), None)
                    role = pnames.get(hit["profile_id"]) if hit else pnames.get(vals["default_profile_id"])
                    test = {"ok": True, "user": info, "rule": hit["ad_group"] if hit else None, "role": role,
                            "username": directory_username(cfg, info)}
            except ldapauth.LdapError as e:
                test = {"ok": False, "text": f"{e.code}: {e}"}
            audit("security", "directory_test", "company", cid, comp["name"],
                  {"result": [None, "ok" if test["ok"] else test.get("text", "")[:200]]})
            con.commit()
        if act == "map_add" and d is not None:
            grp = f.get("ad_group", "").strip()[:300]
            if grp and f.get("profile_id"):
                con.execute("INSERT INTO core.dir_groups(directory_id, ad_group, profile_id, all_groups, sw_groups, sort) "
                            "VALUES (?,?,?,?,?, (SELECT COALESCE(MAX(sort),0)+1 FROM core.dir_groups WHERE directory_id=?))",
                            (d["id"], grp, int(f["profile_id"]), 1 if f.get("all_groups") else 0,
                             ", ".join(f.getlist("sw_groups")), d["id"]))
                audit("security", "directory_update", "company", cid, comp["name"],
                      {"group mapping": [None, f"{grp} → {pnames.get(int(f['profile_id']))}"]})
                con.commit()
            else:
                flash(_("Give the AD group and the role."), "error")
            return redirect(url_for("company_edit", cid=cid) + "#mapping")
        if act in ("map_del", "map_up", "map_save") and d is not None:
            mid = int(f.get("map_id") or 0)
            rule = con.execute("SELECT * FROM core.dir_groups WHERE id=? AND directory_id=?", (mid, d["id"])).fetchone()
            if rule and act == "map_save":
                grp = f.get("ad_group", "").strip()[:300]
                if not grp or not f.get("profile_id"):
                    flash(_("Give the AD group and the role."), "error")
                    return redirect(url_for("company_edit", cid=cid) + "#mapping")
                new = {"ad_group": grp, "profile_id": int(f["profile_id"]), "all_groups": 1 if f.get("all_groups") else 0,
                       "sw_groups": ", ".join(f.getlist("sw_groups"))}
                con.execute("UPDATE core.dir_groups SET ad_group=?, profile_id=?, all_groups=?, sw_groups=? WHERE id=?",
                            (*new.values(), mid))
                audit("security", "directory_update", "company", cid, comp["name"],
                      {"group mapping": [f"{rule['ad_group']} → {pnames.get(rule['profile_id'])}"
                                         f"{' (all switches)' if rule['all_groups'] else ''}",
                                         f"{grp} → {pnames.get(new['profile_id'])}"
                                         f"{' (all switches)' if new['all_groups'] else ''}"]})
                con.commit()
                flash(_("Line saved. It applies at each user's next sign-in."), "ok")
            elif rule:
                if act == "map_del":
                    con.execute("DELETE FROM core.dir_groups WHERE id=?", (mid,))
                    audit("security", "directory_update", "company", cid, comp["name"],
                          {"group mapping": [f"{rule['ad_group']} → {pnames.get(rule['profile_id'])}", None]})
                else:
                    rows = con.execute("SELECT id FROM core.dir_groups WHERE directory_id=? ORDER BY sort, id",
                                       (d["id"],)).fetchall()
                    order = [r[0] for r in rows]
                    i = order.index(mid)
                    if i > 0:
                        order[i - 1], order[i] = order[i], order[i - 1]
                    for k, rid in enumerate(order):
                        con.execute("UPDATE core.dir_groups SET sort=? WHERE id=?", (k + 1, rid))
                con.commit()
            return redirect(url_for("company_edit", cid=cid) + "#mapping")
    maps = con.execute("SELECT * FROM core.dir_groups WHERE directory_id=? ORDER BY sort, id",
                       (d["id"] if d and d["id"] else 0,)).fetchall()
    other = con if cid == con.cid else connect(cid)
    sw_groups = [r[0] for r in other.execute("SELECT name FROM sw_groups ORDER BY name COLLATE NOCASE")]
    if other is not con:
        other.close()
    members = con.execute("""SELECT u.username, u.first_name, u.last_name, u.auth_source, m.source, p.name AS profile
                             FROM core.memberships m JOIN core.accounts u ON u.id=m.user_id
                             LEFT JOIN auth_profiles p ON p.id=m.profile_id WHERE m.company_id=?
                             ORDER BY u.last_name COLLATE NOCASE, u.username""", (cid,)).fetchall()
    return render_template("company_form.html", comp=comp, d=d, maps=maps, profiles=profiles_, pnames=pnames,
                           sw_groups=sw_groups, test=test, members=members, form=None)


# -- audit log
def audit_source():
    """This company's audit log + the shared events (sign-ins, users, security) of its users;
    administrators see every shared event."""
    con = db()
    if con.cid is None:
        return "core.audit"
    shared = "" if current_user()["is_admin"] else \
        f" WHERE user_id IN (SELECT user_id FROM core.memberships WHERE company_id={int(con.cid)})"
    return f"(SELECT * FROM main.audit UNION ALL SELECT * FROM core.audit{shared})"


def _audit_query():
    a = request.args
    where, params = ["1=1"], []
    if a.get("cat"):
        where.append("category=?")
        params.append(a["cat"])
    if a.get("action"):
        where.append("action=?")
        params.append(a["action"])
    if a.get("user"):
        where.append("username=?")
        params.append(a["user"])
    if a.get("frm"):
        where.append("ts>=?")
        params.append(a["frm"])
    if a.get("to"):
        where.append("ts<=?")
        params.append(a["to"] + " 23:59:59")
    if a.get("q"):
        where.append("(target_name LIKE ? OR details LIKE ? OR ip LIKE ?)")
        params += [f"%{a['q']}%"] * 3
    return " AND ".join(where), params


@app.route("/settings/audit")
@require("audit")
def audit_log():
    con = db()
    where, params = _audit_query()
    page = max(1, int(request.args.get("page", 1) or 1))
    per = 100
    src = audit_source()
    total = con.execute(f"SELECT COUNT(*) FROM {src} WHERE {where}", params).fetchone()[0]
    rows = con.execute(f"SELECT * FROM {src} WHERE {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                       (*params, per, (page - 1) * per)).fetchall()
    rows = [dict(r, details=json.loads(r["details"] or "{}")) for r in rows]
    usernames = [r[0] for r in con.execute(f"SELECT DISTINCT username FROM {src} WHERE username<>'' ORDER BY username")]
    actions_used = [r[0] for r in con.execute(f"SELECT DISTINCT action FROM {src} ORDER BY action")]
    args = {k: v for k, v in request.args.items() if k != "page"}
    oldest = con.execute(f"SELECT MIN(ts) FROM {src}").fetchone()[0]
    return render_template("audit.html", rows=rows, total=total, page=page, pages=max(1, -(-total // per)),
                           oldest=oldest, today=datetime.date.today().isoformat(),
                           usernames=usernames, actions_used=actions_used, categories=AUDIT_CATEGORIES,
                           actions=AUDIT_ACTIONS, labels=all_field_labels(), args=args,
                           levels=dict(LEVEL_LABELS), status_labels=dict(SWITCH_STATUSES + STATUSES))


@app.route("/settings/audit/purge", methods=["POST"])
@require("audit", "modify")
def audit_purge():
    con = db()
    before = request.form.get("before", "")
    try:
        day = datetime.date.fromisoformat(before)
    except ValueError:
        flash(_("Choose a valid date."), "error")
        return redirect(url_for("audit_log"))
    if day > datetime.date.today():
        flash(_("The date cannot be in the future."), "error")
        return redirect(url_for("audit_log"))
    n = con.execute("DELETE FROM main.audit WHERE ts < ?", (day.isoformat(),)).rowcount
    if current_user()["is_admin"]:  # sign-ins, users, security: shared by all companies
        n += con.execute("DELETE FROM core.audit WHERE ts < ?", (day.isoformat(),)).rowcount
    audit("security", "audit_purge", "audit", None, "", {"before": [None, day.isoformat()], "rows": [None, n]})
    con.commit()
    con.execute("VACUUM")
    flash(_("%(n)s audit entries before %(d)s were deleted.", n=n, d=day.isoformat()), "ok")
    return redirect(url_for("audit_log"))


@app.route("/settings/audit/export.xlsx")
@require("audit", "edit")
def audit_export():
    import importer
    con = db()
    where, params = _audit_query()
    rows = con.execute(f"SELECT * FROM {audit_source()} WHERE {where} ORDER BY ts DESC, id DESC LIMIT 100000",
                       params).fetchall()
    buf = importer.export_audit(rows, _, dict(AUDIT_CATEGORIES), AUDIT_ACTIONS, all_field_labels(),
                                show=_value_helpers()["show"])
    audit("access", "export_audit", "audit", None, "", {"rows": [None, len(rows)],
                                                        "filters": [None, dict(request.args)] if request.args else [None, ""]})
    con.commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, as_attachment=True, download_name=f"switchdoc-audit-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- topology
import re as _re
EQUIPMENT_KINDS = [  # (kind, regex on equipment text / port type) - first match wins
    ("firewall", r"FORTIGATE|FIREWALL|PARE.?FEU|\bFW\b|\bFG\b|PFSENSE|SOPHOS"),
    ("router", r"ROUTER|ROUTEUR|\bFIBER|\bFIBRE|STARLINK|MODEM|\bISP\b|INTERNET|\bWAN\b|UPLINK|\bLINK\b"),
    ("switch", r"SWITCH|\bSW[A-Z0-9]"),
    ("ap", r"\bAP\b|WIFI|WI-FI|ACCESS ?POINT|BORNE|UNIFI|\bWLAN"),
    ("camera", r"CAM|CAMERA|CAMÉRA|\bNVR\b|\bDVR\b|VIDEO|VIDÉO|CCTV"),
    ("phone", r"PHONE|\bTEL\b|TÉLÉPHONE|TELEPHONE|VOIP|IPBX|PABX|\bVOICE|\bVOIX"),
    ("printer", r"PRINT|IMPRIM|PHOTOCOP|COPIEUR|COPIER|SCAN|\bMFP\b|TRACEUR|PLOTTER"),
    ("server", r"\bESX|\bNAS\d*|SERVER|SERVEUR|\bSRV|-DC-|\bDC\d|HYPER-?V|\bVM\b|PROXMOX|VCENTER|BACKUP|\bSAN\b"),
]
EQUIPMENT_KIND_RE = [(k, _re.compile(rx, _re.I)) for k, rx in EQUIPMENT_KINDS]
INFRA_KINDS = ("firewall", "router")


def equipment_kind(text, ptype=""):
    for kind, rx in EQUIPMENT_KIND_RE:
        if rx.search(text or "") or rx.search(ptype or ""):
            return kind
    return "pc" if any(ch.isalnum() for ch in (text or "")) else "other"


def build_topology(con):
    clause, params = vis("s")
    sws = con.execute(f"""SELECT s.id, s.name, s.ip, s.area, s.location, s.rack, s.model, s.designation, s.layer,
            (SELECT group_concat(g.name, '|') FROM switch_groups sg JOIN sw_groups g ON g.id=sg.group_id
             WHERE sg.switch_id=s.id) AS grp
            FROM switches s LEFT JOIN models m ON m.name=s.model WHERE 1=1{clause}""", params).fetchall()
    by_name = {s["name"]: s for s in sws}
    all_names = {r[0] for r in con.execute("SELECT name FROM switches")}  # incl. switches hidden from this user
    ids = [s["id"] for s in sws]
    akind = {r["name"]: r["kind"] for r in con.execute("SELECT name, kind FROM asset_types")}
    aicon = {r["name"]: r["icon"] or KIND_ICONS.get(r["kind"], "other") for r in con.execute("SELECT name, kind, icon FROM asset_types")}
    infra = [n for n, k in akind.items() if k in INFRA_KINDS]  # firewalls / routers: drawn as nodes with links
    ports = [dict(p) for p in con.execute(f"""SELECT p.*, s.name AS sw FROM ports p JOIN switches s ON s.id=p.switch_id
            WHERE (p.remote_switch<>'' OR (p.equipment<>'' AND p.eq_type IN ({','.join('?' * len(infra)) or 'NULL'})))
            AND p.switch_id IN ({','.join('?' * len(ids)) or 'NULL'})""", (*infra, *ids)).fetchall()]
    ext_kind, ext_icon = {}, {}
    for p in ports:
        if not p["remote_switch"]:
            p["remote_switch"] = p["equipment"]
            ext_kind[p["equipment"]] = akind.get(p["eq_type"], "other")
            ext_icon[p["equipment"]] = aicon.get(p["eq_type"], "other")
    port_index = {}
    for p in ports:
        port_index[(p["sw"], p["name"])] = p
        port_index[(p["sw"], str(p["idx"]))] = p

    externals, edges, issues = {}, {}, []
    for p in ports:
        a, b = p["sw"], p["remote_switch"]
        if b == a:
            continue
        if b not in by_name:
            if b in all_names:
                continue  # remote switch exists but is disabled or outside the user's groups
            externals.setdefault(b, 0)
            externals[b] += 1
        key = tuple(sorted((a, b)))
        e = edges.setdefault(key, {"a": key[0], "b": key[1], "links": [], "_seen": set()})
        rp = p["remote_port"] or "?"
        sig = (a, p["name"], b, rp) if a == key[0] else (b, rp, a, p["name"])
        status = "ok"
        if b in by_name:
            other = port_index.get((b, rp))
            if other is None:
                status = "one-sided"
                issues.append({"kind": "one-sided", "a": a, "ap": p["name"], "b": b, "bp": rp,
                               "msg": _("%(a)s port %(ap)s says it goes to %(b)s port %(bp)s, but %(b)s port %(bp)s "
                                        "has no link back documented", a=a, ap=p["name"], b=b, bp=rp)})
            elif other["remote_switch"] != a or (other["remote_port"] and other["remote_port"] not in (p["name"], str(p["idx"]))):
                status = "mismatch"
                issues.append({"kind": "mismatch", "a": a, "ap": p["name"], "b": b, "bp": rp,
                               "msg": _("%(a)s port %(ap)s → %(b)s port %(bp)s, but %(b)s port %(bp)s says → %(c)s port %(cp)s",
                                        a=a, ap=p["name"], b=b, bp=rp, c=other["remote_switch"] or _("nothing"),
                                        cp=other["remote_port"] or "?")})
        if sig in e["_seen"]:
            continue
        e["_seen"].add(sig)
        e["links"].append({"a_port": sig[1], "b_port": sig[3], "type": p["ptype"], "status": p["status"],
                           "speed": p["speed"],
                           "check": status, "patch": p["patch_port"], "vlan": p["vlan"]})
    seen, uniq = set(), []
    for i in issues:
        k = (i["kind"],) + tuple(sorted([(i["a"], i["ap"]), (i["b"], i["bp"])]))
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    known = sorted(by_name)
    for name in sorted(externals):
        if name in ext_kind:
            continue
        close = difflib.get_close_matches(name, known, n=1, cutoff=0.75)
        if close:
            used = sorted({(p["sw"], p["name"]) for p in ports if p["remote_switch"] == name})
            uniq.append({"kind": "unknown", "a": used[0][0], "ap": used[0][1], "b": name, "bp": "", "suggest": close[0],
                         "msg": _("'%(name)s' is not a documented switch - did you mean %(s)s? Used on: %(used)s",
                                  name=name, s=close[0], used=", ".join(f"{a} {_('port')} {pp}" for a, pp in used))})
    # devices plugged into each switch (ports with connected equipment but no switch-to-switch link)
    equipment = {}
    for q in con.execute(f"""SELECT p.id, p.name, p.equipment, p.eq_type, p.equipment2, p.eq2_type, p.ptype, p.status,
                                    p.vlan, p.user, s.name AS sw
                              FROM ports p JOIN switches s ON s.id=p.switch_id
                              WHERE p.equipment<>'' AND p.remote_switch='' AND p.switch_id IN ({','.join('?' * len(ids)) or 'NULL'})
                              ORDER BY s.name, p.idx""", ids):
        name = q["equipment"].strip()
        if not name or akind.get(q["eq_type"]) in INFRA_KINDS:
            continue
        kind = akind.get(q["eq_type"]) or equipment_kind(name, q["ptype"])
        item = {"port": q["name"], "pid": q["id"], "name": name, "kind": kind, "atype": q["eq_type"] or "",
                "icon": aicon.get(q["eq_type"]) or KIND_ICONS.get(kind, "other"),
                "type": q["ptype"], "status": q["status"], "vlan": q["vlan"], "user": q["user"]}
        if q["equipment2"]:
            item["behind"] = q["equipment2"]
            item["behind_type"] = q["eq2_type"] or ""
            item["behind_kind"] = akind.get(q["eq2_type"], "other")
            item["behind_icon"] = aicon.get(q["eq2_type"]) or "other"
        equipment.setdefault(q["sw"], []).append(item)
    nodes = [{"id": s["name"], "sid": s["id"], "ip": s["ip"], "area": s["area"] or "", "location": s["location"] or "",
              "model": s["model"] or "", "layer": s["layer"] or "", "designation": s["designation"] or "",
              "rack": s["rack"] or "",
              "groups": (s["grp"] or "").split("|") if s["grp"] else [], "external": False} for s in sws]
    nodes += [{"id": n, "external": True, "area": "", "groups": [], "links": c, "kind": ext_kind.get(n, ""),
               "icon": ext_icon.get(n, "")}
              for n, c in externals.items()]
    edge_list = [{k: v for k, v in e.items() if k != "_seen"} for e in edges.values()]
    return {"nodes": nodes, "edges": edge_list, "issues": uniq, "equipment": equipment}


@app.route("/topology")
@require("topology")
def topology():
    con = db()
    data = build_topology(con)
    mcfg = mon_cfg(con)
    if mcfg["mon_enabled"] == "1":  # switch status on the diagram
        clause, params = vis("s")
        sws = con.execute(f"SELECT s.id, s.name, s.ip, s.layer, s.status, s.monitor FROM switches s WHERE 1=1{clause}", params).fetchall()
        info = status_info(con, sws, mcfg)
        data["status"] = {r["name"]: {"state": info[r["id"]]["state"], "label": info[r["id"]]["label"],
                                      "since": info[r["id"]]["since"]} for r in sws}
    ptypes = {t["name"]: t["color"] for t in con.execute("SELECT name, color FROM port_types")}
    groups = [dict(r) for r in visible_groups()]
    saved = {r["node"]: [r["x"], r["y"]] for r in con.execute("SELECT * FROM topo_layout")}
    return render_template("topology.html", data=data, ptypes=ptypes, groups=groups, saved=saved,
                           focus=request.args.get("focus", ""))


@app.route("/api/topology/layout", methods=["POST"])
@require("topology", "edit")
def topology_layout():
    con = db()
    data = request.get_json(force=True) or {}
    if data.get("reset"):
        con.execute("DELETE FROM topo_layout")
    positions = data.get("positions") or {}
    for node, (x, y) in positions.items():
        con.execute("INSERT INTO topo_layout(node,x,y) VALUES (?,?,?) ON CONFLICT(node) DO UPDATE SET x=excluded.x, "
                    "y=excluded.y", (str(node)[:200], float(x), float(y)))
    audit("documentation", "topology_layout", "topology", None, "", {"nodes": [None, len(positions)]})
    con.commit()
    return jsonify({"ok": True})


# -- network access
@app.route("/settings/network-access", methods=["GET", "POST"])
@require("ip_access")
def ip_access():
    con = db()
    me = _client_ip()
    rules = con.execute("SELECT * FROM ip_rules ORDER BY id").fetchall()
    on_row = con.execute("SELECT value FROM app_settings WHERE key='ip_restrict'").fetchone()
    enabled = bool(on_row and on_row[0] == "1")
    if request.method == "POST":
        if not can("ip_access", "edit"):
            abort(403)
        f = request.form
        action = f.get("action")
        if action == "policy":
            old = pw_policy()
            try:
                set_setting(con, "pw_min", max(6, min(64, int(f.get("pw_min") or 10))))
            except ValueError:
                pass
            for k in ("pw_upper", "pw_lower", "pw_digit", "pw_special"):
                set_setting(con, k, "1" if f.get(k) else "0")
            audit("security", "security_settings", "settings", None, "",
                  {k: [old[k], pw_policy()[k]] for k in PW_DEFAULTS if old[k] != pw_policy()[k]})
            con.commit()
            flash(_("Password policy saved. It applies to every new password."), "ok")
            return redirect(url_for("ip_access"))
        if action == "timeout":
            try:
                minutes = int(f.get("minutes") or DEFAULT_SESSION_TIMEOUT)
            except ValueError:
                minutes = DEFAULT_SESSION_TIMEOUT
            minutes = max(1, min(1440, minutes))
            old = session_timeout_min()
            set_setting(con, "session_timeout", minutes)
            audit("security", "security_settings", "settings", None, "", {"session_timeout": [old, minutes]})
            con.commit()
            flash(_("Session timeout set to %(n)s minutes.", n=minutes), "ok")
            return redirect(url_for("ip_access"))
        new_rules = [r["rule"] for r in rules]
        new_on = enabled
        if action == "add":
            try:
                rule = ipaccess.normalise(f.get("rule", ""))
            except ValueError:
                flash(_("“%(r)s” is not a valid IP address, subnet (10.1.0.0/16) or range (10.1.1.10-10.1.1.50).",
                        r=f.get("rule", "")), "error")
                return redirect(url_for("ip_access"))
            new_rules.append(rule)
        elif action == "delete":
            rid = int(f.get("id") or 0)
            row = next((r for r in rules if r["id"] == rid), None)
            if row is None:
                abort(404)
            new_rules.remove(row["rule"])
        elif action in ("enable", "disable"):
            new_on = action == "enable"
        else:
            abort(400)
        # never lock out the person making the change
        if new_on and not ipaccess.allowed(me, ipaccess.compile_rules(new_rules)):
            flash(_("Refused: your own address %(ip)s would no longer be allowed. Add a rule that includes it first.",
                    ip=me), "error")
            return redirect(url_for("ip_access"))
        if new_on and not new_rules:
            flash(_("Add at least one rule before turning the restriction on."), "error")
            return redirect(url_for("ip_access"))
        if action == "add":
            con.execute("INSERT INTO ip_rules(rule, description, created_at, created_by) VALUES (?,?,?,?)",
                        (new_rules[-1], f.get("description", "").strip()[:200], now(), current_user()["username"]))
            audit("security", "ip_access_update", "ip_access", None, new_rules[-1],
                  {"rule": [None, new_rules[-1]], "description": [None, f.get("description", "").strip()]})
        elif action == "delete":
            con.execute("DELETE FROM ip_rules WHERE id=?", (rid,))
            audit("security", "ip_access_update", "ip_access", None, row["rule"], {"rule": [row["rule"], None]})
        else:
            con.execute("INSERT INTO app_settings(key, value) VALUES ('ip_restrict', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if new_on else "0",))
            audit("security", "ip_access_update", "ip_access", None, "", {"active": [enabled, new_on]})
        con.commit()
        ip_rules_reload(con)
        flash(_("Network access rules saved."), "ok")
        return redirect(url_for("ip_access"))
    compiled = {r["id"]: ipaccess.compile_rules([r["rule"]]) for r in rules}
    return render_template("ip_access.html", rules=rules, enabled=enabled, my_ip=me, timeout=session_timeout_min(),
                           policy=pw_policy(),
                           my_ip_ok=ipaccess.allowed(me, ipaccess.compile_rules([r["rule"] for r in rules])),
                           mine={rid for rid, c in compiled.items() if ipaccess.allowed(me, c) and c})


# ---------------------------------------------------------------- maintenance tickets
TICKET_STATUSES = [("open", N_("Open")), ("in_progress", N_("In progress")),
                   ("pending_validation", N_("Pending validation")), ("validated", N_("Validated (closed)")),
                   ("cancelled", N_("Cancelled"))]
TICKET_KINDS = [("maintenance", N_("Maintenance")), ("verification", N_("Documentation verification")),
                ("both", N_("Maintenance + documentation verification"))]
ACTIVE_TICKET = ("open", "in_progress", "pending_validation")


def user_label(row):
    return (f"{row['first_name'] or ''} {row['last_name'] or ''}".strip() or row["username"]) if row else ""


def technicians(con):
    """Active users who can work on tickets (maintenance Edit or more)."""
    out = []
    profiles = {r["id"]: json.loads(r["perms"] or "{}") for r in con.execute("SELECT id, perms FROM auth_profiles")}
    for u in con.execute("SELECT * FROM users WHERE active=1 ORDER BY first_name, last_name, username"):
        lvl = "modify" if u["is_admin"] else profiles.get(u["profile_id"], {}).get("maintenance", "none")
        if LEVELS.get(lvl, 0) >= LEVELS["edit"]:
            out.append(u)
    return out


def ticket_row(con, tid):
    t = con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if t is None:
        abort(404)
    return t


def ticket_assignee_ids(con, tid):
    return {r[0] for r in con.execute("SELECT user_id FROM ticket_assignees WHERE ticket_id=?", (tid,))}


def work_ticket(con, tid):
    """The ticket if the current user may work on it now (in progress, assigned or administrator)."""
    t = con.execute("SELECT * FROM tickets WHERE id=? AND status='in_progress'", (tid,)).fetchone()
    u = current_user()
    if t is None or not can("maintenance", "edit"):
        return None
    if u["id"] in ticket_assignee_ids(con, tid) or u["is_admin"]:
        return t
    return None


def ticket_progress(con, tid):
    r = con.execute("SELECT COUNT(*), SUM(done) FROM ticket_switches WHERE ticket_id=?", (tid,)).fetchone()
    held = con.execute("SELECT COUNT(*) FROM change_requests WHERE ticket_id=? AND status='pending'", (tid,)).fetchone()[0]
    return {"total": r[0] or 0, "done": r[1] or 0, "changes": held}


def can_validate(con, t):
    """A supervisor / administrator who did not do the work (four-eyes principle)."""
    u = current_user()
    if not can("maintenance", "modify") or t["status"] != "pending_validation":
        return False
    return u["id"] not in ticket_assignee_ids(con, t["id"]) and u["id"] != t["submitted_by"]


def my_ticket_counts(con):
    u = current_user()
    if not u or not can("maintenance"):
        return {"todo": 0, "validate": 0, "new": []}
    todo = con.execute("""SELECT COUNT(*) FROM tickets t JOIN ticket_assignees a ON a.ticket_id=t.id
                          WHERE a.user_id=? AND t.status IN ('open', 'in_progress')""", (u["id"],)).fetchone()[0]
    validate = 0
    if can("maintenance", "modify"):
        validate = con.execute("""SELECT COUNT(*) FROM tickets t WHERE t.status='pending_validation' AND
                                  COALESCE(t.submitted_by, 0)<>? AND NOT EXISTS (SELECT 1 FROM ticket_assignees a
                                  WHERE a.ticket_id=t.id AND a.user_id=?)""", (u["id"], u["id"])).fetchone()[0]
    new = con.execute("""SELECT t.id, t.title, t.planned_start, t.planned_end, t.status FROM tickets t
                         JOIN ticket_assignees a ON a.ticket_id=t.id
                         WHERE a.user_id=? AND a.seen=0 AND t.status IN ('open', 'in_progress') ORDER BY t.planned_start""",
                      (u["id"],)).fetchall()
    return {"todo": todo, "validate": validate, "new": [dict(r) for r in new]}


def _dt(v):
    """datetime-local input -> 'YYYY-MM-DD HH:MM'"""
    v = (v or "").strip().replace("T", " ")[:16]
    try:
        datetime.datetime.strptime(v, "%Y-%m-%d %H:%M")
        return v
    except ValueError:
        return ""


@app.route("/maintenance")
@require("maintenance")
def maintenance():
    con = db()
    u = current_user()
    status = request.args.get("status", "active")
    mine = request.args.get("mine") == "1"
    q = request.args.get("q", "").strip()
    d_from, d_to = request.args.get("from", "")[:10], request.args.get("to", "")[:10]
    archived = request.args.get("archived") == "1"
    searching = bool(q or d_from or d_to or archived or status in ("validated", "cancelled", "all", "closed"))
    where, params = "1=1", []
    if status == "active":  # default: only tickets that are not closed
        where += " AND t.status IN ('open', 'in_progress', 'pending_validation')"
    elif status == "closed":
        where += " AND t.status IN ('validated', 'cancelled')"
    elif status in dict(TICKET_STATUSES):
        where += " AND t.status=?"
        params.append(status)
    if not archived:
        where += " AND COALESCE(t.archived, 0)=0"
    if q:
        like = f"%{q}%"
        where += """ AND (CAST(t.id AS TEXT)=? OR t.title LIKE ? OR t.description LIKE ? OR t.scope LIKE ?
                     OR t.tech_comment LIKE ? OR EXISTS (SELECT 1 FROM ticket_switches ts JOIN switches s ON s.id=ts.switch_id
                        WHERE ts.ticket_id=t.id AND s.name LIKE ?)
                     OR EXISTS (SELECT 1 FROM ticket_assignees a JOIN users u ON u.id=a.user_id WHERE a.ticket_id=t.id
                        AND (u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ?)))"""
        params += [q.lstrip("#"), like, like, like, like, like, like, like, like]
    if d_from:
        where += " AND substr(t.planned_start, 1, 10) >= ?"
        params.append(d_from)
    if d_to:
        where += " AND substr(t.planned_start, 1, 10) <= ?"
        params.append(d_to)
    if mine:
        where += " AND EXISTS (SELECT 1 FROM ticket_assignees a WHERE a.ticket_id=t.id AND a.user_id=?)"
        params.append(u["id"])
    if not sees_all():
        clause, vparams = vis("s", all_status=True)
        where += f""" AND (EXISTS (SELECT 1 FROM ticket_assignees a WHERE a.ticket_id=t.id AND a.user_id=?)
                     OR EXISTS (SELECT 1 FROM ticket_switches ts JOIN switches s ON s.id=ts.switch_id
                                WHERE ts.ticket_id=t.id{clause}))"""
        params += [u["id"], *vparams]
    rows = con.execute(f"""SELECT t.* FROM tickets t WHERE {where}
                           ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1
                           WHEN 'pending_validation' THEN 2 ELSE 3 END, t.planned_start, t.id DESC LIMIT 500""",
                       params).fetchall()
    users = {r["id"]: r for r in con.execute("SELECT * FROM users")}
    tickets = []
    for t in rows:
        d = dict(t)
        d["assignees"] = [user_label(users.get(i)) for i in ticket_assignee_ids(con, t["id"])]
        d["mine"] = u["id"] in ticket_assignee_ids(con, t["id"])
        d["progress"] = ticket_progress(con, t["id"])
        d["switches"] = [r[0] for r in con.execute("""SELECT s.name FROM ticket_switches ts JOIN switches s
                                                      ON s.id=ts.switch_id WHERE ts.ticket_id=? ORDER BY s.name""", (t["id"],))]
        d["late"] = t["status"] in ("open", "in_progress") and t["planned_end"] and t["planned_end"] < now()
        tickets.append(d)
    counts = {k: con.execute("SELECT COUNT(*) FROM tickets WHERE status=? AND COALESCE(archived, 0)=0", (k,)).fetchone()[0]
              for k, _l in TICKET_STATUSES}
    return render_template("maintenance.html", tickets=tickets, status=status, mine=mine, counts=counts,
                           statuses=dict(TICKET_STATUSES), kinds=dict(TICKET_KINDS), q=q, d_from=d_from, d_to=d_to,
                           archived=archived, searching=searching)


@app.route("/maintenance/new", methods=["GET", "POST"])
@app.route("/maintenance/<int:tid>/edit", methods=["GET", "POST"])
@require("maintenance", "modify")
def ticket_edit(tid=None):
    con = db()
    t = ticket_row(con, tid) if tid else None
    if t is not None and t["status"] not in ("open", "in_progress"):
        flash(_("Only open or in-progress tickets can be changed."), "error")
        return redirect(url_for("ticket_view", tid=tid))
    clause, params = vis("s")
    sws = con.execute(f"""SELECT s.id, s.name, s.area, s.location, s.rack FROM switches s WHERE 1=1{clause}
                          ORDER BY s.area, s.location, s.rack, s.name""", params).fetchall()
    racks = {}
    for s_ in sws:
        if s_["rack"]:
            racks.setdefault(f"{s_['rack']}|{s_['location'] or ''}|{s_['area'] or ''}", []).append(s_["id"])
    techs = technicians(con)
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()[:200]
        chosen = {int(x) for x in f.getlist("switch_ids") if x.isdigit()}
        for rk in f.getlist("racks"):
            chosen |= set(racks.get(rk, []))
        chosen &= {s_["id"] for s_ in sws}
        assignees = {int(x) for x in f.getlist("assignees") if x.isdigit()} & {u["id"] for u in techs}
        vals = {"title": title, "kind": f.get("kind") if f.get("kind") in dict(TICKET_KINDS) else "maintenance",
                "description": f.get("description", "").strip()[:4000], "planned_start": _dt(f.get("planned_start")),
                "planned_end": _dt(f.get("planned_end"))}
        err = None
        if not title:
            err = _("The title is required.")
        elif not chosen:
            err = _("Choose at least one switch or rack.")
        elif not assignees:
            err = _("Assign at least one technician.")
        elif not vals["planned_start"] or not vals["planned_end"]:
            err = _("Planned start and end dates are required.")
        elif vals["planned_end"] < vals["planned_start"]:
            err = _("The planned end is before the planned start.")
        if err:
            flash(err, "error")
            return render_template("ticket_form.html", t=dict(vals, id=tid, status=t["status"] if t else "open"), sws=sws,
                                   racks=racks, techs=techs, chosen=chosen, assignees=assignees, kinds=TICKET_KINDS,
                                   user_label=user_label)
        names = {s_["id"]: s_["name"] for s_ in sws}
        if t is None:
            tid = con.execute("""INSERT INTO tickets(title, kind, description, status, planned_start, planned_end,
                                 created_by, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                              (vals["title"], vals["kind"], vals["description"], "open", vals["planned_start"],
                               vals["planned_end"], current_user()["id"], now())).lastrowid
            audit("documentation", "ticket_create", "ticket", tid, f"#{tid} {title}",
                  {k: [None, v] for k, v in vals.items() if v} |
                  {"switches": [None, ", ".join(sorted(names[i] for i in chosen))],
                   "assignees": [None, ", ".join(user_label(u) for u in techs if u["id"] in assignees)]})
            old_sw, old_as = set(), set()
        else:
            con.execute(f"UPDATE tickets SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), tid))
            old_sw = {r[0] for r in con.execute("SELECT switch_id FROM ticket_switches WHERE ticket_id=?", (tid,))}
            old_as = ticket_assignee_ids(con, tid)
            ch = diff(t, vals)
            if old_as != assignees:
                lab = {u["id"]: user_label(u) for u in con.execute("SELECT * FROM users")}
                ch["assignees"] = [", ".join(sorted(lab.get(i, "?") for i in old_as)),
                                   ", ".join(sorted(lab.get(i, "?") for i in assignees))]
            if old_sw != chosen:
                allnames = {r[0]: r[1] for r in con.execute("SELECT id, name FROM switches")}
                ch["switches"] = [", ".join(sorted(allnames.get(i, "?") for i in old_sw)),
                                  ", ".join(sorted(allnames.get(i, "?") for i in chosen))]
            if ch:
                audit("documentation", "ticket_update", "ticket", tid, f"#{tid} {title}", ch)
        for i in chosen - old_sw:
            con.execute("INSERT OR IGNORE INTO ticket_switches(ticket_id, switch_id) VALUES (?,?)", (tid, i))
        for i in old_sw - chosen:  # removed switch: its held changes are dropped
            con.execute("DELETE FROM ticket_switches WHERE ticket_id=? AND switch_id=?", (tid, i))
            con.execute("UPDATE change_requests SET status='cancelled' WHERE ticket_id=? AND switch_id=? AND status='pending'",
                        (tid, i))
        for i in assignees - old_as:
            con.execute("INSERT OR IGNORE INTO ticket_assignees(ticket_id, user_id, seen) VALUES (?,?,0)", (tid, i))
        for i in old_as - assignees:
            con.execute("DELETE FROM ticket_assignees WHERE ticket_id=? AND user_id=?", (tid, i))
        scope = ", ".join(sorted({k.split("|")[0] for k in f.getlist("racks")}))
        if scope or t is None:
            con.execute("UPDATE tickets SET scope=? WHERE id=?", (scope, tid))
        con.commit()
        flash(_("Ticket #%(n)s saved.", n=tid), "ok")
        return redirect(url_for("ticket_view", tid=tid))
    pre = {int(x) for x in request.args.getlist("switch") if x.isdigit()}
    chosen = {r[0] for r in con.execute("SELECT switch_id FROM ticket_switches WHERE ticket_id=?", (tid,))} if t else pre
    assignees = ticket_assignee_ids(con, tid) if t else set()
    default = dict(t) if t else {"id": None, "title": "", "kind": "both", "description": "", "status": "open",
                                 "planned_start": "", "planned_end": ""}
    return render_template("ticket_form.html", t=default, sws=sws, racks=racks, techs=techs, chosen=chosen,
                           assignees=assignees, kinds=TICKET_KINDS, user_label=user_label)


@app.route("/maintenance/<int:tid>")
@require("maintenance")
def ticket_view(tid):
    con = db()
    t = ticket_row(con, tid)
    u = current_user()
    assigned = ticket_assignee_ids(con, tid)
    if not sees_all() and u["id"] not in assigned:
        clause, params = vis("s", all_status=True)
        if not con.execute(f"""SELECT 1 FROM ticket_switches ts JOIN switches s ON s.id=ts.switch_id
                               WHERE ts.ticket_id=?{clause}""", (tid, *params)).fetchone():
            abort(404)
    if u["id"] in assigned:
        con.execute("UPDATE ticket_assignees SET seen=1 WHERE ticket_id=? AND user_id=?", (tid, u["id"]))
        con.commit()
    users = {r["id"]: r for r in con.execute("SELECT * FROM users")}
    sws = con.execute("""SELECT ts.*, s.name, s.ip, s.area, s.location, s.rack, s.model, s.status AS sw_status,
                         (SELECT COUNT(*) FROM change_requests c WHERE c.ticket_id=ts.ticket_id AND c.switch_id=ts.switch_id
                          AND c.status='pending') AS held,
                         (SELECT COUNT(*) FROM ports p WHERE p.switch_id=s.id) AS nports
                         FROM ticket_switches ts JOIN switches s ON s.id=ts.switch_id WHERE ts.ticket_id=?
                         ORDER BY s.area, s.rack, s.name""", (tid,)).fetchall()
    changes = con.execute("""SELECT c.*, s.name AS sw_name, p.name AS port_name, p.idx FROM change_requests c
                             JOIN switches s ON s.id=c.switch_id LEFT JOIN ports p ON p.id=c.port_id
                             WHERE c.ticket_id=? AND c.status IN ('pending', 'approved')
                             ORDER BY s.name, p.idx""", (tid,)).fetchall()
    changes = [dict(c, fields=json.loads(c["fields"]), old=json.loads(c["old"]), by=user_label(users.get(c["requested_by"])))
               for c in changes]
    history = [dict(r, details=json.loads(r["details"] or "{}")) for r in con.execute(
        "SELECT * FROM audit WHERE target_type='ticket' AND target_id=? ORDER BY id DESC LIMIT 100", (tid,))]
    return render_template("ticket.html", t=t, sws=sws, changes=changes, progress=ticket_progress(con, tid),
                           assignees=[users[i] for i in assigned if i in users], user_label=user_label,
                           users=users, is_assignee=u["id"] in assigned, can_work=work_ticket(con, tid) is not None,
                           can_validate=can_validate(con, t), statuses=dict(TICKET_STATUSES), kinds=dict(TICKET_KINDS),
                           history=history, field_labels=PORT_FIELD_LABELS, all_labels=all_field_labels(),
                           actions=AUDIT_ACTIONS)


@app.route("/maintenance/<int:tid>/action", methods=["POST"])
@require("maintenance", "edit")
def ticket_action(tid):
    con = db()
    t = ticket_row(con, tid)
    u = current_user()
    f = request.form
    action = f.get("action")
    comment = f.get("comment", "").strip()[:500]
    assigned = u["id"] in ticket_assignee_ids(con, tid) or u["is_admin"]
    label = f"#{tid} {t['title']}"
    back = f.get("next") or url_for("ticket_view", tid=tid)
    if action == "start":
        if t["status"] != "open" or not assigned:
            abort(403)
        con.execute("UPDATE tickets SET status='in_progress', actual_start=? WHERE id=?", (now(), tid))
        audit("documentation", "ticket_start", "ticket", tid, label, {"status": ["open", "in_progress"]})
        flash(_("Ticket #%(n)s is in progress - open each switch below to do the work.", n=tid), "ok")
    elif action == "switch_done":
        if work_ticket(con, tid) is None:
            abort(403)
        sid = int(f.get("switch_id") or 0)
        done = 0 if f.get("undo") else 1
        con.execute("UPDATE ticket_switches SET done=?, done_by=?, done_at=?, note=? WHERE ticket_id=? AND switch_id=?",
                    (done, u["id"] if done else None, now() if done else None, comment, tid, sid))
        name = con.execute("SELECT name FROM switches WHERE id=?", (sid,)).fetchone()
        audit("documentation", "ticket_switch_done", "ticket", tid, label,
              {"switches": [None, name[0] if name else sid], "result": [None, "done" if done else "to do"]}
              | ({"comment": [None, comment]} if comment else {}))
    elif action == "submit":
        if work_ticket(con, tid) is None:
            abort(403)
        p = ticket_progress(con, tid)
        if p["done"] < p["total"]:
            flash(_("Mark every switch as verified before submitting (%(d)s / %(t)s done).", d=p["done"], t=p["total"]),
                  "error")
            return redirect(back)
        con.execute("""UPDATE tickets SET status='pending_validation', actual_end=?, submitted_by=?, tech_comment=?
                       WHERE id=?""", (now(), u["id"], comment, tid))
        audit("documentation", "ticket_submit", "ticket", tid, label,
              {"status": ["in_progress", "pending_validation"], "rows": [None, p["changes"]]}
              | ({"comment": [None, comment]} if comment else {}))
        flash(_("Ticket #%(n)s submitted - a supervisor will validate it.", n=tid), "ok")
    elif action in ("validate", "reject"):
        if not can_validate(con, t):
            flash(_("You cannot validate this ticket: it must be validated by a supervisor or administrator who did "
                    "not do the work."), "error")
            return redirect(back)
        if action == "validate":
            errors = []
            for r in con.execute("""SELECT c.*, s.name AS sw_name, p.name AS port_name FROM change_requests c
                                    JOIN switches s ON s.id=c.switch_id LEFT JOIN ports p ON p.id=c.port_id
                                    WHERE c.ticket_id=? AND c.status='pending' ORDER BY c.id""", (tid,)).fetchall():
                e = apply_change_request(con, r, comment)
                if e:
                    errors.append(e)
            if errors:
                con.rollback()
                flash(_("Nothing was applied - fix these problems first (send the ticket back to the technician):") +
                      " " + " · ".join(errors), "error")
                return redirect(back)
            con.execute("UPDATE tickets SET status='validated', validated_by=?, validated_at=?, validation_comment=?, "
                        "closed_at=? WHERE id=?", (u["id"], now(), comment, now(), tid))
            audit("documentation", "ticket_validate", "ticket", tid, label,
                  {"status": ["pending_validation", "validated"]} | ({"comment": [None, comment]} if comment else {}))
            flash(_("Ticket #%(n)s validated - the changes are now in the documentation.", n=tid), "ok")
            if g.get("linked"):
                flash(_("The other end of the link was updated too: %(list)s", list=", ".join(g.linked)), "ok")
        else:
            if not comment:
                flash(_("Write a comment to tell the technician what to fix."), "error")
                return redirect(back)
            con.execute("UPDATE tickets SET status='in_progress', validation_comment=? WHERE id=?", (comment, tid))
            con.execute("UPDATE ticket_assignees SET seen=0 WHERE ticket_id=?", (tid,))
            audit("documentation", "ticket_reject", "ticket", tid, label,
                  {"status": ["pending_validation", "in_progress"]} | ({"comment": [None, comment]} if comment else {}))
            flash(_("Ticket #%(n)s sent back to the technician.", n=tid), "ok")
    elif action == "delete":
        if not can("maintenance", "modify"):
            abort(403)
        n = con.execute("DELETE FROM change_requests WHERE ticket_id=? AND status='pending'", (tid,)).rowcount
        con.execute("DELETE FROM tickets WHERE id=?", (tid,))
        audit("documentation", "ticket_delete", "ticket", tid, label,
              {"status": [t["status"], None], "cancelled_requests": [None, n]} | ({"comment": [None, comment]} if comment else {}))
        con.commit()
        flash(_("Ticket #%(n)s deleted - its changes that were not validated are dropped.", n=tid), "ok")
        return redirect(url_for("maintenance"))
    elif action == "cancel":
        if not can("maintenance", "modify") or t["status"] not in ("open", "in_progress", "pending_validation"):
            abort(403)
        con.execute("UPDATE tickets SET status='cancelled', validation_comment=?, closed_at=? WHERE id=?",
                    (comment, now(), tid))
        n = con.execute("UPDATE change_requests SET status='cancelled' WHERE ticket_id=? AND status='pending'", (tid,)).rowcount
        audit("documentation", "ticket_cancel", "ticket", tid, label,
              {"status": [t["status"], "cancelled"], "cancelled_requests": [None, n]} | ({"comment": [None, comment]} if comment else {}))
        flash(_("Ticket #%(n)s cancelled.", n=tid), "ok")
    else:
        abort(400)
    con.commit()
    return redirect(back)


@app.route("/api/notifications")
def api_notifications():
    if not g.get("user"):
        abort(401)
    return jsonify(my_ticket_counts(db()))


@app.route("/api/tickets/seen", methods=["POST"])
def api_tickets_seen():
    if not g.get("user"):
        abort(401)
    db().execute("UPDATE ticket_assignees SET seen=1 WHERE user_id=?", (current_user()["id"],))
    db().commit()
    return jsonify({"ok": True})


# -- maintenance tickets: export / archive / delete before a date
def _closed_before(con, before, closed_only=True):
    cond = "t.status IN ('validated', 'cancelled') AND substr(COALESCE(NULLIF(t.closed_at, ''), t.created_at), 1, 10) < ?" \
        if closed_only else "substr(t.created_at, 1, 10) < ?"
    return con.execute(f"SELECT t.* FROM tickets t WHERE {cond} ORDER BY t.id", (before,)).fetchall()


@app.route("/settings/maintenance", methods=["GET", "POST"])
@require("maintenance", "modify")
def maintenance_settings():
    con = db()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        before = f.get("before", "")[:10]
        try:
            datetime.date.fromisoformat(before)
        except ValueError:
            flash(_("Choose a date."), "error")
            return redirect(url_for("maintenance_settings"))
        rows = _closed_before(con, before)
        ids = [r["id"] for r in rows]
        if action == "archive":
            n = con.execute(f"UPDATE tickets SET archived=1 WHERE COALESCE(archived, 0)=0 AND id IN ({','.join('?' * len(ids)) or 'NULL'})",
                            ids).rowcount
            audit("settings", "tickets_archive", "ticket", None, "", {"before": [None, before], "rows": [None, n]})
            flash(_("%(n)s closed tickets archived (hidden from the list; tick “Archived” in the search to see them).", n=n), "ok")
        elif action == "unarchive":
            n = con.execute("UPDATE tickets SET archived=0 WHERE archived=1").rowcount
            audit("settings", "tickets_archive", "ticket", None, "", {"rows": [None, -n]})
            flash(_("%(n)s tickets restored from the archive.", n=n), "ok")
        elif action == "delete":
            if f.get("confirm") != "DELETE":
                flash(_("Type DELETE to confirm."), "error")
                return redirect(url_for("maintenance_settings"))
            q_ids = ','.join('?' * len(ids)) or 'NULL'
            con.execute(f"DELETE FROM change_requests WHERE ticket_id IN ({q_ids}) AND status<>'pending'", ids)
            n = con.execute(f"DELETE FROM tickets WHERE id IN ({q_ids})", ids).rowcount
            audit("settings", "tickets_delete", "ticket", None, "",
                  {"before": [None, before], "rows": [None, n], "name": [None, ", ".join(f"#{i}" for i in ids[:300])]})
            flash(_("%(n)s closed tickets deleted (the audit log keeps their history).", n=n), "ok")
        else:
            abort(400)
        con.commit()
        return redirect(url_for("maintenance_settings"))
    stats = con.execute("""SELECT COUNT(*) AS n, SUM(status IN ('validated', 'cancelled')) AS closed,
                                  SUM(COALESCE(archived, 0)) AS archived, MIN(created_at) AS first FROM tickets""").fetchone()
    return render_template("maintenance_settings.html", stats=stats,
                           today=datetime.date.today().isoformat(),
                           default_before=(datetime.date.today() - datetime.timedelta(days=365)).isoformat())


@app.route("/settings/maintenance/export.xlsx")
@require("maintenance", "modify")
def maintenance_export():
    import importer
    con = db()
    before = request.args.get("before", "")[:10] or (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    closed_only = request.args.get("all") != "1"
    rows = _closed_before(con, before, closed_only)
    users = {r["id"]: user_label(r) for r in con.execute("SELECT * FROM users")}
    items = []
    for t in rows:
        sws = con.execute("""SELECT ts.*, s.name FROM ticket_switches ts JOIN switches s ON s.id=ts.switch_id
                             WHERE ts.ticket_id=? ORDER BY s.name""", (t["id"],)).fetchall()
        chg = con.execute("""SELECT c.*, s.name AS sw, p.name AS port FROM change_requests c JOIN switches s ON s.id=c.switch_id
                             LEFT JOIN ports p ON p.id=c.port_id WHERE c.ticket_id=? ORDER BY c.id""", (t["id"],)).fetchall()
        items.append((t, [users.get(i, "?") for i in ticket_assignee_ids(con, t["id"])], sws, chg))
    buf = importer.export_tickets(items, _, users, dict(TICKET_STATUSES), dict(TICKET_KINDS), PORT_FIELD_LABELS)
    audit("access", "export_tickets", "ticket", None, "", {"before": [None, before], "rows": [None, len(rows)]})
    con.commit()
    return send_file(buf, as_attachment=True, download_name=f"switchdoc-maintenance-before-{before}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- SNMP (live, read-only, nothing stored)
SNMP_SECTIONS = ("system", "interfaces", "vlans", "neighbors", "macs")


@app.route("/snmp")
@require("snmp")
def snmp_page():
    con = db()
    clause, params = vis("s")
    sws = con.execute(f"""SELECT s.id, s.name, s.ip, s.area, s.layer, s.cred_profile_id,
                          (SELECT COUNT(*) FROM cred_fields cf WHERE cf.profile_id=s.cred_profile_id AND cf.kind='snmp'
                           AND cf.value<>'') AS snmp_community
                          FROM switches s LEFT JOIN cred_profiles c ON c.id=s.cred_profile_id
                          WHERE 1=1{clause} ORDER BY s.name COLLATE NOCASE""", params).fetchall()
    sel = request.args.get("switch", type=int)
    # the SNMP communities of the credential profiles (the values never reach the browser)
    comms = con.execute("""SELECT f.id, f.label, f.profile_id, p.name AS profile FROM cred_fields f
                           JOIN cred_profiles p ON p.id=f.profile_id WHERE f.kind='snmp' AND f.value<>''
                           ORDER BY p.name COLLATE NOCASE, f.sort, f.id""").fetchall()
    first = {}
    for c in comms:
        first.setdefault(c["profile_id"], c["id"])
    return render_template("snmp.html", sws=[{"id": r["id"], "name": r["name"], "ip": r["ip"] or "", "area": r["area"] or "",
                                              "layer": r["layer"] or "", "has_comm": bool(r["snmp_community"]),
                                              "comm": first.get(r["cred_profile_id"], "")}
                                             for r in sws], sel=sel, comms=comms)


def _doc_ports_for(con, sid, ifaces):
    """Match the switch interfaces to the documented ports: same name, otherwise the n-th Ethernet interface
    is documented port n (front-panel order)."""
    ports = con.execute("SELECT * FROM ports WHERE switch_id=? ORDER BY idx", (sid,)).fetchall()
    by_name = {p["name"].lower(): p for p in ports}
    by_idx = {p["idx"]: p for p in ports}
    eth = [i for i in ifaces if i.get("type") in (6, 117, 62, 69) and not str(i["name"]).lower().startswith(("vl", "po", "lo", "nu"))]
    seq = {id(i): n for n, i in enumerate(eth, start=1)}
    out = []
    for i in ifaces:
        p = by_name.get(str(i["name"]).lower()) or by_name.get(str(i["descr"]).lower())
        if p is None and id(i) in seq:
            p = by_idx.get(seq[id(i)])
        d = None
        if p is not None:
            d = {"id": p["id"], "name": p["name"], "status": p["status"], "vlan": p["vlan"], "ptype": p["ptype"],
                 "equipment": p["equipment"], "eq_type": p["eq_type"], "equipment2": p["equipment2"],
                 "remote_switch": p["remote_switch"], "remote_port": p["remote_port"], "speed": p["speed"]}
            warn = []
            if p["status"] == "disabled" and i.get("oper") == "up":
                warn.append(_("documented Disabled but the link is up"))
            if p["status"] == "active" and i.get("admin") == "down":
                warn.append(_("documented Active but shut down on the switch"))
            if p["vlan"] and p["vlan"].isdigit() and i.get("vlan") not in (None, "") and str(i["vlan"]) != p["vlan"] \
                    and not i.get("trunk"):
                warn.append(_("VLAN %(a)s on the switch, %(b)s in the documentation", a=i["vlan"], b=p["vlan"]))
            sp = i.get("speed_mbps") or 0
            if i.get("oper") == "up" and p["speed"] == "fast" and sp >= 1000:
                warn.append(_("running at %(s)s Mbit/s", s=sp))
            d["warn"] = warn
        out.append(d)
    return out


@app.route("/api/snmp/<int:sid>/<section>", methods=["POST"])
@require("snmp")
def snmp_api(sid, section):
    import snmp
    if section not in SNMP_SECTIONS:
        abort(404)
    con = db()
    sw = switch_or_404(sid)
    data = request.get_json(force=True) or {}
    host = (data.get("host") or sw["ip"] or "").strip()
    if not host:
        return jsonify({"error": _("This switch has no IP address - type one in the Address field.")}), 400
    port = 161
    if host.count(":") == 1:  # host:port
        host, _p = host.split(":")
        port = int(_p) if _p.isdigit() else 161
    community = (data.get("community") or "").strip() if data.get("community_id") == "other" else ""
    if str(data.get("community_id") or "").isdigit():  # chosen among the SNMP lines of Credentials
        row = con.execute("SELECT value FROM cred_fields WHERE id=? AND kind='snmp'", (int(data["community_id"]),)).fetchone()
        if row is None:
            return jsonify({"error": _("This SNMP community does not exist any more - reload the page.")}), 400
        community = dec(row["value"])
    if not community and sw["cred_profile_id"]:
        community = snmp_community_of(con, sw["cred_profile_id"])
    community = community or "public"
    version = "1" if data.get("version") == "1" else "2c"
    if section == "system":
        audit("access", "snmp_query", "switch", sid, sw["name"], {"result": [None, f"{host}:{port} v{version}"]})
        con.commit()
    try:
        with snmp.Client(host, community, version=version, port=port, timeout=float(data.get("timeout") or 2),
                         retries=1) as c:
            if section == "system":
                out = snmp.read_system(c)
            elif section == "interfaces":
                ifaces = snmp.read_interfaces(c)
                docs = _doc_ports_for(con, sid, ifaces) if can("ports") else [None] * len(ifaces)
                for i, d in zip(ifaces, docs):
                    i["doc"] = d
                out = ifaces
            elif section == "vlans":
                out = snmp.read_vlans(c)
                known = {r[0]: r[1] for r in con.execute("SELECT number, name FROM vlans")}
                for v in out:
                    v["documented"] = known.get(v["id"]) if isinstance(v["id"], int) else None
            else:
                names = {str(i["index"]): i["name"] for i in snmp.read_interfaces_names(c)}
                out = snmp.read_neighbors(c, names) if section == "neighbors" else snmp.read_macs(c, names)
                if section == "macs":
                    out.sort(key=lambda m: (str(m["port"]), m["vlan"], m["mac"]))
            return jsonify({"data": out, "host": f"{host}:{port}", "requests": c.requests})
    except snmp.SnmpError as e:
        return jsonify({"error": str(e)}), 502
    except OSError as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------- switch status (online / offline)
# A background thread pings every switch of every company where it is turned on (Settings > Monitoring), reads its
# uptime with SNMP when it answers, and keeps the current status + the changes (status_events). Nothing else is
# stored. Off by default; it never creates tickets.
MON_DEFAULTS = {"mon_enabled": "0", "mon_interval": "60", "mon_fails": "3", "mon_slow_ms": "100",
                "mon_slow_loss": "10", "mon_snmp": "1", "mon_tcp_port": "", "mon_email": "",
                "mon_email_slow": "0", "mon_quiet_maint": "1", "mon_last_round": ""}
MON_STATES = {"online": N_("Online"), "slow": N_("Slow"), "offline": N_("Offline"), "unknown": N_("Waiting for the first check"),
              "maint": N_("Maintenance"), "off": N_("Not monitored"), "reboot": N_("Rebooted")}
MON_ORDER = {"offline": 4, "slow": 3, "maint": 2, "online": 1, "unknown": 0, "off": 0}
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_pass", "smtp_from")
_MON_RUNNING = set()
_MON_LOCK = threading.Lock()


def mon_cfg(con):
    out = dict(MON_DEFAULTS)
    try:
        out.update({r[0]: r[1] for r in con.execute("SELECT key, value FROM main.company_settings")})
    except sqlite3.Error:
        pass
    return out


def mon_int(cfg, key, lo, hi):
    try:
        return max(lo, min(hi, int(cfg.get(key) or MON_DEFAULTS[key])))
    except ValueError:
        return int(MON_DEFAULTS[key])


def mon_set(con, vals):
    for k, v in vals.items():
        con.execute("INSERT INTO main.company_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value", (k, str(v)))


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts(s):
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def human_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return _("%(n)s s", n=seconds)
    if seconds < 5400:
        return _("%(n)s min", n=round(seconds / 60))
    if seconds < 172800:
        return _("%(n)s h", n=round(seconds / 3600))
    return _("%(n)s days", n=seconds // 86400)


def _probe(sw, cfg, community):
    import monitor
    import snmp
    fake = os.environ.get("SWITCHDOC_MONITOR_FAKE")  # tests only: {ip: {"ok":…, "rtt":…, "loss":…}}
    host = (sw["ip"] or "").split(":")[0].strip()
    if fake:
        try:
            with open(fake, encoding="utf-8") as fh:
                data = json.load(fh)
            if host in data:
                return dict({"ok": False, "rtt": None, "loss": 100, "method": "test", "uptime": None}, **data[host])
        except (OSError, ValueError):
            pass
    tcp = cfg.get("mon_tcp_port") or None
    res = monitor.check(host, count=2, timeout=1.0, tcp_port=int(tcp) if tcp and str(tcp).isdigit() else None)
    res["uptime"] = None
    if res["ok"] and cfg.get("mon_snmp") == "1" and community:
        try:
            port = int(os.environ.get("SWITCHDOC_SNMP_PORT", "161"))
            with snmp.Client(host, community, port=port, timeout=1.0, retries=0, deadline=3) as c:
                v = c.get([snmp.SYSTEM["uptime"]]).get(snmp.SYSTEM["uptime"])
                res["uptime"] = int(v) // 100 if v is not None else None
        except (snmp.SnmpError, OSError, ValueError, TypeError):
            pass
    return res


def maintenance_switches(con):
    """{switch id: ticket id} of the tickets in progress."""
    return {r[0]: r[1] for r in con.execute("""SELECT ts.switch_id, t.id FROM ticket_switches ts JOIN tickets t
                                              ON t.id=ts.ticket_id WHERE t.status='in_progress'""")}


def monitor_round(cid, force=False):
    """One round of checks for a company. Returns the list of changes."""
    import concurrent.futures
    with _MON_LOCK:
        if cid in _MON_RUNNING:
            return []
        _MON_RUNNING.add(cid)
    try:
        con = connect(cid)
        try:
            cfg = mon_cfg(con)
            if cfg["mon_enabled"] != "1" and not force:
                return []
            interval = mon_int(cfg, "mon_interval", 30, 3600)
            last = _ts(cfg.get("mon_last_round"))
            if not force and last and (datetime.datetime.now() - last).total_seconds() < interval - 2:
                return []
            mon_set(con, {"mon_last_round": now_iso()})
            con.commit()
            rows = con.execute("""SELECT id, name, ip, layer, cred_profile_id FROM switches WHERE status='active'
                                  AND ip<>'' AND COALESCE(layer, '')<>'L1' AND COALESCE(monitor, 1)<>0""").fetchall()
            comms = {}
            for r in rows:
                if r["cred_profile_id"] and r["cred_profile_id"] not in comms:
                    comms[r["cred_profile_id"]] = snmp_community_of(con, r["cred_profile_id"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda r: _probe(r, cfg, comms.get(r["cred_profile_id"])), rows))
            changes = _apply_results(con, cfg, rows, results)
            con.execute("DELETE FROM status_events WHERE ts < ?",
                        ((datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d"),))
            con.commit()
            if changes:
                _notify(con, cid, cfg, changes)
            return changes
        finally:
            con.close()
    finally:
        with _MON_LOCK:
            _MON_RUNNING.discard(cid)


def _apply_results(con, cfg, rows, results):
    stamp = now_iso()
    fails_needed = mon_int(cfg, "mon_fails", 1, 20)
    slow_ms = mon_int(cfg, "mon_slow_ms", 1, 100000)
    slow_loss = mon_int(cfg, "mon_slow_loss", 1, 100)
    old = {r["switch_id"]: r for r in con.execute("SELECT * FROM switch_status")}
    changes = []
    for sw, res in zip(rows, results):
        st = old.get(sw["id"])
        prev = st["state"] if st else "unknown"
        fails = st["fails"] if st else 0
        if res["ok"]:
            fails = 0
            slow = (res["rtt"] is not None and res["rtt"] > slow_ms) or res["loss"] >= slow_loss
            new = "slow" if slow else "online"
        else:
            fails += 1
            new = "offline" if fails >= fails_needed else prev
        since = st["since"] if st and new == prev else stamp
        rebooted = st["rebooted_at"] if st else None
        uptime = res.get("uptime")
        if res["ok"] and uptime is not None and st and st["uptime"] is not None and uptime + 120 < st["uptime"]:
            rebooted = (datetime.datetime.now() - datetime.timedelta(seconds=uptime)).strftime("%Y-%m-%d %H:%M:%S")
            con.execute("INSERT INTO status_events(switch_id, ts, state, prev, detail) VALUES (?,?,?,?,?)",
                        (sw["id"], stamp, "reboot", prev, f"uptime {snmp_uptime_text(uptime)}"))
            changes.append({"sw": sw, "state": "reboot", "prev": prev, "detail": f"uptime {snmp_uptime_text(uptime)}"})
        con.execute("""INSERT INTO switch_status(switch_id, state, since, last_check, rtt, loss, fails, uptime, rebooted_at,
                       detail) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(switch_id) DO UPDATE SET state=excluded.state,
                       since=excluded.since, last_check=excluded.last_check, rtt=excluded.rtt, loss=excluded.loss,
                       fails=excluded.fails, uptime=COALESCE(excluded.uptime, switch_status.uptime),
                       rebooted_at=excluded.rebooted_at, detail=excluded.detail""",
                    (sw["id"], new, since, stamp, res["rtt"], res["loss"], fails, uptime, rebooted, res.get("method", "")))
        if new != prev:
            if new == "offline":
                detail = "no answer"
            elif prev == "offline" and st and st["since"]:
                detail = f"after {int((datetime.datetime.now() - _ts(st['since'])).total_seconds() // 60)} min offline"
            elif new == "slow":
                detail = f"{res['rtt']} ms · {res['loss']}% loss"
            else:
                detail = ""
            con.execute("INSERT INTO status_events(switch_id, ts, state, prev, detail) VALUES (?,?,?,?,?)",
                        (sw["id"], stamp, new, prev, detail))
            if prev != "unknown" or new == "offline":
                changes.append({"sw": sw, "state": new, "prev": prev, "detail": detail})
    return changes


def snmp_uptime_text(seconds):
    d, s = divmod(int(seconds), 86400)
    return f"{d} d {s // 3600:02d} h"


def smtp_settings(con):
    vals = {r[0]: r[1] for r in con.execute(
        f"SELECT key, value FROM core.app_settings WHERE key IN ({', '.join('?' * len(SMTP_KEYS))})", SMTP_KEYS)}
    return {k: vals.get(k, "") for k in SMTP_KEYS}


def send_mail(con, recipients, subject, body):
    import smtplib
    from email.message import EmailMessage
    cfg = smtp_settings(con)
    if not cfg["smtp_host"] or not recipients:
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["smtp_from"] or "switchdoc@localhost", ", ".join(recipients)
    msg.set_content(body)
    port = int(cfg["smtp_port"] or (465 if cfg["smtp_security"] == "ssl" else 587 if cfg["smtp_security"] == "starttls" else 25))
    if cfg["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=15)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], port, timeout=15)
    with server:
        if cfg["smtp_security"] == "starttls":
            server.starttls()
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], dec(cfg["smtp_pass"]))
        server.send_message(msg)
    return True


def _notify(con, cid, cfg, changes):
    to = [x.strip() for x in (cfg.get("mon_email") or "").replace(";", ",").split(",") if x.strip()]
    if not to:
        return
    maint = maintenance_switches(con) if cfg.get("mon_quiet_maint") == "1" else {}
    wanted = {"offline", "online", "reboot"} | ({"slow"} if cfg.get("mon_email_slow") == "1" else set())
    lines = []
    for ch in changes:
        if ch["sw"]["id"] in maint or ch["state"] not in wanted:
            continue
        if ch["state"] == "online" and ch["prev"] not in ("offline", "slow"):
            continue
        label = {"offline": "OFFLINE", "online": "back ONLINE", "slow": "SLOW", "reboot": "REBOOTED"}[ch["state"]]
        lines.append(f"- {ch['sw']['name']} ({ch['sw']['ip']}): {label} {ch['detail']}".rstrip())
    if not lines:
        return
    comp = company_by_id(cid) or {"name": "", "code": ""}
    down = sum(1 for ch in changes if ch["state"] == "offline")
    subject = f"SwitchDoc {comp['code'] or comp['name']}: " + (f"{down} switch(es) offline" if down else "switch status changed")
    body = f"{comp['name']} - {now_iso()}\n\n" + "\n".join(lines) + "\n\n-- SwitchDoc (Settings > Monitoring)"
    try:
        send_mail(con, to, subject, body)
    except Exception as e:  # mail problems never stop the checks
        con.execute("""INSERT INTO main.audit(ts, username, category, action, target_type, target_name, details)
                       VALUES (?,?,?,?,?,?,?)""", (now_s(), "system", "settings", "monitor_mail_failed", "", "",
                                                  json.dumps({"error": [None, str(e)[:300]]})))
        con.commit()


def _monitor_tick():
    for c in companies_all():
        if c["active"]:
            try:
                monitor_round(c["id"])
            except sqlite3.Error as e:
                print("monitor round", c["name"], repr(e))


MONITOR = None


@app.before_request
def _start_monitor():
    global MONITOR
    if MONITOR is None:
        import monitor
        MONITOR = monitor.Loop(_monitor_tick, every=10)
        MONITOR.start()


def status_info(con, switches, cfg=None):
    """{switch id: display dict} for the given switch rows (state, label, since text, rtt, uptime…)."""
    cfg = cfg or mon_cfg(con)
    on = cfg["mon_enabled"] == "1"
    rows = {r["switch_id"]: r for r in con.execute("SELECT * FROM switch_status")}
    maint = maintenance_switches(con)
    now_dt = datetime.datetime.now()
    out = {}
    for s in switches:
        st = rows.get(s["id"])
        info = {"state": "off", "since": "", "rtt": None, "loss": None, "uptime": None, "last": "", "ticket": None,
                "real": None, "rebooted": None}
        excluded = "monitor" in s.keys() and s["monitor"] == 0
        if not on or not s["ip"] or (s["layer"] or "") == "L1" or s["status"] != "active" or excluded:
            info["why"] = "off" if not on else ("noip" if not s["ip"] else ("l1" if (s["layer"] or "") == "L1" else
                                                ("excluded" if excluded else "disabled")))
        elif st is None:
            info["state"] = "unknown"
        else:
            info.update(state=st["state"], real=st["state"], rtt=st["rtt"], loss=st["loss"], last=st["last_check"],
                        uptime=snmp_uptime_text(st["uptime"]) if st["uptime"] is not None else None)
            since = _ts(st["since"])
            info["since"] = human_duration((now_dt - since).total_seconds()) if since else ""
            info["since_at"] = st["since"]
            rb = _ts(st["rebooted_at"])
            if rb and (now_dt - rb).total_seconds() < 86400:
                info["rebooted"] = st["rebooted_at"][11:16]
        if s["id"] in maint and info["state"] != "off":
            info["ticket"] = maint[s["id"]]
            if cfg.get("mon_quiet_maint") == "1":
                info["state"] = "maint"
        info["label"] = _(MON_STATES[info["state"]])
        out[s["id"]] = info
    return out


def status_history(con, sids, hours=24):
    """{switch id: [state of each hour, oldest first]} - the worst state seen in each hour."""
    start = datetime.datetime.now() - datetime.timedelta(hours=hours)
    out = {}
    if not sids:
        return out
    marks = ",".join("?" * len(sids))
    before = {r[0]: r[1] for r in con.execute(f"""SELECT switch_id, state FROM status_events e WHERE switch_id IN ({marks})
              AND state<>'reboot' AND ts=(SELECT MAX(ts) FROM status_events x WHERE x.switch_id=e.switch_id
              AND x.state<>'reboot' AND x.ts<?)""", (*sids, start.strftime("%Y-%m-%d %H:%M:%S")))}
    evs = {}
    for r in con.execute(f"""SELECT switch_id, ts, state FROM status_events WHERE switch_id IN ({marks}) AND ts>=?
                             ORDER BY ts""", (*sids, start.strftime("%Y-%m-%d %H:%M:%S"))):
        evs.setdefault(r[0], []).append((_ts(r[1]), r[2]))
    for sid in sids:
        cur = before.get(sid, "unknown")
        events = evs.get(sid, [])
        buckets, i = [], 0
        for h in range(hours):
            b0, b1 = start + datetime.timedelta(hours=h), start + datetime.timedelta(hours=h + 1)
            worst = cur
            while i < len(events) and events[i][0] < b1:
                if events[i][1] != "reboot":
                    cur = events[i][1]
                    worst = cur if MON_ORDER.get(cur, 0) > MON_ORDER.get(worst, 0) else worst
                else:
                    worst = "reboot" if MON_ORDER.get(worst, 0) < 3 else worst
                i += 1
            buckets.append(worst)
        out[sid] = buckets
    return out


def status_timeline(con, sid, hours=24):
    """[(state, percent)] segments of the last hours, and availability over 30 days."""
    now_dt = datetime.datetime.now()

    def segments(start):
        prev = con.execute("""SELECT state FROM status_events WHERE switch_id=? AND state<>'reboot' AND ts<?
                              ORDER BY ts DESC LIMIT 1""", (sid, start.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
        cur, t0 = (prev[0] if prev else "unknown"), start
        segs = []
        for r in con.execute("""SELECT ts, state FROM status_events WHERE switch_id=? AND state<>'reboot' AND ts>=?
                                ORDER BY ts""", (sid, start.strftime("%Y-%m-%d %H:%M:%S"))):
            t = _ts(r[0])
            segs.append((cur, (t - t0).total_seconds()))
            cur, t0 = r[1], t
        segs.append((cur, (now_dt - t0).total_seconds()))
        return segs

    total = hours * 3600
    day = [(s, round(100 * d / total, 2)) for s, d in segments(now_dt - datetime.timedelta(hours=hours)) if d > 0]
    month = segments(now_dt - datetime.timedelta(days=30))
    known = sum(d for s, d in month if s not in ("unknown",))
    down = sum(d for s, d in month if s == "offline")
    avail = round(100 * (known - down) / known, 2) if known else None
    month_start = now_dt.replace(day=1).strftime("%Y-%m-%d")
    outages = con.execute("SELECT COUNT(*) FROM status_events WHERE switch_id=? AND state='offline' AND ts>=?",
                          (sid, month_start)).fetchone()[0]
    return day, avail, outages


def neighbour_links(con, sid):
    """Documented switch-to-switch links of a switch with the status of the other switch."""
    rows = con.execute("""SELECT p.name AS port, p.remote_port, s2.id AS sid, s2.name AS sw FROM ports p
                          JOIN switches s ON s.id=p.switch_id JOIN switches s2 ON s2.name=p.remote_switch
                          WHERE p.switch_id=? AND p.eq_type='Switch' AND s2.status='active' ORDER BY p.idx""",
                       (sid,)).fetchall()
    return [dict(r) for r in rows]


@app.route("/api/status")
@require("switches")
def api_status():
    con = db()
    clause, params = vis("s")
    sws = con.execute(f"SELECT s.id, s.ip, s.layer, s.status, s.monitor FROM switches s WHERE 1=1{clause}", params).fetchall()
    info = status_info(con, sws)
    return jsonify({"switches": {str(k): {"state": v["state"], "label": v["label"], "since": v["since"],
                                          "rtt": v["rtt"]} for k, v in info.items()},
                    "offline": sum(1 for v in info.values() if v["state"] == "offline")})


@app.route("/settings/monitoring", methods=["GET", "POST"])
@require("switches")
def monitoring_settings():
    con = db()
    cfg = mon_cfg(con)
    me = current_user()
    if request.method == "POST":
        f = request.form
        act = f.get("action", "save")
        if act in ("save", "check", "exclude", "include") and not can("switches", "modify"):
            abort(403)
        if act in ("exclude", "include"):
            clause, params = vis("s")
            sids = [int(x) for x in f.getlist("sid") if x.isdigit()]
            want = 0 if act == "exclude" else 1
            names = []
            for sid in sids:
                sw = con.execute(f"SELECT s.id, s.name, s.monitor FROM switches s WHERE s.id=?{clause}", (sid, *params)).fetchone()
                if sw and (sw["monitor"] if sw["monitor"] is not None else 1) != want:
                    con.execute("UPDATE switches SET monitor=?, updated_at=? WHERE id=?", (want, now(), sid))
                    if not want:
                        con.execute("DELETE FROM switch_status WHERE switch_id=?", (sid,))
                    audit("documentation", "switch_update", "switch", sid, sw["name"], {"monitor": [1 - want, want]})
                    names.append(sw["name"])
            con.commit()
            if names:
                flash((_("No longer checked: %(n)s.", n=", ".join(names)) if not want else
                       _("Checked again: %(n)s.", n=", ".join(names))), "ok")
            return redirect(url_for("monitoring_settings") + "#excluded")
        if act == "save":
            vals = {"mon_enabled": "1" if f.get("mon_enabled") else "0",
                    "mon_interval": str(max(30, min(3600, int(f.get("mon_interval") or 60)))) if (f.get("mon_interval") or "60").isdigit() else "60",
                    "mon_fails": str(max(1, min(20, int(f.get("mon_fails") or 3)))) if (f.get("mon_fails") or "3").isdigit() else "3",
                    "mon_slow_ms": str(max(1, int(f.get("mon_slow_ms") or 100))) if (f.get("mon_slow_ms") or "100").isdigit() else "100",
                    "mon_slow_loss": str(max(1, min(100, int(f.get("mon_slow_loss") or 10)))) if (f.get("mon_slow_loss") or "10").isdigit() else "10",
                    "mon_snmp": "1" if f.get("mon_snmp") else "0",
                    "mon_tcp_port": f.get("mon_tcp_port", "").strip() if f.get("mon_tcp_port", "").strip().isdigit() else "",
                    "mon_email": ", ".join(x.strip() for x in f.get("mon_email", "").replace(";", ",").split(",") if x.strip())[:500],
                    "mon_email_slow": "1" if f.get("mon_email_slow") else "0",
                    "mon_quiet_maint": "1" if f.get("mon_quiet_maint") else "0"}
            ch = {k: [cfg.get(k), v] for k, v in vals.items() if str(cfg.get(k)) != v}
            mon_set(con, vals)
            if ch:
                audit("settings", "monitoring_settings", "company", con.cid, (current_company() or {}).get("name", ""), ch)
            con.commit()
            if vals["mon_enabled"] == "1" and cfg["mon_enabled"] != "1":
                threading.Thread(target=monitor_round, args=(con.cid, True), daemon=True).start()
                flash(_("Status checks turned on - the first results appear within a minute."), "ok")
            else:
                flash(_("Monitoring settings saved."), "ok")
        elif act == "check":
            threading.Thread(target=monitor_round, args=(con.cid, True), daemon=True).start()
            flash(_("Checking every switch now - the results appear within a minute."), "ok")
        elif act in ("smtp", "test_mail"):
            if not me["is_admin"]:
                abort(403)
            if act == "smtp":
                old = smtp_settings(con)
                vals = {"smtp_host": f.get("smtp_host", "").strip()[:200], "smtp_port": f.get("smtp_port", "").strip()[:6],
                        "smtp_security": f.get("smtp_security") if f.get("smtp_security") in ("none", "starttls", "ssl") else "starttls",
                        "smtp_user": f.get("smtp_user", "").strip()[:200], "smtp_from": f.get("smtp_from", "").strip()[:200]}
                if f.get("smtp_pass"):
                    vals["smtp_pass"] = enc(f["smtp_pass"])
                elif f.get("smtp_pass_clear"):
                    vals["smtp_pass"] = ""
                for k, v in vals.items():
                    con.execute("INSERT INTO core.app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                                "value=excluded.value", (k, v))
                ch = {k: [old.get(k), v] for k, v in vals.items() if k != "smtp_pass" and old.get(k) != v}
                if "smtp_pass" in vals:
                    ch["smtp_pass"] = [None, "••••••" if vals["smtp_pass"] else ""]
                audit("security", "security_settings", "settings", None, "SMTP", ch)
                con.commit()
                flash(_("Mail server saved."), "ok")
            else:
                to = [x.strip() for x in (f.get("test_to") or cfg.get("mon_email") or me["email"] or "").replace(";", ",").split(",") if x.strip()]
                try:
                    ok = send_mail(con, to, "SwitchDoc - test message",
                                   "This is a test message from SwitchDoc (Settings > Monitoring).")
                    flash(_("Test message sent to %(to)s.", to=", ".join(to)) if ok else
                          _("Give the mail server and at least one recipient first."), "ok" if ok else "error")
                except Exception as e:
                    flash(_("The message could not be sent: %(e)s", e=str(e)[:200]), "error")
        return redirect(url_for("monitoring_settings"))
    clause, params = vis("s")
    sws = con.execute(f"SELECT s.id, s.ip, s.layer, s.status, s.monitor FROM switches s WHERE 1=1{clause}", params).fetchall()
    info = status_info(con, sws, cfg)
    pick = con.execute(f"""SELECT s.id, s.name, s.ip, s.area, COALESCE(s.monitor, 1) AS monitor FROM switches s
                           WHERE s.status='active' AND s.ip<>'' AND COALESCE(s.layer, '')<>'L1'{clause}
                           ORDER BY s.name COLLATE NOCASE""", params).fetchall()
    counts = {}
    for v in info.values():
        counts[v["state"]] = counts.get(v["state"], 0) + 1
    import monitor
    return render_template("monitoring.html", cfg=cfg, counts=counts, total=len(sws),
                           smtp=smtp_settings(con) if me["is_admin"] else None,
                           method="ping" if monitor._PING else "icmp", running=con.cid in _MON_RUNNING,
                           mon_labels=MON_STATES, excluded=[r for r in pick if not r["monitor"]],
                           checkable=[r for r in pick if r["monitor"]])


# ---------------------------------------------------------------- import / export
@app.route("/import", methods=["GET", "POST"])
@require("import_export")
def import_page():
    report = None
    if request.method == "POST":
        if not can("import_export", "edit"):
            abort(403)
        replace = bool(request.form.get("replace"))
        if replace and not can("import_export", "modify"):
            abort(403)
        import importer
        up = request.files.get("file")
        if not up or not up.filename.lower().endswith((".xlsx", ".xlsm")):
            flash(_("Please choose an .xlsx file."), "error")
        else:
            path = os.path.join(DATA, f"upload_{secrets.token_hex(4)}.xlsx")
            up.save(path)
            try:
                con = db()
                report = importer.import_workbook(path, con, enc, replace=replace,
                                                  add_to_groups=my_group_ids(), tr=_)
                migrate_legacy_creds(con)
                ensure_reference_data(con)
                audit("documentation", "import", "import", None, up.filename,
                      {"file": [None, up.filename], "mode": [None, "replace" if replace else "merge"],
                       "result": [None, {k: v for k, v in report.items() if k != "log"}]})
                con.commit()
                flash(_("Import finished."), "ok")
            except Exception as exc:  # noqa: BLE001 - show any parse problem to the user
                db().rollback()
                flash(_("Import failed: %(err)s", err=exc), "error")
            finally:
                os.remove(path)
    return render_template("import.html", report=report)


@app.route("/import/template.xlsx")
@require("import_export")
def import_template():
    import importer
    buf = importer.export_template(db(), _)
    return send_file(buf, as_attachment=True, download_name=f"switchdoc-import-template-{lang()}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export.xlsx")
@require("import_export")
def export_xlsx():
    import importer
    secrets_ok = request.args.get("secrets") == "1" and can("passwords")
    clause, params = vis("s")
    buf = importer.export_workbook(db(), dec, include_secrets=secrets_ok, where=clause, params=params, tr=_)
    audit("access", "export_workbook", "workbook", None, "", {"secrets": [None, secrets_ok]})
    db().commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, as_attachment=True, download_name=f"switchdoc-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- CLI
def _cli():
    cmd = sys.argv[1]
    if cmd == "import" and len(sys.argv) > 2:
        import importer
        want = sys.argv[sys.argv.index("--company") + 1] if "--company" in sys.argv else ""
        comp = next((c for c in companies_all() if want and (str(c["id"]) == want or c["name"].lower() == want.lower()
                                                            or (c["code"] or "").lower() == want.lower())), None)
        if comp is None and want and not want.isdigit():
            comp = company_by_id(create_company(want))
            print("Company created:", comp["name"])
        elif comp is None and not companies_all():
            comp = company_by_id(create_company(FIRST_COMPANY))
            print("Company created:", comp["name"])
        elif comp is None and want:
            print("No company", want)
            return
        con = connect(comp["id"] if comp else None)
        print("Importing into:", company_by_id(con.cid)["name"])
        rep = importer.import_workbook(sys.argv[2], con, enc, replace="--replace" in sys.argv)
        migrate_legacy_creds(con)
        ensure_reference_data(con)
        con.commit()
        for line in rep["log"]:
            print(line)
        print({k: v for k, v in rep.items() if k != "log"})
    elif cmd == "reset-password" and len(sys.argv) > 2:
        import getpass
        pw = getpass.getpass("New password: ")
        con = connect()
        with app.test_request_context():
            bad = password_problems(pw, sys.argv[2])
        if bad:
            print("Refused - the password does not respect the policy:", "; ".join(bad))
            return
        if con.execute("SELECT 1 FROM users WHERE username=? AND auth_source='directory'", (sys.argv[2],)).fetchone():
            print("Refused - this is a domain account: its password is managed in Active Directory.")
            return
        if con.execute("SELECT 1 FROM users WHERE username=?", (sys.argv[2],)).fetchone():
            con.execute("UPDATE users SET pw_hash=?, active=1, must_change=0 WHERE username=?",
                        (generate_password_hash(pw), sys.argv[2]))
        else:
            con.execute("INSERT INTO users(username,pw_hash,is_admin,all_groups,active) VALUES (?,?,1,1,1)",
                        (sys.argv[2], generate_password_hash(pw)))
        con.commit()
        print("Password set.")
    elif cmd == "ip-access" and len(sys.argv) > 2 and sys.argv[2] in ("off", "on", "show"):
        con = connect()
        if sys.argv[2] != "show":
            con.execute("INSERT INTO app_settings(key, value) VALUES ('ip_restrict', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if sys.argv[2] == "on" else "0",))
            con.commit()
            print("Network access restriction turned", sys.argv[2], "- restart SwitchDoc to apply.")
        on = con.execute("SELECT value FROM app_settings WHERE key='ip_restrict'").fetchone()
        print("restriction:", "on" if on and on[0] == "1" else "off")
        for r in con.execute("SELECT rule, description FROM ip_rules"):
            print(" ", r[0], "-", r[1])
    elif cmd == "i18n-check":
        import i18n_tools
        i18n_tools.report()
    else:
        print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
