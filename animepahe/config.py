from datetime import date

BASE_URL = "https://animepahe.pw"
INDEX_URL = f"{BASE_URL}/anime"
TODAY = date.today().strftime("%Y%m%d")
CSV_PATH = f"animepahe/data/{TODAY}.csv"
IMAGE_URLS_CSV = f"animepahe/data/images/{TODAY}.csv"

# "hash" is the "#" tab (titles that start with a digit/symbol), then A-Z.
# Matches the tab-pane ids exactly (href="#A" -> id="A", href="#hash" -> id="hash").
LETTER_ORDER = ["hash"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

FIELD_NAMES = [
    "pahe_id",
    "title",
    "title_romaji",
    "summary",
    "relations",
    "recommendations",
    "synonyms",
    "title_japanese",
    "title_spanish",
    "title_french",
    "type",
    "episodes",
    "status",
    "duration",
    "airing",
    "aired_from",
    "aired_to",
    "season",
    "studios",
    "themes",
    "demographics",
    "external_links",
    "genres",
    "image_url",
    "youtube_url",
]
