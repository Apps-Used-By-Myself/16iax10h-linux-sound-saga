#!/usr/bin/env python3
"""Extract every historical revision of the 16iax10h audio patch from git.

The repository only keeps the patch for the current kernel version and
discards older patch files when a new version is released.  Everything that
was ever committed is still reachable through git history, so this script
walks the full history of the current branch and dumps each distinct content
revision of every patch file that ever existed under any path.

For each kernel label (e.g. ``16iax10h-audio-linux-6.18.patch``) the script
writes:

* ``<label>``                 - the final content of that file, i.e. the
  version that was live right before it was discarded or renamed away (or the
  current version, if the file is still present in the tree), plus
* ``<stem>.rev<N>.patch``     - each earlier content revision of the same
  label (``N`` = 1 for the oldest revision).

An ``INDEX.md`` manifest lists every exported revision together with the git
commit that introduced it.

Run it from anywhere inside the repository.  It only reads from git and never
touches the working tree other than writing to ``--out``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

DEFAULT_PATTERN = r"^16iax10h-audio-linux-.*\.patch$"
SEP = "\x1f"


def git(root: str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", root, *args], check=True, capture_output=True
    ).stdout


def git_text(root: str, *args: str) -> str:
    return git(root, *args).decode("utf-8", "replace")


def snapshot(root: str, commit: str, pattern: re.Pattern, exclude_prefix: str = "") -> Dict[str, str]:
    present: Dict[str, str] = {}
    out = git(root, "ls-tree", "-r", "-z", commit)
    for entry in out.decode("utf-8", "replace").split("\0"):
        if not entry:
            continue
        head, _, path = entry.rpartition("\t")
        if exclude_prefix and path.startswith(exclude_prefix):
            continue
        if pattern.match(os.path.basename(path)):
            present[os.path.basename(path)] = head.split(" ")[2]
    return present


def natural_key(label: str) -> list:
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", label)
    ]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export every revision of every 16iax10h audio patch that ever "
            "existed in this repository's git history."
        )
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="git revision whose history should be scanned (default: HEAD)",
    )
    parser.add_argument(
        "--out",
        default="patch-archive",
        help="output directory (default: <repo root>/patch-archive)",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help=(
            "regular expression matched against the basename of every file "
            "in every scanned commit (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the export plan without writing any files",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the per-label summary"
    )
    args = parser.parse_args(argv)

    pattern = re.compile(args.pattern)
    root = git_text(".", "rev-parse", "--show-toplevel").strip()

    commits = git(root, "rev-list", "--topo-order", "--reverse", args.ref)
    commits = commits.decode().split()
    if not commits:
        parser.error(f"no commits reachable from {args.ref!r}")

    meta: Dict[str, Tuple[str, str, str]] = {}
    log = git(root, "log", "--topo-order", "--reverse",
              f"--format=%H{SEP}%h{SEP}%ad{SEP}%s", "--date=short", args.ref)
    for line in log.decode("utf-8", "replace").splitlines():
        full, short, date, subject = line.split(SEP, 3)
        meta[full] = (short, date, subject)

    segments: Dict[str, List[Tuple[str, int, int]]] = defaultdict(list)
    head_present: Dict[str, str] = {}
    active: Dict[str, Tuple[str, int]] = {}

    out_dir = args.out
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(root, out_dir)
    exclude_prefix = os.path.relpath(out_dir, root) + "/"

    for idx, commit in enumerate(commits):
        present = snapshot(root, commit, pattern, exclude_prefix)
        for label in list(active):
            blob, start = active[label]
            if label not in present or present[label] != blob:
                segments[label].append((blob, start, idx - 1))
                del active[label]
        for label, blob in present.items():
            if label not in active:
                active[label] = (blob, idx)
        head_present = present

    last_idx = len(commits) - 1
    for label, (blob, start) in active.items():
        segments[label].append((blob, start, last_idx))

    revisions: Dict[str, List[Tuple[str, int, int]]] = {}
    for label, segs in segments.items():
        by_blob: Dict[str, List[int]] = defaultdict(list)
        for blob, start, end in segs:
            by_blob[blob].append((start, end))
        ordered: List[Tuple[str, int, int]] = []
        for blob, spans in by_blob.items():
            first = min(s for s, _ in spans)
            last = max(e for _, e in spans)
            ordered.append((blob, first, last))
        ordered.sort(key=lambda item: item[1])
        revisions[label] = ordered

    if not revisions:
        parser.error(
            f"no file matching {args.pattern!r} found anywhere in the history "
            f"of {args.ref!r}"
        )

    plan: List[Tuple[str, str, str, bytes, Tuple[str, str, str], bool]] = []
    for label in sorted(revisions, key=natural_key):
        ordered = revisions[label]
        stem = label[:-len(".patch")] if label.endswith(".patch") else label
        for pos, (blob, first, last) in enumerate(ordered):
            if pos == len(ordered) - 1:
                filename = label
                tag = "final"
            else:
                filename = f"{stem}.rev{pos + 1}.patch"
                tag = f"rev{pos + 1}"
            content = git(root, "cat-file", "blob", blob)
            plan.append((label, filename, tag, content, meta[commits[first]],
                         label in head_present))

    if not args.quiet or args.dry_run:
        print(f"repo root : {root}")
        print(f"ref       : {args.ref}  ({len(commits)} commits scanned)")
        print(f"labels    : {len(revisions)}  revision files: {len(plan)}")
        print(f"output    : {out_dir}")
        print()

    current = 0
    superseded = 0
    rows = []
    for label in sorted(revisions, key=natural_key):
        order = revisions[label]
        n_extra = len(order) - 1
        lines = []
        for pos, (blob, first, last) in enumerate(order):
            if pos == len(order) - 1:
                filename = label
                tag = "final"
            else:
                stem = label[:-len(".patch")] if label.endswith(".patch") else label
                filename = f"{stem}.rev{pos + 1}.patch"
                tag = f"rev{pos + 1}"
            short, date, subject = meta[commits[first]]
            if len(subject) > 56:
                subject = subject[:56] + "..."
            rows.append(
                f"| `{filename}` | `{label}` | {tag} | "
                f"`{blob[:10]}` | {short} | {date} | {subject} |"
            )
            lines.append(f"    {filename:52s} {short} ({date})  blob {blob[:10]}")
        if label in head_present:
            current += 1
            status = "current"
        else:
            superseded += 1
            status = "superseded"
        if not args.quiet:
            suffix = f" + {n_extra} older revision(s)" if n_extra else ""
            print(f"[{status:10s}] {label}{suffix}")
            if not args.dry_run:
                for line in lines:
                    print(line)

    if args.dry_run:
        print()
        print(f"[dry-run] would write {len(plan)} file(s) to {out_dir}")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    skipped = 0
    for _, filename, _, content, _, _ in plan:
        path = os.path.join(out_dir, filename)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                if fh.read() == content:
                    skipped += 1
                    continue
        with open(path, "wb") as fh:
            fh.write(content)

    manifest = os.path.join(out_dir, "INDEX.md")
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write(
            "# Patch archive extracted from git history\n\n"
            f"- repository: `{root}`\n"
            f"- scanned ref: `{args.ref}` ({len(commits)} commits)\n"
            f"- export date: generated by `extract_patches.py`\n"
            f"- kernel labels: {len(revisions)} ({current} still current, "
            f"{superseded} superseded/removed from the repo)\n"
            f"- revision files: {len(plan)}\n\n"
            "For every kernel label ever present, `<label>` holds the final "
            "content of that file (the version that was live right before it "
            "was discarded or renamed away, or the current version for files "
            "still in the tree). `<stem>.rev<N>.patch` files hold earlier "
            "content revisions of the same label (`rev1` is the oldest).\n\n"
            "| file | kernel label | revision | content blob | introduced by | "
            "date | commit subject |\n"
            "|---|---|---|---|---|---|---|\n"
        )
        fh.write("\n".join(rows))
        fh.write("\n")

    if not args.quiet:
        print()
        print(f"wrote {len(plan) - skipped} file(s) + INDEX.md to {out_dir}"
              f"  ({skipped} skipped, already up-to-date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
