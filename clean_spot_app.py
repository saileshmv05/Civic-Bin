"""
Clean Spot - Community Cleanup and Reporting
A civic platform for reporting garbage and illegal dumping in neighbourhood
blind spots. Built with Streamlit, Google Maps, and SQLite.

Run with:
    pip install -r requirements.txt
    streamlit run clean_spot_app.py

REQUIRED: a Google Maps JavaScript API key (needs a GCP project with billing enabled).
    1. https://console.cloud.google.com -> create/select a project
    2. Enable "Maps JavaScript API"
    3. Create an API key (APIs & Services > Credentials)
    4. PowerShell: $env:GOOGLE_MAPS_API_KEY = "AIza..."
    Then run streamlit in the SAME terminal window.

OPTIONAL: "Sign in with Google" (local username/password accounts always still work).
    1. APIs & Services -> OAuth consent screen (External is fine for a demo)
    2. Credentials -> OAuth client ID -> Web application
       Authorized redirect URI: http://localhost:8501
    3. PowerShell:
       $env:GOOGLE_OAUTH_CLIENT_ID = "....apps.googleusercontent.com"
       $env:GOOGLE_OAUTH_CLIENT_SECRET = "..."

OPTIONAL: GROQ_API_KEY enables AI photo verification and AI complaint wording.

Features:
    - Photo verification: a vision model confirms the photo shows garbage, suggests
      a category and severity, and flags possible AI-generated images. Works with a
      live camera photo or an upload.
    - Map: coordinates are captured automatically from a click, the browser's
      location, or a place search. Users can outline a stretch (an alley or lane).
    - Municipal heatmap: open spots and cleaned-up spots, colour-coded.
    - Community tab: reports with photos, upvotes, replies, and before/after proof.
    - Complaint helper: pre-filled text for the local municipal portal, with an
      optional AI rewrite.
    - Duplicate guard: a same-category open report within 40 m points to the
      existing report instead of creating a duplicate.
    - Civic Score and an SLA clock on severe open reports (7 days).
    - News: headlines about waste and sanitation.

NOTE on auth: passwords are stored as SHA-256 hashes in SQLite. This is fine for a
prototype; a real deployment needs salted hashing (bcrypt/argon2) and proper sessions.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
DB_FILE = "clean_spot.db"
UPLOAD_DIR = "uploads"
CHENNAI_COORDS = [13.0827, 80.2707]  # Default map center: Chennai, India
CATEGORIES = ["Illegal Dumping", "Overflowing Bin", "Construction Debris", "Bulk Waste", "Hazardous / Medical Waste"]
SLA_DAYS = 7  # overflowing bins and dumps are time-sensitive, so the public clock is shorter than a road repair

# --------------------------------------------------------------------------
# Municipal complaint routing
# --------------------------------------------------------------------------
# We only have ONE real, verified portal on file: Greater Chennai
# Corporation's PGR system. For anywhere else, we reverse-geocode the
# review's coordinates to find the local district/city name, and if it's
# not a place we recognize, we hand the citizen a pre-filled generic
# complaint plus a search link to find their own municipal office's
# portal - honest about what we do and don't actually know.
MUNICIPALITY_DIRECTORY = {
    "chennai": {
        "name": "Greater Chennai Corporation (GCC)",
        "url": "https://erp.chennaicorporation.gov.in/pgr/citizen/BeforeReg.do",
        # TODO: copy GCC's exact solid-waste complaint group/type labels from the live
        # portal and fill them in here. Until then this is None, so citizens get the
        # generic wording - still linked to the real GCC portal.
        "categories": None,
    },
}
GCC_DETAILS_MAX_CHARS = 400  # matches the real GCC portal's "Details of Complaint" field limit

# Generic fallback office terminology by country - "Municipal Corporation
# / Panchayat Office" means nothing in Miami, just like "City Hall / 311"
# would sound odd for rural India. Keyed by ISO 3166-1 alpha-2 country
# code (what Nominatim's reverse-geocode returns), lowercase.
COUNTRY_OFFICE_LABELS = {
    "in": "Municipal Corporation / Panchayat Office",
    "us": "City Hall / Department of Public Works (311)",
    "gb": "Local Council",
    "ca": "Municipal Office / City Hall",
    "au": "Local Council",
    "ie": "County/City Council",
    "nz": "City/District Council",
    "sg": "Municipal Services Office (OneService)",
    "za": "Municipal Office",
}
DEFAULT_OFFICE_LABEL = "Local Government / Public Works Office"

# --------------------------------------------------------------------------
# Anti-spam / anti-duplicate settings
# --------------------------------------------------------------------------
SUBMISSION_COOLDOWN_SECONDS = 120  # 2 minutes between reviews, per account - adjust to taste
DUPLICATE_RADIUS_METERS = 40  # same-category open report within this radius = "already reported"


@st.cache_data(ttl=3600)
def reverse_geocode(lat, lon):
    """
    Free, no-API-key reverse geocoding via OpenStreetMap's Nominatim -
    turns a review's coordinates into a district/city name AND a country
    code, so we know both which municipal office it falls under and what
    to CALL that office (terminology varies wildly by country). Cached
    for an hour (keyed on rounded coordinates by the caller) since
    Nominatim asks callers not to hammer it with repeat requests.
    Returns (district_name, country_code) - both None on network failure.
    """
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "addressdetails": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "CleanSpot-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, None
    address = data.get("address", {})
    district_name = (
        address.get("city") or address.get("town") or address.get("municipality")
        or address.get("county") or address.get("state_district") or address.get("state")
    )
    country_code = (address.get("country_code") or "").lower() or None
    return district_name, country_code


def get_municipality_info(row):
    """
    Returns (municipality_info_dict, is_known_real_portal). For an
    unrecognized district, builds a Google search link instead of
    guessing a URL we can't verify, and picks office terminology that
    actually matches the country the review is in.
    """
    district_name, country_code = reverse_geocode(round(row["lat"], 3), round(row["lon"], 3))

    if district_name:
        key = district_name.lower()
        for name_key, info in MUNICIPALITY_DIRECTORY.items():
            if name_key in key:
                return info, True

    # Unknown / unmatched district - generic fallback, no invented portal URL.
    label = district_name or "your area"
    office_label = COUNTRY_OFFICE_LABELS.get(country_code, DEFAULT_OFFICE_LABEL)
    search_query = urllib.parse.quote_plus(f"{label} {office_label} public grievance garbage waste complaint")
    return (
        {
            "name": f"{label} {office_label}",
            "url": f"https://www.google.com/search?q={search_query}",
            "categories": None,
        },
        False,
    )
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8501")

st.set_page_config(
    page_title="Civic-Bin | Autonomous Environmental Sanitation & Grievance Intelligence",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Clean Spot / Civic-Bin logo: biophilic vector mark drawn in pure SVG.
CLEAN_SPOT_MARK_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><defs><linearGradient id="cbGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#315C3B"/><stop offset="100%" stop-color="#C7E66A"/></linearGradient><linearGradient id="shieldGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#14251B"/><stop offset="100%" stop-color="#1E3827"/></linearGradient></defs><circle cx="100" cy="100" r="88" fill="none" stroke="#315C3B" stroke-width="2.5" stroke-dasharray="6 5" opacity="0.45"/><circle cx="100" cy="100" r="80" fill="url(#shieldGrad)" stroke="url(#cbGrad)" stroke-width="3.5"/><g fill="none" stroke="#F5F4ED" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"><path d="M72 84 L78 142 Q79 150 88 150 L112 150 Q121 150 122 142 L128 84"/><path d="M62 84 H138"/><path d="M86 84 V76 Q86 71 90 71 H110 Q114 71 114 76 V84"/></g><path d="M100 134 V104" stroke="#C7E66A" stroke-width="4.5" stroke-linecap="round"/><path d="M92 114 L100 102 L108 114" stroke="#C7E66A" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="100" cy="94" r="3.5" fill="#C7E66A"/></svg>'


def render_logo(width, container=st):
    """Shows the Civic-Bin mark, centred, on a transparent background."""
    container.markdown(
        f'<div style="display:flex;justify-content:center;margin:4px 0 12px 0;">'
        f'<div style="width:{width}px;height:{width}px;">{CLEAN_SPOT_MARK_SVG}</div></div>',
        unsafe_allow_html=True,
    )


render_logo(120, st.sidebar)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# THEME - Biophilic Nature-Tech Design System (Verdea, Mossa, Donezo)
# Colors: Forest Deep #14251B, Primary Green #315C3B, Sage #A7B99A, Lime #C7E66A
# Warm off-white background #F5F4ED with crisp surface cards #FFFFFF.
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400&family=Syne:wght@600;700;800&family=Space+Grotesk:wght@500;700&display=swap');

    :root {
        --forest-deep: #14251B;
        --green-primary: #315C3B;
        --green-sage: #A7B99A;
        --lime-muted: #C7E66A;
        --off-white: #F5F4ED;
        --surface-white: #FFFFFF;
        --charcoal: #242923;
        --text-secondary: #777D73;
        --border-subtle: rgba(49, 92, 59, 0.16);
        --border-light: rgba(20, 37, 27, 0.08);
        --card-shadow: 0 4px 20px rgba(20, 37, 27, 0.04), 0 1px 3px rgba(20, 37, 27, 0.02);
        --card-shadow-hover: 0 10px 28px rgba(20, 37, 27, 0.08), 0 2px 6px rgba(20, 37, 27, 0.04);
        --alert-red: #DC2626;
        --warning-amber: #D97706;
    }

    @keyframes fadeUp {
        0%   { opacity: 0; transform: translateY(14px); }
        100% { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulseRing {
        0%   { transform: scale(0.7); opacity: 0.9; }
        80%  { transform: scale(1.8); opacity: 0; }
        100% { transform: scale(1.8); opacity: 0; }
    }
    @keyframes barFill {
        0%   { width: 4%; }
        100% { width: 100%; }
    }

    html, body, [class*="css"], .stApp {
        font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif !important;
        background-color: var(--off-white) !important;
        color: var(--charcoal) !important;
    }

    /* Headings */
    h1, h2, h3, .brand-display {
        font-family: 'Syne', 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 700 !important;
        color: var(--forest-deep) !important;
        letter-spacing: -0.02em !important;
    }

    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
        border-right: 1px solid rgba(49, 92, 59, 0.12) !important;
        box-shadow: 2px 0 16px rgba(20, 37, 27, 0.03) !important;
    }
    section[data-testid="stSidebar"] h1, section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {
        font-size: 1.05rem !important;
        color: var(--forest-deep) !important;
    }

    /* Buttons */
    div.stButton > button, .stForm button, div.stLinkButton > a, div.stDownloadButton > button {
        background-color: var(--forest-deep) !important;
        color: #FFFFFF !important;
        border: 1px solid var(--forest-deep) !important;
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 600 !important;
        font-size: 13.5px !important;
        letter-spacing: 0.01em;
        border-radius: 10px !important;
        padding: 8px 18px !important;
        box-shadow: 0 2px 8px rgba(20, 37, 27, 0.08) !important;
        transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
    }
    div.stButton > button:hover, div.stLinkButton > a:hover, div.stDownloadButton > button:hover {
        background-color: var(--green-primary) !important;
        border-color: var(--green-primary) !important;
        color: var(--lime-muted) !important;
        box-shadow: 0 4px 14px rgba(49, 92, 59, 0.22) !important;
        transform: translateY(-1px) !important;
    }

    /* Primary Accent Button */
    div.stButton > button[kind="primary"], button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, #14251B 0%, #315C3B 100%) !important;
        color: var(--lime-muted) !important;
        border: 1px solid rgba(199, 230, 106, 0.35) !important;
        box-shadow: 0 4px 16px rgba(20, 37, 27, 0.18) !important;
    }

    /* Inputs, Selects, Textareas */
    div[data-baseweb="input"], div[data-baseweb="base-input"], div[data-baseweb="select"] > div,
    div[data-baseweb="textarea"], textarea, .stTextInput input, .stNumberInput input {
        background-color: #FFFFFF !important;
        border: 1px solid #D6DED1 !important;
        color: var(--charcoal) !important;
        border-radius: 9px !important;
        font-size: 13.5px !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.02) !important;
    }
    div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within,
    div[data-baseweb="textarea"]:focus-within {
        border-color: var(--green-primary) !important;
        box-shadow: 0 0 0 3px rgba(49, 92, 59, 0.12) !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        border-bottom: 1px solid rgba(49, 92, 59, 0.14) !important;
        padding-bottom: 2px;
        margin-bottom: 24px;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 600 !important;
        font-size: 14px !important;
        color: var(--text-secondary) !important;
        border-radius: 8px 8px 0 0 !important;
        padding: 10px 18px !important;
        border: none !important;
        background: transparent !important;
        transition: all 0.2s ease !important;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: var(--forest-deep) !important;
        background: rgba(167, 185, 154, 0.15) !important;
    }
    .stTabs [aria-selected="true"] {
        color: var(--forest-deep) !important;
        background: #FFFFFF !important;
        border-bottom: 3px solid var(--green-primary) !important;
        box-shadow: 0 -2px 10px rgba(0,0,0,0.02) !important;
    }

    /* Streamlit Containers & Cards */
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
        background: #FFFFFF !important;
        border: 1px solid rgba(49, 92, 59, 0.14) !important;
        border-radius: 14px !important;
        box-shadow: 0 4px 18px rgba(20, 37, 27, 0.04) !important;
        padding: 18px 20px !important;
    }
    div[data-testid="stExpander"] {
        border: 1px solid rgba(49, 92, 59, 0.14) !important;
        border-radius: 12px !important;
        background: #FFFFFF !important;
        box-shadow: 0 2px 12px rgba(20, 37, 27, 0.03) !important;
    }

    /* Metrics */
    div[data-testid="stMetricValue"] {
        color: var(--forest-deep) !important;
        font-family: 'Syne', 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 700 !important;
    }
    div[data-testid="stMetricLabel"] {
        color: var(--text-secondary) !important;
        font-weight: 600 !important;
        font-size: 0.82rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.04em !important;
    }

    /* Custom Badges & Telemetry Panels */
    .eco-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #E8F0E6;
        color: #23542C;
        border: 1px solid #C5DBC0;
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 11.5px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .eco-pill.dark {
        background: rgba(199, 230, 106, 0.14);
        color: #C7E66A;
        border: 1px solid rgba(199, 230, 106, 0.3);
    }
    .status-open {
        display: inline-block;
        background: #FDF2F2;
        color: #9B1C1C;
        border: 1px solid #F8B4B4;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 700;
    }
    .status-fixed {
        display: inline-block;
        background: #EBF5EC;
        color: #1F6830;
        border: 1px solid #C3E6C7;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 700;
    }
    .hero-banner {
        background: linear-gradient(135deg, #14251B 0%, #1D3625 60%, #14251B 100%);
        border: 1px solid rgba(167, 185, 154, 0.25);
        border-radius: 18px;
        padding: 32px 36px;
        margin: -10px -10px 24px -10px;
        color: #F5F4ED;
        box-shadow: 0 10px 32px rgba(20, 37, 27, 0.15);
        position: relative;
        overflow: hidden;
        animation: fadeUp 0.6s ease-out;
    }
    .hero-banner .hero-eyebrow {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 14px;
        flex-wrap: wrap;
        gap: 8px;
    }
    .hero-banner h1 {
        font-family: 'Syne', sans-serif !important;
        font-size: 2.5rem !important;
        font-weight: 800 !important;
        color: #FFFFFF !important;
        margin: 0 0 10px 0 !important;
        letter-spacing: -0.03em !important;
    }
    .hero-banner p {
        color: #D3DFCE;
        margin: 0;
        max-width: 720px;
        font-size: 1.05rem;
        line-height: 1.55;
    }
    .hero-card {
        background: #FFFFFF;
        border: 1px solid rgba(49, 92, 59, 0.14);
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 12px;
        color: var(--charcoal);
        box-shadow: 0 2px 10px rgba(20, 37, 27, 0.04);
    }
    .hero-card b { color: var(--forest-deep); }

    .eco-card {
        background: #FFFFFF;
        border: 1px solid rgba(49, 92, 59, 0.14);
        border-radius: 14px;
        padding: 18px 20px;
        box-shadow: 0 4px 18px rgba(20, 37, 27, 0.04);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .eco-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(20, 37, 27, 0.07);
    }
    .eco-card-dark {
        background: #14251B;
        border: 1px solid rgba(167, 185, 154, 0.25);
        border-radius: 14px;
        padding: 20px 22px;
        color: #F5F4ED;
        box-shadow: 0 8px 24px rgba(20, 37, 27, 0.15);
    }

    /* Splash Screen */
    .splash-wrap {
        min-height: 78vh;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        text-align: center; padding-top: 4vh;
        animation: fadeUp 0.7s ease-out;
    }
    .splash-core {
        position: relative;
        width: 150px; height: 150px;
        display: flex; align-items: center; justify-content: center;
        margin-bottom: 20px;
    }
    .splash-ring {
        position: absolute; inset: 0;
        border: 2px solid var(--green-primary);
        border-radius: 50%;
        animation: pulseRing 2.4s ease-out infinite;
    }
    .splash-ring.ring2 { border-color: var(--lime-muted); animation-delay: 0.8s; }
    .splash-ring.ring3 { border-color: var(--green-sage); animation-delay: 1.6s; }
    .splash-title {
        font-family: 'Syne', sans-serif;
        font-size: 3.2rem;
        font-weight: 800;
        letter-spacing: -0.03em;
        color: #FFFFFF;
    }
    .splash-title span { color: var(--lime-muted); }
    .splash-sub {
        font-family: 'Plus Jakarta Sans', sans-serif;
        color: #D3DFCE;
        font-size: 14px;
        font-weight: 500;
        margin-top: 6px;
        margin-bottom: 26px;
    }
    .splash-bar {
        width: 320px; max-width: 70vw; height: 6px;
        background: rgba(255, 255, 255, 0.12);
        border-radius: 4px;
        overflow: hidden;
        border: 1px solid rgba(199, 230, 106, 0.3);
        margin-bottom: 12px;
    }
    .splash-bar-fill {
        height: 100%;
        background: linear-gradient(90deg, var(--green-primary), var(--lime-muted));
        animation: barFill 2.6s ease-in-out forwards;
    }
    .splash-status {
        font-family: 'Space Grotesk', monospace;
        color: #A7B99A;
        font-size: 12px;
        letter-spacing: 0.05em;
        margin-bottom: 24px;
    }

    /* Auth Page */
    .auth-card {
        background: #FFFFFF;
        border: 1px solid rgba(49, 92, 59, 0.14);
        border-radius: 18px;
        padding: 28px 32px 10px 32px;
        text-align: center;
        margin-bottom: 8px;
        box-shadow: 0 8px 30px rgba(20, 37, 27, 0.06);
        animation: fadeUp 0.6s ease-out;
    }
    .auth-title {
        font-size: 1.85rem !important;
        margin: 4px 0 8px 0 !important;
        color: var(--forest-deep) !important;
        font-family: 'Syne', sans-serif !important;
    }
    .auth-sub { color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 6px; }
    .auth-divider {
        text-align: center; color: var(--text-secondary); font-family: 'Space Grotesk', monospace;
        font-size: 11px; letter-spacing: 2px; margin: 16px 0 12px 0; text-transform: uppercase;
    }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #EDECE4; }
    ::-webkit-scrollbar-thumb { background: #A7B99A; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #315C3B; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Registers our hand-built HTML/JS components. `path` points at the folder
# containing each index.html - no build step, Streamlit just serves it as-is.
_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmaps_component")
gmaps_component = components.declare_component("gmaps_component", path=_component_dir)

_geo_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_component")
geo_component = components.declare_component("geo_component", path=_geo_component_dir)


# --------------------------------------------------------------------------
# SPLASH SCREEN + ACCESS TERMINAL (auth page)
# --------------------------------------------------------------------------
def render_splash_screen():
    """
    Biophilic animated boot sequence. Auto-advances to the access terminal.
    """
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"], header[data-testid="stHeader"] { display: none !important; }
        .stApp { background-color: #14251B !important; }
        </style>
        <div class="splash-wrap">
            <div class="splash-core">
                <div class="splash-ring ring1"></div>
                <div class="splash-ring ring2"></div>
                <div class="splash-ring ring3"></div>
            </div>
            <div class="eco-pill dark" style="margin-bottom: 12px;">● Autonomous Civic Technology</div>
            <div class="splash-title">CIVIC<span>BIN</span></div>
            <div class="splash-sub">Intelligent Environmental Sanitation & Autonomous Edge Surveillance</div>
            <div class="splash-bar"><div class="splash-bar-fill"></div></div>
            <div class="splash-status">Synchronizing geospatial index & IoT edge sentinel nodes...</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _, mid_col, _ = st.columns([1, 1, 1])
    with mid_col:
        render_logo(140)
        if st.button("Enter Civic-Bin Platform →", key="splash_enter_btn", use_container_width=True):
            st.session_state.splash_done = True
            st.rerun()
    components.html(
        """
        <script>
        setTimeout(function() {
            try {
                const doc = window.parent.document;
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                    if (b.innerText && (b.innerText.includes('Enter Civic-Bin') || b.innerText.includes('Enter Clean Spot'))) {
                        b.click();
                        break;
                    }
                }
            } catch (e) {}
        }, 2600);
        </script>
        """,
        height=0,
    )


def render_auth_page():
    """
    Centered access terminal for local accounts or Google OAuth.
    """
    st.markdown(
        """<style>section[data-testid="stSidebar"], header[data-testid="stHeader"] { display: none !important; }</style>""",
        unsafe_allow_html=True,
    )
    _, mid_col, _ = st.columns([1, 1.25, 1])
    with mid_col:
        render_logo(120)
        st.markdown(
            """
            <div class="auth-card">
                <div class="eco-pill" style="margin-bottom: 10px;">Municipal & Community Access</div>
                <h1 class="auth-title">Sign in to Civic-Bin</h1>
                <p class="auth-sub">Report neighbourhood waste blind spots, track 7-day clean-up SLAs, and earn community Civic Score points.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.session_state.get("_oauth_error"):
            st.error(st.session_state._oauth_error)
            st.session_state._oauth_error = None

        if st.session_state.get("_pending_google_email"):
            pending_email = st.session_state._pending_google_email
            st.success(f"Verified via Google as {pending_email} - choose a username to finish.")
            chosen_username = st.text_input("Choose a username", key="google_username_choice")
            if st.button("Confirm Username", key="confirm_google_username_btn", use_container_width=True):
                if not chosen_username.strip():
                    st.error("Please enter a username.")
                else:
                    ok, err = create_google_user(chosen_username.strip(), pending_email)
                    if ok:
                        st.session_state.current_user = chosen_username.strip()
                        st.session_state._pending_google_email = None
                        st.rerun()
                    else:
                        st.error(err)
        else:
            if GOOGLE_OAUTH_CLIENT_ID:
                st.link_button("Sign in with Google", build_google_auth_url(), use_container_width=True)
                st.markdown('<div class="auth-divider">— or use credentials —</div>', unsafe_allow_html=True)
            else:
                st.caption("Google sign-in not configured (set GOOGLE_OAUTH_CLIENT_ID/SECRET to enable it).")

            login_tab, signup_tab = st.tabs(["Sign In", "Sign Up"])
            with login_tab:
                login_username = st.text_input("Username", key="login_username")
                login_password = st.text_input("Password", type="password", key="login_password")
                if st.button("Sign In", key="login_btn", use_container_width=True):
                    if verify_user(login_username.strip(), login_password):
                        st.session_state.current_user = login_username.strip()
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
            with signup_tab:
                signup_username = st.text_input("Choose a username", key="signup_username")
                signup_password = st.text_input("Choose a password", type="password", key="signup_password")
                if st.button("Sign Up", key="signup_btn", use_container_width=True):
                    if not signup_username.strip() or not signup_password:
                        st.error("Username and password are both required.")
                    else:
                        ok, err = create_user(signup_username.strip(), signup_password)
                        if ok:
                            st.session_state.current_user = signup_username.strip()
                            st.rerun()
                        else:
                            st.error(err)

            st.markdown('<div class="auth-divider">— or explore —</div>', unsafe_allow_html=True)
            if st.button("Continue browsing as Guest", key="guest_btn", use_container_width=True):
                st.session_state.guest_mode = True
                st.rerun()
