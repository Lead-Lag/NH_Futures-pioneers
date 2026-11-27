"""User-approved fake-response checks only; this runner blocks network connects."""
from __future__ import annotations

import argparse
import socket
import unittest
from unittest.mock import patch

from config import HERE, load_config, resolve_config
from runtime import create_run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(HERE/'strategy_config.yaml'))
    parser.add_argument('--out')
    args=parser.parse_args()
    source=load_config(args.config);config=resolve_config(source,'base')
    run=create_run('validation','approved_fake_only',config,source,args.config,out_root=args.out)
    run.metadata.update(tests_executed=True,approval='User explicitly permitted account-order-free automated tests.',
                        network_blocked=True,actual_orders_enabled=False)
    tests=unittest.defaultTestLoader.discover(str(HERE/'tests'),pattern='test_*.py')
    with (run.out/'validation.txt').open('w',encoding='utf-8') as output:
        with patch.object(socket.socket,'connect',side_effect=RuntimeError('Network disabled in approved fake tests')), \
             patch.object(socket,'create_connection',side_effect=RuntimeError('Network disabled in approved fake tests')):
            result=unittest.TextTestRunner(stream=output,verbosity=2).run(tests)
    metrics={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
             'skipped':len(result.skipped),'network_connections_allowed':False,'account_orders_sent':0}
    run.finish(metrics,status='completed' if result.wasSuccessful() else 'failed',
               notes=['Explicitly approved fake-response tests. Network connect is blocked; no live or paper account order can be sent.',
                      'Passing fake tests does not establish actual NH WebSocket/account compatibility.'])
    print(metrics);print(run.out/'summary.md')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
