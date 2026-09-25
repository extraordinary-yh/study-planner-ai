"""Turn durable coaching evidence into bounded, prerequisite-aware core blocks.

No attempt, completion or mastery is written here. Those require observed work.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import _yaml
from .leetcode import due_reviews, load_attempts, schedule, topic_stats


def load_coaching(root: Path) -> dict:
    return _yaml(root / 'profile' / 'coaching.yaml')


def pending_concepts(coaching: dict) -> list[dict]:
    design = coaching.get('design', {})
    evidence = design.get('evidence', {})
    pending = [c for c in design.get('concepts', [])
               if evidence.get(c['id'], {}).get('stage') != 'independent'
               or not evidence.get(c['id'], {}).get('evidence')]
    # The coach updates this order from role relevance and observed learning;
    # the planner never guesses mastery or interview probabilities.
    order = {key: i for i, key in enumerate(design.get('priority_order', []))}
    return sorted(pending, key=lambda c: order.get(c['id'], len(order)))


def coding_candidates(root: Path, today: str, prefs: dict, level: dict, coaching: dict) -> list[dict]:
    """Personal topic evidence, then due repairs/weakness. Respect clean review dates.

    Match priority by explicit problem membership, never the merged graphs topic.
    Historical unknown outcomes remain exposure, never failure or a clean solve.
    """
    attempts = [a for a in load_attempts(root / 'data' / 'leetcode.jsonl') if a['date'] <= today]
    lc = prefs.get('leetcode', {})
    intervals = lc.get('review_intervals_days', {})
    history = {r['slug']: r for r in schedule(attempts, intervals)}
    stats = topic_stats(attempts, today, int(lc.get('weakness_window_days', 90)),
                        int(lc.get('coverage_min_attempts', 3)), level.get('coding', {}).get('topics', {}))
    due = due_reviews(attempts, intervals, today, weakness={s['topic']: s['weakness'] for s in stats})
    selected, seen = [], set()
    priorities = [p for p in coaching.get('coding', {}).get('priorities', []) if not p.get('resolved_on')]
    # One candidate per priority first; transfer problems follow the other weak topic.
    for index in range(max((len(p.get('problems', [])) for p in priorities), default=0)):
        for priority in priorities:
            if index >= len(priority.get('problems', [])):
                continue
            problem = priority['problems'][index]
            old = history.get(problem['slug'])
            if old and old['due'] > today:
                continue
            if index:
                previous = history.get(priority['problems'][index - 1]['slug'])
                if not previous or previous['last_outcome'] != 'clean':
                    continue  # learn/repair the first problem before its transfer test
            selected.append({**problem, 'url': f"https://leetcode.com/problems/{problem['slug']}/",
                             'reason': priority['reason'], 'source': priority['source'],
                             'kind': 'review' if old else 'new', 'last_outcome': old['last_outcome'] if old else None})
            seen.add(problem['slug'])
    for row in due:
        if row['slug'] in seen:
            continue
        selected.append({**row, 'pattern': ', '.join(row['topics']) or 'pattern to identify',
                         'reason': f"Due {row['due']}; last outcome {row['last_outcome']} on {row['last_date']}",
                         'source': f"study-plan/data/leetcode.jsonl#{row['slug']}", 'kind': 'review'})
        seen.add(row['slug'])
    return selected


def apply_coaching(plan, root: Path, prefs: dict, level: dict, coaching: dict) -> None:
    from .plan import Block

    gaps = pending_concepts(coaching)
    design = coaching.get('design', {})
    sd = plan.block('design')
    if sd and gaps and sd.minutes >= 10:
        concept = gaps[0]
        state = design.get('evidence', {}).get(concept['id'], {})
        guided = state.get('stage') in ('learned', 'guided') and state.get('evidence')
        mode = 'Guided practice' if guided else 'Learn'
        sd.title = f"System design · {mode}: {concept['title']}"
        sd.minutes = min(sd.minutes, int(coaching.get("planning", {}).get("max_design_minutes", 60)))
        resource = concept.get('resource', design['resource'])
        anchor = re.sub(r'[^\w\s-]', '', concept['section'].lower()).replace(' ', '-')
        url = (resource if resource.startswith(('https://', 'http://')) else
               './' + resource.removeprefix('study-plan/') + '#' + anchor)
        sd.first_action = f"open [Required today: {concept['section']}]({url}) and read the first example."
        sd.description = [f"Purpose: {mode.lower()} {concept['title'].lower()} before interviewer probes. "
                          "This is a recorded knowledge gap; a completed framework lesson does not establish readiness.",
                          "Work with the example open. If unfamiliar terms block you, stop and record them; "
                          "use the final task as a supported explanation, not a timed interview."]
        if concept.get('reason'):
            sd.description.append('Why now: ' + concept['reason'])
        if concept.get('stop_condition'):
            sd.description.append('Stop condition: ' + concept['stop_condition'])
        sd.items = []
        practice = max(5, sd.minutes // 3 // 5 * 5)
        sd.add_item(f"{concept['task']} ({sd.minutes - practice} min).", refs=[resource])
        practice_task = concept.get('practice', 'Explain the example in your own words and record what still needs help')
        sd.add_item(f"{practice_task} ({practice} min). "
                    "Record independent understanding only if demonstrated without the example.",
                    refs=['study-plan/profile/coaching.yaml'])
        sd.links = [{'label': 'Required today', 'url': url}]
        sd.log_hint = f'uv run studyplan done design --minutes {sd.minutes} --note "<what was understood; what still needs help>"'
        plan.radar.append('Interviewer probes deferred for unlearned concepts; this is supported learning. '
                          'Teach AI cases alongside their prerequisites; release each cold probe only after its needed concepts '
                          'have observed independent explanations.')
    elif sd and gaps:
        plan.blocks.remove(sd)
        plan.radar.append('Design deferred: less than 10 minutes available; keep the next foundation lesson for another day.')

    lc = plan.block('leetcode')
    if lc:
        candidates = coding_candidates(root, plan.date, prefs, level, coaching)
        lc.items, lc.links = [], []
        lc.description = ['Purpose: prioritize observed interview gaps, then due repairs and weak topics. '
                          'Saved company notes prove exposure, not readiness or future question frequency.',
                          coaching.get('coding', {}).get('implementation', '')]
        left = min(lc.minutes, int(coaching.get("planning", {}).get("max_coding_minutes", 90)))
        spent = 0
        max_items = int(coaching.get('planning', {}).get('max_coding_problems', 2))
        for candidate in candidates:
            cost = int(prefs.get('leetcode', {}).get('minutes_per_new', {}).get(candidate['difficulty'], 30))
            # A partial slot is for reviewing the existing reasoning, not claiming a full timed solve.
            if cost > left or len(lc.items) >= max_items:
                break  # do not fill a gap with a lower-priority easy problem
            url = candidate['url']
            lc.add_item(f"{candidate['kind']}: [{candidate['title']}]({url}) — {candidate['pattern']} ({cost} min). "
                        f"Why now: {candidate['reason'].rstrip('.')}. Read the problem, solve in Python, then explain your invariant "
                        "and test edge cases. Open saved solutions only after the attempt.",
                        refs=[url, candidate['source']])
            lc.links.append({'label': candidate['title'], 'url': url})
            left -= cost
            spent += cost
        if left >= 5 and lc.items:
            left = min(left, 10)
            lc.add_item(f"Record the actual outcome, stopping point, wrong assumption and next repair ({left} min). "
                        "A skipped task is not a failed attempt.", refs=['study-plan/profile/notes.md'])
            spent += left
        elif not lc.items and candidates and left >= 5:
            c = candidates[0]
            lc.add_item(f"Read [{c['title']}]({c['url']}) and trace one example ({left} min); "
                        f"focus: {c['pattern']}. Why now: {c['reason']}. Save the full solve for a longer slot; "
                        "this is preparation, not an attempt.", refs=[c['url'], c['source']])
            lc.links = [{'label': c['title'], 'url': c['url']}]
            spent = left
        lc.minutes = spent
        if lc.links:
            lc.first_action = f"open {lc.links[0]['url']} and read the problem statement."
        else:
            plan.blocks.remove(lc)
        assigned = sum(1 for i in lc.items if i.text.startswith(('review:', 'new:')))
        if len(candidates) > assigned:
            plan.radar.append('Additional coding reviews remain outside today’s time box; '
                              'their review dates and attempt history are unchanged.')
    if gaps:
        # Generic story rotation can contain system-design mocks too.
        st = plan.block('stories')
        if st:
            st.items = [i for i in st.items if not i.text.startswith('Mock')]
    plan.blocks = [b for b in plan.blocks if b.minutes > 0]
    unused = plan.minutes - sum(b.minutes for b in plan.blocks)
    if unused > 0:
        plan.radar.append(f'{unused} min of available time left unassigned; finish the focused work before adding lower-priority tasks.')
    block_order = coaching.get('planning', {}).get('block_order', [])
    if block_order:
        positions = {key: i for i, key in enumerate(block_order)}
        plan.blocks.sort(key=lambda b: positions.get('debrief' if b.id.startswith('debrief-') else b.id,
                                                     len(positions)))
    first = next((b for b in plan.blocks if b.id in ('design', 'leetcode')), None)
    if first:
        plan.call = f"Start with {first.title}: " + ('learn the missing concepts before probes.' if first.id == 'design'
                                                    else 'repair the evidenced interview gap within today’s time box.')