# DATABASE LAYER
# --------------------------------------------------------------------------
def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            civic_score   INTEGER DEFAULT 0,
            created_at    TEXT    NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(users)")
    user_cols = [col[1] for col in cursor.fetchall()]
    if "email" not in user_cols:
        # Separate from `username` on purpose: a Google account's verified
        # email identifies WHO they are across logins, but the citizen
        # still picks their own display username - the two shouldn't be
        # forced to be the same thing.
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    # SQLite can't add a UNIQUE constraint via ALTER TABLE, so a partial
    # unique index does the same job for the email column (multiple NULLs
    # allowed, for local accounts with no linked Google email).
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS garbage_reports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lat           REAL    NOT NULL,
            lon           REAL    NOT NULL,
            location_name TEXT    NOT NULL,
            rating        INTEGER NOT NULL,
            category      TEXT    NOT NULL,
            description   TEXT,
            timestamp     TEXT    NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(garbage_reports)")
    existing_cols = [col[1] for col in cursor.fetchall()]
    new_columns = {
        "path_coords": "TEXT",
        "status": "TEXT DEFAULT 'Open'",
        "photo_path": "TEXT",
        "fixed_photo_path": "TEXT",
        "username": "TEXT DEFAULT 'Anonymous'",
    }
    for col_name, col_type in new_columns.items():
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE garbage_reports ADD COLUMN {col_name} {col_type}")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS review_upvotes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(review_id, username),
            FOREIGN KEY (review_id) REFERENCES garbage_reports (id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS review_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id   INTEGER NOT NULL,
            reply_text  TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            FOREIGN KEY (review_id) REFERENCES garbage_reports (id)
        )
        """
    )
    cursor.execute("PRAGMA table_info(review_replies)")
    reply_cols = [col[1] for col in cursor.fetchall()]
    if "username" not in reply_cols:
        cursor.execute("ALTER TABLE review_replies ADD COLUMN username TEXT DEFAULT 'Anonymous'")

    # --- Community News Feed tables ---
    # news_items holds articles pulled from Google News' public RSS feed
    # (a legitimate, keyless, ToS-compliant source - unlike scraping
    # Facebook, which we deliberately did NOT build; see the comment on
    # fetch_news_rss for why).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT    NOT NULL,
            link       TEXT    UNIQUE NOT NULL,
            source     TEXT,
            published  TEXT,
            fetched_at TEXT    NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(news_items)")
    news_cols = [col[1] for col in cursor.fetchall()]
    if "search_query" not in news_cols:
        # Ties each article to the search that found it, so the feed can
        # actually filter by what's currently typed in the search box -
        # without this, every fetch just piled into one shared pool and
        # the displayed list never changed no matter what you searched.
        cursor.execute("ALTER TABLE news_items ADD COLUMN search_query TEXT")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_upvotes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id   INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(news_id, username),
            FOREIGN KEY (news_id) REFERENCES news_items (id)
        )
        """
    )

    # --- One-time self-healing migration for pre-fix news rows ---
    # Older rows may have been saved with the RAW RFC-822 RSS date string
    # ("Mon, 25 Aug 2026...") from before this was fixed to parse dates
    # properly. Those don't sort correctly against the newer ISO-format
    # rows ("2026-08-25 10:00:00"), so on every startup we detect and
    # normalize any leftover raw-format rows in place - no manual DB
    # cleanup required.
    cursor.execute("SELECT id, published FROM news_items")
    for news_id, published_value in cursor.fetchall():
        if not published_value:
            continue
        try:
            # Already in our ISO format - nothing to do.
            datetime.strptime(published_value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                fixed_dt = parsedate_to_datetime(published_value)
                cursor.execute(
                    "UPDATE news_items SET published = ? WHERE id = ?",
                    (fixed_dt.strftime("%Y-%m-%d %H:%M:%S"), news_id),
                )
            except (TypeError, ValueError):
                pass  # genuinely unparseable - leave it, it'll just sort a bit oddly

    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# AUTH
# --------------------------------------------------------------------------
def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, civic_score, created_at) VALUES (?, ?, 0, ?)",
            (username, hash_password(password), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "That username is already taken."
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return row is not None and row[0] == hash_password(password)


def get_civic_score(username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT civic_score FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def add_civic_points(username, points):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET civic_score = civic_score + ? WHERE username = ?", (points, username))
    conn.commit()
    conn.close()


def build_google_auth_url():
    """Builds the URL that sends the citizen to Google's own sign-in/consent screen."""
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_google_code(code):
    """
    Exchanges the one-time authorization code Google sent back for an
    access token, then uses that token to fetch the verified email
    address. Returns (email, error_message) - exactly one is None.
    """
    token_url = "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    try:
        token_request = urllib.request.Request(token_url, data=payload, method="POST")
        with urllib.request.urlopen(token_request, timeout=8) as response:
            token_data = json.loads(response.read().decode("utf-8"))
        access_token = token_data["access_token"]

        userinfo_request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(userinfo_request, timeout=8) as response:
            userinfo = json.loads(response.read().decode("utf-8"))
        return userinfo.get("email"), None
    except urllib.error.HTTPError as exc:
        # Google's actual reason (invalid_client, invalid_grant, etc.) is in
        # the response body - a bare str(exc) only gives "HTTP Error 401:
        # Unauthorized" and throws that detail away, so read the body too.
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            body = "(no response body)"
        return None, f"Google sign-in failed: HTTP {exc.code} - {body}"
    except Exception as exc:  # noqa: BLE001 - surface any other OAuth failure to the sidebar
        return None, f"Google sign-in failed: {exc}"


def find_username_by_email(email):
    """Looks up an existing account already linked to this Google email, if any."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def create_google_user(username, email):
    """
    Creates a new account for a first-time Google sign-in, with a
    citizen-chosen display username (never forced to be their email).
    The stored password hash is a random throwaway value - this account
    can only ever sign in via Google, never the local username/password
    form. Returns (success, error_message).
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, civic_score, created_at, email) VALUES (?, ?, 0, ?, ?)",
            (username, hash_password(secrets.token_hex(16)), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), email),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "That username is already taken - try another."
    finally:
        conn.close()


