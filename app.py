"""
SpotExtend – Spotify Extended Streaming History Dashboard
=========================================================
Upload your Spotify "Extended Streaming History" JSON files
(endsong_0.json, endsong_1.json, …) to get a comprehensive,
interactive visual overview of your listening habits.

How to enable Spotify cover-art / genre lookup:
  1. Create an app at https://developer.spotify.com/dashboard
  2. Copy the Client ID and Client Secret into the sidebar fields.
     The app works out-of-the-box without credentials by falling back
     to TheAudioDB (cover art) and a keyword-based genre mapping.
"""

import io
import json
import re
import time
from functools import lru_cache

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Page config & global CSS (Spotify dark-mode look)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="SpotExtend",
    page_icon="🎵",
    layout="wide",
    initial_sidebar_state="expanded",
)

SPOTIFY_GREEN = "#1DB954"
SPOTIFY_BLACK = "#191414"
CARD_BG = "#282828"
TEXT_COLOR = "#FFFFFF"
SUBTEXT_COLOR = "#B3B3B3"
PLACEHOLDER_IMG = (
    "https://via.placeholder.com/100x100/282828/1DB954?text=%F0%9F%8E5"
)

st.markdown(
    f"""
    <style>
    /* ---- Global ---- */
    html, body, [data-testid="stAppViewContainer"] {{
        background-color: {SPOTIFY_BLACK};
        color: {TEXT_COLOR};
    }}
    [data-testid="stSidebar"] {{
        background-color: #121212;
    }}
    [data-testid="stSidebar"] * {{
        color: {TEXT_COLOR} !important;
    }}
    h1, h2, h3, h4, h5, h6 {{
        color: {TEXT_COLOR};
    }}
    /* ---- Metric cards ---- */
    .metric-card {{
        background: {CARD_BG};
        border-radius: 12px;
        padding: 20px 24px;
        text-align: center;
        box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    }}
    .metric-value {{
        font-size: 2.2rem;
        font-weight: 700;
        color: {SPOTIFY_GREEN};
        line-height: 1.1;
    }}
    .metric-label {{
        font-size: 0.85rem;
        color: {SUBTEXT_COLOR};
        margin-top: 4px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }}
    /* ---- Section headers ---- */
    .section-header {{
        font-size: 1.4rem;
        font-weight: 700;
        color: {TEXT_COLOR};
        border-left: 4px solid {SPOTIFY_GREEN};
        padding-left: 12px;
        margin: 32px 0 16px 0;
    }}
    /* ---- DataFrames ---- */
    [data-testid="stDataFrame"] {{
        background: {CARD_BG};
    }}
    /* ---- Dividers ---- */
    hr {{
        border-color: #333;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Keyword → genre mapping (fallback when no Spotify API credentials)
# ---------------------------------------------------------------------------
GENRE_KEYWORDS: dict[str, list[str]] = {
    "Pop": ["pop", "dance pop", "electropop", "synth-pop", "k-pop", "j-pop"],
    "Rock": ["rock", "indie", "alternative", "grunge", "punk", "emo", "metal"],
    "Hip-Hop": ["hip hop", "rap", "trap", "drill", "grime", "boom bap"],
    "R&B / Soul": ["r&b", "soul", "neo soul", "funk", "motown"],
    "Electronic": [
        "edm",
        "electronic",
        "house",
        "techno",
        "trance",
        "dubstep",
        "dnb",
        "drum and bass",
        "ambient",
    ],
    "Jazz": ["jazz", "blues", "swing", "bebop"],
    "Classical": ["classical", "orchestral", "opera", "chamber"],
    "Country": ["country", "bluegrass", "americana", "folk country"],
    "Latin": ["latin", "reggaeton", "salsa", "bachata", "cumbia", "bossa nova"],
    "Folk": ["folk", "acoustic", "singer-songwriter"],
}


def keyword_genre_mapping(artist_name: str) -> str:
    """Guess a broad genre from the artist name using simple keyword heuristics.
    Returns 'Other' when no keyword matches."""
    name_lower = artist_name.lower()
    for genre, keywords in GENRE_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return genre
    return "Other"


# ---------------------------------------------------------------------------
# Spotify API helpers (optional – requires credentials in sidebar)
# ---------------------------------------------------------------------------

def get_spotify_token(client_id: str, client_secret: str) -> str | None:
    """Obtain a Spotify client-credentials OAuth token."""
    try:
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
    except requests.RequestException:
        pass
    return None


def spotify_search(
    token: str, query: str, search_type: str = "track"
) -> dict | None:
    """Search Spotify Web API.  Returns the first result item or None."""
    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "type": search_type, "limit": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            key = search_type + "s"
            items = data.get(key, {}).get("items", [])
            if items:
                return items[0]
    except requests.RequestException:
        pass
    return None


def get_genres_spotipy(
    token: str, artist_name: str, cache: dict
) -> list[str]:
    """Fetch genres for *artist_name* via Spotify search.  Uses an in-memory
    cache dict to avoid redundant requests."""
    if artist_name in cache:
        return cache[artist_name]
    result = spotify_search(token, artist_name, "artist")
    genres: list[str] = []
    if result:
        genres = result.get("genres", [])
    cache[artist_name] = genres
    return genres


# ---------------------------------------------------------------------------
# TheAudioDB cover-art helper (free, no key required)
# ---------------------------------------------------------------------------
_audiodb_cache: dict[str, str] = {}


def fetch_cover_art_audiodb(artist: str, track: str | None = None) -> str:
    """Return an image URL from TheAudioDB.
    Falls back to PLACEHOLDER_IMG on any error."""
    key = f"{artist}||{track}"
    if key in _audiodb_cache:
        return _audiodb_cache[key]
    try:
        if track:
            url = (
                "https://www.theaudiodb.com/api/v1/json/2/searchtrack.php"
                f"?s={requests.utils.quote(artist)}&t={requests.utils.quote(track)}"
            )
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                tracks = data.get("track") or []
                if tracks and tracks[0].get("strTrackThumb"):
                    img = tracks[0]["strTrackThumb"]
                    _audiodb_cache[key] = img
                    return img
        # Fall back to artist thumbnail
        url = (
            "https://www.theaudiodb.com/api/v1/json/2/search.php"
            f"?s={requests.utils.quote(artist)}"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            artists_list = data.get("artists") or []
            if artists_list:
                img = (
                    artists_list[0].get("strArtistThumb")
                    or artists_list[0].get("strArtistFanart")
                    or PLACEHOLDER_IMG
                )
                _audiodb_cache[key] = img
                return img
    except Exception:
        pass
    _audiodb_cache[key] = PLACEHOLDER_IMG
    return PLACEHOLDER_IMG


def fetch_cover_art_spotify(
    token: str, artist: str, track: str | None = None
) -> str:
    """Return a Spotify album-art URL for the given artist / track."""
    key = f"sp||{artist}||{track}"
    if key in _audiodb_cache:
        return _audiodb_cache[key]
    try:
        if track:
            result = spotify_search(token, f"{artist} {track}", "track")
            if result:
                images = (
                    result.get("album", {}).get("images", [])
                )
                if images:
                    img = images[0]["url"]
                    _audiodb_cache[key] = img
                    return img
        result = spotify_search(token, artist, "artist")
        if result:
            images = result.get("images", [])
            if images:
                img = images[0]["url"]
                _audiodb_cache[key] = img
                return img
    except Exception:
        pass
    _audiodb_cache[key] = PLACEHOLDER_IMG
    return PLACEHOLDER_IMG


# ---------------------------------------------------------------------------
# Data loading & processing
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Processing streaming history…")
def load_and_process(file_contents: list[bytes]) -> pd.DataFrame:
    """Parse, merge, and clean all uploaded JSON files.

    Parameters
    ----------
    file_contents:
        Raw bytes of each uploaded JSON file.

    Returns
    -------
    pd.DataFrame with columns:
        ts, master_metadata_track_name, master_metadata_album_artist_name,
        master_metadata_album_album_name, ms_played, minutes_played,
        hours_played, platform, reason_start, reason_end, shuffle,
        skipped, incognito_mode, year, month, day, hour,
        is_video, is_podcast
    """
    records: list[dict] = []
    for content in file_contents:
        try:
            data = json.loads(content)
            if isinstance(data, list):
                records.extend(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # skip unreadable files

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # ---- Required column guard ----
    required = ["ts", "ms_played"]
    for col in required:
        if col not in df.columns:
            return pd.DataFrame()

    # ---- Deduplication ----
    df.drop_duplicates(inplace=True)

    # ---- Timestamp ----
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df.dropna(subset=["ts"], inplace=True)
    df["year"] = df["ts"].dt.year
    df["month"] = df["ts"].dt.month
    df["day"] = df["ts"].dt.day
    df["hour"] = df["ts"].dt.hour
    df["month_label"] = df["ts"].dt.tz_localize(None).dt.to_period("M").astype(str)

    # ---- Duration ----
    df["ms_played"] = pd.to_numeric(df["ms_played"], errors="coerce").fillna(0)
    df["minutes_played"] = df["ms_played"] / 60_000
    df["hours_played"] = df["ms_played"] / 3_600_000

    # ---- Normalise optional columns ----
    for col in [
        "master_metadata_track_name",
        "master_metadata_album_artist_name",
        "master_metadata_album_album_name",
        "platform",
        "reason_start",
        "reason_end",
    ]:
        if col not in df.columns:
            df[col] = None

    for bool_col in ["shuffle", "skipped", "incognito_mode"]:
        if bool_col not in df.columns:
            df[bool_col] = False
        df[bool_col] = df[bool_col].fillna(False)

    # ---- Classify video vs audio ----
    # Spotify marks video episodes/shows differently; use available signals.
    video_pattern = re.compile(r"video|youtube|watch", re.IGNORECASE)

    def _is_video(row: pd.Series) -> bool:
        platform = str(row.get("platform", "") or "")
        if video_pattern.search(platform):
            return True
        episode_uri = str(row.get("spotify_episode_uri", "") or "")
        if episode_uri.startswith("spotify:episode:"):
            return False
        return False

    if "episode_show_name" in df.columns:
        df["is_podcast"] = df["episode_show_name"].notna()
    else:
        df["is_podcast"] = False

    df["is_video"] = df.apply(_is_video, axis=1)

    # Keep only rows with some track/artist info for music-specific metrics
    # (podcast / empty rows are kept but flagged)
    df["is_music"] = (
        df["master_metadata_track_name"].notna()
        & df["master_metadata_album_artist_name"].notna()
    )

    return df


# ---------------------------------------------------------------------------
# Helper: filtered dataframe based on sidebar year selection
# ---------------------------------------------------------------------------

def apply_year_filter(df: pd.DataFrame, year_choice: str | int) -> pd.DataFrame:
    if year_choice == "All Time":
        return df
    return df[df["year"] == int(year_choice)]


# ---------------------------------------------------------------------------
# Visualisation helpers
# ---------------------------------------------------------------------------

PLOTLY_LAYOUT = dict(
    paper_bgcolor=SPOTIFY_BLACK,
    plot_bgcolor=CARD_BG,
    font=dict(color=TEXT_COLOR, family="Arial"),
    margin=dict(l=20, r=20, t=40, b=20),
)


def section(title: str):
    st.markdown(f'<div class="section-header">{title}</div>', unsafe_allow_html=True)


def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-value">{value}</div>
            <div class="metric-label">{label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------

def build_timeseries_chart(df: pd.DataFrame) -> go.Figure:
    """Monthly listening activity (minutes played)."""
    monthly = (
        df.groupby("month_label")["minutes_played"]
        .sum()
        .reset_index()
        .rename(columns={"month_label": "Month", "minutes_played": "Minutes"})
        .sort_values("Month")
    )
    fig = px.bar(
        monthly,
        x="Month",
        y="Minutes",
        title="🗓️ Monthly Listening Activity",
        color_discrete_sequence=[SPOTIFY_GREEN],
    )
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=16)
    fig.update_xaxes(tickangle=-45, gridcolor="#333")
    fig.update_yaxes(gridcolor="#333")
    return fig


def build_hourly_chart(df: pd.DataFrame) -> go.Figure:
    """Listening count by hour of day."""
    hourly = (
        df.groupby("hour")["ms_played"]
        .count()
        .reset_index()
        .rename(columns={"ms_played": "Streams", "hour": "Hour"})
    )
    fig = px.bar(
        hourly,
        x="Hour",
        y="Streams",
        title="⏰ Listening Activity by Hour of Day",
        color="Streams",
        color_continuous_scale=["#282828", SPOTIFY_GREEN],
    )
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=16, coloraxis_showscale=False)
    fig.update_xaxes(
        tickvals=list(range(24)),
        ticktext=[f"{h:02d}:00" for h in range(24)],
        tickangle=-45,
        gridcolor="#333",
    )
    fig.update_yaxes(gridcolor="#333")
    return fig


def build_top_artists_chart(df: pd.DataFrame, n: int = 10) -> go.Figure:
    top = (
        df[df["is_music"]]
        .groupby("master_metadata_album_artist_name")["hours_played"]
        .sum()
        .nlargest(n)
        .reset_index()
        .rename(
            columns={
                "master_metadata_album_artist_name": "Artist",
                "hours_played": "Hours",
            }
        )
        .sort_values("Hours")
    )
    fig = px.bar(
        top,
        x="Hours",
        y="Artist",
        orientation="h",
        title=f"🎤 Top {n} Artists by Hours Played",
        color_discrete_sequence=[SPOTIFY_GREEN],
    )
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=16)
    fig.update_xaxes(gridcolor="#333")
    fig.update_yaxes(gridcolor="#333")
    return fig


def build_video_vs_audio_chart(df: pd.DataFrame) -> go.Figure:
    counts = df["is_video"].value_counts().reset_index()
    counts.columns = ["Type", "Count"]
    counts["Type"] = counts["Type"].map({True: "Video", False: "Audio"})
    fig = px.pie(
        counts,
        names="Type",
        values="Count",
        title="🎬 Video vs Audio Streams",
        color_discrete_sequence=[SPOTIFY_GREEN, "#535353"],
        hole=0.45,
    )
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=16)
    return fig


def build_genre_radar(genre_hours: dict[str, float]) -> go.Figure:
    """Radar / spider chart for broad genre breakdown."""
    labels = list(genre_hours.keys())
    values = list(genre_hours.values())
    if not labels:
        return go.Figure()
    labels_closed = labels + [labels[0]]
    values_closed = values + [values[0]]
    fig = go.Figure(
        go.Scatterpolar(
            r=values_closed,
            theta=labels_closed,
            fill="toself",
            line_color=SPOTIFY_GREEN,
            fillcolor=f"rgba(29,185,84,0.25)",
            name="Genre Mix",
        )
    )
    fig.update_layout(
        **PLOTLY_LAYOUT,
        title="🕸️ Genre Radar",
        title_font_size=16,
        polar=dict(
            bgcolor=CARD_BG,
            radialaxis=dict(visible=True, gridcolor="#444", color=SUBTEXT_COLOR),
            angularaxis=dict(gridcolor="#444", color=TEXT_COLOR),
        ),
    )
    return fig


def build_yearly_trend(df: pd.DataFrame) -> go.Figure:
    """Total hours per year."""
    yearly = (
        df.groupby("year")["hours_played"]
        .sum()
        .reset_index()
        .rename(columns={"year": "Year", "hours_played": "Hours"})
    )
    fig = px.line(
        yearly,
        x="Year",
        y="Hours",
        markers=True,
        title="📈 Total Listening Hours Per Year",
        color_discrete_sequence=[SPOTIFY_GREEN],
    )
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=16)
    fig.update_xaxes(gridcolor="#333", dtick=1)
    fig.update_yaxes(gridcolor="#333")
    return fig


# ---------------------------------------------------------------------------
# Top-10 with cover art
# ---------------------------------------------------------------------------

def show_top10_with_art(
    top10_df: pd.DataFrame,
    artist_col: str,
    name_col: str,
    count_col: str,
    label: str,
    spotify_token: str | None,
):
    """Render a 2-column layout: cover art on the left, stats on the right."""
    st.markdown(f"#### {label}")
    for rank, row in enumerate(top10_df.itertuples(), start=1):
        artist = getattr(row, artist_col.replace(" ", "_"), "")
        name = getattr(row, name_col.replace(" ", "_"), "")
        count = getattr(row, count_col.replace(" ", "_"), 0)

        if spotify_token:
            img_url = fetch_cover_art_spotify(spotify_token, artist, name)
        else:
            img_url = fetch_cover_art_audiodb(artist, name)

        col_img, col_info = st.columns([1, 5])
        with col_img:
            st.image(img_url, width=80)
        with col_info:
            st.markdown(
                f"**{rank}. {name}**  \n"
                f"<span style='color:{SUBTEXT_COLOR}'>{artist}</span>  \n"
                f"<span style='color:{SPOTIFY_GREEN}'>▶ {count:,} plays</span>",
                unsafe_allow_html=True,
            )
        st.markdown("<hr style='margin:4px 0;border-color:#333'>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Genre breakdown computation
# ---------------------------------------------------------------------------

def compute_genre_hours(
    df: pd.DataFrame, spotify_token: str | None
) -> dict[str, float]:
    """Return a mapping of broad genre → total hours for the radar chart.

    If a Spotify token is available, fetch genres via the API; otherwise use
    the keyword-based fallback mapping."""
    music_df = df[df["is_music"]].copy()
    artist_hours = (
        music_df.groupby("master_metadata_album_artist_name")["hours_played"]
        .sum()
    )

    genre_hours: dict[str, float] = {}
    sp_cache: dict[str, list[str]] = {}

    for artist, hours in artist_hours.items():
        if spotify_token:
            genres = get_genres_spotipy(spotify_token, artist, sp_cache)
            if genres:
                # Map to broad categories
                mapped: set[str] = set()
                for g in genres:
                    g_lower = g.lower()
                    for broad, keywords in GENRE_KEYWORDS.items():
                        if any(kw in g_lower for kw in keywords):
                            mapped.add(broad)
                for broad in mapped or {"Other"}:
                    genre_hours[broad] = genre_hours.get(broad, 0) + hours
                continue
        # keyword fallback
        broad = keyword_genre_mapping(artist)
        genre_hours[broad] = genre_hours.get(broad, 0) + hours

    return genre_hours


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

def main():
    # ---- Sidebar ----
    with st.sidebar:
        st.image(
            "https://storage.googleapis.com/pr-newsroom-wp/1/2018/11/Spotify_Logo_RGB_Green.png",
            use_container_width=True,
        )
        st.markdown("## SpotExtend")
        st.caption("Spotify Extended History Analyser")
        st.markdown("---")

        st.markdown("### 📂 Upload Files")
        uploaded_files = st.file_uploader(
            "Upload endsong_*.json files",
            type="json",
            accept_multiple_files=True,
            help="You can upload multiple JSON files from your Spotify data package.",
        )
        st.markdown("---")

        st.markdown("### 🔑 Spotify API (Optional)")
        st.caption(
            "Add your Spotify Developer credentials for better cover art and genre data. "
            "The app works without them."
        )
        sp_client_id = st.text_input("Client ID", type="password")
        sp_client_secret = st.text_input("Client Secret", type="password")
        spotify_token: str | None = None
        if sp_client_id and sp_client_secret:
            with st.spinner("Authenticating with Spotify…"):
                spotify_token = get_spotify_token(sp_client_id, sp_client_secret)
            if spotify_token:
                st.success("✅ Spotify connected")
            else:
                st.error("❌ Auth failed – check credentials")

        st.markdown("---")
        st.markdown("### 🗓️ Time Filter")
        year_mode = st.radio("View", ["All Time", "Specific Year"], index=0)

    # ---- Landing page ----
    if not uploaded_files:
        st.markdown(
            f"""
            <div style="text-align:center; padding: 60px 20px;">
                <h1 style="font-size:3rem; color:{SPOTIFY_GREEN};">🎵 SpotExtend</h1>
                <p style="font-size:1.2rem; color:{SUBTEXT_COLOR}; max-width:600px; margin:0 auto;">
                    Upload your Spotify <strong>Extended Streaming History</strong> JSON files
                    from the sidebar to explore your listening habits with an interactive dashboard.
                </p>
                <p style="color:{SUBTEXT_COLOR}; margin-top:20px;">
                    Request your data at <strong>Account → Privacy Settings → Request Data</strong>
                    and look for the <em>endsong_*.json</em> files.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    # ---- Load data ----
    file_contents = [f.read() for f in uploaded_files]
    df_full = load_and_process(file_contents)

    if df_full.empty:
        st.error(
            "⚠️ Could not parse any streaming data from the uploaded files. "
            "Please make sure you uploaded valid Spotify endsong_*.json files."
        )
        return

    # ---- Year filter ----
    available_years = sorted(df_full["year"].unique(), reverse=True)
    year_choice: str | int = "All Time"
    if year_mode == "Specific Year":
        year_choice = st.sidebar.selectbox(
            "Select Year", available_years, index=0
        )

    df = apply_year_filter(df_full, year_choice)
    df_music = df[df["is_music"]]

    # ---- Page title ----
    period_label = "All Time" if year_choice == "All Time" else str(year_choice)
    st.markdown(
        f"<h1 style='color:{SPOTIFY_GREEN}; margin-bottom:4px;'>🎵 SpotExtend</h1>"
        f"<p style='color:{SUBTEXT_COLOR}; margin-top:0;'>Listening stats · <strong>{period_label}</strong></p>",
        unsafe_allow_html=True,
    )

    # ================================================================
    # KPI METRIC CARDS
    # ================================================================
    section("📊 Key Metrics")
    total_hours = df["hours_played"].sum()
    unique_artists = df_music["master_metadata_album_artist_name"].nunique()
    unique_songs = df_music["master_metadata_track_name"].nunique()
    total_streams = len(df)
    video_count = int(df["is_video"].sum())
    audio_count = total_streams - video_count

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        metric_card("Total Hours Streamed", f"{total_hours:,.0f}")
    with c2:
        metric_card("Unique Artists", f"{unique_artists:,}")
    with c3:
        metric_card("Unique Songs", f"{unique_songs:,}")
    with c4:
        metric_card("Audio Streams", f"{audio_count:,}")
    with c5:
        metric_card("Video Streams", f"{video_count:,}")

    st.markdown("<br>", unsafe_allow_html=True)

    # ================================================================
    # TIME-SERIES CHARTS
    # ================================================================
    section("📅 Listening Over Time")
    col_ts1, col_ts2 = st.columns(2)
    with col_ts1:
        st.plotly_chart(build_timeseries_chart(df), use_container_width=True)
    with col_ts2:
        st.plotly_chart(build_yearly_trend(df_full), use_container_width=True)

    # ================================================================
    # HOUR OF DAY
    # ================================================================
    section("⏰ Engagement Patterns")
    col_h1, col_h2 = st.columns([3, 2])
    with col_h1:
        st.plotly_chart(build_hourly_chart(df), use_container_width=True)
    with col_h2:
        # Weekday heatmap data
        df_wk = df.copy()
        df_wk["weekday"] = df_wk["ts"].dt.day_name()
        weekday_order = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ]
        wk_counts = (
            df_wk.groupby(["weekday", "hour"])["ms_played"]
            .count()
            .reset_index()
            .rename(columns={"ms_played": "Streams"})
        )
        wk_counts["weekday"] = pd.Categorical(
            wk_counts["weekday"], categories=weekday_order, ordered=True
        )
        wk_pivot = wk_counts.pivot_table(
            index="weekday", columns="hour", values="Streams", fill_value=0
        )
        fig_hm = px.imshow(
            wk_pivot,
            aspect="auto",
            color_continuous_scale=["#191414", SPOTIFY_GREEN],
            title="🗓️ Weekday × Hour Heatmap",
            labels={"x": "Hour of Day", "y": "Weekday", "color": "Streams"},
        )
        fig_hm.update_layout(**PLOTLY_LAYOUT, title_font_size=16)
        st.plotly_chart(fig_hm, use_container_width=True)

    # ================================================================
    # TOP ARTISTS CHART
    # ================================================================
    section("🎤 Top Artists")
    st.plotly_chart(build_top_artists_chart(df_music), use_container_width=True)

    # ================================================================
    # TOP 10 SONGS & ALBUMS WITH COVER ART
    # ================================================================
    section("🏆 Top 10 Lists")
    top_songs = (
        df_music.groupby(
            [
                "master_metadata_track_name",
                "master_metadata_album_artist_name",
                "master_metadata_album_album_name",
            ]
        )
        .size()
        .reset_index(name="Plays")
        .sort_values("Plays", ascending=False)
        .head(10)
        .rename(
            columns={
                "master_metadata_track_name": "Track",
                "master_metadata_album_artist_name": "Artist",
                "master_metadata_album_album_name": "Album",
            }
        )
    )
    top_albums = (
        df_music.groupby(
            [
                "master_metadata_album_album_name",
                "master_metadata_album_artist_name",
            ]
        )
        .size()
        .reset_index(name="Plays")
        .sort_values("Plays", ascending=False)
        .head(10)
        .rename(
            columns={
                "master_metadata_album_album_name": "Album",
                "master_metadata_album_artist_name": "Artist",
            }
        )
    )

    tab_songs, tab_albums = st.tabs(["🎵 Top Songs", "💿 Top Albums"])

    with tab_songs:
        show_top10_with_art(
            top_songs,
            artist_col="Artist",
            name_col="Track",
            count_col="Plays",
            label="Top 10 Most-Played Songs",
            spotify_token=spotify_token,
        )

    with tab_albums:
        show_top10_with_art(
            top_albums,
            artist_col="Artist",
            name_col="Album",
            count_col="Plays",
            label="Top 10 Most-Played Albums",
            spotify_token=spotify_token,
        )

    # ================================================================
    # COMPREHENSIVE TABLES
    # ================================================================
    section("📋 Full Play-Count Tables")
    tab_all_songs, tab_all_albums = st.tabs(["All Songs", "All Albums"])

    with tab_all_songs:
        all_songs = (
            df_music.groupby(
                [
                    "master_metadata_track_name",
                    "master_metadata_album_artist_name",
                    "master_metadata_album_album_name",
                ]
            )
            .agg(
                Plays=("ms_played", "count"),
                Minutes=("minutes_played", "sum"),
            )
            .reset_index()
            .rename(
                columns={
                    "master_metadata_track_name": "Track",
                    "master_metadata_album_artist_name": "Artist",
                    "master_metadata_album_album_name": "Album",
                }
            )
            .sort_values("Plays", ascending=False)
            .reset_index(drop=True)
        )
        all_songs["Minutes"] = all_songs["Minutes"].round(1)
        st.dataframe(
            all_songs,
            use_container_width=True,
            height=400,
        )

    with tab_all_albums:
        all_albums = (
            df_music.groupby(
                [
                    "master_metadata_album_album_name",
                    "master_metadata_album_artist_name",
                ]
            )
            .agg(
                Plays=("ms_played", "count"),
                Minutes=("minutes_played", "sum"),
            )
            .reset_index()
            .rename(
                columns={
                    "master_metadata_album_album_name": "Album",
                    "master_metadata_album_artist_name": "Artist",
                }
            )
            .sort_values("Plays", ascending=False)
            .reset_index(drop=True)
        )
        all_albums["Minutes"] = all_albums["Minutes"].round(1)
        st.dataframe(
            all_albums,
            use_container_width=True,
            height=400,
        )

    # ================================================================
    # GENRE RADAR
    # ================================================================
    section("🕸️ Genre Radar")
    st.caption(
        "Genre data is inferred from artist names using keyword mapping. "
        "Add Spotify API credentials in the sidebar for accurate genre data."
    )
    with st.spinner("Computing genre breakdown…"):
        genre_hours = compute_genre_hours(df, spotify_token)
    if genre_hours:
        # Filter to genres with meaningful share (>0.5 %)
        total_gh = sum(genre_hours.values()) or 1
        genre_filtered = {
            g: h for g, h in genre_hours.items() if h / total_gh > 0.005
        }
        if not genre_filtered:
            genre_filtered = genre_hours
        col_rad1, col_rad2 = st.columns([2, 1])
        with col_rad1:
            st.plotly_chart(
                build_genre_radar(genre_filtered), use_container_width=True
            )
        with col_rad2:
            genre_table = (
                pd.DataFrame(
                    list(genre_filtered.items()), columns=["Genre", "Hours"]
                )
                .sort_values("Hours", ascending=False)
                .reset_index(drop=True)
            )
            genre_table["Hours"] = genre_table["Hours"].round(1)
            genre_table["Share"] = (
                (genre_table["Hours"] / genre_table["Hours"].sum() * 100)
                .round(1)
                .astype(str)
                + "%"
            )
            st.dataframe(genre_table, use_container_width=True, height=300)
    else:
        st.info("Not enough music data to generate a genre breakdown.")

    # ================================================================
    # VIDEO STATS
    # ================================================================
    section("🎬 Video vs Audio Stats")
    col_v1, col_v2 = st.columns([1, 2])
    with col_v1:
        st.plotly_chart(build_video_vs_audio_chart(df), use_container_width=True)
    with col_v2:
        video_df = df[df["is_video"]]
        audio_df = df[~df["is_video"]]
        v_hours = video_df["hours_played"].sum()
        a_hours = audio_df["hours_played"].sum()
        st.markdown(
            f"""
            | Type  | Streams | Hours |
            |-------|--------:|------:|
            | 🎵 Audio | {len(audio_df):,} | {a_hours:,.1f} |
            | 🎬 Video | {len(video_df):,} | {v_hours:,.1f} |
            """
        )
        if not df["is_podcast"].all():
            podcast_df = df[df["is_podcast"]]
            if not podcast_df.empty:
                st.markdown(
                    f"**🎙️ Podcast streams detected:** {len(podcast_df):,} "
                    f"({podcast_df['hours_played'].sum():,.1f} h)"
                )

    # ================================================================
    # SKIPPED & SHUFFLE STATS
    # ================================================================
    section("🔀 Playback Behaviour")
    skipped_pct = df["skipped"].mean() * 100 if len(df) > 0 else 0
    shuffle_pct = df["shuffle"].mean() * 100 if len(df) > 0 else 0
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        skip_fig = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=skipped_pct,
                number={"suffix": "%", "font": {"color": TEXT_COLOR}},
                title={"text": "Skip Rate", "font": {"color": TEXT_COLOR}},
                gauge={
                    "axis": {"range": [0, 100], "tickcolor": SUBTEXT_COLOR},
                    "bar": {"color": SPOTIFY_GREEN},
                    "bgcolor": CARD_BG,
                    "bordercolor": "#444",
                },
            )
        )
        skip_fig.update_layout(**PLOTLY_LAYOUT, height=280)
        st.plotly_chart(skip_fig, use_container_width=True)
    with col_s2:
        shuf_fig = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=shuffle_pct,
                number={"suffix": "%", "font": {"color": TEXT_COLOR}},
                title={"text": "Shuffle Usage", "font": {"color": TEXT_COLOR}},
                gauge={
                    "axis": {"range": [0, 100], "tickcolor": SUBTEXT_COLOR},
                    "bar": {"color": "#1ed760"},
                    "bgcolor": CARD_BG,
                    "bordercolor": "#444",
                },
            )
        )
        shuf_fig.update_layout(**PLOTLY_LAYOUT, height=280)
        st.plotly_chart(shuf_fig, use_container_width=True)

    # ================================================================
    # FOOTER
    # ================================================================
    st.markdown(
        f"""
        <div style="text-align:center; color:{SUBTEXT_COLOR}; padding:40px 0 10px; font-size:0.8rem;">
            SpotExtend · Built with Streamlit &amp; Plotly ·
            Data sourced from your personal Spotify export
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
