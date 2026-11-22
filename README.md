# SwitchDoc

> **Documentation:** [SwitchDoc 12.2 – specifications, rebuild prompt and user manual (PDF)](docs/SwitchDoc-v12.2-Specifications-and-User-Manual.pdf)

A small self-hosted web app to document network switches: inventory, ports, VLANs and access credentials.
Python (Flask) + SQLite — one folder, no external database server, works fully offline on your LAN.

## Features
- **Switches**: name, IP, serial, model, PoE, area, location/floor, rack, designation, notes, groups.
- **Ports**: generated from the port count. Status (Active / Disabled), type (drop-down from Settings › Port types),
  VLAN, patch panel port/label, connected equipment, user, remote switch + port, description. Bulk edit.
- **Front-panel view** coloured by port type; SFP ports drawn separately when the model has SFP ports.
- **Topology** tab: diagram of switch-to-switch links built from the Remote switch / Remote port fields.
  Hierarchy or organic layout, colour by area / group / layer, filter by group or area, click for details,
  save the layout for everyone, download as SVG. Includes a **link consistency check** (one-sided links,
  conflicting ports, and remote-switch names that look like typos).
- **Approval workflow**: users with *Edit* on Ports propose changes; they show in amber on the switch until
  a user with *Modify* on Ports approves or rejects them (Approvals page, with comments and history).
- **Credentials** profiles (SSH, Telnet, console, enable, web, SNMP), encrypted in the database.
- **Settings**
  - *Groups* – a switch can be in several groups; users only see switches in their groups.
  - *VLANs*
  - *Port types* – name, colour, order.
  - *Switch models* – vendor, Layer 1 / 2 / 3, PoE, SFP ports (count), default port count.
  - *Users* – first name, last name, email, title, authorisation profile, switch groups, active flag.
  - *Authorisation profiles* – No access / View / Edit / Modify for every menu.
- Search for a PC / device to find its port; duplicate IP / serial warnings; Excel import & export.

## Version 3 additions
- **Settings › Switches**: every defined switch, including disabled ones and archived hardware. Create switches,
  **disable** (hidden from documentation, topology, search and exports) and **re-enable** them, **replace hardware**
  (new model / serial / IP / port count; the switch keeps its name, groups, credentials, links and history, the old
  hardware is archived with a copy of its port documentation), and **export one switch to Excel**
  (switch info, ports, links, history; passwords optional).
- **Audit log** (Settings › Audit log): sign-ins and failed sign-ins, logouts, password views, exports,
  user / profile / group changes and every documentation change with before → after values. Filter by category,
  action, user, dates and text; export to Excel. New permission line "Settings › Audit log".
- **Languages**: English, Français, Español, Português. Language buttons in the header and on the sign-in page;
  each user can have a default language (Settings › Users). Texts are in `translations/*.json` and can be edited;
  `python app.py i18n-check` lists anything missing. Excel exports use the user's language and the importer
  accepts headers in any of the four languages.

## Version 4 changes
- Switch page: ports are read-only until you click **Edit ports**; changes (and bulk changes) are kept on screen
  and saved together with **Save** (or discarded with **Cancel**). You are warned before leaving with unsaved changes.
- Switch Excel export: first sheet laid out like the switch page (details + port table with the same columns,
  colours and filters); if a filter is active on the port table, only the shown ports are exported.
- Switches page: tick several switches → **Export selected to Excel** (index sheet + one sheet per switch).
- Audit log: export to Excel (current filters) and **Purge** entries before a date (Modify level on Audit log).

## Version 5 changes
- **Settings › Locations, racks & offices**: the lists behind the Location, Rack identifier and Office drop-downs
  (Office replaces "Designation"). Racks and offices can belong to a location; renaming cascades to switches.
  Filled automatically from your existing switches and from imports.
- Switch form: Layer drop-down (Layer 1 / 2 / 3, default Layer 3; Layer 1 = unmanaged, no IP address),
  SFP checkbox with number of SFP ports, Location / Rack identifier / Office drop-downs.
- **Switch-to-switch links**: in Edit ports, Remote switch is a drop-down of your switches (or "Other device" for a
  firewall, AP…) and Remote port a drop-down of that switch's ports showing what each is connected to.
  Saving updates **both ends** automatically (and clears the old ends). Link rows are highlighted and a
  **Connections** panel checks every link against the other switch.

