"""Submit contestant source through the case-scoped platform RPC."""
import argparse,runpy
from pathlib import Path
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--implementation',required=True)
    args=parser.parse_args()
    helper=runpy.run_path(str(Path(__file__).resolve().parents[1]/'run_experiment.py'))
    import json
    print(json.dumps(helper['run']('D-01',args.implementation),ensure_ascii=False,allow_nan=False))
