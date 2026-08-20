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
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

FEEDS = [
    # Politics & Governance
    "https://news.google.com/rss/search?q=Goa+politics+OR+Pramod+Sawant+OR+Goa+BJP+OR+Goa+Congress+OR+Goa+Assembly&hl=en-IN&gl=IN&ceid=IN:en",
    # Civic & Local Municipalities
    "https://news.google.com/rss/search?q=Goa+Panaji+OR+Margao+OR+Mapusa+OR+Ponda+OR+Vasco+civic&hl=en-IN&gl=IN&ceid=IN:en",
    # Crime & Police
    "https://news.google.com/rss/search?q=Goa+police+OR+Goa+crime+OR+Goa+High+Court&hl=en-IN&gl=IN&ceid=IN:en",
    # Tourism & Environment
    "https://news.google.com/rss/search?q=Goa+tourism+OR+Goa+beaches+OR+Goa+environment+OR+Mandovi&hl=en-IN&gl=IN&ceid=IN:en",
    # Business & Economy
    "https://news.google.com/rss/search?q=Goa+business+OR+Goa+economy+OR+Mopa+airport&hl=en-IN&gl=IN&ceid=IN:en",
    # Weather
    "https://news.google.com/rss/search?q=Goa+weather+OR+Goa+monsoon+OR+IMD+Goa&hl=en-IN&gl=IN&ceid=IN:en",
    # Sports
    "https://news.google.com/rss/search?q=Goa+football+OR+FC+Goa+OR+Goa+cricket&hl=en-IN&gl=IN&ceid=IN:en",
    # Direct Local Portals
    "https://digitalgoa.com/feed/"
]

def clean_html_text(raw_html):
    if not raw_html:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', raw_html)
    clean = html.unescape(clean)
    return re.sub(r'\s+', ' ', clean).strip()

# ==========================================
# 1. RETENTION & STATE MANAGEMENT
# ==========================================
def manage_retention_and_state():
    threshold_date = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    recent_news = []
    seen_headlines = set()
    used_images = set()

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
                            if article.get('img_url'):
                                used_images.add(article['img_url'])
                    except Exception:
                        recent_news.append(article)
        except Exception as e:
            print(f"Could not load state, creating fresh: {e}")

    return recent_news, seen_headlines, used_images

# ==========================================
# 2. STRICT ENTITY / WIKIMEDIA & PEXELS FETCHER
# ==========================================
def get_entity_image(search_query, used_images):
    """Fetches high-accuracy image from Wikimedia (Person/Party/Location) or Pexels without repetition."""
    if not search_query or search_query.strip().lower() in ["none", ""]:
        return None

    query = search_query.strip()
    
    # 1. Try Wikimedia Commons for real political leaders, flags, and landmarks
    try:
        wiki_url = "https://commons.wikimedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": 5,
            "prop": "pageimages",
            "piprop": "thumbnail",
            "pithumbsize": 640
        }
        res = requests.get(wiki_url, params=params, timeout=5).json()
        pages = res.get("query", {}).get("pages", {})
        for _, p_data in pages.items():
            thumb = p_data.get("thumbnail", {}).get("source")
            if thumb and thumb not in used_images:
                used_images.add(thumb)
                return thumb
    except Exception as e:
        print(f"Wikimedia notice for '{query}': {e}")

    # 2. Try Pexels for generic Indian events (traffic, football, rain, beach)
    if PEXELS_API_KEY:
        try:
            headers = {"Authorization": PEXELS_API_KEY}
            clean_q = f"{query} India"
            url = f"https://api.pexels.com/v1/search?query={clean_q}&per_page=6&orientation=landscape"
            res = requests.get(url, headers=headers, timeout=5).json()
            for photo in res.get("photos", []):
                p_url = photo["src"]["medium"]
                if p_url not in used_images:
                    used_images.add(p_url)
                    return p_url
        except Exception as e:
            print(f"Pexels notice for '{query}': {e}")

    # If no unique relevant photo is found, return None (NO FALLBACK REPETITION)
    return None

# ==========================================
# 3. RSS COLLECTION & BALANCING
# ==========================================
print("--- STARTING GOA LIVE 100 BALANCED UPDATER ---")
historical_feed, seen_headlines, used_images = manage_retention_and_state()

