from crossposter.richtext import (bsky_segments, build_bsky_facets, length_for, mastodon_segments, plain_segments,
                                  split_for_thread, x_segments)


def test_bsky_facets_use_utf8_byte_offsets():
    text = "héllo 🦋 https://example.com and @alice.bsky.social #python"
    facets = build_bsky_facets(text, lambda h: "did:plc:alice" if h == "alice.bsky.social" else None)
    kinds = [f["features"][0]["$type"].split("#")[1] for f in facets]
    assert kinds == ["link", "mention", "tag"]
    data = text.encode()
    link = facets[0]["index"]
    assert data[link["byteStart"]:link["byteEnd"]].decode() == "https://example.com"
    segs = bsky_segments(text, facets)
    assert [s.kind for s in segs] == ["text", "link", "text", "mention", "text", "tag"]
    assert segs[1].href == "https://example.com"
    assert segs[3].text == "@alice.bsky.social"
    assert segs[5].target == "python"


def test_bsky_unresolved_mention_is_plain_text():
    assert build_bsky_facets("hi @nobody.example", lambda h: None) == []


def test_bsky_bad_facets_are_ignored():
    segs = bsky_segments("short", [{"index": {"byteStart": 3, "byteEnd": 99}, "features": [{"$type": "x#link"}]}])
    assert [s.text for s in segs] == ["short"]


def test_mastodon_html():
    html = ('<p>Hi <span class="h-card"><a href="https://m.social/@bob" class="u-url mention">@<span>bob</span></a></span> '
            'see <a href="https://example.com/a/very/long/path" rel="nofollow"><span class="invisible">https://</span>'
            '<span class="ellipsis">example.com/a/very/lo</span><span class="invisible">ng/path</span></a></p>'
            '<p><a href="https://m.social/tags/python" class="mention hashtag">#<span>python</span></a><br>bye</p>')
    segs = mastodon_segments(html, [{"url": "https://m.social/@bob", "acct": "bob@m.social"}])
    kinds = [s.kind for s in segs]
    assert kinds == ["text", "mention", "text", "link", "text", "tag", "text"]
    assert segs[1].target == "bob@m.social"
    assert segs[3].text == "example.com/a/very/lo…"
    assert segs[3].href == "https://example.com/a/very/long/path"
    assert segs[4].text == "\n\n"
    assert segs[6].text == "\nbye"


def test_x_entities():
    text = "Look https://t.co/abc @jack #ai"
    ents = {"urls": [{"start": 5, "end": 21, "url": "https://t.co/abc", "expanded_url": "https://example.com/x",
                      "display_url": "example.com/x"}],
            "mentions": [{"start": 22, "end": 27, "username": "jack"}],
            "hashtags": [{"start": 28, "end": 31, "tag": "ai"}]}
    segs = x_segments(text, ents)
    assert [s.kind for s in segs] == ["text", "link", "text", "mention", "text", "tag"]
    assert segs[1].text == "example.com/x"


def test_lengths_count_urls_as_23_on_mastodon():
    text = "a https://example.com/" + "x" * 100
    assert length_for("mastodon", text) == 2 + 23
    assert length_for("bluesky", text) == len(text)


def test_split_for_thread():
    text = " ".join(["word"] * 200)
    parts = split_for_thread(text, 300)
    assert len(parts) > 1
    assert all(len(p) <= 300 for p in parts)
    assert parts[0].endswith(f"(1/{len(parts)})")
    assert split_for_thread("short", 300) == ["short"]


def test_plain_segments():
    segs = plain_segments("see https://a.b/c.")
    assert [s.kind for s in segs] == ["text", "link", "text"]
    assert segs[1].href == "https://a.b/c"
