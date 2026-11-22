"""Very small translation layer.

Texts are written in English in the code and templates as _("English text").
Translations live in translations/<lang>.json as {"English text": "Translated text"}.
A missing translation simply falls back to English, so the files can be edited safely.
Placeholders use Python %-style names: _("Switch %(name)s saved.", name="SW1").
"""
import json
import os

LANGUAGES = {"en": "English", "fr": "Français", "es": "Español", "pt": "Português"}
DEFAULT_LANG = os.environ.get("SWITCHDOC_LANG", "en")
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
_CATALOG = {}


def load():
    _CATALOG.clear()
    for code in LANGUAGES:
        path = os.path.join(_DIR, f"{code}.json")
        if code != "en" and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                _CATALOG[code] = json.load(f)


def translate(_lang, _text, **kw):
    out = _CATALOG.get(_lang, {}).get(_text) or _text
    if kw:
        try:
            return out % kw
        except (KeyError, ValueError, TypeError):
            return _text % kw
    return out


def catalog(lang):
    return _CATALOG.get(lang, {})


def best_match(accept_languages):
    """Pick a supported language from a werkzeug Accept-Language header object."""
    if accept_languages:
        m = accept_languages.best_match(list(LANGUAGES))
        if m:
            return m
    return DEFAULT_LANG if DEFAULT_LANG in LANGUAGES else "en"


def N_(text):
    """Mark a string for translation without translating it now (translated at display time)."""
    return text


# strings used by the JavaScript files - sent to the browser in the page language
JS_STRINGS = [
    "for %(d)s", "%(n)s offline", "Online", "Slow", "Offline", "Maintenance", "Not monitored", "Waiting for the first check",
    "Saving…", "All changes saved", "Change sent for approval", "Save failed:", "Pick a status, type or VLAN to apply",
    "%(n)s change requests sent for approval", "Updated %(n)s ports", "Port", "Active", "Disabled", "VLAN",
    "change pending approval", "Show passwords", "Show", "Hide", "Read only",
    "Click a switch or a connection to see details. Drag to move a switch, drag the background to pan, scroll to zoom.",
    "Switches", "Connections", "Other device (not a switch)", "All ports disabled", "Documented on one side only / conflict",
    "link", "links", "Layer 1", "Layer 2", "Layer 3", "Layer not set", "Open switch",
    "Not a documented switch - it is only named in the Remote switch field of a port (firewall, AP, router, typo…).",
    "Designation", "IP", "Model", "Area", "Groups", "To", "Type", "Patch", "Check", "off", "Saved layout", "Saved ✓",
    "Save layout", "Save failed", "(not a documented switch)", "(disabled)", "(empty)", "Select at least one request.",
    "Editing - click Save when you are done", "Discard your changes?",
    "Applied to the selected ports - click Save to keep the changes", "Office", "— none —", "— port —", "free",
    "not found", "other device", "Other device (firewall, AP, router…)",
    "Name of the other device (firewall, access point, router…):",
    "%(sw)s port %(p)s is currently linked to %(x)s - saving will re-link it to this switch", "also updated: %(list)s",
    "PC / workstation", "Server / NAS", "WiFi access point", "Camera", "Printer / copier", "Phone", "Firewall",
    "Router / internet link", "Switch (not documented)", "Other device", "Equipment", "Show connected equipment",
    "Hide connected equipment", "Open port", "Switch", "Status", "User", "port",
    "— choose the equipment type first —", "no ports", "＋ New %(type)s…", "not a documented switch",
    "Name of the new %(type)s (asset names are unique):", "%(name)s already exists as %(t)s - asset names are unique.",
    "%(name)s will be added to the assets when you save", "Port disabled: patch panel, equipment, user and link were emptied",
    "Barcode / label printer", "Fiber", "The password does not respect the policy", "%(n)s port changes held in the maintenance ticket until it is validated",
    "You have maintenance work to do", "Open the ticket", "Later", "New", "planned", "Maintenance", "Stay signed in", "You will be signed out in %(n)s s because of inactivity.", "No rack", "Other devices", "in use: %(where)s", "Defective",
    "chosen on another port", "%(name)s is already in use or defective - choose another asset.", "Unmanaged switch / hub", "Equipment type", "Device behind",
    "Not a documented switch - a port has the equipment type Switch with this name (typo, or a switch that is not documented yet).",
]

load()
