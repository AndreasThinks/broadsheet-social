"""Cross-posting a post from one network onto your accounts on the others."""
import io

import httpx
import pytest
from PIL import Image
from starlette.testclient import TestClient

from crossposter import crosspost as xpost
from crossposter.adapters import Upload, base
from crossposter.adapters.demo import DemoAdapter
from crossposter.models import Account, Media, Post, Segment
from crossposter.richtext import length_for


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def _source(text="Look at this", media=None, network="mastodon", account_id="demo-mastodon", **kw) -> Post:
    return Post(network=network, id="src-1", account_id=account_id,
                author=Account(network=network, id="1", handle="ada@m.example", display_name="Ada Lovelace"),
                created_at=0, url="https://m.example/@ada/1", text=[Segment("text", text)], media=media or [], **kw)


def test_body_uses_full_links_and_handles():
    p = _source()
    p.text = [Segment("text", "Read "), Segment("link", "example.com/a-long…", href="https://example.com/a-long-path"),
              Segment("text", " with "), Segment("mention", "@bob", href="https://m.example/@bob", target="bob@m.example"),
              Segment("text", " "), Segment("tag", "#python", target="python")]
    assert xpost.body_text(p) == "Read https://example.com/a-long-path with @bob@m.example #python"


def test_attribution_names_the_author_without_tagging_them():
    p = _source()
    assert xpost.attribution(p, mine=False) == "Via Ada Lovelace on Mastodon: https://m.example/@ada/1"
    assert xpost.attribution(p, mine=True) == "Originally on Mastodon: https://m.example/@ada/1"


def test_fit_shortens_the_words_but_keeps_the_link():
    attr = "Via Ada on Mastodon: https://m.example/@ada/1"
    body = "word " * 100
    assert xpost.fit("short", attr, "bluesky", 300) is None
    out = xpost.fit(body.strip(), attr, "bluesky", 300)
    assert out.endswith(attr) and "…" in out and length_for("bluesky", out) <= 300
    out = xpost.fit(body.strip(), attr, "x", 280)
    assert out.endswith(attr) and length_for("x", out) <= 280


def test_each_network_gets_what_it_can_take():
    img = Upload(b"i", "image/jpeg")
    vid = Upload(b"v", "video/mp4")
    assert xpost.uploads_for("x", [vid]) == ([], 1)
    assert xpost.uploads_for("bluesky", [vid]) == ([vid], 0)
    assert xpost.uploads_for("mastodon", [img] * 5) == ([img] * 4, 1)
    assert xpost.uploads_for("bluesky", [Upload(b"a", "audio/mpeg")])[0] == []


@pytest.fixture
def media_server(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path.endswith("/missing.jpg"):
            return httpx.Response(404)
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    monkeypatch.setattr(base, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return seen


def test_cross_post_copies_text_link_media_and_alt(media_server, monkeypatch):
    from crossposter import cache
    from crossposter.app import app, db
    sent = []

    async def fake_post(self, text, images, reply_to=None, content_warning="", thread=False):
        sent.append((self.network, text, [(u.mime, u.alt) for u in images], content_warning))
        return [base.PostResult(id="new", url="#")]
    monkeypatch.setattr(DemoAdapter, "post", fake_post)
    src = _source(media=[Media("image", url="https://files.m.example/a.png", thumb="https://files.m.example/a-small.png",
                               alt="A red square"),
                         Media("image", url="https://files.m.example/missing.jpg")],
                  content_warning="spoilers")
    cache.save_posts(db, [src])
    with TestClient(app, base_url="https://testserver") as c:
        page = c.get("/compose", params={"crosspost": src.key}).text
        assert "Cross-post" in page and "Via Ada Lovelace on Mastodon: https://m.example/@ada/1" in page
        assert 'value="A red square"' in page and 'name="malt_1"' in page and 'value="spoilers"' in page
        # It's already on Mastodon, so only Bluesky starts ticked.
        assert 'value="demo-bluesky" checked' in page.replace('data-network="bluesky" ', "")
        assert 'checked data-network="mastodon"' not in page and 'value="demo-mastodon" checked' not in page
        r = c.post("/compose", data={"text": "Look at this\n\nVia Ada Lovelace on Mastodon: https://m.example/@ada/1",
                                     "crosspost": src.key, "accounts": ["demo-bluesky"], "media": ["0", "1"],
                                     "malt_0": "A red square, edited", "malt_1": "", "cw": "spoilers", "thread": "on"})
    assert "Posted." in r.text and "couldn't be copied" in r.text
    assert sent == [("bluesky", "Look at this\n\nVia Ada Lovelace on Mastodon: https://m.example/@ada/1",
                     [("image/png", "A red square, edited")], "spoilers")]
    assert "https://files.m.example/a.png" in media_server


def test_too_long_for_bluesky_prefills_a_shortened_version():
    from crossposter import cache
    from crossposter.app import app, db
    src = _source(text="lorem ipsum " * 40)
    src.id = "src-long"
    cache.save_posts(db, [src])
    with TestClient(app, base_url="https://testserver") as c:
        page = c.get("/compose", params={"crosspost": src.key}).text
    assert "Shortened to fit" in page and "<details open" in page
    assert "…\n\nVia Ada Lovelace on Mastodon" in page


def test_action_bar_offers_cross_post():
    from fasthtml.common import to_xml
    from crossposter.ui import action_bar
    assert "/compose?crosspost=" in to_xml(action_bar(_source()))


def test_missing_source_explains_itself():
    from crossposter.app import app
    with TestClient(app, base_url="https://testserver") as c:
        assert "isn't loaded any more" in c.get("/compose", params={"crosspost": "nope|nope"}).text
