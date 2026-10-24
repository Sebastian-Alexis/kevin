"""Source D: web grounding from live pages via headless Chromium. Labels come from the DOM, so they're exact.

Per page view it emits:
  - click samples for visible, unoccluded links / buttons / inputs / tabs (one frame, many samples)
  - type samples: focus a text field (no submit), screenshot with the caret in it, label {"type": ...}
  - a scroll sample when the page is scrollable
Read-only: never submits forms or clicks anything. Runs fine headless on the GPU box.

  playwright install chromium
  python datagen/web_grounding.py --urls datagen/web_urls.txt --pages-per-site 3 -n 5000
"""
import argparse
import asyncio
import io
import random
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Shard, bbox_to1000  # noqa: E402

VIEWPORTS = [(1280, 720), (1280, 720), (1280, 800), (1440, 900), (1366, 768), (1536, 864)]

# Returns visible, unoccluded interactive elements with a usable label.
EXTRACT_JS = r"""
() => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=searchbox],summary';
  const vw = innerWidth, vh = innerHeight, out = [], seen = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6 || r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) continue;
    if (r.width * r.height > vw * vh * 0.25) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity < 0.1) continue;
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1), cy = Math.min(Math.max(r.top + r.height / 2, 1), vh - 1);
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !(hit === el || el.contains(hit) || hit.contains(el))) continue;   // occluded
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'file'].includes(type)) continue;
    let label = clean(el.getAttribute('aria-label')) || clean(el.innerText) || clean(el.getAttribute('title')) ||
                clean(el.getAttribute('placeholder')) || clean(el.getAttribute('alt')) ||
                clean(el.querySelector && el.querySelector('img[alt]')?.getAttribute('alt')) ||
                (tag === 'input' && ['submit', 'button'].includes(type) ? clean(el.value) : '');
    if (!label || label.length > 60 || !/[A-Za-z0-9].*[A-Za-z0-9]/.test(label)) continue;
    const key = label + '|' + Math.round(cx / 20) + '|' + Math.round(cy / 20);
    if (seen.has(key)) continue; seen.add(key);
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'summary' ? 'button' : tag);
    const textlike = (tag === 'input' && ['', 'text', 'search', 'email', 'url', 'tel'].includes(type)) ||
                     tag === 'textarea' || role === 'searchbox' || role === 'combobox';
    out.push({label, role, type, textlike, placeholder: clean(el.getAttribute('placeholder')),
              name: clean(el.getAttribute('name')), box: [Math.max(r.left, 0), Math.max(r.top, 0),
              Math.min(r.right, vw), Math.min(r.bottom, vh)], idx: [...document.querySelectorAll(sel)].indexOf(el)});
  }
  return {els: out, scrollable: document.documentElement.scrollHeight > vh * 1.3,
          title: document.title, links: [...document.querySelectorAll('a[href]')].map(a => a.href).slice(0, 300)};
}
"""

ROLE_WORD = {"link": "link", "button": "button", "tab": "tab", "menuitem": "menu item", "checkbox": "checkbox",
             "radio": "option", "switch": "toggle", "select": "dropdown", "combobox": "field", "searchbox": "search field",
             "input": "field", "textarea": "text box"}
SEARCH_TERMS = ["weather tomorrow", "python list comprehension", "best pizza near me", "flights to tokyo",
                "how to center a div", "cal hacks 2026", "raspberry pi zero 2 w", "usb hid descriptor",
                "quarterly report template", "react useEffect cleanup", "noise cancelling headphones",
                "san francisco events this weekend", "unit converter", "golden gate park"]
FIELD_TEXT = {"email": ["alex.chen@example.com", "jordan@test.dev"], "url": ["https://example.com"],
              "tel": ["5105550123"], "name": ["Alex Chen", "Jordan Lee"]}


def click_intent(el, rng, site):
    word = ROLE_WORD.get(el["role"], "")
    label = el["label"]
    if el["textlike"]:
        what = el["placeholder"] or label
        opts = [f"Click the '{what}' field", f"Click into the {word or 'field'} labeled '{what}'"]
        if "search" in (what + el["name"] + el["role"]).lower():
            opts.append("Click the search field")
    else:
        opts = [f"Click the '{label}' {word}".rstrip(), f"Click '{label}'", f"Open the '{label}' {word}".rstrip()]
        if el["role"] == "link":
            opts.append(f"Click the {label} link on {site}")
    return rng.choice(opts)


