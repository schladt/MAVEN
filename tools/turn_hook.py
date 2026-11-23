"""Native hook subprocess. Emit only the selected client's protocol; never log event input."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('harness', choices=('omp', 'claude', 'codex', 'copilot', 'gemini'))
    parser.add_argument('phase', choices=('begin', 'observe', 'end', 'guard'))
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.home.is_absolute() or args.home.resolve() != args.home or not args.home.is_dir():
            raise ValueError('invalid installation')
        # Clients may include arbitrary tool output. Bound input and do not spool or echo it.
        limit = 8 * 1024 * 1024
        raw = sys.stdin.buffer.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('oversize event')
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError('invalid event')
        if args.harness == 'omp':
            from tools import turn_runtime as runtime
            common = (args.home, 'omp', event['conversation'], event['cwd'])
            if args.phase == 'begin':
                result = runtime.begin(*common, disable=event.get('disable') is True, enable=event.get('enable') is True,
                                       turn_id=event.get('turn_id'), known=event.get('known'))
            elif args.phase == 'observe':
                result = runtime.observe(*common, write=event.get('write') is True, opaque=event.get('opaque') is True,
                                         turn_key=event.get('turn_key'))
            elif args.phase == 'end':
                result = runtime.end(*common, turn_key=event.get('turn_key'))
            else:
                raise ValueError('unknown phase')
        else:
            from tools.turn_native import dispatch
            result = dispatch(args.harness, args.phase, args.home, event)
    except Exception:
        # Never print native event text, model arguments, record data or SDK errors to stderr.
        print('MAVEN: local turn hook unavailable; consultation and capture are not verified.', file=sys.stderr)
        return 1
    print(json.dumps(result or {}, ensure_ascii=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
