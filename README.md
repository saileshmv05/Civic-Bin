# Civic Bin

Community Cleanup and Reporting. Residents report garbage and illegal dumping in
neighbourhood blind spots, and the community tracks each report until the spot is cleaned.

## Features

- **Photo verification:** a live or uploaded photo is checked by an AI vision model.
  It confirms the photo shows garbage and suggests a category and severity.
- **Automatic location:** coordinates come from a map click, the browser location,
  or a place search. Users can also outline a stretch such as an alley or lane.
- **Map and heatmap:** all reports are plotted. The heatmap shows open spots and
  cleaned-up spots so priorities are easy to see.
- **Community:** reports can be upvoted and discussed, and a spot can be marked
  cleaned with a before-and-after photo.
- **Complaint helper:** generates text for the municipal grievance form. Chennai's
  Greater Chennai Corporation portal is linked; other areas get a search link.
- **Duplicate guard:** a same-category open report within 40 m points to the existing one.
- **Clean-up timer:** severe open reports show a 7-day public clean-up countdown.
- **Civic Score:** points for reporting, voting, replying, and cleaning up.
- **News:** headlines about waste and sanitation.

## Setup

1. Install the packages:
   ```
   py -m pip install -r requirements.txt
   ```
2. Set the environment variables in the same terminal window:
   ```
   $env:GOOGLE_MAPS_API_KEY = "..."             # required for the map
   $env:GROQ_API_KEY = "..."                     # optional: AI photo check and complaint wording
   $env:GOOGLE_OAUTH_CLIENT_ID = "..."           # optional: sign in with Google
   $env:GOOGLE_OAUTH_CLIENT_SECRET = "..."       # optional
   ```
3. Run the app:
   ```
   py -m streamlit run civic_bin_app.py
   ```

The header of `civic_bin_app.py` explains how to obtain each key.

## Before going live

- **GCC complaint labels:** copy the exact solid-waste complaint group and type from
  the live portal into the `"chennai"` entry of `MUNICIPALITY_DIRECTORY`. Until then,
  citizens get generic wording that links to the real portal.
- **Groq models:** confirm `qwen/qwen3.6-27b` and `openai/gpt-oss-120b` are available
  on your account.
- **Safety:** the sidebar reminds users not to touch medical, chemical, or sharp waste.
- **Authentication** is a prototype. Use salted password hashing before real use.
- Citizen photos are stored in `uploads/`, which is excluded from git.
