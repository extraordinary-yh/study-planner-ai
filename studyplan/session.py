"""Focused learner pages and durable coaching checkpoints, separate from completion evidence."""
from __future__ import annotations

import datetime as dt
import fcntl
import html
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit


def valid_date(date: str) -> str:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise ValueError('Choose a study date in YYYY-MM-DD format.')
    dt.date.fromisoformat(date)
    return date


def load_lessons(root: Path, date: str) -> dict:
    path = root / 'sessions' / (valid_date(date) + '.json')
    return json.loads(path.read_text()) if path.exists() else {}


def state_path(root: Path, date: str) -> Path:
    return root / 'data' / 'active-study' / (valid_date(date) + '.json')


def load_state(root: Path, date: str) -> dict:
    path = state_path(root, date)
    return json.loads(path.read_text()) if path.exists() else {'date': date, 'steps': {}, 'events': []}


@contextmanager
def session_lock(root: Path, date: str):
    path = state_path(root, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A separate lock inode survives atomic replacement of the state document.
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def session_url(date: str, block: str = '', item: str = '', *, live: bool = True) -> str:
    if not live:
        return '../sessions/' + quote(date, safe='') + '/' + quote(item, safe='') + '.html'
    return '/study?' + urlencode({k: v for k, v in {'date': date, 'block': block, 'item': item}.items() if v})


def _skipped(item: dict) -> bool:
    decisions = item.get('decisions') or []
    return bool(decisions and decisions[-1]['state'] == 'not_applicable')


def _pairs(plan: dict, lessons: dict) -> list:
    return [(b, i) for b in plan['blocks'] for i in b['items'] if i['id'] in lessons.get('steps', {})]


def _pending(b: dict, i: dict) -> bool:
    return not b['done'] and not i['done'] and not _skipped(i)


def timebox_seconds(lesson: dict, block: dict, item: dict) -> int:
    """Use the step's planned time; this is a pacing aid, not study evidence."""
    minutes = lesson.get('minutes')
    if minutes is None:
        match = re.search(r'\b(\d+)\s*min(?:ute)?s?\b', item['text'])
        minutes = int(match[1]) if match else max(1, round(block['minutes'] / len(block['items'])))
    return max(1, int(minutes)) * 60


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stop_timer(step: dict, now: dt.datetime) -> None:
    timer = step.get('timer', {})
    if timer.get('running_since'):
        elapsed = (now - dt.datetime.fromisoformat(timer['running_since'])).total_seconds()
        timer['elapsed_seconds'] = timer.get('elapsed_seconds', 0) + max(0, elapsed)
        timer['running_since'] = None


def prompt_presentation(prompt: str) -> dict:
    """Separate a scenario from its closing question without changing saved coach text."""
    question_starters = r'(?:What|How|Why|Which|Who|Where|When|Can|Could|Would|Should|Do|Does|Is|Are)\b'
    parts = {'heading': '', 'paragraphs': [], 'question': prompt}
    if re.match(question_starters, prompt) or not prompt.rstrip().endswith('?'):
        return parts
    boundaries = list(re.finditer(r'(?<=[.!?。！？”"])\s+(?=' + question_starters + ')', prompt))
    if not boundaries:
        return parts
    boundary = boundaries[-1]
    context, question = prompt[:boundary.start()].strip(), prompt[boundary.end():].strip()
    if len(context) < 80:
        return parts
    practice = context.lower().startswith('practice ')
    context = re.sub(r'^Practice scenario:\s*', '', context, flags=re.IGNORECASE)
    return {'heading': 'Practice scenario' if practice else 'Context',
            'paragraphs': [p.strip() for p in re.split(r'\n\s*\n', context) if p.strip()],
            'question': question}


def session_payload(root: Path, date: str, block: str = '', item: str = '', *, follow: bool = False,
                    live: bool = True) -> dict:
    from .board import BoardError, plan_payload
    from .resources import canonical_ref
    plan = plan_payload(root, valid_date(date))
    lessons = load_lessons(root, date)
    pairs = _pairs(plan, lessons)
    state = load_state(root, date) if live else {'steps': {}}
    if not pairs:
        raise BoardError('This day has no focused study steps yet. Open the calendar overview.')
    if follow or not (block or item):
        active = state.get('active', {})
        chosen = next(((b, i) for b, i in pairs if i['id'] == active.get('item') and _pending(b, i)), None)
        chosen = chosen or next(((b, i) for b, i in pairs if _pending(b, i)), None)
    else:
        if block and not any(b['id'] == block for b in plan['blocks']):
            raise BoardError('That study block is not part of this day.')
        chosen = next(((b, i) for b, i in pairs if (not block or b['id'] == block) and
                       (not item or i['id'] == item) and (item or _pending(b, i))), None)
        if not chosen and not item:
            chosen = next(((b, i) for b, i in pairs if _pending(b, i)), None)
    calendar = '/?date=' + date if live else '../../day/' + date + '.html'
    if not chosen:
        if item:
            raise BoardError('That study step is not part of this day.')
        return {'date': date, 'finished': True, 'calendar': calendar, 'live': live}
    b, i = chosen
    lesson = lessons['steps'][i['id']]
    progress = state['steps'].get(i['id'], {})
    status = 'completed' if i['done'] or b['done'] else 'skipped' if _skipped(i) else progress.get('status', 'ready')
    if not i['done'] and not b['done'] and not _skipped(i) and status in {'completed', 'skipped'}:
        status = 'paused'  # The calendar or another coach may have restored this item.
    timer_step = {'timer': dict(progress.get('timer', {}))}
    if status != 'in_progress':
        stop_timer(timer_step, _now())
    timer = {'budget_seconds': timebox_seconds(lesson, b, i), 'elapsed_seconds': 0,
             'running_since': None, **timer_step['timer']}
    following = next(((nb, ni) for nb, ni in pairs if ni['id'] != i['id'] and _pending(nb, ni)), None)
    def link(nb, ni):
        return session_url(date, nb['id'], ni['id']) if live else quote(ni['id'], safe='') + '.html'
    resources = []
    for resource in lesson.get('resources', []):
        ref = canonical_ref(resource['url'])
        if not ref:
            continue
        path, _, anchor = ref.partition('#')
        href = ref if urlsplit(ref).scheme else ('/files/' + quote(path, safe='/') if live else
                 '../../files/' + quote(path, safe='/') + '.html') + ('#' + quote(anchor) if anchor else '')
        resources.append({'label': resource['label'], 'url': href})
    payload = {'date': date, 'live': live, 'finished': False, 'calendar': calendar + '#' + b['id'],
               'url': link(b, i), 'block': b['id'], 'block_title': b['title'], 'item': i['id'],
               'title': lesson['title'], 'goal': lesson['goal'], 'mode': lesson.get('mode', 'Coaching'),
               'prompt': progress.get('question') or lesson['prompt'], 'notes': lesson.get('notes', []),
               'resources': resources, 'status': status, 'checkpoint': progress.get('checkpoint', ''),
               'timer': timer,
               'active_step': state.get('active', {}).get('item') == i['id'],
               'assistance': progress.get('assistance', 'unrecorded'),
               'position': b['items'].index(i) + 1, 'count': len(b['items']),
               'next': ({'title': lessons['steps'][following[1]['id']]['title'],
                         'url': link(*following)} if following else None)}
    payload['handoff'] = coaching_prompt(date, b['id'], i['id'])
    payload['presentation'] = prompt_presentation(payload['prompt'])
    return payload


def coaching_prompt(date: str, block: str, item: str) -> str:
    # Only a locator for an optional chat switch. The runbook owns the coaching contract.
    return (f'Resume my {date} study session in Study Planner AI, block {block}, step {item}. '
            'Follow SKILL.md and ask the current saved question.')


def api_session(root: Path, body: dict) -> dict:
    from .board import BoardError, api_done, api_undo, api_item_decision
    date = valid_date(str(body.get('date', '')))
    action = body.get('action')
    if action not in {'start', 'pause', 'reset_timer', 'question', 'checkpoint', 'complete', 'undo', 'skip'}:
        raise BoardError('Unknown study-session action.')
    text = str(body.get('text') or '').strip()
    if len(text) > 6000 or (action in {'question', 'checkpoint', 'skip'} and not text):
        raise BoardError('Enter a short question, checkpoint, or skip reason (up to 6000 characters).')
    assistance = body.get('assistance', 'unrecorded')
    if assistance not in {'unrecorded', 'guided', 'independent'}:
        raise BoardError('Choose guided, independent, or unrecorded assistance.')
    with session_lock(root, date):
        current = session_payload(root, date, str(body.get('block') or ''), str(body.get('item') or ''))
        if current['finished']:
            raise BoardError('No unfinished study step remains.')
        state = load_state(root, date)
        step = state['steps'].setdefault(current['item'], {})
        args = {'date': date, 'block': current['block'], 'item': current['item']}
        instant = _now()
        now = instant.isoformat(timespec='milliseconds')
        resolved = current['status'] in {'completed', 'skipped'}
        if action in {'start', 'question', 'pause', 'reset_timer'} and resolved:
            raise BoardError('This step is resolved. Restore it or open the next step.')
        if action == 'start':
            step.setdefault('started_at', now)
            if body.get('restart_timer') is True:
                step['timer'] = {'elapsed_seconds': 0, 'running_since': None}
            timer = step.setdefault('timer', {'elapsed_seconds': 0, 'running_since': None})
            if not timer.get('running_since'):
                timer['running_since'] = now
            step['status'] = 'in_progress'
        elif action == 'pause':
            step['status'] = 'paused'
        elif action == 'reset_timer':
            step['timer'] = {'elapsed_seconds': 0, 'running_since': None}
            step['status'] = 'ready'
        elif action == 'question':
            step['question'] = text
            step['status'] = current['status']  # Asking never starts the user's countdown.
        elif action == 'complete':
            if current['status'] == 'skipped':
                raise BoardError('Restore the skipped step in the calendar before completing it.')
            if current['status'] != 'completed':
                api_done(root, args)
            step['status'] = 'completed'
        elif action == 'undo':
            if current['status'] == 'skipped':
                api_item_decision(root, {**args, 'state': 'applicable', 'reason': text or 'Restored from study session'})
            elif current['status'] == 'completed':
                # A whole-block tick must be undone in the overview explicitly.
                from .board import plan_payload
                if next(b for b in plan_payload(root, date)['blocks'] if b['id'] == args['block'])['done']:
                    raise BoardError('Restore the completed block in the calendar first.')
                api_undo(root, args)
            step['status'] = 'paused'
        elif action == 'skip':
            if current['status'] == 'completed':
                raise BoardError('Undo completion before skipping this step.')
            if current['status'] != 'skipped':
                api_item_decision(root, {**args, 'state': 'not_applicable', 'reason': text[:1000]})
            step['status'] = 'skipped'
        if action in {'checkpoint', 'complete', 'pause'} and text:
            step['checkpoint'] = text
            step['assistance'] = assistance
        step['updated_at'] = now
        if action in {'pause', 'complete', 'skip', 'undo'}:
            stop_timer(step, instant)
        if action in {'start', 'question'}:
            for other_id, other in state['steps'].items():
                if other_id != current['item'] and other.get('timer', {}).get('running_since'):
                    stop_timer(other, instant)
                    if other.get('status') == 'in_progress':
                        other['status'] = 'paused'
        # Duration remains unknown; a page open or a start timestamp is not active study time.
        step.setdefault('minutes_actual', None)
        if action not in {'checkpoint', 'reset_timer'}:
            state['active'] = {'block': args['block'], 'item': args['item']}
        state['events'].append({'at': now, 'action': action, 'block': args['block'], 'item': args['item'],
                                'text': text, 'assistance': assistance})
        path = state_path(root, date)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        tmp.replace(path)
    return session_payload(root, date, follow=True) if action in {'complete', 'skip'} else session_payload(root, date, args['block'], args['item'])


def session_issues(plan, root: Path, parent: Path) -> list[str]:
    from .resources import resource_path, canonical_ref
    lessons = load_lessons(root, plan.date)
    if not lessons:
        return []
    issues = []
    known = {i.id for b in plan.blocks for i in b.items}
    for item, lesson in lessons.get('steps', {}).items():
        if item not in known or not re.fullmatch(r'[A-Za-z0-9_-]+', item):
            issues.append(f'Session step is not a valid plan item: {item}')
        for field in ('title', 'goal', 'prompt'):
            if not isinstance(lesson.get(field), str) or not lesson[field].strip():
                issues.append(f'{item}: focused session needs {field}')
        if 'minutes' in lesson and (type(lesson['minutes']) is not int or lesson['minutes'] <= 0):
            issues.append(f'{item}: suggested minutes must be a positive whole number')
        for resource in lesson.get('resources', []):
            ref = canonical_ref(resource.get('url', ''))
            if not ref or (not urlsplit(ref).scheme and resource_path(ref, root, parent) is None):
                issues.append(f'{item}: focused resource cannot be opened: {resource.get("url")}')
    if known - lessons.get('steps', {}).keys():
        issues.append('Give every item in a focused study day its own learner step.')
    coach = lessons.get('coach_ref')
    if coach and resource_path(coach, root, parent) is None:
        issues.append(f'Coaching context is missing: {coach}')
    return issues


def render_session(payload: dict) -> str:
    from .board import glass_css
    template = (Path(__file__).parent / 'static' / 'session.html').read_text()
    # Render the question before JavaScript runs, so a stale client cannot blank the lesson.
    seconds = payload.get('timer', {}).get('budget_seconds', 0)
    presentation = prompt_presentation(payload.get('prompt', 'All planned steps are resolved.'))
    values = {'GLASS_THEME': glass_css(), 'SESSION': json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c'),
              'DATE': html.escape(payload['date']), 'CALENDAR': html.escape(payload['calendar'], quote=True),
              'TITLE': html.escape(payload.get('title', 'Day checkpoint')),
              'QUESTION': html.escape(presentation['question']),
              'CONTEXT_HIDDEN': '' if presentation['paragraphs'] else 'hidden',
              'CONTEXT_TITLE': html.escape(presentation['heading']),
              'CONTEXT_PARAGRAPHS': ''.join('<p>' + html.escape(p) + '</p>' for p in presentation['paragraphs']),
              'STEP': f"Step {payload.get('position', 0)} of {payload.get('count', 0)}",
              'TIMER': f'{seconds // 60:02d}:{seconds % 60:02d}'}
    return re.sub(r'__([A-Z_]+)__', lambda match: values.get(match[1], match[0]), template)
