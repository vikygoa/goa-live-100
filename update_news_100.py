
import os
import json
import time
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
FALLBACK_GOA_IMG = "https://images.pexels.com/photos/15160867/pexels-photo-15160867.jpeg?auto=compress&w=600"

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

FEEDS = [
    "https://news.google.com/rss/search?q=Goa+news&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Goa+local+news+OR+Panaji+OR+Margao&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Goa+sports+OR+Goa+football&hl=en-IN&gl=IN&ceid=IN:en",
    "https://digitalgoa.com/feed/"
]

# ==========================================
# 1. RETENTION & STATE MANAGEMENT
# ==========================================
def manage_retention_and_state():
    threshold_date = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    recent_news = []
    seen_titles = set()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                historical_news = json.load(f)
                for article in historical_news:
                    article_date = datetime.fromisoformat(article['timestamp'])
                    if article_date > threshold_date:
                        recent_news.append(article)
                        seen_titles.add(article['headline'].lower())
                print(f"Loaded {len(historical_news)} past items. Keeping {len(recent_news)} within 15 days.")
        except Exception as e:
            print(f"Could not load state, creating fresh: {e}")

    return recent_news, seen_titles

# ==========================================
# 2. PEXELS API (PHOTO FETCHING)
# ==========================================
def get_pexels_image(keyword):
    if not PEXELS_API_KEY:
        return FALLBACK_GOA_IMG

    headers = {"Authorization": PEXELS_API_KEY}
    query_param = f"{keyword} goa"
    url = f"https://api.pexels.com/v1/search?query={query_param}&per_page=1&orientation=landscape"
    
    try:
        res = requests.get(url, headers=headers, timeout=8).json()
        if res.get("photos") and len(res["photos"]) > 0:
            return res["photos"][0]["src"]["medium"]
    except Exception as e:
        print(f"Pexels fetch notice for '{keyword}': {e}")
        
    return FALLBACK_GOA_IMG

# ==========================================
# 3. MAIN EXECUTION FLOW
# ==========================================
print("--- STARTING GOA LIVE 100 UPDATER ---")
new_news_to_display = []

historical_feed, seen_titles = manage_retention_and_state()

raw_articles_to_process = []
raw_feed_seen = set()

for url in FEEDS:
    feed = feedparser.parse(url)
    for entry in feed.entries:
        title = entry.title.strip()
        if title.lower() not in raw_feed_seen and title.lower() not in seen_titles:
            raw_feed_seen.add(title.lower())
            raw_articles_to_process.append({
                "title": title,
                "summary": entry.get("summary", "")
            })

raw_articles_to_process = raw_articles_to_process[:NEWS_LIMIT_PER_RUN]
print(f"Collected {len(raw_articles_to_process)} new items from feeds.")

# Batch AI summarization
if raw_articles_to_process:
    batch_size = 25
    gemini_output_all = []

    for b in range(0, len(raw_articles_to_process), batch_size):
        batch = raw_articles_to_process[b:b+batch_size]
        news_input_text = [f"{i+1}. Title: {item['title']}\nSummary: {item['summary']}" for i, item in enumerate(batch)]
        combined_prompt_text = "\n\n".join(news_input_text)

        prompt = f"""
        You are a journalist rewriting local Goa news into structured paragraph format.
        For each story:
        1. Write a clear, bold headline.
        2. Write 2 readable paragraphs explaining the story in simple English.
        3. Categorize into strictly one: [Tourism, Sports, Politics, Weather, Crime, Civic, Business, General].
        4. Provide one single search keyword for an image (e.g. 'football', 'beach', 'police', 'traffic', 'government').

        Output strictly as a valid JSON array of objects with keys: "category", "headline", "paragraphs" (array of strings), "img_keyword".

        News items:
        {combined_prompt_text}
        """

        gemini_model_candidates = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash"]
        for model_variant in gemini_model_candidates:
            try:
                print(f"Processing batch {b//batch_size+1} with {model_variant}...")
                response = client.models.generate_content(
                    model=model_variant,
                    contents=prompt,
                    config={"response_mime_type": "application/json"}
                )
                batch_data = json.loads(response.text)
                gemini_output_all.extend(batch_data)
                break
            except Exception as e:
                print(f"Model {model_variant} attempt failed: {e}")
                time.sleep(2)

    current_timestamp_iso = datetime.utcnow().isoformat()
    for idx, item in enumerate(gemini_output_all):
        time.sleep(0.08)
        img_keyword = item.get("img_keyword", item.get("category", "Goa"))
        pexel_img_url = get_pexels_image(img_keyword)
        
        full_article = {
            "headline": item.get("headline", ""),
            "paragraphs": item.get("paragraphs", []),
            "category": item.get("category", "General").strip().capitalize(),
            "img_url": pexel_img_url,
            "timestamp": current_timestamp_iso
        }
        
        if full_article['headline'].lower() not in seen_titles:
            new_news_to_display.append(full_article)
            seen_titles.add(full_article['headline'].lower())

final_news_aggregate = new_news_to_display + historical_feed
final_news_aggregate.sort(key=lambda x: x['timestamp'], reverse=True)

with open(STATE_FILE, "w", encoding="utf-8") as f:
    json.dump(final_news_aggregate, f, indent=4)

print(f"Total articles in active feed: {len(final_news_aggregate)}")

# ==========================================
# 4. BUILD CLEAN MAGAZINE HTML
# ==========================================
filter_categories = ["ALL", "CIVIC", "POLITICS", "TOURISM", "SPORTS", "WEATHER", "CRIME", "BUSINESS"]

cat_nav_html = "".join([
    f'<div class="cat-link {"active" if cat == "ALL" else ""}" onclick="filterCat(\'{cat}\', this)">{cat}</div>'
    for cat in filter_categories
])

articles_main_feed_html = ""
for item in final_news_aggregate:
    cat = item.get("category", "General").capitalize()
    headline = item.get("headline", "")
    img_url = item.get("img_url", FALLBACK_GOA_IMG)
    paragraphs = "".join([f"<p>{p}</p>" for p in item.get("paragraphs", [])])

    articles_main_feed_html += f"""
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
        .article-media {{ position: relative; width: 100%; height: 200px; }}
        .article-media img {{ width: 100%; height: 100%; object-fit: cover; }}
        .category-pill {{ position: absolute; top: 10px; left: 10px; background: rgba(17, 24, 39, 0.85); color: #ffffff; font-size: 0.65rem; font-weight: 800; padding: 4px 8px; border-radius: 4px; text-transform: uppercase; }}
        
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
        {articles_main_feed_html}
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

print("Generated clean index.html successfully!")
