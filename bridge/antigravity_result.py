"""Recognize completed current turns beneath AGY's stale error envelope.

An ERROR envelope alone is never enough to recover an answer. Require the
current user-input boundary, completed steps and an exact final-response match.
"""

INTERRUPTED = 'The stream was interrupted. Please continue the task you were working on.'
NOT_FOUND = 'NOT_FOUND (code 404): Requested entity was not found.'


def completed_despite_stale_error(events, result):
    if result.get('status') != 'ERROR' or result.get('error') not in (INTERRUPTED, NOT_FOUND):
        return False
    if result.get('finished_reason') not in (None, 'error'):
        return False
    native = result.get('conversation_id')
    response = result.get('response')
    if not native or not isinstance(response, str) or not response.strip():
        return False
    start = None
    steps = {}
    invalid = False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get('event') == 'error':
            invalid = True
        if event.get('event') != 'step_update':
            continue
        step = event.get('step_update')
        if not isinstance(step, dict) or type(step.get('step_index')) != int:
            return False
        index = step['step_index']
        if step.get('step_type') == 'user_input' and index != start:
            if start is not None and index < start:
                return False
            start = index
            steps = {}
            invalid = False
        if start is None or index < start:
            continue
        if step.get('conversation_id') != native:
            return False
        item = steps.setdefault(index, {'text': ''})
        if 'step_type' in step:
            item['type'] = step['step_type']
        if 'state' in step:
            item['state'] = step['state']
        if step.get('step_type') == 'error_message' or step.get('state') in ('ERROR', 'CANCELLED', 'CANCELED') or step.get('error'):
            invalid = True
        if isinstance(step.get('tool_info'), dict) and step['tool_info'].get('error'):
            invalid = True
        delta = step.get('text_delta', '')
        if not isinstance(delta, str):
            return False
        item['text'] += delta
    if invalid or not steps or any(s.get('state') != 'DONE' for s in steps.values()):
        return False
    last = steps[max(steps)]
    return (max(steps) > start and last.get('type') == 'agent_response'
            and last['text'] == response)