def type_text(el, rng):
    hint = (el["type"] + " " + el["name"] + " " + el["placeholder"]).lower()
    for k, vals in FIELD_TEXT.items():
        if k in hint:
            return rng.choice(vals)
    return rng.choice(SEARCH_TERMS)


async def shoot(page):
    return Image.open(io.BytesIO(await page.screenshot(type="png")))


async def visit(ctx, url, shard, rng, args, stats):
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        info = await page.evaluate(EXTRACT_JS)
        els = info["els"]
        if len(els) < 3:
            return []
        img = await shoot(page)
        w, h = img.size
        frame = shard.add_frame(img)
        site = urlparse(url).netloc.removeprefix("www.")
        for el in rng.sample(els, min(len(els), args.per_frame)):
            box = bbox_to1000(*el["box"], w, h)
            shard.add_step(frame, click_intent(el, rng, site),
                           {"action": "click", "x": (box[0] + box[2]) // 2, "y": (box[1] + box[3]) // 2},
                           bbox=box, app="browser", os="web", url=url, role=el["role"])
            stats["click"] += 1

        text_els = [e for e in els if e["textlike"]]
        if text_els and rng.random() < args.type_rate:
            el = rng.choice(text_els)
            handle = (await page.query_selector_all(
                'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],'
                '[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=searchbox],summary'))[el["idx"]]
            await handle.focus()
            await page.wait_for_timeout(200)
            text = type_text(el, rng)
            fimg = await shoot(page)
            fframe = shard.add_frame(fimg)
            what = el["placeholder"] or el["label"]
            intent = rng.choice([f"Type '{text}' into the {what} field", f"Type '{text}'",
                                 f"Enter '{text}' in the {what} box"])
            shard.add_step(fframe, intent, {"action": "type", "text": text}, app="browser", os="web", url=url)
            stats["type"] += 1
            await handle.evaluate("e => e.blur()")

        if info["scrollable"] and rng.random() < args.scroll_rate:
            ticks = -rng.choice([3, 5, 8])
            shard.add_step(frame, rng.choice(["Scroll down", "Scroll down the page", f"Scroll down to see more of {site}"]),
                           {"action": "scroll", "x": 500, "y": 550, "ticks": ticks}, app="browser", os="web", url=url)
            stats["scroll"] += 1
        same = [l for l in info["links"] if urlparse(l).netloc == urlparse(url).netloc and l.split("#")[0] != url]
        return same
    except Exception as e:  # sites fail all the time; keep going
        stats["errors"] += 1
        print(f"  ! {url}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        return []
    finally:
        await page.close()


async def run(args):
    from playwright.async_api import async_playwright

    rng = random.Random(args.seed)
    urls = [u.strip() for u in Path(args.urls).read_text().splitlines() if u.strip() and not u.startswith("#")]
    rng.shuffle(urls)
    stats = {"click": 0, "type": 0, "scroll": 0, "errors": 0}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        with Shard("web", seed=args.seed, os="web") as shard:
            for site in urls:
                if shard.n_steps >= args.n:
                    break
                vw, vh = rng.choice(VIEWPORTS)
                ctx = await browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1,
                                                color_scheme=rng.choice(["light", "light", "dark"]), locale="en-US")
                queue, done = [site], 0
                while queue and done < args.pages_per_site and shard.n_steps < args.n:
                    url = queue.pop(0)
                    links = await visit(ctx, url, shard, rng, args, stats)
                    done += 1
                    rng.shuffle(links)
                    queue.extend(urljoin(url, l) for l in links[:5])
                await ctx.close()
                print(f"{site}: total {shard.n_steps} samples {stats}", flush=True)
        await browser.close()
    print(f"wrote {shard.n_steps} samples to {shard.dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default=str(Path(__file__).with_name("web_urls.txt")))
    ap.add_argument("-n", type=int, default=5000)
    ap.add_argument("--pages-per-site", type=int, default=3)
    ap.add_argument("--per-frame", type=int, default=8, help="click samples per screenshot")
    ap.add_argument("--type-rate", type=float, default=0.5)
    ap.add_argument("--scroll-rate", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
