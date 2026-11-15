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
    page_title="Civic-Bin | Environmental Sanitation & Grievance Intelligence",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Clean, professional vector mark
CLEAN_SPOT_MARK_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160"><rect width="160" height="160" rx="14" fill="#183B2A"/><path d="M52 64 L58 116 Q59 122 66 122 L94 122 Q101 122 102 116 L108 64" fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><path d="M44 64 H116" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round"/><path d="M66 64 V56 Q66 52 70 52 H90 Q94 52 94 56 V64" stroke="#FFFFFF" stroke-width="4.5" fill="none"/><path d="M80 108 V82 M80 82 Q80 72 92 74 Q92 84 80 88" fill="none" stroke="#B9D96A" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>'


def render_logo(width, container=st):
    """Renders the Civic-Bin brand identity mark."""
    container.markdown(
        f'<div style="display:flex;justify-content:center;margin:6px 0 10px 0;">'
        f'<div style="width:{width}px;height:{width}px;">{CLEAN_SPOT_MARK_SVG}</div></div>',
        unsafe_allow_html=True,
    )


render_logo(88, st.sidebar)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# DESIGN SYSTEM: RESTRAINED EDITORIAL ENVIRONMENTAL TECHNOLOGY
# Palette:
# Primary Forest: #183B2A | Secondary Forest: #28543A | Charcoal: #202923
# Page Background: #F6F6F0 | Surface White: #FFFFFF | Soft Sage: #E7EDE4
# Muted Green: #66836B | Secondary Text: #68736B | Border: #E2E5DE
# Success: #267348 | Warning: #A66B17 | Error: #B54740 | Accent Lime: #B9D96A
# --------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    html, body, [class*="css"], .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
        background-color: #F6F6F0 !important;
        color: #202923 !important;
    }

    /* Universal Text Color Guard (strictly exclude button contents, alerts, selected tabs, badges) */
    .stApp p:not(button *):not([data-testid="stAlert"] *):not([aria-selected="true"] *):not(.badge-open):not(.badge-fixed):not(.volunteer-card-badge):not(.drive-status-scheduled):not(.drive-status-completed),
    .stApp span:not(button *):not([data-testid="stAlert"] *):not([aria-selected="true"] *):not(.badge-open):not(.badge-fixed):not(.volunteer-card-badge):not(.drive-status-scheduled):not(.drive-status-completed),
    .stApp div:not(button *):not([data-testid="stAlert"] *):not([aria-selected="true"] *),
    .stApp li:not(button *),
    .stApp td,
    .stApp th {
        color: #202923;
    }

    /* Headings */
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Inter', sans-serif !important;
        color: #183B2A !important;
        font-weight: 700 !important;
        letter-spacing: -0.015em !important;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
        border-right: 1px solid #E2E5DE !important;
        box-shadow: none !important;
        padding-top: 1rem !important;
    }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {
        font-size: 14px !important;
        font-weight: 700 !important;
        color: #183B2A !important;
        text-transform: uppercase !important;
        letter-spacing: 0.04em !important;
        margin-top: 18px !important;
        margin-bottom: 8px !important;
    }

    /* Form Labels & Captions */
    label[data-testid="stWidgetLabel"],
    label[data-testid="stWidgetLabel"] p,
    label[data-testid="stWidgetLabel"] span {
        color: #202923 !important;
        font-weight: 600 !important;
        font-size: 13px !important;
    }
    [data-testid="stCaptionContainer"] p,
    [data-testid="stCaptionContainer"] {
        color: #68736B !important;
        font-size: 12.5px !important;
    }

    /* ==========================================================================
       BUTTON SYSTEM (Strict High-Contrast Guarantee)
       ========================================================================== */
    
    /* Base resets for all button elements */
    button,
    div.stButton > button,
    .stForm button,
    div.stLinkButton > a,
    div.stDownloadButton > button {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
        font-weight: 600 !important;
        font-size: 13.5px !important;
        line-height: 1.4 !important;
        border-radius: 6px !important;
        padding: 8px 18px !important;
        min-height: 38px !important;
        cursor: pointer !important;
        transition: all 0.15s ease-in-out !important;
        text-decoration: none !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }

    /* PRIMARY BUTTONS: Forest Green (#183B2A) background with crisp White (#FFFFFF) text */
    div.stButton > button[kind="primary"],
    div.stButton > button[data-testid="baseButton-primary"],
    button[data-testid="baseButton-primary"],
    button[kind="primary"],
    .stForm button {
        background-color: #183B2A !important;
        border: 1px solid #183B2A !important;
        box-shadow: 0 1px 2px rgba(24, 59, 42, 0.12) !important;
    }
    div.stButton > button[kind="primary"] *,
    div.stButton > button[kind="primary"] p,
    div.stButton > button[kind="primary"] span,
    div.stButton > button[kind="primary"] div,
    div.stButton > button[data-testid="baseButton-primary"] *,
    div.stButton > button[data-testid="baseButton-primary"] p,
    div.stButton > button[data-testid="baseButton-primary"] span,
    div.stButton > button[data-testid="baseButton-primary"] div,
    button[data-testid="baseButton-primary"] *,
    button[data-testid="baseButton-primary"] p,
    button[data-testid="baseButton-primary"] span,
    button[data-testid="baseButton-primary"] div,
    button[kind="primary"] *,
    button[kind="primary"] p,
    button[kind="primary"] span,
    button[kind="primary"] div,
    .stForm button *,
    .stForm button p,
    .stForm button span,
    .stForm button div {
        color: #FFFFFF !important;
        font-weight: 600 !important;
        letter-spacing: 0.01em !important;
    }

    /* Primary Button Hover */
    div.stButton > button[kind="primary"]:hover,
    div.stButton > button[data-testid="baseButton-primary"]:hover,
    button[data-testid="baseButton-primary"]:hover,
    button[kind="primary"]:hover,
    .stForm button:hover {
        background-color: #28543A !important;
        border-color: #28543A !important;
        box-shadow: 0 2px 6px rgba(24, 59, 42, 0.22) !important;
    }
    div.stButton > button[kind="primary"]:hover *,
    div.stButton > button[data-testid="baseButton-primary"]:hover *,
    button[data-testid="baseButton-primary"]:hover *,
    button[kind="primary"]:hover *,
    .stForm button:hover * {
        color: #FFFFFF !important;
    }

    /* Primary Button Active & Focus */
    div.stButton > button[kind="primary"]:active,
    button[data-testid="baseButton-primary"]:active,
    .stForm button:active {
        background-color: #132E21 !important;
        border-color: #132E21 !important;
        transform: translateY(1px) !important;
    }
    div.stButton > button[kind="primary"]:focus-visible,
    button[data-testid="baseButton-primary"]:focus-visible {
        outline: 2px solid #28543A !important;
        outline-offset: 2px !important;
    }

    /* SECONDARY BUTTONS: Surface White (#FFFFFF) / Sage with Dark Forest (#183B2A) text */
    div.stButton > button[kind="secondary"],
    div.stButton > button[data-testid="baseButton-secondary"],
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]),
    button[data-testid="baseButton-secondary"],
    button[kind="secondary"],
    div.stDownloadButton > button,
    div.stLinkButton > a {
        background-color: #FFFFFF !important;
        border: 1px solid #C3D5BF !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03) !important;
    }
    div.stButton > button[kind="secondary"] *,
    div.stButton > button[kind="secondary"] p,
    div.stButton > button[kind="secondary"] span,
    div.stButton > button[kind="secondary"] div,
    div.stButton > button[data-testid="baseButton-secondary"] *,
    div.stButton > button[data-testid="baseButton-secondary"] p,
    div.stButton > button[data-testid="baseButton-secondary"] span,
    div.stButton > button[data-testid="baseButton-secondary"] div,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) *,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) p,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) span,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) div,
    button[data-testid="baseButton-secondary"] *,
    button[data-testid="baseButton-secondary"] p,
    button[data-testid="baseButton-secondary"] span,
    button[data-testid="baseButton-secondary"] div,
    button[kind="secondary"] *,
    button[kind="secondary"] p,
    button[kind="secondary"] span,
    button[kind="secondary"] div,
    div.stDownloadButton > button *,
    div.stDownloadButton > button p,
    div.stDownloadButton > button span,
    div.stDownloadButton > button div,
    div.stLinkButton > a *,
    div.stLinkButton > a p,
    div.stLinkButton > a span,
    div.stLinkButton > a div {
        color: #183B2A !important;
        font-weight: 600 !important;
    }

    /* Secondary Button Hover */
    div.stButton > button[kind="secondary"]:hover,
    div.stButton > button[data-testid="baseButton-secondary"]:hover,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]):hover,
    button[data-testid="baseButton-secondary"]:hover,
    button[kind="secondary"]:hover,
    div.stDownloadButton > button:hover,
    div.stLinkButton > a:hover {
        background-color: #E7EDE4 !important;
        border-color: #183B2A !important;
        box-shadow: 0 2px 4px rgba(24, 59, 42, 0.08) !important;
    }
    div.stButton > button[kind="secondary"]:hover *,
    div.stButton > button[data-testid="baseButton-secondary"]:hover *,
    div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]):hover *,
    button[data-testid="baseButton-secondary"]:hover *,
    button[kind="secondary"]:hover *,
    div.stDownloadButton > button:hover *,
    div.stLinkButton > a:hover * {
        color: #183B2A !important;
    }

    /* Secondary Button Active & Focus */
    div.stButton > button[kind="secondary"]:active,
    div.stButton > button:not([kind="primary"]):active,
    button[data-testid="baseButton-secondary"]:active {
        background-color: #D9E4D5 !important;
        border-color: #183B2A !important;
        transform: translateY(1px) !important;
    }
    div.stButton > button[kind="secondary"]:focus-visible,
    button[data-testid="baseButton-secondary"]:focus-visible,
    div.stLinkButton > a:focus-visible {
        outline: 2px solid #183B2A !important;
        outline-offset: 2px !important;
    }

    /* DESTRUCTIVE ACTIONS */
    button.btn-destructive,
    div.stButton > button[kind="destructive"] {
        background-color: #B54740 !important;
        border: 1px solid #B54740 !important;
    }
    button.btn-destructive *,
    div.stButton > button[kind="destructive"] * {
        color: #FFFFFF !important;
    }
    button.btn-destructive:hover,
    div.stButton > button[kind="destructive"]:hover {
        background-color: #933630 !important;
        border-color: #933630 !important;
    }

    /* DISABLED BUTTONS */
    button:disabled,
    button[disabled],
    button:disabled:hover,
    button[disabled]:hover,
    div.stButton > button:disabled,
    div.stButton > button[disabled] {
        background-color: #EBECE8 !important;
        border-color: #D6D8D2 !important;
        cursor: not-allowed !important;
        box-shadow: none !important;
        transform: none !important;
    }
    button:disabled *,
    button[disabled] *,
    button:disabled p,
    button[disabled] p,
    button:disabled span,
    button[disabled] span,
    div.stButton > button:disabled *,
    div.stButton > button[disabled] * {
        color: #8A948D !important;
    }

    /* Input Controls */
    div[data-baseweb="input"], div[data-baseweb="base-input"], div[data-baseweb="select"] > div,
    div[data-baseweb="textarea"], textarea, .stTextInput input, .stNumberInput input {
        background-color: #FFFFFF !important;
        border: 1px solid #D1D5DB !important;
        color: #202923 !important;
        border-radius: 6px !important;
        font-size: 13.5px !important;
        box-shadow: none !important;
    }
    div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within,
    div[data-baseweb="textarea"]:focus-within {
        border-color: #183B2A !important;
        box-shadow: 0 0 0 2px rgba(24, 59, 42, 0.12) !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px !important;
        border-bottom: 1px solid #E2E5DE !important;
        background: transparent !important;
        padding-bottom: 0 !important;
        margin-bottom: 22px !important;
    }
    .stTabs [data-baseweb="tab"] {
        background: #FFFFFF !important;
        border: 1px solid #E2E5DE !important;
        border-bottom: none !important;
        border-radius: 6px 6px 0 0 !important;
        padding: 8px 18px !important;
        transition: all 0.15s ease !important;
    }
    .stTabs [data-baseweb="tab"] p,
    .stTabs [data-baseweb="tab"] span,
    .stTabs [data-baseweb="tab"] div {
        color: #68736B !important;
        font-weight: 600 !important;
        font-size: 13px !important;
    }
    .stTabs [data-baseweb="tab"]:hover p,
    .stTabs [data-baseweb="tab"]:hover span {
        color: #183B2A !important;
    }
    .stTabs [aria-selected="true"] {
        background: #183B2A !important;
        border-color: #183B2A !important;
    }
    .stTabs [aria-selected="true"] p,
    .stTabs [aria-selected="true"] span,
    .stTabs [aria-selected="true"] div {
        color: #FFFFFF !important;
        font-weight: 700 !important;
    }

    /* Containers & Expanders */
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
        background: #FFFFFF !important;
        border: 1px solid #E2E5DE !important;
        border-radius: 8px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.02) !important;
        padding: 16px 18px !important;
    }
    div[data-testid="stExpander"] {
        border: 1px solid #E2E5DE !important;
        border-radius: 8px !important;
        background: #FFFFFF !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.02) !important;
        margin-bottom: 10px !important;
    }
    div[data-testid="stExpander"] summary p,
    div[data-testid="stExpander"] summary span {
        color: #202923 !important;
        font-weight: 600 !important;
        font-size: 13.5px !important;
    }

    /* Refined Alerts System (High Contrast & Professional Hierarchy) */
    div[data-testid="stAlert"] {
        border-radius: 6px !important;
        padding: 11px 15px !important;
        margin-bottom: 14px !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03) !important;
        background-color: #FFFDF5 !important;
        border: 1px solid #E8D39E !important;
        border-left: 4px solid #996515 !important;
    }
    div[data-testid="stAlert"] p,
    div[data-testid="stAlert"] span,
    div[data-testid="stAlert"] div {
        color: #5C3D06 !important;
        font-size: 13px !important;
        line-height: 1.5 !important;
        font-weight: 500 !important;
    }
    div[data-testid="stAlert"] svg {
        fill: #996515 !important;
        color: #996515 !important;
    }

    /* Native Metric overrides */
    div[data-testid="stMetricValue"],
    div[data-testid="stMetricValue"] div {
        color: #183B2A !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
        font-size: 1.6rem !important;
    }
    div[data-testid="stMetricLabel"],
    div[data-testid="stMetricLabel"] p {
        color: #68736B !important;
        font-weight: 600 !important;
        font-size: 0.78rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.04em !important;
    }

    /* Editorial Hero */
    .editorial-hero {
        background: #FFFFFF;
        border: 1px solid #E2E5DE;
        border-radius: 8px;
        padding: 24px 28px;
        margin-bottom: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.02);
    }
    .editorial-tag {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: #66836B;
        margin-bottom: 8px;
    }
    .editorial-title {
        font-size: 1.85rem;
        font-weight: 700;
        color: #183B2A;
        margin: 0 0 8px 0;
        letter-spacing: -0.02em;
    }
    .editorial-desc {
        font-size: 14.5px;
        color: #68736B;
        line-height: 1.55;
        margin: 0;
        max-width: 760px;
    }

    /* KPI Row Grid - Non-truncating */
    .kpi-row {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px;
        margin-bottom: 20px;
    }
    .kpi-tile {
        background: #FFFFFF;
        border: 1px solid #E2E5DE;
        border-radius: 8px;
        padding: 14px 16px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .kpi-tile-label {
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        color: #68736B;
        letter-spacing: 0.04em;
        margin-bottom: 6px;
    }
    .kpi-tile-val {
        font-size: 24px;
        font-weight: 700;
        color: #183B2A;
        line-height: 1.1;
    }
    .kpi-tile-val.warning { color: #A66B17; }
    .kpi-tile-val.success { color: #267348; }
    .kpi-tile-val.accent { color: #28543A; }
    .kpi-tile-sub {
        font-size: 11.5px;
        color: #68736B;
        margin-top: 4px;
    }

    /* Badges */
    .badge-open {
        background: #FEF3C7;
        color: #92400E;
        border: 1px solid #FDE68A;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        display: inline-block;
    }
    .badge-fixed {
        background: #E7EDE4;
        color: #267348;
        border: 1px solid #C3D5BF;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        display: inline-block;
    }

    /* Volunteer Network Specific Components */
    .volunteer-card-cat {
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        color: #68736B;
        letter-spacing: 0.05em;
        margin-bottom: 4px;
    }
    .volunteer-card-title {
        font-size: 17px;
        font-weight: 700;
        color: #183B2A;
        margin: 0;
        line-height: 1.25;
    }
    .volunteer-card-badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        background: #E7EDE4;
        color: #267348;
        border: 1px solid #C3D5BF;
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }
    .volunteer-card-desc {
        font-size: 13.5px;
        color: #3F4741;
        line-height: 1.5;
        margin: 8px 0 12px 0;
    }
    .volunteer-card-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 16px;
        font-size: 12.5px;
        color: #68736B;
        margin-bottom: 12px;
        padding-top: 10px;
        border-top: 1px solid #F0F2EE;
    }
    .volunteer-card-meta-item {
        display: flex;
        align-items: center;
        gap: 5px;
    }
    .volunteer-card-tag {
        background: #F0F4EE;
        color: #183B2A;
        border: 1px solid #D6E2D4;
        border-radius: 12px;
        padding: 2px 10px;
        font-size: 11px;
        font-weight: 500;
        display: inline-block;
        margin-right: 6px;
        margin-bottom: 6px;
    }
    .drive-status-scheduled {
        background: #FEF3C7;
        color: #92400E;
        border: 1px solid #FDE68A;
        border-radius: 4px;
        padding: 3px 8px;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .drive-status-completed {
        background: #E7EDE4;
        color: #267348;
        border: 1px solid #C3D5BF;
        border-radius: 4px;
        padding: 3px 8px;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .drive-hotspot-link {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        background: #FFFBEB;
        color: #B45309;
        border: 1px solid #FDE68A;
        border-radius: 4px;
        padding: 3px 8px;
        font-size: 11.5px;
        font-weight: 600;
        margin-bottom: 8px;
    }
    .drive-stat-pill {
        background: #F6F6F0;
        border: 1px solid #E2E5DE;
        border-radius: 6px;
        padding: 6px 12px;
        text-align: center;
        margin-bottom: 8px;
    }
    .drive-stat-num {
        font-size: 17px;
        font-weight: 700;
        color: #183B2A;
    }
    .drive-stat-lbl {
        font-size: 10.5px;
        font-weight: 600;
        text-transform: uppercase;
        color: #68736B;
    }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 7px; height: 7px; }
    ::-webkit-scrollbar-track { background: #F6F6F0; }
    ::-webkit-scrollbar-thumb { background: #D1D5DB; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #9CA3AF; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Registers our hand-built HTML/JS components.
_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmaps_component")
gmaps_component = components.declare_component("gmaps_component", path=_component_dir)

_geo_component_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_component")
geo_component = components.declare_component("geo_component", path=_geo_component_dir)


# --------------------------------------------------------------------------
# SPLASH SCREEN + ACCESS TERMINAL
# --------------------------------------------------------------------------
def render_splash_screen():
    st.markdown(
        """
        <style>
        section[data-testid="stSidebar"], header[data-testid="stHeader"] { display: none !important; }
        .stApp { background-color: #183B2A !important; }
        .stApp p, .stApp span, .stApp div { color: #FFFFFF !important; }
        </style>
        <div style="min-height: 75vh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;">
            <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.1em; color: #B9D96A; text-transform: uppercase; margin-bottom: 12px;">CIVIC ENVIRONMENTAL INTELLIGENCE</div>
            <h1 style="font-size: 2.8rem; font-weight: 700; color: #FFFFFF !important; margin: 0 0 6px 0;">CIVIC-BIN</h1>
            <p style="color: #E7EDE4; font-size: 14px; max-width: 500px; margin: 0 0 24px 0;">Making every public space accountable through community reporting, Gemini AI vision triage, and autonomous edge surveillance.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _, mid_col, _ = st.columns([1, 1, 1])
    with mid_col:
        render_logo(100)
        if st.button("Enter Platform", key="splash_enter_btn", type="primary", use_container_width=True):
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
                    if (b.innerText && (b.innerText.includes('Enter Platform') || b.innerText.includes('Enter Clean Spot') || b.innerText.includes('Enter Civic-Bin'))) {
                        b.click();
                        break;
                    }
                }
            } catch (e) {}
        }, 2200);
        </script>
        """,
        height=0,
    )


def render_auth_page():
    st.markdown(
        """<style>section[data-testid="stSidebar"], header[data-testid="stHeader"] { display: none !important; }</style>""",
        unsafe_allow_html=True,
    )
    _, mid_col, _ = st.columns([1, 1.2, 1])
    with mid_col:
        render_logo(80)
        st.markdown(
            """
            <div style="background: #FFFFFF; border: 1px solid #E2E5DE; border-radius: 8px; padding: 24px 28px 12px 28px; text-align: center; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.03);">
                <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.05em; color: #66836B; text-transform: uppercase; margin-bottom: 4px;">OPERATIONAL ACCESS</div>
                <h2 style="font-size: 1.6rem; color: #183B2A; margin: 0 0 6px 0;">Sign in to Civic-Bin</h2>
                <p style="color: #68736B; font-size: 13px; margin: 0;">Report waste blind spots, track 7-day clean-up SLAs, and verify public resolutions.</p>
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
            if st.button("Confirm Username", key="confirm_google_username_btn", type="primary", use_container_width=True):
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
                st.markdown('<div style="text-align:center; color:#68736B; font-size:11px; margin:12px 0; text-transform:uppercase;">— or use local credentials —</div>', unsafe_allow_html=True)
            else:
                st.caption("Google OAuth not configured (set GOOGLE_OAUTH_CLIENT_ID/SECRET to enable).")

            login_tab, signup_tab = st.tabs(["Sign In", "Sign Up"])
            with login_tab:
                login_username = st.text_input("Username", key="login_username")
                login_password = st.text_input("Password", type="password", key="login_password")
                if st.button("Sign In", key="login_btn", type="primary", use_container_width=True):
                    if verify_user(login_username.strip(), login_password):
                        st.session_state.current_user = login_username.strip()
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
            with signup_tab:
                signup_username = st.text_input("Choose a username", key="signup_username")
                signup_password = st.text_input("Choose a password", type="password", key="signup_password")
                if st.button("Create Account", key="signup_btn", type="primary", use_container_width=True):
                    if not signup_username.strip() or not signup_password:
                        st.error("Username and password are both required.")
                    else:
                        ok, err = create_user(signup_username.strip(), signup_password)
                        if ok:
                            st.session_state.current_user = signup_username.strip()
                            st.rerun()
                        else:
                            st.error(err)

            st.markdown('<div style="text-align:center; color:#68736B; font-size:11px; margin:12px 0; text-transform:uppercase;">— or —</div>', unsafe_allow_html=True)
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

    # --- Volunteer Network tables ---
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS volunteer_groups (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,
            category         TEXT    NOT NULL,
            description      TEXT,
            city             TEXT    NOT NULL,
            areas_served     TEXT,
            focus_areas      TEXT,
            contact_email    TEXT,
            contact_phone    TEXT,
            leader_username  TEXT    NOT NULL,
            member_count     INTEGER DEFAULT 1,
            is_verified      INTEGER DEFAULT 0,
            created_at       TEXT    NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS volunteer_memberships (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL,
            username   TEXT    NOT NULL,
            joined_at  TEXT    NOT NULL,
            UNIQUE(group_id, username),
            FOREIGN KEY (group_id) REFERENCES volunteer_groups (id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cleanup_drives (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id            INTEGER,
            hotspot_report_id   INTEGER,
            title               TEXT    NOT NULL,
            location_name       TEXT    NOT NULL,
            target_date         TEXT    NOT NULL,
            target_time         TEXT    NOT NULL,
            description         TEXT,
            required_volunteers INTEGER DEFAULT 10,
            status              TEXT    DEFAULT 'Scheduled',
            created_by          TEXT    NOT NULL,
            created_at          TEXT    NOT NULL,
            fixed_photo_path    TEXT,
            bags_cleared        INTEGER DEFAULT 0,
            FOREIGN KEY (group_id) REFERENCES volunteer_groups (id),
            FOREIGN KEY (hotspot_report_id) REFERENCES garbage_reports (id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_rsvps (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            drive_id  INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(drive_id, username),
            FOREIGN KEY (drive_id) REFERENCES cleanup_drives (id)
        )
        """
    )

    # Seed Default Authentic Chennai Volunteer Organizations if empty
    cursor.execute("SELECT COUNT(*) FROM volunteer_groups")
    if cursor.fetchone()[0] == 0:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seed_groups = [
            (
                "NSS Anna University Environmental Cell",
                "University / NSS Cell",
                "Student-led collegiate conservation wing mobilizing engineering cohorts for urban canal audits, park reclamation, and campus zero-waste stewardship.",
                "Chennai",
                "Guindy, Adyar, Kotturpuram, Velachery",
                "Illegal Dumping, Overflowing Bins, Campus Perimeters",
                "nss.enviro@annauniv.edu",
                "+91 94440 12890",
                "shakthivel",
                128,
                1,
                now_str,
            ),
            (
                "Namma Chennai Clean Squad",
                "Youth Cleanup Squad",
                "Decentralized youth volunteering guild targeting high-density transit junctions, pedestrian subways, and beach blackspots with weekly community sweeps.",
                "Chennai",
                "Marina Beach, Mylapore, Royapettah, Foreshore Estate",
                "Beach & Waterways, Bulk Waste, Plastic Debris",
                "action@nammachennaiclean.org",
                "+91 98401 55210",
                "sailesh",
                84,
                1,
                now_str,
            ),
            (
                "T. Nagar Green Resident Welfare Association",
                "Resident Welfare Association (RWA)",
                "Proactive neighbourhood citizen committee coordinating with GCC conservancy inspectors to enforce door-to-door source segregation and rapid spot remediation.",
                "Chennai",
                "T. Nagar, West Mambalam, Panagal Park",
                "Commercial Waste, Overflowing Bins, Construction Debris",
                "tnagar.rwa.civic@gmail.com",
                "+91 98842 33100",
                "rakesh",
                45,
                1,
                now_str,
            ),
            (
                "Exnora International Coastal Action Wing",
                "NGO / Non-Profit",
                "Veteran civic society organization operating community compost centers, mangrove cleanup brigades, and hazardous waste containerization across Chennai's coastal belt.",
                "Chennai",
                "Thiruvanmiyur, ECR, Besant Nagar, Ennore",
                "Hazardous / Medical Waste, Coastal Dumps, Plastic Waste",
                "volunteer@exnoracoastal.in",
                "+91 94441 99400",
                "thishanth",
                210,
                1,
                now_str,
            ),
        ]
        cursor.executemany(
            """
            INSERT INTO volunteer_groups (
                name, category, description, city, areas_served, focus_areas,
                contact_email, contact_phone, leader_username, member_count, is_verified, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seed_groups,
        )

        seed_drives = [
            (
                2,
                None,
                "Marina Coastal Fringe & Promenade Deep Cleanup",
                "Marina Beach Promenade (Behind Light House)",
                "2026-11-21",
                "06:30 AM",
                "Targeting microplastics, styrofoam packing waste, and tourist debris along the high-tide line before morning crowds arrive. Gloves, waste gunny bags, and safety hand sanitizers provided.",
                35,
                "Scheduled",
                "sailesh",
                now_str,
            ),
            (
                3,
                None,
                "South Usman Road Commercial Corridor Night Audit & Clearance",
                "Under Usman Road Flyover, T. Nagar",
                "2026-11-22",
                "07:00 AM",
                "Remediation of unattended packing crates, wet organic carton accumulations, and vendor waste overflowing near storm-water drains.",
                20,
                "Scheduled",
                "rakesh",
                now_str,
            ),
        ]
        cursor.executemany(
            """
            INSERT INTO cleanup_drives (
                group_id, hotspot_report_id, title, location_name, target_date, target_time,
                description, required_volunteers, status, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seed_drives,
        )
    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# VOLUNTEER NETWORK BACKEND LAYER
# --------------------------------------------------------------------------
def fetch_volunteer_groups(city_filter=None, category_filter=None, search_term=None):
    """Fetches registered volunteer organizations with optional filtering."""
    conn = get_connection()
    query = "SELECT * FROM volunteer_groups WHERE 1=1"
    params = []
    if city_filter and city_filter != "All Cities":
        query += " AND city = ?"
        params.append(city_filter)
    if category_filter and category_filter != "All Categories":
        query += " AND category = ?"
        params.append(category_filter)
    if search_term and search_term.strip():
        term = f"%{search_term.strip().lower()}%"
        query += " AND (LOWER(name) LIKE ? OR LOWER(areas_served) LIKE ? OR LOWER(focus_areas) LIKE ? OR LOWER(description) LIKE ?)"
        params.extend([term, term, term, term])
    query += " ORDER BY is_verified DESC, member_count DESC, id ASC"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def fetch_volunteer_group_by_id(group_id):
    """Returns a single volunteer group row as a dict or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM volunteer_groups WHERE id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    cols = [col[0] for col in cursor.description]
    conn.close()
    return dict(zip(cols, row))


def create_volunteer_group(name, category, description, city, areas_served, focus_areas, contact_email, contact_phone, leader_username):
    """Registers a new volunteer group and adds the creator as leader and first member."""
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        INSERT INTO volunteer_groups (
            name, category, description, city, areas_served, focus_areas,
            contact_email, contact_phone, leader_username, member_count, is_verified, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
        """,
        (name, category, description, city, areas_served, focus_areas, contact_email, contact_phone, leader_username, now_str),
    )
    group_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO volunteer_memberships (group_id, username, joined_at) VALUES (?, ?, ?)",
        (group_id, leader_username, now_str),
    )
    conn.commit()
    conn.close()
    return group_id


def is_user_group_member(group_id, username):
    """Checks if a user is an active member of a volunteer group."""
    if not username:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM volunteer_memberships WHERE group_id = ? AND username = ?", (group_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def join_volunteer_group(group_id, username):
    """Adds a user to a volunteer group and increments member_count."""
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "INSERT INTO volunteer_memberships (group_id, username, joined_at) VALUES (?, ?, ?)",
            (group_id, username, now_str),
        )
        cursor.execute("UPDATE volunteer_groups SET member_count = member_count + 1 WHERE id = ?", (group_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def leave_volunteer_group(group_id, username):
    """Removes a user from a volunteer group and decrements member_count."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM volunteer_memberships WHERE group_id = ? AND username = ?", (group_id, username))
    if cursor.rowcount > 0:
        cursor.execute("UPDATE volunteer_groups SET member_count = MAX(0, member_count - 1) WHERE id = ?", (group_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def fetch_cleanup_drives(status_filter="All", group_id=None, hotspot_id=None):
    """Fetches cleanup drives joined with group name and RSVP count."""
    conn = get_connection()
    query = """
        SELECT d.*, g.name AS group_name, g.category AS group_category,
               COALESCE(r.rsvp_count, 0) AS current_rsvps
        FROM cleanup_drives d
        LEFT JOIN volunteer_groups g ON d.group_id = g.id
        LEFT JOIN (
            SELECT drive_id, COUNT(*) AS rsvp_count
            FROM drive_rsvps
            GROUP BY drive_id
        ) r ON d.id = r.drive_id
        WHERE 1=1
    """
    params = []
    if status_filter and status_filter != "All":
        query += " AND d.status = ?"
        params.append(status_filter)
    if group_id:
        query += " AND d.group_id = ?"
        params.append(group_id)
    if hotspot_id:
        query += " AND d.hotspot_report_id = ?"
        params.append(hotspot_id)
    query += " ORDER BY CASE WHEN d.status = 'Scheduled' THEN 0 WHEN d.status = 'In Progress' THEN 1 ELSE 2 END, d.target_date ASC, d.id DESC"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def create_cleanup_drive(group_id, hotspot_report_id, title, location_name, target_date, target_time, description, required_volunteers, created_by):
    """Creates a new cleanup drive and automatically registers the organizer as first RSVP."""
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        INSERT INTO cleanup_drives (
            group_id, hotspot_report_id, title, location_name, target_date, target_time,
            description, required_volunteers, status, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Scheduled', ?, ?)
        """,
        (group_id, hotspot_report_id, title, location_name, target_date, target_time, description, required_volunteers, created_by, now_str),
    )
    drive_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO drive_rsvps (drive_id, username, timestamp) VALUES (?, ?, ?)",
        (drive_id, created_by, now_str),
    )
    conn.commit()
    conn.close()
    return drive_id


def is_user_drive_rsvpd(drive_id, username):
    """Checks if a user has RSVP'd to a cleanup drive."""
    if not username:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM drive_rsvps WHERE drive_id = ? AND username = ?", (drive_id, username))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def rsvp_cleanup_drive(drive_id, username):
    """Registers an RSVP for a volunteer on a cleanup drive."""
    conn = get_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "INSERT INTO drive_rsvps (drive_id, username, timestamp) VALUES (?, ?, ?)",
            (drive_id, username, now_str),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def cancel_rsvp_cleanup_drive(drive_id, username):
    """Cancels an RSVP for a cleanup drive."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM drive_rsvps WHERE drive_id = ? AND username = ?", (drive_id, username))
    conn.commit()
    conn.close()


def complete_cleanup_drive(drive_id, fixed_photo_path, bags_cleared, completed_by):
    """
    Marks a cleanup drive as Completed, updates verified photo & bags collected,
    marks any linked hotspot as Fixed/Cleaned in garbage_reports,
    and awards civic points (+50 to drive organizer, +25 to all RSVP'd volunteers).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT hotspot_report_id, created_by FROM cleanup_drives WHERE id = ?", (drive_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    hotspot_id, creator = row[0], row[1]

    cursor.execute(
        """
        UPDATE cleanup_drives
        SET status = 'Completed', fixed_photo_path = ?, bags_cleared = ?
        WHERE id = ?
        """,
        (fixed_photo_path, bags_cleared, drive_id),
    )

    if hotspot_id:
        cursor.execute(
            "UPDATE garbage_reports SET status = 'Fixed', rating = 5, fixed_photo_path = ? WHERE id = ?",
            (fixed_photo_path, hotspot_id),
        )

    if creator:
        cursor.execute("UPDATE users SET civic_score = civic_score + 50 WHERE username = ?", (creator,))

    cursor.execute("SELECT username FROM drive_rsvps WHERE drive_id = ? AND username != ?", (drive_id, creator or ""))
    participants = [r[0] for r in cursor.fetchall()]
    for p in participants:
        cursor.execute("UPDATE users SET civic_score = civic_score + 25 WHERE username = ?", (p,))

    conn.commit()
    conn.close()
    return True


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

# --------------------------------------------------------------------------
# SIDEBAR - CITIZEN PROFILE & REPORTING ENGINE
# --------------------------------------------------------------------------
if st.session_state.current_user is None:
    st.sidebar.markdown(
        """
        <div style="background: #F6F6F0; border: 1px solid #E2E5DE; border-radius: 6px; padding: 12px 14px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <b style="font-size: 13.5px; color: #183B2A;">Guest Mode</b>
                <span style="font-size: 10px; font-weight: 700; color: #68736B; text-transform: uppercase;">Observer</span>
            </div>
            <div style="font-size: 12px; color: #68736B;">Sign in to submit incident reports, vote on hotspots, and earn Civic Score points.</div>
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
        <div style="background: #F6F6F0; border: 1px solid #E2E5DE; border-radius: 6px; padding: 12px 14px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <b style="font-size: 14px; color: #183B2A;">@{st.session_state.current_user}</b>
                <span style="font-size: 11px; font-weight: 700; color: #267348; background: #E7EDE4; padding: 2px 6px; border-radius: 4px;">{civic_badge(score)}</span>
            </div>
            <div style="font-size: 12px; color: #68736B;">Civic Score: <b style="color: #183B2A;">{score} pts</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Log out", key="sidebar_logout_btn", use_container_width=True):
        st.session_state.current_user = None
        st.session_state.guest_mode = False
        st.rerun()

st.sidebar.markdown("---")

# Location targeting
st.sidebar.markdown("### Location Target")

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
    st.sidebar.caption("No location selected - click map or search below.")

with st.sidebar.expander("Target Location via GPS or Search"):
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

# Reporting form
st.sidebar.markdown("### Report Waste Incident")

if st.session_state.current_user is None:
    st.sidebar.info("Sign in above to submit an incident report.")
else:
    st.sidebar.caption("Safety notice: Do not touch hazardous or sharp waste. Report from a safe distance.")
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
        st.sidebar.markdown("**Hardware Node: CLEANSPOT-CAM-01**")
        cam_endpoint = st.sidebar.text_input(
            "Node Endpoint",
            value=st.session_state.get("iot_cam_url", "http://192.168.0.108/capture"),
            key="iot_cam_url_input",
            help="Live URL of the ESP32-CAM node on local Wi-Fi",
        )
        st.session_state["iot_cam_url"] = cam_endpoint
        if st.sidebar.button("Trigger & Fetch from IoT Node", key="btn_trigger_iot", use_container_width=True):
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
                            st.sidebar.success("Captured frame from ESP32-CAM (GPS: 13.0827, 80.2707)")
            except Exception as e:
                st.sidebar.error(f"Could not connect to IoT node: {e}")

        if st.session_state.get("iot_photo_bytes"):
            st.sidebar.image(
                st.session_state.iot_photo_bytes,
                caption="Live Frame: CLEANSPOT-CAM-01",
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
                        f"Non-waste image: {result.get('explanation', 'no reason given')}. Please verify before submitting."
                    )
                    st.session_state.ai_category = None
                    st.session_state.ai_rating = None
                    st.session_state.ai_explanation = None
                else:
                    if result.get("looks_ai_generated"):
                        st.sidebar.warning("Note: Photo shows signs of synthetic generation.")
                    st.session_state.ai_category = result.get("category")
                    st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                    st.session_state.ai_explanation = result.get("explanation")
                    st.sidebar.success(f"Verified: {result.get('category')} ({result.get('severity')})")

    if uploaded_photo is not None:
        if st.sidebar.button("Verify & Analyze with AI", key="btn_verify_upload", use_container_width=True):
            photo_bytes = uploaded_photo.getvalue()
            mime_type = uploaded_photo.type or "image/jpeg"
            result, ai_error = analyze_garbage_photo(photo_bytes, mime_type)
            if ai_error:
                st.sidebar.warning(ai_error)
            elif not result.get("is_garbage_issue"):
                st.sidebar.warning(
                    f"Non-waste image: {result.get('explanation', 'no reason given')}. Please verify before submitting."
                )
                st.session_state.ai_category = None
                st.session_state.ai_rating = None
                st.session_state.ai_explanation = None
            else:
                if result.get("looks_ai_generated"):
                    st.sidebar.warning("Note: Photo shows signs of synthetic generation.")
                st.session_state.ai_category = result.get("category")
                st.session_state.ai_rating = SEVERITY_TO_RATING.get(result.get("severity"), 3)
                st.session_state.ai_explanation = result.get("explanation")
                st.sidebar.success(f"Verified: {result.get('category')} ({result.get('severity')})")
            st.session_state.pending_photo_bytes = photo_bytes

        if st.session_state.get("ai_explanation"):
            st.sidebar.caption(f"AI Verdict: {st.session_state.ai_explanation}")

    default_category_index = (
        CATEGORIES.index(st.session_state.get("ai_category"))
        if st.session_state.get("ai_category") in CATEGORIES else 0
    )
    default_rating = st.session_state.get("ai_rating") or 3

    with st.sidebar.form("review_form", clear_on_submit=True):
        st.caption("Coordinates are captured automatically from your map target selection.")
        category = st.selectbox("Category", CATEGORIES, index=default_category_index)
        rating = st.slider("Cleanliness (1 = severe/filthy, 5 = clean)", 1, 5, default_rating)
        description = st.text_area("Description", placeholder="Describe exact location, landmarks, and waste type...")
        submitted = st.form_submit_button("Submit Incident Report")

        if submitted:
            has_point = st.session_state.clicked_lat is not None
            has_segment = bool(st.session_state.segment_coords)
            cooldown_elapsed = get_seconds_since_last_submission(st.session_state.current_user)
            in_cooldown = cooldown_elapsed is not None and cooldown_elapsed < SUBMISSION_COOLDOWN_SECONDS

            if not has_point and not has_segment:
                st.error("Select a target location on the map, draw a segment, or use GPS first.")
            elif in_cooldown:
                wait_seconds = int(SUBMISSION_COOLDOWN_SECONDS - cooldown_elapsed)
                st.error(f"Anti-spam protection: please wait {wait_seconds}s before submitting again.")
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
                        f"Duplicate prevention: A '{category}' report already exists within 40m "
                        f"('{duplicate['location_name']}'). Please upvote that report in the Community tab."
                    )
                else:
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
                    st.success("Incident report successfully logged. (+10 Civic Score)")

                    for key in ("clicked_lat", "clicked_lon", "segment_coords", "_last_picker_raw", "_last_geo_raw"):
                        st.session_state[key] = defaults[key]
                    for key in ("ai_category", "ai_rating", "ai_explanation", "pending_photo_bytes"):
                        st.session_state[key] = None
                    st.rerun()

# --------------------------------------------------------------------------
# MAIN APPLICATION WORKSPACE
# --------------------------------------------------------------------------
st.markdown(
    """
    <div class="editorial-hero">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <div class="editorial-tag">CIVIC OPERATIONS & ENVIRONMENTAL INTELLIGENCE</div>
            <div style="font-size: 11px; font-weight: 600; color: #267348; background: #E7EDE4; padding: 2px 8px; border-radius: 4px;">SYSTEM OPERATIONAL · NODE ACTIVE</div>
        </div>
        <h1 class="editorial-title">Making every public space accountable.</h1>
        <p class="editorial-desc">
            From community-reported waste hotspots to AI-powered verification and intelligent edge monitoring, Civic-Bin helps communities turn neglected spaces into cleaner, accountable environments.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not GOOGLE_MAPS_API_KEY:
    st.warning(
        "Google Maps JavaScript API key not detected. Set the GOOGLE_MAPS_API_KEY environment variable to view interactive satellite maps and municipal heatmaps. Coordinate targeting and offline incident logging remain operational."
    )

tab_overview, tab_map, tab_reviews, tab_volunteer, tab_news, tab_sentinel = st.tabs([
    "Overview",
    "Geospatial Operations",
    "Community & Grievance SLA",
    "Volunteer Network",
    "Sanitation News",
    "IoT Edge Sentinel",
])

# ----- TAB 1: OVERVIEW -----------------------------------------------------
with tab_overview:
    all_reviews_ov = fetch_all_reviews()
    total_reps = len(all_reviews_ov)
    open_reps = (all_reviews_ov["status"] == "Open").sum() if not all_reviews_ov.empty else 0
    fixed_reps = (all_reviews_ov["status"] == "Fixed").sum() if not all_reviews_ov.empty else 0
    severe_open_reps = ((all_reviews_ov["rating"] <= 2) & (all_reviews_ov["status"] == "Open")).sum() if not all_reviews_ov.empty else 0
    avg_cleanliness = f"{all_reviews_ov['rating'].mean():.1f} / 5" if not all_reviews_ov.empty else "5.0 / 5"
    res_rate = f"{(fixed_reps / total_reps * 100):.0f}%" if total_reps > 0 else "0%"

    # Responsive, non-truncating KPI Tile Grid
    st.markdown(
        f"""
        <div class="kpi-row">
            <div class="kpi-tile">
                <div class="kpi-tile-label">Total Incidents</div>
                <div class="kpi-tile-val">{total_reps}</div>
                <div class="kpi-tile-sub">Logged in registry</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Open Hotspots</div>
                <div class="kpi-tile-val warning">{severe_open_reps}</div>
                <div class="kpi-tile-sub">Severity 1-2 stars</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Resolved Spots</div>
                <div class="kpi-tile-val success">{fixed_reps}</div>
                <div class="kpi-tile-sub">Verified cleanups</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Resolution Rate</div>
                <div class="kpi-tile-val">{res_rate}</div>
                <div class="kpi-tile-sub">Community resolution</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Cleanliness Index</div>
                <div class="kpi-tile-val">{avg_cleanliness}</div>
                <div class="kpi-tile-sub">District average</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Active Sentinels</div>
                <div class="kpi-tile-val accent">1 Online</div>
                <div class="kpi-tile-sub">CLEANSPOT-CAM-01</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_arch, col_ops = st.columns([1.1, 1], gap="medium")

    with col_arch:
        with st.container(border=True):
            st.markdown("#### Platform Architecture & Operational Loop")
            st.markdown(
                """
                Civic-Bin integrates edge hardware, machine intelligence, and municipal routing into an unbroken accountability cycle:
                """
            )
            st.markdown(
                """
                1. **Optical Sourcing & Surveillance:** Citizens photograph street waste via mobile/web capture, or the autonomous ESP32-CAM optical matrix records unattended dumping blind spots over local Wi-Fi.
                2. **Multimodal AI Vision Triage:** Google Gemini Flash Vision analyzes scene imagery, validates waste presence, assigns categories, estimates severity (1–5), and screens synthetic images.
                3. **Grievance Routing & 7-Day SLA:** Synthesizes structured 400-character complaint text aligned with Greater Chennai Corporation (GCC) PGR standards, initiating a public resolution countdown.
                4. **Community Proof & Civic Score:** Neighbors verify cleanups with before-and-after photographic evidence, discuss corrective measures, and earn civic points.
                """
            )

    with col_ops:
        with st.container(border=True):
            st.markdown("#### Real-time Category Distribution")
            if not all_reviews_ov.empty and "category" in all_reviews_ov.columns:
                cat_counts = all_reviews_ov["category"].value_counts().reset_index()
                cat_counts.columns = ["Category", "Count"]
                st.bar_chart(cat_counts.set_index("Category"), color="#183B2A", height=230)
            else:
                st.info("No incident records logged in the database yet.")

    # High-Priority Hotspots & Institutional Footer
    st.markdown("#### High-Priority Hotspots Under Active SLA")
    if not all_reviews_ov.empty:
        severe_df = all_reviews_ov[(all_reviews_ov["rating"] <= 2) & (all_reviews_ov["status"] == "Open")]
        if not severe_df.empty:
            for _, s_row in severe_df.head(3).iterrows():
                rep_dt = datetime.strptime(s_row["timestamp"], "%Y-%m-%d %H:%M:%S")
                d_elapsed = (datetime.now() - rep_dt).days
                days_left = max(0, SLA_DAYS - d_elapsed)
                with st.container(border=True):
                    col_t1, col_t2 = st.columns([3, 1])
                    with col_t1:
                        st.markdown(f"**Ticket #{s_row['id']} — {s_row['category']}** · Rating: `{int(s_row['rating'])}/5`")
                        st.caption(f"Location: {s_row['location_name']} · Reported by: @{s_row['username']}")
                        st.write(s_row['description'])
                    with col_t2:
                        if d_elapsed > SLA_DAYS:
                            st.error(f"SLA Breached ({d_elapsed - SLA_DAYS}d overdue)")
                        else:
                            st.info(f"{days_left}d remaining in SLA")
        else:
            st.success("No active severe hotspots. All priority incidents are currently resolved.")
    else:
        st.caption("No reports registered yet.")

    st.markdown(
        """
        <div style="background: #FFFFFF; border: 1px solid #E2E5DE; border-radius: 6px; padding: 14px 18px; margin-top: 24px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div style="font-size: 12.5px; color: #68736B;">
                    <b>Design Thinking Review Project</b> &nbsp;|&nbsp; 
                    Faculty Head: <b>Mr. Shakthivel</b> &nbsp;|&nbsp; 
                    Team: Sailesh M.V, Rakesh JP, Thishanth T, Tharun B
                </div>
                <div style="font-size: 11.5px; color: #68736B;">
                    Municipal Gateway: GCC PGR &nbsp;·&nbsp; Edge Node: 192.168.0.108
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ----- TAB 2: GEOSPATIAL OPERATIONS ----------------------------------------
with tab_map:
    reviews_df = fetch_all_reviews()

    st.markdown("### Geospatial Incident Operations")
    st.caption("Interactive incident map, municipal heatmaps, and boundary outline tools.")

    view_choice = st.radio(
        "View mode", ["Interactive Incident Map", "Municipal Heatmap (Open vs. Cleaned)"],
        horizontal=True, label_visibility="collapsed",
    )
    is_heatmap_view = view_choice.startswith("Municipal Heatmap")

    col_map, col_stats = st.columns([3, 1], gap="medium")

    with col_stats:
        with st.container(border=True):
            st.markdown("##### District Summary")
            st.metric("Total Incident Markers", len(reviews_df))
            if not reviews_df.empty:
                st.metric("Cleanliness Index", f"{reviews_df['rating'].mean():.1f} / 5")
                open_severe = ((reviews_df["rating"] <= 2) & (reviews_df["status"] == "Open")).sum()
                st.metric("Priority Hotspots", int(open_severe))

            st.markdown("---")
            st.markdown("##### Status Legend")
            st.markdown(
                """
                • <span style="color:#B54740; font-weight:600;">Severe / Critical</span> (1-2★)  
                • <span style="color:#A66B17; font-weight:600;">Moderate Waste</span> (3★)  
                • <span style="color:#267348; font-weight:600;">Clean / Resolved</span> (4-5★)
                """,
                unsafe_allow_html=True,
            )

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
            with st.container(border=True):
                st.markdown("##### Offline Map Workspace")
                st.caption(
                    "Google Maps API key not configured. Satellite map rendering is paused. Coordinate targeting, reverse geocoding via OpenStreetMap, and database incident logging remain fully active."
                )

# ----- TAB 3: COMMUNITY & GRIEVANCE SLA ------------------------------------
with tab_reviews:
    st.markdown("### Community Incident Registry & Grievance SLA")
    st.caption("Citizen-reported incidents, official municipal complaint pre-fill, SLA clocks, and clean-up verification.")

    all_reviews = fetch_all_reviews()

    if all_reviews.empty:
        st.info("No incident reports recorded in the registry yet. Submit a report from the sidebar.")
    else:
        for _, row in all_reviews.iterrows():
            review_id = int(row["id"])
            status_badge = "CLEANED" if row["status"] == "Fixed" else "OPEN HOTSPOT"
            badge_class = "badge-fixed" if row["status"] == "Fixed" else "badge-open"
            stars = "★" * int(row["rating"]) + "☆" * (5 - int(row["rating"]))
            ticket_id = f"GR-{review_id:04d}"

            with st.expander(f"{ticket_id} · [{status_badge}] · {row['location_name']} · ({stars})"):
                st.caption(f"Category: {row['category']} · Reported by: @{row['username']} · Timestamp: {row['timestamp']}")
                st.write(safe_str(row["description"], "_No description provided._"))

                if row["status"] == "Open" and int(row["rating"]) <= 2:
                    reported_dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    days_elapsed = (datetime.now() - reported_dt).days
                    if days_elapsed > SLA_DAYS:
                        st.error(f"SLA Breached: Overdue by {days_elapsed - SLA_DAYS} days past the public {SLA_DAYS}-day clean-up limit.")
                    else:
                        st.info(f"{SLA_DAYS - days_elapsed} day(s) remaining in the public {SLA_DAYS}-day resolution window.")

                # Municipal Grievance Routing
                if row["status"] == "Open":
                    with st.expander("File with Municipal Office (Greater Chennai Corporation)"):
                        municipality_info, is_known_portal = get_municipality_info(row)
                        st.write(f"**Target Authority:** {municipality_info['name']}")

                        civic = build_civic_complaint(row, municipality_info, is_known_portal)
                        st.write(f"**Group:** {civic['group']} &nbsp;|&nbsp; **Type:** {civic['type']}")
                        st.code(civic["title"], language=None)

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
                        st.write(f"**Details of Complaint ({len(details_to_show)}/{GCC_DETAILS_MAX_CHARS} chars):**")
                        st.code(details_to_show, language=None)

                        if row["photo_path"] and isinstance(row["photo_path"], str):
                            st.caption("Attached Incident Photograph:")
                            safe_image(row["photo_path"], width=200)

                        button_label = "Open GCC Complaint Portal ↗" if is_known_portal else "Search Local Municipal Portal ↗"
                        st.link_button(button_label, municipality_info["url"])

                # Community Volunteer Remediation Mobilization
                if row["status"] == "Open":
                    with st.expander("🤝 Mobilize Volunteer Community Cleanup"):
                        st.markdown(f"**Mobilize community action for Hotspot `{ticket_id}`**")
                        st.caption("Coordinate local volunteer intervention with registered NGOs, student NSS cells, or citizen squads.")
                        linked_drives = fetch_cleanup_drives(hotspot_id=review_id)
                        if not linked_drives.empty:
                            st.markdown(f"**Scheduled Cleanup Drives for this Spot ({len(linked_drives)}):**")
                            for _, drow in linked_drives.iterrows():
                                st.info(
                                    f"🗓️ **{drow['title']}** — Scheduled on {drow['target_date']} at {drow['target_time']}\n\n"
                                    f"Host: {drow['group_name'] or 'Independent Squad'} · RSVPs: {drow['current_rsvps']} / {drow['required_volunteers']} volunteers"
                                )
                        else:
                            st.caption("No volunteer drives scheduled for this spot yet.")

                        if st.session_state.current_user is None:
                            st.caption("Sign in to organize a volunteer cleanup drive for this hotspot.")
                        else:
                            with st.form(f"mobilize_drive_form_{review_id}"):
                                st.write("Schedule a New Volunteer Drive for this Spot:")
                                m_title = st.text_input("Drive Title", value=f"Remediation: {row['location_name']}", key=f"m_title_{review_id}")
                                m_groups = fetch_volunteer_groups()
                                group_opts = ["Independent Community Squad"] + [f"{g['name']} (ID: {g['id']})" for _, g in m_groups.iterrows()]
                                m_chosen_group = st.selectbox("Affiliated Organization / Group", group_opts, key=f"m_grp_{review_id}")
                                m_col1, m_col2 = st.columns(2)
                                with m_col1:
                                    m_date = st.date_input("Target Date", key=f"m_date_{review_id}")
                                with m_col2:
                                    m_time = st.text_input("Target Time", value="07:00 AM", key=f"m_time_{review_id}")
                                m_vols = st.number_input("Target Volunteer Headcount", min_value=5, max_value=200, value=15, step=5, key=f"m_vols_{review_id}")
                                m_desc = st.text_area("Assembly Instructions & Safety Details", value=f"Meeting at {row['location_name']} to remediate {row['category'].lower()} accumulations. Gloves and disposal gunny sacks provided.", key=f"m_desc_{review_id}")
                                m_submit = st.form_submit_button("Launch Volunteer Cleanup Drive")
                                if m_submit:
                                    if not m_title.strip():
                                        st.error("Drive title is required.")
                                    else:
                                        sel_group_id = None
                                        if m_chosen_group != "Independent Community Squad":
                                            try:
                                                sel_group_id = int(m_chosen_group.split("ID: ")[1].rstrip(")"))
                                            except Exception:
                                                sel_group_id = None
                                        create_cleanup_drive(
                                            group_id=sel_group_id,
                                            hotspot_report_id=review_id,
                                            title=m_title.strip(),
                                            location_name=row["location_name"],
                                            target_date=str(m_date),
                                            target_time=m_time.strip(),
                                            description=m_desc.strip(),
                                            required_volunteers=int(m_vols),
                                            created_by=st.session_state.current_user,
                                        )
                                        add_civic_points(st.session_state.current_user, 10)
                                        st.success("Cleanup Drive launched! (+10 Civic Points). View in Volunteer Network tab.")
                                        st.rerun()

                col_up, _ = st.columns([1, 5])
                with col_up:
                    if st.session_state.current_user is None:
                        st.caption(f"▲ {row['upvote_count']} upvotes (sign in to vote)")
                    elif has_upvoted(review_id, st.session_state.current_user):
                        st.caption(f"▲ {row['upvote_count']} upvotes (you upvoted)")
                    else:
                        if st.button(f"▲ Upvote ({row['upvote_count']})", key=f"rev_upvote_{review_id}"):
                            if upvote_review(review_id, st.session_state.current_user):
                                add_civic_points(st.session_state.current_user, 1)
                            st.rerun()

                if row["status"] == "Open":
                    st.markdown("---")
                    st.markdown("**Verify & Mark as Cleaned**")
                    if isinstance(row["photo_path"], str):
                        safe_image(row["photo_path"], caption="Reported condition", width=240)
                    if st.session_state.current_user is None:
                        st.caption("Sign in to submit clean-up verification proof.")
                    else:
                        fixed_photo_method = st.radio(
                            "Proof photo method", ["Take a live photo", "Upload from device"],
                            horizontal=True, label_visibility="collapsed", key=f"fixed_photo_method_{review_id}",
                        )
                        if fixed_photo_method.startswith("Take a live"):
                            fixed_photo = st.camera_input("Take clean-up photo", key=f"fixed_photo_camera_{review_id}")
                        else:
                            fixed_photo = st.file_uploader("Upload clean-up photo", type=["jpg", "jpeg", "png"], key=f"fixed_photo_upload_{review_id}")
                        new_rating = st.slider("Verified cleanliness rating", 1, 5, 5, key=f"fixed_rating_{review_id}")
                        if st.button("Confirm Cleaned", key=f"confirm_fixed_{review_id}", type="primary"):
                            fixed_path = save_photo(fixed_photo.getvalue(), f"fixed_{review_id}") if fixed_photo else None
                            mark_review_fixed(review_id, new_rating, fixed_path)
                            add_civic_points(st.session_state.current_user, 15)
                            st.rerun()
                else:
                    st.success("This location has been verified and marked CLEANED by the community.")
                    col_b, col_a = st.columns(2)
                    with col_b:
                        if isinstance(row["photo_path"], str):
                            safe_image(row["photo_path"], caption="Before")
                    with col_a:
                        if isinstance(row["fixed_photo_path"], str):
                            safe_image(row["fixed_photo_path"], caption="After Clean-Up")

                st.markdown("---")
                st.markdown("**Community Discussion:**")
                replies = fetch_replies(review_id)
                if replies.empty:
                    st.caption("No replies posted yet.")
                else:
                    for _, reply in replies.iterrows():
                        st.markdown(f"**@{reply['username']}:** {reply['reply_text']}")
                        st.caption(f"Posted {reply['timestamp']}")

                if st.session_state.current_user is None:
                    st.caption("Sign in to post a reply.")
                else:
                    with st.form(f"reply_form_{review_id}", clear_on_submit=True):
                        reply_text = st.text_input("Add a comment", key=f"reply_input_{review_id}")
                        reply_submitted = st.form_submit_button("Post Reply")
                        if reply_submitted:
                            if reply_text.strip():
                                insert_reply(review_id, st.session_state.current_user, reply_text.strip())
                                add_civic_points(st.session_state.current_user, 2)
                                st.rerun()
                            else:
                                st.warning("Reply cannot be empty.")

# ----- TAB 4: VOLUNTEER NETWORK --------------------------------------------
with tab_volunteer:
    st.markdown(
        """
        <div class="editorial-hero">
            <div class="editorial-tag">CIVIC ACTION & VOLUNTEER RESPONSE NETWORK</div>
            <h2 class="editorial-title">Community Cleanup Squads & NGO Remediation Guilds</h2>
            <p class="editorial-desc">Bridging municipal capacity constraints by connecting unresolved civic waste hotspots with registered environmental NGOs, collegiate NSS units, resident welfare associations, and grassroots community cleanup squads.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Network Metrics
    all_v_groups = fetch_volunteer_groups()
    all_v_drives = fetch_cleanup_drives()
    total_groups = len(all_v_groups)
    total_volunteers = int(all_v_groups["member_count"].sum()) if not all_v_groups.empty else 0
    active_drives = len(all_v_drives[all_v_drives["status"] == "Scheduled"]) if not all_v_drives.empty else 0
    total_bags_cleared = int(all_v_drives["bags_cleared"].sum()) if not all_v_drives.empty else 0

    st.markdown(
        f"""
        <div class="kpi-row">
            <div class="kpi-tile">
                <div class="kpi-tile-label">Registered Guilds</div>
                <div class="kpi-tile-val">{total_groups}</div>
                <div class="kpi-tile-sub">NGOs, NSS cells & RWAs</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Active Volunteers</div>
                <div class="kpi-tile-val accent">{total_volunteers}</div>
                <div class="kpi-tile-sub">Enrolled community force</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Scheduled Drives</div>
                <div class="kpi-tile-val warning">{active_drives}</div>
                <div class="kpi-tile-sub">Action events pending</div>
            </div>
            <div class="kpi-tile">
                <div class="kpi-tile-label">Waste Remediated</div>
                <div class="kpi-tile-val success">{total_bags_cleared} Bags</div>
                <div class="kpi-tile-sub">Cleared from hotspots</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    vol_tab_orgs, vol_tab_drives, vol_tab_register = st.tabs([
        "Organization Directory",
        "Active Cleanup Drives",
        "Register a Volunteer Group",
    ])

    with vol_tab_orgs:
        col_s1, col_s2, col_s3 = st.columns([2, 1.5, 1.5])
        with col_s1:
            search_grp = st.text_input("Search Organizations", placeholder="Filter by name, locality, or keyword...", key="vol_search_input")
        with col_s2:
            cat_filter = st.selectbox(
                "Category",
                ["All Categories", "University / NSS Cell", "NGO / Non-Profit", "Youth Cleanup Squad", "Resident Welfare Association (RWA)", "Corporate CSR"],
                key="vol_cat_select",
            )
        with col_s3:
            city_filter = st.selectbox(
                "Operating City",
                ["All Cities", "Chennai", "Coimbatore", "Madurai", "Trichy", "Salem"],
                key="vol_city_select",
            )

        groups_df = fetch_volunteer_groups(
            city_filter=city_filter,
            category_filter=cat_filter,
            search_term=search_grp,
        )

        if groups_df.empty:
            st.info("No volunteer organizations matching the selected filters found. Be the first to register one!")
        else:
            st.caption(f"Showing {len(groups_df)} registered volunteer organizations.")
            for _, grp in groups_df.iterrows():
                grp_id = int(grp["id"])
                is_member = is_user_group_member(grp_id, st.session_state.current_user) if st.session_state.current_user else False
                verified_badge = '<span class="volunteer-card-badge">✓ Verified Partner</span>' if grp["is_verified"] else ""

                focus_tags = [f.strip() for f in (grp["focus_areas"] or "").split(",") if f.strip()]
                tags_html = "".join([f'<span class="volunteer-card-tag">{t}</span>' for t in focus_tags])

                with st.container(border=True):
                    c_info, c_action = st.columns([3.2, 1])
                    with c_info:
                        st.markdown(
                            f"""
                            <div class="volunteer-card-cat">{grp['category']} · {grp['city']}</div>
                            <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
                                <h3 class="volunteer-card-title">{grp['name']}</h3>
                                {verified_badge}
                            </div>
                            <div class="volunteer-card-desc">{safe_str(grp['description'], 'No description provided.')}</div>
                            <div style="margin-bottom:8px;">{tags_html}</div>
                            <div class="volunteer-card-meta">
                                <div class="volunteer-card-meta-item">📍 <b>Coverage:</b> {safe_str(grp['areas_served'], grp['city'])}</div>
                                <div class="volunteer-card-meta-item">👥 <b>Members:</b> {grp['member_count']}</div>
                                <div class="volunteer-card-meta-item">✉️ <b>Contact:</b> {safe_str(grp['contact_email'], 'N/A')}</div>
                                <div class="volunteer-card-meta-item">📞 <b>Phone:</b> {safe_str(grp['contact_phone'], 'N/A')}</div>
                                <div class="volunteer-card-meta-item">👤 <b>Leader:</b> @{grp['leader_username']}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    with c_action:
                        st.write("")
                        st.write("")
                        if st.session_state.current_user is None:
                            st.caption("Sign in to join this organization.")
                        elif is_member:
                            st.success("✓ Joined")
                            if st.button("Leave Group", key=f"leave_grp_{grp_id}", type="secondary", use_container_width=True):
                                leave_volunteer_group(grp_id, st.session_state.current_user)
                                st.rerun()
                        else:
                            if st.button("Join Group", key=f"join_grp_{grp_id}", type="primary", use_container_width=True):
                                join_volunteer_group(grp_id, st.session_state.current_user)
                                add_civic_points(st.session_state.current_user, 5)
                                st.rerun()

    with vol_tab_drives:
        d_col1, d_col2 = st.columns([3, 1])
        with d_col1:
            st.markdown("#### Scheduled Community Waste Remediation Drives")
            st.caption("Participate in scheduled volunteer cleanups, earn civic score points, and restore degraded public spaces.")
        with d_col2:
            drive_status_sel = st.selectbox("Filter Status", ["All", "Scheduled", "Completed"], key="drive_status_select")

        drives_df = fetch_cleanup_drives(status_filter=drive_status_sel)

        if drives_df.empty:
            st.info("No cleanup drives matching this filter. Schedule a drive from the registry or register a new one!")
        else:
            for _, d_row in drives_df.iterrows():
                drive_id = int(d_row["id"])
                is_rsvpd = is_user_drive_rsvpd(drive_id, st.session_state.current_user) if st.session_state.current_user else False
                status_class = "drive-status-completed" if d_row["status"] == "Completed" else "drive-status-scheduled"
                host_group = d_row["group_name"] or "Independent Community Action Squad"

                with st.container(border=True):
                    d_top_left, d_top_right = st.columns([3, 1])
                    with d_top_left:
                        st.markdown(
                            f"""
                            <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">
                                <span class="{status_class}">{d_row['status']}</span>
                                <span style="font-size:12px; color:#68736B; font-weight:600;">Hosted by {host_group}</span>
                            </div>
                            <h3 style="font-size:17px; font-weight:700; color:#183B2A; margin:0 0 6px 0;">{d_row['title']}</h3>
                            """,
                            unsafe_allow_html=True,
                        )

                        if d_row["hotspot_report_id"]:
                            hotspot_num = int(d_row["hotspot_report_id"])
                            st.markdown(
                                f"""
                                <div class="drive-hotspot-link">
                                    🎯 Linked Hotspot Ticket: <b>GR-{hotspot_num:04d}</b> ({d_row['location_name']})
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )

                        st.markdown(
                            f"""
                            <div style="font-size:13.5px; color:#3F4741; margin-bottom:12px;">{safe_str(d_row['description'], 'No details specified.')}</div>
                            <div class="volunteer-card-meta">
                                <div class="volunteer-card-meta-item">🗓️ <b>Date:</b> {d_row['target_date']}</div>
                                <div class="volunteer-card-meta-item">⏰ <b>Time:</b> {d_row['target_time']}</div>
                                <div class="volunteer-card-meta-item">📍 <b>Meeting Spot:</b> {d_row['location_name']}</div>
                                <div class="volunteer-card-meta-item">👤 <b>Organizer:</b> @{d_row['created_by']}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                    with d_top_right:
                        req_vols = max(1, int(d_row["required_volunteers"]))
                        cur_vols = int(d_row["current_rsvps"])
                        pct = min(1.0, cur_vols / req_vols)

                        st.markdown(
                            f"""
                            <div class="drive-stat-pill">
                                <div class="drive-stat-num">{cur_vols} / {req_vols}</div>
                                <div class="drive-stat-lbl">Volunteers RSVP'd</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        st.progress(pct)

                        if d_row["status"] == "Scheduled":
                            if st.session_state.current_user is None:
                                st.caption("Sign in to RSVP.")
                            elif is_rsvpd:
                                st.success("✓ You are RSVP'd")
                                if st.button("Cancel RSVP", key=f"cancel_rsvp_{drive_id}", type="secondary", use_container_width=True):
                                    cancel_rsvp_cleanup_drive(drive_id, st.session_state.current_user)
                                    st.rerun()
                            else:
                                if st.button("RSVP to Participate", key=f"rsvp_{drive_id}", type="primary", use_container_width=True):
                                    rsvp_cleanup_drive(drive_id, st.session_state.current_user)
                                    add_civic_points(st.session_state.current_user, 5)
                                    st.rerun()
                        elif d_row["status"] == "Completed":
                            st.markdown(
                                f"""
                                <div style="background:#E7EDE4; color:#267348; border:1px solid #C3D5BF; border-radius:6px; padding:6px 10px; font-size:12px; font-weight:700; text-align:center; margin-top:8px;">
                                    ✓ Remediated: {d_row['bags_cleared']} Bags
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )

                    if d_row["status"] == "Scheduled" and st.session_state.current_user is not None:
                        with st.expander("📸 Mark Drive Completed & Verify Remediation"):
                            st.caption("Upload after-action proof of cleaned area and record the volume of waste bags collected to grant civic score rewards.")
                            comp_photo = st.file_uploader("Upload Verification Photo", type=["jpg", "jpeg", "png"], key=f"drive_comp_photo_{drive_id}")
                            comp_bags = st.number_input("Number of Waste Bags Remediated", min_value=1, max_value=500, value=10, step=1, key=f"drive_comp_bags_{drive_id}")

                            if st.button("Confirm Resolution & Award Civic Points", key=f"btn_comp_drive_{drive_id}", type="primary"):
                                comp_path = None
                                if comp_photo:
                                    comp_path = save_photo(comp_photo.getvalue(), f"drive_{drive_id}")
                                complete_cleanup_drive(drive_id, comp_path, int(comp_bags), st.session_state.current_user)
                                st.success("Drive marked Completed! +50 points awarded to organizer, +25 points to all RSVP'd volunteers.")
                                st.rerun()

                    elif d_row["status"] == "Completed" and d_row["fixed_photo_path"]:
                        st.caption("Remediation Proof Photograph:")
                        safe_image(d_row["fixed_photo_path"], caption=f"Cleared: {d_row['bags_cleared']} bags removed", width=260)

    with vol_tab_register:
        st.markdown("#### Register a Volunteer Organization or Squad")
        st.caption("Establish a collegiate environmental NSS cell, registered NGO profile, Resident Welfare Association (RWA) clean squad, or youth civic action group.")

        if st.session_state.current_user is None:
            st.warning("Please sign in or create an account to register a volunteer organization.")
        else:
            with st.form("register_volunteer_group_form"):
                col_g1, col_g2 = st.columns(2)
                with col_g1:
                    reg_name = st.text_input("Organization / Group Name*", placeholder="e.g. Loyola NSS Eco-Brigade")
                    reg_cat = st.selectbox(
                        "Category*",
                        ["University / NSS Cell", "NGO / Non-Profit", "Youth Cleanup Squad", "Resident Welfare Association (RWA)", "Corporate CSR"],
                    )
                    reg_city = st.text_input("Operating City / District*", value="Chennai")
                    reg_areas = st.text_input("Served Localities / Pincodes*", placeholder="e.g. Nungambakkam, Chetpet, Kodambakkam")
                with col_g2:
                    reg_focus = st.multiselect(
                        "Primary Focus Areas*",
                        ["Illegal Dumping", "Overflowing Bins", "Plastic & Microplastics", "Coastal & Waterways", "Construction Debris", "Bulk Waste", "Hazardous / Medical Waste"],
                        default=["Illegal Dumping", "Overflowing Bins"],
                    )
                    reg_email = st.text_input("Coordinator Email*", placeholder="contact@ecobrigade.org")
                    reg_phone = st.text_input("Coordinator Phone*", placeholder="+91 98400 00000")

                reg_desc = st.text_area(
                    "Mission & Description*",
                    placeholder="Describe your organization's background, volunteer membership, regular clean-up schedules, and target areas...",
                )

                reg_submit = st.form_submit_button("Register Organization")
                if reg_submit:
                    if not reg_name.strip() or not reg_city.strip() or not reg_desc.strip():
                        st.error("Please fill in all required fields (Name, City, and Mission Description).")
                    else:
                        focus_str = ", ".join(reg_focus) if reg_focus else "General Sanitation"
                        new_gid = create_volunteer_group(
                            name=reg_name.strip(),
                            category=reg_cat,
                            description=reg_desc.strip(),
                            city=reg_city.strip(),
                            areas_served=reg_areas.strip(),
                            focus_areas=focus_str,
                            contact_email=reg_email.strip(),
                            contact_phone=reg_phone.strip(),
                            leader_username=st.session_state.current_user,
                        )
                        add_civic_points(st.session_state.current_user, 20)
                        st.success(f"Successfully registered '{reg_name.strip()}'! (+20 Civic Points). You are registered as the founding group leader.")
                        st.rerun()


# ----- TAB 5: SANITATION NEWS ----------------------------------------------
with tab_news:
    st.markdown("### Environmental & Sanitation Dispatch")
    st.caption("Public coverage on municipal waste management, illegal dumping, and civic public health via Google News RSS.")

    news_query = st.text_input("Search topic", value="Chennai garbage dumping OR waste management", key="news_search_query")
    if st.button("Fetch Latest Coverage", type="primary"):
        fetched = fetch_news_rss(news_query.strip())
        if fetched:
            store_news_items(fetched, news_query.strip())
            st.session_state.news_page = 1
            st.success(f"Retrieved {len(fetched)} articles.")
        else:
            st.warning("No articles found for that topic.")
        st.rerun()

    if "news_page" not in st.session_state:
        st.session_state.news_page = 1
    if "_last_shown_news_query" not in st.session_state:
        st.session_state._last_shown_news_query = None
    if st.session_state._last_shown_news_query != news_query.strip():
        st.session_state.news_page = 1
        st.session_state._last_shown_news_query = news_query.strip()

    PAGE_SIZE = 8
    all_news = fetch_news_for_query(news_query.strip())

    if all_news.empty:
        st.info("No articles currently loaded. Click 'Fetch Latest Coverage' above.")
    else:
        total_pages = max(1, (len(all_news) + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.news_page = min(st.session_state.news_page, total_pages)
        page = st.session_state.news_page

        start = (page - 1) * PAGE_SIZE
        page_items = all_news.iloc[start:start + PAGE_SIZE]

        for _, item in page_items.iterrows():
            news_id = int(item["id"])
            with st.container(border=True):
                st.markdown(f"**{item['title']}**")
                st.caption(f"{item['source']} · {item['published']}")
                st.link_button("Read Article ↗", item["link"])

                if st.session_state.current_user is None:
                    st.caption(f"▲ {item['upvote_count']} upvotes")
                elif has_upvoted_news(news_id, st.session_state.current_user):
                    st.caption(f"▲ {item['upvote_count']} upvotes (you upvoted)")
                else:
                    if st.button(f"▲ Upvote ({item['upvote_count']})", key=f"news_upvote_{news_id}"):
                        if upvote_news(news_id, st.session_state.current_user):
                            add_civic_points(st.session_state.current_user, 1)
                        st.rerun()

        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("Previous Page", disabled=(page <= 1)):
                st.session_state.news_page -= 1
                st.rerun()
        with col_info:
            st.markdown(f"<div style='text-align:center; padding-top:8px; font-size:12px; color:#68736B;'>Page {page} of {total_pages}</div>", unsafe_allow_html=True)
        with col_next:
            if st.button("Next Page", disabled=(page >= total_pages)):
                st.session_state.news_page += 1
                st.rerun()

# ----- TAB 6: IOT EDGE SENTINEL CONSOLE ------------------------------------
with tab_sentinel:
    st.markdown("### IoT Edge Sentinel Console · Node CLEANSPOT-CAM-01")
    st.caption("Autonomous blind-spot optical surveillance node, hardware spotlight, and automated municipal grievance triage.")

    col_cam, col_ai = st.columns([1, 1], gap="medium")
    sentinel_ip = st.session_state.get("iot_cam_url", "http://192.168.0.108/capture")

    with col_cam:
        with st.container(border=True):
            st.markdown("##### Live Node Optical Viewfinder")
            st.caption("Hardware: **AI-Thinker ESP32-CAM (OV3660 3MP)**")

            if st.session_state.get("sentinel_last_img") is not None:
                st.image(st.session_state.sentinel_last_img, caption="Live Frame Capture (CLEANSPOT-CAM-01)", use_container_width=True)
            else:
                st.info("No optical frame retrieved yet. Click below to grab live frame.")

            btn_c1, btn_c2 = st.columns(2)
            with btn_c1:
                if st.button("Grab Live Frame", use_container_width=True):
                    try:
                        import urllib.request
                        with st.spinner("Connecting to ESP32-CAM..."):
                            req = urllib.request.Request(sentinel_ip, headers={"User-Agent": "CleanSpot-Web/1.0"})
                            with urllib.request.urlopen(req, timeout=6) as response:
                                img_data = response.read()
                                if img_data:
                                    st.session_state.sentinel_last_img = img_data
                                    st.success("Frame retrieved successfully.")
                                    st.rerun()
                    except Exception as e:
                        st.error(f"Cannot reach camera node at {sentinel_ip}: {e}")
            with btn_c2:
                st.link_button("Open Node Server ↗", "http://192.168.0.108", use_container_width=True)

        with st.container(border=True):
            st.markdown("##### Edge Telemetry Stream")
            st.markdown(
                """
                • **Status:** Armed & Monitoring  
                • **Coordinates:** 13.0827° N, 80.2707° E (Chennai Central)  
                • **Optics:** OV3660 3MP Sensor · QVGA (320x240)  
                • **Memory Mode:** Internal DRAM (PSRAM Disabled)  
                • **Night Spotlight:** 1W White SMD Flash (GPIO 4)  
                • **Network Interface:** Local Wi-Fi HTTP Server (:80)
                """
            )

    with col_ai:
        with st.container(border=True):
            st.markdown("##### Autonomous AI Grievance Pipeline")
            st.caption("Evaluates captured frames, filters false alarms, and autonomously logs validated municipal grievances.")

            st.markdown(
                """
                **3-Stage Autonomous Triage Workflow:**  
                1. Hardware Spotlight & Optical Capture  
                2. Gemini Multimodal AI Vision Inspection  
                3. Automatic Grievance Registration in Civic-Bin DB & GCC Routing
                """
            )

            if st.button("Execute Autonomous Surveillance Audit", type="primary", use_container_width=True):
                try:
                    import urllib.request
                    import uuid
                    with st.status("Executing Autonomous Surveillance Audit...", expanded=True) as status_box:
                        st.write("Triggering hardware spotlight & capturing optical frame from ESP32-CAM...")
                        req = urllib.request.Request(sentinel_ip, headers={"User-Agent": "CleanSpot-Web/1.0"})
                        with urllib.request.urlopen(req, timeout=8) as response:
                            frame_bytes = response.read()

                        st.session_state.sentinel_last_img = frame_bytes

                        st.write("Evaluating frame with Gemini Multimodal AI Vision...")
                        result, ai_error = analyze_garbage_photo(frame_bytes, "image/jpeg")

                        if ai_error:
                            status_box.update(label="AI Inspection Failed", state="error")
                            st.error(ai_error)
                        elif not result.get("is_garbage_issue"):
                            status_box.update(label="Scene Clear — No Waste Detected", state="complete")
                            st.info(f"No Waste Detected: {result.get('explanation')}")
                            st.caption("AI filtered false alarm. No ticket logged to municipal queue.")
                        else:
                            st.write("Waste verified! Auto-registering ticket in Civic-Bin DB...")
                            filename = f"sentinel_{uuid.uuid4().hex[:8]}.jpg"
                            filepath = os.path.join(UPLOAD_DIR, filename)
                            with open(filepath, "wb") as f:
                                f.write(frame_bytes)

                            cat = result.get("category") or "Illegal Dumping"
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

                            status_box.update(label="Waste Confirmed — Ticket Auto-Filed!", state="complete")
                            st.success("Ticket Auto-Filed Successfully.")
                            st.markdown(f"• **Category:** `{cat}` · **Severity:** `{sev}` (`{rating_score}/5`)\\n• **AI Reason:** *{result.get('explanation')}*")
                            st.rerun()

                except Exception as e:
                    st.error(f"Audit pipeline error: {e}")

        with st.container(border=True):
            st.markdown("##### Recent Sentinel Auto-Registered Tickets")
            all_reps = fetch_all_reviews()
            if not all_reps.empty and "username" in all_reps.columns:
                sentinel_reps = all_reps[all_reps["username"] == "CLEANSPOT-SENTINEL-01"]
                if not sentinel_reps.empty:
                    for _, r in sentinel_reps.tail(3).iloc[::-1].iterrows():
                        st.markdown(f"**Ticket #{r['id']} — {r['category']}** ({r['rating']}/5)")
                        st.caption(f"📍 {r['location_name']} · {r['timestamp']}")
                        st.write(r['description'])
                else:
                    st.caption("No autonomous tickets logged yet. Execute an audit above.")
            else:
                st.caption("No reports registered yet.")
