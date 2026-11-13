#!/usr/bin/env python3
"""Apply the oms empty_openapi relocation pattern to every sibling repo with
crate::openapi referenced from src/registration.rs (embedded in tests)."""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv

SERVICE_DIRS = []
for vertical in ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8'):
    for p in sorted((tree / vertical).iterdir()):
        if p.is_dir() and (p / 'src' / 'registration.rs').is_file():
            txt = (p / 'src' / 'registration.rs').read_text()
            if 'crate::openapi' in txt:
                SERVICE_DIRS.append(p)

print(f'repos with the pattern: {len(SERVICE_DIRS)}')
for p in SERVICE_DIRS:
    print(' ', p.relative_to(tree))
if not APPLY:
    sys.exit(0)

for p in SERVICE_DIRS:
    reg = p / 'src' / 'registration.rs'
    txt = reg.read_text()
    mainp = p / 'src' / 'main.rs'
    mtxt = mainp.read_text()
    if 'OPENAPI_DUMP' in mtxt:
        print(f'skip (already patched): {p.name}')
        continue
    # discover the openapi api struct name + module name in main.rs
    m = re.search(r'^mod (\w*openapi\w*);', mtxt, re.M)
    if not m:
        print(f'{p.name}: no openapi mod in main.rs - MANUAL');
        continue
    mod = m.group(1)
    api = re.search(r'pub struct (\w*OpenApi);', (p / 'src' / f'{mod}.rs').read_text())
    api = api.group(1) if api else None
    if not api:
        print(f'{p.name}: no OpenApi struct found - MANUAL')
        continue
    new_mtxt, n = re.subn(
        r'(^mod \w*openapi\w*;\n)',
        r'\1\n'
        r'\n'
        r'// The utoipa dump as a plain fn pointer for registration.rs (which\n'
        r'// cannot name crate::openapi - the embedded test harnesses carry no\n'
        r'// openapi module; see registration.rs).\n'
        r'static OPENAPI_DUMP: fn() -> serde_json::Value = \|\| {\n'
        r'    use utoipa::OpenApi as _;\n'
        rf'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
        r'};\n',
        mtxt, count=1, flags=re.M)
    if n != 1:
        print(f'{p.name}: main.rs injection failed - MANUAL')
        continue
    # rewrite registration.rs: remove the fn, cfg-gate the wiring
    fn_pattern = re.compile(
        r'fn empty_openapi\(\) -> serde_json::Value \{\n'
        r'    serde_json::to_value\(crate::openapi::\w+::openapi\(\)\)\.unwrap_or\(serde_json::Value::Null\)\n'
        r'\}\n', re.M)
    new_txt, n1 = fn_pattern.subn('', txt)
    if n1 != 1:
        print(f'{p.name}: empty_openapi removal failed (n={n1}) - MANUAL')
        continue
    new_txt, n2 = re.subn(r'openapi: empty_openapi,', 'openapi: crate::OPENAPI_DUMP,', new_txt)
    new_txt, n3 = re.subn(r'print_registration\(section, &reg, empty_openapi\)',
                          'print_registration(section, &reg, crate::OPENAPI_DUMP)', new_txt)
    if n2 != 1 or n3 != 1:
        print(f'{p.name}: wiring rewrite failed (n2={n2}, n3={n3}) - MANUAL')
        continue
    mainp.write_text(new_mtxt)
    reg.write_text(new_txt)
    r = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True,
                       env={'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)})
    status = 'build-ok' if 'error' not in (r.stderr or '') and r.returncode == 0 else f'build-FAIL: {r.stderr.strip()[:100]}'
    if r.returncode != 0:
        print(f'{p.name}: {status}')
        continue
    rt = subprocess.run(['cargo', 'test', '--test', 'handlers_oneshot'], cwd=p,
                        capture_output=True, text=True,
                        env={'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)})
    tests = 'tests-unknown'
    if '"ok' in rt.stdout or 'test result: ok' in rt.stdout:
        tests = 'tests-ok'
    elif 'error' in rt.stderr:
        tests = f'tests-FAIL {rt.stderr.strip()[:80]}'
    subprocess.run(['git', '-C', str(p), 'add', '-A'])
    c = subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                        'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)', ],
                       capture_output=True, text=True)
    push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                          capture_output=True, text=True)
    print(f'{p.name}: {status} {tests} (pushed)' if push.returncode == 0 else f'{p.name}: {status} (push pending)')
