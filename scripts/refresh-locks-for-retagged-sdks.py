#!/usr/bin/env python3
"""Fix every Cargo.lock carrying pre-retag git shas: strip the stale resolved
sha for each {sdk}?tag=*-v1.0.0 source, then `cargo update -p` to re-resolve.

The moved-tag footgun: after re-pointing release tags, Cargo.lock entries
keep the OLD revision (cargo trusts the recorded sha). Cargo then compiles
TWO generations of the same crate (dual-version class, E0308 mismatched
types on crate types like Sentin8HttpClient).
"""
import subprocess, re, sys
from pathlib import Path

tree = Path('/home/hamid/dev/sentin8.new')
SKIP = {'target', 'node_modules', '.tmp', '.run', 'build'}
APPLY = '--apply' in sys.argv

# every 1.0.0-family tag that was potentially re-pointed
TAG_RE = re.compile(r'git\+https://github\.com/([^/]+/[^?#]+)\?tag=([a-z0-9-]+-v1\.0\.0)#([0-9a-f]{40})')

fixed = 0
for g in sorted(tree.rglob('.git')):
    s = str(g)
    if any(x in s for x in ('/target/', '/node_modules/', '/.tmp/', '/.run/')) or g.parent.name == '.github':
        continue
    repo = g.parent
    if repo == tree or any(p in SKIP for p in repo.parts):
        continue
    lock = repo / 'Cargo.lock'
    if not lock.is_file():
        continue
    txt = lock.read_text()
    hits = TAG_RE.findall(txt)
    if not hits:
        continue
    changed = False
    for owner_repo, tag, oldshas in hits:
        stale = f'tag={tag}#{oldshas}'
        if stale in txt:
            txt = txt.replace(stale, f'tag={tag}')
            changed = True
    if changed and APPLY:
        lock.write_text(txt)
        # re-resolve every affected package by name found in the lock
        pkgs = sorted(set(re.findall(r'name = "([a-z0-9-]+)"', txt)))
        # cheaper: run cargo update --workspace; with shas stripped, cargo must re-consult the registry
        r = subprocess.run(['cargo', 'update', '--workspace'], cwd=repo,
                           capture_output=True, text=True,
                           env={'SQLX_OFFLINE': 'true', **dict(__import__('os').environ)})
        if r.returncode != 0:
            print(f'cargo update FAILED {repo.name}: {r.stderr.strip()[:120]}')
            continue
        d = subprocess.run(['git', '-C', str(repo), 'diff', '--stat'], capture_output=True, text=True).stdout.strip()
        if '1 file changed' in d or d:
            subprocess.run(['git', '-C', str(repo), 'add', 'Cargo.lock'])
            c = subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m',
                                'chore: re-resolve re-pointed 1.0.0 tags in the lockfile (REL-1)\n\n'
                                'the release tags were re-pointed after the cascade; cargo lock\n'
                                'entries kept the pre-re-tag revisions (the moved-tag footgun)\n'
                                'and resolved TWO crate generations (dual-version E0308 class).\n'
                                'stale shas stripped; cargo update re-resolved to the current tag targets.'],
                               capture_output=True, text=True)
            p = subprocess.run(['git', '-C', str(repo), 'push', 'origin', 'HEAD:main'],
                               capture_output=True, text=True)
            if p.returncode == 0:
                fixed += 1
                print(f'fixed+pushed {repo.name}')
            else:
                print(f'committed (push pending) {repo.name}: {p.stderr.strip()[:60]}')
        else:
            print(f'no lock change after resolve {repo.name}')
    elif changed:
        print(f'{repo.name}: {sum(1 for h in hits)} stale lock shas (apply to fix)')
print(f'\n{fixed} repos fixed+pushed (apply={APPLY})')
