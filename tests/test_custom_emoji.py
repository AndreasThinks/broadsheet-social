from crossposter.adapters.mastodon import to_post
from crossposter.models import Post
from crossposter.richtext import emoji_segments, mastodon_segments
from crossposter import ui

GRIN = "https://cdn.example/emoji/ed_grin.png"
STATUS = {
    "id": "9", "created_at": "2026-11-04T10:00:00Z", "url": "https://m.social/@ed/9",
    "content": "<p>Texas is blue? :ed_grin: and :not_an_emoji:</p>",
    "spoiler_text": "politics :ed_grin:",
    "emojis": [{"shortcode": "ed_grin", "url": GRIN, "static_url": GRIN}],
    "account": {"id": "1", "acct": "ed", "username": "ed", "display_name": "Ed :party: Smith",
                "emojis": [{"shortcode": "party", "url": "https://cdn.example/party.gif"}]},
    "poll": {"id": "3", "options": [{"title": "yes :ed_grin:", "votes_count": 1}], "emojis": []},
}


def test_shortcodes_become_emoji_segments():
    segs = emoji_segments("hi :a: :b:", {"a": "u"})
    assert [(s.kind, s.text) for s in segs] == [("text", "hi "), ("emoji", ":a:"), ("text", " :b:")]
    assert segs[1].href == "u" and segs[1].target == "a"


def test_mastodon_html_img_emoji():
    segs = mastodon_segments('<p>yo <img alt=":x:" src="z"></p>', emojis={"x": "u"})
    assert [s.kind for s in segs] == ["text", "emoji"]


def test_status_renders_emoji_images():
    p = to_post(STATUS, "mastodon:1@m.social", "m.social")
    assert [s.kind for s in p.text][:2] == ["text", "emoji"]
    assert "Texas is blue? :ed_grin:" in p.plain_text
    assert p.author.emojis == {"party": "https://cdn.example/party.gif"}
    assert p.author.plain_name == "Ed Smith"
    # survives the cache round trip
    p = Post.from_dict(p.to_dict())
    from fasthtml.common import to_xml
    html = to_xml(ui.post_card(p))
    assert f'src="{GRIN}"' in html and 'class="emoji"' in html
    assert "https://cdn.example/party.gif" in html
    assert ":not_an_emoji:" in html
    assert html.count('class="emoji"') >= 4  # text, name, content warning, poll option


def test_emoji_html_escapes():
    assert ui.emoji_html("<b> :a:", {"a": 'x" onerror="1'}) .startswith("&lt;b&gt; <img")
    assert 'onerror="1' not in ui.emoji_html(":a:", {"a": 'x" onerror="1'})