def civic_badge(score):
    """Badge tiers for the Civic Score."""
    if score >= 100:
        return "Civic Champion"
    elif score >= 50:
        return "Community Leader"
    elif score >= 20:
        return "Active Citizen"
    else:
        return "Newcomer"


def get_seconds_since_last_submission(username):
    """
    Returns seconds since this account's most recent review, or None if
    they've never submitted one. Backs the anti-spam cooldown - reading
    MAX(timestamp) instead of a separate counter keeps this in sync with
    the reviews table automatically, no extra bookkeeping table needed.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(timestamp) FROM garbage_reports WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    last_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    return (datetime.now() - last_dt).total_seconds()


def haversine_distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in meters."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000  # Earth's radius in meters
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def find_nearby_duplicate(lat, lon, category):
    """
    Checks for an existing OPEN review of the same category within
    DUPLICATE_RADIUS_METERS - this is what stops five people creating five
    separate pins for the same pile of garbage instead of upvoting the one that's
    already there. Returns the matching row as a dict, or None.
    """
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, lat, lon, location_name FROM garbage_reports WHERE category = ? AND status = 'Open'",
        conn, params=(category,),
    )
    conn.close()
    for _, row in df.iterrows():
        if haversine_distance_m(lat, lon, row["lat"], row["lon"]) <= DUPLICATE_RADIUS_METERS:
            return row.to_dict()
    return None


def insert_review(lat, lon, location_name, rating, category, description, username,
                   path_coords=None, photo_path=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO garbage_reports
            (lat, lon, location_name, rating, category, description,
             timestamp, path_coords, status, photo_path, username)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?)
        """,
        (
            lat, lon, location_name, rating, category, description,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path_coords, photo_path, username,
        ),
    )
    conn.commit()
    conn.close()


