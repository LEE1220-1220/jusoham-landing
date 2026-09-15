# -*- coding: utf-8 -*-
"""SEO 주소 일관성 검사기 — canonical·사이트맵·링크가 같은 주소 형식을 쓰는지 확인한다.

왜 있나 (2026-09-15 주소로.com 사고)
  Netlify Pretty URLs 가 페이지 안 링크의 .html 을 떼서 실제 링크는 /c/ott 인데
  canonical·og:url·JSON-LD·sitemap 은 /c/ott.html 이었다. 구글은 링크로 찾은 /c/ott 를
  "표준은 따로 있다"며 빼고, /c/ott.html 은 가리키는 링크가 없어 미뤘다 → 100페이지 중 32개만 색인.
  이 검사기는 그 불일치를 배포 전에 잡는다. 오류가 하나라도 있으면 종료코드 1.

사용
  python seo_guard.py <배포폴더> <https://도메인>             배포 폴더 전체 검사
  python seo_guard.py <배포폴더> <https://도메인> --partial   생성기 출력처럼 일부만 있는 폴더(사이트맵에 있는데 파일이 없으면 건너뜀)
  python seo_guard.py --live <https://도메인> [<https://도메인> ...]   라이브 사이트(사이트맵 전 URL 을 받아서 확인)

검사 항목 (오류 = 배포 중단)
  · 페이지: canonical 없음 · canonical 에 .html · canonical 호스트가 다름 · canonical 이 자기 주소가 아님
            · og:url 이 canonical 과 다름 · 같은 도메인의 .html 절대주소(JSON-LD·hreflang 포함)
  · 사이트맵: .html 주소 · 중복 · 다른 호스트 · 리다이렉트/비200(라이브) · 파일의 실제 주소와 형식이 다름
  · 링크: 페이지 안 링크가 사이트맵과 .html 유무만 다른 주소를 가리킴
  (경고 = 참고) 끝 슬래시만 다른 링크(/app ↔ /app/), 사이트맵에 빠진 페이지, 상대 .html 링크
"""
import io
import os
import re
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 seo_guard"
# 검사하지 않는 파일 — 소유확인·404·관리자
SKIP = re.compile(r"(^|/)(google[0-9a-f]+\.html|404\.html)$|admin|관리자", re.I)
VERIFY = re.compile(r"/google[0-9a-f]+\.html$", re.I)

RE_TAG_CANON = re.compile(r"<link\b[^>]*\brel=[\"']canonical[\"'][^>]*>", re.I)
RE_TAG_OGURL = re.compile(r"<meta\b[^>]*\bproperty=[\"']og:url[\"'][^>]*>", re.I)
RE_NOINDEX = re.compile(r"<meta\b[^>]*\bname=[\"']robots[\"'][^>]*noindex", re.I)
RE_HREF = re.compile(r"<a\b[^>]*?\bhref=[\"']([^\"']+)[\"']", re.I)
RE_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


class Result:
    def __init__(self):
        self.err = {}    # 종류 → [상세]
        self.warn = {}

    def e(self, kind, detail=""):
        self.err.setdefault(kind, []).append(detail)

    def w(self, kind, detail=""):
        self.warn.setdefault(kind, []).append(detail)

    def n_err(self):
        return sum(len(v) for v in self.err.values())


def attr(tag, name):
    m = re.search(r"\b%s=[\"']([^\"']*)[\"']" % name, tag, re.I)
    return m.group(1).strip() if m else None


def clean(u):
    """쿼리·프래그먼트를 뗀 주소."""
    s = urlsplit(u)
    return "%s://%s%s" % (s.scheme, s.netloc.lower(), s.path or "/")


def form_key(u):
    """.html·index.html·끝 슬래시를 무시한 비교용 키 — 같은 페이지의 다른 표기를 묶는다."""
    s = urlsplit(u)
    p = s.path or "/"
    if p.endswith("/index.html"):
        p = p[:-10]
    elif p.endswith(".html"):
        p = p[:-5]
    return s.netloc.lower() + (p.rstrip("/") or "/")


