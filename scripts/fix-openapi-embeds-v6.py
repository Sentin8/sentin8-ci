#!/usr/bin/env python3
"""v6 - the 4 lib-owned repos: registration.rs lives in the LIB crate (main
thin binary). The dump static goes in lib.rs ONLY (visible to bin + harnesses
that link the lib); embedded harnesses that #[path]-embed registration.rs get
the cfg(test) null; unused import pruned; build + tests + push."""
import re
import subprocess
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
TARGETS = ['openfin8/openfin8-eu', 'openfin8/openfin8-uk',
           'sentin8/sentin8-acme', 'sentin8/sentin8-auth']

for rel in TARGETS:
    p = tree / rel
    regf = p / 'src' / 'registration.rs'
    libf = p / 'src' / 'lib.rs'
    txt = regf.read_text()
    m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}', txt)
    if not m:
        print(f'{p.name}: no fn pattern (already fixed?)')
        continue
    fnname, api = m.group(1), m.group(2)
    mmod = re.search(r'^(?:pub )?mod (\w*openapi\w*);', libf.read_text(), re.M)
    if not mmod:
        print(f'{p.name}: no openapi mod in lib.rs - MANUAL')
        continue
    mod = mmod.group(1)
    # inject into lib.rs
    lt = libf.read_text()
    if 'OPENAPI_DUMP' not in lt:
        lt = lt.replace(f'mod {mod};', f'pub mod {mod};') if f'mod {mod};' in lt else lt
        lt = re.sub(rf'^((?:pub )?mod {mod};)',
            rf'\1\n\n'
            '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
            '// name crate::openapi when #-path-embedded into harness crates; see\n'
            '// registration.rs). The lib owns the openapi module, so this static is\n'
            '// visible to the bin AND to every harness that links the lib.\n'
            'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
            '    use utoipa::OpenApi as _;\n'
            f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
            '};\n', lt, count=1, flags=re.M)
        libf.write_text(lt)
    # registration.rs: drop fn + cfg-gate
    t = re.sub(rf'\n(fn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n)',
               '\n', txt, count=1)
    gate = (
        '    // The utoipa dump is a LIB-owned static (lib.rs): harness crates that\n'
        '    // #path-embed this file without the lib (or without invoking print)\n'
        '    // see neither the static nor the openapi module - they build with null.\n'
        '    #[cfg(not(test))]\n'
        '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
        '    #[cfg(test)]\n'
        '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n')
    t = t.replace('let reg = Registration {', gate + '\n    let reg = Registration {', 1)
    t = re.sub(rf'openapi: {fnname},', 'openapi: dump,', t)
    t = re.sub(rf'print_registration\(section, &reg, {fnname}\)', 'print_registration(section, &reg, dump)', t)
    if fnname in t:
        print(f'{p.name}: residual {fnname} - MANUAL')
        continue
    if t.count('use utoipa::OpenApi;') == 1:
        body = t.split('use utoipa::OpenApi;', 1)[1]
        if not re.search(r'OpenApi as _|\w+::openapi\(\)', body):
            t = t.replace('use utoipa::OpenApi;\n\n', '').replace('use utoipa::OpenApi;\n', '')
    regf.write_text(t)
    env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
    rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
    warns = 'warning' in (rb.stderr or '')
    if rb.returncode != 0:
        print(f'FAILED build {p.name}: {(rb.stderr or rb.stdout).strip()[-160:]}')
        subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
        continue
    rt_ok = True
    for tname in ('handlers_oneshot', 'policy'):
        if (p / 'tests' / f'{tname}.rs').is_file():
            rt = subprocess.run(['cargo', 'test', '--test', tname, '--', '--quiet'],
                                cwd=p, capture_output=True, text=True, env=env)
            if rt.returncode != 0:
                rt_ok = False
                print(f'FAILED test {tname} {p.name}')
                break
    if not rt_ok or warns:
        print(f'{p.name}: builds/tests issue (warns={warns}) - MANUAL')
        subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
        continue
    subprocess.run(['git', '-C', str(p), 'add', '-A'])
    subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                    'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                   capture_output=True)
    push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                          capture_output=True, text=True)
    print(f"FIXED {p.name} [{'pushed' if push.returncode == 0 else 'push-pending'}]")
