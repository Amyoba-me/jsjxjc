"""
Telegram Resource Checker — single-file version.

Environment variables:
    BOT_TOKEN              required
    MOD_CHAT_ID            required
    DB_PATH                optional, default /data/checker.db
    MAX_DEPTH              optional, default 2
    MAX_RESOURCES          optional, default 20
    MAX_TELEGRAM_POSTS     optional, default 30
    REQUEST_TIMEOUT        optional, default 15
    MIN_ALERT_SCORE        optional, default 55
    ADMIN_IDS              optional, comma-separated Telegram IDs

Install:
    pip install aiogram aiohttp beautifulsoup4 aiosqlite lxml

Run:
    python bot.py
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MOD_CHAT_ID = int(os.getenv("MOD_CHAT_ID", "0"))

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/checker.db",
)

MAX_DEPTH = int(os.getenv("MAX_DEPTH", "2"))
MAX_RESOURCES = int(os.getenv("MAX_RESOURCES", "20"))
MAX_TELEGRAM_POSTS = int(os.getenv("MAX_TELEGRAM_POSTS", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
MIN_ALERT_SCORE = int(os.getenv("MIN_ALERT_SCORE", "55"))

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

USER_AGENT = "Mozilla/5.0 (compatible; TelegramResourceChecker/2.0)"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not MOD_CHAT_ID:
    raise RuntimeError("MOD_CHAT_ID is not configured")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("telegram-resource-checker")


# ============================================================
# CONSTANTS
# ============================================================

VERDICTS = {
    "WORK": "🟢 Рабочий",
    "PERSONAL": "🔴 Личный",
    "SUSPICIOUS": "🟠 Подозрительный",
    "UNKNOWN": "⚪ Не удалось определить",
}

RULES_TEXT = {
    "8.1.1": (
        "Личный ресурс содержит личное пространство автора: "
        "щитпосты, повседневные посты, стадии работ, спойлеры, "
        "репосты, самопиар и другой личный контент."
    ),
    "8.1.2": (
        "К личным ресурсам относятся личные Telegram-каналы/группы, "
        "арт-каналы и блоги с собственными рисунками и другой "
        "нерабочий контент."
    ),
    "8.1.3": (
        "Рабочий портфолио-/прайс-ресурс разрешён, если содержит "
        "только рабочую информацию."
    ),
    "8.1.4": "В рабочем ресурсе не должно быть ссылок на личные ресурсы.",
    "8.1.5": "Связанные сообщества также должны соответствовать требованиям.",
    "8.2.1": (
        "Нельзя размещать ресурс, внутри которого находится путь "
        "к личному сообществу."
    ),
    "8.2.2": "Передача такой ссылки от другого лица не отменяет нарушение.",
    "8.2.3": "Правило действует для цепочек переходов.",
    "8.3.1": "Рабочий ресурс используется для показа работ и приёма заказов.",
    "8.3.2": "Личный ресурс содержит личный контент вместе с работами.",
    "8.3.3": "Само наличие портфолио не делает ресурс личным.",
}

URL_RE = re.compile(
    r"(?i)\b("
    r"https?://[^\s<>\"]+"
    r"|www\.[^\s<>\"]+"
    r"|t\.me/[^\s<>\"]+"
    r"|telegram\.me/[^\s<>\"]+"
    r"|@[a-zA-Z0-9_]{5,32}"
    r")"
)

TELEGRAM_HOSTS = {
    "t.me",
    "telegram.me",
    "www.t.me",
    "www.telegram.me",
}


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class ResourceNode:
    url: str
    depth: int
    parent: Optional[str] = None
    final_url: Optional[str] = None
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    telegram: bool = False
    accessible: bool = True
    error: Optional[str] = None
    has_round_video: bool = False
    has_voice: bool = False


@dataclass
class Classification:
    verdict: str
    score: int
    confidence: float
    reasons: list[str]
    rules: list[str]
    features: dict
    telegram_resources: list[str]


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    url = url.strip().strip(".,;:!?)]}>\"'")

    if url.startswith("@"):
        url = "https://t.me/" + url[1:]
    elif url.startswith("www."):
        url = "https://" + url
    elif url.startswith("t.me/") or url.startswith("telegram.me/"):
        url = "https://" + url
    elif not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    url, _ = urldefrag(url)
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower().split(":")[0]

    path = re.sub(r"/+", "/", parsed.path)

    return f"{scheme}://{host}{path}".rstrip("/")


def is_telegram_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host in TELEGRAM_HOSTS
    except Exception:
        return False


def extract_urls(message: Message) -> list[str]:
    result = []
    
    raw_text = message.text or message.caption or ""
    for raw in URL_RE.findall(raw_text):
        url = normalize_url(raw)
        if url not in result:
            result.append(url)

    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == "text_link" and entity.url:
            url = normalize_url(entity.url)
            if url not in result:
                result.append(url)
        elif entity.type == "mention":
            offset = entity.offset
            length = entity.length
            mention_text = raw_text[offset : offset + length]
            url = normalize_url(mention_text)
            if url not in result:
                result.append(url)

    return result


def shorten(text: str, length: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()

    if len(text) <= length:
        return text

    return text[: length - 1] + "…"


def normalize_features(features: dict) -> dict:
    return {
        k: bool(v)
        for k, v in sorted(features.items())
        if isinstance(v, bool)
    }


def source_message_link(
    chat_id: int,
    message_id: int,
) -> Optional[str]:
    chat_string = str(chat_id)

    if chat_string.startswith("-100"):
        internal_id = chat_string[4:]
        return f"https://t.me/c/{internal_id}/{message_id}"

    return None


def get_telegram_target(url: str):
    if not is_telegram_url(url):
        return None

    parsed = urlparse(url)
    path = parsed.path.strip("/")

    if not path:
        return None

    parts = path.split("/")

    if len(parts) >= 3 and parts[0].lower() == "s":
        username = parts[1]
        message_id = int(parts[2]) if parts[2].isdigit() else None
        return username, message_id

    if len(parts) >= 2:
        username = parts[0]
        message_id = int(parts[1]) if parts[1].isdigit() else None
        return username, message_id

    return parts[0], None


def extract_telegram_links(element, base_username: str = "") -> set[str]:
    """Извлекает гиперссылки, кнопки и упоминания из HTML-элементов Telegram."""
    extracted = set()
    if not element:
        return extracted

    for a in element.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        if "t.me/iv?" in href and "url=" in href:
            try:
                parsed_q = parse_qs(urlparse(href).query)
                if "url" in parsed_q:
                    for real_url in parsed_q["url"]:
                        extracted.add(normalize_url(real_url))
                    continue
            except Exception:
                pass

        if href.startswith("//"):
            href = f"https:{href}"
        elif href.startswith("/"):
            if base_username and not href.startswith("/s/"):
                href = f"https://t.me{href}"
            elif href.startswith("/s/"):
                href = f"https://t.me{href}"

        if href.startswith("tg://resolve?domain="):
            domain = href.split("domain=")[-1].split("&")[0]
            href = f"https://t.me/{domain}"

        if re.match(r"^(https?://|t\.me/|telegram\.me/|@[a-zA-Z0-9_]{5,32})", href, re.I):
            extracted.add(normalize_url(href))

    return extracted


# ============================================================
# DATABASE
# ============================================================

class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        directory = os.path.dirname(self.path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    submitted_by INTEGER,
                    original_url TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    reasons TEXT,
                    rules TEXT,
                    features TEXT,
                    chain TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    rating TEXT NOT NULL,
                    corrected_verdict TEXT,
                    comment TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(scan_id, moderator_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS verified_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    features TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    rules TEXT,
                    confidence REAL NOT NULL,
                    confirmations INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(fingerprint, verdict)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    resource_type TEXT,
                    features TEXT,
                    verdict TEXT,
                    score INTEGER,
                    verified INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)

            await db.commit()

    async def create_scan(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
        submitted_by: int,
        original_url: str,
        result: Classification,
        chain: list[str],
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO scans (
                    source_chat_id,
                    source_message_id,
                    submitted_by,
                    original_url,
                    verdict,
                    score,
                    confidence,
                    reasons,
                    rules,
                    features,
                    chain,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_chat_id,
                    source_message_id,
                    submitted_by,
                    original_url,
                    result.verdict,
                    result.score,
                    result.confidence,
                    json.dumps(result.reasons, ensure_ascii=False),
                    json.dumps(result.rules, ensure_ascii=False),
                    json.dumps(result.features, ensure_ascii=False),
                    json.dumps(chain, ensure_ascii=False),
                    utc_now(),
                ),
            )

            await db.commit()
            return cursor.lastrowid

    async def add_feedback(
        self,
        scan_id: int,
        moderator_id: int,
        rating: str,
        corrected_verdict: Optional[str] = None,
        comment: Optional[str] = None,
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO feedback (
                    scan_id,
                    moderator_id,
                    rating,
                    corrected_verdict,
                    comment,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    moderator_id,
                    rating,
                    corrected_verdict,
                    comment,
                    utc_now(),
                ),
            )
            await db.commit()

    async def get_scan(self, scan_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM scans WHERE id = ?",
                (scan_id,),
            )

            return await cursor.fetchone()

    async def save_verified_case(
        self,
        features: dict,
        verdict: str,
        rules: list[str],
        resource_type: str,
    ):
        normalized = json.dumps(
            normalize_features(features),
            sort_keys=True,
            ensure_ascii=False,
        )

        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = utc_now()

        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id
                FROM verified_cases
                WHERE fingerprint = ?
                  AND verdict = ?
                """,
                (fingerprint, verdict),
            )

            existing = await cursor.fetchone()

            if existing:
                await db.execute(
                    """
                    UPDATE verified_cases
                    SET confirmations = confirmations + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, existing[0]),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO verified_cases (
                        fingerprint,
                        resource_type,
                        features,
                        verdict,
                        rules,
                        confidence,
                        confirmations,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        resource_type,
                        json.dumps(features, ensure_ascii=False),
                        verdict,
                        json.dumps(rules, ensure_ascii=False),
                        1.0,
                        1,
                        now,
                        now,
                    ),
                )

            await db.commit()

    async def get_verified_cases(self, limit: int = 500):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT *
                FROM verified_cases
                WHERE confirmations >= 2
                ORDER BY confirmations DESC
                LIMIT ?
                """,
                (limit,),
            )

            return await cursor.fetchall()

    async def stats(self):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN verdict = 'PERSONAL' THEN 1 ELSE 0 END) AS personal,
                    SUM(CASE WHEN verdict = 'WORK' THEN 1 ELSE 0 END) AS work,
                    SUM(CASE WHEN verdict = 'SUSPICIOUS' THEN 1 ELSE 0 END) AS suspicious,
                    SUM(CASE WHEN verdict = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown
                FROM scans
                """
            )

            scans = await cursor.fetchone()

            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN rating = 'correct' THEN 1 ELSE 0 END) AS correct,
                    SUM(CASE WHEN rating = 'partial' THEN 1 ELSE 0 END) AS partial,
                    SUM(CASE WHEN rating = 'wrong' THEN 1 ELSE 0 END) AS wrong
                FROM feedback
                """
            )

            feedback = await cursor.fetchone()

            return scans, feedback


