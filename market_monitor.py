#!/usr/bin/env python3
"""Hourly wide-market Parr/Badger discovery monitor.

Near-real-time charity monitors remain unchanged. This slower layer scans broad
marketplace/dealer feeds and rotates exact Parr/Badger searches across eBay UK,
AbeBooks and Biblio. Existing stock is silently baselined per feed/query.
"""
from __future__ import annotations

import argparse, html, json, os, re, sys, urllib.parse
from pathlib import Path
from typing import Any

import charity_monitor as charity
import ebay_api
import external_monitor as ext
from parr_badger_runner import load_master, match_listing, normalize

FEEDS: list[dict[str, Any]] = [
    {"id":"ebay_photobook","name":"eBay UK photobook","kind":"ebay","url":"https://www.ebay.co.uk/sch/i.html?_nkw=photobook&_sop=10&_ipg=240"},
    {"id":"ebay_photo_book","name":"eBay UK photography books","kind":"ebay","url":"https://www.ebay.co.uk/sch/i.html?_nkw=photography+book&_sop=10&_ipg=240"},
    {"id":"ebay_antiquarian_photo","name":"eBay UK antiquarian photography","kind":"ebay","url":"https://www.ebay.co.uk/sch/i.html?_nkw=photography&_sacat=29223&_sop=10&_ipg=240"},
    {"id":"abe_photobook","name":"AbeBooks UK recent photobooks","kind":"abebooks","url":"https://www.abebooks.co.uk/servlet/SearchResults?kn=photobook&sortby=17&ds=100"},
    {"id":"abe_photography","name":"AbeBooks UK recent photography","kind":"abebooks","url":"https://www.abebooks.co.uk/servlet/SearchResults?kn=photography&sortby=17&ds=100"},
    {"id":"tpg_new","name":"The Photographers' Gallery new arrivals","kind":"shopify_collection","base":"https://bookshop.thephotographersgallery.org.uk","handle":"new-arrivals-1"},
    {"id":"photobookstore","name":"Photobookstore","kind":"shopify","base":"https://photobookstore.co.uk"},
    {"id":"village","name":"Village Books","kind":"shopify","base":"https://villagebooks.co"},
    {"id":"setanta","name":"Setanta Books","kind":"shopify","base":"https://www.setantabooks.com"},
]
TARGET_MARKETS = ("ebay", "abebooks", "biblio")
DEFAULT_TARGETS = 12
ABE_ID = re.compile(r"(?:[?&]bi=|/)(\d{8,15})(?:/bd|[&#/?]|$)", re.I)
BIBLIO_ID = re.compile(r"/d/(\d{6,15})(?:[/?#]|$)", re.I)
EDITION_SIGNALS = (
    "first edition","1st edition","first printing","1st printing","first impression",
    "signed","inscribed","association copy","limited edition","numbered",
    "artist proof","artist's proof","with print","original print","slipcase",
    "slip case","glassine","acetate","dust jacket","dustjacket",
)


def utc_now() -> str:
    return ext.utc_now()


def parse_gbp(text: Any) -> float | None:
    return ext.parse_price(str(text or ""))


