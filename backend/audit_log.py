"""
An audit trail that lives in a file, because the database is not ours to
change.

    import audit_log
    audit_log.register(app, bot)        # two lines, at the very bottom of main2.py

WHAT IT RECORDS
    Every attempt to change the ledger, whether it succeeded or not:

        voucher.create   voucher.update   voucher.void
        party.create     party.update
        account.create   account.rename   account.activate / deactivate

    A refusal and a crash are recorded exactly as carefully as a success.
    That is the half people leave out, and it is the half that answers the
    question actually asked six weeks later - "I posted that, why isn't it
    there?" - which a log of successes cannot answer even in principle. The
    1062 duplicate-id failures you have been seeing land in here as
    `outcome: "error"` with the full reason.

    Updates carry BEFORE and AFTER. A trail that says "amount changed" and
    not what it changed from is not a trail.

WHY JSON LINES AND NOT CSV
    One event per line, appended, never rewritten. A crash mid-write can
    corrupt one line and nothing else, and `verify()` finds that line. CSV
    cannot hold the before/after pair without flattening it into columns
    that go stale the moment a field is added.

WHY THE HASH CHAIN
    A file anyone can open in Notepad is not evidence unless editing it
    shows. Every record carries `prev`, the hash of the record before it,
    and its own `hash` over (prev + its content). Change one number in a
    line from last month and every hash after it stops matching - so
    tampering is not prevented (nothing in a file can prevent it) but it
    cannot be made to pass unnoticed. GET /api/audit/verify walks the chain
    and names the first line that breaks.

    The chain continues across day files: the first record of a new day
    carries the last hash of the previous one, so removing a whole day is
    visible too.

FILES
    audit/audit-2026-09-18.jsonl        one per day, created on first write
    The directory is created if it does not exist. Nothing is ever deleted
    or rewritten by this module.

SETTINGS (.env)
    AUDIT_DIR=audit                 where the files go
    AUDIT_ENABLED=1                 0 turns the whole thing off
    AUDIT_FSYNC=1                   force to disk on every write; 0 is faster
                                    and loses the tail on a hard power cut
    AUDIT_REQUIRED=0                1 makes a write that cannot be audited
                                    FAIL the operation. Default is 0 so a
                                    full disk cannot stop bookkeeping, but
                                    for a real audit obligation set it to 1
                                    and mean it.
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import threading
import traceback
from contextvars import ContextVar
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

AUDIT_DIR = os.getenv("AUDIT_DIR", "audit")
AUDIT_ENABLED = os.getenv("AUDIT_ENABLED", "1") == "1"
AUDIT_FSYNC = os.getenv("AUDIT_FSYNC", "1") == "1"
AUDIT_REQUIRED = os.getenv("AUDIT_REQUIRED", "0") == "1"

GENESIS = "0" * 64

_lock = threading.Lock()
_chain: Dict[str, Any] = {"seq": 0, "hash": GENESIS, "loaded": False}

# Who is doing this, and under which request. Set by the wrappers below and
# by the HTTP middleware, so an event knows its session without every
# function in the app having to pass one down.
_ctx: ContextVar[Dict[str, Any]] = ContextVar("audit_ctx", default={})

_M = None            # the host module (main2 / main_v6), found at register()


# ==========================================================================
# The file
# ==========================================================================
def _ensure_dir() -> None:
    os.makedirs(AUDIT_DIR, exist_ok=True)


def _day_path(d: Optional[str] = None) -> str:
    d = d or datetime.now().strftime('%Y-%m-%d')
    return os.path.join(AUDIT_DIR, f"audit-{d}.jsonl")


def _day_files() -> List[str]:
    """Every audit file, oldest first. The name carries the date, so sorting
    the names sorts the days - that is why the date is in the filename in
    that order and not as 18-09-2026."""
    if not os.path.isdir(AUDIT_DIR):
        return []
    names = [n for n in os.listdir(AUDIT_DIR)
             if n.startswith('audit-') and n.endswith('.jsonl')]
    return [os.path.join(AUDIT_DIR, n) for n in sorted(names)]


def _canonical(obj: Any) -> str:
    """One byte-for-byte spelling of a record, so the same content always
    hashes the same. sort_keys is the whole point: Python dict order must not
    be able to change a hash."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, default=str)


def _digest(prev: str, body: Dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(body)).encode('utf-8')).hexdigest()


def _last_record() -> Optional[Dict[str, Any]]:
    """The newest record on disk, for picking the chain back up on restart.

    Read from the newest file backwards, because the last line is sometimes
    a half-written one from a process that was killed mid-append; the record
    before it is still good, and the chain should continue from whatever the
    last COMPLETE record was. verify() will still report the broken line.
    """
    for path in reversed(_day_files()):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and 'hash' in rec and 'seq' in rec:
                return rec
    return None


def _load_chain() -> None:
    """Where the chain left off. Taken from the files themselves, never from
    a side-car state file - a state file is one more thing that can be edited
    to make a doctored log look consistent."""
    if _chain["loaded"]:
        return
    last = _last_record()
    if last:
        _chain["seq"] = int(last.get("seq") or 0)
        _chain["hash"] = str(last.get("hash") or GENESIS)
    _chain["loaded"] = True


