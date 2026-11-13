#!/usr/bin/env python3
"""Batch v3: handles both main.rs-based and lib.rs-based openapi modules, and
any fn name. Relocates the utoipa dump pointer out of registration.rs, cfg-
gates it for the embedded harnesses, and pushes each repo green."""
import re
import subprocess
import sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
APPLY = '--apply' in sys.argv
VERTICALS = ('exchange8', 'hotel8', 'trade8', 'bank8', 'bill8', 'email8', 'openfin8', 'sentin8')
SKIP_UNFIXABLE = {}


def find_api_module(p, api_struct):
    """Where does the openapi module live: main.rs or lib.rs?"""
    for root in ('main.rs', 'lib.rs'):
        f = p / 'src' / root
        if not f.is_file():
            continue
        for m in re.finditer(r'^(?:pub )?mod (\w*openapi\w*);', f.read_text(), re.M):
            mod = m.group(1)
            sf = p / 'src' / f'{mod}.rs'
            if sf.is_file() and f'pub struct {api_struct};' in sf.read_text():
                return root, mod
    return None, None


done = manual = 0
for v in VERTICALS:
    d = tree / v
    if not d.is_dir():
        continue
    for p in sorted(d.iterdir()):
        f = p / 'src' / 'registration.rs'
        if not f.is_file() or 'crate::openapi' not in f.read_text():
            continue
        txt = f.read_text()
        if 'OPENAPI_DUMP' in (p / 'src' / 'main.rs').read_text():
            continue
        m = re.search(r'fn (\w+)\(\) -> serde_json::Value \{(?:[^}]|\n)*?crate::openapi::(\w+)::openapi\(\)(?:[^}]|\n)*?\}', txt)
        if not m:
            print(f'MANUAL {p.name}: no openapi fn pattern')
            manual += 1
            continue
        fnname, api = m.group(1), m.group(2)
        root, mod = find_api_module(p, api)
        if not mod:
            print(f'MANUAL {p.name}: api module for {api} not found')
            manual += 1
            continue
        rootfile = p / 'src' / root
        rtxt = rootfile.read_text()
        if 'OPENAPI_DUMP' in rtxt:
            continue
        if not APPLY:
            print(f'would fix {p.name} (fn={fnname} api={api} mod={mod} in {root})')
            done += 1
            continue
        # 1. inject OPENAPI_DUMP next to the mod decl (binary crates ONLY -
        #    lib.rs modules cannot hold it since tests embed the file in an
        #    independent harness crate; a lib-declared static IS visible
        #    there, but main-only binaries are not, hence: only main.rs is
        #    safe. If root==lib.rs, register the dump through a test-crate
        #    shim instead.)
        if root == 'main.rs':
            inj = rtxt.replace(f'mod {mod};',
                f'mod {mod};\n\n'
                '// The utoipa dump as a fn pointer for registration.rs (which cannot\n'
                '// name crate::openapi - the embedded test harnesses carry no openapi\n'
                '// module; see registration.rs).\n'
                'static OPENAPI_DUMP: fn() -> serde_json::Value = || {\n'
                '    use utoipa::OpenApi as _;\n'
                f'    serde_json::to_value({mod}::{api}::openapi()).unwrap_or(serde_json::Value::Null)\n'
                '};\n', 1)
            if inj == rtxt:
                print(f'MANUAL {p.name}: main.rs injection failed')
                manual += 1
                continue
            new_reg = re.sub(
                rf'\n(fn {fnname}\(\) -> serde_json::Value \{{(?:[^}}]|\n)*?crate::openapi::{api}::openapi\(\)(?:[^}}]|\n)*?\}}\n)',
                '\n', txt, count=1)
            new_reg = new_reg.replace(f'openapi: {fnname},', 'openapi: crate::OPENAPI_DUMP,')
            new_reg = re.sub(rf'\b{fnname}\b', 'crate::OPENAPI_DUMP', new_reg)
            # cfg-gate: embedded harnesses cannot see binary-crate statics
            new_reg = new_reg.replace(
                'openapi: crate::OPENAPI_DUMP,',
                '// The utoipa dump comes from the BINARY crate (main.rs); tests embedding\n'
                '    // this file never print a spec, so the harness builds with a null dump.\n'
                '    #[cfg(not(test))]\n'
                '    let dump: fn() -> serde_json::Value = crate::OPENAPI_DUMP;\n'
                '    #[cfg(test)]\n'
                '    let dump: fn() -> serde_json::Value = || serde_json::Value::Null;\n'
                '    let reg = Registration {')
            hmm = 'openapi: crate::OPENAPI_DUMP,' in new_reg
            hmm2 = 'print_registration(section, &reg, crate::OPENAPI_DUMP)' in new_reg
            if hmm or hmm2:
                print(f'MANUAL {p.name}: post-substitution leftovers (hmm={hmm} hmm2={hmm2})')
                manual += 1
                continue
            new_reg = re.sub(r'print_registration\(section, &reg, (crate::OPENAPI_DUMP)\)', 'print_registration(section, &reg, dump)', new_reg)
        else:
            # lib-owned module: keep the static in lib.rs (visible to both
            # the bin and the embedded harnesses - the harness must link the
            # lib for this to resolve; verify locally before committing).
            print(f'MANUAL {p.name}: openapi mod is lib-owned ({root}) - needs a local build check')
            manual += 1
            continue
        # write + build + test + commit
        rootfile.write_text(inj)
        f.write_text(new_reg)
        env = {'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)}
        rb = subprocess.run(['cargo', 'build'], cwd=p, capture_output=True, text=True, env=env)
        if rb.returncode != 0:
            print(f'FAILED {p.name}: {rb.stderr.strip()[:110]}')
            manual += 1
            # revert
            subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        ok_t = True
        for t in ['handlers_oneshot', 'policy']:
            if (p / 'tests' / f'{t}.rs').is_file():
                rt = subprocess.run(['cargo', 'test', '--test', t, '--', '--quiet'],
                                    cwd=p, capture_output=True, text=True, env=env)
                if rt.returncode != 0:
                    ok_t = False
                    print(f'FAILED {p.name} (test {t}): {rt.stderr.strip()[:90]}')
                    break
        if not ok_t:
            manual += 1
            subprocess.run(['git', '-C', str(p), 'checkout', '--', 'src/'], capture_output=True)
            continue
        subprocess.run(['git', '-C', str(p), 'add', '-A'])
        subprocess.run(['git', '-C', str(p), 'commit', '-q', '-m',
                        'fix: registration.rs carries no openapi reference in test harnesses (REL-1 release lane)'],
                       capture_output=True)
        push = subprocess.run(['git', '-C', str(p), 'push', 'origin', 'HEAD:main'],
                              capture_output=True, text=True)
        state = 'pushed' if push.returncode == 0 else 'push-pending'
        done += 1
        print(f'FIXED {p.name} [{state}]')
print(f'\ndone={done} manual={manual}')
