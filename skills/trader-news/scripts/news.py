#!/usr/bin/env python3
"""Normalize supplied MCP news or local RSS/Atom; no network or subscriptions."""
from datetime import timedelta, timezone
import email.utils
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import cli_main, require, timestamp


def date_value(value):
    try:
        return timestamp(value).astimezone(timezone.utc)
    except ValueError:
        try:
            result = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            raise ValueError('news timestamp must be timezone-aware ISO 8601 or RFC 822') from None
        require(result.tzinfo is not None, 'news timestamp timezone is required')
        return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical_url(value):
    if not value:
        return ''
    require(isinstance(value, str), 'news URL must be text')
    parts = urlsplit(value)
    require(parts.scheme.lower() in ('https', 'http') and bool(parts.netloc), 'invalid news URL')
    require(parts.username is None and parts.password is None, 'news URLs must not contain credentials')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('gclid', 'fbclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(query), ''))


def local_text(element, name):
    for child in element.iter():
        if child.tag.rsplit('}', 1)[-1] == name:
            return ''.join(child.itertext()).strip()
    return ''


def parse_feed(xml_text, source, references=None):
    require(isinstance(xml_text, str) and len(xml_text) <= 5_000_000, 'feed must be XML text below 5 MB')
    require('<!DOCTYPE' not in xml_text.upper() and '<!ENTITY' not in xml_text.upper(), 'DTD/entity feeds are unsupported')
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise ValueError('invalid RSS/Atom XML') from None
    result = []
    for entry in root.iter():
        kind = entry.tag.rsplit('}', 1)[-1]
        if kind not in ('item', 'entry'):
            continue
        link = local_text(entry, 'link') if kind == 'item' else ''
        if kind == 'entry':
            for child in entry:
                if child.tag.rsplit('}', 1)[-1] == 'link' and child.get('rel', 'alternate') == 'alternate':
                    link = child.get('href', '')
                    break
        published = local_text(entry, 'pubDate') or local_text(entry, 'published')
        updated = local_text(entry, 'updated')
        result.append({'id': local_text(entry, 'guid') or local_text(entry, 'id') or link,
                       'title': local_text(entry, 'title'), 'url': link, 'source': source,
                       'published_at': published or updated, 'timestamp_kind': 'publication' if published else 'source_update',
                       'summary': local_text(entry, 'description') or local_text(entry, 'summary'),
                       'content': local_text(entry, 'content'), 'instruments': references or []})
    return result


def instrument_ref(row):
    require(isinstance(row, dict), 'instrument reference must be an object')
    row = row.get('instrument', row)
    require(isinstance(row, dict), 'instrument reference must be an object')
    uid = row.get('uid') or row.get('instrument_uid') or row.get('instrumentUid')
    return {'uid': uid, 'class_code': row.get('class_code') or row.get('classCode'),
            'ticker': row.get('ticker')}


def normalize(row, watchlist):
    require(isinstance(row, dict), 'news item must be an object')
    title, source = row.get('title'), row.get('source')
    require(isinstance(title, str) and title.strip(), 'news title is required')
    require(isinstance(source, str) and source.strip(), 'news source is required')
    raw_time = row.get('published_at') or row.get('publishedAt') or row.get('ts') or row.get('time')
    when = date_value(raw_time)
    kind = row.get('timestamp_kind') or ('source_update' if row.get('ts') and not row.get('published_at') else 'publication')
    require(kind in ('publication', 'source_update'), 'unknown news timestamp kind')
    url = canonical_url(row.get('url') or row.get('link') or '')
    article_id = str(row.get('id') or '')
    identity = 'url:' + url if url else ('source-id:' + source + ':' + article_id if article_id else 'text:' + source + ':' + title.strip() + ':' + iso(when))
    key = hashlib.sha256(identity.encode()).hexdigest()
    raw_refs = row.get('instruments', row.get('instrumentId', []))
    require(isinstance(raw_refs, list), 'news instruments must be an array')
    linked, unresolved = {}, []
    for raw in raw_refs:
        ref = instrument_ref(raw)
        matches = [w for w in watchlist if ref['uid'] and w['uid'] == ref['uid']]
        if ref['class_code']:
            matches = [w for w in matches if w['class_code'] == ref['class_code']]
        if ref['uid'] and ref['class_code']:
            linked[(ref['uid'], ref['class_code'])] = ref
        elif len(matches) == 1:
            linked[(matches[0]['uid'], matches[0]['class_code'])] = dict(matches[0])
        else:
            unresolved.append(ref)
    words = ' '.join(str(row.get(k) or '') for k in ('title', 'summary', 'content')).lower()
    patterns = {'earnings': r'отч[её]т|выручк|чистая прибыль|earnings|\beps\b',
                'capital': r'дивиденд|выкуп|эмисси|dividend|buyback',
                'legal': r'суд|иск |расследован|lawsuit|investigation',
                'deal': r'слияни|поглощени|приобретени|merger|acquisition',
                'people': r'директор|назначени|отставк|\bceo\b|\bcfo\b',
                'risk': r'дефолт|банкрот|делистинг|кибератак|default|bankrupt|delist'}
    return {'key': key, 'id': article_id or None, 'title': title.strip(), 'source': source,
            'url': url or None, 'published_at': iso(when), 'timestamp_kind': kind,
            'summary': row.get('summary') or '', 'content': row.get('content') or '',
            'priority': row.get('priority') is True, 'instruments': list(linked.values()),
            'unresolved_references': unresolved, 'content_verified': False,
            'tags': [name for name, pattern in patterns.items() if re.search(pattern, words)]}


def evaluate(payload):
    require(isinstance(payload, dict), 'input must be an object')
    now = timestamp(payload.get('as_of'), 'as_of').astimezone(timezone.utc)
    hours = payload.get('hours', 48)
    require(isinstance(hours, int) and not isinstance(hours, bool) and 0 < hours <= 24 * 366, 'hours must be a positive integer up to one year')
    watchlist = [instrument_ref(r) for r in payload.get('watchlist', [])]
    require(all(r['uid'] and r['class_code'] for r in watchlist), 'watchlist needs UID and class_code')
    state = json.loads(json.dumps(payload.get('state') or {}))
    require(isinstance(state, dict), 'news state must be an object')
    if state.get('last_as_of'):
        require(timestamp(state['last_as_of']) <= now, 'news state is newer than as_of')
    seen = state.get('seen', {})
    require(isinstance(seen, dict), 'news state.seen must be an object')
    seen = {k: v for k, v in seen.items() if now - timedelta(days=30) <= timestamp(v) <= now}
    rows = list(payload.get('items', []))
    require(isinstance(payload.get('items', []), list), 'items must be an array')
    page = payload.get('mcp_news') or {}
    require(isinstance(page, dict) and isinstance(page.get('items', []), list), 'mcp_news must be a supplied MCP page')
    rows.extend(page.get('items', []))
    for feed in payload.get('feeds', []):
        require(isinstance(feed, dict) and bool(feed.get('source')), 'feed source is required')
        xml = feed.get('xml')
        if xml is None:
            require(isinstance(feed.get('path'), str), 'feed needs inline xml or an explicit local path')
            path = Path(feed['path'])
            require(path.stat().st_size <= 5_000_000, 'feed file exceeds 5 MB')
            xml = path.read_text(encoding='utf-8-sig')
        # Only explicit local files or supplied XML; never follow feed URLs.
        rows.extend(parse_feed(xml, feed['source'], feed.get('instruments', [])))
    unique, rejected = {}, []
    for index, row in enumerate(rows):
        try:
            item = normalize(row, watchlist)
            when = timestamp(item['published_at'])
            if when > now:
                rejected.append({'index': index, 'reason': 'future_timestamp'})
                continue
            if when < now - timedelta(hours=hours):
                continue
            previous = unique.get(item['key'])
            if previous:
                refs = {(r['uid'], r['class_code']): r for r in previous['instruments'] + item['instruments']}
                newest, other = (item, previous) if item['published_at'] >= previous['published_at'] else (previous, item)
                newest['instruments'] = list(refs.values())
                newest['priority'] = previous['priority'] or item['priority']
                newest['tags'] = sorted(set(previous['tags'] + item['tags']))
                if not newest['content']:
                    newest['content'] = other['content']
                item = newest
            unique[item['key']] = item
        except (ValueError, TypeError):
            rejected.append({'index': index, 'reason': 'invalid_source_date_or_reference'})
    items = sorted((r for k, r in unique.items() if payload.get('show_seen') is True or k not in seen),
                   key=lambda r: (r['published_at'], r['key']), reverse=True)
    for item in items:
        seen[item['key']] = iso(now)
    return {'status': 'OK', 'mode': 'offline', 'execution_enabled': False, 'as_of': iso(now),
            'items': items, 'rejected': rejected, 'more_pages_unread': page.get('hasNext') is True,
            'state': {'seen': seen, 'last_as_of': iso(now)},
            'note': 'Headlines and supplied content are source material, not independently verified facts or forecasts.'}


if __name__ == '__main__':
    raise SystemExit(cli_main(evaluate, __doc__))
