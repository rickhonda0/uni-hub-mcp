"""Read-only Canvas client and course sync into the local corpus.

Students at USyd cannot list course files or pages directly (403/404), so content is
reached the way the Canvas UI reaches it: through module items, plus file links found
inside pages, assignments and announcements.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import extract
from .classify import classify, is_solution, parse_week, slug, term_key, unit_codes
from .config import CANVAS_BASE, MAX_DOWNLOAD_BYTES, ROOT, preferred_unit_names, secret
from .store import Store


class CanvasError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Canvas:
    """Minimal GET-only Canvas client. The token is only ever sent to the Canvas host."""

    def __init__(self) -> None:
        self.base = CANVAS_BASE
        self.host = urlparse(self.base).netloc
        self._headers = {"Authorization": f"Bearer {secret('CANVAS_API_KEY')}"}
        self.http = httpx.Client(timeout=90, follow_redirects=False)

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            if urlparse(path).netloc != self.host:
                raise CanvasError(0, f"refusing non-Canvas host {urlparse(path).netloc}")
            return path
        return f"{self.base}/api/v1/{path.lstrip('/')}"

    def _request(self, url: str, params: dict | None) -> httpx.Response:
        response = None
        for attempt in range(5):
            response = self.http.get(url, params=params, headers=self._headers)
            throttled = response.status_code == 429 or (
                response.status_code == 403 and "rate limit" in response.text.lower()
            )
            if throttled or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                raise CanvasError(response.status_code, f"{response.status_code} for {urlparse(url).path}")
            return response
        raise CanvasError(response.status_code if response else 0, f"gave up on {urlparse(url).path}")

    def get(self, path: str, params: dict | None = None):
        return self._request(self._url(path), params).json()

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 100) -> list:
        url, query, out = self._url(path), {"per_page": 100, **(params or {})}, []
        for _ in range(max_pages):
            response = self._request(url, query)
            data = response.json()
            out.extend(data if isinstance(data, list) else [data])
            next_url = response.links.get("next", {}).get("url")
            if not next_url or urlparse(next_url).netloc != self.host:
                break
            url, query = next_url, None
        return out

    def download(self, url: str, dest: Path) -> Path:
        """Stream a file. Redirects to file storage are followed without the token."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = self._headers if urlparse(url).netloc == self.host else {}
        for _ in range(6):
            with self.http.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    if urlparse(url).netloc != self.host:
                        headers = {}
                    continue
                if response.status_code >= 400:
                    raise CanvasError(response.status_code, f"{response.status_code} downloading {dest.name}")
                partial = dest.with_name(dest.name + ".part")
                with open(partial, "wb") as handle:
                    for block in response.iter_bytes():
                        handle.write(block)
                partial.replace(dest)
                return dest
        raise CanvasError(0, "too many redirects")


# ---- unit discovery ----------------------------------------------------------

@dataclass
class Unit:
    key: str
    aliases: list[str]
    name: str
    term: str
    term_key: tuple[int, int] | None
    canvas_id: int


def discover_units(cv: Canvas) -> list[Unit]:
    """Every Canvas course that carries a unit code, newest term first."""
    preferred = preferred_unit_names()
    units = []
    for course in cv.paginate("courses", {"include[]": ["term"]}):
        if "name" not in course:  # access-restricted course stubs
            continue
        codes = unit_codes(course.get("course_code"), course.get("name"))
        if not codes:
            continue
        term = (course.get("term") or {}).get("name") or ""
        key = next((code for code in codes if code in preferred), codes[0])
        units.append(Unit(
            key=key,
            aliases=codes,
            name=course["name"],
            term=term,
            term_key=term_key(term) or term_key(f"{course.get('course_code')} {course['name']}"),
            canvas_id=course["id"],
        ))
    return sorted(units, key=lambda u: (u.term_key or (0, 0), u.key), reverse=True)


