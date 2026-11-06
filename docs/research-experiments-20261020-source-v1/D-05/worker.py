"""Public I/O adapter. Algorithm, packaging and D-02 rank timing are separate stages.

This adapter does not establish a sandbox or authenticate experiment results;
those responsibilities belong to the caller and trusted platform collector.
"""
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from artifacts import package

PROTOCOL=json.loads(Path(__file__).with_name('protocol.json').read_text(encoding='utf-8'))
random.seed(PROTOCOL['seed'])


def main():
    message=json.load(sys.stdin)
    if not isinstance(message,dict) or set(message) != {'stage','payload'}:
        raise ValueError('Worker expects exactly stage and payload')
    stage,payload=message['stage'],message['payload']
    if not isinstance(stage,str) or not isinstance(payload,dict):
        raise ValueError('Worker stage must be a string and payload an object')
    case=PROTOCOL['case_id']
    if stage=='package':
        if payload.get('case_id') != case:
            raise ValueError('Packaging case does not match this worker')
        result=package(payload)
    elif re.fullmatch(r'benchmark_(?:baseline|candidate)_[1-9][0-9]*',stage):
        if case != 'D-02':
            raise ValueError('Retrieval benchmark stage is available only for D-02')
        request=payload.get('request');calls=payload.get('calls')
        if (not isinstance(request,dict) or request.get('case_id') != case
                or request.get('phase') != 'rank' or type(calls) is not int or calls <= 0):
            raise ValueError('D-02 benchmark requires a rank request and positive integer calls')
        import implementation
        count=0;digest=hashlib.sha256()
        for _ in range(calls):
            value=implementation.run_experiment(request)
            ranking=value['candidate']
            ids={q:[r['id'] for r in rows] for q,rows in ranking.items()}
            digest.update(json.dumps(ids,sort_keys=True,separators=(',',':')).encode()+b'\n')
            count+=len(request['data']['test_queries'])
        result={'completed_queries':count,'rankings_sha256':digest.hexdigest()}
    elif stage in ('run','rank','repeat') or re.fullmatch(r'(?:primary|boundary)_[1-9][0-9]*',stage):
        phase='rank' if stage=='rank' else 'run'
        if (payload.get('case_id') != case or payload.get('phase') != phase
                or (stage=='rank' and case!='D-02')):
            raise ValueError('Algorithm request case or phase does not match this worker stage')
        import implementation
        result=implementation.run_experiment(payload)
    else:
        raise ValueError('Unsupported worker stage: '+stage)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':main()
