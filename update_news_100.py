import os
import json
import time
import re
import html
import requests
import feedparser
from datetime import datetime, timedelta
from google import genai

# ==========================================
# 0. CONFIGURATION & RETENTION
# ==========================================
RETENTION_DAYS = 15
NEWS_LIMIT_PER_RUN = 100
STATE_FILE = "news_data.json"

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
FALLBACK_GOA_IMG = "https://images.pexels.com/photos/4428289/pexels-photo-4428289.jpeg?auto=compress&cs=tinysrgb&w=600"

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

FEEDS = [
    "https://news.google.com/rss/search?q=Goa+news&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Goa+local+OR+Panaji+OR+Margao+OR+Mapusa&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Goa+sports+OR+football&hl=en-IN&gl=IN&ceid=IN:en",
    "https://digitalgoa.com/feed/"
]

def clean_html_text(raw_html):
    """Strip HTML tags, links, and publisher tags completely."""
    if not raw_html:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', raw_html)
    clean = html.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

# ==========================================
# 1. RETENTION & STATE MANAGEMENT
# ==========================================
def manage_retention_and_state():
    threshold_date = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    recent_news = []
    seen_headlines = set()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                historical_news = json.load(f)
                for article in historical_news:
                    try:
                        article_date = datetime.fromisoformat(article.get('timestamp', ''))
                        if article_date > threshold_date:
                            recent_news.append(article)
                            seen_headlines.add(article.get('headline', '').strip().lower())
                    except Exception:
                        recent_news.append(article)
                print(f"Loaded {len(historical_news)} items. Retaining {len(recent_news)} within 15 days.")
        except Exception as e:
            print(f"Could not load state, creating fresh: {e}")

    return recent_news, seen_headlines

# ==========================================
# 2. ACCURATE PEXELS PHOTO SEARCH
# ==========================================
def get_pexels_image(keyword, category):
    if not PEXELS_API_KEY:
        return FALLBACK_GOA_IMG

    headers = {"Authorization": PEXELS_API_KEY}
    
    # Priority 1: Search using Gemini's precise visual keyword
    clean_kw = re.sub(r'[^a-zA-Z0-9 ]', '', keyword).strip()
    if not clean_kw or len(clean_kw) < 3:
        clean_kw = category

    try:
        url = f"https://api.pexels.com/v1/search?query={clean_kw}&per_page=5&orientation=landscape"
        res = requests.get(url, headers=headers, timeout=6).json()
        if res.get("photos") and len(res["photos"]) > 0:
            return res["photos"][0]["src"]["medium"]
    except Exception as e:
        print(f"Pexels search failed for keyword '{clean_kw}': {e}")

    # Priority 2: Fallback to broader Category keyword
    try:
        url = f"https://api.pexels.com/v1/search?query={category}&per_page=3&orientation=landscape"
        res = requests.get(url, headers=headers, timeout=6).json()
        if res.get("photos") and len(res["photos"]) > 0:
            return res["photos"][0]["src"]["medium"]
    except Exception:
        pass
        
    return FALLBACK_GOA_IMG

# ==========================================
# 3. RSS COLLECTION & PRE-FILTERING
# ==========================================
print("--- STARTING GOA LIVE 100 UPDATER ---")
historical_feed, seen_headlines = manage_retention_and_state()

raw_items = []
seen_titles = set()

for url in FEEDS:
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            # Remove publisher suffix (e.g., "- Times of India", "- Herald Goa")
            clean_title = re.sub(r'\s*-\s*[^-]+$', '', title).strip()
            summary = clean_html_text(entry.get("summary", ""))

            # Avoid exact title duplicates
            if clean_title and clean_title.lower() not in seen_titles and clean_title.lower() not in seen_headlines:
                seen_titles.add(clean_title.lower())
                raw_items.append({
                    "title": clean_title,
                    "summary": summary
                })
    except Exception as e:
        print(f"Feed error {url}: {e}")

raw_items = raw_items[:NEWS_LIMIT_PER_RUN]
print(f"Collected {len(raw_items)} new raw items for AI processing.")

# ==========================================
# 4. AI DEDUPLICATION, REWRITING & KEYWORDS
# ==========================================
new_articles_list = []

