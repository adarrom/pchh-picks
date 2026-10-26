"""Collect movie and TV picks from Pop Culture Happy Hour's "What's Making Us Happy".

Runs on a schedule (GitHub Actions). For each new episode it reads NPR's
transcript, asks Claude which picks are movies or TV, looks up a poster, and
updates picks.json, which the TV app reads. Older picks are never removed.

  python update.py              # normal run (needs ANTHROPIC_API_KEY)
  python update.py --dry-run    # fetch feed + transcripts only, no AI calls, no writes
"""
import email.utils
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FEED = "https://feeds.npr.org/510282/podcast.xml"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)  # user chose: picks from 2026 onward
GIVE_UP_DAYS = 21  # stop retrying an episode whose transcript never appears
MODEL = "claude-opus-5-5"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "picks.json")
UA = {"User-Agent": "Mozilla/5.0 (personal TV recommendations row; github actions)"}

SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["movie", "tv"]},
                    "year": {"type": "integer"},
                    "host": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["title", "kind", "year", "host", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

PROMPT = """Below is part of a transcript of NPR's Pop Culture Happy Hour, including the closing segment "What's Making Us Happy", where each panelist recommends something.

List only the recommendations from that segment that are a specific movie or TV show (series, season, miniseries, special, documentary, concert film, or anime film/series all count). Leave out books, albums, songs, podcasts, games, social media accounts, YouTube or TikTok creators, live shows, articles, and people in general. If a panelist names a person (an actor, say) and then recommends specific movies or shows of theirs, include those titles. Ignore titles that are only mentioned in passing, and ignore the episode's main review topic unless a panelist explicitly makes it their pick.

For each pick give:
- title: the official title as it would appear on a streaming service
- kind: "movie" or "tv"
- year: release year (first-season year for TV); use 0 if unsure
- host: the panelist's surname as it appears in the transcript
- why: one short sentence in your own words on why they recommended it (no quotes)

If there are no movie or TV picks, return an empty list.

Transcript:
"""


def get(url, tries=3):
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def episodes():
    channel = ET.fromstring(get(FEED)).find("channel")
    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        if "making us happy" not in title.lower().replace("’", "'"):
            continue
        date = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
        if date < START:
            continue
        link = item.findtext("link") or ""
        m = re.search(r"/(nx-s1-\d+|\d{9,})/", link)
        yield {"guid": item.findtext("guid") or link, "title": title, "date": date,
               "link": link, "story": m.group(1) if m else None}


def transcript(story):
    """Return transcript lines, or None if NPR hasn't published one (yet)."""
    try:
        page = get("https://www.npr.org/transcripts/" + story)
    except Exception:
        return None
    i = page.find("transcript storytext")
    if i < 0:
        return None
    body = page[i:]
    end = body.find("</article>")  # page chrome after the transcript
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body[:end] if end > 0 else body))
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines if len(lines) > 20 else None


def segment(lines):
    """The closing segment, with a little lead-in. Whole transcript if the cue isn't found."""
    norm = [l.lower().replace("’", "'") for l in lines]
    cues = [i for i, l in enumerate(norm) if "making us happy" in l or "making you happy" in l]
    start = cues[0] if cues and cues[0] > len(lines) // 3 else None
    if start is None:
        later = [i for i in cues if i > len(lines) // 3]
        start = later[0] if later else 0
    return "\n".join(lines[max(0, start - 3):])


def extract(client, text):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT + text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined this transcript")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("model output was cut off")
    out = next(b.text for b in response.content if b.type == "text")
    return json.loads(out)["picks"]


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80] or "untitled"


def poster(title, kind, year):
    """Poster art from Apple's free iTunes Search API, then Wikipedia. '' if nothing found."""
    entity = "movie" if kind == "movie" else "tvSeason"
    q = urllib.parse.urlencode({"term": title, "media": "movie" if kind == "movie" else "tvShow",
                                "entity": entity, "limit": 10, "country": "US"})
    try:
        results = json.loads(get("https://itunes.apple.com/search?" + q)).get("results", [])
        want = slug(title)

        def name(r):
            return r.get("trackName") if kind == "movie" else r.get("artistName") or r.get("collectionName", "")

        def score(r):
            s = 0
            if slug(name(r) or "") == want:
                s += 2
            elif want in slug(r.get("collectionName", "") or name(r) or ""):
                s += 1
            if year and str(r.get("releaseDate", ""))[:4] == str(year):
                s += 1
            return s
        best = max(results, key=score, default=None)
        if best and score(best) >= 2 and best.get("artworkUrl100"):
            return best["artworkUrl100"].replace("100x100bb", "600x900bb")
    except Exception:
        pass
    hint = "film" if kind == "movie" else "television series"
    q = urllib.parse.urlencode({"action": "query", "list": "search", "format": "json", "srlimit": 3,
                                "srsearch": f"{title} {year or ''} {hint}"})
    try:
        hits = json.loads(get("https://en.wikipedia.org/w/api.php?" + q))["query"]["search"]
    except Exception:
        hits = []
    want = slug(title)
    for h in hits:
        if not slug(h["title"]).startswith(want[:20]):
            continue  # top hits can be unrelated articles; require the title to match
        try:
            s = json.loads(get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(h["title"].replace(" ", "_"))))
        except Exception:
            continue
        img = (s.get("originalimage") or s.get("thumbnail") or {}).get("source", "")
        if s.get("type") == "standard" and img.startswith("https://"):
            return img.split("?")[0]
    return ""


def main():
    dry = "--dry-run" in sys.argv
    data = {"updated": None, "picks": [], "episodes": {}}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    client = None
    if not dry:
        import anthropic
        client = anthropic.Anthropic()

    seen = {p["id"] for p in data["picks"]}
    changed = False
    now = datetime.now(timezone.utc)
    # Oldest first, so the newest episode's picks end up at the front of the row.
    for ep in sorted(episodes(), key=lambda e: e["date"]):
        state = data["episodes"].get(ep["guid"], {})
        if state.get("status") in ("done", "no_transcript"):
            continue
        lines = transcript(ep["story"]) if ep["story"] else None
        label = ep["date"].strftime("%Y-%m-%d") + " " + ep["title"]
        if lines is None:
            gave_up = (now - ep["date"]).days > GIVE_UP_DAYS
            print(("no transcript, giving up: " if gave_up else "no transcript yet: ") + label)
            if gave_up and not dry:
                data["episodes"][ep["guid"]] = {"status": "no_transcript", "title": ep["title"]}
                changed = True
            continue
        if dry:
            seg = segment(lines)
            print(f"would extract: {label}  ({len(seg)} chars)")
            continue
        picks = extract(client, segment(lines))
        new = []
        for p in picks:
            pid = p["kind"] + "-" + slug(p["title"])
            if pid in seen:
                continue
            seen.add(pid)
            new.append({
                "id": pid, "title": p["title"], "kind": p["kind"], "year": p["year"] or None,
                "host": p["host"].title(), "description": p["why"],
                "image": poster(p["title"], p["kind"], p["year"]),
                "episode": ep["title"], "episode_date": ep["date"].strftime("%Y-%m-%d"),
                "episode_url": ep["link"],
            })
        print(f"{label}: {len(picks)} movie/TV picks, {len(new)} new: " + ", ".join(n["title"] for n in new))
        data["picks"] = new + data["picks"]
        data["episodes"][ep["guid"]] = {"status": "done", "title": ep["title"], "picks": len(picks)}
        changed = True

    if changed and not dry:
        data["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, OUT)
    print(f"{len(data['picks'])} picks total; {'updated' if changed else 'no changes'}")


if __name__ == "__main__":
    main()