def file_url(base, rel):
    """배포 폴더 안 파일 → Netlify 가 서빙하는 확장자 없는 주소."""
    if rel == "index.html":
        return base + "/"
    if rel.endswith("/index.html"):
        return base + "/" + rel[:-10]
    return base + "/" + rel[:-5]


def check_page(r, html, url, host, local):
    """페이지 하나. 반환: canonical(없으면 None, noindex 면 'NOINDEX')."""
    if RE_NOINDEX.search(html):
        return "NOINDEX"
    path = urlsplit(url).path or "/"
    t = RE_TAG_CANON.search(html)
    canon = attr(t.group(0), "href") if t else None
    if not canon:
        r.e("canonical 없음", path)
    else:
        if ".html" in canon:
            r.e("canonical 에 .html 이 붙어 있음", "%s → %s" % (path, canon))
        elif urlsplit(canon).netloc.lower() != host:
            r.e("canonical 이 다른 호스트", "%s → %s" % (path, canon))
        elif canon != url:
            r.e("canonical 이 자기 주소가 아님", "%s → %s (기대값 %s)" % (path, canon, url))
    t = RE_TAG_OGURL.search(html)
    og = attr(t.group(0), "content") if t else None
    if og and canon and og != canon:
        r.e("og:url 이 canonical 과 다름", "%s: og %s / canonical %s" % (path, og, canon))
    bad = [u for u in re.findall(r"https?://%s(/[^\"'\s<>()\\]*?\.html)(?=[\"'\s<>?#)\\]|$)"
                                 % re.escape(host), html, re.I)
           if not VERIFY.search(u)]
    if bad:
        r.e("같은 도메인의 .html 절대주소(JSON-LD·hreflang 등)", "%s: %d곳, 예 %s" % (path, len(bad), bad[0]))
    if local:
        rel_html = [h for h in RE_HREF.findall(html)
                    if h.endswith(".html") and "://" not in h and not VERIFY.search("/" + h)]
        if rel_html:
            r.w("링크에 .html (Netlify 가 떼 주지만 형식을 맞출 것)", "%s: %d곳, 예 %s" % (path, len(rel_html), rel_html[0]))
    return canon


def internal_links(html, page_url, host):
    for h in RE_HREF.findall(html):
        if h.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        a = urljoin(page_url, h)
        if urlsplit(a).netloc.lower() == host:
            yield clean(a)


def check_links(r, pages, locs):
    """pages: [(주소, html)] · 링크가 사이트맵과 표기만 다른 주소를 가리키는지."""
    exact = set(locs)
    by_key = {form_key(u): u for u in locs}
    host = urlsplit(locs[0]).netloc.lower() if locs else ""
    seen = {}
    for url, html in pages:
        for a in internal_links(html, url, host):
            if a in exact or VERIFY.search(a):
                continue
            loc = by_key.get(form_key(a))
            if loc:
                seen.setdefault((a, loc), 0)
                seen[(a, loc)] += 1
    for (a, loc), n in sorted(seen.items(), key=lambda x: -x[1]):
        d = "%s ×%d → 사이트맵은 %s" % (urlsplit(a).path, n, urlsplit(loc).path)
        if a.endswith(".html") or loc.endswith(".html"):
            r.e("링크와 사이트맵의 주소 형식이 다름(.html 유무)", d)
        else:
            r.w("링크와 사이트맵이 끝 슬래시만 다름(301 한 번 거침)", d)


def check_sitemap_list(r, locs, host):
    if not locs:
        r.e("사이트맵이 비어 있음")
        return
    dup = sorted(set(u for u in locs if locs.count(u) > 1))
    for u in dup:
        r.e("사이트맵 중복", "%s ×%d" % (u, locs.count(u)))
    for u in locs:
        if ".html" in u:
            r.e("사이트맵에 .html 주소", u)
        if urlsplit(u).netloc.lower() != host:
            r.e("사이트맵에 다른 호스트", u)