if raw_items:
    batch_size = 12
    for b in range(0, len(raw_items), batch_size):
        batch = raw_items[b:b+batch_size]
        news_input = [f"Item {i+1}:\nTitle: {item['title']}\nDetails: {item['summary']}" for i, item in enumerate(batch)]
        combined_text = "\n\n".join(news_input)

        prompt = f"""
        You are a chief news editor for a Goa local news portal.
        Your tasks:
        1. DEDUPLICATE: If two items discuss the exact same news event, merge them into ONE story or drop the duplicate completely.
        2. REWRITE: Rewrite the story completely in your own simple, friendly English so even young readers can understand. 
           - Do NOT mention any external news channels, websites, or source names.
           - Provide exactly 2 short, readable paragraphs.
        3. CATEGORIZE: Choose strictly one: [Politics, Sports, Tourism, Civic, Weather, Crime, Business, General].
        4. IMAGE SEARCH KEYWORD: Provide a simple 1-2 word universal visual keyword that represents this story on a stock photo site.
           Examples:
           - Football match -> "football"
           - Political party rally -> "politician"
           - Beach inspection -> "beach"
           - Monsoon / flood -> "rain storm"
           - Traffic or highway -> "highway traffic"
           - Court order / Police -> "police car"

        Strictly output a JSON array of objects:
        [
          {{
            "headline": "Clear engaging headline",
            "paragraphs": ["First easy paragraph.", "Second easy paragraph."],
            "category": "Politics",
            "img_keyword": "politician"
          }}
        ]

        Input stories:
        {combined_text}
        """

        gemini_models = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.5-flash-lite"]
        batch_success = False

        for model_name in gemini_models:
            try:
                print(f"Processing batch {b//batch_size+1} with {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={"response_mime_type": "application/json"}
                )
                parsed = json.loads(response.text.strip())
                if isinstance(parsed, list):
                    new_articles_list.extend(parsed)
                    batch_success = True
                    break
            except Exception as e:
                print(f"Error on {model_name}: {e}")
                time.sleep(1)

        if not batch_success:
            print(f"Fallback for batch {b//batch_size+1}")
            for item in batch:
                new_articles_list.append({
                    "headline": item['title'],
                    "paragraphs": [item.get('summary', item['title']), "Follow Goa Live for ongoing local updates regarding this development."],
                    "category": "General",
                    "img_keyword": "Goa"
                })

# ==========================================
# 5. FETCH RELEVANT PHOTOS & ASSEMBLE
# ==========================================
current_timestamp_iso = datetime.utcnow().isoformat()
final_new_processed = []

for item in new_articles_list:
    headline = item.get("headline", "").strip()
    if not headline or headline.lower() in seen_headlines:
        continue
    
    seen_headlines.add(headline.lower())
    cat = item.get("category", "General").capitalize()
    kw = item.get("img_keyword", cat)
    
    time.sleep(0.08)  # Rate limit courtesy
    photo_url = get_pexels_image(kw, cat)

    final_new_processed.append({
        "headline": headline,
        "paragraphs": item.get("paragraphs", []),
        "category": cat,
        "img_url": photo_url,
        "timestamp": current_timestamp_iso
    })

final_news_aggregate = final_new_processed + historical_feed
final_news_aggregate.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

# Save state
with open(STATE_FILE, "w", encoding="utf-8") as f:
    json.dump(final_news_aggregate, f, indent=4)

print(f"Total unique stories published: {len(final_news_aggregate)}")

# ==========================================
# 6. GENERATE HTML
# ==========================================
filter_categories = ["ALL", "CIVIC", "POLITICS", "TOURISM", "SPORTS", "WEATHER", "CRIME", "BUSINESS", "GENERAL"]

cat_nav_html = "".join([
    f'<div class="cat-link {"active" if cat == "ALL" else ""}" onclick="filterCat(\'{cat}\', this)">{cat}</div>'
    for cat in filter_categories
])

articles_html = ""
for item in final_news_aggregate:
    cat = item.get("category", "General").capitalize()
    headline = item.get("headline", "")
    img_url = item.get("img_url", FALLBACK_GOA_IMG)
    paragraphs = "".join([f"<p>{p}</p>" for p in item.get("paragraphs", [])])

    articles_html += f"""
        <article class="news-article-card" data-category="{cat.upper()}">
            <div class="article-media">
                <img src="{img_url}" alt="{cat}" loading="lazy" onerror="this.src='{FALLBACK_GOA_IMG}'">
                <span class="category-pill">{cat}</span>
            </div>
            <div class="article-body">
                <h2 class="article-title">{headline}</h2>
                <div class="article-paragraphs">{paragraphs}</div>
            </div>
        </article>
    """