db = Database(DB_PATH)


# ============================================================
# HTTP
# ============================================================

class HttpClient:
    def __init__(self):
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )

    async def close(self):
        await self.session.close()

    async def get(self, url: str):
        return await self.session.get(url, allow_redirects=True)


http: Optional[HttpClient] = None


# ============================================================
# WEB PARSER
# ============================================================

async def fetch_web_page(url: str) -> ResourceNode:
    node = ResourceNode(
        url=url,
        depth=0,
        telegram=is_telegram_url(url),
    )

    if http is None:
        node.accessible = False
        node.error = "HTTP client is not initialized"
        return node

    try:
        async with await http.get(url) as response:
            node.final_url = str(response.url)

            content_type = response.headers.get("Content-Type", "").lower()

            if "text/html" not in content_type:
                return node

            raw = await response.text(errors="ignore")

    except Exception as exc:
        node.accessible = False
        node.error = f"{type(exc).__name__}: {exc}"
        return node

    soup = BeautifulSoup(raw, "lxml")

    title = soup.find("title")
    if title:
        node.title = shorten(title.get_text(" ", strip=True), 300)

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    node.text = shorten(soup.get_text(" ", strip=True), 30000)

    links = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()

        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        absolute = urljoin(node.final_url or url, href)

        if absolute.startswith(("http://", "https://")):
            links.add(normalize_url(absolute))

    node.links = list(links)
    return node


