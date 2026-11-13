#!/usr/bin/env python3
"""Static NANP area code -> IANA timezone table for the send gate.

Covers United States and Canadian area codes. Each entry maps the three-digit
area code to every IANA zone that area code can legitimately cover; the gate
requires the recipient-local send window to hold in ALL listed zones, so a
multi-zone area code is governed by its most restrictive member at any moment.

Deliberately omitted: toll-free/premium codes (800/833/844/855/866/877/888/900),
non-geographic codes, and +1 destinations outside the US and Canada (Caribbean,
Guam, etc.). An unmapped area code fails closed: the gate cannot determine the
recipient's local time and refuses the send.
"""

from __future__ import annotations

import re
from functools import lru_cache
from zoneinfo import ZoneInfo

_E164_NANP_RE = re.compile(r"^\+1([0-9]{3})[0-9]{7}$")

EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"
PACIFIC = "America/Los_Angeles"
ALASKA = "America/Anchorage"
ALEUTIAN = "America/Adak"
HAWAII = "Pacific/Honolulu"
CA_EASTERN = "America/Toronto"
CA_CENTRAL = "America/Winnipeg"
CA_MOUNTAIN = "America/Edmonton"
CA_SASKATCHEWAN = "America/Regina"
CA_PACIFIC = "America/Vancouver"
CA_ATLANTIC = "America/Halifax"
CA_NEW_BRUNSWICK = "America/Moncton"
CA_NEWFOUNDLAND = "America/St_Johns"
CA_YUKON = "America/Whitehorse"
CA_EASTERN_NU = "America/Iqaluit"

_AREA_CODE_ZONES: dict[str, tuple[str, ...]] = {}


def _register(zones: tuple[str, ...], *codes: str) -> None:
    for code in codes:
        if code in _AREA_CODE_ZONES:
            raise ValueError(f"duplicate area code registration: {code}")
        _AREA_CODE_ZONES[code] = zones


# --- United States ---

_register(
    (EASTERN,),
    # Connecticut
    "203", "475", "860", "959",
    # District of Columbia
    "202", "771",
    # Delaware
    "302",
    # Georgia
    "229", "404", "470", "478", "678", "706", "762", "770", "912", "943",
    # Florida (all except the panhandle codes below)
    "239", "305", "321", "352", "386", "407", "561", "645", "689", "727", "728",
    "754", "772", "786", "813", "863", "904", "941", "954",
    # Indiana (Eastern portion)
    "260", "317", "463", "765",
    # Kentucky (Eastern portion)
    "502", "606", "859",
    # Maine
    "207",
    # Maryland
    "227", "240", "301", "410", "443", "667",
    # Massachusetts
    "339", "351", "413", "508", "617", "774", "781", "857", "978",
    # Michigan (all Eastern except the multi-zone 906 below)
    "231", "248", "269", "313", "517", "586", "616", "679", "734", "810", "947", "989",
    # New Hampshire
    "603",
    # New Jersey
    "201", "551", "609", "640", "732", "848", "856", "862", "908", "973",
    # New York
    "212", "315", "329", "332", "347", "363", "516", "518", "607", "624", "631",
    "646", "680", "716", "718", "838", "845", "914", "917", "929", "934",
    # North Carolina
    "252", "336", "472", "704", "743", "828", "910", "919", "980", "984",
    # Ohio
    "216", "220", "234", "283", "326", "330", "380", "419", "436", "440", "513",
    "567", "614", "740", "937",
    # Pennsylvania
    "215", "223", "267", "272", "412", "445", "484", "570", "582", "610", "717",
    "724", "814", "835", "878",
    # Rhode Island
    "401",
    # South Carolina
    "803", "821", "839", "843", "854", "864",
    # Tennessee (Knoxville)
    "865",
    # Vermont
    "802",
    # Virginia
    "276", "434", "540", "571", "686", "703", "757", "804", "826", "948",
    # West Virginia
    "304", "681",
)

_register(
    (CENTRAL,),
    # Alabama
    "205", "251", "256", "334", "659", "938",
    # Arkansas
    "479", "501", "870",
    # Illinois
    "217", "224", "309", "312", "331", "447", "464", "618", "630", "708", "730",
    "773", "779", "815", "847", "861", "872",
    # Indiana (northwest, Central)
    "219",
    # Iowa
    "319", "515", "563", "641", "712",
    # Kansas (fully Central codes only)
    "316", "913",
    # Louisiana
    "225", "318", "337", "504", "985",
    # Minnesota
    "218", "320", "507", "612", "651", "763", "924", "952",
    # Mississippi
    "228", "601", "662", "769",
    # Missouri
    "314", "417", "557", "573", "636", "660", "816", "975",
    # Nebraska (eastern)
    "402", "531",
    # Oklahoma
    "405", "539", "572", "580", "918",
    # Tennessee (Nashville/Memphis)
    "615", "629", "731", "901", "931",
    # Texas (Central codes only; El Paso and 432 are below)
    "210", "214", "254", "281", "325", "346", "361", "409", "430", "469", "512",
    "621", "682", "713", "726", "737", "806", "817", "830", "832", "903", "936",
    "940", "945", "956", "972", "979",
    # Wisconsin
    "262", "274", "353", "414", "534", "608", "715", "920",
)

