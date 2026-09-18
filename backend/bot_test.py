"""
Which Groq model should GROQ_MODEL be?   ->  python3 bot_test_models.py

The point of this file is that "the API returned 200" is not the same as
"the model can do this job". Five of six models passed that test and only one
of them produced anything the app could use.

So every reply is graded the way the app will actually treat it: it is fed
straight into main2.py's OWN parser, and the question is whether the command
that comes out is the one that was meant. A model whose answer parses into a
vendor called `Echo " 450 Handy Fix LLC ...` scores worse than one that fails
outright, because a failure is visible and that is not.

Two things it measures besides correctness:
  * latency, because this sits on the critical path of every chat message
  * whether the model invents numbers, which is the one thing the guard in
    the app (_numbers_are_the_users) exists to catch
"""
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Import the real module so the real prompt and the real parser are used. The
# file is main_v6.py in the repo and main2.py on the server, so try both.
M = None
for name in ('main2', 'main_v6', 'main'):
    try:
        os.environ.setdefault('DB_HOST', 'x'); os.environ.setdefault('DB_USER', 'x')
        os.environ.setdefault('DB_PASSWORD', 'x'); os.environ.setdefault('DB_NAME', 'x')
        M = __import__(name)
        break
    except ImportError:
        continue
if M is None:
    print("Couldn't import your main module (tried main2, main_v6, main).")
    print("Run this from the folder that contains it.")
    sys.exit(1)

KEY = os.getenv("ACCOUNTING_GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
if not KEY:
    print("No API key. Set ACCOUNTING_GROQ_API_KEY in .env")
    sys.exit(1)

from groq import Groq                                          # noqa: E402
client = Groq(api_key=KEY)

SYSTEM = M._FALLBACK_SYSTEM          # the app's real prompt, not an imitation


# --------------------------------------------------------------------------
# What a right answer looks like. Each case is judged by running the model's
# line through the app's own routers, in the same order process_message does.
# --------------------------------------------------------------------------
def kind_of(line):
    """What would the app DO with this line? Returns (kind, detail)."""
    line = (line or '').strip()
    if not line:
        return 'empty', ''
    if line.upper().startswith('UNKNOWN'):
        return 'unknown', ''
    if M._VOID_CMD_RE.match(line):
        return 'void', M._VOID_CMD_RE.match(line).group('id')
    if M._UPDATE_CMD_RE.match(line):
        return 'update', M._UPDATE_CMD_RE.match(line).group('id')
    if M._parse_profile_command(line):
        p = M._parse_profile_command(line)
        return 'profile', p['name']
    if M._parse_add_account_command(line):
        return 'account', M._parse_add_account_command(line)['name']
    ex = M._rule_based_extract(line)
    if ex.get('action') == 'create_transaction':
        return ex['entry_type'], ex.get('party_name') or ''
    return 'unparsed', ex.get('action', '')


CASES = [
    # (messy input, expected kind, a word the detail must contain)
    ('paid 450 handy fix llc repair maintanence bofa 9523',      'CPV',     'handy fix'),
    ('recieved 1250 abc trading invoice 2045 chase 4582',        'CRV',     'abc trading'),
    ('chnage 260902000001 amt to 500',                           'update',  '260902000001'),
    ('new custmer abc trading llc ph 555-123-4567',              'profile', 'abc trading'),
    ('make a new expence acct calld Fuel',                       'account', 'fuel'),
    ('whats the weather in karachi',                             'unknown', ''),
]


def ask(model, message, extra=None):
    t0 = time.time()
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": f"message: {message}"}],
        temperature=0, max_tokens=(extra or {}).pop('max_tokens', 160),
        timeout=30, **(extra or {}),
    )
    out = (r.choices[0].message.content or '').strip()
    return out, time.time() - t0


def grade(model):
    score, notes, times = 0, [], []
    for message, want_kind, want_in in CASES:
        try:
            raw, dt = ask(model, message)
            # A reasoning model can spend the whole budget thinking and return
            # nothing. Give it one more go with reasoning turned down before
            # calling it broken - that is a setting, not an ability.
            if not raw:
                try:
                    raw, dt2 = ask(model, message,
                                   {'reasoning_effort': 'low', 'max_tokens': 400})
                    dt += dt2
                    if raw:
                        notes.append('needs reasoning_effort=low')
                except Exception:
                    pass
        except Exception as e:
            notes.append(f"{message[:18]}...: {type(e).__name__}")
            continue
        times.append(dt)

        line = raw.splitlines()[0].strip().strip('`"') if raw else ''
        line = line[len('command:'):].strip() if line.lower().startswith('command:') else line
        got, detail = kind_of(line)

        ok = got == want_kind and want_in.lower() in (detail or '').lower()
        # An answer that parses into the WRONG thing is worse than one that
        # doesn't parse: the app would act on it, and a bad party name becomes
        # a real profile the first time someone saves without reading.
        if ok:
            score += 2
        elif got in ('empty', 'unparsed'):
            score += 0
            notes.append(f"{message[:16]}.. -> no command")
        else:
            score -= 1
            notes.append(f"{message[:16]}.. -> {got} {str(detail)[:26]!r} (wrong, and it PARSES)")

        if raw and not M._numbers_are_the_users(message, line):
            score -= 2
            notes.append(f"{message[:16]}.. -> INVENTED A NUMBER")
        if raw and ('|' in raw or '**' in raw or len(raw.splitlines()) > 2):
            notes.append(f"{message[:16]}.. -> chatty/markdown, not one line")

    avg = sum(times) / len(times) if times else 0
    return score, avg, notes


if __name__ == '__main__':
    wanted = sys.argv[1:] or [m.id for m in client.models.list().data
                              if not any(x in m.id for x in
                                         ('whisper', 'tts', 'guard', 'orpheus'))]
    print(f"grading {len(wanted)} models on {len(CASES)} real cases "
          f"(max score {len(CASES) * 2})\n")
    results = []
    for model in wanted:
        print(f"--- {model}")
        score, avg, notes = grade(model)
        results.append((score, avg, model, notes))
        print(f"    score {score}/{len(CASES) * 2}   avg {avg:.2f}s")
        for n in notes[:4]:
            print(f"      - {n}")

    print("\n" + "=" * 66)
    results.sort(key=lambda r: (-r[0], r[1]))
    for score, avg, model, _n in results:
        verdict = ('USABLE   ' if score >= len(CASES) * 2 - 2 else
                   'marginal ' if score > 0 else 'NO       ')
        print(f"  {verdict} {score:>3}/{len(CASES)*2}  {avg:>5.2f}s  {model}")
    if results and results[0][0] > 0:
        print(f"\n  GROQ_MODEL={results[0][2]}")
    else:
        print("\n  None of them are usable. Keep LLM_FALLBACK=0 - the app is\n"
              "  fully functional on the regex alone; only typo-forgiveness is lost.")