🌐 Website Downloader CLI (Advanced Offline Web Mirror)

Fully-featured authenticated web mirror tool — ideal for training platforms, LMS systems, internal sites, and content libraries.

✅ What it does

This tool creates a complete offline copy of websites — including authenticated portals — preserving viewing, navigation, images, video streams (CloudFront/Wistia), and downloadable files.

Core features

🕷️ Recursive mirror of linked pages (configurable depth + page count control)

📎 Downloads all static & media assets (JS/CSS/fonts/images/videos/PDF/ZIP)

🔗 Rewrites links to relative local paths (index.html stubs)

🚀 Multi-threaded asset fetch with retry/backoff engine

💾 Robust path handling (auto-hash long paths)

🧠 Saves srcset, video poster, source, track, and picture assets

🌍 Download from extra content-hosts (e.g., Wistia, CloudFront) via --allow-host

🛑 Skip unwanted URLs (--exclude login --exclude checkout)

🛡️ Full authentication support (cookies, session, JWT, CSRF, form login)

🔐 CloudFront signed cookies support for gated media

Use cases: LMS/offline training, secure portal archiving, research, travel/offline study, compliance, internal site backup.

🔒 Authentication Support
Method	Supported	Notes
Browser cookies	✅	--cookie or --cookie-file
HttpOnly cookies	✅	Works — not restricted like a browser
Bearer token / JWT	✅	--bearer-token
Basic Auth	✅	--basic-user --basic-pass
Form login	✅	--login-url --login-data or --login-json
CSRF token	✅	Auto-scrape from HTML if needed
CloudFront signed URLs/cookies	✅	Full media unlock
Session testing	✅	--probe-url auto checks access
🧭 Navigation & Control
Feature	Flag
Limit pages	--max-pages N
Limit depth	--max-depth N
Exclude URLs	--exclude keyword
Allow external asset hosts	--allow-host domain.com
(Optional) crawl external HTML	--crawl-external-html
Target folder	--destination PATH
🚀 Quick Start (Public Site)
python WebCopier.py \
  --url "https://example.com" \
  --destination ./example_backup \
  --max-pages 200 \
  --max-depth 3 \
  --threads 8

🔐 Authenticated Site Example
python WebCopier.py ^
 --url "https://redacted/" ^
 --cookie "sj_sessionid=...; domain=redacted; path=/; httponly" ^
 --cookie "sj_csrftoken=...; domain=redacted; path=/; secure" ^
 --cookie "auth_content_wp=...; domain=redacted; path=/content/wp/...; secure" ^
 --cookie "CloudFront-Key-Pair-Id=...; domain=redacted; path=/content/wp/...; secure" ^
 --cookie "CloudFront-Policy=...; domain=redacted; path=/content/wp/...; secure" ^
 --cookie "CloudFront-Signature=...; domain=redacted; path=/content/wp/...; secure" ^
 --allow-host embed-ssl.wistia.com ^
 --max-depth 2 ^
 --max-pages 10000 ^
 --exclude "login" --exclude "logout" --exclude "cart" --exclude "checkout" ^
 --destination "C:\mywebsite"

📦 Downloads & Rewrites
Supports	Details
HTML pages	yes
Images (including srcset)	✅ rewritten & saved
CSS/JS/fonts	✅
PDF/ZIP/doc	✅
Audio/Video <audio> <video> <source> <track>	✅
Video posters	✅
Wistia thumbnails + streams	✅ via --allow-host
CloudFront media	✅ signed cookies supported
🧠 Smart Behavior

Ignores mailto:, tel:, javascript:, data:

Ignores links matching --exclude

Detects infinite loops & re-crawl

Hash fallback for long filenames

Cross-platform path safety (Windows/Linux/Mac)

📂 Output Example
C:\mywebsite\
└─ redacted_com\
   ├─ index.html
   ├─ samplewebsite\
   │  └─ index.html
   ├─ content/wp/... (full mirrored LMS paths)
   └─ embed-ssl.videos.com\ (if allow-host used)


Open index.html — browse offline like the real site ✅

🛠️ Requirements

Python 3.10+

requests, beautifulsoup4

pip install -r requirements.txt

🧾 Recent Enhancements

✅ cookie-based authenticated crawling

✅ CloudFront secure media support

✅ srcset image parsing + rewrite

✅ video poster + <source> rewrite

✅ external allowed hosts (--allow-host)

✅ optional external HTML crawling

✅ URL exclude rules

✅ depth-controlled crawl

✅ long-path hashing

✅ improved logging + retry logic

🤝 Contributing

PRs welcome — feel free to add:

Browser cookie auto-import

GUI wrapper

Resume interrupted download

File size limits

robots.txt modes

📜 License

MIT — free for personal & commercial use
⚠️ Please respect site terms & ownership when archiving content.

✨ Enjoy fast, clean, authenticated offline mirroring!

Looking sharp — your tool is now at professional web-copy engine level 💪🚀
If you'd like, next step can be a browser cookie auto-extract helper or a GUI launcher.
