"""Read-only Ed Discussion client and forum sync into the local corpus.

Only GET endpoints are used. Avoid `?view=1` and the lesson completion endpoints: those
record views/completions against the student's account.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable

import httpx

from .classify import parse_week, term_key, unit_codes
from .config import ED_BASE, ED_WEB, secret
from .store import Store

STAFF_ROLES = {"admin": "Staff", "staff": "Staff", "lecturer": "Staff", "tutor": "Tutor", "mentor": "Tutor"}


class EdError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Ed:
    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=ED_BASE + "/",
            headers={"Authorization": f"Bearer {secret('EDSTEM_API_KEY')}"},
            timeout=60,
            follow_redirects=False,
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        response = None
        for attempt in range(6):
            response = self.http.get(path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                raise EdError(response.status_code, f"{response.status_code} for {path}")
            return response.json()
        raise EdError(response.status_code if response else 0, f"gave up on {path}")

    def courses(self) -> list[dict]:
        return [entry.get("course", entry) for entry in self.get("user").get("courses", [])]


def match_course(ed_courses: list[dict], aliases: list[str], term: tuple[int, int] | None) -> dict | None:
    """Find the Ed course for a Canvas unit: shared unit code and same semester."""
    for course in ed_courses:
        codes = unit_codes(course.get("code"), course.get("name"))
        same_term = term_key(f"{course.get('session')} {course.get('year')}") == term
        if set(codes) & set(aliases) and same_term:
            return course
    return None


def _who(user_id: int | None, anonymous: bool, users: dict[int, dict]) -> str:
    if anonymous or not user_id:
        return "Anonymous"
    return STAFF_ROLES.get((users.get(user_id) or {}).get("course_role", ""), "Student")


def render_thread(thread: dict, users: dict[int, dict]) -> tuple[str, bool]:
    """Markdown for a thread. Names are reduced to roles. Returns (markdown, has_staff_reply)."""
    staff_reply = False

    def comments(items: list[dict] | None, depth: int) -> list[str]:
        nonlocal staff_reply
        lines = []
        for item in items or []:
            who = _who(item.get("user_id"), item.get("is_anonymous", False), users)
            staff_reply |= who != "Student" and who != "Anonymous"
            flag = ", endorsed" if item.get("is_endorsed") else ""
            text = (item.get("document") or "").strip().replace("\n", "\n" + "  " * (depth + 1))
            lines.append(f"{'  ' * depth}- **{who}{flag}:** {text}")
            lines += comments(item.get("comments"), depth + 1)
        return lines

    category = " / ".join(filter(None, [thread.get("category"), thread.get("subcategory"), thread.get("subsubcategory")]))
    author = _who(thread.get("user_id"), thread.get("is_anonymous", False), users)
    lines = [
        f"Category: {category or 'none'}",
        f"Type: {thread.get('type')} | Posted {str(thread.get('created_at') or '')[:10]} by {author}"
        f" | Answered: {bool(thread.get('is_answered'))} | Votes: {thread.get('vote_count', 0)}",
        "",
        "## Post",
        "",
        (thread.get("document") or "").strip(),
    ]
    for number, answer in enumerate(thread.get("answers") or [], 1):
        who = _who(answer.get("user_id"), answer.get("is_anonymous", False), users)
        staff_reply |= who in ("Staff", "Tutor")
        flags = [who]
        if answer.get("is_endorsed"):
            flags.append("endorsed")
        if thread.get("accepted_id") and thread.get("accepted_id") == answer.get("id"):
            flags.append("accepted")
        lines += ["", f"## Answer {number} ({', '.join(flags)})", "", (answer.get("document") or "").strip()]
        lines += comments(answer.get("comments"), 0)
    if thread.get("comments"):
        lines += ["", "## Comments", ""] + comments(thread.get("comments"), 0)
    return "\n".join(lines), staff_reply


class EdSync:
    def __init__(self, ed: Ed, store: Store, log: Callable[[str], None] = print) -> None:
        self.ed, self.store, self.log = ed, store, log

    def run(self, unit_key: str, course: dict) -> Counter:
        stats: Counter = Counter()
        course_id = course["id"]
        threads: dict[int, dict] = {}
        offset = 0
        while True:
            batch = self.ed.get(f"courses/{course_id}/threads", {"limit": 100, "offset": offset, "sort": "new"}).get("threads") or []
            for thread in batch:
                threads[thread["id"]] = thread  # pinned threads repeat across pages
            if len(batch) < 100:
                break
            offset += 100

        for summary in threads.values():
            doc_id = f"ed:thread:{summary['id']}"
            fingerprint = "|".join(str(summary.get(k)) for k in ("updated_at", "reply_count", "vote_count", "is_answered"))
            if self.store.is_fresh(doc_id, fingerprint):
                stats["unchanged"] += 1
                continue
            try:
                full = self.ed.get(f"threads/{summary['id']}")
            except EdError as error:
                stats["unavailable"] += 1
                if error.status not in (401, 403, 404):
                    self.log(f"    ! thread {summary['id']}: {error}")
                continue
            thread = full.get("thread") or summary
            users = {user["id"]: user for user in full.get("users") or []}
            body, staff_reply = render_thread(thread, users)
            category = " / ".join(filter(None, [thread.get("category"), thread.get("subcategory"), thread.get("subsubcategory")]))
            title = thread.get("title") or "Untitled thread"
            tags = [t for t, on in (("staff-reply", staff_reply), ("announcement", thread.get("type") == "announcement")) if on]
            self.store.put(dict(
                doc_id=doc_id, unit=unit_key, source="ed", kind="forum",
                week=parse_week(title, thread.get("subcategory"), thread.get("subsubcategory"), thread.get("category")),
                title=f"#{thread.get('number')} {title}", module=category, module_pos=999, item_pos=0,
                tags=",".join(tags), url=f"{ED_WEB}/courses/{course_id}/discussion/{summary['id']}",
                raw_path=None, parent_id=None, fingerprint=fingerprint, updated_at=summary.get("updated_at"),
            ), body)
            stats["updated"] += 1
        stats["threads"] = len(threads)
        return stats