# ============================================================
# TELEGRAM PUBLIC PARSER
# ============================================================

async def fetch_telegram_resource(url: str) -> ResourceNode:
    node = ResourceNode(
        url=url,
        depth=0,
        telegram=True,
    )

    if http is None:
        node.accessible = False
        node.error = "HTTP client is not initialized"
        return node

    target = get_telegram_target(url)

    if not target:
        node.accessible = False
        node.error = "Invalid Telegram URL"
        return node

    username, message_id = target
    
    texts = []
    links = set()
    pinned_messages = set()
    
    # 1. Извлекаем описание (Bio) со страницы профиля t.me/username
    try:
        info_url = f"https://t.me/{username}"
        async with await http.get(info_url) as response:
            if response.status == 200:
                raw_info = await response.text(errors="ignore")
                info_soup = BeautifulSoup(raw_info, "lxml")
                desc_node = info_soup.select_one(".tgme_page_description")
                if desc_node:
                    desc_text = desc_node.get_text(" ", strip=True)
                    if desc_text:
                        texts.append(f"Описание канала: {desc_text}")
                        links.update(extract_telegram_links(desc_node, username))
                        for raw in URL_RE.findall(desc_text):
                            links.add(normalize_url(raw))
    except Exception:
        pass

    # 2. Получаем посты через /s/ и ищем закреп
    public_url = f"https://t.me/s/{username}"
    try:
        async with await http.get(public_url) as response:
            node.final_url = str(response.url)

            if response.status != 200:
                node.accessible = False
                node.error = f"HTTP {response.status}"
                return node

            raw = await response.text(errors="ignore")
    except Exception as exc:
        node.accessible = False
        node.error = f"{type(exc).__name__}: {exc}"
        return node

    soup = BeautifulSoup(raw, "lxml")
    
    # Ищем плашку закрепленного сообщения вверху t.me/s/
    pinned_tags = soup.find_all(class_=re.compile(r"pinned", re.I))
    for tag in pinned_tags:
        anchors = [tag] if tag.name == "a" else tag.find_all("a", href=True)
        for a_tag in anchors:
            href = a_tag.get("href", "")
            if f"/{username}/" in href or (href.startswith("/") and href.strip("/").isdigit()):
                full_url = href if href.startswith("http") else f"https://t.me{href}"
                pinned_messages.add(full_url)
        
        data_post = tag.get("data-post")
        if data_post:
            pinned_messages.add(f"https://t.me/{data_post}")

    for a in soup.select(".tgme_channel_info_pinned_message, .tgme_widget_message_pinned, a[class*='pinned']"):
        href = a.get("href", "")
        if href:
            full_url = href if href.startswith("http") else f"https://t.me{href}"
            pinned_messages.add(full_url)

    # Обрабатываем основные посты ленты до лимита
    posts = soup.select(".tgme_widget_message")
    for post in posts[:MAX_TELEGRAM_POSTS]:
        text_node = post.select_one(".tgme_widget_message_text")
        if text_node:
            post_text = text_node.get_text(" ", strip=True)
            texts.append(post_text)
            for raw in URL_RE.findall(post_text):
                links.add(normalize_url(raw))

        # Собираем гиперссылки и кнопки со всего сообщения
        links.update(extract_telegram_links(post, username))

        if post.find(class_=re.compile(r"round_video")):
            node.has_round_video = True
        if post.find(class_=re.compile(r"voice")):
            node.has_voice = True

    # 3. Принудительно запрашиваем закрепленные сообщения и целевой message_id через ?embed=1
    target_messages = pinned_messages.copy()
    if message_id:
        target_messages.add(f"https://t.me/{username}/{message_id}")
        
    for msg_url in target_messages:
        try:
            fetch_url = msg_url if "?embed" in msg_url else f"{msg_url}?embed=1"
            async with await http.get(fetch_url) as response:
                if response.status == 200:
                    raw_specific = await response.text(errors="ignore")
                    specific_soup = BeautifulSoup(raw_specific, "lxml")

                    for post in specific_soup.select(".tgme_widget_message"):
                        text_node = post.select_one(".tgme_widget_message_text")
                        if text_node:
                            post_text = text_node.get_text(" ", strip=True)
                            texts.append(post_text)
                            for raw in URL_RE.findall(post_text):
                                links.add(normalize_url(raw))

                        # Собираем гиперссылки и кнопки из закрепленного/целевого поста
                        links.update(extract_telegram_links(post, username))

                        if post.find(class_=re.compile(r"round_video")):
                            node.has_round_video = True
                        if post.find(class_=re.compile(r"voice")):
                            node.has_voice = True
        except Exception:
            pass

    node.text = "\n".join(texts)[:50000]
    node.links = list(links)
    return node


