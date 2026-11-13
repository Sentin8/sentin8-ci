#!/usr/bin/env python3
"""Batch v4 - simplest correct form per repo:
1. Inject OPENAPI_DUMP into main.rs next to `mod {mod};` (binary static).
2. In registration.rs: delete the whole `fn {name}() -> serde_json::Value...` block.
3. Replace BOTH usages (`openapi: {name},` and `, {name})`) with cfg-gated
   local `dump`:
     #[cfg(not(test))] let dump = crate::OPENAPI_DUMP;
     #[cfg(test)] let dump: fn()->Value = || Null;
   and pass `dump` to both the field and print_registration.
Local build + embedded tests gate every commit."""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')


def api_module_root(p, api):
    for root_name in ('main.rs', 'lib.rs'):
        f = p / 'src' / root_name
        if not f.is_file():
            continue
        for m in re.finditer(r'^(?:pub )?mod (\w*openapi\w*);', f.read_text(), re.M):
            mod = m.group(1)
            if (p / 'src' / f'{mod}.rs').is_file() and f'pub struct {api};' in (p / 'src' / f'{mod}.rs').read_text():
                return root_name, mod
    return None, None


done = manual = 0
for v in VERTICALS:
    d = tree / v
    if not d.is_dir():
        continue
    for p in sorted(d.iterdir()):
        regf = p / 'src' / 'registration.rs'
        if not regf.is_file() or 'crate::openapi' not in regf.read_text():
            continue
        mtxt = (p / 'src' / 'main.rs').read_text()
        if 'OPENAPI_DUMP' in mtxt:
            continue
        m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}',
                      regf.read_text())
        if not m:
            print(f'MANUAL {p.name}: no fn pattern')
            manual += 1
            continue
        fnname, api = m.group(1), m.group(2)
        root, mod = api_module_root(p, api)
        if root != 'main.rs':
            print(f'MANUAL {p.name}: openapi module not in main.rs (root={root})')
            manual += 1
            continue
        if not APPLY:
            print(f'would fix {p.name} (fn={fnname} api={api} mod={mod})')
            done += 1
            continue
        inj = mtxt.replace(f'mod {mod};',
            f'mod {mod};\n\n'
            '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
            '// name crate::openapi - tests embed registration.rs into harness crates\n'
            '// with no openapi module; see registration.rs).\n'
            'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
            '    use utoipa::OpenApi as _;\n'
            f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
            '};\n', 1)
        if inj == mtxt:
            print(f'MANUAL {p.name}: injection failed')
            manual += 1
            continue
        t = regf.read_text()
        t = re.sub(rf'\nfn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n',
                   '\n', t, count=1)
        # cfg gate + both usage rewrites
        gate = (
            '    // The utoipa dump is a BINARY-crate static (main.rs): the embedded test\n'
            '    // harnesses that compose this file cannot see it and never print a spec.\n'
            '    #[cfg(not(test))]\n'
            '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
            '    #[cfg(test)]\n'
            '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n')
        t = t.replace('let reg = Registration {', gate + '\n    let reg = Registration {', 1)
        t = re.sub(rf'openapi: {fnname},', 'openapi: dump,', t)
        t = re.sub(rf'print_registration\(section, &reg, {fnname}\)', 'print_registration(section, &reg, dump)', t)
        if fnname in t:
            print(f'MANUAL {p.name}: residual {fnname} refs')
            manual += 1
            continue
        (p / 'src' / 'main.rs').write_text(inj)
        regf.write_text(t)
        env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
        rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
        if rb.returncode != 0:
            print(f'FAILED build {p.name}: {(rb.stderr or rb.stdout).strip()[:100]}')
            manual += 1
            subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        for tname in ('handlers_oneshot', 'policy'):
            tf = p / 'tests' / f'{tname}.rs'
            if tf.is_file():
                rt = subprocess.run(['cargo', 'test', '--test', tname, '--', '--quiet'],
                                    cwd=p, capture_output=True, text=True, env=env)
                if rt.returncode != 0:
                    print(f'FAILED test {tname} in {p.name}: {(rt.stderr or rt.stdout).strip()[:90]}')
                    manual += 1
                    subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
                    break
        else:
            subprocess.run(['git', '-C', str(p), 'add', '-A'])
            subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                            'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                           capture_output=True)
            push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                                  capture_output=True, text=True)
            done += 1
            print(f"FIXED {p.name} [{'pushed' if push.returncode == 0 else 'push-pending'}]")
print(f'\ndone={done} manual={manual}')