# ==========================================================================
# Writing one event
# ==========================================================================
def record(action: str, *, outcome: str = 'ok', target: Any = None,
           before: Optional[Dict] = None, after: Optional[Dict] = None,
           detail: Optional[Dict] = None, error: Optional[str] = None
           ) -> Optional[Dict[str, Any]]:
    """
    Append one event. Returns the written record, or None if auditing is off.

    Never raises unless AUDIT_REQUIRED=1, in which case a failure to write
    the trail is raised to the caller and the operation it was recording
    fails with it - which is the correct behaviour when an audit trail is an
    obligation rather than a convenience.
    """
    if not AUDIT_ENABLED:
        return None

    ctx = _ctx.get() or {}
    now = datetime.now()
    body = {
        'ts': now.isoformat(timespec='milliseconds'),
        'action': action,
        'outcome': outcome,                       # ok | refused | error
        'target': str(target) if target is not None else None,
        'actor': ctx.get('actor') or getattr(_M, 'USER_ID', None),
        'system_id': getattr(_M, 'SYSTEM_ID', None),
        'session_id': ctx.get('session_id'),
        'request_id': ctx.get('request_id'),
        'ip': ctx.get('ip'),
        'route': ctx.get('route'),
        'source_message': ctx.get('message'),
        'before': _clean(before, keep_none=True),
        'after': _clean(after, keep_none=True),
        'changed': _diff(before, after),
        'detail': _clean(detail),
        'error': error,
    }

    try:
        with _lock:
            _load_chain()
            _ensure_dir()
            seq = _chain["seq"] + 1
            prev = _chain["hash"]
            body_with_seq = {**body, 'seq': seq, 'prev': prev}
            h = _digest(prev, body_with_seq)
            rec = {**body_with_seq, 'hash': h}

            path = _day_path(now.strftime('%Y-%m-%d'))
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(_canonical(rec) + "\n")
                fh.flush()
                if AUDIT_FSYNC:
                    os.fsync(fh.fileno())

            _chain["seq"], _chain["hash"] = seq, h
            return rec
    except Exception as e:
        # The trail failing must be loud. Whether it is also fatal is a
        # policy question, and the policy is a setting.
        print(f"AUDIT: could not write {action!r} ({type(e).__name__}: {e})")
        traceback.print_exc()
        if AUDIT_REQUIRED:
            raise RuntimeError(
                "The audit trail could not be written, so this was not saved. "
                "Check that the server can write to the audit folder, then "
                "try again."
            ) from e
        return None


_MAX_FIELD = 2000


def _clean(d: Optional[Dict], keep_none: bool = False) -> Optional[Dict]:
    """
    Plain JSON-able values, and nothing enormous. A 400-page statement
    description in an audit record makes the file unreadable and tells you
    nothing the first 2000 characters didn't.

    keep_none is on for before/after snapshots and off everywhere else: a
    field that was cleared - a cheque number removed - has to survive as an
    explicit null, or clearing a field would read as not touching it. In a
    `detail` block a null is just noise, so there it goes.
    """
    if not d:
        return None
    out = {}
    for k, v in d.items():
        if v is None:
            if keep_none:
                out[k] = None
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v[:_MAX_FIELD] if isinstance(v, str) and len(v) > _MAX_FIELD else v
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            s = str(v)
            out[k] = s[:_MAX_FIELD]
    return out or None


def _diff(before: Optional[Dict], after: Optional[Dict]) -> Optional[Dict]:
    """
    Only what actually moved. An update that names six fields and changes one
    should read as one change, or nobody will read it at all.

    Compared over the fields the two snapshots SHARE, never their union. An
    after-state that carries fewer fields than the before-state - a rename
    returning just the code and the new name - is not a claim that everything
    else became null, and the union spelling reported exactly that:
    "level: main -> None" on every chart edit, which is both wrong and the
    kind of noise that teaches people to skip the column.
    """
    if not before or not after:
        return None
    changed = {}
    for k in sorted(set(before) & set(after)):
        b, a = before.get(k), after.get(k)
        if _same(b, a):
            continue
        changed[k] = {'from': _scalar(b), 'to': _scalar(a)}
    return changed or None


def _same(b, a) -> bool:
    if b == a:
        return True
    try:                                   # 450 and 450.00 are the same amount
        return abs(float(b) - float(a)) < 0.005
    except (TypeError, ValueError):
        return str(b or '') == str(a or '')