# ============================================================
# RECURSIVE CRAWLER
# ============================================================

async def crawl(start_url: str) -> list[ResourceNode]:
    start_url = normalize_url(start_url)

    visited = set()
    queue = [(start_url, 0, None)]
    nodes = []

    while queue:
        url, depth, parent = queue.pop(0)

        if url in visited:
            continue

        if depth > MAX_DEPTH:
            continue

        if len(nodes) >= MAX_RESOURCES:
            break

        visited.add(url)

        if is_telegram_url(url):
            node = await fetch_telegram_resource(url)
        else:
            node = await fetch_web_page(url)

        node.depth = depth
        node.parent = parent

        nodes.append(node)

        for link in node.links:
            if link in visited:
                continue

            queue.append((link, depth + 1, url))

    return nodes


# ============================================================
# FEATURES
# ============================================================

PATTERNS = {
    "personal_posts": [
        r"\bщитпост\b",
        r"\bщит ?пост\b",
        r"\bличный пост\b",
        r"\bличное\b",
        r"\bличный контент\b",
        r"\bосновной\b",
        r"\bосновнойтгк\b",
        r"\bосновной тгк\b",
        r"\bоснова\b",
        r"\bповседневн\b",
        r"\bмоя жизнь\b",
        r"\bиз жизни\b",
    ],
    "first_person_lifestyle": [
        r"\bтут я\b",
        r"\bскидываю\b",
        r"\bпривет(,\s*|\s+)",
        r"\bвлог\b",
        r"\bмои будни\b",
        r"\bмоя жизнь\b",
        r"\bкружочек\b",
        r"\bголосовое\b",
        r"\bзабыли\b",
        r"\bбыли в\b",
        r"\bсоседи\b",
    ],
    "work_in_progress": [
        r"\bwip\b",
        r"\bw\.i\.p\b",
        r"\bстадия работы\b",
        r"\bстадия рисунка\b",
        r"\bпроцесс работы\b",
        r"\bпроцесс рисования\b",
        r"\bпроцесс\b",
        r"\bскетч\b",
        r"\bэскиз\b",
    ],
    "reposts": [
        r"\bрепост\b",
        r"\brepost\b",
        r"\bперерепост\b",
    ],
    "self_promotion": [
        r"\bмой канал\b",
        r"\bмоя группа\b",
        r"\bмой тг\b",
        r"\bмой телеграм\b",
        r"\bподпишись\b",
        r"\bподписывайся\b",
        r"\bподписывайтесь\b",
    ],
    "spoilers": [
        r"\bспойлер\b",
        r"\bспойлеры\b",
    ],
    "portfolio": [
        r"\bпортфолио\b",
        r"\bportfolio\b",
        r"\bмои работы\b",
        r"\bпримеры работ\b",
    ],
    "prices": [
        r"\bцена\b",
        r"\bцены\b",
        r"\bпрайс\b",
        r"\bстоимость\b",
        r"\bпрайслист\b",
    ],
    "commissions": [
        r"\bкоммиш",
        r"\bкомиссион",
        r"\bcommission",
        r"\bзаказ\b",
        r"\bзаказы\b",
        r"\bзаказать\b",
    ],
    "contacts": [
        r"\bконтакт\b",
        r"\bконтакты\b",
        r"\bсвязаться\b",
        r"\bдля связи\b",
        r"\btelegram\b",
        r"\bтелеграм\b",
        r"\bтг\b",
    ],
    "personal_photos": [
        r"\bселфи\b",
        r"\bфото с собой\b",
        r"\bмоя фотка\b",
        r"\bмоя фотография\b",
        r"\bфотками\b",
        r"\bфотки\b",
    ],
}


