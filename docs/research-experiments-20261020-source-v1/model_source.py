"""Extract a complete model response without rewriting its Python source.

This is an offline format check, not a platform source credential.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re


def _normalize(source):
    if not isinstance(source, str) or not 0 < len(source.encode('utf-8')) <= 65536:
        raise ValueError('Source must be 1..65536 UTF-8 bytes')
    source = source.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'
    tree = ast.parse(source, feature_version=(3, 9))
    if not any(isinstance(n, ast.FunctionDef) and n.name == 'run_experiment' for n in tree.body):
        raise ValueError('Missing run_experiment(request)')
    return source


def extract_source(response):
    if not isinstance(response, dict):
        raise ValueError('Expected a complete chat/completions JSON response')
    found = set()
    for choice in response.get('choices', []):
        if not isinstance(choice, dict) or choice.get('finish_reason') != 'stop':
            continue
        message = choice.get('message', {})
        if message.get('role') != 'assistant' or not isinstance(message.get('content'), str):
            continue
        content = message['content']
        candidates = [content]
        candidates.extend(re.findall(r'^```(?:python|py)?[ \t]*\r?\n(.*?)^```[ \t]*$',
                                     content, re.MULTILINE | re.DOTALL))
        for candidate in candidates:
            try:
                found.add(_normalize(candidate))
            except (SyntaxError, ValueError, RecursionError):
                pass
    if len(found) != 1:
        raise ValueError('Expected exactly one complete Python implementation with finish_reason=stop; request a full new response')
    return found.pop()


def save_source(response, destination):
    source = extract_source(response)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.encode('utf-8'))
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--response', required=True, help='Saved complete chat/completions JSON, not only message.content')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    response = json.loads(Path(args.response).read_text(encoding='utf-8-sig'))
    print(json.dumps({'source_sha256': save_source(response, args.output),
                      'offline_format_only': True, 'platform_credential_issued': False}))