# ── 배포 폴더 ────────────────────────────────────────────────
def check_folder(folder, base, partial=False):
    r = Result()
    base = base.rstrip("/")
    host = urlsplit(base).netloc.lower()
    pages = {}      # 주소 → (html, canonical)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for f in files:
            if not f.lower().endswith(".html"):
                continue
            rel = os.path.relpath(os.path.join(root, f), folder).replace(os.sep, "/")
            if SKIP.search(rel):
                continue
            html = io.open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
            url = file_url(base, rel)
            pages[url] = (html, check_page(r, html, url, host, local=True))

    smp = os.path.join(folder, "sitemap.xml")
    if not os.path.exists(smp):
        r.e("sitemap.xml 없음")
        return r
    locs = RE_LOC.findall(io.open(smp, encoding="utf-8", errors="replace").read())
    check_sitemap_list(r, locs, host)
    by_key = {form_key(u): u for u in pages}
    for u in set(locs):
        if u in pages:
            if pages[u][1] == "NOINDEX":
                r.e("사이트맵에 noindex 페이지", u)
        elif form_key(u) in by_key:
            r.e("사이트맵 주소 형식이 실제 페이지 주소와 다름", "%s ↔ %s" % (u, by_key[form_key(u)]))
        elif not partial:
            r.e("사이트맵 주소에 해당하는 파일 없음", u)
    in_sm = set(form_key(u) for u in locs)
    for u, (html, canon) in pages.items():
        if canon == u and form_key(u) not in in_sm:
            r.w("사이트맵에 빠진 페이지", u)
    check_links(r, [(u, h) for u, (h, _) in pages.items()], locs)
    return r


# ── 라이브 ──────────────────────────────────────────────────
def fetch(u):
    p = subprocess.run(["curl", "-s", "-L", "--max-time", "30", "-A", UA,
                        "-w", "\n@@%{http_code} %{url_effective}", u],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = p.stdout.decode("utf-8", "replace")
    i = out.rfind("\n@@")
    if i < 0:
        return "", "000", u
    code, _, eff = out[i + 3:].partition(" ")
    return out[:i], code.strip(), eff.strip()


def check_live(base):
    r = Result()
    base = base.rstrip("/")
    host = urlsplit(base).netloc.lower()
    sm, code, _ = fetch(base + "/sitemap.xml")
    if code != "200":
        r.e("sitemap.xml 응답 %s" % code)
        return r, 0
    locs = RE_LOC.findall(sm)
    check_sitemap_list(r, locs, host)
    uniq = sorted(set(locs))
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(fetch, uniq))
    pages = []
    for u, (html, c, eff) in zip(uniq, got):
        path = urlsplit(u).path
        if c != "200":
            r.e("사이트맵 주소가 200 이 아님", "%s (%s)" % (path, c))
            continue
        if eff != u:
            r.e("사이트맵 주소가 리다이렉트됨", "%s → %s" % (path, eff))
        if check_page(r, html, u, host, local=False) == "NOINDEX":
            r.e("사이트맵에 noindex 페이지", path)
        pages.append((u, html))
    check_links(r, pages, locs)
    return r, len(uniq)


def report(title, r):
    print("== %s   오류 %d · 경고 %d" % (title, r.n_err(), sum(len(v) for v in r.warn.values())))
    for mark, group in (("[오류]", r.err), ("[경고]", r.warn)):
        for kind, items in group.items():
            items = [x for x in items if x] or [""]
            print("   %s %s (%d)" % (mark, kind, len(items)))
            for x in items[:4]:
                if x:
                    print("          %s" % x)
            if len(items) > 4:
                print("          … 외 %d" % (len(items) - 4))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    total = 0
    if argv[0] == "--live":
        for b in argv[1:]:
            r, n = check_live(b)
            report("라이브 %s (사이트맵 %d URL)" % (b, n), r)
            total += r.n_err()
    else:
        if len(argv) < 2:
            print(__doc__)
            return 2
        r = check_folder(argv[0], argv[1], partial="--partial" in argv)
        report("폴더 %s → %s" % (argv[0], argv[1]), r)
        total = r.n_err()
    print("\n결과: " + ("통과" if total == 0 else "실패 — 오류 %d건. 위 항목을 고친 뒤 다시 돌릴 것" % total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