def detect_features(nodes: list[ResourceNode]) -> dict:
    texts = [node.text for node in nodes if node.text]
    combined = "\n".join(texts).lower()

    features = {}

    for feature, patterns in PATTERNS.items():
        features[feature] = any(
            re.search(pattern, combined, re.IGNORECASE)
            for pattern in patterns
        )

    features["has_telegram"] = any(node.telegram for node in nodes)
    features["has_nested_telegram"] = any(
        node.telegram and node.depth > 0 for node in nodes
    )
    features["has_external_links"] = any(node.links for node in nodes)
    
    features["has_round_video"] = any(getattr(node, "has_round_video", False) for node in nodes)
    features["has_voice"] = any(getattr(node, "has_voice", False) for node in nodes)

    features["has_work_signals"] = any(
        features.get(name, False)
        for name in ("portfolio", "prices", "commissions", "contacts")
    )

    return features


# ============================================================
# LEARNING
# ============================================================

async def apply_verified_cases(features: dict) -> Optional[tuple[str, float]]:
    cases = await db.get_verified_cases()

    if not cases:
        return None

    current = normalize_features(features)
    best_verdict = None
    best_similarity = 0.0

    for case in cases:
        try:
            case_features = json.loads(case["features"])
        except Exception:
            continue

        case_features = normalize_features(case_features)
        keys = set(current) | set(case_features)

        if not keys:
            continue

        matches = sum(
            current.get(k, False) == case_features.get(k, False)
            for k in keys
        )

        similarity = matches / len(keys)
        weighted = similarity * (1.0 + min(case["confirmations"], 10) * 0.02)

        if weighted > best_similarity:
            best_similarity = weighted
            best_verdict = case["verdict"]

    if best_verdict and best_similarity >= 0.88:
        return best_verdict, min(0.99, best_similarity)

    return None