def select_units(units: list[Unit], wanted: list[str] | None) -> list[Unit]:
    """Requested unit codes (latest offering of each), or every unit in the latest term."""
    if not wanted:
        latest = next((u.term_key for u in units if u.term_key), None)
        return [u for u in units if latest and u.term_key == latest]
    picked: dict[int, Unit] = {}
    for code in (w.upper() for w in wanted):
        match = next((u for u in units if code in u.aliases), None)  # units are newest first
        if match:
            picked[match.canvas_id] = match
    return list(picked.values())


# ---- course sync -------------------------------------------------------------

@dataclass(frozen=True)
class Ctx:
    module: str = ""
    subheader: str = ""
    module_pos: int = 999
    item_pos: int = 0
    week: int | None = None
    parent_kind: str | None = None


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


class CanvasSync:
    def __init__(self, cv: Canvas, store: Store, log: Callable[[str], None] = print) -> None:
        self.cv, self.store, self.log = cv, store, log

    def run(self, unit: Unit) -> Counter:
        self.unit, self.cid = unit.key, unit.canvas_id
        self.seen: set[str] = set()
        self.stats: Counter = Counter()
        self._modules()
        self._assignments()
        self._announcements()
        self._syllabus()
        return self.stats

    def _safe(self, label: str, action: Callable[[], object]):
        try:
            return action()
        except CanvasError as error:
            self.stats["unavailable"] += 1
            if error.status not in (401, 403, 404):
                self.log(f"    ! {label}: {error}")
        except Exception as error:  # a corrupt file must not stop the sync
            self.stats["errors"] += 1
            self.log(f"    ! {label}: {type(error).__name__}: {error}")
        return None

    def _meta(self, doc_id: str, title: str, ctx: Ctx, *, kind: str, url: str | None, fingerprint: str,
              updated_at: str | None = None, raw_path: str | None = None, parent_id: str | None = None,
              extra_texts: tuple[str, ...] = (), tags: set[str] | None = None) -> dict:
        week = parse_week(title, *extra_texts, ctx.subheader) or ctx.week or parse_week(ctx.module)
        tags = set(tags or ())
        if is_solution(title) or any(is_solution(t) for t in extra_texts):
            tags.add("solutions")
        return dict(
            doc_id=doc_id, unit=self.unit, source="canvas", kind=kind, week=week, title=title,
            module=ctx.module, module_pos=ctx.module_pos, item_pos=ctx.item_pos, tags=",".join(sorted(tags)),
            url=url, raw_path=raw_path, fingerprint=fingerprint, updated_at=updated_at, parent_id=parent_id,
        )

    def _save(self, meta: dict, body: str) -> None:
        if self.store.is_fresh(meta["doc_id"], meta["fingerprint"]):
            self.store.touch(meta)
            self.stats["unchanged"] += 1
        else:
            self.store.put(meta, body)
            self.stats["updated"] += 1

    def _context_kind(self, texts: tuple[str, ...], ctx: Ctx, default: str) -> str:
        return classify((*texts, ctx.subheader, ctx.module), ctx.parent_kind or default)

    # -- modules --
    def _modules(self) -> None:
        modules = self._safe("modules", lambda: self.cv.paginate(f"courses/{self.cid}/modules", {"include[]": ["items"]}))
        for module_pos, module in enumerate(modules or []):
            items = module.get("items")
            if items is None:  # Canvas omits items for very large modules
                items = self._safe("module items", lambda: self.cv.paginate(
                    f"courses/{self.cid}/modules/{module['id']}/items")) or []
            subheader = ""
            for item_pos, item in enumerate(items):
                if item.get("type") == "SubHeader":
                    subheader = item.get("title") or ""
                    continue
                ctx = Ctx(module=module.get("name") or "", subheader=subheader, module_pos=module_pos, item_pos=item_pos)
                self._safe(item.get("title") or "item", lambda: self._item(item, ctx))

    def _item(self, item: dict, ctx: Ctx) -> None:
        kind = item.get("type")
        if kind == "Page":
            self._page(item["url"], ctx)
        elif kind == "File":
            self._file(item["content_id"], ctx, title=item.get("title"), keep_unsupported=True)
        elif kind == "Assignment":
            self._assignment(self.cv.get(f"courses/{self.cid}/assignments/{item['content_id']}"), ctx)
        elif kind == "Quiz":
            self._quiz(item["content_id"], ctx)
        elif kind == "Discussion":
            self._discussion(item["content_id"], ctx)
        elif kind in ("ExternalUrl", "ExternalTool"):
            self._link(item, ctx)

    def _page(self, api_url: str, ctx: Ctx) -> None:
        page = self.cv.get(api_url)
        title = page.get("title") or "Untitled page"
        html = page.get("body") or ""
        doc_id = f"canvas:page:{self.cid}:{page.get('page_id') or page.get('url')}"
        kind = self._context_kind((title,), ctx, "page")
        meta = self._meta(doc_id, title, ctx, kind=kind, url=page.get("html_url"),
                          fingerprint=_hash(page.get("updated_at"), len(html)), updated_at=page.get("updated_at"))
        self._save(meta, extract.html_to_markdown(html))
        self._linked_files(html, replace(ctx, week=meta["week"], parent_kind=kind), doc_id)

    def _linked_files(self, html: str, ctx: Ctx, parent_id: str) -> None:
        for file_id in dict.fromkeys(re.findall(r"/files/(\d+)", html or "")):
            self._safe(f"linked file {file_id}", lambda: self._file(int(file_id), ctx, parent_id=parent_id))

    def _file(self, file_id: int, ctx: Ctx, title: str | None = None, parent_id: str | None = None,
              keep_unsupported: bool = False) -> None:
        doc_id = f"canvas:file:{file_id}"
        if doc_id in self.seen:
            return
        self.seen.add(doc_id)
        try:
            info = self.cv.get(f"courses/{self.cid}/files/{file_id}")
        except CanvasError:
            info = self.cv.get(f"files/{file_id}")
        name = info.get("display_name") or info.get("filename") or f"file {file_id}"
        ext = Path(info.get("filename") or name).suffix.lower()
        if not extract.supported(ext) and not keep_unsupported:
            return  # inline images and the like
        title = title or name
        kind = self._context_kind((title, name), ctx, "other")
        raw_rel = f"raw/{self.unit}/{file_id}_{slug(Path(name).stem)}{ext}"
        meta = self._meta(
            doc_id, title, ctx, kind=kind, url=f"{CANVAS_BASE}/courses/{self.cid}/files/{file_id}",
            fingerprint=_hash(info.get("updated_at"), info.get("size")), updated_at=info.get("updated_at"),
            raw_path=raw_rel, parent_id=parent_id, extra_texts=(name,),
        )
        if self.store.is_fresh(doc_id, meta["fingerprint"]):
            self.store.touch(meta)
            self.stats["unchanged"] += 1
            return
        size = info.get("size") or 0
        if not extract.supported(ext) or size > MAX_DOWNLOAD_BYTES or not info.get("url") or info.get("locked_for_user"):
            meta["raw_path"] = None
            reason = "locked" if info.get("locked_for_user") else f"{ext or 'unknown'} file, {size // 1024} KB"
            self.store.put(meta, f"_Not extracted ({reason}). Open in Canvas: {meta['url']}_")
            self.stats["metadata_only"] += 1
            return
        self.cv.download(info["url"], ROOT / raw_rel)
        body = extract.to_markdown(ROOT / raw_rel)
        pages = body.count("\n## Page ") + body.count("\n## Slide ") + 1
        if body.count(extract.NO_TEXT) * 2 > pages:
            meta["tags"] = ",".join(filter(None, [meta["tags"], "needs-vision"]))
        self.store.put(meta, body)
        self.stats["files"] += 1
        self.stats["updated"] += 1

    def _assignment(self, assignment: dict, ctx: Ctx) -> None:
        doc_id = f"canvas:assignment:{assignment['id']}"
        if doc_id in self.seen:
            return
        self.seen.add(doc_id)
        html = assignment.get("description") or ""
        header = "\n".join([
            f"Due: {assignment.get('due_at') or 'n/a'}",
            f"Points: {assignment.get('points_possible')}",
            f"Submission: {', '.join(assignment.get('submission_types') or [])}",
        ])
        meta = self._meta(doc_id, assignment.get("name") or "Assignment", ctx, kind="assignment",
                          url=assignment.get("html_url"), fingerprint=_hash(assignment.get("updated_at"), len(html)),
                          updated_at=assignment.get("updated_at"))
        self._save(meta, f"{header}\n\n{extract.html_to_markdown(html)}")
        self._linked_files(html, replace(ctx, week=meta["week"], parent_kind="assignment"), doc_id)

    def _quiz(self, quiz_id: int, ctx: Ctx) -> None:
        quiz = self.cv.get(f"courses/{self.cid}/quizzes/{quiz_id}")
        html = quiz.get("description") or ""
        header = f"Due: {quiz.get('due_at') or 'n/a'} | Questions: {quiz.get('question_count')} | Time limit: {quiz.get('time_limit')} min"
        meta = self._meta(f"canvas:quiz:{quiz_id}", quiz.get("title") or "Quiz", ctx, kind="quiz",
                          url=quiz.get("html_url"), fingerprint=_hash(header, len(html)))
        self._save(meta, f"{header}\n\n{extract.html_to_markdown(html)}")
        self._linked_files(html, replace(ctx, week=meta["week"], parent_kind="quiz"), meta["doc_id"])

    def _discussion(self, topic_id: int, ctx: Ctx) -> None:
        topic = self.cv.get(f"courses/{self.cid}/discussion_topics/{topic_id}")
        html = topic.get("message") or ""
        meta = self._meta(f"canvas:discussion:{topic_id}", topic.get("title") or "Discussion", ctx, kind="discussion",
                          url=topic.get("html_url"), fingerprint=_hash(len(html), topic.get("last_reply_at")))
        self._save(meta, extract.html_to_markdown(html))
        self._linked_files(html, replace(ctx, week=meta["week"]), meta["doc_id"])

    def _link(self, item: dict, ctx: Ctx) -> None:
        target = item.get("external_url") or item.get("html_url") or ""
        title = item.get("title") or "Link"
        meta = self._meta(f"canvas:link:{item['id']}", title, ctx, kind="link", url=target, fingerprint=_hash(title, target))
        self._save(meta, f"External link ({item.get('type')}): {target}")

    # -- course-wide --
    def _assignments(self) -> None:
        ctx = Ctx(module="Assignments", module_pos=900)
        for assignment in self._safe("assignments", lambda: self.cv.paginate(f"courses/{self.cid}/assignments")) or []:
            self._safe(assignment.get("name") or "assignment", lambda: self._assignment(assignment, ctx))

    def _announcements(self) -> None:
        ctx = Ctx(module="Announcements", module_pos=950)
        params = {"context_codes[]": [f"course_{self.cid}"], "start_date": "2015-01-01", "end_date": "2100-01-01"}
        for post in self._safe("announcements", lambda: self.cv.paginate("announcements", params)) or []:
            html = post.get("message") or ""
            meta = self._meta(f"canvas:announcement:{post['id']}", post.get("title") or "Announcement", ctx,
                              kind="announcement", url=post.get("html_url"),
                              fingerprint=_hash(post.get("title"), html), updated_at=post.get("posted_at"))
            self._save(meta, f"Posted: {post.get('posted_at')}\n\n{extract.html_to_markdown(html)}")
            self._linked_files(html, replace(ctx, week=meta["week"], parent_kind="announcement"), meta["doc_id"])

    def _syllabus(self) -> None:
        course = self._safe("syllabus", lambda: self.cv.get(f"courses/{self.cid}", {"include[]": ["syllabus_body"]}))
        html = (course or {}).get("syllabus_body") or ""
        if not html.strip():
            return
        meta = self._meta(f"canvas:syllabus:{self.cid}", "Syllabus", Ctx(module="Syllabus", module_pos=0), kind="syllabus",
                          url=f"{CANVAS_BASE}/courses/{self.cid}/assignments/syllabus", fingerprint=_hash(html))
        self._save(meta, extract.html_to_markdown(html))
        self._linked_files(html, Ctx(module="Syllabus", module_pos=0, parent_kind="syllabus"), meta["doc_id"])
