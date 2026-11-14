#!/usr/bin/env python3
"""v7 - surgical variant for repos where v4/v5 left the OLD fn intact
(empty_openapi/openapi_spec still present + referencing crate::openapi):
delete the fn, cfg-gate the two use sites, inject the static into main.rs
(and lib.rs for dual-crate repos), verify build+tests, commit, push, move tag."""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')


def find_api_mod(p, api_struct):
    for root_name in ('main.rs', 'lib.rs'):
        f = p / 'src' / root_name
        if not f.is_file():
            continue
        for mm in re.finditer(r'^(?:pub )?mod (\w*openapi\w*);', f.read_text(), re.M):
            mod = mm.group(1)
            if (p / 'src' / f'{mod}.rs').is_file() and f'pub struct {api_struct};' in (p / 'src' / f'{mod}.rs').read_text():
                return root_name, mod
    return None, None


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
        if not m:
            continue  # already fixed or different shape
        fnname, api = m.group(1), m.group(2)
        # skip repos with no tests (no harness breaks) - the bin compiles fn fine
        if not (p / 'tests').is_dir():
            continue
        root, mod = find_api_mod(p, api)
        if root != 'main.rs' and root != 'lib.rs':
            print(f'MANUAL {p.name}: mod not found for {api}')
            manual += 1
            continue
        if not APPLY:
            print(f'would fix {p.name} (fn={fnname} api={api} root={root})')
            done += 1
            continue
        mtxt = (p / 'src' / 'main.rs').read_text()
        inj_main = mtxt
        if 'OPENAPI_DUMP' not in mtxt and root == 'main.rs':
            inj_main = mtxt.replace(f'mod {mod};',
                f'mod {mod};\n\n'
                '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
                '// name crate::openapi - embedded test harnesses carry no openapi module;\n'
                '// see registration.rs).\n'
                'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
                '    use utoipa::OpenApi as _;\n'
                f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
                '};\n', 1)
            if inj_main == mtxt:
                print(f'MANUAL {p.name}: injection failed')
                manual += 1
                continue
        elif root == 'lib.rs':
            print(f'MANUAL {p.name}: lib-owned {api} - needs v6-style manual')
            manual += 1
            continue
        # registration.rs surgery
        t2 = re.sub(rf'\nfn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n',
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
            print(f'MANUAL {p.name}: residual refs')
            manual += 1
            continue
        # dual-crate: mirror into lib.rs when the lib owns registration
        libf = p / 'src' / 'lib.rs'
        new_lib = None
        if libf.is_file() and 'pub mod registration;' in libf.read_text() and 'OPENAPI_DUMP' not in libf.read_text():
            lt = libf.read_text()
            mmod = re.search(r'^(?:pub )?mod (\w*openapi\w*);', lt, re.M)
            if mmod:
                lmod = mmod.group(1)
                if f'pub struct {api};' in (p / 'src' / f'{lmod}.rs').read_text():
                    new_lib = lt.replace(mmod.group(0),
                        mmod.group(0) + '\n\n'
                        '// The utoipa dump for registration::print - mirrored from main.rs; the\n'
                        '// lib crate compiles registration::print too (harnesses embedding via\n'
                        '// #[path] never invoke it, so the pointer is referenced, not run).\n'
                        'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
                        '    use utoipa::OpenApi as _;\n'
                        f'    serde_json::to_value({lmod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
                        '};\n', 1)
        (p / 'src' / 'main.rs').write_text(inj_main)
        if new_lib:
            libf.write_text(new_lib)
        regf.write_text(t2)
        env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
        rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
        if rb.returncode != 0 or 'warning' in (rb.stderr or ''):
            print(f'FAILED build {p.name}: {(rb.stderr or rb.stdout).strip()[-140:]}')
            manual += 1
            subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        rt_ok = True
        for tname in ('handlers_oneshot', 'policy'):
            if (p / 'tests' / f'{tname}.rs').is_file():
                rt = subprocess.run(['cargo', 'test', '--test', tname, '--', '--quiet'],
                                    cwd=p, capture_output=True, text=True, env=env)
                if rt.returncode != 0:
                    rt_ok = False
                    print(f'FAILED test {tname} {p.name}: {(rt.stderr or rt.stdout).strip()[:90]}')
                    break
        if not rt_ok:
            manual += 1
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
