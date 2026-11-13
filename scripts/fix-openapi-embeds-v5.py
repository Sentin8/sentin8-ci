#!/usr/bin/env python3
"""v5 - handles the dual-crate (lib.rs + main.rs) ownership: inject the
OPENAPI_DUMP static into BOTH crate roots, cfg-gate the use, prune the unused
utoipa import, verify build + tests, commit + push."""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')


def api_module(p, api):
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
        # already patched? (dump reference w/out the old fn pattern)
        t = regf.read_text()
        m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}', t)
        if not m and 'OPENAPI_DUMP' in t:
            continue  # patched earlier (oms)
        if not m:
            print(f'MANUAL {p.name}: no fn pattern')
            manual += 1
            continue
        fnname, api = m.group(1), m.group(2)
        mtxt = (p / 'src' / 'main.rs').read_text()
        if 'OPENAPI_DUMP' in mtxt:
            continue  # v4 handled the bin-side; the lib crate remains - skip only fully-fixed
        mt_name = re.search(r'^mod (\w*openapi\w*);\s*$', mtxt, re.M)
        root, mod = api_module(p, api)
        if not mod:
            print(f'MANUAL {p.name}: api module not found for {api}')
            manual += 1
            continue
        if not APPLY:
            print(f'would fix {p.name} (fn={fnname} api={api} mod={mod} modroot={root})')
            done += 1
            continue
        # 1. main.rs static (if the crate has a bin root with the mod)
        new_main = mtxt
        if mt_name:
            if 'OPENAPI_DUMP' not in new_main:
                new_main = new_main.replace(f'mod {mod};',
                    f'mod {mod};\n\n'
                    '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
                    '// name crate::openapi - embedded test harnesses have no openapi module;\n'
                    '// see registration.rs and the mirrored static in lib.rs).\n'
                    'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
                    '    use utoipa::OpenApi as _;\n'
                    f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
                    '};\n', 1)
        # 2. lib.rs static (if the crate has a lib owning the mod)
        libf = p / 'src' / 'lib.rs'
        new_lib = libf.read_text() if libf.is_file() else None
        if new_lib is not None and f'pub mod {mod};' in new_lib and 'OPENAPI_DUMP' not in new_lib:
            new_lib = new_lib.replace(f'pub mod {mod};',
                f'pub mod {mod};\n\n'
                '// The utoipa dump for registration::print - mirrored from main.rs; the\n'
                '// lib crate compiles registration::print too (test harnesses embedding\n'
                '// via #[path] never invoke it, so the pointer is referenced, not run).\n'
                'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
                '    use utoipa::OpenApi as _;\n'
                f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
                '};\n', 1)
        # 3. registration.rs: drop the fn; cfg-gate the use; prune unused import
        t = re.sub(rf'\n(fn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n)',
                   '\n', t, count=1)
        gate = (
            '    // The utoipa dump lives OUTSIDE this file (OPENAPI_DUMP static in\n'
            '    // main.rs/lib.rs): test harnesses embedding this file via #[path] carry\n'
            '    // neither the static nor the openapi module and never print a spec.\n'
            '    #[cfg(not(test))]\n'
            '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
            '    #[cfg(test)]\n'
            '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n')
        t = t.replace('let reg = Registration {', gate + '\n    let reg = Registration {', 1)
        t = re.sub(rf'openapi: {fnname},', 'openapi: dump,', t)
        t = re.sub(rf'print_registration\(section, &reg, {fnname}\)', 'print_registration(section, &reg, dump)', t)
        if re.search(rf'\b{fnname}\b', t):
            print(f'MANUAL {p.name}: residual {fnname} refs')
            manual += 1
            continue
        if t.count('use utoipa::OpenApi;') == 1 and 'OpenApi as' not in t.split('use utoipa::OpenApi;')[1][:200]:
            # the file's own import may now be unused - safe prune when nothing else in the file uses the trait
            body = t.split('use utoipa::OpenApi;', 1)[1]
            if not re.search(r'OpenApi as _|\w+::openapi\(\)', body):
                t = t.replace('use utoipa::OpenApi;\n\n', '').replace('use utoipa::OpenApi;\n', '')
        if not APPLY:
            print(f'FIXED-DRY {p.name}')
            done += 1
            continue
        (p / 'src' / 'main.rs').write_text(new_main)
        if new_lib is not None:
            libf.write_text(new_lib)
        regf.write_text(t)
        env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
        rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
        if rb.returncode != 0:
            print(f'FAILED build {p.name}: {(rb.stderr or rb.stdout).strip()[-180:]}')
            manual += 1
            subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        warns = 'warning' in (rb.stderr or '')
        rt_ok = True
        for tname in ('handlers_oneshot', 'policy'):
            tf = p / 'tests' / f'{tname}.rs'
            if tf.is_file():
                rt = subprocess.run(['cargo', 'test', '--test', tname, '--', '--quiet'],
                                    cwd=p, capture_output=True, text=True, env=env)
                if rt.returncode != 0:
                    rt_ok = False
                    print(f'FAILED test {tname} {p.name}: {(rt.stderr or rt.stdout).strip()[:90]}')
                    break
        if not rt_ok or warns:
            manual += 1
            if not rt_ok:
                subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        subprocess.run(['git', '-C', str(p), 'add', '-A'])
        subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                        'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                       capture_output=True)
        push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                              capture_output=True, text=True)
        done += 1
        print(f"FIXED {p.name} [{'pushed' if push.returncode == 0 else 'push-pending'}]")
print(f'\ndone={done} manual={manual}')
