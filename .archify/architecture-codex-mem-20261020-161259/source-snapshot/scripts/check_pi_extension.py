#!/usr/bin/env python3
"""Typecheck the optional extension against an installed Pi package and Node types."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi-package', required=True, type=Path)
    parser.add_argument('--tsc', default='tsc')
    args = parser.parse_args()
    package = args.pi_package.resolve()
    if json.loads((package / 'package.json').read_text())['version'] != '1.4.0':
        parser.error('Pi 1.4.0 declarations are required')
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / 'tsconfig.json'
        config.write_text(json.dumps({'compilerOptions': {
            'target': 'ES2023', 'module': 'NodeNext', 'moduleResolution': 'NodeNext',
            'strict': True, 'noEmit': True, 'skipLibCheck': True,
            'paths': {'@earendil-works/pi-coding-agent': [str(package / 'dist/index.d.ts')]},
            'typeRoots': [str(package / 'node_modules/@types')],
        }, 'files': [str(Path(__file__).resolve().parents[1] / 'pi/extension.ts')]}))
        return subprocess.run([args.tsc, '-p', str(config)], timeout=60).returncode


if __name__ == '__main__':
    raise SystemExit(main())