raw_items = []
seen_titles = set()

for url in FEEDS:
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            clean_title = re.sub(r'\s*-\s*[^-]+$', '', title).strip()
            summary = clean_html_text(entry.get("summary", ""))

            if clean_title and clean_title.lower() not in seen_titles and clean_title.lower() not in seen_headlines:
                seen_titles.add(clean_title.lower())
                raw_items.append({
                    "title": clean_title,
                    "summary": summary
                })
    except Exception as e:
        print(f"Feed error {url}: {e}")

raw_items = raw_items[:NEWS_LIMIT_PER_RUN]
print(f"Collected {len(raw_items)} items for AI rewriting.")

# ==========================================
# 4. GEMINI AI DEDUPLICATION & STRICT KEYWORDS
# ==========================================
new_articles_list = []

if raw_items:
    batch_size = 12
    for b in range(0, len(raw_items), batch_size):
        batch = raw_items[b:b+batch_size]
        news_input = [f"Item {i+1}:\nHeadline: {item['title']}\nSummary: {item['summary']}" for i, item in enumerate(batch)]
        combined_text = "\n\n".join(news_input)

        prompt = f"""
        You are an editor for a local Goa portal rewriting news for kids and common citizens in simple English.
        
        Rules:
        1. DEDUPLICATE: If stories repeat the same event, output only ONE combined story.
        2. NO SOURCES: Do NOT mention external news agencies, reporters, or websites.
        3. PARAGRAPHS: Provide exactly 2 short, easily readable paragraphs per story.
        4. CATEGORY: Choose strictly one: [Politics, Civic, Tourism, Sports, Weather, Crime, Business, General].
        5. STRICT IMAGE SEARCH ENTITY ("img_entity"):
           - If a specific person is mentioned (e.g. Pramod Sawant, Narendra Modi, Rahul Gandhi, Amit Shah, Vijai Sardesai), output their full name.
           - If a political party is central, output "Bharatiya Janata Party flag" or "Indian National Congress flag" or "Aam Aadmi Party".
           - If a place or generic event is central, output the specific place/topic (e.g. "Panaji", "Mandovi Bridge", "Indian football match", "Monsoon rain Goa", "Goa beach").
           - If no specific image makes sense, output "none".

        Strictly output a JSON array of objects:
        [
          {{
            "headline": "Clear engaging title",
            "paragraphs": ["First simple paragraph.", "Second simple paragraph."],
            "category": "Politics",
            "img_entity": "Pramod Sawant"
          }}
        ]

        Input items:
        {combined_text}
        """

        gemini_models = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
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
                print(f"Model {model_name} notice: {e}")
                time.sleep(1)

        if not batch_success:
            for item in batch:
                new_articles_list.append({
                    "headline": item['title'],
                    "paragraphs": [item.get('summary', item['title']), "Follow Goa Live for ongoing local reports."],
                    "category": "General",
                    "img_entity": "Goa"
                })

# ==========================================
# 5. ASSEMBLE (NO DUPLICATE PHOTOS)
# ==========================================
current_timestamp_iso = datetime.utcnow().isoformat()
final_new_processed = []

for item in new_articles_list:
    headline = item.get("headline", "").strip()
    if not headline or headline.lower() in seen_headlines:
        continue
    
    seen_headlines.add(headline.lower())
    cat = item.get("category", "General").capitalize()
    entity = item.get("img_entity", "none")
    
    time.sleep(0.04)
    photo_url = get_entity_image(entity, used_images)

    final_new_processed.append({
        "headline": headline,
        "paragraphs": item.get("paragraphs", []),
        "category": cat,
        "img_url": photo_url, # None if no accurate photo
        "timestamp": current_timestamp_iso
    })

final_news_aggregate = final_new_processed + historical_feed
final_news_aggregate.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

with open(STATE_FILE, "w", encoding="utf-8") as f:
    json.dump(final_news_aggregate, f, indent=4)

print(f"Saved total {len(final_news_aggregate)} active stories.")

# ==========================================
# 6. HTML WITH PAGINATION (30 PER PAGE)
# ==========================================
filter_categories = ["ALL", "POLITICS", "CIVIC", "TOURISM", "SPORTS", "WEATHER", "CRIME", "BUSINESS"]