## Version 6 changes
- **Topology: connected equipment.** Every switch with documented devices shows a **+ n** button. Click it to show
  the devices plugged into that switch (from the "Connected equipment" field of ports without a switch-to-switch
  link), each with its own shape and colour: PC, server/NAS, WiFi AP, camera, printer, phone, firewall,
  router/internet link, undocumented switch, other. Click **−** to collapse. Works in the Hierarchy layout
  (space is made under the switch) and the Organic layout (devices form a ring around the switch and move with it).
  **Expand all / Collapse all**, a legend, device details on click (with a link to the port), and **Find** also
  finds devices by name. Expanded switches are remembered in your browser.

## Access model
| Level | Meaning |
|---|---|
| No access | Menu hidden |
| View | Read only |
| Edit | Create / change. **On Ports: changes need approval.** |
| Modify | Edit + delete. **On Ports: changes apply directly, can approve others.** |

Visibility of switches is separate: a user sees a switch only if they share a group with it, unless they are an
administrator or have "Can see all switches". Four starter profiles are created: Administrator, Network supervisor,
Technician, Read only — adjust them under Settings › Authorisation profiles.

## Install on Ubuntu (recommended)
```bash
sudo apt install -y python3-venv
sudo useradd -r -m -d /opt/switchdoc switchdoc
sudo cp -r . /opt/switchdoc && sudo chown -R switchdoc: /opt/switchdoc
cd /opt/switchdoc
sudo -u switchdoc python3 -m venv venv
sudo -u switchdoc venv/bin/pip install -r requirements.txt
sudo cp switchdoc.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now switchdoc
```
Open `http://SERVER-IP:8080` — the first visit asks you to create the admin account.

## Or with Docker
```bash
docker compose up -d --build
```

## Or on Windows
1. Install Python 3.10+ from python.org: choose **Customize installation**, tick **Add python.exe to PATH**
   and, on the next page, **Install for all users**.
2. Unzip SwitchDoc to a permanent folder, e.g. `C:\SwitchDoc`.
3. Right-click `install-windows.bat` → **Run as administrator**. It installs the packages, opens port 8080 in the
   firewall and creates a "SwitchDoc" scheduled task that starts the app with Windows (even when nobody is logged in).
4. Open `http://localhost:8080`. Logs: `data\service.log`. Remove with `uninstall-windows.bat` (keeps your data).

Just to try it without installing: double-click `start-windows.bat` and keep its window open.

## Assets, equipment types and the port table (version 7)

* **Assets** menu: equipment types (PC, Phone, Printer, Barcode printer, Access point, Camera, Server, Firewall,
  Router, Unmanaged switch, Other - rename or add your own, choose the shape used in the topology) and, under each
  type, the assets. Asset names are unique (also against switch names). **Switch** is a built-in type whose assets
  are the documented switches. Several assets can be added at once (one name per line). Each asset shows the
  switch ports it is connected to.
* Port table columns: Port, Status, Type, **VLAN (drop-down from Settings › VLANs, ALL = trunk)**, Patch panel port,
  Patch panel label, **Equipment type**, **Connected equipment** (only the assets of the chosen type, or "+ New …"),
  User, Remote port, Description. The Remote switch column is gone: choose equipment type *Switch*, then the switch,
  and Remote port becomes a drop-down of that switch's ports (both ends kept in sync).
* **Device behind**: a PC plugged into a phone is shown nested - `1114` then `↳ PC PC0107`.
* **Disabling a port empties** its patch panel, equipment, user and link (on screen, and enforced by the server).
* On the first start, the old free-text equipment is converted automatically: `1114/PC0107` → Phone 1114 with
  PC PC0107 behind it; names of documented switches → type Switch; Fortigate / FG → Firewall; AP Wifi → Access
  point; Honeywell → Barcode printer; etc. Check the Assets menu afterwards and move anything mis-classified.
* **Speed** column on every port: Fast Ethernet / Gigabit / Fiber (default Gigabit, bulk-editable). Fiber is shown
  as a red label with white text, and so is the port Type (Access / Trunk) of every switch-to-switch link - on the
  switch page, in the Connections panel, the topology link details and the Excel exports.
* **Import / Export → Download the import template**: Instructions sheet (every column explained, with an example)
  plus Summary, Ports, Assets and VLAN sheets with drop-downs built from your settings; Connected equipment only
  lists the assets of the Equipment type chosen in the previous column. Exports use the same layout, so an export
  can be edited and imported back. Old workbooks (free-text equipment / Remote switch) are still accepted.

