from datetime import datetime, timezone

from babbel.api.topics import matching_interests, parse_feed

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Club Brugge wint van Gent</title><link>https://ex.be/a</link>
<description>&lt;p&gt;Blauw-zwart wint met 2-0.&lt;/p&gt;</description>
<pubDate>Mon, 23 Nov 2026 20:00:00 +0100</pubDate></item>
<item><title>Oudere titel</title><link>https://ex.be/b</link><pubDate>Sun, 22 Nov 2026 08:00:00 +0100</pubDate></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Tuintips voor de winter</title>
<link href="https://ex.be/c"/><summary>Bescherm je planten.</summary><updated>2026-11-23T09:00:00Z</updated></entry></feed>"""


def test_parse_rss_strips_html_and_sorts():
    items = parse_feed(RSS)
    assert items[0].title == "Club Brugge wint van Gent"
    assert items[0].summary == "Blauw-zwart wint met 2-0."
    assert items[0].published_at < datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_parse_atom():
    items = parse_feed(ATOM)
    assert items[0].url == "https://ex.be/c" and items[0].summary == "Bescherm je planten."


def test_matching_interests():
    feeds = {"voetbal": ["u"], "tuin": ["v"], "wielrennen": []}
    assert matching_interests(["Voetbal (Club Brugge)", "tuin"], feeds) == ["voetbal", "tuin"]