# ============================================================
# CLASSIFIER
# ============================================================

async def classify(nodes: list[ResourceNode]) -> Classification:
    features = detect_features(nodes)

    score = 0
    reasons = []
    rules = []

    def add(points: int, reason: str, rule: str):
        nonlocal score
        score += points
        reasons.append(reason)
        rules.append(rule)

    if features.get("personal_posts"):
        add(28, "обнаружены личные посты", "8.1.1")
        
    if features.get("first_person_lifestyle"):
        add(20, "обнаружено общение от первого лица / лайфстайл", "8.1.1")

    if features.get("work_in_progress"):
        add(18, "обнаружены стадии работ / WIP", "8.1.1")

    if features.get("reposts"):
        add(12, "обнаружены репосты", "8.1.1")

    if features.get("self_promotion"):
        add(12, "обнаружен личный самопиар", "8.1.1")

    if features.get("spoilers"):
        add(10, "обнаружены спойлеры", "8.1.1")

    if features.get("personal_photos"):
        add(18, "обнаружен личный фотоконтент", "8.1.1")
        
    if features.get("has_round_video"):
        add(30, "обнаружены кружочки (характерно для личных блогов)", "8.1.1")

    if features.get("has_voice"):
        add(15, "обнаружены голосовые сообщения", "8.1.1")

    if features.get("has_nested_telegram"):
        add(25, "обнаружен Telegram-ресурс в цепочке переходов", "8.2.1")

    work_signals = sum(
        bool(features.get(x))
        for x in ("portfolio", "prices", "commissions", "contacts")
    )

    if work_signals >= 2:
        score -= 10

    verified = await apply_verified_cases(features)
    confidence = min(0.98, 0.50 + score / 200)

    if verified:
        verified_verdict, verified_conf = verified

        if verified_conf >= 0.92:
            if verified_verdict == "PERSONAL":
                score = max(score, 70)
            elif verified_verdict == "WORK":
                score = min(score, 40)

            confidence = max(confidence, verified_conf)

    score = max(0, min(100, score))

    if score >= 70:
        verdict = "PERSONAL"
    elif score >= MIN_ALERT_SCORE:
        verdict = "SUSPICIOUS"
    elif work_signals >= 2 and score < 45:
        verdict = "WORK"
    elif score < 45:
        verdict = "WORK"
    else:
        verdict = "UNKNOWN"

    accessible = [
        node for node in nodes
        if node.accessible and (node.text or node.links)
    ]

    if not accessible:
        verdict = "UNKNOWN"
        confidence = 0.15
        reasons = ["содержимое ресурса недоступно для автоматической проверки"]
        rules = []

    if not reasons:
        reasons = ["явных признаков личного ресурса не обнаружено"]

    telegram_resources = [node.url for node in nodes if node.telegram]

    return Classification(
        verdict=verdict,
        score=score,
        confidence=confidence,
        reasons=list(dict.fromkeys(reasons)),
        rules=list(dict.fromkeys(rules)),
        features=features,
        telegram_resources=telegram_resources,
    )


# ============================================================
# REPORT
# ============================================================

def build_report(
    scan_id: int,
    result: Classification,
    original_url: str,
    nodes: list[ResourceNode],
    source_link: Optional[str],
) -> str:

    report = (
        "🚨 <b>ПРОВЕРКА РЕСУРСА</b>\n\n"
        f"<b>Вердикт:</b> {VERDICTS.get(result.verdict, result.verdict)}\n"
        f"<b>Оценка риска:</b> {result.score}/100\n"
        f"<b>Уверенность:</b> {round(result.confidence * 100)}%\n\n"
        f"<b>Исходная ссылка:</b>\n"
        f'<a href="{html.escape(original_url)}">'
        f"{html.escape(shorten(original_url, 300))}"
        f"</a>\n"
    )

    if source_link:
        report += (
            "\n<b>Исходное сообщение:</b>\n"
            f'<a href="{source_link}">Открыть сообщение</a>\n'
        )

    report += "\n<b>Причины:</b>\n"
    for reason in result.reasons:
        report += f"• {html.escape(reason)}\n"

    if result.rules:
        report += "\n<b>Связанные статьи:</b>\n"
        for rule in result.rules:
            report += f"• {rule}\n"

    report += (
        "\n<b>Обход:</b>\n"
        f"• Ресурсов: {len(nodes)}\n"
        f"• Telegram: {len(result.telegram_resources)}\n"
        f"• Максимальная глубина: {MAX_DEPTH}\n"
    )

    if result.telegram_resources:
        report += "\n<b>Telegram-ресурсы:</b>\n"
        for url in result.telegram_resources[:10]:
            report += (
                f'• <a href="{html.escape(url)}">'
                f"{html.escape(shorten(url, 180))}"
                "</a>\n"
            )

    report += f"\n<code>SCAN #{scan_id}</code>"
    return report


