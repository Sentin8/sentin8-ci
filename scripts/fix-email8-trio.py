#!/usr/bin/env python3
"""email8 access/mta/store - same v4/v5 shape with old fn present: surgical
fix using the exact field/print lines found in each."""
import re
import subprocess
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
VERTICALS = ['email8/email8-access', 'email8/email8-mta', 'email8/email8-store']

for rel in VERTICALS:
    p = tree / rel
    regf = p / 'src' / 'registration.rs'
    t = regf.read_text()
    m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}', t)
    if not m:
        print(f'{p.name}: no fn - MANUAL')
        continue
    fnname, api = m.group(1), m.group(2)
    mtxt = (p / 'src' / 'main.rs').read_text()
    if 'OPENAPI_DUMP' not in mtxt:
        mmod = re.search(r'^mod (\w*openapi\w*);', mtxt, re.M)
        if not mmod:
            print(f'{p.name}: no openapi mod - MANUAL')
            continue
        mod = mmod.group(1)
        # OpenApi struct name must match what registration.rs's fn referenced
        mtxt = mtxt.replace(f'mod {mod};',
            f'mod {mod};\n\n'
            '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
            '// name crate::openapi - embedded test harnesses carry no openapi module;\n'
            '// see registration.rs).\n'
            'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
            '    use utoipa::OpenApi as _;\n'
            f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
            '};\n', 1)
    # registration.rs surgery
    t2 = re.sub(rf'\n(fn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n)',
                '\n', t, count=1)
    gate = (
        '    // The utoipa dump is a binary-crate static (main.rs): test harnesses\n'
        '    // embedding this file carry neither the static nor the openapi module\n'
        '    // and never print a spec.\n'
        '    #[cfg(not(test))]\n'
        '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
        '    #[cfg(test)]\n'
        '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n')
    t2 = t2.replace('let reg = Registration {', gate + '\n    let reg = Registration {', 1)
    t2 = re.sub(rf'openapi: {fnname},', 'openapi: dump,', t2)
    t2 = re.sub(rf'print_registration\(section, &reg, {fnname}\)', 'print_registration(section, &reg, dump)', t2)
    if fnname in t2:
        print(f'{p.name}: residual {fnname} - MANUAL')
        continue
    if t2.count('utoipa::OpenApi') == 1 and 'derive(OpenApi)' not in t2 and 'OpenApi::openapi' not in t2:
        t2 = t2.replace('use utoipa::OpenApi;\n', '', 1)
    (p / 'src' / 'main.rs').write_text(mtxt)
    regf.write_text(t2)
    env = {'SQLX_OFFLINE': 'true', **__import__('os').environ}
    rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
    if rb.returncode != 0 or 'warning' in (rb.stderr or ''):
        print(f'FAILED build {p.name}: {(rb.stderr or rb.stdout).strip()[-120:]}')
        subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
        continue
    results = []
    for tname in ('policy', 'handlers_oneshot'):
        tf = p / 'tests' / f'{tname}.rs'
        if tf.is_file():
            rt = subprocess.run(['cargo', 'test', '--test', tname], cwd=p,
                                capture_output=True, text=True, env=env)
            results.append(f'{tname}:{"green" if rt.returncode == 0 else "FAIL"}')
            if rt.returncode != 0:
                print(f'FAILED test {tname} {p.name}: {(rt.stderr or rt.stdout).strip()[:100]}')
                break
    else:
        subprocess.run(['git', '-C', str(p), 'add', '-A'])
        subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                        'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                       capture_output=True)
        push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                              capture_output=True, text=True)
        print(f"FIXED {p.name} [{'pushed' if push.returncode == 0 else 'push-pending'}] {' '.join(results)}")