def _scalar(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ==========================================================================
# Reading it back
# ==========================================================================
def read_events(*, limit: int = 200, at_id: Optional[str] = None,
                action: Optional[str] = None, outcome: Optional[str] = None,
                day: Optional[str] = None, session_id: Optional[str] = None,
                contains: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first. Reads the files backwards so a common question - what
    happened in the last hour - does not read a year of history first."""
    want = []
    files = _day_files()
    if day:
        files = [p for p in files if p.endswith(f"audit-{day}.jsonl")]
    for path in reversed(files):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                want.append({'_unreadable': line[:200], 'ts': None})
                continue
            if at_id and str(rec.get('target') or '') != str(at_id):
                continue
            if action and not str(rec.get('action') or '').startswith(action):
                continue
            if outcome and rec.get('outcome') != outcome:
                continue
            if session_id and rec.get('session_id') != session_id:
                continue
            if contains and contains.lower() not in _canonical(rec).lower():
                continue
            want.append(rec)
            if len(want) >= limit:
                return want
    return want


def verify(day: Optional[str] = None) -> Dict[str, Any]:
    """
    Recompute every hash from the beginning and report the first break.

    A clean result means: no line has been edited, no line has been removed,
    and no line has been inserted, since it was written. It does NOT mean the
    events themselves were correct - only that the file still says what it
    said.
    """
    files = _day_files()
    if day:
        # Verifying one day alone cannot check that day's link to the day
        # before it, so say so rather than implying a stronger result.
        files = [p for p in files if p.endswith(f"audit-{day}.jsonl")]
    prev = GENESIS
    seq = 0
    checked = 0
    problems: List[Dict[str, Any]] = []
    first_file = True

    for path in files:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        except OSError as e:
            problems.append({'file': path, 'problem': f'unreadable ({e})'})
            continue
        for n, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                problems.append({'file': os.path.basename(path), 'line': n,
                                 'problem': 'not valid JSON - the line is '
                                            'corrupt or was edited'})
                continue
            checked += 1
            body = {k: v for k, v in rec.items() if k != 'hash'}
            expect = _digest(rec.get('prev') or '', body)
            if rec.get('hash') != expect:
                problems.append({
                    'file': os.path.basename(path), 'line': n,
                    'seq': rec.get('seq'), 'ts': rec.get('ts'),
                    'action': rec.get('action'),
                    'problem': 'the content does not match its own hash - '
                               'this record was changed after it was written'})
            elif day and first_file and n == 1:
                pass                     # can't check the link to the day before
            elif rec.get('prev') != prev:
                problems.append({
                    'file': os.path.basename(path), 'line': n,
                    'seq': rec.get('seq'), 'ts': rec.get('ts'),
                    'problem': 'the chain is broken here - a record before '
                               'this one was removed or replaced'})
            if rec.get('seq') != seq + 1 and not (day and first_file and n == 1):
                problems.append({
                    'file': os.path.basename(path), 'line': n,
                    'problem': f"numbering jumps from {seq} to {rec.get('seq')}"})
            seq = int(rec.get('seq') or seq + 1)
            prev = rec.get('hash') or prev
        first_file = False

    return {
        'ok': not problems,
        'records_checked': checked,
        'files': [os.path.basename(p) for p in files],
        'scope': f"day {day}" if day else 'the whole trail',
        'problems': problems[:50],
        'summary': ("Intact - every record still hashes to what it did when "
                    "it was written." if not problems else
                    f"{len(problems)} problem(s). The audit trail has been "
                    f"altered, truncated, or damaged since it was written."),
    }


def to_csv(day: Optional[str] = None, limit: int = 5000) -> str:
    """The trail as a spreadsheet, for handing to somebody who will not open
    a .jsonl. The hash columns come along - without them it is a report, not
    a trail."""
    rows = read_events(limit=limit, day=day)
    rows.reverse()                                    # oldest first, to read
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['seq', 'timestamp', 'action', 'outcome', 'target', 'actor',
                'session_id', 'changed', 'detail', 'error', 'hash'])
    for r in rows:
        changed = r.get('changed') or {}
        w.writerow([
            r.get('seq'), r.get('ts'), r.get('action'), r.get('outcome'),
            r.get('target'), r.get('actor'), r.get('session_id'),
            '; '.join(f"{k}: {v.get('from')} -> {v.get('to')}"
                      for k, v in changed.items()) if changed else '',
            _canonical(r.get('detail')) if r.get('detail') else '',
            r.get('error') or '', (r.get('hash') or '')[:16],
        ])
    return buf.getvalue()


# ==========================================================================
# Snapshots - the "before" half of a trail
# ==========================================================================
_VOUCHER_FIELDS = ('at_id', 'entry_type', 'amount', 'transaction_date',
                   'party_code', 'party_name', 'bank_acc_code', 'bank_account',
                   'category_acc_code', 'category_account', 'cheque_no',
                   'description', 'at_status')

_PARTY_FIELDS = ('p_code', 'p_type', 'company_name', 'person_name', 'email',
                 'phone', 'fax', 'address', 'city', 'state', 'zipcode',
                 'sale_tax_no', 'fedral_id_no', 'job_title', 'p_account',
                 'status')


def _snap(d: Optional[Dict], fields) -> Optional[Dict]:
    """Every field in the list, present or not. Dropping the empty ones would
    make before and after different shapes, and a field that goes from a value
    to empty - a cheque number cleared - would then vanish from the diff
    instead of appearing in it."""
    if not isinstance(d, dict):
        return None
    return {k: d.get(k) for k in fields}


def _voucher_snapshot(conn, at_id) -> Optional[Dict]:
    """What a voucher looks like right now. Wrapped in its own try: a trail
    that cannot read the before-state should still record the after-state,
    rather than losing the event entirely."""
    try:
        v = _M.fetch_voucher(conn, at_id)
        return _snap(v, _VOUCHER_FIELDS) if v.get('found') else None
    except Exception:
        return None


def _party_snapshot(conn, p_code) -> Optional[Dict]:
    try:
        p = _M.fetch_party(conn, p_code)
        return _snap(p, _PARTY_FIELDS) if p.get('found') else None
    except Exception:
        return None


def _account_snapshot(conn, code) -> Optional[Dict]:
    try:
        row = _M.find_account(conn, code)
        if row:
            return {'code': row['code'], 'name': row['desc'],
                    'qualified': row['qualified'], 'level': row['level']}
        tree = _M.build_chart_tree(conn)
        for n in tree:
            for m in n['mains']:
                if m['code'] == str(code):
                    return {'code': m['code'], 'name': m['name'],
                            'level': 'main', 'postable': m['postable']}
                for s in m['subs']:
                    if s['code'] == str(code):
                        return {'code': s['code'], 'name': s['name'],
                                'level': 'sub'}
    except Exception:
        pass
    return None


# ==========================================================================
# The wrappers
#
# Every one has the same shape: take the before-state, call the real
# function, record what happened. A refusal (ValueError - a message written
# for the operator) and a crash (anything else) are told apart, because
# "you can't edit a reconciled voucher" and "the database fell over" are not
# the same entry in a trail.
# ==========================================================================
def _wrap(name: str, factory) -> None:
    original = getattr(_M, name, None)
    if original is None:
        print(f"AUDIT: {name}() isn't in this build - skipping it")
        return
    if getattr(original, '_audited', False):
        return                                        # register() ran twice
    wrapped = factory(original)
    wrapped._audited = True
    wrapped.__name__ = getattr(original, '__name__', name)
    wrapped.__doc__ = getattr(original, '__doc__', None)
    setattr(_M, name, wrapped)


def _outcome_of(e: Exception) -> str:
    return 'refused' if isinstance(e, ValueError) else 'error'


def _install_wrappers() -> None:

    # ---- vouchers -------------------------------------------------------
    def voucher_create(orig):
        def f(conn, entry_type, party_code, bank_res, cat_res, amount,
              trans_date, description, cheque_no=None):
            detail = {
                'entry_type': entry_type, 'amount': amount,
                'transaction_date': trans_date, 'party_code': party_code,
                'bank_acc_code': getattr(bank_res, 'code', None),
                'bank_account': getattr(bank_res, 'qualified', None),
                'bank_match': getattr(bank_res, 'match_type', None),
                'category_acc_code': getattr(cat_res, 'code', None),
                'category_account': getattr(cat_res, 'qualified', None),
                'category_match': getattr(cat_res, 'match_type', None),
                'cheque_no': cheque_no, 'description': description,
            }
            try:
                new_id = orig(conn, entry_type, party_code, bank_res, cat_res,
                              amount, trans_date, description, cheque_no)
            except Exception as e:
                record('voucher.create', outcome=_outcome_of(e), detail=detail,
                       error=f"{type(e).__name__}: {e}")
                raise
            record('voucher.create', target=new_id,
                   after={**detail, 'at_id': new_id,
                          'voucher_number': f"{entry_type}-{new_id}"},
                   detail=detail)
            return new_id
        return f

    def voucher_update(orig):
        def f(conn, at_id, upd):
            before = _voucher_snapshot(conn, at_id)
            try:
                out = orig(conn, at_id, upd)
            except Exception as e:
                record('voucher.update', target=at_id, outcome=_outcome_of(e),
                       before=before, error=f"{type(e).__name__}: {e}")
                raise
            record('voucher.update', target=at_id, before=before,
                   after=_snap(out, _VOUCHER_FIELDS))
            return out
        return f

    def voucher_void(orig):
        def f(conn, at_id):
            before = _voucher_snapshot(conn, at_id)
            try:
                out = orig(conn, at_id)
            except Exception as e:
                record('voucher.void', target=at_id, outcome=_outcome_of(e),
                       before=before, error=f"{type(e).__name__}: {e}")
                raise
            record('voucher.void', target=at_id, before=before,
                   after=_snap(out, _VOUCHER_FIELDS))
            return out
        return f

    _wrap('insert_voucher', voucher_create)
    _wrap('update_voucher', voucher_update)
    _wrap('void_voucher', voucher_void)

    # ---- parties --------------------------------------------------------
    def party_create_quick(orig):
        # create_party() is the one that fires mid-sentence while posting a
        # voucher, so it is where a profile nobody meant to make comes from.
        def f(conn, name, p_type):
            try:
                code, display = orig(conn, name, p_type)
            except Exception as e:
                record('party.create', outcome=_outcome_of(e),
                       detail={'name': name, 'p_type': p_type},
                       error=f"{type(e).__name__}: {e}")
                raise
            record('party.create', target=code,
                   after={'p_code': code, 'p_type': p_type,
                          'company_name': display},
                   detail={'asked_for': name, 'via': 'while posting'})
            return code, display
        return f

    def party_create_full(orig):
        def f(conn, data):
            try:
                r = orig(conn, data)
            except Exception as e:
                record('party.create', outcome=_outcome_of(e),
                       detail={'company_name': getattr(data, 'company_name', None),
                               'p_type': getattr(data, 'p_type', None)},
                       error=f"{type(e).__name__}: {e}")
                raise
            # A reused profile is not a created one, and the trail should not
            # claim it was.
            record('party.create' if r.get('created') else 'party.reuse',
                   target=r.get('p_code'),
                   after={'p_code': r.get('p_code'), 'p_type': r.get('p_type'),
                          'company_name': r.get('company_name'),
                          'person_name': r.get('person_name'),
                          'email': getattr(data, 'email', None),
                          'phone': getattr(data, 'phone', None),
                          'address': getattr(data, 'address', None)},
                   detail={'via': 'reviewed draft'})
            return r
        return f

    def party_update(orig):
        def f(conn, p_code, changes):
            before = _party_snapshot(conn, p_code)
            try:
                out = orig(conn, p_code, changes)
            except Exception as e:
                record('party.update', target=p_code, outcome=_outcome_of(e),
                       before=before,
                       detail={'requested': ', '.join(sorted(changes or {}))},
                       error=f"{type(e).__name__}: {e}")
                raise
            record('party.update', target=p_code, before=before,
                   after=_snap(out, _PARTY_FIELDS))
            return out
        return f

    _wrap('create_party', party_create_quick)
    _wrap('create_party_full', party_create_full)
    _wrap('update_party', party_update)

    # ---- chart of accounts ----------------------------------------------
    def account_create(orig):
        def f(conn, level, name, parent_code):
            detail = {'level': level, 'name': name, 'parent_code': parent_code}
            try:
                r = orig(conn, level, name, parent_code)
            except Exception as e:
                record('account.create', outcome=_outcome_of(e), detail=detail,
                       error=f"{type(e).__name__}: {e}")
                raise
            record('account.create', target=r.get('code'),
                   after={'code': r.get('code'), 'name': r.get('name'),
                          'level': level, 'parent_code': parent_code,
                          'parent_name': r.get('parent_name')},
                   detail=detail)
            return r
        return f

    def account_rename(orig):
        def f(conn, code, new_name):
            before = _account_snapshot(conn, code)
            try:
                r = orig(conn, code, new_name)
            except Exception as e:
                record('account.rename', target=code, outcome=_outcome_of(e),
                       before=before, detail={'new_name': new_name},
                       error=f"{type(e).__name__}: {e}")
                raise
            # A rename reaches every tenant sharing the code. The trail says
            # so, because that is the fact somebody will want back.
            record('account.rename', target=code, before=before,
                   after={'code': r.get('code'), 'name': r.get('name')},
                   detail={'note': 'the name is shared - this changes it for '
                                   'every company using this code'})
            return r
        return f

    def account_active(orig):
        def f(conn, code, active):
            before = _account_snapshot(conn, code)
            try:
                r = orig(conn, code, active)
            except Exception as e:
                record('account.activate' if active else 'account.deactivate',
                       target=code, outcome=_outcome_of(e), before=before,
                       error=f"{type(e).__name__}: {e}")
                raise
            record('account.activate' if active else 'account.deactivate',
                   target=code, before=before,
                   after={'code': r.get('code'), 'name': r.get('name'),
                          'active': active},
                   detail={'note': 'posted vouchers keep this account either way'})
            return r
        return f

    _wrap('add_chart_account', account_create)
    _wrap('rename_chart_account', account_rename)
    _wrap('set_chart_account_active', account_active)

    # ---- context ---------------------------------------------------------
    # /api/commit carries both the session and the sentence that started it,
    # which is the pair worth having in a trail. This is a wrapper like the
    # rest, not part of the HTTP plumbing, so it works even when register()
    # is called with no app - in a test, or from a script.
    def commit_ctx(orig):
        def f(conn, d):
            base = dict(_ctx.get() or {})
            base.update({'session_id': getattr(d, 'session_id', None)
                                       or base.get('session_id'),
                         'message': (getattr(d, 'source_message', None)
                                     or base.get('message') or '')[:500] or None})
            token = _ctx.set(base)
            try:
                return orig(conn, d)
            finally:
                _ctx.reset(token)
        return f

    _wrap('commit_voucher', commit_ctx)


# ==========================================================================
# Context - who asked for this
#
# insert_voucher() has no idea which session it is serving, and threading a
# session_id through nine functions to tell it would be a worse change than
# this one. So the two public entry points put it in a contextvar on the way
# in, and every event written underneath picks it up.
# ==========================================================================
def _install_context(app, bot) -> None:

    # /api/chat
    original_process = getattr(bot, 'process_message', None)
    if original_process is not None and not getattr(original_process, '_audited', False):
        async def process_message(message, session_id, mode=None, preview=False,
                                  _rewritten=False):
            base = dict(_ctx.get() or {})
            base.update({'session_id': session_id,
                         'message': (message or '')[:500],
                         'mode': mode})
            token = _ctx.set(base)
            try:
                return await original_process(message, session_id, mode, preview,
                                              _rewritten)
            finally:
                _ctx.reset(token)
        process_message._audited = True
        bot.process_message = process_message

    # Request-level context. Added at import time, before uvicorn starts -
    # Starlette will not accept middleware once the app is serving, so a
    # failure here is reported and the rest still works.
    try:
        import uuid

        @app.middleware("http")
        async def _audit_request_ctx(request, call_next):
            token = _ctx.set({
                'request_id': uuid.uuid4().hex[:12],
                'ip': getattr(getattr(request, 'client', None), 'host', None),
                'route': f"{request.method} {request.url.path}",
            })
            try:
                return await call_next(request)
            finally:
                _ctx.reset(token)
    except Exception as e:
        print(f"AUDIT: request context unavailable ({type(e).__name__}: {e}); "
              f"events will still be written, without the IP and route.")


# ==========================================================================
# Reading the trail from the chat.
#
# A trail nobody reads is filing, not auditing. The endpoints below are for
# a browser; this is for the person who is already typing in the box, and it
# is where the questions actually get asked - "when did that change?", "who
# put it there?", "did it save or not?".
#
# These commands are checked BEFORE the ordinary pipeline, so they can never
# be read as a posting. They are also deliberately narrow: every one starts
# with a word the voucher grammar does not use, so "Paid 450 to Audit Corp"
# stays a payment.
# ==========================================================================
_AUD_ID = r'(?:(?:crv|cpv)[\s\-]?)?(?P<id>\d{8,20})'

_AUD_RECENT_RE = re.compile(
    r'^\s*(?:show\s+|view\s+|open\s+|see\s+)?'
    r'(?:the\s+|my\s+)?audit(?:\s+trail|\s+log|\s+history)?\s*$', re.IGNORECASE)
_AUD_TARGET_RE = re.compile(
    r'^\s*(?:audit|history|trail|log)\s+(?:of\s+|for\s+|on\s+)?'
    r'(?:voucher\s+)?' + _AUD_ID + r'\s*$', re.IGNORECASE)
_AUD_WHO_RE = re.compile(
    r'^\s*who\s+(?:changed|edited|touched|posted|made|created|voided)\s+'
    r'(?:voucher\s+)?' + _AUD_ID + r'\s*$', re.IGNORECASE)
_AUD_FAILED_RE = re.compile(
    r'^\s*(?:show\s+)?audit\s+(?P<what>failures?|failed|errors?|refus\w+|'
    r'reject\w+|problems?)\s*$', re.IGNORECASE)
_AUD_VERIFY_RE = re.compile(
    r'^\s*(?:verify|check|validate)\s+(?:the\s+)?audit(?:\s+trail|\s+log)?\s*$',
    re.IGNORECASE)
_AUD_DAY_RE = re.compile(
    r'^\s*(?:show\s+)?audit\s+(?:for\s+|on\s+|from\s+)?'
    r'(?P<date>today|yesterday|\d{4}-\d{2}-\d{2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})'
    r'\s*$', re.IGNORECASE)
_AUD_SEARCH_RE = re.compile(
    r'^\s*(?:search\s+)?audit\s+(?:for\s+|search\s+|matching\s+)(?P<q>.+?)\s*$',
    re.IGNORECASE)

def strip_emojis(t: str) -> str:
    """The host module's, when it is there. A reply that renders in the chat
    bubble goes through the same cleaner every other reply does."""
    f = getattr(_M, 'strip_emojis', None)
    return f(t) if callable(f) else t


_AUD_LABEL = {
    'voucher.create': 'posted',
    'voucher.update': 'changed',
    'voucher.void': 'voided',
    'party.create': 'new profile',
    'party.reuse': 'profile reused',
    'party.update': 'profile changed',
    'account.create': 'new account',
    'account.rename': 'account renamed',
    'account.deactivate': 'account retired',
    'account.activate': 'account restored',
}


def _aud_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return '-' if v in (None, '') else str(v)


def _aud_val(key: str, v) -> str:
    if v in (None, ''):
        return '(blank)'
    if key == 'amount':
        return _aud_money(v)
    return str(v)


def _aud_changes(e: Dict) -> str:
    ch = e.get('changed') or {}
    return ', '.join(f"{k.replace('_', ' ')} {_aud_val(k, v.get('from'))}"
                     f" -> {_aud_val(k, v.get('to'))}" for k, v in ch.items())


def _aud_what(e: Dict) -> str:
    """One phrase for what this event was."""
    label = _AUD_LABEL.get(e.get('action') or '', e.get('action') or '?')
    outcome = e.get('outcome')
    if outcome == 'refused':
        return f"{label} - REFUSED"
    if outcome == 'error':
        return f"{label} - FAILED"
    return label


def _aud_subject(e: Dict) -> str:
    """Which voucher, profile or account this was about, as a person says it."""
    a = (e.get('action') or '')
    after = e.get('after') or {}
    before = e.get('before') or {}
    tgt = e.get('target') or ''
    if a.startswith('voucher'):
        vn = after.get('voucher_number') or before.get('voucher_number')
        if vn:
            return vn
        et = after.get('entry_type') or before.get('entry_type') or ''
        return f"{et}-{tgt}" if tgt and et else (tgt or '(not created)')
    if a.startswith('party'):
        return (after.get('company_name') or before.get('company_name')
                or (e.get('detail') or {}).get('asked_for') or tgt or '?')
    if a.startswith('account'):
        return (after.get('name') or before.get('name')
                or (e.get('detail') or {}).get('name') or tgt or '?')
    return tgt or '?'


def _aud_time(ts: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(ts).strftime('%H:%M')
    except Exception:
        return (ts or '')[11:16]


def _aud_day(ts: Optional[str]) -> str:
    try:
        d = datetime.fromisoformat(ts).date()
    except Exception:
        return (ts or '')[:10]
    today = date.today()
    if d == today:
        return 'Today'
    if (today - d).days == 1:
        return 'Yesterday'
    return d.strftime('%d %B %Y').lstrip('0')


def _aud_line(e: Dict) -> str:
    """One event on one line: when, what, which, and what moved."""
    tail = _aud_changes(e)
    if not tail:
        if e.get('outcome') in ('refused', 'error'):
            tail = (e.get('error') or '').split(': ', 1)[-1][:70]
        else:
            after = e.get('after') or {}
            if after.get('amount') is not None:
                who = after.get('party_code') or ''
                tail = _aud_money(after['amount'])
                acc = after.get('category_account') or after.get('bank_account')
                if acc:
                    tail += f"  {acc}"
            elif after.get('phone') or after.get('email'):
                tail = after.get('phone') or after.get('email')
    return f"  {_aud_time(e.get('ts')):<6} {_aud_what(e):<22} " \
           f"{_aud_subject(e):<26} {tail}"


def _aud_recent_reply(events: List[Dict], heading: str,
                      empty: str) -> Dict[str, Any]:
    if not events:
        return {'status': 'error', 'message': empty, 'analysis': empty,
                'confidence': 'high', '_final': True}

    ok = sum(1 for e in events if e.get('outcome') == 'ok')
    bad = len(events) - ok
    lines = [heading, ""]
    day = None
    for e in reversed(events):                      # oldest first inside the list
        d = _aud_day(e.get('ts'))
        if d != day:
            day, _ = d, lines.append("") if lines[-1] else None
            lines.append(d.upper())
        lines.append(_aud_line(e))
    lines += ["", f"{ok} went through"
                  + (f", {bad} did not" if bad else "")
                  + ". Nothing here can be edited - the trail is append-only."]
    lines.append("Type  audit <voucher id>  for one voucher's whole history, "
                 "or  verify audit  to check nothing has been altered.")
    text = strip_emojis("\n".join(lines))
    return {'status': 'success', 'message': text, 'analysis': text,
            'action': 'audit', 'confidence': 'high', '_final': True,
            'card': {'kind': 'audit', 'events': events, 'heading': heading,
                     'ok': ok, 'failed': bad}}


def _aud_history_reply(at_id: str, events: List[Dict]) -> Dict[str, Any]:
    if not events:
        t = (f"Nothing in the audit trail for {at_id}.\n\n"
             f"Either it was posted before the trail was switched on, or it "
             f"was written straight into LockInLedger rather than through "
             f"here - this trail only knows what this assistant did.")
        return {'status': 'error', 'message': t, 'analysis': t,
                'confidence': 'high', '_final': True}

    name = _aud_subject(events[0])
    lines = [f"{name} - everything recorded about it", ""]
    for e in events:                                 # already oldest first
        when = f"{_aud_day(e.get('ts'))} {_aud_time(e.get('ts'))}"
        lines.append(f"  {when:<18} {_aud_what(e)}")
        after, ch = e.get('after') or {}, _aud_changes(e)
        if ch:
            for part in ch.split(', '):
                lines.append(f"  {'':<18}   {part}")
        elif e.get('action') == 'voucher.create' and e.get('outcome') == 'ok':
            lines.append(f"  {'':<18}   {_aud_money(after.get('amount'))}"
                         f"  on {after.get('transaction_date') or '?'}")
            if after.get('bank_account'):
                lines.append(f"  {'':<18}   Bank: {after['bank_account']}")
            if after.get('category_account'):
                lines.append(f"  {'':<18}   Category: {after['category_account']}")
        if e.get('error'):
            lines.append(f"  {'':<18}   {e['error'][:90]}")
        if e.get('source_message'):
            lines.append(f"  {'':<18}   typed: \"{e['source_message'][:70]}\"")
        who = e.get('actor')
        if who:
            lines.append(f"  {'':<18}   by user {who}"
                         + (f", session {e['session_id'][-8:]}"
                            if e.get('session_id') else ''))
        lines.append("")
    lines.append(f"{len(events)} recorded change{'' if len(events) == 1 else 's'}. "
                 f"This is the trail, not the voucher - to see the voucher "
                 f"itself, type {at_id}.")
    text = strip_emojis("\n".join(lines))
    return {'status': 'success', 'message': text, 'analysis': text,
            'action': 'audit', 'confidence': 'high', '_final': True,
            'card': {'kind': 'audit', 'at_id': at_id, 'events': events,
                     'heading': name}}


def _aud_verify_reply() -> Dict[str, Any]:
    r = verify()
    if r['ok']:
        lines = ["The audit trail is intact.", "",
                 f"  {r['records_checked']} record"
                 f"{'' if r['records_checked'] == 1 else 's'} checked across "
                 f"{len(r['files'])} file{'' if len(r['files']) == 1 else 's'}",
                 "  every one still hashes to what it did when it was written",
                 "",
                 "That means nothing has been edited, removed or inserted since "
                 "it was recorded. It does not mean the entries themselves were "
                 "right - only that the file still says what it said."]
        status = 'success'
    else:
        lines = ["THE AUDIT TRAIL HAS BEEN ALTERED.", "",
                 f"  {r['records_checked']} records checked, "
                 f"{len(r['problems'])} problem(s):", ""]
        for p in r['problems'][:6]:
            lines.append(f"  {p.get('file', '?')} line {p.get('line', '?')}"
                         + (f" (entry {p['seq']})" if p.get('seq') else ''))
            lines.append(f"      {p['problem']}")
        lines += ["", "Whoever maintains this server should be told. The trail "
                      "is append-only by design; nothing this assistant does "
                      "can produce these."]
        status = 'error'
    text = strip_emojis("\n".join(lines))
    return {'status': status, 'message': text, 'analysis': text,
            'action': 'audit', 'confidence': 'high', '_final': True,
            'card': {'kind': 'audit_verify', **r}}


def _audit_chat_reply(msg: str) -> Optional[Dict[str, Any]]:
    """An audit command, answered - or None when this isn't one."""
    msg = (msg or '').strip()
    if not msg:
        return None

    m = _AUD_TARGET_RE.match(msg) or _AUD_WHO_RE.match(msg)
    if m:
        at_id = m.group('id')
        ev = read_events(limit=500, at_id=at_id)
        ev.reverse()
        return _aud_history_reply(at_id, ev)

    if _AUD_VERIFY_RE.match(msg):
        return _aud_verify_reply()

    m = _AUD_FAILED_RE.match(msg)
    if m:
        ev = [e for e in read_events(limit=400)
              if e.get('outcome') in ('refused', 'error')][:40]
        return _aud_recent_reply(
            ev, "Everything that did NOT go through",
            "Nothing has been refused or failed - every change recorded went "
            "through. (This only covers what the trail has seen; type  audit  "
            "to see how far back that is.)")

    m = _AUD_DAY_RE.match(msg)
    if m:
        raw = m.group('date').lower()
        if raw == 'today':
            day = date.today().isoformat()
        elif raw == 'yesterday':
            day = (date.today() - timedelta(days=1)).isoformat()
        else:
            day = _M.parse_date_text(raw) if hasattr(_M, 'parse_date_text') else raw
        ev = read_events(limit=300, day=day)
        pretty = _aud_day(f"{day}T00:00:00")
        return _aud_recent_reply(
            ev, f"Audit trail - {pretty} ({day})",
            f"Nothing was changed on {day}, or the server wasn't running.")

    m = _AUD_SEARCH_RE.match(msg)
    if m:
        q = m.group('q').strip().strip('"\'')
        # "audit trail" and friends are handled above; a one-word noise match
        # here would turn a lookup into a search for the word "log".
        if q.lower() in ('trail', 'log', 'history', 'everything', 'all'):
            return _aud_recent_reply(read_events(limit=25),
                                     "Audit trail - the last 25 changes",
                                     "Nothing has been recorded yet.")
        ev = read_events(limit=60, contains=q)
        return _aud_recent_reply(
            ev, f"Audit trail - anything mentioning \"{q}\"",
            f"Nothing in the trail mentions \"{q}\".")

    if _AUD_RECENT_RE.match(msg):
        return _aud_recent_reply(
            read_events(limit=25), "Audit trail - the last 25 changes",
            "Nothing has been recorded yet. The trail starts from the moment "
            "auditing was switched on, so anything posted before that isn't "
            "in it.")
    return None


def _install_chat(bot) -> None:
    """Answer audit questions before the ordinary pipeline sees them."""
    original = getattr(bot, 'process_message', None)
    if original is None or getattr(original, '_audit_chat', False):
        return

    async def process_message(message, session_id, mode=None, preview=False,
                              _rewritten=False):
        try:
            hit = _audit_chat_reply(message)
        except Exception as e:
            # Reading the trail must never be able to break the chat.
            print(f"AUDIT: couldn't answer that ({type(e).__name__}: {e})")
            hit = None
        if hit is not None:
            hit.pop('_final', None)
            return hit
        return await original(message, session_id, mode, preview, _rewritten)

    process_message._audit_chat = True
    process_message._audited = True
    bot.process_message = process_message

    # The help screen is where people find out a command exists. Appending to
    # it here keeps the whole feature inside this module.
    original_help = getattr(bot, 'get_help_response', None)
    if original_help is not None and not getattr(original_help, '_audit_chat', False):
        def get_help_response():
            r = original_help()
            extra = (
                "\n\nAUDIT TRAIL - what was changed, when, and by whom\n"
                "  audit                            the last 25 changes\n"
                "  audit today                      one day\n"
                "  audit 260902000001               one voucher's whole history\n"
                "  who changed 260902000001\n"
                "  audit failures                   everything that did NOT save\n"
                "  audit for Handy Fix              anything mentioning a name\n"
                "  verify audit                     has the trail been altered?\n"
                "  The trail is append-only and records refusals and errors as\n"
                "  carefully as successes."
            )
            for k in ('message', 'analysis'):
                if isinstance(r.get(k), str):
                    r[k] = r[k] + extra
            return r
        get_help_response._audit_chat = True
        bot.get_help_response = get_help_response


# ==========================================================================
# The endpoints
# ==========================================================================
_ROUTES_INSTALLED = False


def _install_routes(app) -> None:
    # The wrappers all carry their own "already done" marker, but routes had
    # nothing: calling register() twice added a second copy of every path.
    # FastAPI serves the first match so it worked, which is exactly how a
    # duplicate survives to confuse the API docs later.
    global _ROUTES_INSTALLED
    if _ROUTES_INSTALLED:
        return
    _ROUTES_INSTALLED = True

    from fastapi import Query
    from fastapi.responses import PlainTextResponse

    @app.get("/api/audit")
    async def api_audit(limit: int = Query(200, le=2000),
                        at_id: Optional[str] = None,
                        action: Optional[str] = None,
                        outcome: Optional[str] = None,
                        day: Optional[str] = None,
                        session_id: Optional[str] = None,
                        q: Optional[str] = None):
        """Recent events, newest first."""
        return {'status': 'success',
                'events': read_events(limit=limit, at_id=at_id, action=action,
                                      outcome=outcome, day=day,
                                      session_id=session_id, contains=q),
                'audit_dir': os.path.abspath(AUDIT_DIR),
                'enabled': AUDIT_ENABLED}

    @app.get("/api/audit/voucher/{at_id}")
    async def api_audit_voucher(at_id: str):
        """Everything that ever happened to one voucher, oldest first - which
        is the order you read a history in."""
        ev = read_events(limit=500, at_id=at_id)
        ev.reverse()
        return {'status': 'success', 'at_id': at_id, 'events': ev,
                'count': len(ev)}

    @app.get("/api/audit/verify")
    async def api_audit_verify(day: Optional[str] = None):
        """Has the trail been altered since it was written?"""
        return verify(day)

    @app.get("/api/audit/export", response_class=PlainTextResponse)
    async def api_audit_export(day: Optional[str] = None,
                               limit: int = Query(5000, le=50000)):
        """The trail as CSV."""
        return PlainTextResponse(
            to_csv(day=day, limit=limit), media_type='text/csv',
            headers={'Content-Disposition':
                     f'attachment; filename="audit-{day or "all"}.csv"'})

    @app.get("/api/audit/stats")
    async def api_audit_stats():
        """Where the trail lives and how much of it there is."""
        files = _day_files()
        _load_chain()
        return {
            'status': 'success',
            'enabled': AUDIT_ENABLED,
            'required': AUDIT_REQUIRED,
            'directory': os.path.abspath(AUDIT_DIR),
            'days': [{'file': os.path.basename(p),
                      'bytes': os.path.getsize(p),
                      'events': sum(1 for line in open(p, encoding='utf-8')
                                    if line.strip())}
                     for p in files],
            'total_events': _chain['seq'],
            'last_hash': _chain['hash'][:16],
        }


# ==========================================================================
def register(app=None, bot=None, main_module=None) -> None:
    """
    Turn auditing on. Call it LAST, after the app and bot exist.

        import audit_log
        audit_log.register(app, bot)

    The host module is found from the bot instance, so renaming main_v6.py to
    main2.py - or to anything else - does not break this.
    """
    global _M
    if main_module is not None:
        _M = main_module
    elif bot is not None:
        _M = sys.modules[type(bot).__module__]
    else:
        _M = sys.modules['__main__']

    if not AUDIT_ENABLED:
        print("AUDIT: disabled (AUDIT_ENABLED=0). Nothing is being recorded.")
        return

    _ensure_dir()
    _load_chain()
    _install_wrappers()
    if bot is not None or app is not None:
        _install_context(app, bot)
    if bot is not None:
        _install_chat(bot)
    if app is not None:
        _install_routes(app)

    print(f"AUDIT: writing to {os.path.abspath(AUDIT_DIR)}  "
          f"({_chain['seq']} events so far, chain at {_chain['hash'][:12]}…)")
    if not AUDIT_REQUIRED:
        print("AUDIT: AUDIT_REQUIRED=0 - if the trail can't be written, the "
              "work still goes through. Set it to 1 to make auditing binding.")


if __name__ == '__main__':
    # python3 audit_log.py            -> verify the trail
    # python3 audit_log.py 2026-09-18 -> verify one day
    day = sys.argv[1] if len(sys.argv) > 1 else None
    r = verify(day)
    print(f"{r['summary']}\n  scope:   {r['scope']}\n"
          f"  files:   {', '.join(r['files']) or '(none yet)'}\n"
          f"  records: {r['records_checked']}")
    for p in r['problems']:
        print(f"  !! {p}")
    sys.exit(0 if r['ok'] else 1)