def fetch_all_reviews():
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT r.*, COALESCE(u.upvote_count, 0) AS upvote_count
        FROM garbage_reports r
        LEFT JOIN (
            SELECT review_id, COUNT(*) AS upvote_count
            FROM review_upvotes
            GROUP BY review_id
        ) u ON r.id = u.review_id
        ORDER BY r.timestamp DESC
        """,
        conn,
    )
    conn.close()
    return df


def has_upvoted(review_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM review_upvotes WHERE review_id = ? AND username = ?", (review_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def upvote_review(review_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO review_upvotes (review_id, username, timestamp) VALUES (?, ?, ?)",
            (review_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def mark_review_fixed(review_id, new_rating, fixed_photo_path):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE garbage_reports SET status = 'Fixed', rating = ?, fixed_photo_path = ? WHERE id = ?",
        (new_rating, fixed_photo_path, review_id),
    )
    conn.commit()
    conn.close()


def insert_reply(review_id, username, reply_text):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO review_replies (review_id, username, reply_text, timestamp) VALUES (?, ?, ?, ?)",
        (review_id, username, reply_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()


def fetch_replies(review_id):
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT * FROM review_replies WHERE review_id = ? ORDER BY timestamp ASC",
        conn, params=(review_id,),
    )
    conn.close()
    return df


# --------------------------------------------------------------------------
# COMMUNITY NEWS FEED (Google News RSS - NOT Facebook)
# --------------------------------------------------------------------------
# Idea: surface external reports of garbage problems
# for the community to corroborate. Scraping Facebook posts isn't
# something we can build - it violates Meta's terms of service regardless
# of project size, and there's no public API for arbitrary post access.
# Google News' RSS feed is the legitimate equivalent: public, keyless,
# and explicitly meant to be consumed this way.
def fetch_news_rss(query, max_items=10):
    """
    Pulls headlines from Google News' public RSS search feed. Returns a
    list of dicts, or an empty list on any network/parsing failure.
    """
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "CleanSpot-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            xml_bytes = response.read()
    except (urllib.error.URLError, TimeoutError):
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = item.findtext("title") or "Untitled"
        link = item.findtext("link") or ""
        raw_pub_date = item.findtext("pubDate") or ""
        source_el = item.find("source")
        source = source_el.text if source_el is not None else "Google News"

        # RSS dates are RFC-822 strings ("Mon, 25 Aug 2026 10:00:00 GMT"),
        # which do NOT sort correctly as plain text - this is the actual
        # root cause of old articles behaving unpredictably in the feed.
        # Parse to a real datetime and store it as a sortable ISO string;
        # fall back to "now" if a feed ever sends a malformed date.
        try:
            published_dt = parsedate_to_datetime(raw_pub_date)
        except (TypeError, ValueError):
            published_dt = datetime.now()
        published_iso = published_dt.strftime("%Y-%m-%d %H:%M:%S")

        if link:
            items.append({"title": title, "link": link, "source": source, "published": published_iso})
    return items


def store_news_items(items, search_query):
    """
    Inserts new articles tagged with the search that found them, skipping
    ones we've already stored (by link). If the same article link was
    already stored under a DIFFERENT earlier query, this update re-tags
    it to the current query too - so re-running a search still surfaces
    it under that search, rather than silently hiding it forever.
    """
    conn = get_connection()
    cursor = conn.cursor()
    for item in items:
        try:
            cursor.execute(
                "INSERT INTO news_items (title, link, source, published, fetched_at, search_query) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item["title"], item["link"], item["source"], item["published"],
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), search_query),
            )
        except sqlite3.IntegrityError:
            # Already have this article - just make sure it's tagged
            # under this query too, so it still shows up here.
            cursor.execute("UPDATE news_items SET search_query = ? WHERE link = ?", (search_query, item["link"]))
    conn.commit()
    conn.close()


def fetch_news_for_query(search_query, limit=200):
    """
    Fetches articles matching the CURRENT search query only - this is
    what actually makes different searches show different results,
    instead of one giant pool of everything ever fetched. Sorted purely
    by publish date; upvotes are shown as a stat but never affect
    ordering, so a popular old article can't permanently bury newer ones.
    """
    conn = get_connection()
    df = pd.read_sql_query(
        """
        SELECT n.*, COALESCE(u.upvote_count, 0) AS upvote_count
        FROM news_items n
        LEFT JOIN (
            SELECT news_id, COUNT(*) AS upvote_count FROM news_upvotes GROUP BY news_id
        ) u ON n.id = u.news_id
        WHERE n.search_query = ?
        ORDER BY n.published DESC
        LIMIT ?
        """,
        conn, params=(search_query, limit),
    )
    conn.close()
    return df


def has_upvoted_news(news_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM news_upvotes WHERE news_id = ? AND username = ?", (news_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def upvote_news(news_id, username):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO news_upvotes (news_id, username, timestamp) VALUES (?, ?, ?)",
            (news_id, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# SESSION STATE
# --------------------------------------------------------------------------
defaults = {
    "current_user": None,
    "_pending_google_email": None,
    "ai_category": None,
    "ai_rating": None,
    "ai_explanation": None,
    "pending_photo_bytes": None,
    "clicked_lat": None,
    "clicked_lon": None,
    "segment_coords": None,
    "_last_picker_raw": None,
    "_last_geo_raw": None,
    "map_center": CHENNAI_COORDS,
    "splash_done": False,
    "guest_mode": False,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

# --------------------------------------------------------------------------
# GOOGLE OAUTH CALLBACK - Google redirects back to this same URL with
# ?code=... after the citizen signs in. We exchange it for their verified
# email, then either log them straight in (if this Google account has
# signed in before) or ask them to pick a display username (first time
# only - never forces the username to be their email address).
# --------------------------------------------------------------------------
query_params = st.query_params
if "code" in query_params and st.session_state.current_user is None:
    oauth_code = query_params["code"]
    verified_email, oauth_error = exchange_google_code(oauth_code)
    st.query_params.clear()
    if verified_email:
        existing_username = find_username_by_email(verified_email)
        if existing_username:
            st.session_state.current_user = existing_username
        else:
            st.session_state._pending_google_email = verified_email
    else:
        st.session_state._oauth_error = oauth_error
    st.rerun()


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
def save_photo(file_bytes, prefix):
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(file_bytes)
    return path


def safe_str(value, default=""):
    """
    Safely reads an optional text column (description, photo_path, etc.).
    pandas reads a SQL NULL as NaN (a float) rather than None or "" - and
    since NaN is truthy in Python, a plain `value or default` lets it
    through unchanged instead of falling back to default. isinstance
    check avoids that trap everywhere we read one of these columns.
    """
    return value if isinstance(value, str) else default


def safe_image(path, **kwargs):
    """
    Shows a photo if its file still exists on disk, or a friendly message
    if not - instead of a hard crash. This matters specifically on
    Streamlit Community Cloud: its local filesystem is EPHEMERAL, wiped
    on every redeploy/sleep-wake cycle, so a photo_path saved earlier can
    end up pointing at a file that no longer physically exists even
    though the database row referencing it still does.
    """
    if isinstance(path, str) and os.path.isfile(path):
        st.image(path, **kwargs)
    else:
        st.caption("Photo unavailable (may have been cleared on app restart).")


SEVERITY_TO_RATING = {"Mild": 3, "Severe": 2, "Critical": 1}


# Updated for Google Gemini API model availability:
# gemini-2.5-flash and older 2.0 models are discontinued/404 for new users.
# We prioritize the latest active, high-throughput Flash models with automatic retry and fallbacks:
GEMINI_VISION_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
]


def analyze_garbage_photo(image_bytes, mime_type="image/jpeg"):
    """
    Checks an uploaded or live-captured photo with Google Gemini. It verifies the
    photo shows garbage, suggests a category and severity, and flags possible
    AI-generated images. Returns (result_dict, error_message); exactly one is None.
    """
    import base64
    import time

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key and hasattr(st, "secrets"):
        try:
            api_key = st.secrets.get("GEMINI_API_KEY") or st.secrets.get("GOOGLE_API_KEY")
        except Exception:
            pass

    if not api_key:
        return None, "Set the GEMINI_API_KEY environment variable to enable photo verification."

    category_list = ", ".join(f'"{c}"' for c in CATEGORIES)
    prompt = (
        "You are verifying a citizen-submitted photo for a civic garbage and waste reporting platform, "
        "to filter out irrelevant, joke, or unrelated images. Look at this photo and respond with "
        "ONLY a JSON object, no other text, in exactly this shape: "
        '{"is_garbage_issue": true or false, '
        f'"category": one of [{category_list}] or null if is_garbage_issue is false, '
        '"severity": one of ["Mild","Severe","Critical"] or null if is_garbage_issue is false. '
        "Severity guide: Mild = a little litter; Severe = a pile that blocks a path, or that "
        "smells or attracts pests; Critical = hazardous, medical, chemical, or sharp waste, or a "
        "large dump blocking a road. "
        '"looks_ai_generated": true or false - your best-effort guess at whether this image is '
        "AI-generated or synthetic rather than a real photograph, "
        '"explanation": a short one-sentence reason for your verdict}.'
    )

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    body = json.dumps({
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64_image}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode("utf-8")

    errors = []
    for model_name in GEMINI_VISION_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                raw_text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text.strip("`")
                    if raw_text.lower().startswith("json"):
                        raw_text = raw_text[4:]
                return json.loads(raw_text.strip()), None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")
                try:
                    err_json = json.loads(detail)
                    detail = err_json.get("error", {}).get("message", detail[:120])
                except Exception:
                    detail = detail[:120]

                # If temporary overload (503) or rate limit (429), pause and retry once
                if exc.code in (503, 429) and attempt < max_attempts - 1:
                    time.sleep(1.2)
                    continue

                errors.append(f"{model_name}: HTTP {exc.code} ({detail.strip()})")
                break
            except Exception as exc:  # noqa: BLE001 - try the next model, report all failures
                errors.append(f"{model_name}: {exc}")
                break

    # Secondary fallback: Groq Vision if GROQ_API_KEY is configured
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            data_url = f"data:{mime_type};base64,{b64_image}"
            groq_models = ["llama-3.2-11b-vision-preview", "llama-3.2-90b-vision-preview"]
            for g_model in groq_models:
                try:
                    resp = client.chat.completions.create(
                        model=g_model,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt + " Return pure JSON ONLY."},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }],
                        response_format={"type": "json_object"},
                    )
                    content = resp.choices[0].message.content.strip()
                    if content.startswith("```"):
                        content = content.strip("`")
                        if content.lower().startswith("json"):
                            content = content[4:]
                    return json.loads(content.strip()), None
                except Exception as g_err:
                    errors.append(f"Groq {g_model}: {g_err}")
        except Exception:
            pass

    return None, "Photo analysis failed: " + " | ".join(errors)


def polish_complaint_with_ai(details_text, category, rating, location_name):
    """
    Optional: sends the plain complaint text to Groq (openai/gpt-oss-120b,
    an open-weight OpenAI model served fast via Groq) and asks for a more
    formal, persuasive rewrite suitable for an official government
    complaint form - still capped at the real 400-char field limit.
    Returns (polished_text, error_message) - exactly one is None. Fails
    safely (returns an error message, not an exception) if the 'groq'
    package or GROQ_API_KEY isn't set up.
    """
    try:
        from groq import Groq
    except ImportError:
        return None, "Install the 'groq' package to enable AI-polished complaint wording (pip install groq)."

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "Set the GROQ_API_KEY environment variable to enable AI-polished complaint wording."

    prompt = (
        "Rewrite this citizen garbage-complaint description as a formal, persuasive, but factual "
        "complaint suitable for a government public-grievance portal. Keep every concrete detail "
        "(location, GPS coordinates, category). Do not invent facts that aren't in the original. "
        f"Respond with ONLY the rewritten text, under {GCC_DETAILS_MAX_CHARS} characters, no preamble, "
        "no quotation marks.\n\n"
        f"Category: {category}\nSeverity rating: {rating}/5\nLocation: {location_name}\n"
        f"Original text: {details_text}"
    )

    try:
        client = Groq(api_key=api_key)
        # Model name per Groq's own deprecation notice (llama-3.3-70b-versatile
        # was retired) - update this string again if Groq renames its
        # recommended open-weight text model in the future.
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
        )
        polished = response.choices[0].message.content.strip().strip('"')
        return polished[:GCC_DETAILS_MAX_CHARS], None
    except Exception as exc:  # noqa: BLE001 - surface any API failure to the UI
        return None, f"AI rewrite failed: {exc}"


def build_civic_complaint(row, municipality_info, is_known_portal):
    """
    Builds the fields a citizen would paste into their local municipal
    complaint portal: the right complaint group/type wording where we
    actually know it (Chennai/GCC), a generic-but-usable version
    everywhere else, and a details string trimmed to a safe universal
    length limit (matches GCC's real 400-char field; most portals are
    similar or more generous).
    """
    if is_known_portal and municipality_info.get("categories"):
        info = municipality_info["categories"].get(row["category"])
        group, complaint_type = (info["group"], info["type"]) if info else ("Solid Waste / Sanitation", row["category"])
    else:
        group, complaint_type = "Solid Waste / Sanitation", row["category"]

    title = f"Garbage report - {row['location_name']}"[:80]

    description = safe_str(row["description"], "Reported via the Clean Spot citizen platform.")
    prefix = f"[{row['category']}] Location: {row['location_name']} (GPS: {row['lat']:.5f}, {row['lon']:.5f}). "
    details = (prefix + description)[:GCC_DETAILS_MAX_CHARS]

    return {"group": group, "type": complaint_type, "title": title, "details": details}


def geocode_location(query):
    """Free, no-API-key location search via OpenStreetMap's Nominatim."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "CleanSpot-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


@st.cache_data(ttl=3600)
def get_location_name(lat, lon):
    """
    Auto-derives a human-readable location name straight from the review's
    actual coordinates via reverse geocoding, instead of trusting a free-
    text field a citizen typed - which could otherwise be filled with
    nonsense, offensive, or unrelated text with no connection to the real
    spot. Falls back to a plain coordinate string if geocoding fails.
    Cached for an hour (same pattern as the other geocoding calls) since
    Nominatim asks callers not to hammer it with repeat requests.
    """
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 0}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "CleanSpot-Prototype/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return f"Near {lat:.5f}, {lon:.5f}"
    return data.get("display_name") or f"Near {lat:.5f}, {lon:.5f}"


