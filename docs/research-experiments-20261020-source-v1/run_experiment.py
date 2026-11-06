"""Contestant-side client for the platform's case-scoped Unix socket RPC."""
import argparse
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket


def run(case_id, implementation=None):
    if case_id != os.environ.get('RESEARCH_EXPERIMENT_CASE'):
        raise ValueError('This experiment case is not active')
    url = os.environ['RESEARCH_EXPERIMENT_URL']
    if not url.startswith('unix:///'):
        raise ValueError('Platform experiment socket unavailable')

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(url[len('unix://'):])

    body = {'case_id': case_id}
    if os.environ.get('RESEARCH_LAYERED_D') == 'layered-d-v3':
        if implementation is None:raise ValueError('Layered D requires --implementation')
        source=Path(implementation).read_bytes()
        if not 0<len(source)<=65536:raise ValueError('Implementation source size invalid')
        body.update(protocol_version='layered-d-v3',implementation_source=source.decode('utf-8'))
    elif case_id == 'D-01' and os.environ.get('RESEARCH_D01_SCAFFOLD') == '1':
        if implementation is None:
            raise ValueError('D-01 requires --implementation <your completed implementation.py>')
        source = Path(implementation).read_bytes()
        if not 0 < len(source) <= 65536:
            raise ValueError('D-01 implementation must be 1..65536 UTF-8 bytes')
        body['implementation_source'] = source.decode('utf-8')
    def rpc(path, payload):
        connection = UnixConnection('localhost', timeout=600 if os.environ.get('RESEARCH_LAYERED_D')=='layered-d-v3' else 125 if os.environ.get('RESEARCH_D01_SCAFFOLD') == '1' and case_id == 'D-01' else 110)
        try:
            connection.request('POST', path, body=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + os.environ['RESEARCH_EXPERIMENT_TOKEN']})
            response = connection.getresponse()
            result = json.load(response)
            if response.status != 200:
                raise RuntimeError(str(result.get('error', 'Trusted experiment failed; do not invent metrics')))
            return result
        finally:
            connection.close()
    if os.environ.get('RESEARCH_D_SOURCE_PROVENANCE') == 'd-model-source-v1':
        receipt = rpc('/source', body)
        body = {'case_id': case_id, 'protocol_version': 'layered-d-v3',
                'source_credential': receipt['source_credential']}
    result = rpc('/run', body)
    if result.get('status') != 'complete' or result.get('case_id') != case_id:
        raise RuntimeError('Experiment did not complete')
    submission = result['submission']
    team = os.environ['RESEARCH_WORKSPACE_TEAM']
    source_root = Path('/data/experiment-results')
    output = Path(os.environ.get('RESEARCH_RESULT_ROOT', '/output'))
    for figure in submission['figures']:
        relative = PurePosixPath(figure['path'])
        if relative.is_absolute() or '..' in relative.parts or '\\' in figure['path'] or not str(relative).startswith(f'data/figure/{team}/'):
            raise ValueError('Invalid experiment figure path')
        target = output / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / str(relative), target)
    target = output / 'data' / team / (case_id + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(submission, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return submission


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True)
    parser.add_argument('--implementation')
    args = parser.parse_args()
    print(json.dumps(run(args.case, args.implementation), ensure_ascii=False, allow_nan=False))