## Version 12.2 – exclude a switch from the status check

Safe for a running installation: one column (`monitor`, default 1) is added to `switches`; every existing switch stays
checked until you change it.

* Switch form: **Check the status of this switch (online / offline)** – untick it for a lab or spare switch, or one that
  blocks ping. The switch shows *Not monitored*, is never pinged, never counted in the red "offline" badge and sends no
  e-mail. The change is written to the audit log.
* *Settings › Monitoring › Switches not checked*: the list of excluded switches, **Stop checking** (choose a switch) and
  **Check the selected switches again**. *Settings › Switches* marks them "status not checked".
* Excel: a **Status check** column (Yes / No) at the end of the Summary sheet in the export and the import template;
  on import, an empty cell leaves the switch unchanged.

## Version 12.1 – switch status (online / slow / offline)

Safe for a running installation: the update only **adds** tables (`switch_status`, `status_events`,
`company_settings`) to each company database; nothing existing is changed. Monitoring is **off** after the update.

* *Settings › Monitoring* (per company, users with Modify on Switches): turn on *Check the status of the switches*.
  SwitchDoc pings every active switch with an IP (L1 / disabled switches are not checked) every 60 s (30–3600).
  **Offline** after 3 failed checks in a row (configurable), **Slow** above 100 ms or 10 % loss. Optional TCP port
  when ping is blocked. With *Read the uptime by SNMP*, a reboot is detected (uses the switch's SNMP credential).
* Shown on the **switch list** (status tiles that filter, a Status column and a *Last 24 h* bar),
  the **switch page** (status card: since when, response time, uptime, 30-day availability, outages this month,
  linked switches, event history), the **topology** (status dot, red outline and red dashed links for offline
  switches) and a red **"N offline"** badge in the top bar. Pages refresh the status every minute.
* A switch under a maintenance ticket in progress shows **Maintenance** (no alarm).
* **No ticket is ever created** by the status checks. Optional e-mail alerts (offline / back online, optionally slow)
  use the mail server set by an administrator on the same page (shared by all companies, password encrypted).
* Firewall: the SwitchDoc server must be allowed to ping the switches (ICMP echo) and, for the uptime, reach UDP 161.
  On Windows the built-in `ping.exe` is used (no administrator rights needed).
* Events are kept 400 days. Set `SWITCHDOC_NO_MONITOR=1` to stop the background checks entirely.
* Port table on the switch page: the table now fits the screen (narrower columns); on smaller screens or in edit mode
  it scrolls inside its own frame with the header row kept visible, instead of the whole page scrolling sideways.

**Updating a running server (Windows):** back up the whole `C:\switchdoc\data` folder (with `secret.key` and
`session.key`), `schtasks /End /TN SwitchDoc`, extract the zip over `C:\switchdoc` (the `data` folder is not in the zip),
run `install-windows.bat` as administrator (it restarts the service).

## Version 12

* **Several companies in one SwitchDoc.** Each company has its own documentation (switches, ports, assets,
  credentials, VLANs, groups, port types, models, locations, maintenance tickets, audit log) in its own database file
  `data/company-N.db`. Users and authorisation profiles are shared and stored in `data/core.db` with the security
  settings. **Each user has a role per company**: e.g. supervisor (validates) in one company, technician in another,
  nothing in a third. The 🏢 menu at the top changes company. Administrators see every company.
  - *Settings › Companies & domain sign-in* (administrators): create, rename, deactivate or delete a company
    (deleting keeps the database file aside as `deleted-….db`).
  - On a user's page, *Roles in the other companies* sets his role in each company; switch groups are chosen
    inside each company.
  - On the first start of version 12 your existing documentation becomes the first company,
    named by `SWITCHDOC_FIRST_COMPANY` (default **My company**, rename it in *Settings › Companies*) (a copy of the old file is kept as `switchdoc-before-v12.db`).
  - Import into a company from the command line: `python app.py import FILE.xlsx --company "Company name"`.
* **Domain sign-in (Active Directory), one directory per company**, with local accounts still working.
  On the company page: domain controllers (LDAPS 636 recommended, StartTLS or plain LDAP), base DN, UPN suffix,
  NetBIOS name, a read-only service account (password encrypted), nested groups, and **AD group → role** lines
  (first match wins; optionally "all switches" or given switch groups). Users in no mapped group are refused unless a
  default role is set. *Save and test the connection* and *Save and test this user* show the groups found and the role
  that would be given. Users sign in with `DOMAIN\user`, `user@domain` or `user` (sign-in page: *Sign in with*).
  The SwitchDoc account is created at the first sign-in (name, email, title from AD) and the role is refreshed at every
  sign-in; a role an administrator sets by hand is kept. Domain users change their password in Windows.
  No extra package is needed (built-in LDAP client). Open TCP 636 (or 389) from the SwitchDoc server to the DCs.
* **SNMP**: the community is chosen from the SNMP lines of Credentials (default: the switch's own profile), or typed.

## Version 11

* **Moving an asset**: in Assets, the ⇄ button moves a device from its port to another port (any switch) in one
  action: both ports change together, a phone can take the PC behind it along, and an occupied destination can be
  replaced. Administrators / supervisors: applied at once. Technicians: sent for approval and the two ports change
  together when approved. With a maintenance ticket: held in the ticket until it is validated.
  The port table is smarter too: an asset freed on another port in the same save, in the same ticket, or in your own
  pending change is offered again (not greyed out), and approving / validating applies the whole move.
* A maintenance ticket can be **deleted** by a supervisor / administrator (button at the bottom of the ticket): its
  changes that were not validated are dropped.
* **In service since**: date the switch was first put in service (switch form, switch page, print sheet, exports,
  import, hardware replacement).
* **Switch list**: on the Switches page, *Switch list → Export the list to Excel / Print the list* - name, model,
  serial, IP address, location, rack, number of ports, PoE, in service since - for the selected switches, or for all
  the switches shown by the current filter.

## Version 10

* **Credentials are dynamic**: a credential profile is a list of lines (label, kind, value). Kinds: username,
  password / secret, SNMP community, other information. Add, remove and reorder lines; "Add the lines of a model"
  fills the usual lines for Cisco IOS (console, enable, configuration terminal, SSH, web, SNMP), D-Link / TP-Link,
  HPE / Aruba, MikroTik, FortiSwitch or a generic username / password. Secrets stay encrypted; the old fixed fields
  were converted automatically. Excel exports have a *Credentials* sheet (profile, label, kind, value) that the import
  reads back.
* **Passwords of the users**: policy in Settings › Security (minimum length - default 10 -, uppercase, lowercase,
  digit, special character, different from the username). A live checklist is shown under every password field.
  Everybody can change their password in *My account*. When an administrator sets a password he chooses
  *temporary* (default: the user must choose his own password at the next sign-in) or *permanent*.
  Existing passwords keep working; the policy applies to new passwords.
  `python app.py reset-password USER` also follows the policy.
* **Logos for equipment types**: a library of 41 IT icons (PC, laptop, IP phone, printers, barcode printer and
  scanner, access point, cameras, NVR, server, NAS, firewall, router, switch, UPS, PBX, badge reader, time clock,
  POS, PLC, scale…) ships in `static/icons`. Choose one per equipment type in Assets; it is shown in the Assets menu,
  the port table and the topology, where expanded switches show each device as its logo with its name below.

## Version 9

* **Session timeout** (Settings › Security, default 15 minutes): users are signed out after that time without
  activity; a warning with "Stay signed in" appears one minute before. Typing in a page counts as activity.
* **Print**: on a switch, *Export / Print → Print the layout (verification sheet)* - front panel, every port line
  with an OK box and a Remarks column, and Verified by / Date / Signature at the bottom (A4 landscape). It follows the
  port filter of the page.
* **Maintenance** tab: tickets for one or more switches or whole racks, with a type (maintenance, documentation
  verification, both), planned start / end and one or more technicians.
  - Open → In progress (the technician clicks Start; the actual start is set to now) → Pending validation
    (the technician marks every switch verified, writes a short comment and submits) → Validated.
  - The ticket opens each switch in *maintenance mode*: port changes made there are held in the ticket and applied
    only when the ticket is validated. The ticket list and page show the progress (switches verified, changes waiting).
  - Validation by a supervisor / administrator who is not assigned to the ticket and did not submit it (four eyes);
    he can also send it back with a comment. Supervisors and administrators create tickets, change the technicians
    and cancel tickets.
  - Technicians see a 🔔 counter next to their name, a counter on the Maintenance tab, and a pop-up when a ticket is
    assigned to them (checked every minute).
  - Permission "Maintenance": View = see, Edit = technician, Modify = supervisor.
* Ticket list: by default only tickets that are **not closed** (Open, In progress, Pending validation). Validated
  (= closed) and cancelled tickets are found with the search: status, text (ticket #, title, switch, technician),
  planned dates, "Archived".
* **Settings › Maintenance tickets** (supervisors / administrators): export to Excel, archive (hidden from the list,
  still searchable) or delete the tickets closed before a date. Tickets that are not closed are never archived or
  deleted; deleting needs "DELETE" typed and is recorded in the audit log.
* **Passwords** permission, separate from Credentials: the profile *Technician* sees passwords, the new profile
  *Technician (no passwords)* sees the credential usernames only, *Read only* sees no credentials.
* **A disabled port holds no value**: VLAN, patch panel, equipment, user, link and description are emptied and locked
  until the port is enabled again (port name, type and speed stay). Existing disabled ports were emptied once at
  upgrade (listed in the audit log), and the Excel import applies the same rule.
* SQLite runs in WAL mode (several users reading while one writes).

## Version 8

* **Asset status**: an asset connected to a port is automatically *In use* (greyed out in the Assets menu); a free
  asset is *In stock* or *Defective*. Tiles at the top give the totals (in use / free / defective) and filter the list.
* **One port per asset**: unless its equipment type has "Can be connected to several ports" (on by default for
  Server, Firewall, Router), an asset already connected - or defective - is greyed out in the port drop-down and
  refused by the server (also when a change request is approved). Duplicates that already exist stay until edited.
* **One link per port**: a port of another switch that is already linked, used by equipment, or disabled cannot be
  chosen as Remote port (greyed out in the picker, refused by the server).
* **Topology → Racks**: switches drawn inside their rack (Rack identifier), grouped by area / location; links
  between switches of the same rack loop out on the side; firewalls / routers in an "Other devices" box.
* **SNMP** menu (permission "SNMP (live)"): pick a switch, read it with SNMP v2c / v1 and its read-only community
  (from its credential profile, or typed - never sent back to the browser). Shows system info, interfaces (status,
  speed, VLAN, traffic, errors) compared with the documented ports, VLANs, LLDP / CDP neighbours and the MAC table.
  Nothing is stored; the query is written to the audit log. The SwitchDoc server must be allowed to reach the
  switches on UDP 161 (switch ACL / firewall).
* **Settings › Network access**: allowed IP addresses, subnets (10.10.0.0/16) or ranges (10.10.2.10-50). When on,
  every other address gets "Access refused" (logged in the audit). You cannot remove your own access, the server
  itself (127.0.0.1) is always allowed, and `python app.py ip-access off` turns it off if needed (then restart).

## Import your existing workbook
Import / Export → choose the .xlsx → Import.  Or from the command line:
```bash
venv/bin/python app.py import switches.xlsx
```
The importer reads the **Summary** sheet (switches + credentials, grouped into credential profiles; a switch group
is created per Area),
the **VLAN** sheet and every sheet with `Switch` and `Port Number` columns. Sheets made only of formulas
(like `View`) are skipped. Re-importing updates switches by name.

## Upgrading from version 1
Copy the new files over the old ones and keep your `data/` folder. The database is upgraded automatically on
start; users from version 1 become administrators. From version 2, the Administrator profile automatically gets
the new Audit log permission; give it to other profiles as needed.

## Backups — important
Everything lives in `data/`:
- `switchdoc.db` — the database
- `secret.key` — the key that encrypts the passwords. **Without it the stored passwords cannot be read.**
- `session.key` — login session signing key

Since version 12 there are several files: `core.db` (users, profiles, companies, domain settings) and one
`company-N.db` / `switchdoc.db` per company. Back up the whole `data/` folder (e.g. a nightly copy) and keep it somewhere safe.

## Security notes
- Keep it on the management network / behind the FortiGate; don't publish it to the internet.
- For HTTPS, put it behind a reverse proxy (nginx, or a FortiGate virtual server with SSL offload).
- Forgot the admin password: `venv/bin/python app.py reset-password admin`
- Optional environment variables: `SWITCHDOC_DATA` (data folder), `SWITCHDOC_KEY` (encryption key instead of
  `secret.key`), `PORT` (dev server port).
