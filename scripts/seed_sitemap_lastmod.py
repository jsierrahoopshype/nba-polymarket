"""
One-time seed for data/sitemap_lastmod.json: give every sitemap URL the date of
the last commit that changed its page, instead of the first build's time.

Needs full git history (`git fetch --unshallow --filter=blob:none origin main`;
CI's shallow checkout is not enough, which is why this is not part of the poll).

Run:  python scripts/build_entities.py      # writes the state with every key
      python scripts/seed_sitemap_lastmod.py
      python scripts/build_entities.py      # re-renders sitemap.xml from it
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_entities as be  # noqa: E402
import sitemap_lastmod as slm  # noqa: E402


def git(*args):
    return subprocess.run(["git", *args], cwd=be.REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout


def main():
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        sys.exit("shallow clone: run `git fetch --unshallow --filter=blob:none origin main` first")
    state = slm.load_state(be.SITEMAP_STATE_PATH)
    if not state:
        sys.exit("no state yet: run build_entities.py first")

    # newest-first walk: the first time a path shows up is its last change
    last_change, when = {}, None
    for line in git("log", "--format=@%ct", "--name-only", "HEAD", "--", "docs").splitlines():
        if line.startswith("@"):
            when = datetime.fromtimestamp(int(line[1:]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif line and line not in last_change:
            last_change[line] = when
    dirty = set(git("diff", "--name-only", "HEAD", "--", "docs").split())

    seeded = kept = 0
    for key, (h, lm) in state.items():
        rel = be.key_to_file(key).relative_to(be.REPO_ROOT).as_posix()
        if rel in last_change and rel not in dirty and h == slm.content_hash(be.key_to_file(key).read_bytes()):
            state[key] = [h, last_change[rel]]
            seeded += 1
        else:
            kept += 1
    be.SITEMAP_STATE_PATH.write_text(slm.dump_state(state), encoding="utf-8")
    print(f"seeded {seeded} URLs from git history, {kept} kept at build time")


if __name__ == "__main__":
    main()
