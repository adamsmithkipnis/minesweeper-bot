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
from datetime import datetime
from types import SimpleNamespace

import config
import votes

logger = logging.getLogger(__name__)

_client = None
_dry_counter = 0

# Maps a bare name (no '@', lowercased) to a DID, so mentions in post text
# become real clickable links. Populated at login with both the full handle
# and the short form the post copy uses.
_did_by_name = {}

# The CID of every post this process made, by URI. A reply needs its
# parent's CID as well as its URI, and the posting functions return only the
# URI, which every caller already stores; this is how the bot can thread a
# follow-up under a post it has just made.
_cids = {}

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
        return _remember_cid(_write_dry(text, kind, image, alt, extra_dids))
    return _remember_cid(_send_or_adopt(text, lambda: _client.send_image(
        text=text, image=image, image_alt=alt,
        facets=build_facets(text, extra_dids),
    )))


def post_text(text: str, kind: str = "post", extra_dids: dict | None = None) -> str:
    text = clamp(text)
    if _dry():
        return _remember_cid(_write_dry(text, kind, extra_dids=extra_dids))
    return _remember_cid(_send_or_adopt(text, lambda: _client.send_post(
        text=text, facets=build_facets(text, extra_dids))))


def _remember_cid(posted) -> str:
    """Note a new post's CID and return its URI; a dry run's is a stand-in."""
    if isinstance(posted, str):
        _cids[posted] = "dry"
        return posted
    _cids[posted.uri] = getattr(posted, "cid", "") or ""
    return posted.uri


def cid_of(uri: str) -> str:
    """The CID of a post this process made, or '' if it made no such post."""
    return _cids.get(uri, "")


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
    return _send_or_adopt(text, lambda: _client.send_post(
        text=text, facets=build_facets(text, extra_dids),
        reply_to=models.AppBskyFeedPost.ReplyRef(parent=parent, root=root),
    ), parent_uri=parent_uri).uri


# A post can be published and still fail here. The request reaches Bluesky,
# the post goes up, and the response times out on its way back, so the bot
# never learns the URI. For a board post that is the worst outcome: players
# reply to a post the bot does not know it made, and every one of them is
# ignored. So a failure is only believed once the account's newest posts have
# been checked for the one we tried to make.
_ADOPT_ATTEMPTS = 3
_ADOPT_PAUSE = 2         # seconds between looks; the commit may land late
_ADOPT_LOOKBACK = 25     # newest records to search
_ADOPT_SLACK = 60        # seconds; createdAt comes from our own clock


class _Adopted:
    """Stands in for atproto's response: the two fields the callers read."""

    def __init__(self, uri: str, cid: str):
        self.uri, self.cid = uri, cid


def _send_or_adopt(text: str, send, parent_uri: str = ""):
    started = time.time()
    try:
        return send()
    except Exception:
        found = find_published(text, started - _ADOPT_SLACK, parent_uri)
        if found is None:
            raise
        logger.warning("Post failed locally but Bluesky published it; "
                       "adopting %s", found.uri)
        return found