cat_nav_html = "".join([
    f'<div class="cat-link {"active" if cat == "ALL" else ""}" onclick="filterCat(\'{cat}\', this)">{cat}</div>'
    for cat in filter_categories
])

articles_html = ""
for item in final_news_aggregate:
    cat = item.get("category", "General").capitalize()
    headline = item.get("headline", "")
    img_url = item.get("img_url")
    paragraphs = "".join([f"<p>{p}</p>" for p in item.get("paragraphs", [])])

    media_block = ""
    if img_url:
        media_block = f"""
            <div class="article-media">
                <img src="{img_url}" alt="{cat}" loading="lazy">
                <span class="category-pill">{cat}</span>
            </div>
        """
    else:
        media_block = f"""
            <div class="article-media-none">
                <span class="category-pill-inline">{cat}</span>
            </div>
        """

    articles_html += f"""
        <article class="news-article-card" data-category="{cat.upper()}">
            {media_block}
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
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f1f5f9; color: #0f172a; padding-bottom: 50px; }}
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
        
        .news-article-card {{ background: #ffffff; border-radius: 14px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 2px 8px rgba(0,0,0,0.05); display: none; }}
        .article-media {{ position: relative; width: 100%; height: 210px; background-color: #e2e8f0; }}
        .article-media img {{ width: 100%; height: 100%; object-fit: cover; }}
        .category-pill {{ position: absolute; top: 10px; left: 10px; background: rgba(17, 24, 39, 0.85); color: #ffffff; font-size: 0.65rem; font-weight: 800; padding: 4px 8px; border-radius: 4px; text-transform: uppercase; backdrop-filter: blur(4px); }}
        
        .article-media-none {{ padding: 14px 18px 0; }}
        .category-pill-inline {{ display: inline-block; background: #e2e8f0; color: #1e293b; font-size: 0.65rem; font-weight: 800; padding: 4px 8px; border-radius: 4px; text-transform: uppercase; }}
        
        .article-body {{ padding: 16px 18px 20px; }}
        .article-title {{ font-size: 1.12rem; font-weight: 800; line-height: 1.35; color: #0f172a; margin-bottom: 10px; }}
        .article-paragraphs {{ font-size: 0.92rem; line-height: 1.6; color: #475569; }}
        .article-paragraphs p {{ margin-bottom: 10px; }}
        
        .load-more-btn {{ display: block; width: calc(100% - 28px); max-width: 760px; margin: 20px auto 10px; padding: 14px 20px; background: #111827; color: #ffffff; text-align: center; border-radius: 10px; font-weight: 700; border: none; cursor: pointer; }}
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

    <button class="load-more-btn" id="loadMoreBtn" onclick="loadMoreStories()">Load More Stories (30 More)</button>
    
    <footer>
        <p><strong>GOA LIVE</strong> &copy; 2026 – 15-Day Rotating Digest</p>
        <p style="margin-top: 4px;">Powered by Gemini AI &middot; Verified Entity & CC Visuals</p>
    </footer>
    
    <script>
        let currentCount = 0;
        const pageSize = 30;
        let activeCategory = 'ALL';

        function renderArticles() {{
            const cards = Array.from(document.querySelectorAll('.news-article-card'));
            let matched = cards.filter(card => {{
                const cardCat = card.getAttribute('data-category');
                return activeCategory === 'ALL' || cardCat.toUpperCase() === activeCategory.toUpperCase();
            }});

            cards.forEach(card => card.style.display = 'none');

            matched.slice(0, currentCount).forEach(card => card.style.display = 'block');

            const btn = document.getElementById('loadMoreBtn');
            if (currentCount >= matched.length) {{
                btn.style.display = 'none';
            }} else {{
                btn.style.display = 'block';
            }}
        }}

        function loadMoreStories() {{
            currentCount += pageSize;
            renderArticles();
        }}

        function filterCat(selectedCategory, element) {{
            document.querySelectorAll('.cat-link').forEach(link => link.classList.remove('active'));
            element.classList.add('active');
            activeCategory = selectedCategory;
            currentCount = pageSize;
            renderArticles();
        }}

        // Initial Load
        currentCount = pageSize;
        renderArticles();
    </script>
</body>
</html>
"""

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html_template)

print("Generated clean, paginated Goa news portal successfully!")
