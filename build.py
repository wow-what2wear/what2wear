"""Pre-fetch all 39 specs from Raider.IO and write static JSON for the frontend.

Usage:
  python build.py                 # build everything (default top=100)
  python build.py --top 500       # full sample
  python build.py --skip-existing # don't refetch specs whose data file is < 24h old
  python build.py --spec mage/frost druid/guardian   # only specific specs
  python build.py --no-names      # skip CN name lookup (just gear data)
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

# ============================================================
# Static metadata (mirror of the frontend CLASSES / SLOT_ORDER)
# ============================================================

CLASSES = [
    ('death-knight', [('blood', 'tank'), ('frost', 'dps'), ('unholy', 'dps')]),
    ('demon-hunter', [('havoc', 'dps'), ('vengeance', 'tank'), ('devourer', 'dps')]),
    ('druid',        [('balance', 'dps'), ('feral', 'dps'), ('guardian', 'tank'), ('restoration', 'healer')]),
    ('evoker',       [('devastation', 'dps'), ('preservation', 'healer'), ('augmentation', 'dps')]),
    ('hunter',       [('beast-mastery', 'dps'), ('marksmanship', 'dps'), ('survival', 'dps')]),
    ('mage',         [('arcane', 'dps'), ('fire', 'dps'), ('frost', 'dps')]),
    ('monk',         [('brewmaster', 'tank'), ('mistweaver', 'healer'), ('windwalker', 'dps')]),
    ('paladin',      [('holy', 'healer'), ('protection', 'tank'), ('retribution', 'dps')]),
    ('priest',       [('discipline', 'healer'), ('holy', 'healer'), ('shadow', 'dps')]),
    ('rogue',        [('assassination', 'dps'), ('outlaw', 'dps'), ('subtlety', 'dps')]),
    ('shaman',       [('elemental', 'dps'), ('enhancement', 'dps'), ('restoration', 'healer')]),
    ('warlock',      [('affliction', 'dps'), ('demonology', 'dps'), ('destruction', 'dps')]),
    ('warrior',      [('arms', 'dps'), ('fury', 'dps'), ('protection', 'tank')]),
]

PHYSICAL_TO_LOGICAL = {
    'head': 'head', 'neck': 'neck', 'shoulder': 'shoulder', 'back': 'back',
    'chest': 'chest', 'wrist': 'wrist', 'hands': 'hands', 'waist': 'waist',
    'legs': 'legs', 'feet': 'feet',
    'finger1': 'fingers', 'finger2': 'fingers',
    'trinket1': 'trinkets', 'trinket2': 'trinkets',
    'mainhand': 'mainhand', 'offhand': 'offhand',
}
LOGICAL_SLOTS = ['head', 'neck', 'shoulder', 'back', 'chest', 'wrist',
                 'hands', 'waist', 'legs', 'feet', 'fingers', 'trinkets',
                 'mainhand', 'offhand']

RAIDERIO_BASE = 'https://raider.io/api'
WOWHEAD_TOOLTIP = 'https://nether.wowhead.com/tooltip/item/{id}?locale=zh'

UA = 'WhatToWear/1.0 (build script; +https://github.com/)'


# ============================================================
# HTTP helpers
# ============================================================

def http_get_json(url, retries=2, timeout=20):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 429:
                    time.sleep(5)
                    continue
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            if e.code == 404:
                return None
            last_err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f'GET {url}: {last_err}')


# ============================================================
# Raider.IO calls
# ============================================================

def fetch_top_characters(class_slug, role, spec_slug, region, season, target, max_pages=15):
    """Page through M+ rankings, filtering to the target spec, until `target` collected."""
    collected = []
    for page in range(max_pages):
        if len(collected) >= target:
            break
        url = (f'{RAIDERIO_BASE}/mythic-plus/rankings/characters'
               f'?region={region}&season={season}&class={class_slug}&role={role}&page={page}')
        data = http_get_json(url)
        rows = (data or {}).get('rankings', {}).get('rankedCharacters', [])
        if not rows:
            break
        for row in rows:
            ch = row.get('character') or {}
            if (ch.get('spec') or {}).get('slug') != spec_slug:
                continue
            collected.append({
                'name': ch.get('name'),
                'realm': (ch.get('realm') or {}).get('slug'),
                'region': (ch.get('region') or {}).get('slug'),
                'rank': row.get('rank'),
                'score': row.get('score'),
            })
            if len(collected) >= target:
                break
    return collected


def fetch_gear(char):
    url = (f'{RAIDERIO_BASE}/v1/characters/profile'
           f'?region={urllib.parse.quote(char["region"])}'
           f'&realm={urllib.parse.quote(char["realm"])}'
           f'&name={urllib.parse.quote(char["name"])}'
           f'&fields=gear')
    data = http_get_json(url, retries=1)
    if not data:
        return None
    return (data.get('gear') or {}).get('items') or None


def fetch_all_gear(characters, concurrency=8):
    gear_list = []
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_gear, c): c for c in characters}
        total = len(futures)
        done = 0
        for f in concurrent.futures.as_completed(futures):
            done += 1
            try:
                items = f.result()
                if items:
                    gear_list.append(items)
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f'    profile failed: {e}', file=sys.stderr)
            if done % 25 == 0 or done == total:
                print(f'    profiles {done}/{total} (failed: {failed})')
    return gear_list, failed


# ============================================================
# Aggregation (mirrors frontend `aggregate()`)
# ============================================================

def variant_hash(bonuses):
    if not bonuses:
        return '—'
    h = 5381
    for b in bonuses:
        h = ((h << 5) + h + b) & 0xFFFFFFFF
    return f'{h:x}'[-3:]


def aggregate(gear_list):
    by_item = {s: {} for s in LOGICAL_SLOTS}
    by_variant = {s: {} for s in LOGICAL_SLOTS}

    for items in gear_list:
        for physical, item in items.items():
            logical = PHYSICAL_TO_LOGICAL.get(physical)
            if not logical or not item or not item.get('item_id'):
                continue
            iid = item['item_id']
            bonuses = sorted(item.get('bonuses') or [])
            ilvl = item.get('item_level') or 0
            name = item.get('name') or f'Item {iid}'
            icon = item.get('icon') or ''

            ikey = str(iid)
            bucket = by_item[logical]
            if ikey in bucket:
                bucket[ikey]['count'] += 1
                if ilvl > bucket[ikey]['item_level']:
                    bucket[ikey]['item_level'] = ilvl
            else:
                bucket[ikey] = {
                    'item_id': iid, 'name': name, 'icon': icon,
                    'item_level': ilvl, 'bonuses': [], 'variant': '', 'count': 1,
                }

            vkey = f'{iid}|{",".join(str(b) for b in bonuses)}'
            vbucket = by_variant[logical]
            if vkey in vbucket:
                vbucket[vkey]['count'] += 1
            else:
                vbucket[vkey] = {
                    'item_id': iid, 'name': name, 'icon': icon,
                    'item_level': ilvl, 'bonuses': bonuses,
                    'variant': variant_hash(bonuses), 'count': 1,
                }

    def sort_desc(d):
        return sorted(d.values(), key=lambda x: -x['count'])

    return {
        'byItem':    {s: sort_desc(by_item[s])    for s in LOGICAL_SLOTS},
        'byVariant': {s: sort_desc(by_variant[s]) for s in LOGICAL_SLOTS},
    }


# ============================================================
# Wowhead CN name lookup
# ============================================================

def fetch_cn_name(item_id):
    url = WOWHEAD_TOOLTIP.format(id=item_id)
    try:
        data = http_get_json(url, retries=1, timeout=15)
        if not data or data.get('error'):
            return None
        return data.get('name')
    except Exception:
        return None


def update_cn_names(item_ids, existing, concurrency=8):
    """Fetch missing CN names. existing: dict item_id_str -> cn name."""
    missing = [i for i in item_ids if str(i) not in existing]
    if not missing:
        return 0
    print(f'  Wowhead CN names: {len(missing)} new lookups')
    new_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_cn_name, i): i for i in missing}
        done = 0
        for f in concurrent.futures.as_completed(futures):
            done += 1
            iid = futures[f]
            try:
                cn = f.result()
            except Exception:
                cn = None
            if cn:
                existing[str(iid)] = cn
                new_count += 1
            if done % 50 == 0 or done == len(missing):
                print(f'    names {done}/{len(missing)} (got: {new_count})')
    return new_count


# ============================================================
# File I/O
# ============================================================

def spec_filename(class_slug, spec_slug):
    return f'{class_slug}__{spec_slug}.json'


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    tmp.replace(path)


def read_json(path):
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'  warning: cannot read {path}: {e}', file=sys.stderr)
        return None


# ============================================================
# Main
# ============================================================

def build_spec(class_slug, spec_slug, role, args, out_dir):
    out_path = out_dir / spec_filename(class_slug, spec_slug)
    if args.skip_existing and out_path.exists():
        existing = read_json(out_path)
        if existing:
            age_h = (time.time() * 1000 - existing.get('fetchedAt', 0)) / 3600000
            if age_h < args.skip_age:
                print(f'  skip (cached {age_h:.1f}h old)')
                return existing

    print(f'  fetching rankings ...')
    characters = fetch_top_characters(
        class_slug=class_slug, role=role, spec_slug=spec_slug,
        region=args.region, season=args.season, target=args.top,
    )
    if not characters:
        print(f'  WARNING: no characters found')
        return None
    print(f'  collected {len(characters)} characters')

    print(f'  fetching profiles ({args.concurrency} concurrent) ...')
    gear_list, failed = fetch_all_gear(characters, concurrency=args.concurrency)
    if not gear_list:
        print(f'  WARNING: all profile fetches failed')
        return None

    aggregated = aggregate(gear_list)
    payload = {
        'classSlug': class_slug,
        'specSlug':  spec_slug,
        'role':      role,
        'region':    args.region,
        'season':    args.season,
        'sampleSize': len(gear_list),
        'sampleRequested': args.top,
        'failed':    failed,
        'fetchedAt': int(time.time() * 1000),
        'byItem':    aggregated['byItem'],
        'byVariant': aggregated['byVariant'],
    }
    write_json(out_path, payload)
    print(f'  wrote {out_path} ({out_path.stat().st_size // 1024} KB)')
    return payload


def collect_all_item_ids(out_dir):
    ids = set()
    for path in out_dir.glob('*__*.json'):
        data = read_json(path)
        if not data:
            continue
        for variant_bucket in data.get('byVariant', {}).values():
            for it in variant_bucket:
                ids.add(it['item_id'])
    return sorted(ids)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--top', type=int, default=100, help='target sample size per spec')
    ap.add_argument('--season', default='season-mn-1')
    ap.add_argument('--region', default='world')
    ap.add_argument('--output', default='data', help='output directory')
    ap.add_argument('--spec', action='append', default=[],
                    help='only build specific specs, e.g. --spec mage/frost --spec druid/guardian')
    ap.add_argument('--skip-existing', action='store_true',
                    help='skip specs whose JSON is fresher than --skip-age hours')
    ap.add_argument('--skip-age', type=float, default=24, help='hours threshold for --skip-existing')
    ap.add_argument('--no-names', action='store_true', help='skip CN name lookup')
    ap.add_argument('--concurrency', type=int, default=8)
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Filter specs if requested
    spec_filter = set(args.spec)
    todo = []
    for cls, specs in CLASSES:
        for spec, role in specs:
            ident = f'{cls}/{spec}'
            if spec_filter and ident not in spec_filter:
                continue
            todo.append((cls, spec, role))

    print(f'Building {len(todo)} spec(s) into {out_dir}/')
    print(f'  region={args.region}  season={args.season}  top={args.top}')

    summary = {
        'season': args.season,
        'region': args.region,
        'sampleRequested': args.top,
        'builtAt': int(time.time() * 1000),
        'specs': [],
    }

    for i, (cls, spec, role) in enumerate(todo, 1):
        print(f'\n[{i}/{len(todo)}] {cls} / {spec} ({role})')
        try:
            payload = build_spec(cls, spec, role, args, out_dir)
            if payload:
                summary['specs'].append({
                    'classSlug': cls, 'specSlug': spec, 'role': role,
                    'file': spec_filename(cls, spec),
                    'sampleSize': payload['sampleSize'],
                    'fetchedAt': payload['fetchedAt'],
                })
        except Exception as e:
            print(f'  ERROR: {e}', file=sys.stderr)

    # Rebuild index.json by scanning data dir, so partial runs (e.g. --spec X)
    # don't shrink the index. Specs not just-built keep their previous entry.
    just_built = {(s['classSlug'], s['specSlug']): s for s in summary['specs']}
    all_specs = []
    for path in sorted(out_dir.glob('*__*.json')):
        cls = path.stem.split('__')[0]
        spec = path.stem.split('__', 1)[1] if '__' in path.stem else ''
        if not spec:
            continue
        if (cls, spec) in just_built:
            all_specs.append(just_built[(cls, spec)])
        else:
            d = read_json(path)
            if d:
                all_specs.append({
                    'classSlug': d.get('classSlug', cls),
                    'specSlug':  d.get('specSlug', spec),
                    'role':      d.get('role', ''),
                    'file':      path.name,
                    'sampleSize': d.get('sampleSize', 0),
                    'fetchedAt': d.get('fetchedAt', 0),
                    'season':    d.get('season', ''),  # so the UI can flag stale-season entries
                })
    summary['specs'] = all_specs
    write_json(out_dir / 'index.json', summary)
    print(f'\nwrote {out_dir / "index.json"} (covers {len(summary["specs"])} specs)')

    # Wowhead CN names
    if not args.no_names:
        names_path = out_dir / 'names.cn.json'
        existing = read_json(names_path) or {}
        all_ids = collect_all_item_ids(out_dir)
        print(f'\nCN name pass: {len(all_ids)} unique item ids across all data')
        new_count = update_cn_names(all_ids, existing, concurrency=args.concurrency)
        write_json(names_path, existing)
        print(f'wrote {names_path} ({len(existing)} total, {new_count} new)')

    print('\nDone.')


if __name__ == '__main__':
    main()