def parse_abe_or_biblio(page: str, source: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    patt = ABE_ID if kind == "abebooks" else BIBLIO_ID
    prefix = "abebooks" if kind == "abebooks" else "biblio"
    base = "https://www.abebooks.co.uk" if kind == "abebooks" else "https://www.biblio.com"
    for m in ext.ANCHOR_RE.finditer(page):
        href = html.unescape(m.group("href") or "")
        ident = patt.search(href)
        if not ident:
            continue
        title = ext.anchor_title(m)
        if not title:
            continue
        item_id = ident.group(1)
        ctx = ext.context_from_html(page, m.start(), 2300)
        row = {
            "key":f"{prefix}:{item_id}", "external_id":item_id,
            "source_id":source["id"], "source_name":source["name"],
            "title":title[:350], "url":urllib.parse.urljoin(base, href),
            "price_gbp":parse_gbp(ctx), "context":ctx,
        }
        old = found.get(row["key"])
        if old is None or len(row["title"]) > len(old.get("title", "")):
            found[row["key"]] = row
    return list(found.values())


def shopify(source: dict[str, Any]) -> list[dict[str, Any]]:
    base = source["base"].rstrip("/")
    if source["kind"] == "shopify_collection":
        url = f"{base}/collections/{urllib.parse.quote(source['handle'])}/products.json?limit=250"
    else:
        url = f"{base}/products.json?limit=250"
    payload = charity.request_json(url)
    products = payload.get("products")
    if not isinstance(products, list):
        raise RuntimeError("Shopify response has no products list")
    out = []
    for p in products:
        if not isinstance(p, dict) or p.get("id") is None:
            continue
        variants = p.get("variants") if isinstance(p.get("variants"), list) else []
        if variants and not any(isinstance(v, dict) and v.get("available") for v in variants):
            continue
        prices = []
        for v in variants:
            try: prices.append(float(v.get("price")))
            except (TypeError, ValueError, AttributeError): pass
        tags = p.get("tags")
        tags = " ".join(map(str, tags)) if isinstance(tags, list) else str(tags or "")
        handle = str(p.get("handle") or "")
        out.append({
            "key":f"{source['id']}:{p['id']}", "external_id":str(p["id"]),
            "source_id":source["id"], "source_name":source["name"],
            "title":str(p.get("title") or "Untitled product"),
            "url":f"{base}/products/{handle}" if handle else base,
            "price_gbp":round(min(prices),2) if prices else None,
            "context":charity.strip_html(p.get("body_html"))[:1600],
            "vendor":p.get("vendor"), "tags":tags,
        })
    return out


def fetch_feed(source: dict[str, Any]) -> list[dict[str, Any]]:
    kind = source["kind"]
    if kind == "ebay_api":
        return ebay_api.search_listings(
            str(source["query"]),
            source,
            limit=int(source.get("limit") or 200),
            category_ids=str(source.get("category_ids") or "") or None,
            fixed_price_only=bool(source.get("fixed_price_only", True)),
        )
    if kind == "ebay":
        return ext.parse_ebay({"id":source["id"],"source_name":source["name"],"url":source["url"]}, ext.request_html(source["url"]))
    if kind in {"abebooks","biblio"}:
        rows = parse_abe_or_biblio(ext.request_html(source["url"]), source, kind)
        if not rows: raise RuntimeError("page fetched but no listings parsed")
        return rows
    return shopify(source)


def master_rows() -> list[dict[str, Any]]:
    seen, rows = set(), []
    for r in load_master():
        key = (normalize(r.get("Contributor")), normalize(r.get("Title")))
        if not key[1] or key in seen: continue
        seen.add(key); rows.append(r)
    rows.sort(key=lambda r:(0 if str(r.get("Search tier") or "").upper()=="CORE" else 1, normalize(r.get("Contributor")), normalize(r.get("Title"))))
    return rows


def contributor_name(value: Any) -> str:
    return re.split(r"\s*(?:/|\band\b|\bwith\b|\btext by\b)\s*", str(value or ""), maxsplit=1, flags=re.I)[0].strip()


def target_url(market: str, row: dict[str, Any]) -> tuple[str,str]:
    title, author = str(row.get("Title") or "").strip(), contributor_name(row.get("Contributor"))
    query = f"{author} {title}".strip()
    if market in {"ebay", "ebay_api"}:
        return "https://www.ebay.co.uk/sch/i.html?" + urllib.parse.urlencode({"_nkw":query,"_sop":"10","_ipg":"60"}), query
    if market == "abebooks":
        p = {"tn":title,"sortby":"17","ds":"50"}
        if author: p["an"] = author
        return "https://www.abebooks.co.uk/servlet/SearchResults?" + urllib.parse.urlencode(p), query
    p = {"title":title,"author":author,"keyisbn":"","format":"any","stage":"1","order":"datedesc","pageper":"50"}
    return "https://www.biblio.com/search.php?" + urllib.parse.urlencode(p), query


def fetch_target(market: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    url, query = target_url(market, row)
    market_name = "eBay UK API" if market == "ebay_api" else market.title()
    source = {"id":f"target_{market}","name":f"{market_name} targeted Parr/Badger search","url":url}
    if market == "ebay_api":
        items = ebay_api.search_listings(query, source, limit=50, fixed_price_only=True)
    else:
        page = ext.request_html(url)
    if market == "ebay":
        items = ext.parse_ebay({"id":source["id"],"source_name":source["name"],"url":url}, page)
    elif market != "ebay_api":
        items = parse_abe_or_biblio(page, source, market)
    for item in items: item["target_query"] = query
    return items


def matches(item: dict[str, Any]) -> list[dict[str, Any]]:
    return match_listing(title=item.get("title"), author=item.get("vendor"), description=item.get("context"), extra_text=item.get("tags"), limit=3)


def signals(item: dict[str, Any]) -> list[str]:
    text = normalize(" ".join(str(item.get(k) or "") for k in ("title","context","tags")))
    return [s for s in EDITION_SIGNALS if normalize(s) in text]


def priority(item: dict[str, Any]) -> int:
    ms = item.get("parr_badger_matches") or []
    best = ms[0] if ms else {}
    score = int(best.get("score") or 0) + (12 if str(best.get("search_tier") or "").upper()=="CORE" else 0) + min(12,3*len(signals(item)))
    price = item.get("price_gbp")
    if isinstance(price,(int,float)): score += 12 if price<=25 else 8 if price<=50 else 3 if price<=100 else 0
    return score


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists(): return {"version":1,"feeds":{},"queries":{},"cursor":0}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data,dict): raise RuntimeError("market state is not an object")
    for k,v in (("feeds",{}),("queries",{}),("cursor",0)): data.setdefault(k,v)
    return data


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value,indent=2,ensure_ascii=False,sort_keys=True)+"\n",encoding="utf-8")


