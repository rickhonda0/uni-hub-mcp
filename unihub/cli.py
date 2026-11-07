"""Command line: sync Canvas and Ed into the corpus, ingest local files, search."""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from . import extract
from .classify import parse_week
from .config import ROOT
from .store import Store


def cmd_courses(_args) -> None:
    from .canvas import Canvas, discover_units

    for unit in discover_units(Canvas()):
        print(f"{unit.key:10} {unit.term:22} {', '.join(unit.aliases):30} {unit.name}")


def cmd_sync(args) -> None:
    from .canvas import Canvas, CanvasSync, discover_units, select_units

    store = Store()
    cv = Canvas()
    units = select_units(discover_units(cv), args.unit)
    if not units:
        sys.exit("No matching units. Run `uni-hub courses` to see what Canvas offers.")
    for unit in units:
        store.put_unit(unit.key, unit.aliases, unit.name, unit.term, unit.canvas_id)

    if not args.skip_canvas:
        sync = CanvasSync(cv, store)
        for unit in units:
            started = time.time()
            print(f"Canvas {unit.key} ({unit.term})")
            stats = sync.run(unit)
            store.mark_synced(unit.key, "canvas")
            print(f"  {dict(stats)} in {time.time() - started:.0f}s")

    if not args.skip_ed:
        from .ed import Ed, EdSync, match_course

        ed = Ed()
        ed_courses = ed.courses()
        sync = EdSync(ed, store)
        for unit in units:
            course = match_course(ed_courses, unit.aliases, unit.term_key)
            if not course:
                print(f"Ed {unit.key}: no matching Ed course")
                continue
            started = time.time()
            print(f"Ed {unit.key} ({course.get('code')})")
            stats = sync.run(unit.key, course)
            store.mark_synced(unit.key, "ed", ed_id=course["id"])
            print(f"  {dict(stats)} in {time.time() - started:.0f}s")


def cmd_ingest(args) -> None:
    """Add local files (lecture transcripts, your own notes) to a unit's corpus."""
    store = Store()
    unit = store.resolve_unit(args.unit)
    if not unit:
        sys.exit(f"Unknown unit {args.unit}; sync it first.")
    root = Path(args.path).resolve()
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    added = 0
    for path in files:
        if not extract.supported(path.suffix):
            continue
        stat = path.stat()
        doc_id = f"local:{hashlib.sha1(str(path).lower().encode()).hexdigest()[:16]}"
        fingerprint = f"{stat.st_mtime_ns}|{stat.st_size}"
        if store.is_fresh(doc_id, fingerprint):
            continue
        try:
            raw = path.relative_to(ROOT).as_posix()
        except ValueError:
            raw = str(path)
        store.put(dict(
            doc_id=doc_id, unit=unit["unit"], source="local", kind=args.kind, week=parse_week(path.stem, path.parent.name),
            title=path.stem, module=str(path.parent.name), module_pos=800, item_pos=0, tags="", url=path.as_uri(),
            raw_path=raw, parent_id=None, fingerprint=fingerprint, updated_at=None,
        ), extract.to_markdown(path))
        added += 1
    print(f"Ingested {added} file(s) into {unit['unit']} as kind={args.kind}")


def cmd_status(_args) -> None:
    store = Store()
    for unit in store.units():
        counts = ", ".join(f"{row['kind']} {row['n']}" for row in store.counts(unit["unit"]))
        print(f"{unit['unit']:10} {unit['term']:20} canvas={unit['canvas_synced'] or '-'} ed={unit['ed_synced'] or '-'}")
        print(f"           {counts or 'no documents'}")


def cmd_search(args) -> None:
    store = Store()
    units = None
    if args.unit:
        row = store.resolve_unit(args.unit)
        units = [row["unit"]] if row else [args.unit.upper()]
    for row in store.search(args.query, units=units, limit=args.limit):
        week = f"w{row['week']}" if row["week"] is not None else "w?"
        print(f"[{row['unit']} {week} {row['kind']}] {row['title']} > {row['heading']}  ({row['doc_id']})")
        print("   " + row["snip"].replace("\n", " ")[:300])


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="uni-hub", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("courses", help="list Canvas courses that carry a unit code").set_defaults(func=cmd_courses)

    sync = sub.add_parser("sync", help="sync Canvas content and Ed threads (default: latest semester)")
    sync.add_argument("--unit", action="append", help="unit code; repeatable (e.g. --unit MATH1902 for a past unit)")
    sync.add_argument("--skip-canvas", action="store_true")
    sync.add_argument("--skip-ed", action="store_true")
    sync.set_defaults(func=cmd_sync)

    ingest = sub.add_parser("ingest", help="add local files (transcripts, notes) to a unit")
    ingest.add_argument("path")
    ingest.add_argument("--unit", required=True)
    ingest.add_argument("--kind", default="notes", help="e.g. transcript, notes, tutorial")
    ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("status", help="show synced units and document counts").set_defaults(func=cmd_status)

    search = sub.add_parser("search", help="full-text search across the corpus")
    search.add_argument("query")
    search.add_argument("--unit")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
