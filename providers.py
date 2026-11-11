"""Provider adapters cùng một interface. Không chứa agent loop."""
import json
import os
import uuid
import urllib.error
import urllib.request

DEFAULT_OPENCODE_ENDPOINT = 'https://opencode.ai/inference/go/openai/v1'

DEFAULT_NVIDIA_ENDPOINT = 'https://integrate.api.nvidia.com/v1'

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
    # OpenAI-compatible: key=API_KEY, URL từ API_ENDPOINT (+ /chat/completions).
    'opencode': ('API_KEY', None),
    # NVIDIA NIM / integrate.api.nvidia.com
    'nvidia': ('NVIDIA_API_KEY', None),
}


def chat_completions_url(endpoint):
    base = (endpoint or DEFAULT_OPENCODE_ENDPOINT).rstrip('/')
    if base.endswith('/chat/completions'):
        return base
    return f'{base}/chat/completions'


def post_json(url, headers, payload, timeout=180, retries=4):
    raw = json.dumps(payload).encode()
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=raw, headers={'Content-Type': 'application/json', **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors='replace')[:240]
            last_exc = RuntimeError(
                f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota. {body}'
            )
            # Free-tier Gemini often returns 429/503; wait and retry a few times.
            if exc.code in (429, 503) and attempt + 1 < retries:
                import time
                time.sleep(20 * (attempt + 1))
                continue
            raise last_exc from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc
    raise last_exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if name in ('opencode', 'nvidia') and not model:
            model = os.getenv('API_MODEL') or os.getenv('MODEL')
        if not model:
            raise ValueError('Cần đặt MODEL (hoặc API_MODEL) là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, url = CONFIG[name]
        if name == 'opencode':
            self.url = chat_completions_url(os.getenv('API_ENDPOINT'))
        elif name == 'nvidia':
            self.url = chat_completions_url(os.getenv('NVIDIA_BASE_URL') or DEFAULT_NVIDIA_ENDPOINT)
        else:
            self.url = url
        self.key = os.getenv(key_name) or (os.getenv('API_KEY') if name == 'nvidia' else None)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md / .env.example.')
        # Go lane requires a stable session id per conversation for routing/cache.
        self.session = os.getenv('OPENCODE_SESSION') or f'ses_{uuid.uuid4().hex}'

    def _headers(self):
        headers = {'Authorization': f'Bearer {self.key}', 'User-Agent': 'StatusAgentLab/1.0'}
        if self.name == 'opencode':
            headers['x-opencode-session'] = self.session
        return headers

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        # Lab agent loop needs non-streaming tool calls; keep NVIDIA extras off by default.
        if self.name == 'nvidia':
            payload.setdefault('temperature', float(os.getenv('NVIDIA_TEMPERATURE', '0.2')))
            payload.setdefault('top_p', float(os.getenv('NVIDIA_TOP_P', '0.95')))
            payload.setdefault('max_tokens', int(os.getenv('NVIDIA_MAX_TOKENS', '2048')))
        data = post_json(self.url, self._headers(), payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = []
        for c in message.get('tool_calls') or []:
            fn = c.get('function') or {}
            call_id = c.get('id') or f'call_{uuid.uuid4().hex[:12]}'
            c['id'] = call_id
            calls.append({'id': call_id, 'name': fn.get('name'), 'arguments': fn.get('arguments') or '{}'})
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