def output(name: str, value: Any) -> None:
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"],"a",encoding="utf-8") as f: f.write(f"{name}={value}\n")


def remember(bucket: dict[str, Any], items: list[dict[str, Any]], limit: int) -> None:
    now = [str(x.get("key") or "") for x in items if x.get("key")]
    old = [str(x) for x in bucket.get("seen",[]) if x]
    bucket["seen"] = (now + [x for x in old if x not in now])[:limit]


def process_feeds(state: dict[str, Any], stamp: str) -> tuple[list[dict[str, Any]],list[str],int]:
    new, failures, ok = [], [], 0
    for src in FEEDS:
        try: items = fetch_feed(src)
        except Exception as exc:
            failures.append(f"{src['name']}: {exc}"); print("WARNING:",failures[-1],file=sys.stderr); continue
        ok += 1; bucket = state["feeds"].get(src["id"])
        if not isinstance(bucket,dict) or not bucket.get("initialized"):
            bucket={"initialized":True,"first_successful_fetch":stamp,"seen":[]}; state["feeds"][src["id"]]=bucket
            remember(bucket,items,5000); bucket["last_successful_fetch"]=stamp; bucket["last_count"]=len(items)
            print(f"{src['name']}: baseline {len(items)}"); continue
        seen=set(bucket.get("seen",[])); count=0
        for item in items:
            if item["key"] in seen: continue
            ms=matches(item)
            if ms: item["parr_badger_matches"]=ms; new.append(item); count+=1
        remember(bucket,items,5000); bucket["last_successful_fetch"]=stamp; bucket["last_count"]=len(items)
        print(f"{src['name']}: {len(items)} visible, {count} new PB match(es)")
    return new,failures,ok


def process_targets(state: dict[str, Any], stamp: str, n: int) -> tuple[list[dict[str,Any]],list[str],int,str]:
    rows=master_rows()
    if not rows: return [],["targeted search: empty master"],0,"empty master"
    cursor=int(state.get("cursor") or 0)%len(rows); count=max(1,min(n,len(rows))); selected=[rows[(cursor+i)%len(rows)] for i in range(count)]
    new,failures,ok=[],[],0
    for row in selected:
        rk=f"{normalize(row.get('Contributor'))}|{normalize(row.get('Title'))}"
        for market in TARGET_MARKETS:
            bk=f"{market}|{rk}"
            try: items=fetch_target(market,row)
            except Exception as exc: failures.append(f"{market} target {row.get('Contributor')} / {row.get('Title')}: {exc}"); continue
            ok+=1; bucket=state["queries"].get(bk)
            if not isinstance(bucket,dict) or not bucket.get("initialized"):
                bucket={"initialized":True,"first_successful_fetch":stamp,"seen":[]}; state["queries"][bk]=bucket
                remember(bucket,items,80); bucket["last_successful_fetch"]=stamp; continue
            seen=set(bucket.get("seen",[]))
            for item in items:
                if item["key"] in seen: continue
                ms=matches(item)
                if ms: item["parr_badger_matches"]=ms; new.append(item)
            remember(bucket,items,80); bucket["last_successful_fetch"]=stamp
    state["cursor"]=(cursor+count)%len(rows)
    note=f"records {cursor+1}-{cursor+count} of {len(rows)}; next {state['cursor']+1}"
    return new,failures,ok,note


def dedupe(items: list[dict[str,Any]]) -> list[dict[str,Any]]:
    out={}
    for item in items:
        key=str(item.get("key") or item.get("url") or "")
        if key and (key not in out or priority(item)>priority(out[key])): out[key]=item
    return sorted(out.values(),key=priority,reverse=True)


