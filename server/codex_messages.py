"""Classify Codex message content by provenance, without treating runtime context as a user prompt."""

TEXT = ('input_text', 'output_text', 'text')
GOALS = {'goal.internal_context': ('Goal continuation', 'Codex continues toward the active goal.'),
         'user.goal': ('Goal updated', 'The goal or its status changed.'),
         'user.goal.omitted': ('Goal updated', 'The goal or its status changed.')}
CONTEXT = {'agents_md.instructions', 'environments.environment_context'}


def parts(payload):
    content = payload.get('content')
    if not isinstance(content, list):
        return []
    meta = payload.get('internal_chat_message_metadata_passthrough')
    meta = meta if isinstance(meta, dict) else {}
    kinds, origins = meta.get('content_item_kinds'), meta.get('content_item_metadata')
    aligned = isinstance(kinds, list) and isinstance(origins, list) and len(kinds) == len(origins) == len(content)
    result = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        origin = origins[index].get('provenance') if aligned and isinstance(origins[index], dict) else {}
        origin = origin if isinstance(origin, dict) else {}
        kind = kinds[index] if aligned and isinstance(kinds[index], str) else ''
        user = origin.get('type') == 'user'
        if not user and origin.get('type') in ('harness', 'agents_md'):
            if kind in CONTEXT:
                continue
            if kind in GOALS and part.get('type') in TEXT:
                title, text = GOALS[kind]
                result.append({'role': 'assistant', 'kind': 'notice', 'title': title, 'text': text})
                continue
        text = part.get('text')
        if part.get('type') in TEXT and isinstance(text, str) and text.strip():
            # Unknown metadata and literal user examples stay visible.
            result.append({'role': payload.get('role'), 'text': text, '_user': user})
        elif part.get('type') in ('input_image', 'image', 'image_url'):
            result.append({'role': payload.get('role'), 'text': '[Image attachment]', '_user': user})
    # Keep ordinary multi-part messages together, while leaving runtime notices separate.
    merged = []
    for part in result:
        if merged and not part.get('kind') and not merged[-1].get('kind') and part.get('role') == merged[-1].get('role') and part.get('_user') == merged[-1].get('_user'):
            merged[-1]['text'] += '\n' + part['text']
        else:
            merged.append(part)
    return merged


def background(payload):
    """True only when known runtime content supplied the input, never for explicit user content."""
    parsed = parts(payload)
    return bool(parsed) and all(part.get('title') == 'Goal continuation' for part in parsed)
