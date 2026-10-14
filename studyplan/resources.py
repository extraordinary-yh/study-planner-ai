"""One path contract for plan actions, local document routes and exported hub files."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

TEXT_SUFFIXES = {'.md', '.txt', '.py', '.json', '.jsonl', '.yaml', '.yml', '.csv', '.tsv'}
BINARY_SUFFIXES = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | BINARY_SUFFIXES | {'.html', '.htm'}


def first_target(text: str) -> str:
    for pattern in (r'\]\(([^)]+)\)', r'https?://[^\s<)`]+',
                    r'(?:\.{1,2}/|study-plan/|profile/|docs/|reports/|exercises/)[^\s`<>]+'):
        match = re.search(pattern, text)
        if match:
            return (match.group(1) if match.lastindex else match.group()).rstrip('.,;')
    return ''


def canonical_ref(raw: str, *, action: bool = False) -> str:
    """Actions are relative to study-plan; refs are repo-root relative."""
    raw = unquote(raw.strip())
    parsed = urlsplit(raw)
    if parsed.scheme:
        return raw if parsed.scheme in ('http', 'https', 'mailto') else ''
    if raw.startswith('//'):
        return ''
    if raw.startswith('#'):
        return raw
    path = parsed.path
    if path.startswith('/files/'):
        path = path[len('/files/'):]
    elif path.startswith('/'):
        return ''
    elif path.startswith(('./', '../')):
        path = posixpath.join('study-plan', path)
    elif action and not path.startswith(('study-plan/', 'profile/context/', 'profile/stories/', 'reports/')):
        path = posixpath.join('study-plan', path)
    path = posixpath.normpath(path)
    if any(p.startswith('.') for p in path.split('/')) or path == '.':
        return ''
    return path + ('#' + parsed.fragment if parsed.fragment else '')


def resource_path(ref: str, root: Path, parent: Path) -> Path | None:
    ref = canonical_ref(ref)
    if not ref or urlsplit(ref).scheme or ref.startswith('#'):
        return None
    path = ref.partition('#')[0]
    base = root if path.startswith('study-plan/') else parent
    relative = path[len('study-plan/'):] if path.startswith('study-plan/') else path
    src = (base / relative).resolve()
    if not src.is_relative_to(base.resolve()) or src.suffix.lower() not in SUPPORTED_SUFFIXES:
        return None
    return src if src.is_file() else None


def plan_resources(plan) -> set[str]:
    refs = set()
    for block in plan.blocks:
        for raw in [first_target(block.first_action)] + [l['url'] for l in block.links]:
            if raw:
                refs.add(canonical_ref(raw, action=True))
        for item in block.items:
            refs.update(canonical_ref(r) for r in item.refs)
            raw = item.action_url or first_target(item.first_action)
            if raw:
                refs.add(canonical_ref(raw, action=True))
    return refs - {''}


def resource_issues(plan, root: Path, parent: Path) -> list[str]:
    issues = []
    for ref in sorted(plan_resources(plan)):
        if urlsplit(ref).scheme or ref.startswith('#'):
            continue
        src = resource_path(ref, root, parent)
        if src is None:
            issues.append(f'Resource is missing or cannot be opened in the board: {ref}')
            continue
        _, _, fragment = ref.partition('#')
        if fragment and src.suffix == '.md':
            from .hub import _anchor
            anchors = {_anchor(m) for m in re.findall(r'^#{1,6}\s+(.+)$', src.read_text(), re.M)}
            if fragment not in anchors:
                issues.append(f'Resource section does not exist: {ref}')
        elif fragment and src.suffix.lower() in {'.html', '.htm'}:
            anchors = re.findall(r'\bid=["\x27]([^"\x27]+)["\x27]', src.read_text())
            if fragment not in anchors:
                issues.append(f'Resource section does not exist: {ref}')
    for block in plan.blocks:
        for item in block.items:
            if item.action_url and not canonical_ref(item.action_url, action=True):
                issues.append(f'{item.id}: unsupported action destination')
    return issues