_register(
    (MOUNTAIN,),
    # Colorado
    "303", "719", "720", "970", "983",
    # Montana
    "406",
    # New Mexico
    "505", "575",
    # Texas (El Paso / Hudspeth only)
    "915",
    # Utah (Wasatch)
    "385", "801",
    # Wyoming
    "307",
)

_register(
    (ARIZONA,),
    # Arizona codes that never cover the DST-observing Navajo Nation
    "480", "520", "602", "623",
)

_register(
    (PACIFIC,),
    # California
    "209", "213", "279", "310", "323", "341", "350", "369", "408", "415", "424",
    "442", "510", "530", "559", "562", "619", "626", "628", "650", "657", "661",
    "669", "707", "714", "747", "760", "805", "818", "831", "837", "840", "858",
    "909", "916", "925", "949", "951",
    # Nevada (southern)
    "702", "725",
    # Oregon (Portland metro only; 541/458 cover Mountain-observing Malheur)
    "503", "971",
    # Washington
    "206", "253", "360", "425", "509", "564",
)

_register(
    (ALASKA, ALEUTIAN),
    # 907 covers the far Aleutians (Hawaii-Aleutian time) as well as Alaska time.
    "907",
)

_register((HAWAII,), "808")

# --- US multi-zone area codes ---
# A send must satisfy the window in every listed zone.

_register(
    (CENTRAL, EASTERN),
    # Florida panhandle: Pensacola Central, Tallahassee Eastern
    "324", "448", "850",
    # Indiana southwest: Evansville Central, rest of 812/930 Eastern
    "812", "930",
    # Indiana north-central: Starke/Pulaski Central inside an Eastern overlay
    "574",
    # Kentucky west: Central with some Eastern counties
    "270", "364",
    # Michigan Upper Peninsula: four western counties are Central
    "906",
    # Tennessee Chattanooga: Marion County is Central
    "423",
)

_register(
    (CENTRAL, MOUNTAIN),
    # Kansas: Mountain counties under both codes
    "620", "785",
    # Nebraska panhandle is Mountain
    "308",
    # North Dakota southwest is Mountain
    "701",
    # South Dakota west is Mountain
    "605",
    # Texas 432 covers Mountain-observing Culberson County
    "432",
)

_register(
    (PACIFIC, MOUNTAIN),
    # Idaho: panhandle Pacific, south Mountain
    "208", "986",
    # Nevada 775: West Wendover/Jackpot observe Mountain
    "775",
    # Oregon 541/458: Malheur County is Mountain
    "458", "541",
)

_register(
    (ARIZONA, MOUNTAIN),
    # 928 covers the Navajo Nation, which observes DST unlike the rest of AZ
    "928",
    # 435 covers Utah (Mountain DST) and the no-DST Arizona Strip
    "435",
)

# --- Canada ---

_register(
    (CA_EASTERN,),
    # Ontario (all Eastern except the multi-zone 807 below)
    "226", "249", "289", "343", "365", "382", "416", "437", "519", "548", "613",
    "647", "683", "705", "742", "905", "942",
    # Quebec (all Eastern except the 418-family below)
    "263", "354", "438", "450", "468", "514", "579", "819", "873",
)

_register(
    (CA_EASTERN, CA_ATLANTIC),
    # Quebec 418-family reaches the Atlantic-observing Magdalen Islands
    "367", "418", "581",
    # Ontario 807: Thunder Bay Eastern, Kenora/Rainy River Central
    "807",
)

_register((CA_CENTRAL,), "204", "431", "584")

_register(
    (CA_SASKATCHEWAN, CA_MOUNTAIN),
    # Saskatchewan stays on CST year-round except Lloydminster, which follows Alberta
    "306", "474", "639",
)

_register((CA_MOUNTAIN,), "368", "403", "587", "780", "825")

_register(
    (CA_PACIFIC, CA_MOUNTAIN),
    # British Columbia: Peace River and East Kootenay observe Mountain
    "236", "250", "257", "604", "672", "778",
)

_register((CA_NEW_BRUNSWICK,), "428", "506")

_register((CA_ATLANTIC,), "782", "902")

_register(
    (CA_NEWFOUNDLAND, CA_ATLANTIC),
    # Newfoundland time with Atlantic-observing Labrador coast communities
    "709",
)

_register(
    (CA_YUKON, CA_MOUNTAIN, CA_CENTRAL, CA_EASTERN_NU),
    # 867 spans Yukon (permanent UTC-7), the NWT (Mountain DST), and Nunavut
    # (Eastern and Central communities)
    "867",
)


def area_code_timezones(area_code: str) -> tuple[ZoneInfo, ...]:
    """Return every IANA zone the area code can cover, or an empty tuple."""
    names = _AREA_CODE_ZONES.get(str(area_code).strip())
    if not names:
        return ()
    return tuple(ZoneInfo(name) for name in names)


@lru_cache(maxsize=1024)
def timezones_for_e164(number: str) -> tuple[ZoneInfo, ...]:
    """Map an E.164 NANP number to its recipient timezone candidates."""
    match = _E164_NANP_RE.match(str(number).strip())
    if not match:
        return ()
    return area_code_timezones(match.group(1))


def is_known_area_code(area_code: str) -> bool:
    return str(area_code).strip() in _AREA_CODE_ZONES
