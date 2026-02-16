import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", flags=re.I)
IMAGE_EXT_PATTERN = re.compile(r"\.(?:jpg|jpeg|png|webp|avif|gif)(?:$|\?)", flags=re.I)
VIDEO_EXT_PATTERN = re.compile(r"\.(?:mp4|m3u8|mov|webm)(?:$|\?)", flags=re.I)
VIDEO_HINTS = ("video", "play", "stream", "hls", "goods_video", "video_url")
IMAGE_HINTS = ("image", "img", "cover", "thumb", "pic")
STATIC_ASSET_EXT_PATTERN = re.compile(r"\.(?:js|css|map|json|html|htm|txt|xml)(?:$|\?)", flags=re.I)
BLOCKED_URL_KEYWORDS = (
    "down_download",
    "android_browser_download",
    "ios_fast_download",
    "need_popover=true",
    "itunes.apple.com",
    "apps.apple.com",
)
LOGIN_URL_KEYWORDS = ("login", "passport", "oauth", "verify", "sms")
STORAGE_STATE_FILE = os.path.join(os.getcwd(), ".playwright_storage_state.json")
PLAYWRIGHT_USER_DATA_DIR = os.path.join(os.getcwd(), ".playwright_user_data")


def is_blocked_jump_url(url: str) -> bool:
    low = url.lower()
    return any(k in low for k in BLOCKED_URL_KEYWORDS)


def apply_anti_detection_scripts(context: Any) -> None:
    try:
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
    except Exception:
        pass


def cleanup_stale_test_browsers() -> dict[str, Any]:
    # Kill Chromium processes that are likely launched by Playwright for this project.
    patterns = [
        PLAYWRIGHT_USER_DATA_DIR,
        "--remote-debugging-pipe",
        "--disable-blink-features=AutomationControlled",
    ]
    matched_patterns: list[str] = []
    errors: list[str] = []
    for pattern in patterns:
        try:
            # rc=0 means at least one process matched and got signal.
            # rc=1 means no match; it's not an error for cleanup flows.
            result = subprocess.run(["pkill", "-f", pattern], check=False, capture_output=True, text=True)
            if result.returncode == 0:
                matched_patterns.append(pattern)
            elif result.returncode not in (0, 1):
                err = (result.stderr or result.stdout or "").strip()
                errors.append(f"{pattern}: rc={result.returncode} {err}")
        except Exception as exc:
            errors.append(f"{pattern}: {exc}")
    return {"matched_patterns": matched_patterns, "errors": errors}


def force_kill_chromium_processes() -> dict[str, Any]:
    try:
        result = subprocess.run(["pkill", "-x", "Chromium"], check=False, capture_output=True, text=True)
        return {
            "killed": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip(),
        }
    except Exception as exc:
        return {"killed": False, "returncode": -1, "stderr": str(exc)}


@dataclass
class ProductInfo:
    source_url: str
    final_url: str = ""
    title: str = ""
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def extract_url(text: str) -> str:
    pattern = re.compile(r"(https?://[^\s]+)")
    match = pattern.search(text.strip())
    if match:
        return match.group(1).strip("，。,.")
    return text.strip()


def normalize_url(raw: str) -> str:
    cleaned = extract_url(raw)
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if not parsed.scheme:
        cleaned = f"https://{cleaned}"
    return cleaned


