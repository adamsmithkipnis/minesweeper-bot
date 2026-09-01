"""AT Protocol wrapper: posting, reading replies, and deleting posts.

Vote parsing lives in votes.py; this module only adapts Bluesky's objects into
the small records that module works with.

Set POST_MODE=dry to run the whole bot with no network and no credentials:
every post is written to DRY_DIR with its text, character count, facet byte
ranges and image, and reply fetching returns nothing (so the bot plays its own
moves and a full board can be watched through to the end).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import config
import votes

logger = logging.getLogger(__name__)

_client = None
_dry_counter = 0

# Maps a bare name (no '@', lowercased) to a DID, so mentions in post text
# become real clickable links. Populated at login with both the full handle
# and the short form the post copy uses.
_did_by_name = {}

POST_LIMIT = 300        # Bluesky's per-post grapheme limit
ALT_LIMIT = 2000


def _dry() -> bool:
    return config.POST_MODE == "dry"


def login() -> None:
    """Authenticate, or set up the dry-run stand-in."""
    global _client
    if _dry():
        os.makedirs(config.DRY_DIR, exist_ok=True)
        handle = config.HANDLE or "minesweeper.bsky.social"
        _register_handle(handle, "did:plc:dryrun")
        logger.info("POST_MODE=dry — writing posts to %s, no network",
                    config.DRY_DIR)
        return

    from atproto import Client
    client = Client()
    profile = client.login(config.HANDLE, config.APP_PASSWORD)
    _client = client
    _register_handle(config.HANDLE, profile.did)
    logger.info("Logged in as %s did=%s", config.HANDLE, profile.did)


def _register_handle(handle: str, did: str) -> None:
    _did_by_name[handle.lower()] = did
    _did_by_name[handle.split(".", 1)[0].lower()] = did


def login_with_retry() -> None:
    """Keep retrying so a boot with no network waits instead of crash-looping."""
    delay = 15
    while True:
        try:
            login()
            return
        except Exception:
            logger.exception("Login failed (network down?); retrying in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 600)


def get_did() -> str:
    return _did_by_name.get((config.HANDLE or "").lower(), "")


# ---------------------------------------------------------------------------
# Richtext facets — what makes @mentions and #hashtags clickable
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z0-9_]+)")


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def facet_ranges(text: str, extra_dids: dict | None = None) -> list:
    """(kind, value, byte_start, byte_end) for every mention and tag.

    Ranges are UTF-8 **byte** offsets, not character offsets. The post copy is
    full of multi-byte emoji, so string indices would point into the wrong
    place and the link would land on the wrong words.

    Split out from `build_facets` so the offsets can be asserted in a test and
    printed by the dry run without constructing atproto models.
    """
    lookup = dict(_did_by_name)
    for handle, did in (extra_dids or {}).items():
        lookup[handle.lstrip("@").lower()] = did

    out = []
    for match in _MENTION_RE.finditer(text):
        did = lookup.get(match.group(1).lower())
        if did is None:
            continue        # unknown handle: leave as plain text rather than guess
        out.append(("mention", did,
                    _byte_len(text[:match.start()]), _byte_len(text[:match.end()])))
    for match in _TAG_RE.finditer(text):
        out.append(("tag", match.group(1),
                    _byte_len(text[:match.start()]), _byte_len(text[:match.end()])))
    return out


def build_facets(text: str, extra_dids: dict | None = None) -> list:
    from atproto import models
    facets = []
    for kind, value, start, end in facet_ranges(text, extra_dids):
        feature = (models.AppBskyRichtextFacet.Mention(did=value)
                   if kind == "mention"
                   else models.AppBskyRichtextFacet.Tag(tag=value))
        facets.append(models.AppBskyRichtextFacet.Main(
            features=[feature],
            index=models.AppBskyRichtextFacet.ByteSlice(byte_start=start,
                                                        byte_end=end),
        ))
    return facets


def clamp(text: str, limit: int = POST_LIMIT) -> str:
    """Trim to Bluesky's limit.

    Facets are always built from the clamped text, so a mention cut in half
    simply loses its link instead of pointing past the end of the post.
    """
    if len(text) <= limit:
        return text
    logger.warning("Post text over %d chars; trimming", limit)
    return text[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _write_dry(text: str, kind: str, image: bytes = b"", alt: str = "",
               extra_dids: dict | None = None) -> str:
    global _dry_counter
    _dry_counter += 1
    stem = os.path.join(config.DRY_DIR, f"{_dry_counter:03d}-{kind}")
    facets = facet_ranges(text, extra_dids)
    with open(f"{stem}.txt", "w") as handle:
        handle.write(text + "\n")
        handle.write(f"\n--- {len(text)}/{POST_LIMIT} chars, "
                     f"{_byte_len(text)} bytes ---\n")
        for kindname, value, start, end in facets:
            excerpt = text.encode("utf-8")[start:end].decode("utf-8", "replace")
            handle.write(f"{kindname} {value} bytes {start}-{end} -> {excerpt!r}\n")
        if alt:
            handle.write(f"\n--- alt text, {len(alt)} chars ---\n{alt}\n")
    if image:
        with open(f"{stem}.png", "wb") as handle:
            handle.write(image)
    logger.info("[dry] %s (%d chars, %d facets) -> %s.txt",
                kind, len(text), len(facets), stem)
    return f"at://did:plc:dryrun/app.bsky.feed.post/{_dry_counter:03d}"


def post_with_image(text: str, image: bytes, alt: str = "", kind: str = "post",
                    extra_dids: dict | None = None) -> str:
    """Post text plus the board image; return the AT URI."""
    text = clamp(text)
    alt = clamp(alt or "Minesweeper board.", ALT_LIMIT)
    if _dry():
        return _write_dry(text, kind, image, alt, extra_dids)
    return _client.send_image(
        text=text, image=image, image_alt=alt,
        facets=build_facets(text, extra_dids),
    ).uri


def post_text(text: str, kind: str = "post", extra_dids: dict | None = None) -> str:
    text = clamp(text)
    if _dry():
        return _write_dry(text, kind, extra_dids=extra_dids)
    return _client.send_post(
        text=text, facets=build_facets(text, extra_dids)).uri


def post_reply(text: str, parent_uri: str, parent_cid: str,
               root_uri: str = "", root_cid: str = "",
               kind: str = "credit", extra_dids: dict | None = None) -> str:
    """Reply to a follower's post — this is what notifies them."""
    text = clamp(text)
    if _dry():
        return _write_dry(text, kind, extra_dids=extra_dids)

    from atproto import models
    parent = models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid)
    root = (models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid)
            if root_uri and root_cid else parent)
    return _client.send_post(
        text=text, facets=build_facets(text, extra_dids),
        reply_to=models.AppBskyFeedPost.ReplyRef(parent=parent, root=root),
    ).uri


