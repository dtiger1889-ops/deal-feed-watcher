# What the feeds taught

Each section is something that went wrong in a version of this watcher that ran for months against live feeds, what it cost, and what replaced it. The code comments say what the code does; this is the why.

## The cheapest number in a deal title is usually not the price

The first price extractor took the smallest dollar figure in the title and description. Deal copy is full of other dollar figures: shipping charges, "free shipping over $35", "$10 off", "under $50". A measurement on the stored corpus found most self-priced titles carried the wrong price, every error biased low, so deals looked cheaper than they were and price caps let the wrong things through.

What replaced it: the price the title itself claims ("for $X", "only $X", a "($X)" the parser added) wins; otherwise a figure only counts if the words around it are not shipping, coupon or threshold language. If an original price is stated, the sale price is the largest clean figure below it.

## Descriptions make bad categories

Categories used to be chosen from the title and description together. Roundup posts list many unrelated products in the description, so a warehouse-club sale became "electronics" because its blurb named a tablet, and shoe roundups landed there off a streaming-box mention. Hundreds of rows were misfiled.

What replaced it: the category comes from the title only. Watchlist matching still reads the description, because that is how a roundup that names something you want earns its match tag. A short list of anti-words stops a category word from misfiring (a cartoon mouse is not a computer mouse).

## Most deals never state a discount

Some feeds give a list price, most do not, and Woot's titles never say "was $X" even though its API carries a ListPrice field. Without a discount, a "hot only if at least N% off" rule could never fire for most of the corpus.

What replaced it, in two parts: Woot's ListPrice is read into the description so the normal parser finds it; and for everything else, similar listings are grouped (source plus the most distinctive title words, with pack sizes and capacities kept apart so a 4-pack and a 12-pack are different products) and a new price is compared against the group's median. The digest labels which kind of discount it is, because "the feed says 40% off" and "40% below what this usually sells for" are different claims.

## A running average is not a median

The first baseline kept a running mean in a column named median_price, with a spread figure that was never updated. It never converged to a median, and one bad price skewed a group for good.

What replaced it: each group's median and median absolute deviation are recomputed exactly from the group's stored rows whenever one of them changes price. Groups that are too small or too spread out are not trusted for scoring at all.

## Repeat offers have to be rescored

When a feed returned an offer already in the database, only its price and description were refreshed. A repriced offer kept a "hot" tier it no longer deserved, and editing the watchlist did nothing to deals the feeds kept returning.

What replaced it: every returned offer is rescored against the current watchlist on every poll. Price drops are measured against the lowest price the offer has ever shown, not against yesterday, because some sources move prices up and down a little every day and a day-over-day rule reported the same offer as a fresh drop again and again.

## A missing feed item is not a sold-out deal

The Woot feed is not a complete live catalog: a live offer can be absent from it while its page is still selling. Treating absence as expiry would have hidden live deals.

What replaced it: absence is never used. Woot's getoffers endpoint answers sold-out or live per offer id, and the watcher asks it about exactly the rows the page will show. One trap on the way: the feed calls the id OfferId and getoffers calls it Id, and reading the wrong name silently marked nothing as sold out.

## Telemetry has to describe the page that renders

The tuning report counted unmatched rows per category as "shown", including categories the page only displays when they match. It reported more than a thousand phantom rows of noise that no one could see.

What replaced it: one function defines what each dashboard section shows, and the page, the tuning report and the Woot availability check all use it.

## A CSS rule can break a filter

The phone layout set `display` on table rows, which outranks the browser's default rule for the `hidden` attribute. The filters were setting `hidden` correctly and nothing on the phone changed. One `[hidden] { display: none !important; }` line fixed it.

## Some stores cannot be watched directly

Store product pages behind Cloudflare refused every scripted request tried while building this, whatever the user agent. Feeds and official APIs were the inputs that held up, which is why the watcher leans on them; the one HTML source it keeps is a deal site, read with the expectation that a redesign will break it.