def feedback_keyboard(scan_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Верно",
                    callback_data=f"feedback:{scan_id}:correct",
                ),
                InlineKeyboardButton(
                    text="⚠️ Частично",
                    callback_data=f"feedback:{scan_id}:partial",
                ),
                InlineKeyboardButton(
                    text="❌ Ошибка",
                    callback_data=f"feedback:{scan_id}:wrong",
                ),
            ],
        ]
    )


def correction_keyboard(scan_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Рабочий",
                    callback_data=f"correct:{scan_id}:WORK",
                ),
                InlineKeyboardButton(
                    text="🔴 Личный",
                    callback_data=f"correct:{scan_id}:PERSONAL",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🟠 Подозрительный",
                    callback_data=f"correct:{scan_id}:SUSPICIOUS",
                ),
                InlineKeyboardButton(
                    text="⚪ Неизвестно",
                    callback_data=f"correct:{scan_id}:UNKNOWN",
                ),
            ],
        ]
    )


# ============================================================
# BOT HANDLERS
# ============================================================

router = Router()


def is_mod(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def process_url(message: Message, url: str):
    started = time.monotonic()
    await message.reply("🔎 Проверяю ссылку и связанные ресурсы…")

    try:
        nodes = await crawl(url)
        result = await classify(nodes)
    except Exception:
        log.exception("Analysis failed")
        await message.reply("❌ Во время проверки произошла ошибка.")
        return

    elapsed = round(time.monotonic() - started, 2)

    source_link = source_message_link(
        message.chat.id,
        message.message_id,
    )

    scan_id = await db.create_scan(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        submitted_by=message.from_user.id if message.from_user else 0,
        original_url=url,
        result=result,
        chain=[node.url for node in nodes],
    )

    async with aiosqlite.connect(DB_PATH) as database:
        now = utc_now()

        for node in nodes:
            await database.execute(
                """
                INSERT OR REPLACE INTO resources (
                    url,
                    resource_type,
                    features,
                    verdict,
                    score,
                    verified,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.url,
                    "telegram" if node.telegram else "web",
                    json.dumps(result.features, ensure_ascii=False),
                    result.verdict,
                    result.score,
                    0,
                    now,
                ),
            )

        await database.commit()

    if result.verdict == "WORK":
        await message.reply(
            "🟢 <b>Явных признаков личного ресурса не обнаружено.</b>\n\n"
            f"Уверенность: {round(result.confidence * 100)}%\n"
            f"Проверено ресурсов: {len(nodes)}\n"
            f"Время: {elapsed} сек."
        )
        return

    report = build_report(
        scan_id=scan_id,
        result=result,
        original_url=url,
        nodes=nodes,
        source_link=source_link,
    )

    if result.verdict == "UNKNOWN":
        report = "🟠 <b>ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА</b>\n\n" + report

    await message.bot.send_message(
        chat_id=MOD_CHAT_ID,
        text=report,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=feedback_keyboard(scan_id),
    )

    await message.reply("⚠️ Результат отправлен модераторам на проверку.")


@router.message(F.text)
async def message_handler(message: Message):
    urls = extract_urls(message)
    for url in urls[:5]:
        await process_url(message, url)


@router.message(F.caption)
async def caption_handler(message: Message):
    urls = extract_urls(message)
    for url in urls[:5]:
        await process_url(message, url)


# ============================================================
# FEEDBACK HANDLERS
# ============================================================

@router.callback_query(F.data.startswith("feedback:"))
async def feedback_handler(callback: CallbackQuery):
    if not callback.message:
        return

    if callback.message.chat.id != MOD_CHAT_ID:
        await callback.answer("Недоступно", show_alert=True)
        return

    try:
        _, scan_id, rating = callback.data.split(":")
        scan_id = int(scan_id)
    except Exception:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    scan = await db.get_scan(scan_id)

    if not scan:
        await callback.answer("Проверка не найдена", show_alert=True)
        return

    if rating == "correct":
        await db.add_feedback(scan_id, callback.from_user.id, "correct")

        try:
            raw_features = scan["features"] if scan["features"] is not None else "{}"
            raw_rules = scan["rules"] if scan["rules"] is not None else "[]"

            features = json.loads(raw_features)
            rules = json.loads(raw_rules)

            await db.save_verified_case(
                features=features,
                verdict=scan["verdict"],
                rules=rules,
                resource_type=(
                    "telegram" if "t.me/" in scan["original_url"] else "web"
                ),
            )
        except Exception:
            log.exception("Could not save verified case")

        await callback.answer("Сохранено как подтверждённый кейс")

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(
            f"#{scan_id}: ✅ результат подтверждён модератором."
        )
        return

    if rating in {"partial", "wrong"}:
        await db.add_feedback(scan_id, callback.from_user.id, rating)
        await callback.answer("Выбери правильный вердикт")

        await callback.message.answer(
            f"Проверка #{scan_id}\nКакой вердикт должен быть?",
            reply_markup=correction_keyboard(scan_id),
        )


@router.callback_query(F.data.startswith("correct:"))
async def correction_handler(callback: CallbackQuery):
    if not callback.message:
        return

    if callback.message.chat.id != MOD_CHAT_ID:
        await callback.answer("Недоступно", show_alert=True)
        return

    try:
        _, scan_id, verdict = callback.data.split(":")
        scan_id = int(scan_id)
    except Exception:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    scan = await db.get_scan(scan_id)

    if not scan:
        await callback.answer("Проверка не найдена", show_alert=True)
        return

    await db.add_feedback(
        scan_id=scan_id,
        moderator_id=callback.from_user.id,
        rating="wrong",
        corrected_verdict=verdict,
    )

    try:
        raw_features = scan["features"] if scan["features"] is not None else "{}"
        raw_rules = scan["rules"] if scan["rules"] is not None else "[]"

        features = json.loads(raw_features)
        rules = json.loads(raw_rules)

        await db.save_verified_case(
            features=features,
            verdict=verdict,
            rules=rules,
            resource_type=(
                "telegram" if "t.me/" in scan["original_url"] else "web"
            ),
        )
    except Exception:
        log.exception("Could not save corrected case")

    await callback.answer("Исправление сохранено")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        f"#{scan_id}: исправлено на <b>{VERDICTS.get(verdict, verdict)}</b>.\n"
        "Кейс сохранён для будущего анализа.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# COMMANDS
# ============================================================

@router.message(Command("stats"))
async def stats_handler(message: Message):
    if not message.from_user or not is_mod(message.from_user.id):
        return

    scans, feedback = await db.stats()

    total = scans["total"] or 0
    personal = scans["personal"] or 0
    work = scans["work"] or 0
    suspicious = scans["suspicious"] or 0
    unknown = scans["unknown"] or 0

    feedback_total = feedback["total"] or 0
    correct = feedback["correct"] or 0
    partial = feedback["partial"] or 0
    wrong = feedback["wrong"] or 0

    accuracy = (correct / feedback_total * 100) if feedback_total else 0

    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"Всего проверок: <b>{total}</b>\n\n"
        f"🟢 Рабочих: {work}\n"
        f"🔴 Личных: {personal}\n"
        f"🟠 Подозрительных: {suspicious}\n"
        f"⚪ Неизвестных: {unknown}\n\n"
        "<b>Оценки модераторов:</b>\n"
        f"Всего: {feedback_total}\n"
        f"✅ Верно: {correct}\n"
        f"⚠️ Частично: {partial}\n"
        f"❌ Ошибка: {wrong}\n\n"
        f"<b>Текущая точность:</b> {accuracy:.1f}%"
    )


@router.message(Command("rules"))
async def rules_handler(message: Message):
    if not message.from_user or not is_mod(message.from_user.id):
        return

    text = "📚 <b>Правила классификации</b>\n\n"

    for rule, description in RULES_TEXT.items():
        text += f"<b>{rule}</b> — {html.escape(description)}\n\n"

    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "🔎 <b>Resource Checker</b>\n\n"
        "Просто отправь ссылку в чат.\n"
        "Бот проверит ресурс и цепочку связанных ссылок.\n\n"
        "<b>Команды модератора:</b>\n"
        "/stats — статистика\n"
        "/rules — правила классификации\n"
        "/help — помощь",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# STARTUP
# ============================================================

async def main():
    global http

    await db.init()
    http = HttpClient()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot started")
    log.info("Database: %s", DB_PATH)
    log.info("Moderator chat: %s", MOD_CHAT_ID)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await http.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")