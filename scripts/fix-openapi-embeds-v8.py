#!/usr/bin/env python3
"""Sweep every tagged repo: does registration.rs still reference
crate::openapi or crate::OPENAPI_DUMP without the cfg gate? Fix remaining."""
import re
import subprocess
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
SKIP = {'target', 'node_modules', '.tmp', '.run', 'build'}
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')
gate = (
    '    // The utoipa dump is a binary-crate static (main.rs): test harnesses\n'
    '    // embedding this file carry neither the static nor the openapi module\n'
    '    // and never print a spec.\n'
    '    #[cfg(not(test))]\n'
    '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
    '    #[cfg(test)]\n'
    '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n')
done = manual = 0
for v in VERTICALS:
    d = tree / v
    if not d.is_dir():
        continue
    for p in sorted(d.iterdir()):
        regf = p / 'src' / 'registration.rs'
        if not regf.is_file():
            continue
        t = regf.read_text()
        m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}', t)
        # any direct crate::OPENAPI_DUMP reference in struct field/print w/o a gate
        direct = ('openapi: crate::OPENAPI_DUMP,' in t or 'reg, crate::OPENAPI_DUMP' in t)
        if not m and not direct:
            continue
        if m:
            fnname, api = m.group(1), m.group(2)
            print(f'MANUAL {p.name}: old fn {fnname} still present ({api})')
            manual += 1
            continue
        if 'let dump' in t:
            continue  # already gated; leftover direct refs are inside the gate
        if not direct:
            continue
        if not (p / 'tests').is_dir():
            continue
        if not t.count('let dump'):
            t = t.replace('let reg = Registration {', gate + '\n    let reg = Registration {', 1)
            t = t.replace('openapi: crate::OPENAPI_DUMP,', 'openapi: dump,')
            t = t.replace('print_registration(section, &reg, crate::OPENAPI_DUMP', 'print_registration(section, &reg, dump')
            regf.write_text(t)
            env = {'SQLX_OFFLINE': 'true', **__import__('os').environ}
            rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
            if rb.returncode != 0 or 'warning' in (rb.stderr or ''):
                print(f'FAILED build {p.name}')
                subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
                manual += 1
                continue
            rt_ok = all(
                subprocess.run(['cargo', 'test', '--test', tf.name.replace('.rs',''), '--', '--quiet'],
                               cwd=p, capture_output=True, env=env).returncode == 0
                for tf in (p / 'tests').glob('*.rs'))
            if rt_ok:
                subprocess.run(['git', '-C', str(p), 'add', '-A'])
                subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                                'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                               capture_output=True)
                push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'], capture_output=True, text=True)
                done += 1
                print(f"FIXED {p.name} [{'pushed' if push.returncode == 0 else 'pending'}]")
            else:
                manual += 1
                print(f'FAILED tests {p.name} - reverted')
                subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
print(f'\ndone={done} manual={manual}')