def reviews_to_payload(df):
    """
    Converts the reviews DataFrame into plain JSON-safe dicts to hand to
    the Google Maps component (declare_component args must be JSON
    serializable - a pandas DataFrame is not, so we convert row by row).
    """
    records = []
    for _, row in df.iterrows():
        records.append({
            "lat": row["lat"],
            "lon": row["lon"],
            "location_name": row["location_name"],
            "rating": int(row["rating"]),
            "category": row["category"],
            "status": row["status"],
            "username": row["username"],
            "upvote_count": int(row["upvote_count"]),
            "description": safe_str(row["description"]),
            # pandas reads a SQL NULL as NaN (a float), and NaN is truthy in
            # Python - so a plain `if row["path_coords"]` check let NaN
            # through to json.loads() and crashed. isinstance(..., str)
            # only accepts it when there's a real JSON string to parse.
            "path_coords": json.loads(row["path_coords"]) if isinstance(row["path_coords"], str) else None,
        })
    return records


# --------------------------------------------------------------------------
# SPLASH SCREEN + ACCESS GATE - everything below is unreachable until the
# boot animation has been dismissed, and (unless the citizen explicitly
# chooses to browse as a guest) until they're signed in.
# --------------------------------------------------------------------------
if not st.session_state.splash_done:
    render_splash_screen()
    st.stop()

if st.session_state.current_user is None and not st.session_state.guest_mode:
    render_auth_page()
    st.stop()

