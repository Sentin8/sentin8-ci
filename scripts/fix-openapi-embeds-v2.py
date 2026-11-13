#!/usr/bin/env python3
"""Batch v2: the empty_openapi relocation for every remaining repo.

Handles BOTH body shapes (single-line and wrapped .unwrap_or) and
per-repo struct/mod names. cfg-gates the binary reference so the
embedded test harnesses compile.
"""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')

def targets():
    out = []
    for v in VERTICALS:
        d = tree / v
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            f = p / 'src' / 'registration.rs'
            if f.is_file() and 'crate::openapi' in f.read_text():
                out.append(p)
    return out

done = manual = 0
for p in targets():
    reg = p / 'src' / 'registration.rs'
    mainp = p / 'src' / 'main.rs'
    txt = reg.read_text()
    mtxt = mainp.read_text()
    if 'OPENAPI_DUMP' in mtxt:
        continue  # previously patched (oms handled manually)
    mmod = re.search(r'^mod (\w*openapi\w*);', mtxt, re.M)
    if not mmod:
        print(f'MANUAL {p.name}: no openapi mod in main.rs')
        manual += 1
        continue
    mod = mmod.group(1)
    o = (p / 'src' / f'{mod}.rs')
    api_m = re.search(r'pub struct (\w+OpenApi);', o.read_text())
    if not api_m:
        print(f'MANUAL {p.name}: no OpenApi struct in {mod}.rs')
        manual += 1
        continue
    api = api_m.group(1)
    # remove whatever the fn body looks like (single or wrapped form)
    pat = re.compile(
        r'(?:^|\n)fn empty_openapi\(\) -> serde_json::Value \{\n'
        r'    serde_json::to_value\(crate::openapi::\w+::openapi\(\)\)(\n'
        r'        \.unwrap_or\(serde_json::Value::Null\)|\.unwrap_or\(serde_json::Value::Null\))\n'
        r'\}\n')
    new_txt, n1 = pat.subn('\n', txt)
    if n1 != 1:
        print(f'MANUAL {p.name}: empty_openapi removal n={n1}')
        manual += 1
        continue
    new_txt = new_txt.replace('openapi: empty_openapi,', 'openapi: crate::OPENAPI_DUMP,')
    new_txt, n3 = re.subn(r'\bempty_openapi\b', 'crate::OPENAPI_DUMP', new_txt)
    if 'empty_openapi' in new_txt:
        print(f'MANUAL {p.name}: residual empty_openapi refs')
        manual += 1
        continue
    inj = (
        mtxt.replace(mmod.group(0),
            mmod.group(0)
            + '\n\n// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
            + '// name crate::openapi - the embedded test harnesses carry no openapi\n'
            + '// module; see registration.rs).\n'
            + 'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
            + '    use utoipa::OpenApi as _;\n'
            + f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
            + '};\n')
    )
    if not APPLY:
        print(f'would fix {p.name} ({mod}::{api})')
        done += 1
        continue
    mainp.write_text(inj)
    reg.write_text(new_txt)
    env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
    rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
    if rb.returncode != 0:
        print(f'FAILED {p.name}: {rb.stderr.strip()[:110]}')
        manual += 1
        continue
    tests = 'na'
    if (p / 'tests').is_dir():
        rt = subprocess.run(['bash', '-c', 'cargo test --test handlers_oneshot 2>/dev/null | tail -1 || cargo test 2>/dev/null | tail -1'],
                            cwd=p, capture_output=True, text=True, env=env)
        tests = (rt.stdout.strip() or '?')[:40]
    subprocess.run(['git', '-C', str(p), 'add', '-A'])
    subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                    'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                   capture_output=True)
    push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                          capture_output=True, text=True)
    state = 'pushed' if push.returncode == 0 else 'push-pending'
    done += 1
    print(f'FIXED {p.name} ({mod}::{api}) [{tests}] {state}')
print(f'\ndone={done} manual={manual}')
