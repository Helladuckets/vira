"""Synthetic vocabulary for the walkthrough anonymization layer.

Name pools are bucketed by length at pick time so a fake name lands
within a character or two of the real one and the captured layout holds.
Everything here is deterministic: pick() derives its choice from a
sha256 of the real string, so the same real name maps to the same
synthetic name in every run, every session, every walkthrough.
"""
import hashlib

SALT = "vira-anon-v1"  # bump to re-roll every mapping at once


def h(s):
    """Deterministic integer hash of a string (salted, case-folded)."""
    return int(hashlib.sha256((SALT + s.lower()).encode()).hexdigest(), 16)


# First names a synthetic person can wear. Deliberately ordinary; length
# spread 3-9 so pick() can length-match.
FIRST = [
    "Abe", "Ada", "Avi", "Ben", "Bea", "Cal", "Dex", "Eli", "Eve", "Gus",
    "Ida", "Ike", "Jed", "Kai", "Lev", "Lia", "Mia", "Ned", "Nia", "Ora",
    "Oz", "Pax", "Rex", "Sid", "Tad", "Uma", "Val", "Wes", "Zed", "Zoe",
    "Alden", "Anika", "Ansel", "Arlo", "Basil", "Blair", "Brice", "Brynn",
    "Caleb", "Carys", "Cedar", "Clive", "Coral", "Cyrus", "Delia", "Dorian",
    "Edith", "Elias", "Elsa", "Emory", "Enzo", "Esme", "Etta", "Ezra",
    "Fern", "Finn", "Flora", "Gideon", "Greta", "Hollis", "Hugo", "Ines",
    "Ione", "Iris_", "Jasper", "Jonas", "Jorah", "Judith", "Juniper",
    "Keir", "Kenji", "Lars", "Leif", "Lenora", "Linus", "Lorna", "Lucian",
    "Lydia", "Mabel", "Magnus", "Marisol", "Merrit", "Milo", "Mira",
    "Moira", "Nadia", "Nell", "Nico", "Noemi", "Odessa", "Olin", "Ophelia",
    "Orin", "Oscar", "Otis", "Paloma", "Petra", "Phineas", "Priya",
    "Quincy", "Ramona", "Renata", "Rhea", "Roscoe", "Rowan", "Rufus",
    "Sable", "Selma", "Simone", "Soren", "Tamsin", "Tavish", "Thea",
    "Tobias", "Ursula", "Vada", "Vera", "Vincent", "Wilda", "Xenia",
    "Yara", "Yusuf", "Zelda", "Zora", "Alaric", "Beatrix", "Callum",
    "Damaris", "Eloise", "Fiona", "Gwendolyn", "Hadley", "Imogen",
    "Jericho", "Katya", "Leopold", "Mordecai", "Nerissa", "Octavia",
    "Percival", "Quilla", "Rosalind", "Sylvester", "Theodora", "Ulysses",
    "Vivienne", "Wallace", "Xiomara", "Yolanda", "Zachariah",
]
FIRST = [n for n in FIRST if not n.endswith("_")]  # drop escaped dupes

# Surnames, same idea, length spread 3-12.
LAST = [
    "Ash", "Cobb", "Dane", "Fenn", "Gale", "Hale", "Kerr", "Lund", "Mott",
    "Nash", "Orr", "Pike", "Quill", "Rand", "Sorel", "Tate", "Vance",
    "Webb", "Yates", "Zink", "Ainsley", "Alcott", "Barlow", "Bexley",
    "Braddock", "Brammer", "Calloway", "Carraway", "Cavanagh", "Colfax",
    "Crandall", "Danvers", "Delacroix", "Ellsworth", "Everhart", "Fairbanks",
    "Farrow", "Fenwick", "Galloway", "Garrick", "Granville", "Hargrove",
    "Hartwell", "Hawthorne", "Hollister", "Ingram", "Irving", "Jessup",
    "Kestrel", "Kingsley", "Lachlan", "Lattimer", "Ledger", "Lockhart",
    "Marlowe", "Mercer", "Middleton", "Montrose", "Nettles", "Norwood",
    "Oakhurst", "Okafor", "Pemberton", "Penrose", "Quimby", "Radcliffe",
    "Ravenel", "Redmond", "Renshaw", "Rutledge", "Sablewood", "Selwyn",
    "Severin", "Shackleton", "Sinclair", "Southgate", "Stroud", "Talmadge",
    "Tennyson", "Thackeray", "Thorne", "Tillman", "Umber", "Underhill",
    "Vandermeer", "Varga", "Wexford", "Whitfield", "Wickham", "Winslow",
    "Wolcott", "Yarrow", "Zeller", "Abernathy", "Beaumont", "Castellano",
    "Delgado", "Eastwood", "Fairweather", "Giordano", "Halloran",
    "Iverson", "Jacoby", "Kensington", "Lindqvist", "Moriarty",
    "Northcott", "Oleander", "Pellegrino", "Quintero", "Rasmussen",
    "Sandoval", "Thistlewood", "Uphoff", "Villanueva", "Westergaard",
]