# --------------------------------------------------------------------------
# SIDEBAR - PROFILE
# --------------------------------------------------------------------------
if st.session_state.current_user is None:
    st.sidebar.markdown(
        """
        <div class="eco-card" style="padding: 14px 16px; margin-bottom: 10px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <b style="font-size: 14px; color: #14251B;">Guest Citizen</b>
                <span class="eco-pill" style="background:#EDECE4; color:#5A6255; border-color:#D6DED1;">Visitor</span>
            </div>
            <div style="font-size: 12px; color: #777D73;">
                Sign in to submit reports, vote on hotspots, and earn Civic Score points.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Sign In / Sign Up", key="sidebar_auth_btn", use_container_width=True):
        st.session_state.guest_mode = False
        st.rerun()
else:
    score = get_civic_score(st.session_state.current_user)
    st.sidebar.markdown(
        f"""
        <div class="eco-card" style="padding: 14px 16px; margin-bottom: 10px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <b style="font-size: 15px; color: #14251B;">@{st.session_state.current_user}</b>
                <span class="eco-pill">{civic_badge(score)}</span>
            </div>
            <div style="font-size: 13px; color: #777D73;">
                Civic Score: <b style="color: #315C3B; font-size: 15px;">{score} pts</b>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Log out", key="sidebar_logout_btn", use_container_width=True):
        st.session_state.current_user = None
        st.session_state.guest_mode = False
        st.rerun()

st.sidebar.markdown("---")

# --------------------------------------------------------------------------
# SIDEBAR - LOCATION TOOLS (decluttered: one status line always visible,
# everything else tucked into a collapsed expander)
# --------------------------------------------------------------------------
st.sidebar.header("Location")

if st.session_state.segment_coords:
    points = st.session_state.segment_coords
    centroid_lat = sum(p[0] for p in points) / len(points)
    centroid_lon = sum(p[1] for p in points) / len(points)
    st.sidebar.success(f"Segment selected ({len(points)} points)")
    st.sidebar.caption(f"{get_location_name(centroid_lat, centroid_lon)}")
elif st.session_state.clicked_lat is not None:
    st.sidebar.success(
        f"{get_location_name(st.session_state.clicked_lat, st.session_state.clicked_lon)}"
    )
else:
    st.sidebar.caption("Nothing selected - click the map, or use a tool below.")

with st.sidebar.expander("Use my location or search"):
    geo_result = geo_component(key="geo_button", default=None)
    if geo_result and geo_result != st.session_state._last_geo_raw:
        st.session_state._last_geo_raw = geo_result
        st.session_state.clicked_lat = geo_result["latitude"]
        st.session_state.clicked_lon = geo_result["longitude"]
        st.session_state.segment_coords = None
        st.session_state.map_center = [geo_result["latitude"], geo_result["longitude"]]

    search_query = st.text_input("Search for a location", placeholder="e.g. T Nagar, Chennai")
    if st.button("Search"):
        if search_query.strip():
            result = geocode_location(search_query.strip())
            if result:
                st.session_state.clicked_lat, st.session_state.clicked_lon = result
                st.session_state.segment_coords = None
                st.session_state.map_center = list(result)
                st.success(f"Found: {result[0]:.5f}, {result[1]:.5f}")
            else:
                st.error("Couldn't find that location - try being more specific.")
        else:
            st.warning("Type a location to search first.")

    if st.button("Clear selection"):
        st.session_state.clicked_lat = None
        st.session_state.clicked_lon = None
        st.session_state.segment_coords = None
        st.rerun()

# --------------------------------------------------------------------------
# SIDEBAR - SUBMISSION FORM
# --------------------------------------------------------------------------
st.sidebar.header("Report Garbage")

if st.session_state.current_user is None:
    st.sidebar.info("Sign in above to submit a review.")
else:
    # Both a live camera and a file upload are offered - live capture is
    # the stronger anti-AI-image safeguard (no file picker to upload a
    # pre-existing/synthetic image from), but not everyone can safely stop
    # and shoot a photo mid-drive, so upload-from-device stays available
    # too. The AI "looks_ai_generated" check below is the safety net
    # for the upload path - a soft signal, not a guarantee either way.
    st.sidebar.caption("Never touch medical, chemical, or sharp waste. Report it from a safe distance.")
    st.sidebar.caption("Add a photo, then verify it with AI")
    photo_method = st.sidebar.radio(
        "Photo method", ["Take a live photo", "Upload from device", "📡 IoT Edge Node"],
        horizontal=True, label_visibility="collapsed", key="review_photo_method",
    )
    if photo_method.startswith("Take a live"):
        uploaded_photo = st.sidebar.camera_input("Take a photo", key="review_photo_camera")
    elif photo_method.startswith("Upload"):
        uploaded_photo = st.sidebar.file_uploader(
            "Upload a photo of the garbage", type=["jpg", "jpeg", "png"], key="review_photo_uploader"
        )
    else:
        uploaded_photo = None
        st.sidebar.markdown("**📡 CLEANSPOT-CAM-01 (ESP32-CAM)**")
        cam_endpoint = st.sidebar.text_input(
            "Node Endpoint",
            value=st.session_state.get("iot_cam_url", "http://192.168.0.108/capture"),
            key="iot_cam_url_input",
            help="Live URL of the ESP32-CAM node on local Wi-Fi",
        )
        st.session_state["iot_cam_url"] = cam_endpoint
        if st.sidebar.button("📸 Trigger & Fetch from IoT Node", key="btn_trigger_iot", use_container_width=True):
            try:
                import urllib.request
                with st.spinner("Connecting to CLEANSPOT-CAM-01 & triggering flash..."):
                    req = urllib.request.Request(cam_endpoint, headers={"User-Agent": "CleanSpot-Web/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as response:
                        img_bytes = response.read()
                        if img_bytes:
                            st.session_state.iot_photo_bytes = img_bytes
                            st.session_state.pending_photo_bytes = img_bytes
                            st.session_state.clicked_lat = 13.0827
                            st.session_state.clicked_lon = 80.2707
                            st.sidebar.success("Captured live frame from ESP32-CAM! Node GPS: 13.0827, 80.2707")
            except Exception as e:
                st.sidebar.error(f"Could not connect to IoT node: {e}")

        if st.session_state.get("iot_photo_bytes"):
            st.sidebar.image(
                st.session_state.iot_photo_bytes,
                caption="Live Capture: CLEANSPOT-CAM-01 (Chennai Central)",
                use_container_width=True,
            )
            if st.sidebar.button("Verify & Analyze with AI", key="btn_verify_iot", use_container_width=True):
                photo_bytes = st.session_state.iot_photo_bytes
                mime_type = "image/jpeg"
                result, ai_error = analyze_garbage_photo(photo_bytes, mime_type)
                if ai_error:
                    st.sidebar.warning(ai_error)
                elif not result.get("is_garbage_issue"):
                    st.sidebar.warning(
                        f"This doesn't look like a garbage problem: {result.get('explanation', 'no reason given')}. "
                        "You can still submit, but double-check your photo."
                    )
                    st.session_state.ai_category = None
                    st.session_state.ai_rating = None
                    st.session_state.ai_explanation = None
                else:
                    if result.get("looks_ai_generated"):
                        st.sidebar.warning(
                            "This photo has some signs of being AI-generated rather than a real "
                            "photograph - best-effort AI guess, not a certainty, but worth a second look."
                        )
                    st.session_state.ai_category = result.get("category")
                    st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                    st.session_state.ai_explanation = result.get("explanation")
                    st.sidebar.success(
                        f"Verified garbage report - {result.get('severity')}. Form below is pre-filled."
                    )
    if uploaded_photo is not None:
        if st.sidebar.button("Verify & Analyze with AI"):
            photo_bytes = uploaded_photo.getvalue()
            mime_type = uploaded_photo.type or "image/jpeg"
            result, ai_error = analyze_garbage_photo(photo_bytes, mime_type)
            if ai_error:
                st.sidebar.warning(ai_error)
            elif not result.get("is_garbage_issue"):
                st.sidebar.warning(
                    f"This doesn't look like a garbage problem: {result.get('explanation', 'no reason given')}. "
                    "You can still submit, but double-check your photo."
                )
                st.session_state.ai_category = None
                st.session_state.ai_rating = None
                st.session_state.ai_explanation = None
            else:
                if result.get("looks_ai_generated"):
                    st.sidebar.warning(
                        "This photo has some signs of being AI-generated rather than a real "
                        "photograph - best-effort AI guess, not a certainty, but worth a second look."
                    )
                st.session_state.ai_category = result.get("category")
                st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                st.session_state.ai_explanation = result.get("explanation")
                st.sidebar.success(
                    f"Verified garbage report - {result.get('severity')}. Form below is pre-filled."
                )
            st.session_state.pending_photo_bytes = photo_bytes

        if st.session_state.get("ai_explanation"):
            st.sidebar.caption(f"AI verdict: {st.session_state.ai_explanation}")

    default_category_index = (
        CATEGORIES.index(st.session_state.get("ai_category"))
        if st.session_state.get("ai_category") in CATEGORIES else 0
    )
    default_rating = st.session_state.get("ai_rating") or 3

    with st.sidebar.form("review_form", clear_on_submit=True):
        st.caption("Location is captured automatically from your map selection above - no typing needed.")
        category = st.selectbox("Category", CATEGORIES, index=default_category_index)
        rating = st.slider("Cleanliness (1 = hazardous / filthy, 5 = clean)", 1, 5, default_rating)
        description = st.text_area("Description", placeholder="Describe the garbage and the exact spot (e.g. behind the bus-stop wall, next to the drain)...")
        submitted = st.form_submit_button("Submit Review")

        if submitted:
            has_point = st.session_state.clicked_lat is not None
            has_segment = bool(st.session_state.segment_coords)
            cooldown_elapsed = get_seconds_since_last_submission(st.session_state.current_user)
            in_cooldown = cooldown_elapsed is not None and cooldown_elapsed < SUBMISSION_COOLDOWN_SECONDS

            if not has_point and not has_segment:
                st.error("Select a point, draw a segment, use your location, or search first.")
            elif in_cooldown:
                wait_seconds = int(SUBMISSION_COOLDOWN_SECONDS - cooldown_elapsed)
                st.error(f"Please wait {wait_seconds}s before submitting another report.")
            else:
                if has_segment:
                    points = st.session_state.segment_coords
                    centroid_lat = sum(p[0] for p in points) / len(points)
                    centroid_lon = sum(p[1] for p in points) / len(points)
                    path_json = json.dumps(points)
                else:
                    centroid_lat = st.session_state.clicked_lat
                    centroid_lon = st.session_state.clicked_lon
                    path_json = None

                duplicate = find_nearby_duplicate(centroid_lat, centroid_lon, category)

                if duplicate:
                    st.warning(
                        f"A similar '{category}' report already exists nearby "
                        f"(\"{duplicate['location_name']}\"). Please upvote that one in the "
                        "Community tab instead of creating a duplicate."
                    )
                else:
                    # Location name comes straight from the coordinates, not
                    # free text - closes off the "type something nonsensical
                    # as the location name" troll vector entirely.
                    derived_location_name = get_location_name(centroid_lat, centroid_lon)

                    pending_bytes = st.session_state.get("pending_photo_bytes")
                    photo_path = save_photo(pending_bytes, "review") if pending_bytes else None

                    insert_review(
                        lat=centroid_lat, lon=centroid_lon, location_name=derived_location_name,
                        rating=rating, category=category, description=description.strip(),
                        username=st.session_state.current_user,
                        path_coords=path_json, photo_path=photo_path,
                    )
                    add_civic_points(st.session_state.current_user, 10)
                    st.success("Report submitted. (+10 Civic Score)")

                    for key in ("clicked_lat", "clicked_lon", "segment_coords", "_last_picker_raw", "_last_geo_raw"):
                        st.session_state[key] = defaults[key]
                    for key in ("ai_category", "ai_rating", "ai_explanation", "pending_photo_bytes"):
                        st.session_state[key] = None
                    st.rerun()


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# MAIN AREA - HERO & TABS
# --------------------------------------------------------------------------
st.markdown(
    """
    <div class="hero-banner">
        <div class="hero-eyebrow">
            <span class="eco-pill dark">🌿 SMART CIVIC SANITATION INFRASTRUCTURE</span>
            <span style="font-family: 'Space Grotesk', monospace; font-size: 11.5px; color: #C7E66A; letter-spacing: 0.08em; display: inline-flex; align-items: center; gap: 6px;">
                <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#C7E66A; box-shadow:0 0 8px #C7E66A;"></span>
                NODE: CLEANSPOT-CAM-01 ACTIVE
            </span>
        </div>
        <h1>Spot it. Audit it. Resolve it.</h1>
        <p>
            Transforming urban waste blind spots into clean, accountable public spaces through community reporting, Gemini Multimodal AI vision triage, and autonomous cyber-physical edge sentinels.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not GOOGLE_MAPS_API_KEY:
    st.warning(
        "No Google Maps API key found. Set the GOOGLE_MAPS_API_KEY environment variable and restart "
        "to see the map (both the picker and heatmap views). (See the setup instructions at the top of clean_spot_app.py.)"
    )

tab_overview, tab_map, tab_reviews, tab_news, tab_sentinel = st.tabs([
    "🌿 Overview",
    "🗺️ Geospatial Operations",
    "👥 Community & Grievance SLA",
    "📰 Sanitation News",
    "🛡️ IoT Edge Sentinel",
])

# ----- TAB 1: EXECUTIVE OVERVIEW & LANDING EXPERIENCE -----------------------
with tab_overview:
    all_reviews_ov = fetch_all_reviews()
    total_reps = len(all_reviews_ov)
    open_reps = (all_reviews_ov["status"] == "Open").sum() if not all_reviews_ov.empty else 0
    fixed_reps = (all_reviews_ov["status"] == "Fixed").sum() if not all_reviews_ov.empty else 0
    severe_open_reps = ((all_reviews_ov["rating"] <= 2) & (all_reviews_ov["status"] == "Open")).sum() if not all_reviews_ov.empty else 0
    avg_cleanliness = f"{all_reviews_ov['rating'].mean():.1f} / 5" if not all_reviews_ov.empty else "5.0 / 5"
    res_rate = f"{(fixed_reps / total_reps * 100):.0f}%" if total_reps > 0 else "0%"

    # Top KPI metric cards
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
    with kpi1:
        st.metric("Total Incidents", total_reps)
    with kpi2:
        st.metric("Open Hotspots", int(severe_open_reps))
    with kpi3:
        st.metric("Resolved Spots", int(fixed_reps))
    with kpi4:
        st.metric("Resolution Rate", res_rate)
    with kpi5:
        st.metric("Avg Cleanliness", avg_cleanliness)
    with kpi6:
        st.metric("Edge Sentinels", "1 Active")

    st.markdown("---")

    # Problem Statement & 4-Stage Ecosystem Visual
    c_story, c_eco = st.columns([1.1, 1], gap="large")
    with c_story:
        st.markdown(
            """
            <div style="padding: 6px 0;">
                <span class="eco-pill" style="margin-bottom: 12px;">The Municipal Blind-Spot Challenge</span>
                <h2 style="font-size: 1.85rem; margin: 8px 0 14px 0; color: #14251B; line-height: 1.25;">
                    From Unattended Waste Chaos to Science-Backed, Actionable Civic Resolution
                </h2>
                <p style="color: #4A5244; font-size: 1.02rem; line-height: 1.6; margin-bottom: 16px;">
                    Traditional municipal complaint portals fail because citizens lack simple reporting tools, reports get lost in bureaucratic silos, and illegal dumping recurs in unattended neighbourhood blind spots.
                </p>
                <p style="color: #4A5244; font-size: 1.02rem; line-height: 1.6;">
                    <b>Civic-Bin</b> bridges this gap with an end-to-end cyber-physical platform: deploying autonomous IoT edge sentinels for optical surveillance, multimodal Gemini AI vision for automated triage, and community-driven SLA accountability linked directly to Greater Chennai Corporation (GCC) grievance infrastructure.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            """
            <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px;">
                <div class="eco-card" style="flex: 1; min-width: 140px; padding: 12px 14px; text-align: center;">
                    <div style="font-size: 1.4rem;">📸</div>
                    <div style="font-weight: 700; color: #14251B; font-size: 13px; margin-top: 4px;">Citizen Capture</div>
                    <div style="font-size: 11px; color: #777D73;">Mobile & Web Camera</div>
                </div>
                <div class="eco-card" style="flex: 1; min-width: 140px; padding: 12px 14px; text-align: center;">
                    <div style="font-size: 1.4rem;">🧠</div>
                    <div style="font-weight: 700; color: #14251B; font-size: 13px; margin-top: 4px;">Gemini Multimodal</div>
                    <div style="font-size: 11px; color: #777D73;">AI Vision Classification</div>
                </div>
                <div class="eco-card" style="flex: 1; min-width: 140px; padding: 12px 14px; text-align: center;">
                    <div style="font-size: 1.4rem;">🏛️</div>
                    <div style="font-weight: 700; color: #14251B; font-size: 13px; margin-top: 4px;">Municipal Routing</div>
                    <div style="font-size: 11px; color: #777D73;">GCC PGR Wording</div>
                </div>
                <div class="eco-card" style="flex: 1; min-width: 140px; padding: 12px 14px; text-align: center;">
                    <div style="font-size: 1.4rem;">🛡️</div>
                    <div style="font-weight: 700; color: #14251B; font-size: 13px; margin-top: 4px;">Edge Sentinel</div>
                    <div style="font-size: 11px; color: #777D73;">ESP32-CAM 3MP Node</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_eco:
        st.markdown(
            """
            <div class="eco-card-dark" style="height: 100%;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <span class="eco-pill dark">CYBER-PHYSICAL ECOSYSTEM ARCHITECTURE</span>
                    <span style="font-family: 'Space Grotesk', monospace; font-size: 11px; color: #C7E66A;">REV 1.0</span>
                </div>
                <h3 style="color: #FFFFFF; font-size: 1.45rem; margin: 0 0 14px 0;">4-Stage Civic Intelligence Loop</h3>
                <div style="display: flex; flex-direction: column; gap: 12px;">
                    <div style="background: rgba(255,255,255,0.06); padding: 12px 14px; border-radius: 10px; border-left: 3px solid #C7E66A;">
                        <b style="color: #C7E66A; font-size: 13px;">1. Optical Sourcing (Human & Node):</b>
                        <div style="color: #D3DFCE; font-size: 12px; margin-top: 2px;">
                            Citizens photograph street waste or CLEANSPOT-CAM-01 captures frames autonomously via local Wi-Fi.
                        </div>
                    </div>
                    <div style="background: rgba(255,255,255,0.06); padding: 12px 14px; border-radius: 10px; border-left: 3px solid #A7B99A;">
                        <b style="color: #A7B99A; font-size: 13px;">2. Multimodal AI Vision Triage:</b>
                        <div style="color: #D3DFCE; font-size: 12px; margin-top: 2px;">
                            Gemini Flash Vision validates waste presence, suggests category, estimates severity (1-5), and screens synthetic AI images.
                        </div>
                    </div>
                    <div style="background: rgba(255,255,255,0.06); padding: 12px 14px; border-radius: 10px; border-left: 3px solid #315C3B;">
                        <b style="color: #C7E66A; font-size: 13px;">3. Grievance Routing & SLA Enforcement:</b>
                        <div style="color: #D3DFCE; font-size: 12px; margin-top: 2px;">
                            Synthesizes official 400-char Greater Chennai Corporation (GCC) complaint text and starts a 7-day clean-up countdown clock.
                        </div>
                    </div>
                    <div style="background: rgba(255,255,255,0.06); padding: 12px 14px; border-radius: 10px; border-left: 3px solid #C7E66A;">
                        <b style="color: #A7B99A; font-size: 13px;">4. Community Verification & Civic Score:</b>
                        <div style="color: #D3DFCE; font-size: 12px; margin-top: 2px;">
                            Neighbours upvote reports, discuss local solutions, and submit before/after clean-up photos to earn Civic Score points.
                        </div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Category Breakdown & High-Priority Hotspots
    col_chart, col_hotspots = st.columns([1, 1], gap="medium")
    with col_chart:
        st.markdown("### 📊 Real-time Incident Distribution by Category")
        if not all_reviews_ov.empty and "category" in all_reviews_ov.columns:
            cat_counts = all_reviews_ov["category"].value_counts().reset_index()
            cat_counts.columns = ["Category", "Count"]
            st.bar_chart(cat_counts.set_index("Category"), color="#315C3B")
        else:
            st.info("No incident records in the database yet.")

    with col_hotspots:
        st.markdown("### 🚨 High-Priority Hotspots Under SLA")
        if not all_reviews_ov.empty:
            severe_df = all_reviews_ov[(all_reviews_ov["rating"] <= 2) & (all_reviews_ov["status"] == "Open")]
            if not severe_df.empty:
                for _, s_row in severe_df.head(3).iterrows():
                    rep_dt = datetime.strptime(s_row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    d_elapsed = (datetime.now() - rep_dt).days
                    days_left = max(0, SLA_DAYS - d_elapsed)
                    with st.container(border=True):
                        st.markdown(f"**Ticket #{s_row['id']} — {s_row['category']}** · Rating: `{int(s_row['rating'])}/5`")
                        st.caption(f"📍 {s_row['location_name']} · Reported by: **{s_row['username']}**")
                        if d_elapsed > SLA_DAYS:
                            st.error(f"⚠️ SLA Breached: Overdue by {d_elapsed - SLA_DAYS} day(s)")
                        else:
                            st.info(f"⏳ {days_left} day(s) remaining in 7-day clean-up SLA")
            else:
                st.success("🟢 No active severe hotspots! All severe reports have been resolved.")
        else:
            st.caption("No reports filed yet.")

    st.markdown("---")

    # Institutional Review Footer
    st.markdown(
        """
        <div style="background: #FFFFFF; border: 1px solid rgba(49, 92, 59, 0.16); border-radius: 14px; padding: 22px 28px; margin-top: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px;">
                <div>
                    <h4 style="margin: 0 0 6px 0; color: #14251B; font-family: 'Syne', sans-serif;">Civic-Bin · Design Thinking Review Project</h4>
                    <p style="margin: 0; color: #777D73; font-size: 13px;">
                        <b>Faculty Head:</b> Mr. Shakthivel &nbsp;|&nbsp; 
                        <b>Development Team:</b> Sailesh M.V, Rakesh JP, Thishanth T, Tharun B
                    </p>
                </div>
                <div style="font-family: 'Space Grotesk', monospace; font-size: 12px; color: #315C3B; text-align: right;">
                    <span>GCC PGR Gateway Active</span><br>
                    <span style="color: #777D73;">ESP32-CAM Node: 192.168.0.108</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ----- TAB 1: MAP (interactive picker + heatmap, toggle between them) -------
with tab_map:
    reviews_df = fetch_all_reviews()

    view_choice = st.radio(
        "View", ["Map (click / draw to report)", "Heatmap (open vs. cleaned)"],
        horizontal=True, label_visibility="collapsed",
    )
    is_heatmap_view = view_choice.startswith("Heatmap")

    col_map, col_stats = st.columns([3, 1])

    with col_stats:
        st.metric("Total Reports", len(reviews_df))
        if not reviews_df.empty:
            st.metric("Average Cleanliness", f"{reviews_df['rating'].mean():.1f} / 5")
            open_severe = ((reviews_df["rating"] <= 2) & (reviews_df["status"] == "Open")).sum()
            st.metric("Open Hotspots (2 stars or less)", int(open_severe))
        if is_heatmap_view:
            st.caption("Red = still open &nbsp; Green = cleaned up.")
        else:
            st.caption("Use the Draw Segment button (top-right of the map) to outline a stretch (an alley or lane) with garbage.")

    with col_map:
        if GOOGLE_MAPS_API_KEY:
            zoom_level = 13 if st.session_state.map_center == CHENNAI_COORDS else 15

            if is_heatmap_view:
                gmaps_component(
                    api_key=GOOGLE_MAPS_API_KEY,
                    center={"lat": CHENNAI_COORDS[0], "lng": CHENNAI_COORDS[1]},
                    zoom=12,
                    reviews=reviews_to_payload(reviews_df),
                    enable_picker=False,
                    show_heatmap=True,
                    key="heatmap",
                    default=None,
                )
            else:
                picker_result = gmaps_component(
                    api_key=GOOGLE_MAPS_API_KEY,
                    center={"lat": st.session_state.map_center[0], "lng": st.session_state.map_center[1]},
                    zoom=zoom_level,
                    reviews=reviews_to_payload(reviews_df),
                    enable_picker=True,
                    show_heatmap=False,
                    key="road_map",
                    default=None,
                )

                if picker_result and picker_result != st.session_state._last_picker_raw:
                    st.session_state._last_picker_raw = picker_result
                    if picker_result.get("type") == "click":
                        st.session_state.clicked_lat = picker_result["lat"]
                        st.session_state.clicked_lon = picker_result["lng"]
                        st.session_state.segment_coords = None
                    elif picker_result.get("type") == "drawing":
                        st.session_state.segment_coords = picker_result["path"]
                        st.session_state.clicked_lat = None
                        st.session_state.clicked_lon = None
        else:
            st.info("Map unavailable - add a Google Maps API key (see the warning above).")

    if not is_heatmap_view:
        st.markdown(
            "**Legend:** Red = 1-2 stars (Severe / hazardous) &nbsp;&nbsp; Orange = 3 stars (Moderate) "
            "&nbsp;&nbsp; Green = 4-5 stars (Clean, incl. cleaned up)"
        )

# ----- TAB 3: COMMUNITY (all reviews, upvotes, replies, fixes) -------------
with tab_reviews:
    st.markdown(
        """
        <div style="background: #FFFFFF; border: 1px solid rgba(49, 92, 59, 0.16); border-radius: 14px; padding: 22px 28px; margin-bottom: 20px; box-shadow: 0 4px 18px rgba(20, 37, 27, 0.04);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <div>
                    <span class="eco-pill" style="margin-bottom: 6px;">COMMUNITY GRIEVANCE TRACKER</span>
                    <h2 style="font-family: 'Syne', sans-serif; color: #14251B; margin: 2px 0 4px 0;">Public Incidents & Grievance SLA</h2>
                    <p style="color: #777D73; font-size: 13.5px; margin: 0;">Verified citizen and autonomous sentinel incident logs with photographic proof, municipal routing, and 7-day clean-up countdowns.</p>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    all_reviews = fetch_all_reviews()

    if all_reviews.empty:
        st.info("No reports yet - sign in and submit one from the sidebar!")
    else:
        for _, row in all_reviews.iterrows():
            review_id = int(row["id"])
            is_segment = isinstance(row["path_coords"], str)  # see note in reviews_to_payload re: NaN truthiness
            location_kind = "" if is_segment else ""
            status_pill = "🟢 CLEANED" if row["status"] == "Fixed" else "🔴 OPEN"
            stars = "★" * int(row["rating"]) + "☆" * (5 - int(row["rating"]))
            ticket_id = f"GR-{review_id:04d}"

            with st.expander(
                f"{ticket_id} · {status_pill} · {row['location_name']} · {stars}"
            ):
                st.caption(f"{row['category']} · reported by **{row['username']}**")
                st.write(safe_str(row["description"], "_No description provided._"))

                if row["status"] == "Open" and int(row["rating"]) <= 2:
                    reported_dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    days_elapsed = (datetime.now() - reported_dt).days
                    if days_elapsed > SLA_DAYS:
                        st.error(f"SLA breached - reported {days_elapsed} days ago (public {SLA_DAYS}-day clean-up limit)")
                    else:
                        st.info(f"{SLA_DAYS - days_elapsed} day(s) remaining under the {SLA_DAYS}-day clean-up SLA")

                # --- File with the citizen's actual local municipal office ---
                if row["status"] == "Open":
                    with st.expander("File this with your local municipal office"):
                        municipality_info, is_known_portal = get_municipality_info(row)
                        st.write(f"**Reporting to:** {municipality_info['name']}")

                        if is_known_portal:
                            st.caption(
                                "This is a real public complaints system. It requires your own mobile "
                                "number + OTP to submit, so we can't file this for you - but everything "
                                "below is pre-formatted to paste straight in."
                            )
                        else:
                            st.caption(
                                "We don't have this municipality's exact portal on file yet, so here's a "
                                "search link to find it, plus a generic complaint you can adapt to whatever "
                                "form it uses."
                            )

                        civic = build_civic_complaint(row, municipality_info, is_known_portal)
                        st.write(f"**Complaint Category:** {civic['group']}")
                        st.write(f"**Complaint Type:** {civic['type']}")
                        st.write("**Complaint Title:**")
                        st.code(civic["title"], language=None)

                        # An AI-polished version is optional and session-only - we
                        # never overwrite the DB, just what's displayed here.
                        polish_key = f"ai_polished_{review_id}"
                        if st.button("Improve wording with AI", key=f"ai_polish_btn_{review_id}"):
                            polished, ai_error = polish_complaint_with_ai(
                                civic["details"], row["category"], int(row["rating"]), row["location_name"]
                            )
                            if ai_error:
                                st.warning(ai_error)
                            else:
                                st.session_state[polish_key] = polished

                        details_to_show = st.session_state.get(polish_key, civic["details"])
                        st.write(f"**Details of Complaint** ({len(details_to_show)}/{GCC_DETAILS_MAX_CHARS} chars):")
                        st.code(details_to_show, language=None)
                        if polish_key in st.session_state:
                            st.caption("AI-polished wording shown above. This isn't saved - re-generate anytime.")

                        if row["photo_path"] and isinstance(row["photo_path"], str):
                            st.caption("Attach the photo below when you upload it on the portal:")
                            safe_image(row["photo_path"], width=200)

                        button_label = "Open Complaint Portal " if is_known_portal else "Search for Local Portal "
                        st.link_button(button_label, municipality_info["url"])

                col_up, _ = st.columns([1, 5])
                with col_up:
                    if st.session_state.current_user is None:
                        st.caption(f"{row['upvote_count']} · sign in to upvote")
                    elif has_upvoted(review_id, st.session_state.current_user):
                        st.caption(f"{row['upvote_count']} · you upvoted this")
                    else:
                        if st.button(f"{row['upvote_count']}", key=f"rev_upvote_{review_id}"):
                            if upvote_review(review_id, st.session_state.current_user):
                                add_civic_points(st.session_state.current_user, 1)
                            st.rerun()

                if row["status"] == "Open":
                    st.markdown("---")
                    st.markdown("**Mark as Cleaned**")
                    if isinstance(row["photo_path"], str):
                        safe_image(row["photo_path"], caption="Reported photo", width=250)
                    if st.session_state.current_user is None:
                        st.caption("Sign in to mark this spot as cleaned.")
                    else:
                        fixed_photo_method = st.radio(
                            "After-photo method", ["Take a live photo", "Upload from device"],
                            horizontal=True, label_visibility="collapsed", key=f"fixed_photo_method_{review_id}",
                        )
                        if fixed_photo_method.startswith("Take a live"):
                            fixed_photo = st.camera_input(
                                "Take an after-clean-up photo (optional)", key=f"fixed_photo_camera_{review_id}",
                            )
                        else:
                            fixed_photo = st.file_uploader(
                                "Upload an after-clean-up photo (optional)", type=["jpg", "jpeg", "png"],
                                key=f"fixed_photo_upload_{review_id}",
                            )
                        new_rating = st.slider("New cleanliness rating after clean-up", 1, 5, 5, key=f"fixed_rating_{review_id}")
                        if st.button("Confirm Cleaned", key=f"confirm_fixed_{review_id}"):
                            fixed_path = save_photo(fixed_photo.getvalue(), f"fixed_{review_id}") if fixed_photo else None
                            mark_review_fixed(review_id, new_rating, fixed_path)
                            add_civic_points(st.session_state.current_user, 15)
                            st.rerun()
                else:
                    st.success("This spot has been marked CLEANED by the community.")
                    col_before, col_after = st.columns(2)
                    with col_before:
                        if isinstance(row["photo_path"], str):
                            safe_image(row["photo_path"], caption="Before")
                    with col_after:
                        if isinstance(row["fixed_photo_path"], str):
                            safe_image(row["fixed_photo_path"], caption="After")

                st.markdown("---")
                st.markdown("**Replies:**")
                replies = fetch_replies(review_id)
                if replies.empty:
                    st.caption("No replies yet - be the first to respond.")
                else:
                    for _, reply in replies.iterrows():
                        st.markdown(f"**{reply['username']}:** {reply['reply_text']}")
                        st.caption(reply["timestamp"])

                if st.session_state.current_user is None:
                    st.caption("Sign in to reply.")
                else:
                    with st.form(f"reply_form_{review_id}", clear_on_submit=True):
                        reply_text = st.text_input("Add a reply", key=f"reply_input_{review_id}")
                        reply_submitted = st.form_submit_button("Post Reply")
                        if reply_submitted:
                            if reply_text.strip():
                                insert_reply(review_id, st.session_state.current_user, reply_text.strip())
                                add_civic_points(st.session_state.current_user, 2)
                                st.rerun()
                            else:
                                st.warning("Reply can't be empty.")

# ----- TAB 4: News (community news feed) ----------------------------
with tab_news:
    st.markdown(
        """
        <div style="background: #FFFFFF; border: 1px solid rgba(49, 92, 59, 0.16); border-radius: 14px; padding: 24px 28px; text-align: center; margin-bottom: 20px; box-shadow: 0 4px 18px rgba(20, 37, 27, 0.04);">
            <span class="eco-pill" style="margin-bottom: 8px;">SANITATION & ENVIRONMENTAL DISPATCH</span>
            <h1 style="font-family: 'Syne', sans-serif; font-size: 2.2rem; color: #14251B; margin: 4px 0 6px 0; letter-spacing: -0.02em;">Civic Environmental News</h1>
            <p style="color: #777D73; font-size: 13.5px; margin: 0; max-width: 600px; margin: auto;">Real-time coverage on solid waste management, illegal dumping blind spots, and public sanitation infrastructure powered by Google News RSS.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "Real news coverage of garbage and sanitation problems, pulled from Google News. "
        "Sorted newest first - upvotes show community trust, but never bump an article out of order."
    )

    news_query = st.text_input(
        "Search topic", value="Chennai garbage dumping OR waste management", key="news_search_query"
    )
    if st.button("Fetch Latest Headlines"):
        fetched = fetch_news_rss(news_query.strip())
        if fetched:
            store_news_items(fetched, news_query.strip())
            st.session_state.news_page = 1  # jump back to page 1 on a fresh fetch
            st.success(f"Fetched {len(fetched)} articles.")
        else:
            st.warning("No articles found, or the request failed - try a different search topic.")
        st.rerun()

    if "news_page" not in st.session_state:
        st.session_state.news_page = 1
    if "_last_shown_news_query" not in st.session_state:
        st.session_state._last_shown_news_query = None
    if st.session_state._last_shown_news_query != news_query.strip():
        # The search box changed since the last render - reset to page 1
        # rather than potentially landing on an out-of-range page for a
        # topic with fewer results than the last one.
        st.session_state.news_page = 1
        st.session_state._last_shown_news_query = news_query.strip()

    PAGE_SIZE = 10
    all_news = fetch_news_for_query(news_query.strip())

    if all_news.empty:
        st.info("No headlines for this topic yet - click 'Fetch Latest Headlines' above to pull some in.")
    else:
        total_pages = max(1, (len(all_news) + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.news_page = min(st.session_state.news_page, total_pages)
        page = st.session_state.news_page

        start = (page - 1) * PAGE_SIZE
        page_items = all_news.iloc[start:start + PAGE_SIZE]

        for _, item in page_items.iterrows():
            news_id = int(item["id"])
            with st.container(border=True):
                st.markdown(f'<div class="feed-article"><b>{item["title"]}</b></div>', unsafe_allow_html=True)
                st.caption(f"{item['source']} · {item['published']}")
                st.link_button("Read article ", item["link"])

                if st.session_state.current_user is None:
                    st.caption(f"{item['upvote_count']} · sign in to upvote")
                elif has_upvoted_news(news_id, st.session_state.current_user):
                    st.caption(f"{item['upvote_count']} · you upvoted this")
                else:
                    if st.button(f"{item['upvote_count']}", key=f"news_upvote_{news_id}"):
                        if upvote_news(news_id, st.session_state.current_user):
                            add_civic_points(st.session_state.current_user, 1)
                        st.rerun()

        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("Prev", disabled=(page <= 1)):
                st.session_state.news_page -= 1
                st.rerun()
        with col_info:
            st.markdown(
                f"<div style='text-align:center; padding-top:6px;'>Page {page} of {total_pages} "
                f"({len(all_news)} articles)</div>",
                unsafe_allow_html=True,
            )
        with col_next:
            if st.button("Next ", disabled=(page >= total_pages)):
                st.session_state.news_page += 1
                st.rerun()

# ==============================================================================
# TAB 4: AUTONOMOUS IOT EDGE SENTINEL (CLEANSPOT-CAM-01)
# ==============================================================================
with tab_sentinel:
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #14251B 0%, #1D3625 100%); border: 1px solid rgba(167, 185, 154, 0.25); border-radius: 16px; padding: 24px 28px; color: #F5F4ED; margin-bottom: 22px; box-shadow: 0 8px 28px rgba(20, 37, 27, 0.14);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 8px;">
                <span class="eco-pill dark">🛡️ CYBER-PHYSICAL EDGE VISION NODE</span>
                <span style="font-family: 'Space Grotesk', monospace; font-size: 11px; color: #C7E66A;">HARDWARE: ESP32-CAM (OV3660 3MP)</span>
            </div>
            <h1 style="font-family: 'Syne', sans-serif; font-size: 2.2rem; color: #FFFFFF; margin: 4px 0 8px 0; letter-spacing: -0.02em;">Autonomous IoT Edge Sentinel</h1>
            <p style="color: #D3DFCE; font-size: 14px; margin: 0; max-width: 780px;">
                Automated blind-spot optical surveillance node with 1W hardware spotlight, edge Wi-Fi server, Gemini Multimodal AI vision triage, and direct Greater Chennai Corporation (GCC) grievance escalation.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_cam, col_ai = st.columns([1, 1], gap="medium")

    sentinel_ip = st.session_state.get("iot_cam_url", "http://192.168.0.108/capture")

    with col_cam:
        st.markdown("### 📡 Live Edge Node Viewfinder")
        st.caption("Hardware: **ESP32-CAM 3MP Optical Matrix** · Node ID: `CLEANSPOT-CAM-01`")

        if st.session_state.get("sentinel_last_img") is not None:
            st.image(st.session_state.sentinel_last_img, caption="Live Frame from CLEANSPOT-CAM-01", use_container_width=True)
        else:
            st.info("No frame captured yet. Click below to grab a live frame from your desk camera.")

        btn_c1, btn_c2 = st.columns(2)
        with btn_c1:
            if st.button("📸 Grab Live Frame", use_container_width=True):
                try:
                    import urllib.request
                    with st.spinner("Connecting to ESP32-CAM..."):
                        req = urllib.request.Request(sentinel_ip, headers={"User-Agent": "CleanSpot-Web/1.0"})
                        with urllib.request.urlopen(req, timeout=6) as response:
                            img_data = response.read()
                            if img_data:
                                st.session_state.sentinel_last_img = img_data
                                st.success("Frame retrieved successfully!")
                                st.rerun()
                except Exception as e:
                    st.error(f"Could not reach camera node at {sentinel_ip}: {e}")
        with btn_c2:
            st.link_button("🌐 Open Node Web Server", "http://192.168.0.108", use_container_width=True)

        with st.container(border=True):
            st.markdown("#### ⚙️ Edge Telemetry Stream")
            st.markdown(
                "• **Operational Status:** 🟢 `ARMED & MONITORING`  \n"
                "• **Node Coordinates:** `13.0827° N, 80.2707° E` (Chennai Central Hotspot)  \n"
                "• **Night Vision Spotlight:** `Ready (1W White SMD Flash)`  \n"
                "• **Surveillance Domain:** Unattended Neighborhood Blind Spot"
            )

    with col_ai:
        st.markdown("### 🤖 Autonomous AI Grievance Engine")
        st.caption("Evaluates live camera frames, filters false alarms, and autonomously registers municipal tickets.")

        st.markdown(
            "When triggered, the Sentinel executes our 3-stage cyber-physical triage pipeline:  \n"
            "**1. Optical Capture** ➔ **2. Gemini Vision Triage** ➔ **3. Auto-Filing in Civic-Bin DB & GCC Routing**"
        )

        if st.button("⚡ EXECUTE AUTONOMOUS SURVEILLANCE AUDIT", type="primary", use_container_width=True):
            try:
                import urllib.request
                import uuid
                with st.status("Executing Autonomous Sentinel Pipeline...", expanded=True) as status_box:
                    st.write("📡 [1/3] Triggering hardware spotlight & capturing optical frame from ESP32-CAM...")
                    req = urllib.request.Request(sentinel_ip, headers={"User-Agent": "CleanSpot-Web/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as response:
                        frame_bytes = response.read()

                    st.session_state.sentinel_last_img = frame_bytes

                    st.write("🧠 [2/3] Passing frame to Gemini Multimodal AI Vision Engine...")
                    result, ai_error = analyze_garbage_photo(frame_bytes, "image/jpeg")

                    if ai_error:
                        status_box.update(label="AI Inspection Failed", state="error")
                        st.error(ai_error)
                    elif not result.get("is_garbage_issue"):
                        status_box.update(label="Scene Clear — No Waste Detected", state="complete")
                        st.info(f"🟢 **No Waste Violation:** {result.get('explanation')}")
                        st.caption("AI successfully filtered false alarm. No duplicate or junk ticket submitted to municipal queue.")
                    else:
                        st.write("🏛️ [3/3] Waste confirmed! Auto-registering ticket in Civic-Bin DB & synthesizing GCC complaint...")
                        filename = f"sentinel_{uuid.uuid4().hex[:8]}.jpg"
                        filepath = os.path.join(UPLOAD_DIR, filename)
                        with open(filepath, "wb") as f:
                            f.write(frame_bytes)

                        cat = result.get("category") or "General Litter"
                        sev = result.get("severity") or "Severe"
                        rating_score = SEVERITY_TO_RATING.get(sev, 2)
                        desc = f"[AUTONOMOUS SENTINEL DETECTION] {result.get('explanation')}"
                        loc_name = "Chennai Central Hotspot 01"

                        insert_review(
                            lat=13.0827,
                            lon=80.2707,
                            location_name=loc_name,
                            rating=rating_score,
                            category=cat,
                            description=desc,
                            username="CLEANSPOT-SENTINEL-01",
                            path_coords=None,
                            photo_path=filepath
                        )

                        status_box.update(label="🚨 Illegal Dumping Confirmed! Ticket Auto-Filed!", state="complete")
                        st.success(f"**Ticket Auto-Filed Successfully!**")
                        st.markdown(f"• **Category:** `{cat}`  \n• **Severity Level:** `{sev}` (Rating: `{rating_score}/5`)  \n• **Reporter:** `CLEANSPOT-SENTINEL-01`")
                        st.markdown(f"• **AI Explanation:** *{result.get('explanation')}*")
                        st.caption("The new ticket has been plotted on the live map and added to the public clean-up countdown clock.")
                        st.rerun()

            except Exception as e:
                st.error(f"Autonomous audit error: {e}")

        st.markdown("---")
        st.markdown("#### 📋 Recent Sentinel Auto-Registered Tickets")
        all_reps = fetch_all_reviews()
        if not all_reps.empty and "username" in all_reps.columns:
            sentinel_reps = all_reps[all_reps["username"] == "CLEANSPOT-SENTINEL-01"]
            if not sentinel_reps.empty:
                for _, r in sentinel_reps.tail(3).iloc[::-1].iterrows():
                    with st.container(border=True):
                        st.markdown(f"**Ticket #{r['id']} — {r['category']}** (Severity: {r['rating']}/5)")
                        st.caption(f"📍 {r['location_name']} · Timestamp: {r['timestamp']}")
                        st.write(r['description'])
            else:
                st.caption("No autonomous tickets filed yet. Run an audit on a garbage sample above!")
        else:
            st.caption("No reports registered yet.")

