"""텔레그램 데스크톱 HTML 내보내기 → JSONL (채널 말투 학습용)."""
import json, re, sys, glob, os
from bs4 import BeautifulSoup

def text_of(el):
    if el is None: return ""
    for br in el.find_all("br"): br.replace_with("\n")
    return el.get_text().strip()

def parse(folder):
    files = sorted(glob.glob(os.path.join(folder, "messages*.html")),
                   key=lambda p: int(re.search(r"messages(\d*)", p).group(1) or 1))
    for fp in files:
        soup = BeautifulSoup(open(fp, encoding="utf-8").read(), "html.parser")
        for m in soup.select("div.message.default"):
            body = m.find("div", class_="body")
            d = body.find("div", class_="date")
            t = body.find("div", class_="text", recursive=False)
            reply = body.find("div", class_="reply_to")
            fwd = body.select_one("div.forwarded.body")
            links = [a["href"] for a in body.find_all("a", href=True) if a["href"].startswith("http")]
            media = [a["href"] for a in body.select("a.photo_wrap, a.media_wrap, a.video_file_wrap") if a.get("href")]
            reacts = sum(int(c.get_text().strip() or 0) for c in body.select("span.reaction span.count") if c.get_text().strip().isdigit())
            yield {
                "id": m.get("id"), "date": d["title"] if d else "",
                "text": text_of(t) if t else text_of(fwd.find("div", class_="text")) if fwd else "",
                "forwarded_from": fwd.find("div", class_="from_name").find(string=True, recursive=False).strip() if fwd else "",
                "reply_to": (reply.find("a")["href"].split("go_to_")[-1] if reply and reply.find("a") else ""),
                "links": links, "media": media, "reactions": reacts,
            }

if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for r in parse(src):
            f.write(json.dumps(r, ensure_ascii=False) + "\n"); n += 1
    print(n, "messages")