def extract_goods_id(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("goods_id", "goodsId", "gid"):
        if key in query and query[key]:
            match = re.search(r"\d{5,}", str(query[key][0]))
            if match:
                return match.group(0)
    for pattern in (r"goods_id=(\d{5,})", r"/goods/(\d{5,})", r"goods/(\d{5,})"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def canonicalize_pdd_goods_url(url: str) -> str:
    goods_id = extract_goods_id(url)
    if not goods_id:
        return url
    return f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"


def parse_cookie_header(cookie_text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not cookie_text.strip():
        return cookies
    parts = cookie_text.split(";")
    for part in parts:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        key = k.strip()
        val = v.strip()
        if key:
            cookies[key] = val
    return cookies


def fetch_html(url: str, cookie_text: str = "") -> tuple[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if cookie_text.strip():
        headers["Cookie"] = cookie_text.strip()
    resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, resp.url


def meta_values(soup: BeautifulSoup, keys: list[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        for tag in soup.find_all("meta", attrs={"property": key}):
            content = (tag.get("content") or "").strip()
            if content:
                values.append(content)
        for tag in soup.find_all("meta", attrs={"name": key}):
            content = (tag.get("content") or "").strip()
            if content:
                values.append(content)
    seen = set()
    uniq = []
    for v in values:
        if v not in seen:
            uniq.append(v)
            seen.add(v)
    return uniq


def uniq_by_path(items: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in items:
        normalized = item.split("?")[0]
        if normalized not in seen:
            seen.add(normalized)
            out.append(item)
    return out


def classify_media_urls(candidates: list[str]) -> tuple[list[str], list[str]]:
    images: list[str] = []
    videos: list[str] = []
    for raw in candidates:
        url = normalize_candidate_url(raw.strip())
        if not url.startswith("http"):
            continue
        low = url.lower()
        if STATIC_ASSET_EXT_PATTERN.search(low):
            continue

        parsed = urlparse(url)
        path = parsed.path.lower()

        if IMAGE_EXT_PATTERN.search(low):
            images.append(url)
            continue
        if VIDEO_EXT_PATTERN.search(low):
            videos.append(url)
            continue
        # Avoid false positives like "svideo_index.js".
        if any(h in low for h in VIDEO_HINTS) and any(
            token in path for token in ("/video", "video-", "/play", "m3u8", "mp4")
        ):
            videos.append(url)
            continue
        if any(h in low for h in IMAGE_HINTS) and any(
            token in path for token in ("/image", "/img", "cover", "thumb", "pic")
        ):
            images.append(url)
    return uniq_by_path(images), uniq_by_path(videos)


def extract_urls_from_text(text: str) -> list[str]:
    normalized = text.replace("\\u002F", "/").replace("\\/", "/")
    return URL_PATTERN.findall(normalized)


def normalize_candidate_url(value: str) -> str:
    v = value.strip()
    if v.startswith("//"):
        return f"https:{v}"
    return v


def extract_media_from_json_obj(obj: Any, key_path: str = "") -> tuple[list[str], list[str]]:
    images: list[str] = []
    videos: list[str] = []

    def classify_by_key(path: str, value: str) -> None:
        low_path = path.lower()
        url = normalize_candidate_url(value)
        if not url.startswith("http"):
            return
        if any(h in low_path for h in VIDEO_HINTS):
            videos.append(url)
            return
        if any(h in low_path for h in IMAGE_HINTS):
            images.append(url)
            return
        classified_images, classified_videos = classify_media_urls([url])
        images.extend(classified_images)
        videos.extend(classified_videos)

    if isinstance(obj, dict):
        for k, v in obj.items():
            next_path = f"{key_path}.{k}" if key_path else str(k)
            sub_images, sub_videos = extract_media_from_json_obj(v, next_path)
            images.extend(sub_images)
            videos.extend(sub_videos)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            next_path = f"{key_path}[{idx}]"
            sub_images, sub_videos = extract_media_from_json_obj(item, next_path)
            images.extend(sub_images)
            videos.extend(sub_videos)
    elif isinstance(obj, str):
        classify_by_key(key_path, obj)
        for url in extract_urls_from_text(obj):
            classify_by_key(key_path, url)

    return uniq_by_path(images), uniq_by_path(videos)


def filter_valid_video_urls(urls: list[str]) -> list[str]:
    valid: list[str] = []
    for raw in urls:
        url = normalize_candidate_url(raw.strip())
        if not url.startswith("http"):
            continue
        low = url.lower()
        if STATIC_ASSET_EXT_PATTERN.search(low):
            continue
        if VIDEO_EXT_PATTERN.search(low):
            valid.append(url)
            continue
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(token in path for token in ("/video", "video-", "/play")) and any(
            token in low for token in ("m3u8", "mp4", "video")
        ):
            valid.append(url)
    return uniq_by_path(valid)


def extract_from_html(html: str) -> tuple[str, list[str], list[str]]:
    soup = BeautifulSoup(html, "lxml")

    title = ""
    title_candidates = meta_values(soup, ["og:title", "twitter:title"])
    if title_candidates:
        title = title_candidates[0]
    elif soup.title and soup.title.text:
        title = soup.title.text.strip()

    image_candidates = meta_values(soup, ["og:image", "twitter:image"])
    video_candidates = meta_values(soup, ["og:video", "og:video:url", "twitter:player"])

    for img in soup.find_all("img"):
        src = normalize_candidate_url((img.get("src") or img.get("data-src") or img.get("data-original") or "").strip())
        if src.startswith("http"):
            image_candidates.append(src)
    for video in soup.find_all("video"):
        src = normalize_candidate_url((video.get("src") or "").strip())
        if src.startswith("http"):
            video_candidates.append(src)
        for source in video.find_all("source"):
            source_src = normalize_candidate_url((source.get("src") or "").strip())
            if source_src.startswith("http"):
                video_candidates.append(source_src)

    script_text = " ".join(script.get_text(" ", strip=True) for script in soup.find_all("script"))
    script_urls = extract_urls_from_text(script_text)
    classified_images, classified_videos = classify_media_urls(script_urls)
    image_candidates.extend(classified_images)
    video_candidates.extend(classified_videos)
    return title, uniq_by_path(image_candidates), uniq_by_path(video_candidates)


def parse_static(source_url: str, cookie_text: str = "") -> ProductInfo:
    info = ProductInfo(source_url=source_url)
    html, final_url = fetch_html(source_url, cookie_text=cookie_text)
    info.final_url = final_url
    title, images, videos = extract_from_html(html)
    info.title = title
    all_images = uniq_by_path(images)
    all_videos = uniq_by_path(videos)
    info.images = all_images[:6]
    info.videos = all_videos[:3]
    info.raw = {
        "html_length": len(html),
        "method": "static",
        "video_candidates": all_videos[:12],
        "image_candidates": all_images[:12],
    }
    return info


def parse_dynamic_with_playwright(
    source_url: str,
    cookie_text: str = "",
    live_page: Optional[Any] = None,
) -> ProductInfo:
    info = ProductInfo(source_url=source_url)

    network_urls: list[str] = []
    response_urls: list[str] = []
    json_urls: list[str] = []
    json_images: list[str] = []
    json_videos: list[str] = []
    should_close = False
    if live_page is not None:
        page = live_page
    else:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("未安装 playwright，请先执行: playwright install chromium") from exc
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}
        p = sync_playwright().start()
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context_kwargs: dict[str, Any] = {
            "user_agent": DESKTOP_USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
        }
        if os.path.exists(STORAGE_STATE_FILE):
            context_kwargs["storage_state"] = STORAGE_STATE_FILE
        context = browser.new_context(**context_kwargs)
        apply_anti_detection_scripts(context)
        page = context.new_page()
        should_close = True

    context = page.context

    def safe_goto(target_url: str) -> None:
        page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        if is_blocked_jump_url(page.url):
            page.goto(source_url, wait_until="domcontentloaded", timeout=45000)

    def on_request(request: Any) -> None:
        req_url = request.url
        if req_url.startswith("http"):
            network_urls.append(req_url)

    def on_response(response: Any) -> None:
        res_url = response.url
        if res_url.startswith("http"):
            response_urls.append(res_url)
        content_type = (response.headers.get("content-type") or "").lower()
        resource_type = response.request.resource_type
        if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
            return
        if len(json_urls) >= 80:
            return
        try:
            body = response.text()
        except Exception:
            return
        if "http" not in body:
            return
        body_low = body.lower()
        if not any(k in body_low for k in ("video", "image", "goods", "mp4", "m3u8")):
            return
        json_urls.extend(extract_urls_from_text(body))
        try:
            payload = json.loads(body)
        except Exception:
            return
        extracted_images, extracted_videos = extract_media_from_json_obj(payload)
        json_images.extend(extracted_images)
        json_videos.extend(extracted_videos)

    def on_popup(popup: Any) -> None:
        # Some pages open login windows during click simulation; keep extraction on the main page.
        try:
            popup.close()
        except Exception:
            pass

    def on_context_page(new_page: Any) -> None:
        if new_page == page:
            return
        try:
            new_page.close()
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("popup", on_popup)
    context.on("page", on_context_page)
    final_url = source_url
    html = ""
    page_assets: dict[str, list[str]] = {"imgs": [], "videos": [], "links": []}
    try:
        # If we already have a logged-in live page with visible content, avoid extra navigation.
        use_current_page_first = False
        if live_page is not None:
            try:
                use_current_page_first = not page_looks_logged_out(page)
            except Exception:
                use_current_page_first = False

        if not use_current_page_first:
            safe_goto(source_url)

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

        # 触发首屏后的懒加载素材
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(1200)
        page.mouse.wheel(0, -800)
        page.wait_for_timeout(500)
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(800)

        # Optional aggressive click probing. Disabled by default to avoid opening login popups.
        click_probe_enabled = os.getenv("PLAYWRIGHT_CLICK_PROBE", "0").strip().lower() in {"1", "true", "yes"}
        if click_probe_enabled:
            for selector in ("video", "[class*=video-play]", "[class*=player]", "button[aria-label*=播放]"):
                locator = page.locator(selector).first
                try:
                    if locator.is_visible(timeout=500):
                        locator.click(timeout=800, force=True)
                        page.wait_for_timeout(500)
                except Exception:
                    pass

        final_url = page.url
        html = page.content()

        page_assets = page.evaluate(
            """() => {
                const imgs = Array.from(document.querySelectorAll("img"))
                  .map(n => n.currentSrc || n.src || n.getAttribute("data-src") || "")
                  .filter(Boolean);
                const videos = Array.from(document.querySelectorAll("video"))
                  .map(n => n.currentSrc || n.src || "")
                  .filter(Boolean);
                const links = Array.from(document.querySelectorAll("source"))
                  .map(n => n.src || "")
                  .filter(Boolean);
                return { imgs, videos, links };
            }"""
        )
    finally:
        try:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
            page.remove_listener("popup", on_popup)
        except Exception:
            pass
        try:
            context.remove_listener("page", on_context_page)
        except Exception:
            pass
    if should_close:
        try:
            page.context.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass

    title, images, videos = extract_from_html(html)
    images.extend(page_assets.get("imgs", []))
    videos.extend(page_assets.get("videos", []))
    videos.extend(page_assets.get("links", []))
    images.extend(json_images)
    videos.extend(json_videos)
    all_network = network_urls + response_urls + json_urls
    net_images, net_videos = classify_media_urls(all_network)
    images.extend(net_images)
    videos.extend(net_videos)

    info.final_url = final_url
    info.title = title
    all_images = uniq_by_path(images)
    all_videos = uniq_by_path(videos)
    info.images = all_images[:6]
    info.videos = all_videos[:3]
    info.raw = {
        "html_length": len(html),
        "method": "playwright",
        "network_urls_count": len(uniq_by_path(all_network)),
        "json_video_candidates": len(uniq_by_path(json_videos)),
        "video_candidates": all_videos[:12],
        "image_candidates": all_images[:12],
    }
    return info


def score_info(info: ProductInfo) -> int:
    title_bonus = 0
    if info.title and info.title != "拼多多商城":
        title_bonus = 1
    return len(info.videos) * 100 + len(info.images) * 10 + title_bonus


def merge_info(base: ProductInfo, incoming: ProductInfo, source_label: str) -> None:
    if incoming.title and (not base.title or base.title == "拼多多商城"):
        base.title = incoming.title
    if incoming.final_url:
        base.final_url = incoming.final_url

    merged_images = uniq_by_path(base.images + incoming.images)
    merged_videos = uniq_by_path(base.videos + incoming.videos)
    base.images = merged_images[:6]
    base.videos = merged_videos[:3]

    base_video_candidates = base.raw.get("video_candidates", [])
    incoming_video_candidates = incoming.raw.get("video_candidates", [])
    base.raw["video_candidates"] = uniq_by_path(base_video_candidates + incoming_video_candidates)[:12]
    base_image_candidates = base.raw.get("image_candidates", [])
    incoming_image_candidates = incoming.raw.get("image_candidates", [])
    base.raw["image_candidates"] = uniq_by_path(base_image_candidates + incoming_image_candidates)[:12]

    attempts = base.raw.get("merge_from", [])
    attempts.append(source_label)
    base.raw["merge_from"] = attempts


def ensure_login_browser_session() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError("未安装 playwright，请先执行: playwright install chromium") from exc

    pw = sync_playwright().start()
    browser = None
    context = None
    os.makedirs(PLAYWRIGHT_USER_DATA_DIR, exist_ok=True)
    context_kwargs: dict[str, Any] = {
        "headless": False,
        "locale": "zh-CN",
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    try:
        # Use a persistent browser profile to keep login/session data across runs.
        context = pw.chromium.launch_persistent_context(
            user_data_dir=PLAYWRIGHT_USER_DATA_DIR,
            channel="chrome",
            **context_kwargs,
        )
    except Exception:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        fallback_context_kwargs: dict[str, Any] = {
            "locale": "zh-CN",
        }
        if os.path.exists(STORAGE_STATE_FILE):
            fallback_context_kwargs["storage_state"] = STORAGE_STATE_FILE
        context = browser.new_context(**fallback_context_kwargs)
    apply_anti_detection_scripts(context)

    def on_dialog(dialog: Any) -> None:
        try:
            dialog.dismiss()
        except Exception:
            pass

    page = context.pages[0] if context.pages else context.new_page()
    page.on("dialog", on_dialog)

    return {"pw": pw, "browser": browser, "context": context, "page": page}


def close_login_browser_session(session: dict[str, Any]) -> None:
    context = session.get("context")
    browser = session.get("browser")
    pw = session.get("pw")
    try:
        if context:
            context.storage_state(path=STORAGE_STATE_FILE)
            context.close()
    except Exception:
        pass
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if pw:
            pw.stop()
    except Exception:
        pass
    # Do not force-kill browser processes here; graceful close avoids "restore pages" prompts.


def browser_session_alive(session: Optional[dict[str, Any]]) -> bool:
    if not session:
        return False
    context = session.get("context")
    if context is None:
        return False
    try:
        if context.is_closed():
            return False
    except Exception:
        return False
    page = session.get("page")
    if page is None:
        return True
    try:
        if page.is_closed():
            return True
    except Exception:
        return True
    return True


def page_looks_blank(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    const body = document.body;
                    if (!body) return true;
                    const hasMedia = !!document.querySelector("img, video, source, iframe, canvas");
                    const textLen = (body.innerText || "").trim().length;
                    const childCount = body.children ? body.children.length : 0;
                    return !hasMedia && textLen === 0 && childCount <= 1;
                }"""
            )
        )
    except Exception:
        return False


def ensure_session_page(browser_session: dict[str, Any]) -> Any:
    context = browser_session.get("context")
    page = browser_session.get("page")
    if context is None:
        raise RuntimeError("浏览器上下文不可用，请重新打开登录浏览器。")

    if page is not None:
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass

    for candidate in context.pages:
        try:
            if not candidate.is_closed():
                browser_session["page"] = candidate
                return candidate
        except Exception:
            continue

    new_page = context.new_page()
    browser_session["page"] = new_page
    return new_page


def close_extra_pages(context: Any, keep_page: Any) -> None:
    try:
        pages = list(context.pages)
    except Exception:
        return
    for p in pages:
        if p == keep_page:
            continue
        try:
            p.close()
        except Exception:
            pass


def goto_with_recover(url: str, browser_session: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    page = ensure_session_page(browser_session)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        if page_looks_blank(page):
            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
        return browser_session, page
    except Exception as exc:
        msg = str(exc)
        if "Target page, context or browser has been closed" not in msg:
            raise
        # Recover within the same context first; only recreate session if context is gone.
        try:
            page = ensure_session_page(browser_session)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return browser_session, page
        except Exception:
            raise RuntimeError("当前登录会话不可用，请点击“关闭登录浏览器”后再重新点击“开始生成”。")


def page_looks_logged_out(page: Any) -> bool:
    try:
        current_url = (page.url or "").lower()
    except Exception:
        return True

    parsed = urlparse(current_url)
    path = parsed.path or ""

    # If the page already renders media/content, treat it as ready even if query contains "login".
    try:
        has_content = page.evaluate(
            """() => {
                const hasVideo = !!document.querySelector('video[src], video source[src], source[src*=".mp4"], source[src*=".m3u8"]');
                const bodyText = (document.body && document.body.innerText) ? document.body.innerText : "";
                const hasGoodsSignals = /立即拼单|已拼|券后|看视频享专属优惠|商品/.test(bodyText);
                return hasVideo || hasGoodsSignals;
            }"""
        )
        if bool(has_content):
            return False
    except Exception:
        pass

    # Only detect login pages by path/host level features, not by query params like refer_page_name=login.
    if any(k in path for k in ("/login", "/passport", "/oauth", "/verify")):
        return True
    if "needs_login=1" in current_url and "goods_id=" not in current_url and "fyxmkief" not in path:
        return True
    if is_blocked_jump_url(current_url):
        return True

    try:
        has_login_ui = page.evaluate(
            """() => {
                const text = document.body ? document.body.innerText : "";
                if (!text) return false;
                const loginWords = /登录|注册|手机号登录|验证码登录|请先登录/;
                const hasLoginForm = !!document.querySelector('input[type="password"], input[type="tel"], input[name*="phone"], input[name*="mobile"]');
                return loginWords.test(text) && hasLoginForm;
            }"""
        )
        return bool(has_login_ui)
    except Exception:
        return True


def has_login_cookies(page: Any) -> bool:
    try:
        cookies = page.context.cookies()
    except Exception:
        return False
    cookie_names = {c.get("name", "").lower() for c in cookies}
    # Common auth/session cookie signals for PDD web sessions.
    signals = {"api_uid", "pdd_user_id", "pdd_user_uin", "_nano_fp", "ua"}
    return len(cookie_names.intersection(signals)) >= 1


def parse_product_info(
    source_url: str,
    cookie_text: str = "",
    live_page: Optional[Any] = None,
) -> ProductInfo:
    canonical_url = canonicalize_pdd_goods_url(source_url)
    candidate_urls: list[str] = [source_url]
    if canonical_url != source_url:
        candidate_urls.append(canonical_url)

    static_infos: list[ProductInfo] = []
    static_errors: list[str] = []
    for u in candidate_urls:
        try:
            static_info = parse_static(u, cookie_text=cookie_text)
            static_infos.append(static_info)
        except Exception as exc:
            static_errors.append(f"{u} -> {exc}")

    if not static_infos:
        raise RuntimeError("静态抓取全部失败: " + " | ".join(static_errors))

    best = max(static_infos, key=score_info)
    info = ProductInfo(
        source_url=source_url,
        final_url=best.final_url,
        title=best.title,
        images=list(best.images),
        videos=list(best.videos),
        raw=dict(best.raw),
    )
    info.raw["method"] = "static"
    info.raw["canonical_url"] = canonical_url
    info.raw["attempted_urls"] = candidate_urls
    info.raw["needs_login"] = "needs_login=1" in source_url
    if static_errors:
        info.raw["static_errors"] = static_errors

    need_dynamic = (not info.title) or (len(info.images) < 1) or (len(info.videos) < 1)
    if not need_dynamic:
        return info

    dynamic_attempt_urls: list[str] = []
    first_try_url = info.final_url or source_url
    if not is_blocked_jump_url(first_try_url):
        dynamic_attempt_urls.append(first_try_url)
    else:
        dynamic_attempt_urls.append(source_url)
    for u in candidate_urls:
        if u not in dynamic_attempt_urls and not is_blocked_jump_url(u):
            dynamic_attempt_urls.append(u)

    dynamic_logs: list[str] = []
    for idx, u in enumerate(dynamic_attempt_urls):
        try:
            dynamic_info = parse_dynamic_with_playwright(
                u,
                cookie_text=cookie_text,
                live_page=live_page,
            )
            merge_info(info, dynamic_info, source_label=f"dynamic_{idx}:{u}")
            info.raw["network_urls_count"] = max(
                int(info.raw.get("network_urls_count", 0)),
                int(dynamic_info.raw.get("network_urls_count", 0)),
            )
            info.raw["json_video_candidates"] = max(
                int(info.raw.get("json_video_candidates", 0)),
                int(dynamic_info.raw.get("json_video_candidates", 0)),
            )
            dynamic_logs.append(f"{u} -> ok")
            info.raw["fallback"] = "playwright"
            info.raw["method"] = "hybrid(static+playwright)"
            if info.videos:
                break
        except Exception as exc:
            dynamic_logs.append(f"{u} -> failed: {exc}")

    if dynamic_logs:
        info.raw["dynamic_attempts"] = dynamic_logs
    if "fallback" not in info.raw and dynamic_logs:
        info.raw["fallback"] = "playwright_failed: " + " | ".join(dynamic_logs)

    info.videos = filter_valid_video_urls(info.videos)[:3]
    info.raw["video_candidates"] = filter_valid_video_urls(info.raw.get("video_candidates", []))[:12]

    return info


def fallback_copy(info: ProductInfo) -> dict[str, str]:
    title = info.title or "该商品"
    points = (
        f"1) 用户关注点：{title}是否真有性价比。\n"
        "2) 核心卖点：价格门槛低、下单链路短、适合快速决策。\n"
        "3) 下单触发：限时、限量、真实使用场景。"
    )
    script = (
        f"开场3秒：今天测一个爆款，名字叫《{title}》。\n"
        "中段15秒：我先说结论，它最大的优势是入手门槛低，功能覆盖常见需求。"
        "如果你跟我一样追求省钱省事，这个配置已经够用。\n"
        "收尾12秒：适合学生党、租房党、和第一次尝试的人群。"
        "想要链接我放在评论区，先领券再下单。"
    )
    xhs = (
        f"标题建议：挖到宝了｜{title}值不值？\n"
        "正文建议：\n"
        "最近在做平价好物测评，这个我实际看下来有3个优点：\n"
        "1. 预算友好\n2. 使用门槛低\n3. 日常场景覆盖广\n"
        "不夸张不踩雷，建议先领券再决定。"
    )
    return {"selling_points": points, "script_30s": script, "xhs_rewrite": xhs}


def generate_ai_copy(info: ProductInfo) -> dict[str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return fallback_copy(info)

    try:
        from openai import OpenAI
    except Exception:
        return fallback_copy(info)

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    prompt = {
        "title": info.title,
        "images": info.images,
        "videos": info.videos,
        "goal": [
            "卖点拆解（3-5条）",
            "30秒带货脚本（分段）",
            "小红书版本改写（标题+正文）",
        ],
    }
    resp = client.chat.completions.create(
        model=model,
        temperature=0.7,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "你是电商内容策略师。请根据商品信息输出JSON，字段固定为"
                    "selling_points, script_30s, xhs_rewrite。内容使用简体中文。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    data = json.loads(content)
    if not all(key in data for key in ["selling_points", "script_30s", "xhs_rewrite"]):
        return fallback_copy(info)
    return {
        "selling_points": str(data["selling_points"]),
        "script_30s": str(data["script_30s"]),
        "xhs_rewrite": str(data["xhs_rewrite"]),
    }


def main() -> None:
    st.set_page_config(page_title="PDD 内容生成 MVP", page_icon="🛍️", layout="wide")
    st.title("拼多多商品内容生成 MVP")
    st.caption("输入拼多多商品链接，自动提取标题/主图/视频，并生成内容文案。")

    view_mode = st.sidebar.radio("界面模式", ["用户视图", "管理员视图"], index=0)
    is_admin = view_mode == "管理员视图"
    admin_key = os.getenv("ADMIN_VIEW_KEY", "").strip()
    if is_admin and admin_key:
        key_input = st.sidebar.text_input("管理员口令", type="password")
        if key_input != admin_key:
            st.warning("管理员口令错误，已切换为用户视图。")
            is_admin = False

    raw_input = st.text_area(
        "商品链接（可粘贴微信分享文本）",
        placeholder="例如：https://mobile.yangkeduo.com/goods.html?goods_id=xxxx",
        height=90,
    )

    cookie_input = ""
    if is_admin:
        cookie_input = st.text_area(
            "可选：拼多多 Cookie（用于需要登录态的链接）",
            placeholder="例如：api_uid=xxx; PDDAccessToken=xxx; ...",
            height=80,
        )
    if "browser_session" not in st.session_state:
        st.session_state["browser_session"] = None

    action_col, login_col, close_col, cleanup_col, force_cleanup_col = st.columns([1, 1, 1, 1, 1])
    with action_col:
        run = st.button("开始生成", type="primary")
    with login_col:
        login_confirmed = st.toggle(
            "登录状态：我已完成登录",
            value=False,
            key="login_confirmed_toggle",
            help="点开=已确认登录；关闭=未确认登录。",
        )
        login_state_text = "已点开（已确认登录）" if login_confirmed else "未点开（未确认登录）"
        login_state_style = (
            "background:#e8f7ee;color:#0f6b38;border:1px solid #a7dfbe;"
            if login_confirmed
            else "background:#fff3e8;color:#9c4b00;border:1px solid #ffc999;"
        )
        st.markdown(
            (
                "<div style='margin-top:4px;padding:6px 10px;border-radius:8px;"
                f"font-weight:600;display:inline-block;{login_state_style}'>"
                f"当前状态：{login_state_text}</div>"
            ),
            unsafe_allow_html=True,
        )
        st.caption("勾选后请再点击“开始生成”以继续采集。")
    with close_col:
        close_browser = st.button("关闭登录浏览器")
    with cleanup_col:
        cleanup_browser = st.button("清理残留测试浏览器")
    with force_cleanup_col:
        force_cleanup = st.button("强力清理Chromium")

    if close_browser:
        session = st.session_state.get("browser_session")
        if browser_session_alive(session):
            close_login_browser_session(session)
            st.success("已关闭登录浏览器会话。")
        st.session_state["browser_session"] = None

    if cleanup_browser:
        session = st.session_state.get("browser_session")
        if browser_session_alive(session):
            close_login_browser_session(session)
            st.session_state["browser_session"] = None
        cleanup_result = cleanup_stale_test_browsers()
        matched_patterns = cleanup_result.get("matched_patterns", [])
        errors = cleanup_result.get("errors", [])
        if matched_patterns:
            st.success(f"已清理残留进程（命中{len(matched_patterns)}个特征）。")
        else:
            st.warning("未命中可清理的测试进程特征。可尝试“强力清理Chromium”。")
        if errors:
            st.error("清理命令有异常: " + " | ".join(errors))

    if force_cleanup:
        session = st.session_state.get("browser_session")
        if browser_session_alive(session):
            close_login_browser_session(session)
            st.session_state["browser_session"] = None
        kill_result = force_kill_chromium_processes()
        if kill_result.get("killed"):
            st.success("已强力清理所有 Chromium 进程。")
        else:
            rc = kill_result.get("returncode")
            err = kill_result.get("stderr")
            if rc == 1:
                st.info("当前没有可清理的 Chromium 进程。")
            else:
                st.error(f"强力清理失败（rc={rc}）。{err}")

    if run:
        source_url = normalize_url(raw_input)
        if not source_url:
            st.error("请先输入有效链接。")
            return
        url = canonicalize_pdd_goods_url(source_url)
        if url != source_url:
            st.info("已自动转换为商品直达链接，减少登录跳转。")
        st.info("已自动打开浏览器并加载链接。若页面提示登录，请先完成登录并勾选“我已完成登录”。")
        with st.spinner("正在抓取并生成..."):
            browser_session: Optional[dict[str, Any]] = st.session_state.get("browser_session")
            try:
                if not browser_session_alive(browser_session):
                    if browser_session:
                        close_login_browser_session(browser_session)
                        st.session_state["browser_session"] = None
                    browser_session = ensure_login_browser_session()
                    st.session_state["browser_session"] = browser_session
                    st.info("已新建登录浏览器会话。")
                else:
                    st.info("复用已打开的登录浏览器会话。")
                browser_session, page = goto_with_recover(url, browser_session)
                st.session_state["browser_session"] = browser_session

                # Hard gate: never collect unless user explicitly confirms login.
                if not login_confirmed:
                    try:
                        page.context.storage_state(path=STORAGE_STATE_FILE)
                    except Exception:
                        pass
                    st.warning("未勾选“我已完成登录”，本次不会执行采集。请完成登录并勾选后再点击开始生成。")
                    return

                if login_confirmed:
                    st.info("已按“我已完成登录”继续抓取。")

                try:
                    page.context.storage_state(path=STORAGE_STATE_FILE)
                except Exception:
                    pass
                active_url = page.url or url
                close_extra_pages(page.context, page)

                info = parse_product_info(
                    active_url,
                    cookie_text=cookie_input,
                    live_page=page,
                )
            except Exception as exc:
                if browser_session and browser_session.get("page"):
                    try:
                        browser_session["page"].context.storage_state(path=STORAGE_STATE_FILE)
                    except Exception:
                        pass
                st.exception(exc)
                return
            copy_result = generate_ai_copy(info)

        st.subheader("抓取结果")
        st.write(f"- 标题: {info.title or '未提取到'}")

        if is_admin:
            with st.expander("管理员调试信息", expanded=True):
                st.write(f"- 输入链接: `{info.source_url}`")
                st.write(f"- 最终链接: `{info.final_url}`")
                st.write(f"- 规范化链接: `{info.raw.get('canonical_url', info.source_url)}`")
                st.write(f"- 抓取方式: `{info.raw.get('method', 'unknown')}`")
                if "network_urls_count" in info.raw:
                    st.write(f"- 动态网络URL数: `{info.raw['network_urls_count']}`")
                if "json_video_candidates" in info.raw:
                    st.write(f"- JSON视频候选数: `{info.raw['json_video_candidates']}`")
                if "fallback" in info.raw:
                    st.write(f"- 动态抓取: `{info.raw['fallback']}`")
                if "dynamic_attempts" in info.raw:
                    st.write("- 动态尝试:")
                    for row in info.raw["dynamic_attempts"]:
                        st.write(f"  - {row}")
                st.write("- 登录会话: `单次会话（登录态已持久化）`")
                st.write(f"- 登录状态勾选: `{'是' if login_confirmed else '否'}`")
                if info.raw.get("needs_login"):
                    st.info("该分享链接包含 needs_login=1，商品视频可能需要登录态才能返回。")
                st.write(f"- Cookie状态: `{'已提供' if cookie_input.strip() else '未提供'}`")
                if info.title == "拼多多商城" and not info.videos:
                    st.warning("当前链接可能被重定向到商城首页而非商品详情页。建议粘贴包含 goods_id 的分享链接。")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**主图**")
            if info.images:
                for image in info.images:
                    st.image(image, use_container_width=True)
            else:
                st.info("未提取到主图。")
        with col2:
            st.markdown("**视频**")
            if info.videos:
                for video in info.videos:
                    st.video(video)
                if is_admin:
                    st.caption("已提取视频URL")
                    st.code("\n".join(info.videos), language="text")
            else:
                st.info("未提取到视频。")
                candidates = info.raw.get("video_candidates", [])
                if is_admin and candidates:
                    st.caption("检测到视频候选URL（可手动验证）")
                    st.code("\n".join(candidates), language="text")

        st.subheader("AI 输出")
        st.markdown("**卖点拆解**")
        st.write(copy_result["selling_points"])
        st.markdown("**30秒带货脚本**")
        st.write(copy_result["script_30s"])
        st.markdown("**小红书版本改写**")
        st.write(copy_result["xhs_rewrite"])

        if not os.getenv("OPENAI_API_KEY"):
            st.warning("检测到未配置 OPENAI_API_KEY，当前展示的是本地模板生成结果。")


if __name__ == "__main__":
    main()