# Company-name components for entities marked class_hint == "company".
COMPANY_A = [
    "Halcyon", "Meridian", "Beacon", "Cobalt", "Vantage", "Northwind",
    "Silverline", "Crestway", "Bluepeak", "Ironwood", "Latitude",
    "Summit", "Keystone", "Brightwater", "Fairhaven", "Stonebridge",
]
COMPANY_B = [
    "Group", "Partners", "Holdings", "Labs", "Systems", "Financial",
    "Trust", "Collective", "Ventures", "Advisory", "Works", "Union",
]

# First/last-name tokens that are also everyday English words. These are
# never replaced (or scanned) as solo words -- only inside multi-token
# name literals -- so "Mark all read" and "I will call" survive intact.
COMMON_WORDS = {
    "will", "mark", "grant", "chase", "hunter", "dawn", "summer", "hope",
    "june", "april", "may", "august", "rose", "ivy", "iris", "jasmine",
    "lily", "daisy", "violet", "heather", "holly", "laurel", "hazel",
    "pearl", "ruby", "amber", "crystal", "ginger", "sunny", "sandy",
    "rusty", "buck", "chip", "cliff", "dean", "don", "gene", "hank",
    "jack", "jay", "joy", "kim", "lee", "max", "pat", "penny", "rob",
    "art", "bill", "bob", "ray", "roy", "victor", "wade", "west",
    "winter", "brook", "brooks", "lane", "reed", "stone", "wood",
    "woods", "park", "hill", "bell", "berry", "bird", "black", "brown",
    "white", "green", "gray", "grey", "young", "long", "short", "small",
    "little", "king", "knight", "bishop", "price", "rich", "gold",
    "silver", "day", "frost", "snow", "rain", "storm", "fox", "wolf",
    "swan", "drake", "martin", "robin", "jean", "carol", "faith",
    "grace", "charity", "melody", "harmony", "destiny", "journey",
    "main", "page", "book", "love", "dear", "major", "minor", "guy",
    "sky", "norm", "colt", "flip", "duke", "earl", "china", "india",
    "georgia", "virginia", "austin", "phoenix", "savannah", "sage",
    "ryder", "rider", "hunter", "archer", "mason", "cooper", "porter",
    "carter", "walker", "turner", "baker", "fisher", "farmer", "smith",
    "banks", "wells", "rivers", "field", "fields", "ford", "post",
    "star", "moon", "june", "olive", "cash", "money", "case", "chance",
}

# Frequent English words. A junky contact name ("Mom and Dad", a vendor,
# a note-to-self entry) can carry ordinary words as name tokens; if those
# were replaced solo, prose everywhere would corrupt ("the" -> a name).
# Any token here is demoted to pair-context, same as COMMON_WORDS.
COMMON_ENGLISH = set("""
the a an and or but nor for yet so if then else when where while as of
in on at by to from with without within into onto over under between
about against during before after above below up down out off again
once here there all any both each few more most other some such no not
only own same than too very just also ever never always often sometimes
now soon later early late today tomorrow yesterday week weeks day days
month months year years hour hours minute minutes second seconds time
times date dates morning afternoon evening night tonight i me my we us
our you your he him his she her it its they them their this that these
those who whom whose which what why how is am are was were be been
being have has had having do does did doing will would shall should can
could may might must need dare used get gets got getting go goes went
gone going come comes came coming make makes made making take takes
took taken taking see sees saw seen seeing know knows knew known say
says said saying think thinks thought want wants wanted give gives gave
given find finds found tell tells told ask asks asked work works worked
working call calls called calling try tries tried leave leaves left use
uses used feel feels felt seem seems seemed keep keeps kept let lets
begin begins began start starts started starting show shows showed
shown hear hears heard play plays played run runs ran running move
moves moved live lives lived believe believed hold holds held bring
brings brought happen happens happened write writes wrote written
sit sits sat sitting stand stands stood lose loses lost pay pays paid
meet meets met include includes included continue set sets put puts
end ends ended follow follows followed stop stops stopped create
created speak speaks spoke read reads reading send sends sent sending
receive received expect expected build builds built stay stays stayed
fall falls fell cut cuts reach reached kill remain remains remained
suggest raise raised pass passed sell sells sold require requires
report reports decide decided pull pulls plan plans planned wait waits
waited delayed delay serve served die died buy buys bought open opens
opened close closes closed walk walks walked win wins won offer offers
offered remember remembered consider considered appear appears
appeared thank thanks thanked good new old great high low big small
large little long short right wrong left early late young important
public bad able best better sure free true false full empty easy hard
strong weak clear dark light real whole certain main only different
possible next last first final past recent ready busy quiet loud fast
slow warm cold hot cool nice fine okay ok fun sorry glad happy sad
worth still even also back well far away ahead behind forward home
house work office school store shop street road city town state
country world thing things way ways case cases part parts place places
point points fact facts group groups number numbers person people man
woman child kids kid family friend friends name names word words line
lines side sides kind kinds head hand hands eye eyes face body life
lives door room area money business job jobs issue issues idea ideas
list lists item items note notes text texts message messages email
emails phone phones photo photos picture pictures video videos link
links document documents file files card cards event events meeting
meetings visit visiting visited trip travel train plane flight bus car
drive dinner lunch breakfast coffee drink food water game games team
teams question questions answer answers reason reasons result results
change changes level levels order orders account accounts service
services price prices cost costs charge charges bill bills check
checks deal deals loop loops thread threads reply replies inbox unread
brief search feed radar triage journal action actions window windows
station status update updates renewal downgrade upgrade summary
overlap remote hook hooks glance action worth land lands landed
mom dad mum mama papa nana grandma grandpa aunt uncle cousin sister
brother son daughter wife husband partner baby babysitter nanny sitter
doctor dentist coach teacher vet plumber cleaner landlord neighbor
pick picks picked picking apply applies applied applying track tracks
tracked tracking rank ranks ranked score scores scored match matches
matched mode modes queue queues room rooms board boards click clicks
clicked tap taps tapped skip skips skipped sort sorts sorted filter
filters filtered draft drafts drafted mark marks marked star stars
starred save saves saved share shares shared view views viewed
delivery management client clients sector sectors domain domains
industry industries commercial technical strategy solutions systems
services partner partners lead leads leadership senior staff principal
director manager engineer architect analyst associate specialist
consultant advisor capital ventures labs studio holdings enterprise
platform product products program programs project projects operations
finance financial research science sales marketing support success
"""
.split())
# Why the block above: these are ORDINARY WORDS OF THIS APP'S OWN PROSE
# that also happen to be real surnames. A contact named Pick made the
# anonymizer rewrite the word "pick" everywhere, and a reading room's
# subtitle came out of a capture reading "Varga a moment, varga a mode".
# That is not a leak — it is the opposite — but it makes an anonymized
# film look broken, which is its own reason not to ship it. Demoting
# them here keeps "Pick Whitfield" mapped as a person while leaving the
# verb alone. Add to this list whenever a capture reads oddly; the
# scanner gate covers the leak direction, nothing else covers this one.