# ---------------------------------------------------------------------------
# Reading replies
# ---------------------------------------------------------------------------

def get_replies(post_uri: str) -> list:
    """Direct replies to a post, as votes.Reply records."""
    if _dry() or not post_uri:
        return []
    response = _client.app.bsky.feed.get_post_thread(
        {"uri": post_uri, "depth": 1})
    raw = getattr(response.thread, "replies", None) or []

    out = []
    for item in raw:
        post = getattr(item, "post", None)
        if post is None:
            continue
        record = getattr(post, "record", None)
        reply_ref = getattr(record, "reply", None)
        root = getattr(reply_ref, "root", None)
        out.append(votes.Reply(
            did=getattr(post.author, "did", "") or "",
            handle=getattr(post.author, "handle", "") or "",
            text=getattr(record, "text", "") or "",
            uri=getattr(post, "uri", "") or "",
            cid=getattr(post, "cid", "") or "",
            created_at=getattr(record, "created_at", "") or "",
            root_uri=getattr(root, "uri", "") or "",
            root_cid=getattr(root, "cid", "") or "",
        ))
    return out


# ---------------------------------------------------------------------------
# Enumerating and deleting posts (the reset tool)
# ---------------------------------------------------------------------------

POST_COLLECTION = "app.bsky.feed.post"
_DELETE_BATCH = 50
_RATE_LIMIT_PAUSE = 60


def rkey_from_uri(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


def list_all_posts() -> list:
    """Every post in the account's repo, oldest first.

    Walks listRecords with a cursor, so it sees the account's actual contents
    — including anything posted before the bot started logging.
    """
    did = _client.me.did
    out, cursor = [], None
    while True:
        page = _client.com.atproto.repo.list_records({
            "repo": did, "collection": POST_COLLECTION,
            "limit": 100, "cursor": cursor,
        })
        for record in page.records:
            out.append({
                "uri": record.uri,
                "cid": record.cid,
                "rkey": rkey_from_uri(record.uri),
                "text": getattr(record.value, "text", "") or "",
                "created_at": getattr(record.value, "created_at", "") or "",
            })
        cursor = getattr(page, "cursor", None)
        if not cursor or not page.records:
            break
    out.sort(key=lambda p: p["created_at"])
    return out


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def delete_posts(rkeys: list, on_progress=None) -> tuple:
    """Delete posts by record key, in batches.

    A rate limit is treated as normal: pause and retry rather than aborting
    and leaving the account half-cleaned.
    """
    from atproto import models
    did = _client.me.did
    deleted, failed = [], []
    total = len(rkeys)

    index = 0
    while index < total:
        batch = rkeys[index:index + _DELETE_BATCH]
        writes = [models.ComAtprotoRepoApplyWrites.Delete(
            collection=POST_COLLECTION, rkey=rkey) for rkey in batch]
        try:
            _client.com.atproto.repo.apply_writes({"repo": did, "writes": writes})
            deleted.extend(batch)
            index += len(batch)
        except Exception as exc:
            if _is_rate_limited(exc):
                logger.warning("Rate limited; pausing %ds", _RATE_LIMIT_PAUSE)
                time.sleep(_RATE_LIMIT_PAUSE)
                continue
            # One bad record shouldn't stop the run — retry singly.
            logger.warning("Batch delete failed (%s); retrying individually", exc)
            for rkey in batch:
                try:
                    _client.com.atproto.repo.delete_record({
                        "repo": did, "collection": POST_COLLECTION, "rkey": rkey})
                    deleted.append(rkey)
                except Exception as inner:
                    if _is_rate_limited(inner):
                        time.sleep(_RATE_LIMIT_PAUSE)
                        continue
                    logger.warning("Could not delete %s: %s", rkey, inner)
                    failed.append(rkey)
            index += len(batch)
        if on_progress:
            on_progress(len(deleted) + len(failed), total)
    return deleted, failed
