import json

from fasthtml.common import to_xml

from crossposter.models import Account, Media, Post
from crossposter.ui import media_grid


def _post(media):
    author = Account(network="bluesky", id="did:plc:a", handle="a.bsky.social", display_name="A")
    return Post(network="bluesky", id="p1", account_id="acct", author=author, created_at=0, media=media)


def test_every_picture_reaches_the_viewer_and_extras_show_as_a_count():
    pics = [Media("image", url=f"https://cdn.example/{i}.jpg", alt="a heron" if i == 0 else "") for i in range(6)]
    grid = media_grid(_post(pics))
    html = to_xml(grid)
    listing = json.loads(grid.attrs["data-media"])
    assert html.count('data-index="') == 4
    assert "+2" in html
    assert html.count(">ALT<") == 1
    assert 'target="_blank"' in html  # still opens somewhere without scripts
    assert len(listing) == 6


def test_media_listing_keeps_order_and_drops_unsafe_urls():
    media = [Media("image", url="javascript:alert(1)"), Media("video", url="https://v.example/a.mp4", thumb="https://v.example/a.jpg"),
             Media("gif", url="https://v.example/b.mp4"), Media("audio", url="https://a.example/c.mp3")]
    grid = media_grid(_post(media))
    listing = json.loads(grid.attrs["data-media"])
    assert [m["kind"] for m in listing] == ["image", "video", "gif"]
    assert listing[0]["url"] == "#"
    html = to_xml(grid)
    assert "media-open" in html and "media-pause" in html and "Audio attachment" in html