COMMON_WORDS = COMMON_WORDS | COMMON_ENGLISH

_CONSONANTS = "bcdfghjklmnpqrstvwz"
_VOWELS = "aeiou"


def pseudoword(seed, length):
    """Deterministic pronounceable lowercase word of the given length."""
    length = max(3, length)
    out = []
    n = h(f"pw:{seed}")
    for i in range(length):
        if i % 2 == 0:
            out.append(_CONSONANTS[n % len(_CONSONANTS)])
            n //= len(_CONSONANTS)
        else:
            out.append(_VOWELS[n % len(_VOWELS)])
            n //= len(_VOWELS)
        if n < 40:
            n = h(f"pw:{seed}:{i}")
    return "".join(out)


def pseudodigits(seed, length, lead="9"):
    """Deterministic digit string; leading digit fixed so it can't be
    mistaken for a real number's shape."""
    n = h(f"pd:{seed}")
    out = [lead]
    while len(out) < length:
        out.append(str(n % 10))
        n //= 10
        if n < 10:
            n = h(f"pd:{seed}:{len(out)}")
    return "".join(out[:length])


def pick(pool, real, taken, forbidden):
    """Pick a length-similar pool entry for `real`, deterministically.

    Probes a few slots for one not yet `taken` and never in `forbidden`
    (the lowercased set of every real string, so a fake can't collide
    with a real identity). Falls back to a capitalized pseudoword.
    """
    target = len(real)
    ranked = sorted(pool, key=lambda n: (abs(len(n) - target), n))
    window = [n for n in ranked if abs(len(n) - target) <= 1] or ranked[:24]
    base = h(f"pick:{real}")
    for k in range(min(12, len(window))):
        cand = window[(base + k * 7) % len(window)]
        if cand.lower() in forbidden:
            continue
        if cand not in taken:
            taken.add(cand)
            return cand
    for k in range(len(window)):  # accept reuse before pseudoword
        cand = window[(base + k) % len(window)]
        if cand.lower() not in forbidden:
            return cand
    return pseudoword(real, target).capitalize()


def pick_company(real, taken, forbidden):
    """Two-word synthetic company name, length-matched to the real one."""
    target = len(real)
    cands = sorted((a + " " + b for a in COMPANY_A for b in COMPANY_B),
                   key=lambda n: (abs(len(n) - target), n))
    base = h(f"company:{real}")
    window = cands[:40]
    for k in range(min(16, len(window))):
        cand = window[(base + k * 11) % len(window)]
        if cand.lower() in forbidden:
            continue
        if cand not in taken:
            taken.add(cand)
            return cand
    return window[base % len(window)]
