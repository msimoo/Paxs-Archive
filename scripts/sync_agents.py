import os
import json
import requests

CONFIG_PATH = r'D:\Paxs Archive\settings.json'
AGENTS_DIR = os.path.expanduser(r'~/.gemini/config/agents')
os.makedirs(AGENTS_DIR, exist_ok=True)

try:
    r = requests.get('http://127.0.0.1:8899/v1beta/models', timeout=5)
    models = r.json().get('models', [])
except Exception as e:
    print(f'Error fetching models: {e}')
    models = []

print(f'Syncing {len(models)} models to {AGENTS_DIR}...')

# Clean old auto-generated agents
for fname in os.listdir(AGENTS_DIR):
    if fname.endswith('.md'):
        try:
            os.remove(os.path.join(AGENTS_DIR, fname))
        except:
            pass

created = 0
for m in models:
    name = m.get('baseModelId') or m.get('name', '').replace('models/', '')
    display = m.get('displayName', name)
    desc = m.get('description', f'AI Model {name}')
    
    # Clean filename
    safe_name = name.replace('/', '_').replace(':', '_')
    file_path = os.path.join(AGENTS_DIR, f'{safe_name}.md')
    
    content = f"""---
name: {safe_name}
description: {display} - {desc}
model: {name}
---

You are an expert AI assistant powered by {display}.
"""
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    created += 1

print(f'Successfully created {created} agents in {AGENTS_DIR}!')
