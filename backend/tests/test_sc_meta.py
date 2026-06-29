"""SoundCloud title/artist parsing."""
from sc_client_simple import infer_sc_artist_title


def test_publisher_metadata_strict():
    title, artist = infer_sc_artist_title(
        {
            "title": "Dup Artist - Dup Artist - Real Song",
            "publisher_metadata": {"artist": "Dup Artist", "title": "Real Song"},
        },
        query="Dup Artist",
    )
    assert artist == "Dup Artist"
    assert title == "Real Song"


def test_publisher_metadata_no_strip_when_both_set():
    title, artist = infer_sc_artist_title(
        {
            "title": "ignored raw",
            "publisher_metadata": {"artist": "AC/DC", "title": "Highway to Hell"},
        },
        query="AC/DC",
    )
    assert artist == "AC/DC"
    assert title == "Highway to Hell"


def test_split_raw_title():
    title, artist = infer_sc_artist_title({"title": "Beatles - Hey Jude"})
    assert artist == "Beatles"
    assert title == "Hey Jude"


def test_no_username_as_artist():
    title, artist = infer_sc_artist_title({"title": "пасош — улицы"}, query="пасош")
    assert artist == "пасош"
    assert title == "улицы"


def test_split_matches_multiword_query():
    title, artist = infer_sc_artist_title({"title": "пасош — улицы"}, query="пасош улицы")
    assert artist == "пасош"
    assert title == "улицы"


def test_dash_split_wins_over_mistagged_uploader_artist():
    """Mis-tag: настоящий артист в title перед дефисом, а в metadata стоит имя загрузчика."""
    title, artist = infer_sc_artist_title(
        {
            "title": "пасош - я очень устал",
            "metadata_artist": "Petar Martic",
            "user": {"username": "petar-martic", "full_name": "Petar Martic"},
        }
    )
    assert artist == "пасош"
    assert title == "я очень устал"


def test_no_dash_keeps_structured_artist():
    """Без дефиса в названии — берём артиста из метаданных."""
    title, artist = infer_sc_artist_title(
        {"title": "Каждый День", "metadata_artist": "пасош"}
    )
    assert artist == "пасош"
    assert title == "Каждый День"