html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>GOA LIVE – 100 Daily Goa News</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f1f5f9; color: #0f172a; padding-bottom: 70px; }}
        .top-navbar {{ background-color: #111827; color: #ffffff; padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 1000; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }}
        .brand-logo {{ display: flex; align-items: center; gap: 8px; text-decoration: none; color: #ffffff; font-size: 1.25rem; font-weight: 900; }}
        .brand-logo span {{ color: #e63946; }}
        .lang-switch {{ font-size: 0.8rem; font-weight: 600; color: #94a3b8; }}
        .lang-switch span.active {{ color: #ffffff; }}
        
        .cat-nav {{ background-color: #1f2937; overflow-x: auto; white-space: nowrap; display: flex; gap: 6px; padding: 6px 12px; scrollbar-width: none; }}
        .cat-nav::-webkit-scrollbar {{ display: none; }}
        .cat-link {{ display: inline-block; padding: 8px 12px; color: #94a3b8; font-size: 0.82rem; font-weight: 700; text-transform: uppercase; border-bottom: 3px solid transparent; cursor: pointer; }}
        .cat-link.active {{ color: #ffffff; border-bottom-color: #e63946; }}
        
        .section-header {{ padding: 16px 14px 8px; font-size: 1.1rem; font-weight: 900; font-style: italic; text-transform: uppercase; }}
        .feed-container {{ padding: 0 14px; display: flex; flex-direction: column; gap: 18px; max-width: 760px; margin: 0 auto; }}
        
        .news-article-card {{ background: #ffffff; border-radius: 14px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
        .article-media {{ position: relative; width: 100%; height: 210px; background-color: #e2e8f0; }}
        .article-media img {{ width: 100%; height: 100%; object-fit: cover; }}
        .category-pill {{ position: absolute; top: 10px; left: 10px; background: rgba(17, 24, 39, 0.85); color: #ffffff; font-size: 0.65rem; font-weight: 800; padding: 4px 8px; border-radius: 4px; text-transform: uppercase; backdrop-filter: blur(4px); }}
        
        .article-body {{ padding: 16px 18px 20px; }}
        .article-title {{ font-size: 1.12rem; font-weight: 800; line-height: 1.35; color: #0f172a; margin-bottom: 10px; }}
        .article-paragraphs {{ font-size: 0.92rem; line-height: 1.6; color: #475569; }}
        .article-paragraphs p {{ margin-bottom: 10px; }}
        
        footer {{ background: #111827; color: #94a3b8; padding: 24px 16px; text-align: center; font-size: 0.78rem; margin-top: 30px; }}
        footer a {{ color: #cbd5e1; text-decoration: none; }}
    </style>
</head>
<body>
    <header class="top-navbar">
        <a href="#" class="brand-logo">GOA <span>LIVE</span></a>
        <div class="lang-switch"><span class="active">ENG</span> | <span>कोंकणी</span></div>
    </header>
    
    <nav class="cat-nav">{cat_nav_html}</nav>
    <div class="section-header">LATEST STORIES</div>
    
    <main class="feed-container" id="newsFeed">
        {articles_html}
    </main>
    
    <footer>
        <p><strong>GOA LIVE</strong> &copy; 2026 – 15-Day Auto-Rotating Digest</p>
        <p style="margin-top: 4px;">Powered by Gemini AI &middot; Photos via <a href="https://www.pexels.com" target="_blank" rel="noopener noreferrer">Pexels</a></p>
    </footer>
    
    <script>
        function filterCat(selectedCategory, element) {{
            document.querySelectorAll('.cat-link').forEach(link => link.classList.remove('active'));
            element.classList.add('active');
            const cards = document.querySelectorAll('.news-article-card');
            cards.forEach(card => {{
                const cardCat = card.getAttribute('data-category');
                if (selectedCategory === 'ALL' || cardCat.toUpperCase() === selectedCategory.toUpperCase()) {{
                    card.style.display = 'block';
                }} else {{ card.style.display = 'none'; }}
            }});
        }}
    </script>
</body>
</html>
"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_template)

print("Generated clean, unique news digest successfully!")
