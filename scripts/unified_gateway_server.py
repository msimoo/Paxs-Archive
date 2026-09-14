import asyncio
import json
import logging
import os
import re
import uuid
from typing import Dict, Any, List, Optional
import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('UnifiedGateway')

app = FastAPI(title='Antigravity Unified Multi-Provider Gateway')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(WORKSPACE_ROOT, 'settings.json')
AGENTS_DIR = os.path.expanduser(r'~/.gemini/config/agents')

# Default mappings for /model presets
DEFAULT_MODEL_MAPPINGS = {
    'gemini-3.7-flash': 'deepseek-v4-flash',
    'gemini-3.1-pro': 'claude-opus-5-thinking',
    'gemini-3.5-flash': 'gpt-5.6-luna',
    'gemini-3.6-flash': 'claude-fable-5',
    'gemini-2.5-pro': 'claude-opus-5',
    'gemini-2.5-flash': 'deepseek-v4-flash'
}

def load_providers() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f'Config file not found at {CONFIG_PATH}')
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('gatewayProviders', {})
    except Exception as e:
        logger.error(f'Error reading config: {e}')
        return {}

cached_models_map: Dict[str, str] = {} # model_id -> provider_name
cached_models_list: List[Dict[str, Any]] = []

def sync_agents_to_filesystem(models: List[Dict[str, Any]]):
    try:
        os.makedirs(AGENTS_DIR, exist_ok=True)
        # Clear existing agent files
        for fname in os.listdir(AGENTS_DIR):
            if fname.endswith('.md'):
                try:
                    os.remove(os.path.join(AGENTS_DIR, fname))
                except:
                    pass

        for m in models:
            name = m.get('baseModelId') or m.get('name', '').replace('models/', '')
            display = m.get('displayName', name)
            desc = m.get('description', f'Gateway model {name}')
            safe_name = name.replace('/', '_').replace(':', '_')
            file_path = os.path.join(AGENTS_DIR, f'{safe_name}.md')

            content = f"""---
name: {safe_name}
description: {display}
model: {name}
---

You are an expert AI assistant powered by {display}.
"""
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        logger.info(f'Synced {len(models)} model agents to {AGENTS_DIR}')
    except Exception as e:
        logger.error(f'Failed to sync agents: {e}')