def issue(items: list[dict[str,Any]], stamp: str, failures: list[str], note: str) -> tuple[str,str]:
    lines=["## Comprehensive Parr/Badger market discovery","",f"Detected at **{stamp}**.","","Only newly seen listings that matched the local Parr/Badger master are shown. Verify edition, printing, completeness, condition and all-in UK price before purchase.","",f"Targeted sweep: {note}",""]
    for item in items:
        best=(item.get("parr_badger_matches") or [{}])[0]
        lines += [f"### {item.get('title') or 'Untitled listing'}","",f"- **Source:** {item.get('source_name')}",f"- **Listing:** {item.get('url')}"]
        if isinstance(item.get("price_gbp"),(int,float)): lines.append(f"- **Observed price:** £{item['price_gbp']:.2f}")
        if item.get("target_query"): lines.append(f"- **Search query:** `{item['target_query']}`")
        lines.append(f"- **Best Parr/Badger match:** V{str(best.get('volumes') or '?').replace(';','/')} {str(best.get('search_tier') or '').upper()} | {best.get('contributor')}, *{best.get('title')}* | {best.get('score')}/100")
        if best.get("pb_refs"): lines.append(f"- **Parr/Badger refs:** {best['pb_refs']}")
        if signals(item): lines.append(f"- **Edition clues:** {', '.join(signals(item))}")
        lines.append(f"- **Discovery priority:** {priority(item)}")
        if item.get("context"): lines.append(f"- **Listing context:** {str(item['context'])[:1200]}")
        lines.append("")
    if failures: lines += ["### Source warnings",""]+[f"- {x}" for x in failures[:20]]+[""]
    return f"EXTERNAL_NEW: {len(items)} Parr/Badger market match{'es' if len(items)!=1 else ''}","\n".join(lines).rstrip()+"\n"


def self_test() -> int:
    if len(load_master())<600 or len(master_rows())<600: return 1
    e='<a href="https://www.ebay.co.uk/itm/foo/123456789012">Robert Frank The Americans</a> £25.00'
    if not ext.parse_ebay({"id":"x","source_name":"x","url":"x"},e): return 1
    a='<a href="/servlet/BookDetailsPL?bi=32073032306">The Americans - Robert Frank</a> £40.00'
    if not parse_abe_or_biblio(a,{"id":"x","name":"x"},"abebooks"): return 1
    b='<a href="https://www.biblio.com/book/the-americans-robert-frank/d/1234567890">The Americans - Robert Frank</a>'
    if not parse_abe_or_biblio(b,{"id":"x","name":"x"},"biblio"): return 1
    if not match_listing(title="Robert Frank The Americans Grove Press"): return 1
    print(f"Market monitor self-test OK: {len(load_master())} master records")
    return 0


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--state",default="data/market_state.json"); ap.add_argument("--runtime-dir",default="runtime/market"); ap.add_argument("--targets-per-run",type=int,default=DEFAULT_TARGETS); ap.add_argument("--self-test",action="store_true"); args=ap.parse_args()
    if args.self_test: return self_test()
    state=load_state(Path(args.state)); runtime=Path(args.runtime_dir); runtime.mkdir(parents=True,exist_ok=True); stamp=utc_now()
    a,af,ao=process_feeds(state,stamp); b,bf,bo,note=process_targets(state,stamp,args.targets_per_run); failures=af+bf
    if ao+bo==0: raise RuntimeError("all comprehensive market requests failed")
    new=dedupe(a+b); state.update({"last_successful_run":stamp,"last_successful_requests":ao+bo,"last_failures":failures[-50:]})
    write_json(runtime/"proposed-state.json",state); write_json(runtime/"latest-snapshot.json",{"checked_at":stamp,"new_matches":new,"failures":failures,"targeted":note})
    if new:
        title,body=issue(new,stamp,failures,note); (runtime/"issue-title.txt").write_text(title+"\n",encoding="utf-8"); (runtime/"issue-body.md").write_text(body,encoding="utf-8")
    output("new_count",len(new)); output("state_changed","true"); output("successful_requests",ao+bo); output("failed_requests",len(failures))
    print(f"Market sweep: {ao}/{len(FEEDS)} feeds + {bo} targeted requests succeeded; {len(new)} new match(es). {note}")
    return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc: print(f"ERROR: {exc}",file=sys.stderr); raise