def find_published(text: str, since: float, parent_uri: str = ""):
    """Our own post with exactly this text, made after `since`, or None.

    A reply must also sit under `parent_uri`: two players can be sent the
    same words, and adopting the other one's reply would misfile it.
    """
    for attempt in range(_ADOPT_ATTEMPTS):
        if attempt:
            time.sleep(_ADOPT_PAUSE)
        try:
            page = _client.com.atproto.repo.list_records({
                "repo": _client.me.did, "collection": POST_COLLECTION,
                "limit": _ADOPT_LOOKBACK,
            })
        except Exception as exc:
            logger.warning("Could not check whether the post went up: %s", exc)
            continue
        for record in page.records:        # newest first
            value = record.value
            reply = getattr(value, "reply", None)
            parent = getattr(getattr(reply, "parent", None), "uri", "") or ""
            if (getattr(value, "text", "") == text
                    and parent == parent_uri
                    and _timestamp(getattr(value, "created_at", "")) >= since):
                return _Adopted(record.uri, record.cid)
    return None


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(
            (value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Reading replies
# ---------------------------------------------------------------------------

def get_profile_counts() -> dict | None:
    """{'followers', 'follows', 'posts'} for the bot's account, or None in a
    dry run, where there is no network to ask."""
    if _dry():
        return None
    profile = _client.app.bsky.actor.get_profile({"actor": _client.me.did})
    return {"followers": profile.followers_count or 0,
            "follows": profile.follows_count or 0,
            "posts": profile.posts_count or 0}


def own_did() -> str:
    """The bot's own DID, or '' when there is no login (dry runs)."""
    return "" if _dry() else (getattr(_client.me, "did", "") or "")


def _author_did(item) -> str:
    return getattr(getattr(getattr(item, "post", None), "author", None),
                   "did", "") or ""


def get_replies(post_uri: str) -> list:
    """Replies to a post, as votes.Reply records: the direct ones, plus each
    player's own follow-ups underneath theirs (see votes.with_follow_ups)."""
    return read_replies(post_uri)[0]


# On 27 October Bluesky answered Turn 11's thread with a 200 and none of its
# three replies, while the post's own reply count said three. The empty answer
# is cached per request URL (the same thread could be empty at one depth and
# whole at the next), so asking again the same way only gets it again. Each
# retry asks for a different depth, and the bot's reply notifications, which
# are indexed apart from threads, are a second witness. Replies older than the
# newest _NOTIFICATION_LIMIT notifications are the thread's job alone.
_THREAD_DEPTHS = (votes.FOLLOW_UP_DEPTH, votes.FOLLOW_UP_DEPTH - 1,
                  votes.FOLLOW_UP_DEPTH + 1)
_NOTIFICATION_LIMIT = 100


def read_replies(post_uri: str) -> tuple:
    """(replies, complete) for a post.

    The replies are the thread's (see get_replies) plus any direct reply that
    only the bot's notifications know about. `complete` is False when the
    post's own reply count is still higher than the direct replies found after
    every depth has been tried; the caller decides whether to wait.
    """
    if _dry() or not post_uri:
        return [], True
    best = None
    for depth in _THREAD_DEPTHS:
        response = _client.app.bsky.feed.get_post_thread(
            {"uri": post_uri, "depth": depth})
        direct = [item for item in
                  getattr(response.thread, "replies", None) or []
                  if getattr(item, "post", None) is not None]
        counted = getattr(getattr(response.thread, "post", None),
                          "reply_count", None) or 0
        if best is None or len(direct) > len(best[0]):
            best = (direct, counted)
        if counted <= len(direct):
            break
        logger.warning("Thread %s at depth %d shows %d of %d replies",
                       post_uri, depth, len(direct), counted)
    direct, counted = best

    raw = votes.with_follow_ups(
        direct, _author_did, lambda item: getattr(item, "replies", None))
    out = [_as_reply(item.post) for item in raw
           if getattr(item, "post", None) is not None]
    seen = {r.uri for r in out}
    direct_uris = {getattr(item.post, "uri", "") for item in direct}
    for post in _notified_replies(post_uri):
        if post.uri not in seen:
            out.append(_as_reply(post))
            seen.add(post.uri)
            direct_uris.add(post.uri)
    return out, counted <= len(direct_uris)


def _notified_replies(post_uri: str) -> list:
    """Direct replies to post_uri among the bot's recent reply notifications,
    shaped like a thread's post views. A failure here costs nothing: the
    thread is still read."""
    try:
        response = _client.app.bsky.notification.list_notifications(
            {"limit": _NOTIFICATION_LIMIT, "reasons": ["reply"]})
    except Exception:
        logger.exception("Failed to read reply notifications")
        return []
    found = []
    for note in getattr(response, "notifications", None) or []:
        record = getattr(note, "record", None)
        parent = getattr(getattr(record, "reply", None), "parent", None)
        if getattr(parent, "uri", "") != post_uri:
            continue
        found.append(SimpleNamespace(author=note.author, record=record,
                                     uri=note.uri, cid=note.cid))
    return found


def _as_reply(post) -> votes.Reply:
    record = getattr(post, "record", None)
    reply_ref = getattr(record, "reply", None)
    root = getattr(reply_ref, "root", None)
    return votes.Reply(
        did=getattr(post.author, "did", "") or "",
        handle=getattr(post.author, "handle", "") or "",
        text=getattr(record, "text", "") or "",
        uri=getattr(post, "uri", "") or "",
        cid=getattr(post, "cid", "") or "",
        created_at=getattr(record, "created_at", "") or "",
        root_uri=getattr(root, "uri", "") or "",
        root_cid=getattr(root, "cid", "") or "",
    )


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