async def fetch_all_models() -> List[Dict[str, Any]]:
    global cached_models_map, cached_models_list
    providers = load_providers()
    models_out = []
    model_to_prov = {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        for prov_name, prov_info in providers.items():
            base_url = prov_info.get('baseUrl', '').rstrip('/')
            api_key = prov_info.get('apiKey', '')
            
            models_url = f'{base_url}/v1/models' if not base_url.endswith('/v1') else f'{base_url}/models'
            headers = {'Authorization': f'Bearer {api_key}'}
            
            try:
                r = await client.get(models_url, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    m_list = data.get('data', [])
                    for m in m_list:
                        m_id = m.get('id') if isinstance(m, dict) else str(m)
                        if not m_id:
                            continue
                        
                        namespaced_id = f'{prov_name}/{m_id}'
                        model_to_prov[namespaced_id] = prov_name
                        model_to_prov[m_id] = prov_name
                        
                        display_name = f'{m_id} ({prov_name})'
                        models_out.append({
                            'name': f'models/{namespaced_id}',
                            'baseModelId': namespaced_id,
                            'version': '1.0',
                            'displayName': display_name,
                            'description': f'Provided via {prov_name} gateway ({base_url})',
                            'inputTokenLimit': 200000,
                            'outputTokenLimit': 8192,
                            'supportedGenerationMethods': ['generateContent', 'streamGenerateContent', 'countTokens']
                        })
                        models_out.append({
                            'name': f'models/{m_id}',
                            'baseModelId': m_id,
                            'version': '1.0',
                            'displayName': m_id,
                            'description': f'Auto-routed to {prov_name}',
                            'inputTokenLimit': 200000,
                            'outputTokenLimit': 8192,
                            'supportedGenerationMethods': ['generateContent', 'streamGenerateContent', 'countTokens']
                        })
                else:
                    logger.warning(f'Provider {prov_name} returned status {r.status_code} on models endpoint')
            except Exception as e:
                logger.warning(f'Could not fetch models for provider {prov_name}: {e}')

    seen = set()
    unique_models = []
    for m in models_out:
        if m['name'] not in seen:
            seen.add(m['name'])
            unique_models.append(m)

    cached_models_map = model_to_prov
    cached_models_list = unique_models
    sync_agents_to_filesystem(unique_models)
    return unique_models

@app.on_event('startup')
async def on_startup():
    await fetch_all_models()

@app.get('/')
@app.get('/health')
async def health():
    providers = load_providers()
    return {'status': 'healthy', 'active_providers': list(providers.keys()), 'cached_models_count': len(cached_models_list)}

@app.get('/v1beta/models')
@app.get('/v1/models')
async def list_models(request: Request):
    models = await fetch_all_models()
    return {'models': models}

@app.get('/v1beta/models/{model_name:path}')
async def get_model(model_name: str):
    models = await fetch_all_models()
    clean_name = model_name.replace('models/', '')
    for m in models:
        if m['name'] == f'models/{clean_name}' or m['baseModelId'] == clean_name:
            return m
    return {
        'name': f'models/{clean_name}',
        'baseModelId': clean_name,
        'displayName': clean_name,
        'supportedGenerationMethods': ['generateContent', 'streamGenerateContent', 'countTokens']
    }

def convert_gemini_to_openai(body: Dict[str, Any], target_model: str) -> Dict[str, Any]:
    messages = []
    
    sys_inst = body.get('systemInstruction')
    if sys_inst:
        parts = sys_inst.get('parts', [])
        sys_text = ' '.join([p.get('text', '') for p in parts if 'text' in p])
        if sys_text:
            messages.append({'role': 'system', 'content': sys_text})

    contents = body.get('contents', [])
    for c in contents:
        role = c.get('role', 'user')
        if role == 'model':
            role = 'assistant'
        
        parts = c.get('parts', [])
        text_content = ''
        tool_calls = []
        tool_responses = []

        for p in parts:
            if 'text' in p:
                text_content += p['text']
            elif 'functionCall' in p:
                fc = p['functionCall']
                call_id = f'call_{uuid.uuid4().hex[:12]}'
                tool_calls.append({
                    'id': call_id,
                    'type': 'function',
                    'function': {
                        'name': fc.get('name', ''),
                        'arguments': json.dumps(fc.get('args', {}))
                    }
                })
            elif 'functionResponse' in p:
                fr = p['functionResponse']
                tool_responses.append({
                    'role': 'tool',
                    'tool_call_id': fr.get('id', f'call_{uuid.uuid4().hex[:12]}'),
                    'content': json.dumps(fr.get('response', {}).get('content', fr.get('response', {})))
                })

        if tool_responses:
            for tr in tool_responses:
                messages.append(tr)
        elif tool_calls:
            msg = {'role': 'assistant'}
            if text_content:
                msg['content'] = text_content
            msg['tool_calls'] = tool_calls
            messages.append(msg)
        else:
            messages.append({'role': role, 'content': text_content or ' '})

    openai_tools = []
    for t in body.get('tools', []):
        for fd in t.get('functionDeclarations', []):
            openai_tools.append({
                'type': 'function',
                'function': {
                    'name': fd.get('name'),
                    'description': fd.get('description', ''),
                    'parameters': fd.get('parameters', {'type': 'object', 'properties': {}})
                }
            })

    gen_config = body.get('generationConfig', {})
    temperature = gen_config.get('temperature', 0.7)
    max_tokens = gen_config.get('maxOutputTokens')

    openai_payload = {
        'model': target_model,
        'messages': messages,
        'temperature': temperature,
    }
    if max_tokens:
        openai_payload['max_tokens'] = max_tokens
    if openai_tools:
        openai_payload['tools'] = openai_tools

    return openai_payload

def resolve_provider_and_model(model_param: str) -> tuple[str, str, Dict[str, str]]:
    clean = model_param.replace('models/', '').split(':')[0]
    providers = load_providers()

    # Map preset Gemini models to top gateway models
    if clean in DEFAULT_MODEL_MAPPINGS:
        mapped_target = DEFAULT_MODEL_MAPPINGS[clean]
        logger.info(f'Mapping preset model [{clean}] -> [{mapped_target}]')
        clean = mapped_target

    if '/' in clean:
        prov_name, actual_model = clean.split('/', 1)
        if prov_name in providers:
            return prov_name, actual_model, providers[prov_name]

    if clean in cached_models_map:
        prov_name = cached_models_map[clean]
        if prov_name in providers:
            return prov_name, clean, providers[prov_name]

    if providers:
        first_prov = next(iter(providers.keys()))
        return first_prov, clean, providers[first_prov]

    raise HTTPException(status_code=400, detail=f'No gateway provider available for model {clean}')

@app.post('/v1beta/models/{model_action:path}')
@app.post('/v1/models/{model_action:path}')
async def handle_generate(model_action: str, request: Request):
    body = await request.json()
    is_stream = ':streamGenerateContent' in model_action or request.query_params.get('alt') == 'sse'
    
    prov_name, actual_model, prov_info = resolve_provider_and_model(model_action)
    base_url = prov_info.get('baseUrl', '').rstrip('/')
    api_key = prov_info.get('apiKey', '')
    
    chat_url = f'{base_url}/chat/completions' if base_url.endswith('/v1') else f'{base_url}/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }

    openai_payload = convert_gemini_to_openai(body, actual_model)
    openai_payload['stream'] = is_stream

    logger.info(f'Routing request to [{prov_name}] model [{actual_model}] (stream={is_stream})')

    if not is_stream:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(chat_url, headers=headers, json=openai_payload)
            if resp.status_code != 200:
                logger.error(f'Upstream error from {prov_name}: {resp.status_code} - {resp.text}')
                return JSONResponse(status_code=resp.status_code, content={'error': resp.text})
            
            data = resp.json()
            choice = data.get('choices', [{}])[0]
            msg = choice.get('message', {})
            
            parts = []
            if msg.get('content'):
                parts.append({'text': msg['content']})
            if msg.get('tool_calls'):
                for tc in msg['tool_calls']:
                    fn = tc.get('function', {})
                    args_obj = {}
                    try:
                        args_obj = json.loads(fn.get('arguments', '{}'))
                    except:
                        args_obj = {'raw': fn.get('arguments')}
                    parts.append({
                        'functionCall': {
                            'name': fn.get('name'),
                            'args': args_obj
                        }
                    })

            gemini_resp = {
                'candidates': [
                    {
                        'content': {
                            'role': 'model',
                            'parts': parts or [{'text': ''}]
                        },
                        'finishReason': 'STOP',
                        'index': 0
                    }
                ]
            }
            return gemini_resp

    async def sse_generator():
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream('POST', chat_url, headers=headers, json=openai_payload) as resp:
                if resp.status_code != 200:
                    err_bytes = await resp.aread()
                    err_msg = err_bytes.decode('utf-8', errors='replace')
                    logger.error(f'Upstream stream error from {prov_name}: {resp.status_code} - {err_msg}')
                    err_gemini = {
                        'candidates': [{
                            'content': {'parts': [{'text': f'[Error from {prov_name}: {err_msg}]'}]},
                            'finishReason': 'STOP'
                        }]
                    }
                    yield f'data: {json.dumps(err_gemini)}\n\n'
                    return

                accumulated_tool_calls = {}

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith('data: '):
                        data_str = line[6:].strip()
                        if data_str == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            choice = chunk.get('choices', [{}])[0]
                            delta = choice.get('delta', {})

                            text = delta.get('content')
                            if text:
                                gemini_chunk = {
                                    'candidates': [{
                                        'content': {
                                            'role': 'model',
                                            'parts': [{'text': text}]
                                        }
                                    }]
                                }
                                yield f'data: {json.dumps(gemini_chunk)}\n\n'

                            tool_deltas = delta.get('tool_calls', [])
                            for td in tool_deltas:
                                idx = td.get('index', 0)
                                if idx not in accumulated_tool_calls:
                                    accumulated_tool_calls[idx] = {
                                        'name': td.get('function', {}).get('name', ''),
                                        'arguments': ''
                                    }
                                if td.get('function', {}).get('name'):
                                    accumulated_tool_calls[idx]['name'] = td['function']['name']
                                if td.get('function', {}).get('arguments'):
                                    accumulated_tool_calls[idx]['arguments'] += td['function']['arguments']

                        except Exception as e:
                            logger.debug(f'Stream parse error: {e}')

                if accumulated_tool_calls:
                    fc_parts = []
                    for idx, call in accumulated_tool_calls.items():
                        args_obj = {}
                        try:
                            args_obj = json.loads(call['arguments'])
                        except:
                            args_obj = {'raw': call['arguments']}
                        fc_parts.append({
                            'functionCall': {
                                'name': call['name'],
                                'args': args_obj
                            }
                        })
                    tool_chunk = {
                        'candidates': [{
                            'content': {
                                'role': 'model',
                                'parts': fc_parts
                            },
                            'finishReason': 'STOP'
                        }]
                    }
                    yield f'data: {json.dumps(tool_chunk)}\n\n'

    return StreamingResponse(sse_generator(), media_type='text/event-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8899))
    logger.info(f'Starting Antigravity Unified Gateway Proxy on http://127.0.0.1:{port}')
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='info')
