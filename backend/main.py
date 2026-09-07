"""
LedgerAssist - AI Accounting Chatbot  (v6)
==========================================

NEW IN v6 - two things beyond posting from chat:

A. UPDATE AN EXISTING VOUCHER          GET/PUT /api/voucher/{at_id}
   Change party, either account, amount, date, cheque or description on a
   CRV/CPV that is still open.  Mirrors admin_model::updatePayment():
   UPDATE the master, DELETE acc_trans_d + acc_trans_reconcile for that
   at_id, re-INSERT the legs.  at_id never changes and at_cr_user/at_cr_date
   are preserved.

   Editing is REFUSED when the host UI would also refuse:
     * at_status != 1  - 0 is void, 2 is posted to the ledger.  The PHP
       voucher lists only render Edit at status 1 (debitMemoList()).
     * any leg reconciled (at_acc_reconcile = 1) - invoice_list() and
       debitMemoList() hide Edit and Void once reconciled > 0.
     * more than two detail legs - vouchers written by the PHP screens can
       carry many legs plus a regrouped reconcile side; rewriting those as
       two legs would destroy data.
     * changing CRV <-> CPV - the doc type is baked into at_id at chars 5-6
       and other pages read it back out (invoice_list). Void and re-post.
   POST /api/voucher/{at_id}/void is the escape hatch for all of the above.

   Accounts are chosen BY CODE on an update, never by fuzzy name match -
   an edit is a deliberate act and should not go through the guesser.

B. CREATE A PROFILE UP FRONT           GET/POST /api/parties
   acc_party rows can now be created deliberately, with the full field set,
   instead of only being conjured mid-sentence while posting a voucher.
   p_code is still seeded from the type prefix exactly as
   admin_model::addAccount() does (C 10103 / P 20101 / E 20102 / O 20103),
   and a same-name profile of the same type is reused rather than duplicated.

Everything below this point is v5 and unchanged.


v5 rewrites account resolution to match how the host PHP application
(admin_model) actually models a chart of accounts.  Summary of what changed
and why:

1. ACCOUNT RESOLUTION now reads `v_trans_accounts_m2`, scoped by @system_id.
   ------------------------------------------------------------------------
   v4 searched `SELECT ... FROM accounts`, which is the *shared, cross-tenant
   master table of 13-digit individual accounts*.  In this system the code
   length IS the hierarchy level:

       length  1  -> nature      (account_nature)        e.g. 4
       length  5  -> main        (account_main)          e.g. 45016
       length  9  -> sub         (account_sub)           e.g. 450215001
       length 13  -> individual  (accounts)              e.g. 2000150015101

   ...and a node is POSTABLE exactly when it has no children (see
   admin_model::transactionableAccount()).  Most real accounts in this
   customer's chart ("Repair and Maintenance" 45016, "Bank of America 9523"
   100045001) are childless MAINs and SUBs - they have no row in `accounts`
   at all, so v4 could never find them.

   `v_trans_accounts_m2` already materialises precisely the postable set for
   one tenant (childless mains U childless subs U individual accounts).  It is
   what admin_model::cashBankAccounts() and trialBalanace() use.  One SELECT
   replaces v4's resolve/_detect_flag/_main_is_postable hierarchy walk.

2. DISPLAY NAMES.  The UI shows "PARENT/LEAF" - e.g. "EXPENSE/Repair and
   Maintenance", "Payroll Taxes/FICA" - assembled at render time as
   subsi_acc_desc + "/" + trans_acc_desc (custTransDetail, voucherListSummary,
   generalLedgerCustom, debitMemoList).  We index BOTH the qualified and the
   bare name so a user can paste either.

3. NATURE comes from the LEADING DIGIT of the account code, never from
   `acc_dc` (which only exists on 13-digit rows) and never from subsi_acc_id
   (which is only the nature for leaf mains):
       1 asset  2 liability  3 revenue  4 expense  5 equity  6 COGS  7 unearned
   This mirrors trialBalanace() and inlineProfitLoss().

4. VOUCHER IDs are now generated the way PHP does:
       at_id = max( YYMM || doc_type || '000001' ,  MAX(at_id)+1 )
   filtered by at_doc_type AND system_id.  v4 took a global MAX(at_id) across
   every doc type and tenant, which destroys the convention other pages rely
   on (invoice_list does substring(idt_at_id,5,2) to route crv.php vs cpv.php).

5. PARTY TYPE for vendors is 'P' (payee), not 'V'.  Valid types are
   C customer / P payee-vendor / E employee / O other.  p_code is seeded from
   the type's control-account prefix exactly as admin_model::addAccount() does.

6. Posting is guarded by a port of transactionableAccount(); reports filter
   at_status >= 1; at_pmode is 1 for CRV / 2 for CPV; at_desc carries the
   fixed voucher label and at_remarks the user's text, matching PHP.

NOTE ON acc_trans_reconcile: PHP does NOT write a 1:1 mirror of acc_trans_d -
addPayment1()/addIncomeTest() regroup the bank side so each debit line gets a
matching credit line.  The 1:1 mirror below is correct ONLY because this bot
always posts exactly two legs.  If you ever add split entries, revisit
insert_voucher().

CREDENTIALS come from the environment only.  Rotate anything previously
committed.
"""

import os
import re
import csv
import json
import time
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import mysql.connector
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="LedgerAssist - AI Accounting Chatbot v6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    message: str
    # Optional UI hint: "post" | "update" | "profile". Only used to disambiguate
    # an otherwise bare message (a lone voucher id, or a lone name); every
    # command works in any mode.
    mode: Optional[str] = None


class PartyCreate(BaseModel):
    """A 'profile' - a row in acc_party. Mirrors admin_model::addAccount()."""
    p_type: str                                  # C customer / P payee / E employee / O other
    company_name: Optional[str] = None
    person_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    sale_tax_no: Optional[str] = None
    fedral_id_no: Optional[str] = None
    job_title: Optional[str] = None
    business_desc: Optional[str] = None
    other_desc: Optional[str] = None
    p_account: Optional[str] = None              # default GL account for this party
    status: int = 1


class VoucherUpdate(BaseModel):
    """
    Fields that may be changed on an existing CRV/CPV. Anything omitted keeps
    its current value.  entry_type is accepted only so the client can send the
    whole form back unchanged - changing it is REFUSED (see update_voucher).
    """
    entry_type: Optional[str] = None
    amount: Optional[float] = None
    transaction_date: Optional[str] = None       # YYYY-MM-DD
    party_code: Optional[str] = None
    bank_acc_code: Optional[str] = None
    category_acc_code: Optional[str] = None
    cheque_no: Optional[str] = None              # "" clears it
    description: Optional[str] = None


DB_CONFIG = {
    'host': os.getenv("DB_HOST", ""),
    'user': os.getenv("DB_USER", ""),
    'password': os.getenv("DB_PASSWORD", ""),
    'database': os.getenv("DB_NAME", ""),
    'port': int(os.getenv("DB_PORT", "3306")),
}

SYSTEM_ID = int(os.getenv("SYSTEM_ID", "146"))
USER_ID = os.getenv("USER_ID", "86")          # written to at_cr_user (varchar(20))

DOC_TYPE_CRV = '01'      # Cash Receipt Voucher
DOC_TYPE_CPV = '02'      # Cash Payment Voucher

PMODE = {DOC_TYPE_CRV: 1, DOC_TYPE_CPV: 2}     # matches addIncomeTest / addPayment1

# acc_party.p_type - see signupTypes / admin_model::addAccount()
P_TYPE_CUSTOMER = 'C'
P_TYPE_VENDOR = 'P'      # 'P' = Payee/Vendor.  v4 wrote 'V', which is not a
                         # valid type and makes the party invisible to
                         # vendors_list.php and showAccounts().
P_TYPE_EMPLOYEE = 'E'
P_TYPE_OTHER = 'O'

# Control-account prefixes p_code is seeded from (admin_model constants).
PARTY_CODE_PREFIX = {
    'C': '10103',   # DEBITORS
    'P': '20101',   # CREDITORS
    'E': '20102',   # EMPLOYEES
    'O': '20103',   # OTHERS
}

# Account nature, taken from the first digit of the account code.
NATURE_ASSET = '1'
NATURE_LIABILITY = '2'
NATURE_REVENUE = '3'
NATURE_EXPENSE = '4'
NATURE_EQUITY = '5'
NATURE_COGS = '6'
NATURE_UNEARNED = '7'

NATURE_LABEL = {
    '1': 'Asset', '2': 'Liability', '3': 'Revenue', '4': 'Expense',
    '5': 'Equity', '6': 'Cost of Goods Sold', '7': 'Unearned Income',
}

# Which natures may carry the income/expense leg of each voucher type.
CRV_INCOME_NATURES = {NATURE_REVENUE}
CPV_EXPENSE_NATURES = {NATURE_EXPENSE, NATURE_COGS}
if os.getenv("CRV_ALLOW_UNEARNED", "0") == "1":
    CRV_INCOME_NATURES.add(NATURE_UNEARNED)

# Explicit fallbacks. Strongly recommended - guessing a cash account by name
# is how "Bank Service Fee" ends up receiving deposits.
DEFAULT_BANK_ACC = (os.getenv("DEFAULT_BANK_ACC") or "").strip()
DEFAULT_REVENUE_ACC = (os.getenv("DEFAULT_REVENUE_ACC") or "").strip()
DEFAULT_EXPENSE_ACC = (os.getenv("DEFAULT_EXPENSE_ACC") or "").strip()

# acc_trans_m flags.  PHP defaults include_in_billing to the form checkbox
# (unchecked = 0); v4 hardcoded 1.  Kept configurable so billing reports do
# not change under you without a decision.
INCLUDE_IN_BILLING = int(os.getenv("INCLUDE_IN_BILLING", "1"))

# Block postings dated outside the open fiscal / audit year (business_year).
# PHP has validate_transaction_within_fiscalyear() but leaves it commented
# out, so default here is warn-only.
STRICT_FISCAL_YEAR = os.getenv("STRICT_FISCAL_YEAR", "0") == "1"

FUZZY_MATCH_THRESHOLD = 0.72
AMBIGUITY_MARGIN = 0.08          # two candidates this close => refuse, don't guess

CHART_CACHE_TTL = int(os.getenv("CHART_CACHE_TTL", "300"))   # seconds

# at_bank      varchar(13)  - cosmetic display label, clipped
# at_bank_acc  varchar(20)  - the real code reports/reconciliation join on
AT_BANK_LABEL_MAX_LEN = 13
AT_BANK_ACC_MAX_LEN = 20

LLM_LOG_FILE = "llm_extractions.csv"
QUERY_LOG_FILE = "query_log.csv"
VOUCHER_LOG_FILE = "voucher_postings.csv"

_GENERIC_BANK_PHRASES = {
    'cash', 'bank', 'checking', 'savings', 'cash on hand',
    'petty cash', 'bank account',
}

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    if not text:
        return text
    return _EMOJI_PATTERN.sub('', text).strip()


# --------------------------------------------------------------------------
# Logging helpers
# --------------------------------------------------------------------------
def log_query(query: str, params: Any, result: Any, duration_ms: float, context: str = ""):
    try:
        file_exists = os.path.isfile(QUERY_LOG_FILE)
        with open(QUERY_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'context', 'query', 'params',
                                 'result_rows', 'result_sample', 'duration_ms'])
            result_str = str(result)
            if len(result_str) > 500:
                result_str = result_str[:500] + '...'
            writer.writerow([
                datetime.now().isoformat(), context,
                query.replace('\n', ' ').strip(), str(params),
                len(result) if isinstance(result, list) else 'N/A',
                result_str, f"{duration_ms:.2f}",
            ])
    except Exception as e:
        print(f"Failed to log query: {e}")


def log_llm_extraction(extracted_data: Dict, user_message: str, session_id: str) -> None:
    try:
        file_exists = os.path.isfile(LLM_LOG_FILE)
        with open(LLM_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            fieldnames = ['timestamp', 'session_id', 'user_message', 'action',
                          'entry_type', 'amount', 'party_name', 'description',
                          'transaction_date', 'cheque_number', 'bank_text', 'category_hint']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                'timestamp': datetime.now().isoformat(),
                'session_id': session_id,
                'user_message': user_message[:500],
                'action': extracted_data.get('action', ''),
                'entry_type': extracted_data.get('entry_type', ''),
                'amount': extracted_data.get('amount', ''),
                'party_name': extracted_data.get('party_name', ''),
                'description': extracted_data.get('description', ''),
                'transaction_date': extracted_data.get('transaction_date', ''),
                'cheque_number': extracted_data.get('cheque_number', '') or extracted_data.get('cheque_no', ''),
                'bank_text': extracted_data.get('bank_text', ''),
                'category_hint': extracted_data.get('category_hint', ''),
            })
    except Exception as e:
        print(f"Failed to log LLM extraction: {e}")


def log_voucher_posting(voucher_number: str, entry_type: str, details: Dict,
                        session_id: str, user_message: str) -> None:
    party_score = details.get('party_score') or 0.0
    bank_score = details.get('bank_score') or 0.0
    cat_score = details.get('category_score') or 0.0
    lines = [
        f"[voucher] {voucher_number} ({entry_type}) posted - session {session_id}",
        f"  message:    {user_message[:200]}",
        f"  amount:     {details.get('amount')}",
        f"  party:      {details.get('party_display')} "
        f"[type={details.get('party_type')}, match={details.get('party_match_type')}, score={party_score:.2f}]",
        f"  bank_leg:   {details.get('bank_desc')} (code={details.get('bank_code')}) "
        f"[level={details.get('bank_level')}, match={details.get('bank_match_type')}, "
        f"score={bank_score:.2f}, search='{details.get('bank_text')}']",
        f"  category:   {details.get('category_desc')} (code={details.get('category_code')}) "
        f"[level={details.get('category_level')}, match={details.get('category_match_type')}, "
        f"score={cat_score:.2f}]",
        f"  date:       {details.get('trans_date')}",
        f"  cheque_no:  {details.get('cheque_no')}",
    ]
    print("\n".join(lines))
    try:
        file_exists = os.path.isfile(VOUCHER_LOG_FILE)
        with open(VOUCHER_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    'timestamp', 'session_id', 'voucher_number', 'entry_type', 'amount',
                    'party_display', 'party_type', 'party_match_type', 'party_score',
                    'bank_desc', 'bank_code', 'bank_level', 'bank_match_type', 'bank_score', 'bank_text',
                    'category_desc', 'category_code', 'category_level', 'category_match_type', 'category_score',
                    'trans_date', 'cheque_no', 'user_message',
                ])
            writer.writerow([
                datetime.now().isoformat(), session_id, voucher_number, entry_type, details.get('amount'),
                details.get('party_display'), details.get('party_type'),
                details.get('party_match_type'), party_score,
                details.get('bank_desc'), details.get('bank_code'), details.get('bank_level'),
                details.get('bank_match_type'), bank_score, details.get('bank_text'),
                details.get('category_desc'), details.get('category_code'), details.get('category_level'),
                details.get('category_match_type'), cat_score,
                details.get('trans_date'), details.get('cheque_no'), user_message[:500],
            ])
    except Exception as e:
        print(f"Failed to log voucher posting: {e}")


# --------------------------------------------------------------------------
# Text / name utilities
# --------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = unicodedata.normalize('NFKD', str(name)).lower()
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def title_case_name(name: str) -> str:
    """Title-case, preserving short all-caps tokens that look like suffixes or
    acronyms (LLC, LTD, INC, XYZ). A wholly uppercase input is a shouted name
    from a bank statement ("JOHN SMITH"), not an acronym, so title-case it all."""
    if not name:
        return name
    shouted = name.isupper()
    out = []
    for w in name.split():
        bare = w.strip('.,').upper()
        if bare in _BUSINESS_SUFFIXES:
            out.append(bare + w[len(w.rstrip('.,')):])
        elif not shouted and w.isupper() and len(w) <= 4 and not w.isdigit():
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return ' '.join(out)


_BUSINESS_SUFFIXES = {
    'LLC', 'L.L.C', 'LTD', 'INC', 'LLP', 'PLC', 'PC', 'LP', 'CO', 'CORP',
    'SA', 'NV', 'BV', 'GMBH', 'AG', 'PVT', 'PTE', 'PTY', 'DBA', 'USA', 'US',
}


def _strip_standalone_numbers(text: str) -> str:
    return ' '.join(t for t in text.split() if not t.isdigit())


_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
_MONTH_ALT = '|'.join(_MONTH_NAMES.keys())
_CHEQUE_RE = re.compile(
    r'\b(?:cheque|check|chq|ch)\.?\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{3,20})\b',
    re.IGNORECASE,
)


def parse_date_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    if re.search(r'\btoday\b', t):
        return datetime.now().date().isoformat()
    if re.search(r'\byesterday\b', t):
        return (datetime.now().date() - timedelta(days=1)).isoformat()
    if re.search(r'\btomorrow\b', t):
        return (datetime.now().date() + timedelta(days=1)).isoformat()
    m = re.search(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b', text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', text)
    if m:
        try:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(' + _MONTH_ALT + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b', t)
    if m:
        try:
            mo = _MONTH_NAMES[m.group(1)]
            d = int(m.group(2))
            y = int(m.group(3)) if m.group(3) else datetime.now().year
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(' + _MONTH_ALT + r')\.?(?:\s+(\d{4}))?\b', t)
    if m:
        try:
            d = int(m.group(1))
            mo = _MONTH_NAMES[m.group(2)]
            y = int(m.group(3)) if m.group(3) else datetime.now().year
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass
    return None


def normalize_date_str(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = str(raw).strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        try:
            datetime.strptime(raw, '%Y-%m-%d')
            return raw
        except ValueError:
            pass
    return parse_date_text(raw)


# --------------------------------------------------------------------------
# Deterministic (rule-based) extraction
#
# The LLM is an enhancement, not a dependency.  v4 returned {"action":"help"}
# for every extraction failure - missing API key, dead model, 429, non-JSON
# reply - so a perfectly well-formed "Paid $450 to X for Y from Z" came back
# as the help screen with no clue why.  These rules handle the documented
# formats without any network call; the LLM then overrides where it did
# better (usually party names and unusual phrasings).
# --------------------------------------------------------------------------
_CPV_VERBS = re.compile(
    r'\b(paid|pay|payment|paying|sent|spent|spend|purchase[ds]?|bought|buy|'
    r'withdrew|withdrawal|withdraw|debit|debited|charge|charged|'
    r'ach\s+debit|wire\s+out|check\s+to)\b', re.IGNORECASE)
_CRV_VERBS = re.compile(
    r'\b(received|receive|receipt|deposit|deposited|got|collected|collection|'
    r'income|credited|credit\s+from|refund|refunded|'
    r'ach\s+deposit|wire\s+in)\b', re.IGNORECASE)

# Segment markers. The role each preposition introduces depends on direction:
#   CPV:  to -> party,  from|via|out of|using -> bank,  for -> category
#   CRV:  from -> party, via|into|to|through   -> bank,  for -> category
_MARKER_RE = re.compile(
    r'\b(to|from|for|via|through|thru|into|using|out\s+of|by\s+cheque|by\s+check)\b',
    re.IGNORECASE)

_DATE_TAIL_RE = re.compile(
    r'\b(?:on|dated|date)?\s*\b('
    r'today|yesterday|tomorrow'
    r'|\d{4}-\d{1,2}-\d{1,2}'
    r'|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
    r'|(?:' + _MONTH_ALT + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?'
    r'|\d{1,2}(?:st|nd|rd|th)?\s+(?:' + _MONTH_ALT + r')\.?(?:\s+\d{4})?'
    r')\b', re.IGNORECASE)

_AMOUNT_CUR_RE = re.compile(r'[\$£€]\s*(\d[\d,]*(?:\.\d{1,2})?)')
_AMOUNT_DEC_RE = re.compile(r'\b(\d[\d,]*\.\d{1,2})\b')
_AMOUNT_INT_RE = re.compile(r'\b(\d[\d,]{2,})\b')
_TRAILING_JUNK_RE = re.compile(r'^[\s,;:\-–—.]+|[\s,;:\-–—.]+$')
_LEADING_FILLER_RE = re.compile(
    r'^(?:the|my|our|a|an|account|acct|gl|ledger)\s+', re.IGNORECASE)


def _parse_amount(text: str) -> Optional[float]:
    """Currency-marked number wins; then a decimal number; then a bare integer.
    Dates and cheque numbers are removed first so 06/04/2026 and 'cheque 4521'
    can't be mistaken for the amount."""
    work = _CHEQUE_RE.sub(' ', text)
    work = _DATE_TAIL_RE.sub(' ', work)
    for rx in (_AMOUNT_CUR_RE, _AMOUNT_DEC_RE, _AMOUNT_INT_RE):
        m = rx.findall(work)
        if m:
            try:
                return abs(float(m[-1].replace(',', '')))
            except ValueError:
                continue
    return None


def _clean_segment(seg: str) -> str:
    """Strip dates, cheque refs, amounts and punctuation from a captured span."""
    s = _CHEQUE_RE.sub(' ', seg)
    s = _DATE_TAIL_RE.sub(' ', s)
    s = _AMOUNT_CUR_RE.sub(' ', s)
    s = _AMOUNT_DEC_RE.sub(' ', s)
    s = re.sub(r'\b(?:cheque|check|chq|ref|reference|memo|note)\b.*$', ' ', s,
               flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip()
    s = _TRAILING_JUNK_RE.sub('', s)
    s = _LEADING_FILLER_RE.sub('', s)
    return _TRAILING_JUNK_RE.sub('', s).strip()


# --- bank-statement line format -------------------------------------------
#   <date>  <ACCOUNT or DESCRIPTOR> - <PARTY>  <amount>
#
# The line carries no verb, so direction has to come from one of:
#   * a statement descriptor      "ACH DEPOSIT", "ACH DEBIT", "CHECK"
#   * a signed / bracketed amount  -659.25   (659.25)
#   * an explicit DR / CR prefix   "DR Rent Expense - LANDLORD 2400.00"
# With none of those the direction is genuinely unknowable and we say so
# rather than guessing - a reversed voucher is worse than a rejected one.
_STMT_CREDIT_RE = re.compile(
    r'\b(ach\s+deposit|ach\s+credit|direct\s+deposit|deposit|dep|wire\s+in|'
    r'incoming\s+wire|inbound\s+wire|credit|refund|reversal|rebate|interest)\b',
    re.IGNORECASE)
_STMT_DEBIT_RE = re.compile(
    r'\b(ach\s+debit|ach\s+withdrawal|withdrawal|withdraw|debit|check|chk|cheque|'
    r'pos|purchase|payment|pmt|bill\s*pay|fee|charge|service\s+charge|'
    r'wire\s+out|outgoing\s+wire|transfer\s+out|autopay)\b', re.IGNORECASE)

_STMT_LINE_RE = re.compile(
    r'^\s*(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})\s+(?P<body>.+)$')
_STMT_AMOUNT_RE = re.compile(
    r'(?P<open>\()?\s*(?P<sign>[-+])?\s*[\$£€]?\s*'
    r'(?P<amt>\d[\d,]*(?:\.\d{1,2})?)\s*(?P<close>\))?\s*$')
_DRCR_PREFIX_RE = re.compile(r'^\s*(?P<dc>dr|cr)\b[\s:.\-]*', re.IGNORECASE)


def _parse_statement_line(msg: str) -> Optional[Dict[str, Any]]:
    """Parses '<date> <ACCOUNT|DESCRIPTOR> - <PARTY> <amount>'."""
    m = _STMT_LINE_RE.match(msg)
    if not m:
        return None
    date_iso = parse_date_text(m.group('date'))
    body = m.group('body').strip()

    am = _STMT_AMOUNT_RE.search(body)
    if not am:
        return None
    try:
        amount = abs(float(am.group('amt').replace(',', '')))
    except ValueError:
        return None
    if amount == 0:
        return None
    body = body[:am.start()].strip()

    negative = am.group('sign') == '-' or bool(am.group('open') and am.group('close'))

    entry_type = None
    inferred = False
    dc = _DRCR_PREFIX_RE.match(body)
    if dc:
        entry_type = 'CPV' if dc.group('dc').lower() == 'dr' else 'CRV'
        body = body[dc.end():].strip()
    elif negative:
        entry_type = 'CPV'
    elif am.group('sign') == '+':
        entry_type = 'CRV'

    # Split "<account or descriptor> - <party>"
    account_text, party_text = '', body
    parts = re.split(r'\s+[-–—]\s+', body, maxsplit=1)
    if len(parts) == 2:
        account_text, party_text = parts[0].strip(), parts[1].strip()

    # A descriptor like "ACH DEPOSIT" sets direction and is NOT an account.
    probe = account_text or body
    if entry_type is None:
        if _STMT_CREDIT_RE.search(probe):
            entry_type = 'CRV'
        elif _STMT_DEBIT_RE.search(probe):
            entry_type = 'CPV'
    descriptor = bool(_STMT_CREDIT_RE.search(account_text)
                      or _STMT_DEBIT_RE.search(account_text)) if account_text else False

    party = _clean_segment(party_text)
    party = re.sub(r'\b(ach|eft|wire|transfer|pos|dep)\b', ' ', party, flags=re.IGNORECASE)
    party = _clean_segment(party)
    if not party:
        return None

    if entry_type is None:
        return {"action": "ambiguous_direction",
                "_parsed": {"date": date_iso, "account": account_text,
                            "party": party, "amount": amount},
                "_source": "rules"}

    out: Dict[str, Any] = {
        "action": "create_transaction",
        "entry_type": entry_type,
        "amount": amount,
        "party_name": title_case_name(party),
        "description": ('Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher'),
        "_source": "rules",
        "_direction_inferred": inferred,
    }
    if date_iso:
        out["transaction_date"] = date_iso
    if account_text and not descriptor:
        # Which leg this belongs to depends on its nature, which the parser
        # can't know. process_message() routes it after looking the code up.
        out["account_text"] = _clean_segment(account_text)
    chq = _CHEQUE_RE.search(msg)
    if chq:
        out["cheque_number"] = chq.group(1)
    return out


def _rule_based_extract(message: str) -> Dict[str, Any]:
    """
    Parses the documented one-line formats without an LLM. Returns the same
    shape as the LLM path, {"action": "ambiguous_direction"} for a statement
    line with no in/out signal, or {"action": "help"} when nothing parses.
    """
    msg = (message or '').strip()
    if not msg:
        return {"action": "help"}

    is_cpv = bool(_CPV_VERBS.search(msg))
    is_crv = bool(_CRV_VERBS.search(msg))
    if is_cpv and is_crv:                       # both present: first one wins
        is_cpv = _CPV_VERBS.search(msg).start() < _CRV_VERBS.search(msg).start()
        is_crv = not is_cpv
    if not is_cpv and not is_crv:
        return _parse_statement_line(msg) or {"action": "help"}
    entry_type = 'CPV' if is_cpv else 'CRV'

    amount = _parse_amount(msg)
    if amount is None:
        return {"action": "help"}

    if entry_type == 'CPV':
        role_of = {'to': 'party', 'from': 'bank', 'via': 'bank', 'through': 'bank',
                   'thru': 'bank', 'using': 'bank', 'out of': 'bank',
                   'into': 'bank', 'for': 'category'}
    else:
        role_of = {'from': 'party', 'via': 'bank', 'through': 'bank',
                   'thru': 'bank', 'into': 'bank', 'to': 'bank',
                   'using': 'bank', 'out of': 'bank', 'for': 'category'}

    # Walk markers left to right. A marker only opens a segment if its role is
    # still unfilled - so the inner "to" in "Loan to Shareholders" is absorbed
    # into the bank segment instead of splitting it.
    filled: Dict[str, str] = {}
    open_role: Optional[str] = None
    open_at = 0

    for m in _MARKER_RE.finditer(msg):
        word = re.sub(r'\s+', ' ', m.group(1).lower())
        role = role_of.get(word)
        if role is None or role in filled:
            continue
        if open_role is not None:
            filled[open_role] = _clean_segment(msg[open_at:m.start()])
        open_role, open_at = role, m.end()
    if open_role is not None:
        filled[open_role] = _clean_segment(msg[open_at:])

    party = filled.get('party') or ''
    # Statement style with no prepositions: "06/04/2026 ACH DEPOSIT - JOHN SMITH 659.25"
    if not party:
        body = _clean_segment(_CPV_VERBS.sub(' ', _CRV_VERBS.sub(' ', msg)))
        if '-' in body or '–' in body:
            tail = re.split(r'[-–]', body)[-1]
            party = _clean_segment(tail)
        else:
            party = body
        party = re.sub(r'\b(ach|eft|wire|transfer|payment|deposit|debit|credit)\b',
                       ' ', party, flags=re.IGNORECASE)
        party = _clean_segment(party)

    out: Dict[str, Any] = {
        "action": "create_transaction",
        "entry_type": entry_type,
        "amount": amount,
        "party_name": title_case_name(party) if party else None,
        "description": filled.get('category') or (
            'Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher'),
        "_source": "rules",
    }
    if filled.get('bank'):
        out["bank_text"] = filled['bank']
    if filled.get('category'):
        out["category_hint"] = filled['category']

    d = parse_date_text(msg)
    if d:
        out["transaction_date"] = d
    chq = _CHEQUE_RE.search(msg)
    if chq:
        out["cheque_number"] = chq.group(1)

    if not out["party_name"]:
        return {"action": "help"}
    return out


def _looks_transactional(message: str) -> bool:
    """Has a direction word AND a number - used to stop the intent keyword
    shortcuts from hijacking a real posting (e.g. 'report' in a description)."""
    m = message or ''
    return bool((_CPV_VERBS.search(m) or _CRV_VERBS.search(m))
                and _parse_amount(m) is not None)


# --------------------------------------------------------------------------
# Chat commands for editing vouchers and creating profiles.
#
# Everything is typed - there is no form. The parsers below are deliberate
# rather than fuzzy: an edit names its target by id, and a field is only
# changed when the user names that field. Values (party / account names) still
# go through the normal resolver, which refuses on ambiguity rather than
# guessing.
# --------------------------------------------------------------------------
_VID = r'(?:(?:crv|cpv)[\s\-]?)?(?P<id>\d{8,20})'

_UPDATE_CMD_RE = re.compile(
    r'^\s*(?:update|edit|change|modify|amend|correct|fix|set)\s+'
    r'(?:voucher\s+|entry\s+)?' + _VID + r'\b(?P<rest>.*)$', re.IGNORECASE | re.DOTALL)
_SHOW_CMD_RE = re.compile(
    r'^\s*(?:show|view|open|load|get|display|fetch)\s+'
    r'(?:voucher\s+|entry\s+)?' + _VID + r'\s*$', re.IGNORECASE)
_BARE_ID_RE = re.compile(r'^\s*' + _VID.replace('{8,20}', '{10,20}') + r'\s*$', re.IGNORECASE)
_VOID_CMD_RE = re.compile(
    r'^\s*(?:void|cancel|reverse|kill)\s+'
    r'(?:voucher\s+|entry\s+)?' + _VID + r'\s*$', re.IGNORECASE)

# field keyword -> canonical name
_UPD_FIELD_MAP = {
    'amount': 'amount', 'amt': 'amount', 'value': 'amount', 'total': 'amount',
    'date': 'transaction_date', 'dated': 'transaction_date',
    'party': 'party', 'customer': 'party', 'vendor': 'party',
    'payee': 'party', 'supplier': 'party', 'client': 'party',
    'bank': 'bank', 'cash': 'bank', 'contra': 'bank', 'source': 'bank',
    'category': 'category', 'classification': 'category', 'account': 'category',
    'expense': 'category', 'income': 'category', 'revenue': 'category', 'head': 'category',
    'cheque': 'cheque_no', 'check': 'cheque_no', 'chq': 'cheque_no',
    'note': 'description', 'notes': 'description', 'description': 'description',
    'desc': 'description', 'memo': 'description', 'remark': 'description',
    'remarks': 'description', 'narration': 'description',
}
_UPD_FIELD_RE = re.compile(
    r'\b(' + '|'.join(sorted(_UPD_FIELD_MAP, key=len, reverse=True)) + r')\b'
    r'\s*(?:to|as|=|:)?\s*', re.IGNORECASE)

# Short fields whose value ends at its own natural boundary. Anything after it
# is a fresh field, comma or no comma. Long fields (party / bank / category /
# description) run to the next comma instead, because their values legitimately
# contain field keywords: "bank Bank of America 9523",
# "category EXPENSE/Repair and Maintenance", "note check with client".
_UPD_SHORT_VALUE = {
    'amount': re.compile(r'\$?\s*-?\d[\d,]*(?:\.\d+)?'),
    'transaction_date': re.compile(
        r'\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}'
        r'|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{2,4}'
        r'|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s*\d{2,4}', re.IGNORECASE),
    'cheque_no': re.compile(r'[A-Za-z0-9][A-Za-z0-9\-/]*'),
}
_SEGMENT_END_RE = re.compile(r'[,;\n]')


def _parse_update_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'update 260902000001 amount 500, category Printing, date 06/10/2026'
    Returns {"at_id": ..., "fields": {...}} or None.

    Scans left to right and consumes each value before looking for the next
    keyword, so a keyword sitting inside a value is never mistaken for a new
    field.
    """
    m = _UPDATE_CMD_RE.match(msg or '')
    if not m:
        return None
    at_id = m.group('id')
    rest = (m.group('rest') or '').strip(' ,;:-')
    if not rest:
        return {"at_id": at_id, "fields": {}}

    fields: Dict[str, str] = {}
    pos = 0
    while pos < len(rest):
        mk = _UPD_FIELD_RE.search(rest, pos)
        if not mk:
            break
        key = _UPD_FIELD_MAP[mk.group(1).lower()]
        vstart = mk.end()
        short = _UPD_SHORT_VALUE.get(key)
        vm = short.match(rest, vstart) if short else None
        if vm and vm.group().strip():
            val, pos = vm.group(), vm.end()
        else:
            sep = _SEGMENT_END_RE.search(rest, vstart)
            end = sep.start() if sep else len(rest)
            val, pos = rest[vstart:end], end + 1
        val = val.strip().strip(' ,;')
        if val and key not in fields:
            fields[key] = val

    if not fields:
        return {"at_id": at_id, "fields": {}, "unparsed_tail": rest}
    return {"at_id": at_id, "fields": fields}


_PROFILE_KIND_MAP = {
    'customer': P_TYPE_CUSTOMER, 'client': P_TYPE_CUSTOMER, 'buyer': P_TYPE_CUSTOMER,
    'vendor': P_TYPE_VENDOR, 'payee': P_TYPE_VENDOR, 'supplier': P_TYPE_VENDOR,
    'employee': P_TYPE_EMPLOYEE, 'staff': P_TYPE_EMPLOYEE, 'worker': P_TYPE_EMPLOYEE,
    'other': P_TYPE_OTHER, 'party': P_TYPE_OTHER, 'profile': P_TYPE_OTHER,
}
_PROFILE_CMD_RE = re.compile(
    r'^\s*(?:new|add|create|register|make|setup|set\s+up)\s+'
    r'(?P<kind>' + '|'.join(_PROFILE_KIND_MAP) + r')\b\s*[:\-]?\s*'
    r'(?P<rest>.+)$', re.IGNORECASE | re.DOTALL)

_PROFILE_FIELD_MAP = {
    'email': 'email', 'e-mail': 'email', 'mail': 'email',
    'phone': 'phone', 'tel': 'phone', 'telephone': 'phone', 'mobile': 'phone', 'cell': 'phone',
    'fax': 'fax',
    'address': 'address', 'street': 'address',
    'city': 'city', 'state': 'state', 'zip': 'zipcode', 'zipcode': 'zipcode', 'postcode': 'zipcode',
    'tax': 'sale_tax_no', 'sales tax': 'sale_tax_no', 'sale tax': 'sale_tax_no',
    'fed': 'fedral_id_no', 'federal': 'fedral_id_no', 'ein': 'fedral_id_no', 'fein': 'fedral_id_no',
    'title': 'job_title', 'job': 'job_title', 'role': 'job_title', 'position': 'job_title',
    'account': 'p_account', 'gl': 'p_account', 'gl account': 'p_account',
    'contact': 'person_name', 'person': 'person_name', 'attn': 'person_name',
    'company': 'company_name',
    'note': 'other_desc', 'notes': 'other_desc', 'memo': 'other_desc', 'about': 'business_desc',
}
_PROFILE_FIELD_RE = re.compile(
    r'^(' + '|'.join(sorted(_PROFILE_FIELD_MAP, key=len, reverse=True)) + r')\b'
    r'\s*(?:is|=|:)?\s*', re.IGNORECASE)
# The only two attributes people run onto the name line without a comma.
# Everything else must be comma-separated, so that a name containing a field
# word ("State Farm", "Wages & Co") survives intact.
_PROFILE_INLINE_RE = re.compile(
    r'\b(e-?mail|mail|phone|tel|telephone|mobile|cell)\b\s*(?:is|=|:)?\s*',
    re.IGNORECASE)


def _parse_profile_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143'
    Returns {"p_type", "name", "fields", "unparsed"} or None.

    The name is whatever precedes the first comma, never split on a field
    keyword - company names contain words like "state", "wages" and "account".
    """
    m = _PROFILE_CMD_RE.match(msg or '')
    if not m:
        return None
    p_type = _PROFILE_KIND_MAP[m.group('kind').lower()]
    rest = (m.group('rest') or '').strip()

    segments = [s.strip().strip(' ;:-') for s in re.split(r'[,;\n]', rest)]
    segments = [s for s in segments if s]
    if not segments:
        return None

    fields: Dict[str, str] = {}
    unparsed: list = []

    def take(seg: str) -> bool:
        mk = _PROFILE_FIELD_RE.match(seg)
        if not mk:
            return False
        key = _PROFILE_FIELD_MAP[re.sub(r'\s+', ' ', mk.group(1).lower())]
        val = seg[mk.end():].strip().strip(' ,;')
        if val and key not in fields:
            fields[key] = val
        return True

    name = segments[0]
    # Only a keyword that itself names the party may consume the first segment.
    # Any other keyword there is part of the name - "State Farm", "Wages And Co".
    first = _PROFILE_FIELD_RE.match(name)
    if first and _PROFILE_FIELD_MAP[re.sub(r'\s+', ' ', first.group(1).lower())] \
            in ('company_name', 'person_name') and take(name):
        name = ''
    else:
        inline = _PROFILE_INLINE_RE.search(name)
        if inline:                       # "add vendor Acme email ops@acme.com"
            tail, name = name[inline.start():], name[:inline.start()].strip()
            for chunk in re.split(r'\s+(?=(?:e-?mail|mail|phone|tel|telephone|mobile|cell)\b)',
                                  tail, flags=re.IGNORECASE):
                if not take(chunk.strip()):
                    unparsed.append(chunk.strip())

    for seg in segments[1:]:
        if not take(seg):
            unparsed.append(seg)

    name = name.strip().strip(' ,;:-')
    if not name and not fields.get('company_name') and not fields.get('person_name'):
        return None
    return {"p_type": p_type, "name": name, "fields": fields, "unparsed": unparsed}


def resolve_party_existing(conn, name: str,
                           prefer_type: Optional[str] = None) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Like resolve_party() but NEVER creates. Used when editing a voucher - an
    edit should not conjure a profile as a side effect.
    """
    if not name or not name.strip():
        return None, "No name given."
    target = normalize_name(name)
    for search_type in ([prefer_type, None] if prefer_type else [None]):
        cands = find_party_candidates(conn, name, p_type=search_type)
        best, best_name, best_score = None, None, 0.0
        for p in cands:
            for cand in (p.get('company_name'), p.get('person_name')):
                if not cand:
                    continue
                if normalize_name(cand) == target:
                    return {**p, 'matched_name': cand}, None
                sc = name_similarity(name, cand)
                if sc > best_score:
                    best_score, best, best_name = sc, p, cand
        if best and best_score >= FUZZY_MATCH_THRESHOLD:
            return {**best, 'matched_name': best_name}, None
    return None, (f"No profile matching \"{name}\". Create it first, e.g.\n"
                  f"    new {'customer' if prefer_type == P_TYPE_CUSTOMER else 'vendor'} {name}")


# --------------------------------------------------------------------------
# DB connection
# --------------------------------------------------------------------------
def _assert_db_config():
    missing = [k for k in ('host', 'user', 'password', 'database') if not DB_CONFIG.get(k)]
    if missing:
        raise RuntimeError(
            "Missing DB configuration: " + ", ".join("DB_" + m.upper() for m in missing) +
            ". Set them in the environment / .env - credentials are no longer "
            "hardcoded. If the old password was ever committed, rotate it."
        )


def get_connection():
    _assert_db_config()
    conn = mysql.connector.connect(use_pure=True, **DB_CONFIG)
    conn.autocommit = False
    return conn


def _fetch_all(conn, query: str, params: tuple = (), context: str = "") -> List[Dict]:
    start = datetime.now()
    cur = conn.cursor(dictionary=True)
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    log_query(query, list(params), rows, (datetime.now() - start).total_seconds() * 1000, context)
    return rows


# --------------------------------------------------------------------------
# THE CHART OF ACCOUNTS
#
# v_trans_accounts_m2 returns exactly the postable leaves for one tenant:
#     childless mains  U  childless subs  U  individual accounts
# scoped by the @system_id session variable (the views call system_id()).
#
# The PHP sets that variable inline via a derived table so the whole thing is
# one statement - see cashBankAccounts().  We do the same, and additionally
# SET it on the connection first, so the lookup survives an optimiser that
# folds the derived table away.
# --------------------------------------------------------------------------
_CHART_SQL = """
    SELECT trans_acc_id, trans_acc_desc, subsi_acc_id, subsi_acc_desc, trans_system_id
    FROM (SELECT @system_id := %s p) parm, v_trans_accounts_m2
    WHERE trans_acc_display = 1
    ORDER BY SUBSTRING(trans_acc_id, 1, 1),
             SUBSTRING(trans_acc_id, 1, 5),
             acc_main_sort, sub_sort, acc_sort, trans_acc_id
"""

_chart_cache: Dict[str, Any] = {"rows": None, "at": 0.0}


def account_level(code: Any) -> str:
    """Hierarchy level implied by the code length (see transactionableAccount)."""
    n = len(str(code))
    return {1: 'nature', 5: 'main', 9: 'sub', 13: 'individual'}.get(n, f'unknown({n})')


def account_nature(code: Any) -> str:
    """Nature = first digit of the code. Used by trialBalanace / inlineProfitLoss."""
    s = str(code)
    return s[0] if s else ''


def qualified_name(row: Dict) -> str:
    """
    The label the UI shows: "PARENT/LEAF".
    Assembled exactly as custTransDetail() / voucherListSummary() do:
        subsi_acc_desc + "/" + trans_acc_desc
    e.g. "EXPENSE/Repair and Maintenance", "Payroll Taxes/FICA".
    """
    parent = (row.get('subsi_acc_desc') or '').strip()
    leaf = (row.get('trans_acc_desc') or '').strip()
    return f"{parent}/{leaf}" if parent else leaf


def get_chart(conn, force: bool = False) -> List[Dict]:
    """All postable accounts for SYSTEM_ID, cached for CHART_CACHE_TTL seconds."""
    now = time.time()
    if (not force and _chart_cache["rows"] is not None
            and now - _chart_cache["at"] < CHART_CACHE_TTL):
        return _chart_cache["rows"]

    cur = conn.cursor()
    try:
        cur.execute("SET @system_id = %s", (SYSTEM_ID,))
    except Exception:
        pass
    finally:
        cur.close()

    rows = _fetch_all(conn, _CHART_SQL, (SYSTEM_ID,), "get_chart")
    for r in rows:
        r['code'] = str(r['trans_acc_id'])
        r['desc'] = (r.get('trans_acc_desc') or '').strip()
        r['parent_desc'] = (r.get('subsi_acc_desc') or '').strip()
        r['qualified'] = qualified_name(r)
        r['level'] = account_level(r['code'])
        r['nature'] = account_nature(r['code'])

    _chart_cache["rows"] = rows
    _chart_cache["at"] = now
    return rows


def chart_by_nature(conn, natures: set) -> List[Dict]:
    return [r for r in get_chart(conn) if r['nature'] in natures]


def find_account(conn, code: Any) -> Optional[Dict]:
    code = str(code)
    return next((r for r in get_chart(conn) if r['code'] == code), None)


# --------------------------------------------------------------------------
# Port of admin_model::transactionableAccount()
#
# Returns False when the node HAS children (i.e. it is a group/rollup and must
# not be posted to).  PHP throws "Check Chart of Account" on false.
# --------------------------------------------------------------------------
_CHILD_CHECKS = {
    1: ("SELECT 1 FROM account_nature_user anu "
        "JOIN account_main_user amu ON anu.acc_nature_id = amu.acc_nature_id "
        "  AND anu.acc_nature_system_id = amu.acc_main_system_id "
        "WHERE anu.acc_nature_id = %s AND anu.acc_nature_system_id = %s LIMIT 1"),
    5: ("SELECT 1 FROM account_main_user acm "
        "JOIN account_sub_user asu ON acm.acc_main_system_id = asu.acc_sub_system_id "
        "  AND acm.acc_main_id = asu.acc_main_id "
        "WHERE acm.acc_main_id = %s AND acm.acc_main_system_id = %s LIMIT 1"),
    9: ("SELECT 1 FROM account_sub_user asu "
        "JOIN accounts_user au ON asu.acc_sub_system_id = au.acc_system_id "
        "  AND asu.acc_sub_id = au.acc_sub_id "
        "WHERE asu.acc_sub_id = %s AND asu.acc_sub_system_id = %s LIMIT 1"),
}


def transactionable_account(conn, code: Any) -> bool:
    code = str(code)
    sql = _CHILD_CHECKS.get(len(code))
    if not sql:
        return True                     # 13-digit individual accounts are always leaves
    cur = conn.cursor()
    cur.execute(sql, (code, SYSTEM_ID))
    has_children = cur.fetchone() is not None
    cur.close()
    return not has_children


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
class Resolution:
    """A resolved, POSTABLE account."""

    def __init__(self, row: Dict, match_type: str, score: float):
        self.code = row['code']              # -> at_acc_code / at_bank_acc
        self.desc = row['desc']              # leaf description
        self.qualified = row['qualified']    # "PARENT/LEAF" as the UI shows it
        self.parent_desc = row['parent_desc']
        self.level = row['level']            # main | sub | individual
        self.nature = row['nature']
        self.match_type = match_type
        self.score = score

    def __repr__(self):
        return f"<Resolution {self.code} {self.qualified} ({self.match_type})>"


def _candidate_names(row: Dict) -> List[str]:
    """Every string a user might reasonably type for this account."""
    names = [row['desc'], row['qualified']]
    return [n for n in names if n]


def _match_account(search_text: str,
                   candidates: List[Dict]) -> Tuple[Optional[Dict], float, str, bool]:
    """
    Returns (row, score, match_type, ambiguous).

    Tries, in order:
      1. exact on the qualified "PARENT/LEAF" name
      2. exact on the bare leaf name
      3. exact on the number-stripped leaf name
      4. substring, disambiguated by any identifying number in the query
      5. fuzzy, refusing when the top two are within AMBIGUITY_MARGIN
    """
    if not search_text or not search_text.strip():
        return None, 0.0, 'none', False

    norm_search = normalize_name(search_text)
    stripped_search = _strip_standalone_numbers(norm_search)
    search_numbers = [t for t in norm_search.split() if t.isdigit()]

    # 0) the user pasted a raw account code
    if norm_search.isdigit():
        for row in candidates:
            if row['code'] == norm_search:
                return row, 1.0, 'code', False

    # 1 + 2) exact on qualified or bare name
    for row in candidates:
        for name in _candidate_names(row):
            if normalize_name(name) == norm_search:
                return row, 1.0, 'exact', False

    # 3) exact once identifying numbers are removed
    if stripped_search:
        for row in candidates:
            for name in _candidate_names(row):
                if normalize_name(name) == stripped_search:
                    return row, 1.0, 'exact', False

    # 4) substring
    if stripped_search:
        matches = []
        for row in candidates:
            for name in _candidate_names(row):
                nm = normalize_name(name)
                if stripped_search in nm or nm in stripped_search:
                    matches.append(row)
                    break

        if search_numbers and len(matches) > 1:
            num_matches = [
                row for row in matches
                if any(n in normalize_name(row['desc']).split() for n in search_numbers)
            ]
            if len(num_matches) == 1:
                return num_matches[0], 0.98, 'substring_number', False
            if len(num_matches) > 1:
                matches = num_matches

        if len(matches) == 1:
            return matches[0], 0.95, 'substring', False
        if len(matches) > 1:
            matches.sort(key=lambda r: len(r['desc']))
            return matches[0], 0.90, 'substring', True

    # 5) fuzzy on the best of each account's names
    scored = []
    for row in candidates:
        best = max(name_similarity(search_text, n) for n in _candidate_names(row))
        scored.append((best, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored and scored[0][0] >= FUZZY_MATCH_THRESHOLD:
        best_score, best_row = scored[0]
        ambiguous = len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_MARGIN
        return best_row, best_score, 'fuzzy', ambiguous

    return None, 0.0, 'none', False


def _resolve(conn, search_text: str, candidates: List[Dict],
             what: str) -> Tuple[Optional[Resolution], Optional[str]]:
    if not search_text or not search_text.strip():
        return None, f"No {what} account name provided."

    row, score, mtype, ambiguous = _match_account(search_text, candidates)

    if ambiguous:
        near = [r['qualified'] for _, r in
                sorted(((max(name_similarity(search_text, n) for n in _candidate_names(c)), c)
                        for c in candidates), key=lambda x: x[0], reverse=True)[:4]]
        return None, (f"\"{search_text}\" matches more than one account "
                      f"({', '.join(near)}). Please use the exact account name.")

    if not row:
        return None, (f"I couldn't find a postable {what} account matching "
                      f"\"{search_text}\". Use the name as it appears in your chart of "
                      f"accounts - either \"Repair and Maintenance\" or the full "
                      f"\"EXPENSE/Repair and Maintenance\".")

    if not transactionable_account(conn, row['code']):
        return None, (f"\"{row['qualified']}\" is a group account with child accounts "
                      f"under it and can't be posted to directly. Please name the "
                      f"specific account.")

    return Resolution(row, mtype, score), None


def resolve_bank_account(conn, search_text: str,
                         allow_default: bool) -> Tuple[Optional[Resolution], Optional[str]]:
    """
    The cash/bank/contra leg.  Deliberately NOT restricted to assets - the host
    app's own bank dropdown (cashBankAccounts) offers every postable account,
    which is how "Paid $500 to ABC Corp from Loan to Shareholders" works.
    """
    candidates = get_chart(conn)
    if not candidates:
        return None, _empty_chart_message()

    if not search_text or not search_text.strip():
        if allow_default:
            return _default_bank_resolution(conn)
        return None, ("I couldn't tell which account the money moved through. "
                      "Name it explicitly, e.g. \"...via Bank of America 9523\".")

    res, err = _resolve(conn, search_text, candidates, "bank/cash")
    if res:
        return res, None

    # Ambiguous bank text -> use the configured default instead of refusing.
    # Set DEFAULT_BANK_ACC=100045001 to make "Bank of America" (or any other
    # phrase that hits several bank accounts) land on Bank of Amercia 9523.
    # match_type 'default_on_conflict' keeps it out of the exact/code/default
    # allow-list, so the voucher is still flagged for review.
    if DEFAULT_BANK_ACC:
        _row, _score, _mtype, _ambiguous = _match_account(search_text, candidates)
        if _ambiguous:
            fallback = find_account(conn, DEFAULT_BANK_ACC)
            if fallback:
                print(f"NOTE: \"{search_text}\" was ambiguous; using "
                      f"DEFAULT_BANK_ACC {DEFAULT_BANK_ACC} "
                      f"({fallback['qualified']}).")
                return Resolution(fallback, 'default_on_conflict', 0.0), None

    norm = normalize_name(search_text)
    if allow_default and any(p == norm or p in norm for p in _GENERIC_BANK_PHRASES):
        return _default_bank_resolution(conn)
    return None, err


def resolve_category_account(conn, search_text: str,
                             natures: set) -> Tuple[Optional[Resolution], Optional[str]]:
    """
    The income (CRV) or expense (CPV) leg, restricted by nature so a payment can
    never land on a revenue account or vice versa.

    Returns (None, None)  -> no accounts of that nature exist at all
            (None, "")    -> nothing to search on; caller should default
    """
    candidates = chart_by_nature(conn, natures)
    if not candidates:
        return None, None
    if not search_text or not search_text.strip():
        return None, ""
    return _resolve(conn, search_text, candidates, "category")


def _empty_chart_message() -> str:
    return (f"No postable accounts came back for system_id {SYSTEM_ID}. Check that "
            f"v_trans_accounts_m2 exists and that this company's chart is assigned "
            f"in accounts_user / account_sub_user / account_main_user. "
            f"GET /api/debug/chart shows what the view returns.")


def _default_bank_resolution(conn) -> Tuple[Optional[Resolution], Optional[str]]:
    if DEFAULT_BANK_ACC:
        row = find_account(conn, DEFAULT_BANK_ACC)
        if row:
            return Resolution(row, 'default', 0.0), None
        return None, (f"DEFAULT_BANK_ACC is set to {DEFAULT_BANK_ACC}, but that is not "
                      f"a postable account for this company.")
    return None, ("No default bank account is configured, so I can't guess where the "
                  "money moved. Either name the account in your message or set "
                  "DEFAULT_BANK_ACC to a specific code (e.g. 100045001).")


def _default_category_resolution(conn, entry_type: str,
                                 natures: set) -> Tuple[Optional[Resolution], Optional[str]]:
    configured = DEFAULT_REVENUE_ACC if entry_type == 'CRV' else DEFAULT_EXPENSE_ACC
    if configured:
        row = find_account(conn, configured)
        if row and row['nature'] in natures:
            return Resolution(row, 'default', 0.0), None

    candidates = chart_by_nature(conn, natures)
    if not candidates:
        return None, None
    for row in candidates:
        if 'miscellaneous' in row['desc'].lower():
            return Resolution(row, 'default', 0.0), None

    kind = 'revenue' if entry_type == 'CRV' else 'expense'
    return None, (f"I couldn't work out which {kind} account this belongs to. Name the "
                  f"category in your message, or set "
                  f"{'DEFAULT_REVENUE_ACC' if entry_type == 'CRV' else 'DEFAULT_EXPENSE_ACC'}.")


# --------------------------------------------------------------------------
# Fiscal year (business_year) - port of validate_transaction_within_fiscalyear
# --------------------------------------------------------------------------
def within_open_year(conn, trans_date) -> bool:
    query = (
        "SELECT 1 FROM business_year "
        "WHERE by_system_id = %s AND ("
        "  (financial_year_start <= %s AND %s <= financial_year_end AND financial_year_status = 1)"
        "  OR (audit_year_start <= %s AND %s <= audit_year_end AND audit_year_status = 1)"
        ") LIMIT 1"
    )
    d = trans_date.isoformat() if hasattr(trans_date, 'isoformat') else str(trans_date)
    try:
        rows = _fetch_all(conn, query, (SYSTEM_ID, d, d, d, d), "within_open_year")
        return bool(rows)
    except Exception as e:
        print(f"Fiscal-year check skipped: {e}")
        return True


# --------------------------------------------------------------------------
# Parties
# --------------------------------------------------------------------------
def find_party_candidates(conn, name: str, p_type: Optional[str] = None,
                          limit: int = 200) -> List[Dict]:
    if not name or not name.strip():
        return []
    name_stripped = name.strip()
    cur = conn.cursor(dictionary=True)
    seen: Dict[Any, Dict] = {}

    base = ("SELECT p_code, p_type, company_name, person_name, Phone, Email, p_account "
            "FROM acc_party WHERE status = 1 AND system_id = %s")
    type_clause = " AND p_type = %s" if p_type else ""
    type_param = (p_type,) if p_type else ()

    queries = [
        (base + type_clause +
         " AND (LOWER(TRIM(company_name))=LOWER(TRIM(%s)) OR LOWER(TRIM(person_name))=LOWER(TRIM(%s)))",
         (SYSTEM_ID,) + type_param + (name_stripped, name_stripped)),
        (base + type_clause + " AND (company_name LIKE %s OR person_name LIKE %s) LIMIT %s",
         (SYSTEM_ID,) + type_param + (name_stripped + "%", name_stripped + "%", limit)),
        (base + type_clause + " AND (company_name LIKE %s OR person_name LIKE %s) LIMIT %s",
         (SYSTEM_ID,) + type_param + ("%" + name_stripped + "%", "%" + name_stripped + "%", limit)),
    ]
    first_token = name_stripped.split()[0] if name_stripped.split() else name_stripped
    if len(first_token) >= 3:
        queries.append(
            (base + type_clause + " AND (company_name LIKE %s OR person_name LIKE %s) LIMIT %s",
             (SYSTEM_ID,) + type_param + ("%" + first_token + "%", "%" + first_token + "%", limit)))

    for q, params in queries:
        start = datetime.now()
        cur.execute(q, params)
        rows = cur.fetchall()
        log_query(q, params, rows, (datetime.now() - start).total_seconds() * 1000,
                  f"find_party_candidates({name})")
        for r in rows:
            seen[(r['p_code'], r['p_type'])] = r
    cur.close()
    return list(seen.values())


def _next_party_code(cur, p_type: str) -> str:
    """
    Mirrors admin_model::addAccount() - seed from the type's control-account
    prefix, then take MAX(p_code)+1 within that p_type and system.
    """
    seed = int(PARTY_CODE_PREFIX[p_type] + "00001")
    cur.execute(
        "SELECT IFNULL(MAX(p_code), 0) FROM acc_party WHERE p_type = %s AND system_id = %s",
        (p_type, SYSTEM_ID),
    )
    current = int(cur.fetchone()[0] or 0)
    return str(max(seed, current + 1))


def create_party(conn, name: str, p_type: str) -> Tuple[str, str]:
    display_name = title_case_name(name)
    target_norm = normalize_name(display_name)

    for p in find_party_candidates(conn, display_name, p_type=p_type, limit=20):
        for cand in (p.get('company_name'), p.get('person_name')):
            if cand and normalize_name(cand) == target_norm:
                return str(p['p_code']), cand

    cur = conn.cursor()
    new_code = _next_party_code(cur, p_type)
    query = ("INSERT INTO acc_party (p_code, p_type, company_name, person_name, "
             "status, system_id) VALUES (%s, %s, %s, %s, 1, %s)")
    start = datetime.now()
    cur.execute(query, (new_code, p_type, display_name, display_name, SYSTEM_ID))
    conn.commit()
    cur.close()
    log_query(query, [new_code, p_type, display_name, display_name, SYSTEM_ID],
              ['INSERT OK'], (datetime.now() - start).total_seconds() * 1000, "create_party")
    return new_code, display_name


def resolve_party(conn, name: str, entry_type: str) -> Tuple[str, str, str, float, str]:
    """Returns (p_code, display_name, match_type, score, p_type)."""
    if not name or not name.strip():
        raise ValueError("Empty party name")

    p_type = P_TYPE_CUSTOMER if entry_type == 'CRV' else P_TYPE_VENDOR

    # Prefer a party already of the right type; fall back to any type so we
    # don't create a duplicate of an existing employee/other.
    for search_type in (p_type, None):
        candidates = find_party_candidates(conn, name, p_type=search_type)
        target_norm = normalize_name(name)
        best_row, best_name, best_score = None, None, 0.0
        for p in candidates:
            for cand in (p.get('company_name'), p.get('person_name')):
                if not cand:
                    continue
                if normalize_name(cand) == target_norm:
                    return str(p['p_code']), cand, 'exact', 1.0, p['p_type']
                score = name_similarity(name, cand)
                if score > best_score:
                    best_score, best_row, best_name = score, p, cand
        if best_row is not None and best_score >= FUZZY_MATCH_THRESHOLD:
            return (str(best_row['p_code']), best_name, 'fuzzy',
                    best_score, best_row['p_type'])

    p_code, display_name = create_party(conn, name, p_type)
    return p_code, display_name, 'new', 0.0, p_type


# --------------------------------------------------------------------------
# Double entry - the one place polarity is defined
# --------------------------------------------------------------------------
def _build_legs(entry_type: str, bank_code: str, category_code: str,
                amount: float) -> List[Dict]:
    """
    CRV (receipt):  Dr Bank    / Cr Revenue
    CPV (payment):  Dr Expense / Cr Bank

    Debit leg carries +amount, credit leg -amount, matching the host app
    (addIncomeTest / addPayment1 / addjv all store credits negative).
    """
    if entry_type == 'CRV':
        return [
            {'sno': 1, 'code': bank_code,     'dc': 'D', 'amount': amount},
            {'sno': 2, 'code': category_code, 'dc': 'C', 'amount': -amount},
        ]
    return [
        {'sno': 1, 'code': category_code, 'dc': 'D', 'amount': amount},
        {'sno': 2, 'code': bank_code,     'dc': 'C', 'amount': -amount},
    ]


def _next_voucher_id(cur, doc_type: str) -> int:
    """
    Mirrors PHP exactly:
        $aid = date('ym') . DOC_TYPE . '000001';
        SELECT ifnull(max(at_id),0) FROM acc_trans_m
         WHERE at_doc_type = ? AND system_id = ?
        if (max + 1 > $aid) $aid = max + 1;

    The structure matters: other pages read the doc type back out of the id,
    e.g. invoice_list() does substring(idt_at_id, 5, 2) to choose crv.php
    vs cpv.php.
    """
    seed = int(f"{datetime.now():%y%m}{doc_type}000001")
    cur.execute(
        "SELECT IFNULL(MAX(at_id), 0) FROM acc_trans_m "
        "WHERE at_doc_type = %s AND system_id = %s",
        (doc_type, SYSTEM_ID),
    )
    current = int(cur.fetchone()[0] or 0)
    return max(seed, current + 1)


def insert_voucher(conn, entry_type: str, party_code, bank_res: Resolution,
                   cat_res: Resolution, amount, trans_date, description,
                   cheque_no=None):
    doc_type = DOC_TYPE_CRV if entry_type == 'CRV' else DOC_TYPE_CPV
    legs = _build_legs(entry_type, bank_res.code, cat_res.code, amount)

    # PHP puts a fixed label in at_desc and the user's text in at_remarks;
    # voucher lists render at_desc, so free text there looks wrong.
    at_desc = 'Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher'

    cur = conn.cursor()
    now = datetime.now()
    cheque_date = trans_date if cheque_no else None
    bank_label = (bank_res.desc or bank_res.code)[:AT_BANK_LABEL_MAX_LEN]
    bank_acc_val = bank_res.code[:AT_BANK_ACC_MAX_LEN]

    try:
        start = datetime.now()
        new_id = _next_voucher_id(cur, doc_type)

        master_sql = """
            INSERT INTO acc_trans_m (
                at_id, at_date, at_fiscal_year, at_desc, at_doc_type,
                at_f_key, at_ref_type, at_pmode, at_bank, at_bank_branch,
                at_bank_acc, at_cheque_no, at_cheque_date, at_address,
                at_memo, at_m_amount, at_party_code, at_remarks,
                at_status, at_cr_user, at_cr_date, at_up_user, at_up_date,
                system_id, tag_status, include_in_billing,
                exclude_billing_new, check_in_reconcile
            ) VALUES (
                %s,%s,NULL,%s,%s, NULL,NULL,%s,%s,NULL,
                %s,%s,%s,NULL, %s,%s,%s,%s,
                1,%s,%s,NULL,NULL, %s,0,%s,0,0
            )
        """
        cur.execute(master_sql, (
            new_id, trans_date, at_desc, doc_type, PMODE[doc_type],
            bank_label, bank_acc_val, cheque_no, cheque_date,
            description, amount, party_code, description,
            USER_ID, now, SYSTEM_ID, INCLUDE_IN_BILLING,
        ))

        detail_sql = (
            "INSERT INTO acc_trans_d "
            "(at_id,at_sno,at_acc_code,at_dc_type,at_amount,"
            " at_qty,at_remarks,atd_date,atd_party,"
            " at_system_id,at_acc_reconcile,doc_path,at_sno_by_user) "
            "VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,0,NULL,0)"
        )
        # See module docstring: a 1:1 mirror is correct only for two-leg
        # vouchers. PHP regroups the bank side for multi-line entries.
        reconcile_sql = (
            "INSERT INTO acc_trans_reconcile "
            "(at_id,at_sno,at_acc_code,at_dc_type,at_amount,"
            " at_remarks,atd_date,atd_party,at_system_id,"
            " at_sno_by_user,at_acc_reconcile,at_rec_id,at_rec_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,NULL,NULL)"
        )
        for leg in legs:
            cur.execute(detail_sql, (new_id, leg['sno'], leg['code'], leg['dc'],
                                     leg['amount'], description, trans_date,
                                     party_code, SYSTEM_ID))
            cur.execute(reconcile_sql, (new_id, leg['sno'], leg['code'], leg['dc'],
                                        leg['amount'], description, trans_date,
                                        party_code, SYSTEM_ID))

        cur.execute(
            "INSERT INTO voucher_cousting "
            "(system_id,voucher_id,ref_id,voucher_type,start_date_time,end_date_time,time_spent) "
            "VALUES (%s,%s,NULL,%s,%s,%s,0) "
            "ON DUPLICATE KEY UPDATE end_date_time = VALUES(end_date_time)",
            (SYSTEM_ID, new_id, doc_type, now, now),
        )
        conn.commit()
        log_query(f"INSERT {entry_type}",
                  [new_id, party_code, bank_res.code, cat_res.code, amount],
                  ['COMMIT OK'], (datetime.now() - start).total_seconds() * 1000,
                  f"insert_voucher({new_id})")
        cur.close()
        return new_id
    except Exception:
        conn.rollback()
        cur.close()
        raise


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
class AccountingBot:
    def __init__(self):
        self.name = "LedgerAssist"
        api_key = os.getenv("ACCOUNTING_GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
        if api_key:
            self.groq_client = Groq(api_key=api_key)
            self.model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            print("Groq client initialized")
        else:
            self.groq_client = None
            print("Groq API key not set")
        self._sessions: Dict[str, Any] = {}

    def get_session_db(self, session_id: str):
        conn = self._sessions.get(session_id)
        if conn is None or not conn.is_connected():
            conn = get_connection()
            self._sessions[session_id] = conn
        return conn

    def is_greeting(self, message: str) -> bool:
        greetings = ['hello', 'hi', 'hey', 'greetings', 'good morning',
                     'good afternoon', 'good evening', 'howdy', 'yo']
        m = message.lower().strip()
        return m in greetings or m in [g + '!' for g in greetings]

    def is_help(self, message: str) -> bool:
        return any(w in message.lower() for w in
                   ['help', 'what can you do', 'how do you work', 'capabilities',
                    'what do you do', 'guide', 'tutorial'])

    def is_financial_analysis(self, message: str) -> bool:
        return any(w in message.lower() for w in
                   ['financial', 'analysis', 'summary', 'overview',
                    'how am i doing', 'performance', 'health', 'report'])

    def is_query_records(self, message: str) -> bool:
        return any(w in message.lower() for w in
                   ['show', 'view', 'list', 'records', 'transactions', 'history', 'last'])

    def get_help_response(self) -> Dict:
        text = (
            'Everything is typed here. Four things I can do:\n\n'
            'RECORD  - posts a CRV or CPV immediately\n'
            '  06/04/2026 ACH DEPOSIT - JOHN SMITH 659.25\n'
            '  Received $659.25 from John Smith today via Bank of America 9523\n'
            '  Paid $300 to XYZ Ltd for Office Supplies and Expense yesterday\n'
            '  Paid $700 to XYZ Ltd for EXPENSE/Repair and Maintenance, cheque 4521\n\n'
            'REVIEW / EDIT - name the voucher by its id\n'
            '  show 260902000001\n'
            '  update 260902000001 amount 500\n'
            '  update 260902000001 category Printing, date 06/10/2026\n'
            '  update 260902000001 party Handy Fix LLC, bank Bank of America 9523\n'
            '  void 260902000001\n'
            '  Editable fields: amount, date, party, bank, category,\n'
            '  cheque no, description. Only the fields you name change.\n\n'
            'PROFILES - create a party before you need it\n'
            '  add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143\n'
            '  new customer Acme Corp\n'
            '  add employee Maria Lopez, phone 555-0192\n'
            '  Kinds: customer, vendor/payee, employee, other.\n\n'
            'LOOK UP\n'
            '  show my transactions\n'
            '  show my financial summary\n\n'
            'Account names work either way - "Repair and Maintenance" or the\n'
            'full "EXPENSE/Repair and Maintenance" as your reports show it.'
        )
        return {'status': 'help', 'message': text, 'analysis': text, 'confidence': 'high'}

    async def get_financial_analysis(self, session_id: str) -> Dict:
        conn = self.get_session_db(session_id)
        query = ("SELECT at_doc_type, COUNT(*) AS cnt, SUM(at_m_amount) AS total "
                 "FROM acc_trans_m WHERE at_doc_type IN (%s,%s) AND system_id = %s "
                 "AND at_status >= 1 GROUP BY at_doc_type")
        rows = _fetch_all(conn, query, (DOC_TYPE_CRV, DOC_TYPE_CPV, SYSTEM_ID),
                          "get_financial_analysis")
        income = next((r['total'] for r in rows if r['at_doc_type'] == DOC_TYPE_CRV), 0) or 0
        expense = next((r['total'] for r in rows if r['at_doc_type'] == DOC_TYPE_CPV), 0) or 0
        net = float(income) - float(expense)
        text = (f"Financial Overview\n\n"
                f"Total Income  (CRV): ${float(income):,.2f}\n"
                f"Total Expenses(CPV): ${float(expense):,.2f}\n"
                f"Net Cash Flow:       ${net:,.2f}")
        return {'status': 'financial_analysis', 'message': text, 'analysis': text,
                'confidence': 'high'}

    async def get_records(self, session_id: str, limit: int = 10) -> Dict:
        conn = self.get_session_db(session_id)
        query = ("SELECT at_id, at_doc_type, at_desc, at_remarks, at_m_amount, at_date "
                 "FROM acc_trans_m WHERE at_doc_type IN (%s,%s) AND system_id = %s "
                 "AND at_status >= 1 ORDER BY at_id DESC LIMIT %s")
        rows = _fetch_all(conn, query, (DOC_TYPE_CRV, DOC_TYPE_CPV, SYSTEM_ID, limit),
                          "get_records")
        if not rows:
            text = "No transactions recorded yet."
        else:
            lines = ["Recent Transactions\n"]
            for r in rows:
                label = 'CRV' if r['at_doc_type'] == DOC_TYPE_CRV else 'CPV'
                note = r.get('at_remarks') or r.get('at_desc') or ''
                lines.append(f" {label}-{r['at_id']} - ${float(r['at_m_amount']):,.2f} - "
                             f"{note} ({r['at_date']})")
            text = "\n".join(lines)
        return {'status': 'query_records', 'message': text, 'analysis': text,
                'confidence': 'high'}

    async def extract_transaction_info(self, message: str, session_id: str) -> Dict:
        prompt = f"""
Extract financial transaction information from this message.

User message: "{message}"

IMPORTANT RULES:
1. If the user mentions ANY account name (like "Current Assets", "Loan to Shareholders",
   "Bank of America 9523", "Cash on Hand"), extract it EXACTLY as `bank_text`.
2. The account name can appear ANYWHERE - before the party name, after, with dashes,
   colons, or commas.
3. Account names in this system are sometimes written as "CATEGORY/Account Name",
   e.g. "EXPENSE/Repair and Maintenance" or "REVENUE/Healthcare Services". If the
   user writes that form, keep it EXACTLY as-is including the slash.
4. Examples:
   - "06/04/2026 Current Assets - JOHN SMITH 659.25" -> bank_text: "Current Assets"
   - "Paid $500 to ABC Corp from Loan to Shareholders" -> bank_text: "Loan to Shareholders"
   - "Paid $300 for EXPENSE/Website Development" -> category_hint: "EXPENSE/Website Development"
   - "ACH DEPOSIT - JOHN SMITH 659.25" -> NO bank_text (it's just a description)
5. Do NOT invent account names. Only extract what the user actually wrote.
6. CRITICAL: If an account name includes an identifying NUMBER (e.g. the last 4 digits
   of a bank account, like "Bank of America 9523"), you MUST keep that number. It
   distinguishes it from similarly-named accounts. Never drop it.
   - "Bank of America 9523 JOHN SMITH 3435.6" -> bank_text: "Bank of America 9523"

Return ONLY valid JSON with these fields:
- action: "create_transaction", "help", "financial_analysis", "query_records", or "error"
- entry_type: "CRV" (money received/incoming) or "CPV" (money paid/outgoing)
- amount: number
- party_name: the full company or person name, normalized to Title Case
- description: short description of what the transaction is for
- transaction_date: in strict "YYYY-MM-DD" if mentioned; omit if not mentioned
- cheque_number: cheque/check number as a string if mentioned; omit if not
- bank_text: the RAW text the user used for the bank/cash/source account. Omit if none.
- category_hint: the RAW text for the income/expense category. Omit if none.

If NOT a financial transaction return {{"action": "help"}}.
Return ONLY valid JSON, no markdown, no extra text.
"""
        # Deterministic parse first - it costs nothing and gives us a floor.
        rules = _rule_based_extract(message)
        llm: Dict[str, Any] = {}
        llm_error: Optional[str] = None

        if self.groq_client:
            try:
                response = self.groq_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=400,
                )
                raw = response.choices[0].message.content.strip()
                raw = re.sub(r'^```(?:json)?\s*', '', raw)
                raw = re.sub(r'\s*```$', '', raw)
                llm = json.loads(raw)
                for key in ('party_name', 'description', 'bank_text', 'category_hint'):
                    if isinstance(llm.get(key), str):
                        llm[key] = strip_emojis(llm[key])
                print(f"DEBUG: LLM extracted: {llm}")
            except Exception as e:
                llm_error = f"{type(e).__name__}: {e}"
                print(f"Groq extraction error: {llm_error}")
        else:
            llm_error = ("no API key (set ACCOUNTING_GROQ_API_KEY or GROQ_API_KEY)")

        llm_ok = (llm.get('action') == 'create_transaction'
                  and llm.get('entry_type') and llm.get('amount')
                  and llm.get('party_name'))
        rules_ok = rules.get('action') == 'create_transaction'

        # Statement line with no in/out signal: accept an LLM direction if we
        # have one, but mark it inferred so the confirmation flags it.
        if rules.get('action') == 'ambiguous_direction':
            if llm_ok:
                extracted = dict(llm)
                p = rules.get('_parsed') or {}
                extracted.setdefault('transaction_date', p.get('date'))
                if p.get('account'):
                    extracted.setdefault('account_text', p['account'])
                extracted['_source'] = 'llm+rules'
                extracted['_direction_inferred'] = True
                log_llm_extraction(extracted, message, session_id)
                return extracted
            extracted = dict(rules)
            if llm_error:
                extracted['_llm_error'] = llm_error
            log_llm_extraction(extracted, message, session_id)
            return extracted

        if llm_ok:
            # LLM wins, but backfill anything it dropped from the rules.
            extracted = dict(rules) if rules_ok else {}
            extracted.update({k: v for k, v in llm.items() if v not in (None, '')})
            extracted['_source'] = 'llm+rules' if rules_ok else 'llm'
        elif rules_ok:
            extracted = rules
            extracted['_source'] = 'rules'
            if llm_error:
                extracted['_llm_error'] = llm_error
                print(f"NOTE: LLM unavailable, used rule-based parse. {llm_error}")
        else:
            # Genuinely couldn't parse it. Say so - don't disguise it as help.
            extracted = {"action": "unparsed", "_source": "none"}
            if llm_error:
                extracted['_llm_error'] = llm_error
            if isinstance(llm, dict) and llm.get('action') in (
                    'help', 'financial_analysis', 'query_records', 'error'):
                extracted['action'] = llm['action']

        log_llm_extraction(extracted, message, session_id)
        return extracted

    # ---------------- chat handlers for v6 features ----------------
    @staticmethod
    def _voucher_card(v: Dict, extra_note: Optional[str] = None) -> Dict:
        """Structured payload the client renders as a record card."""
        return {
            "kind": "voucher",
            "at_id": v.get("at_id"),
            "voucher_number": v.get("voucher_number"),
            "entry_type": v.get("entry_type"),
            "amount": v.get("amount"),
            "party_name": v.get("party_name"),
            "party_code": v.get("party_code"),
            "bank_account": v.get("bank_account"),
            "bank_acc_code": v.get("bank_acc_code"),
            "category_account": v.get("category_account"),
            "category_acc_code": v.get("category_acc_code"),
            "transaction_date": v.get("transaction_date"),
            "cheque_no": v.get("cheque_no"),
            "description": v.get("description"),
            "at_status": v.get("at_status"),
            "status_label": {'0': 'Void', '1': 'Open', '2': 'Posted to ledger'}
                            .get(str(v.get("at_status")), str(v.get("at_status"))),
            "editable": v.get("editable"),
            "blockers": v.get("blockers") or [],
            "reconciled_legs": v.get("reconciled_legs"),
            "legs": v.get("legs") or [],
            "note": extra_note,
        }

    def _reply(self, text: str, status: str = 'success', card: Optional[Dict] = None,
               **extra) -> Dict:
        return {"status": status, "message": text, "analysis": text,
                "card": card, "confidence": "high" if status == 'success' else "low",
                **extra}

    async def handle_show_voucher(self, conn, at_id: str) -> Dict:
        v = fetch_voucher(conn, at_id)
        if not v.get("found"):
            return self._reply(v.get("error", f"No voucher {at_id}."), 'error')
        lines = [f"{v['voucher_number']} — ${v['amount']:,.2f}",
                 f"{'Customer' if v['entry_type'] == 'CRV' else 'Vendor'}: {v['party_name'] or '—'}",
                 f"Bank/Cash: {v['bank_account']} [{v['bank_acc_code']}]",
                 f"Category: {v['category_account']} [{v['category_acc_code']}]",
                 f"Date: {v['transaction_date']}"]
        if v.get('cheque_no'):
            lines.append(f"Cheque #{v['cheque_no']}")
        if not v['editable']:
            lines.append("")
            lines.append("Locked: " + "; ".join(v['blockers']) + ".")
        else:
            lines.append("")
            lines.append("To change it, name the fields, e.g.")
            lines.append(f"    update {v['at_id']} amount 500, category Printing")
        return self._reply("\n".join(lines), card=self._voucher_card(v), action='show')

    async def handle_update_voucher(self, conn, at_id: str, fields: Dict[str, str],
                                    tail: Optional[str] = None) -> Dict:
        if not fields:
            r = await self.handle_show_voucher(conn, at_id)
            if tail:
                r['message'] = (f"I couldn't see a field name in \"{tail}\". "
                                f"Name what to change — amount, date, party, bank, "
                                f"category, cheque or note.\n\n") + r['message']
                r['analysis'] = r['message']
            return r

        current = fetch_voucher(conn, at_id)
        if not current.get("found"):
            return self._reply(current.get("error", f"No voucher {at_id}."), 'error')
        if not current.get("editable"):
            return self._reply(
                f"{current['voucher_number']} can't be edited — it is "
                + "; ".join(current['blockers']) + ".\n\n"
                f"Void it and post a fresh one:\n    void {at_id}",
                'error', card=self._voucher_card(current))

        entry_type = current['entry_type']
        upd = VoucherUpdate(entry_type=entry_type)
        resolved_notes: List[str] = []

        if 'amount' in fields:
            amt = _parse_amount(fields['amount'])
            if amt is None:
                return self._reply(f"I couldn't read \"{fields['amount']}\" as an amount.", 'error')
            upd.amount = amt
        if 'transaction_date' in fields:
            iso = normalize_date_str(fields['transaction_date']) or parse_date_text(fields['transaction_date'])
            if not iso:
                return self._reply(f"I couldn't read \"{fields['transaction_date']}\" as a date.", 'error')
            upd.transaction_date = iso
        if 'cheque_no' in fields:
            v = fields['cheque_no'].strip()
            upd.cheque_no = '' if v.lower() in ('none', 'blank', 'clear', 'remove', '-') else v
        if 'description' in fields:
            upd.description = fields['description']

        if 'party' in fields:
            prefer = P_TYPE_CUSTOMER if entry_type == 'CRV' else P_TYPE_VENDOR
            row, err = resolve_party_existing(conn, fields['party'], prefer)
            if not row:
                return self._reply(err, 'error')
            upd.party_code = str(row['p_code'])
            resolved_notes.append(f"party → {row['matched_name']}")

        if 'bank' in fields:
            res, err = resolve_bank_account(conn, fields['bank'], allow_default=False)
            if not res:
                return self._reply(err, 'error')
            upd.bank_acc_code = res.code
            resolved_notes.append(f"bank → {res.qualified}")

        if 'category' in fields:
            natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
            res, err = resolve_category_account(conn, fields['category'], natures)
            if not res:
                return self._reply(err or f"No category account matching "
                                          f"\"{fields['category']}\".", 'error')
            upd.category_acc_code = res.code
            resolved_notes.append(f"category → {res.qualified}")

        try:
            out = update_voucher(conn, at_id, upd)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(f"Update failed: {type(e).__name__}: {e}", 'error')

        text = out.get('message', f"Updated {at_id}.")
        if resolved_notes:
            text += "\n\nResolved: " + ", ".join(resolved_notes)
        return self._reply(text, card=self._voucher_card(out, out.get('review_note')),
                           action='updated')

    async def handle_void_voucher(self, conn, at_id: str) -> Dict:
        try:
            out = void_voucher(conn, at_id)
        except ValueError as e:
            return self._reply(str(e), 'error')
        return self._reply(out.get('message', f"Voided {at_id}."),
                           card=self._voucher_card(out), action='voided')

    async def handle_create_profile(self, conn, p_type: str, name: str,
                                    fields: Dict[str, str],
                                    unparsed: Optional[List[str]] = None) -> Dict:
        payload = PartyCreate(p_type=p_type)
        payload.company_name = fields.get('company_name') or name
        payload.person_name = fields.get('person_name') or (
            name if p_type == P_TYPE_EMPLOYEE else None)
        for k in ('email', 'phone', 'fax', 'address', 'city', 'state', 'zipcode',
                  'sale_tax_no', 'fedral_id_no', 'job_title', 'business_desc', 'other_desc'):
            if fields.get(k):
                setattr(payload, k, fields[k])

        if fields.get('p_account'):
            res, err = resolve_bank_account(conn, fields['p_account'], allow_default=False)
            if not res:
                return self._reply(f"Default account: {err}", 'error')
            payload.p_account = res.code

        try:
            r = create_party_full(conn, payload)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(f"Could not create the profile: {type(e).__name__}: {e}", 'error')

        label = VALID_P_TYPES[p_type]
        lines = [r['message'], ""]
        for k, lbl in (('email', 'Email'), ('phone', 'Phone'), ('address', 'Address'),
                       ('job_title', 'Title'), ('sale_tax_no', 'Sales tax no.'),
                       ('fedral_id_no', 'Federal ID')):
            if getattr(payload, k, None):
                lines.append(f"{lbl}: {getattr(payload, k)}")
        if payload.p_account:
            row = find_account(conn, payload.p_account)
            lines.append(f"Default account: {row['qualified'] if row else payload.p_account}")
        if unparsed:
            lines.append("")
            lines.append("Not saved — I don't know which field these belong to: "
                         + "; ".join(f'"{u}"' for u in unparsed) + ".")
            lines.append("Label them, e.g. address 12 Main St, city Austin.")
        lines.append("")
        lines.append(f"You can now use \"{r['company_name']}\" in a voucher.")

        return self._reply("\n".join(lines), action='profile', card={
            "kind": "profile", "created": r['created'], "p_code": r['p_code'],
            "p_type": p_type, "type_label": label,
            "company_name": r['company_name'], "person_name": r['person_name'],
            "email": payload.email, "phone": payload.phone, "address": payload.address,
            "job_title": payload.job_title, "p_account": payload.p_account,
        })

    async def process_message(self, message: str, session_id: str,
                              mode: Optional[str] = None) -> Dict:
        conn = self.get_session_db(session_id)
        msg = (message or "").strip()

        # ---- v6 commands, checked before anything else --------------------
        if msg:
            mv = _VOID_CMD_RE.match(msg)
            if mv:
                return await self.handle_void_voucher(conn, mv.group('id'))

            mu = _UPDATE_CMD_RE.match(msg)
            if mu:
                parsed = _parse_update_command(msg)
                return await self.handle_update_voucher(
                    conn, parsed['at_id'], parsed['fields'], parsed.get('unparsed_tail'))

            ms = _SHOW_CMD_RE.match(msg) or (
                _BARE_ID_RE.match(msg) if mode in (None, 'update') else None)
            if ms:
                return await self.handle_show_voucher(conn, ms.group('id'))

            mp = _parse_profile_command(msg)
            if mp:
                return await self.handle_create_profile(
                    conn, mp['p_type'], mp['name'], mp['fields'], mp.get('unparsed'))

        # Intent keywords are plain substring tests, so "for a report" or
        # "Handy ... list" could hijack a real posting. Skip them entirely once
        # the message carries a direction verb AND an amount.
        transactional = _looks_transactional(msg)

        if not msg or (not transactional and (self.is_greeting(msg) or self.is_help(msg))):
            return self.get_help_response()
        if not transactional and self.is_financial_analysis(msg):
            return await self.get_financial_analysis(session_id)
        if not transactional and self.is_query_records(msg):
            m = re.search(r'\d+', msg)
            return await self.get_records(session_id, int(m.group()) if m else 10)

        extracted = await self.extract_transaction_info(msg, session_id)

        action = extracted.get('action')
        if action == 'ambiguous_direction':
            p = extracted.get('_parsed') or {}
            t = (f"I can read that line - {p.get('party')} , "
                 f"{p.get('amount'):,.2f} on {p.get('date') or 'no date'}"
                 + (f", account \"{p.get('account')}\"" if p.get('account') else "")
                 + " - but nothing in it says whether the money came IN or went OUT, "
                   "so I won't guess. Any one of these fixes it:\n"
                   f"  Received {p.get('amount'):,.2f} from {p.get('party')} ...\n"
                   f"  Paid {p.get('amount'):,.2f} to {p.get('party')} ...\n"
                   f"  ...same line but with the amount as ({p.get('amount'):,.2f}) "
                   f"or -{p.get('amount'):,.2f} for money out\n"
                   f"  ...or prefix the account with DR (paid) or CR (received)")
            return {'status': 'error', 'message': t, 'analysis': t,
                    'confidence': 'low', 'extraction_source': extracted.get('_source')}
        if action == 'unparsed':
            hint = ("I couldn't read that as a transaction. I need a direction "
                    "(paid / received), an amount, and a name - for example:\n"
                    "  Paid $450 to Handy Fix LLC for Repair and Maintenance "
                    "from Bank of America 9523")
            if extracted.get('_llm_error'):
                hint += (f"\n\n(The language model step also failed: "
                         f"{extracted['_llm_error']}. The rule-based parser is "
                         f"still active, so plain 'paid/received' phrasing works.)")
            return {'status': 'error', 'message': hint, 'analysis': hint,
                    'confidence': 'low',
                    'extraction_source': extracted.get('_source')}
        if action in (None, 'help'):
            return self.get_help_response()
        if action == 'financial_analysis':
            return await self.get_financial_analysis(session_id)
        if action == 'query_records':
            return await self.get_records(session_id, extracted.get('limit', 10))
        if action == 'error':
            t = extracted.get('message', 'Could not understand that transaction.')
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

        entry_type = extracted.get('entry_type')
        amount = extracted.get('amount')
        party_name = extracted.get('party_name')
        description = extracted.get('description') or (
            'Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher')

        if not entry_type or not amount or not party_name:
            missing = []
            if not entry_type:
                missing.append('whether this is money received or paid')
            if not amount:
                missing.append('the amount')
            if not party_name:
                missing.append('the customer/vendor name')
            t = ("I couldn't post that - missing " + ", ".join(missing) + ". "
                 "Please resend with full detail, e.g. "
                 '"06/04/2026 Current Assets - JOHN SMITH 659.25".')
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

        try:
            amount = abs(float(amount))
        except (TypeError, ValueError):
            t = "I couldn't read the amount as a number."
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}
        if amount == 0:
            t = "The amount came out as zero, so there's nothing to post."
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

        norm_date = normalize_date_str(extracted.get('transaction_date')) or parse_date_text(msg)
        date_str = norm_date or datetime.now().date().isoformat()
        try:
            trans_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            trans_date = datetime.now().date()

        cheque_no = extracted.get('cheque_no') or extracted.get('cheque_number')
        if not cheque_no:
            chq = _CHEQUE_RE.search(msg)
            cheque_no = chq.group(1) if chq else None

        review_items = []

        try:
            # ---- Fiscal year ----
            if not within_open_year(conn, trans_date):
                if STRICT_FISCAL_YEAR:
                    t = (f"{trans_date:%m/%d/%Y} falls outside the open financial or "
                         f"audit year for this company, so I haven't posted it.")
                    return {'status': 'error', 'message': t, 'analysis': t,
                            'confidence': 'low'}
                review_items.append('date (outside the open fiscal year)')

            # ---- Party ----
            party_code, party_display, party_match_type, party_score, party_type = \
                resolve_party(conn, party_name, entry_type)

            # ---- Statement-line account: route it to the leg its nature implies
            # "06/04/2026 Current Assets - JOHN SMITH 659.25"  -> asset  -> bank leg
            # "06/04/2026 DR Rent Expense - LANDLORD 2400.00"  -> expense-> category leg
            account_text = extracted.get('account_text')
            if account_text and not extracted.get('bank_text') \
                    and not extracted.get('category_hint'):
                probe, _probe_err = _resolve(conn, account_text, get_chart(conn), "account")
                if probe and probe.nature in (NATURE_REVENUE, NATURE_EXPENSE,
                                              NATURE_COGS, NATURE_UNEARNED):
                    extracted['category_hint'] = account_text
                else:
                    extracted['bank_text'] = account_text

            # ---- Bank / cash leg ----
            bank_text = extracted.get('bank_text')
            bank_res, bank_err = resolve_bank_account(
                conn, bank_text, allow_default=not bool(bank_text and bank_text.strip()))
            if not bank_res:
                t = bank_err or f"Could not resolve the bank/cash account for '{bank_text}'."
                return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

            # ---- Category leg ----
            natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
            category_hint = extracted.get('category_hint')
            cat_res, cat_err = resolve_category_account(conn, category_hint, natures)

            if cat_res is None and cat_err is None:
                kind = 'revenue' if entry_type == 'CRV' else 'expense'
                t = (f"No {kind} accounts are set up for this company yet "
                     f"(nature {', '.join(sorted(natures))}) - add at least one before "
                     f"posting vouchers.")
                return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

            # An explicit hint that matched nothing is an error, not a reason to
            # silently post somewhere else.
            if cat_res is None and cat_err:
                return {'status': 'error', 'message': cat_err, 'analysis': cat_err,
                        'confidence': 'low'}

            category_matched = cat_res is not None
            if not cat_res:
                cat_res, _ = resolve_category_account(
                    conn, f"{party_name} {description}", natures)
                category_matched = cat_res is not None
            if not cat_res:
                cat_res, default_err = _default_category_resolution(conn, entry_type, natures)
                category_matched = False
                if not cat_res:
                    t = default_err or "Could not determine the category account."
                    return {'status': 'error', 'message': t, 'analysis': t,
                            'confidence': 'low'}

            # ---- Sanity: both legs must not be the same account ----
            if bank_res.code == cat_res.code:
                t = (f"Both sides of this entry resolved to the same account "
                     f"({bank_res.qualified}), which would be a self-cancelling "
                     f"voucher. Please name the two accounts separately.")
                return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

            # ---- Post ----
            new_id = insert_voucher(conn, entry_type, party_code, bank_res, cat_res,
                                    amount, trans_date, description, cheque_no)

            voucher_number = f"{entry_type}-{new_id}"
            party_label = "Customer" if entry_type == 'CRV' else "Vendor"
            if entry_type == 'CRV':
                direction = f"Dr {bank_res.qualified}  /  Cr {cat_res.qualified}"
            else:
                direction = f"Dr {cat_res.qualified}  /  Cr {bank_res.qualified}"

            log_voucher_posting(
                voucher_number, entry_type,
                {
                    'amount': amount,
                    'party_display': party_display,
                    'party_type': party_type,
                    'party_match_type': party_match_type,
                    'party_score': party_score,
                    'bank_desc': bank_res.qualified,
                    'bank_code': bank_res.code,
                    'bank_level': bank_res.level,
                    'bank_match_type': bank_res.match_type,
                    'bank_score': bank_res.score,
                    'bank_text': bank_text,
                    'category_desc': cat_res.qualified,
                    'category_code': cat_res.code,
                    'category_level': cat_res.level,
                    'category_match_type': cat_res.match_type,
                    'category_score': cat_res.score,
                    'trans_date': trans_date.isoformat(),
                    'cheque_no': cheque_no,
                },
                session_id, msg,
            )

            if party_match_type == 'fuzzy' and party_score < 0.85:
                review_items.append(party_label.lower())
            if party_match_type == 'new':
                review_items.append(f'new {party_label.lower()} record')
            if bank_res.match_type not in ('exact', 'code', 'default') and bank_res.score < 0.90:
                review_items.append('bank/cash account')
            if not category_matched:
                review_items.append('category')
            if extracted.get('_direction_inferred'):
                review_items.append('DIRECTION (the line gave no in/out signal)')

            review_line = ""
            if review_items:
                review_line = (f"\n\nPlease double check the {', '.join(review_items)} "
                               f"above - matched automatically.")

            cheque_line = f"\nCheque #{cheque_no}" if cheque_no else ""
            text = strip_emojis(
                f"Posted {voucher_number} for ${amount:,.2f}\n\n"
                f"{party_label}: {party_display}\n"
                f"Bank/Cash: {bank_res.qualified}  [{bank_res.code}]\n"
                f"Category: {cat_res.qualified}  [{cat_res.code}]\n"
                f"Date: {trans_date.strftime('%m/%d/%Y')}\n"
                f"Journal entry: {direction}"
                f"{cheque_line}{review_line}"
            )

            return {
                'status': 'success',
                'voucher_number': voucher_number,
                'at_id': new_id,
                'entry_type': entry_type,
                'amount': amount,
                'party_name': party_display,
                'party_code': party_code,
                'party_type': party_type,
                'party_match_type': party_match_type,
                'party_match_score': party_score,
                'bank_account': bank_res.qualified,
                'bank_acc_code': bank_res.code,
                'bank_level': bank_res.level,
                'category_account': cat_res.qualified,
                'category_acc_code': cat_res.code,
                'category_level': cat_res.level,
                'category_matched': category_matched,
                # --- aliases the React client reads (App.jsx
                # buildVoucherMessageText / VoucherCard). Keep both names so
                # neither side has to be renamed. ---
                'first_leg_account': bank_res.qualified,
                'account_name': cat_res.qualified,
                'bank_acc_id': bank_res.code,
                'income_acc_id': cat_res.code if entry_type == 'CRV' else None,
                'expense_acc_id': cat_res.code if entry_type == 'CPV' else None,
                # Review flags travel as their own field, because the client
                # rebuilds the bubble text and would otherwise drop them.
                'review_note': ', '.join(review_items) if review_items else None,
                'review_items': review_items,
                'description': description,
                'transaction_date': trans_date.isoformat(),
                'cheque_no': cheque_no,
                'message': text,
                'analysis': text,
                'confidence': 'high' if party_match_type != 'fuzzy' else 'medium',
                'extraction_source': extracted.get('_source'),
                'isVoucher': True,
            }

        except Exception as e:
            t = f"Failed to post {entry_type}: {str(e)}"
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

    async def health_check(self) -> Dict[str, Any]:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM acc_party WHERE system_id = %s", (SYSTEM_ID,))
            party_count = cur.fetchone()[0]
            cur.close()
            chart = get_chart(conn, force=True)
            by_nature: Dict[str, int] = {}
            for r in chart:
                by_nature[NATURE_LABEL.get(r['nature'], r['nature'])] = \
                    by_nature.get(NATURE_LABEL.get(r['nature'], r['nature']), 0) + 1
            conn.close()
            return {
                "bot": self.name, "status": "healthy", "database": "connected",
                "groq": "connected" if self.groq_client else "not_configured",
                "system_id": SYSTEM_ID,
                "parties": party_count,
                "postable_accounts": len(chart),
                "accounts_by_nature": by_nature,
            }
        except Exception as e:
            return {"bot": self.name, "status": "degraded", "database": f"error: {str(e)}",
                    "groq": "connected" if self.groq_client else "not_configured"}


# ==========================================================================
# v6 - PROFILES (acc_party)
# ==========================================================================
VALID_P_TYPES = {P_TYPE_CUSTOMER: 'Customer', P_TYPE_VENDOR: 'Payee / Vendor',
                 P_TYPE_EMPLOYEE: 'Employee', P_TYPE_OTHER: 'Other'}


def create_party_full(conn, data: PartyCreate) -> Dict[str, Any]:
    """
    Create a profile explicitly, with the full field set.  Column names follow
    acc_party exactly (note Email / Phone / Fax / Address are capitalised in
    the schema; MySQL is case-insensitive on column names so either works).
    """
    p_type = (data.p_type or '').strip().upper()
    if p_type not in VALID_P_TYPES:
        raise ValueError(
            f"p_type must be one of {', '.join(f'{k} ({v})' for k, v in VALID_P_TYPES.items())}")

    company = (data.company_name or '').strip()
    person = (data.person_name or '').strip()
    if not company and not person:
        raise ValueError("Give at least a company name or a person name.")
    # Mirror the host UI: whichever is blank echoes the other, so lists that
    # render only one column are never empty.
    company = title_case_name(company or person)
    person = title_case_name(person or company)

    # Duplicate guard - same normalised name within this p_type + system.
    target = normalize_name(company)
    for p in find_party_candidates(conn, company, p_type=p_type, limit=50):
        for cand in (p.get('company_name'), p.get('person_name')):
            if cand and normalize_name(cand) == target:
                return {"created": False, "p_code": str(p['p_code']),
                        "p_type": p['p_type'], "company_name": p.get('company_name'),
                        "person_name": p.get('person_name'),
                        "message": (f"A {VALID_P_TYPES[p_type]} called \"{cand}\" already "
                                    f"exists as {p['p_code']} - reusing it rather than "
                                    f"creating a duplicate.")}

    # p_account, when given, must be a real postable account for this tenant.
    p_account = (data.p_account or '').strip() or None
    if p_account and not find_account(conn, p_account):
        raise ValueError(f"p_account {p_account} is not a postable account for this company.")

    cur = conn.cursor()
    try:
        new_code = _next_party_code(cur, p_type)
        cur.execute(
            "INSERT INTO acc_party (p_code, p_type, company_name, person_name, "
            " Address, user_city, user_state, user_zipcode, Email, Phone, Fax, "
            " sale_tax_no, fedral_id_no, job_title, business_desc, other_desc, "
            " p_account, status, system_id, marital_status) "
            "VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,0)",
            (new_code, p_type, company, person,
             data.address, data.city, data.state, data.zipcode,
             data.email, data.phone, data.fax,
             data.sale_tax_no, data.fedral_id_no, data.job_title,
             data.business_desc, data.other_desc,
             p_account, int(data.status), SYSTEM_ID),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()

    print(f"[profile] created {VALID_P_TYPES[p_type]} {new_code} - {company}")
    return {"created": True, "p_code": new_code, "p_type": p_type,
            "company_name": company, "person_name": person,
            "message": f"Created {VALID_P_TYPES[p_type]} {new_code} - {company}"}


def list_parties(conn, q: Optional[str] = None, p_type: Optional[str] = None,
                 limit: int = 500) -> List[Dict]:
    sql = ("SELECT p_code, p_type, company_name, person_name, Email, Phone, "
           "       p_account, status "
           "FROM acc_party WHERE system_id = %s AND status = 1")
    params: tuple = (SYSTEM_ID,)
    if p_type:
        sql += " AND p_type = %s"
        params += (p_type,)
    if q:
        sql += " AND (company_name LIKE %s OR person_name LIKE %s OR p_code LIKE %s)"
        like = f"%{q.strip()}%"
        params += (like, like, like)
    sql += " ORDER BY p_type, company_name LIMIT %s"
    params += (limit,)
    rows = _fetch_all(conn, sql, params, "list_parties")
    for r in rows:
        r['p_code'] = str(r['p_code'])
        r['type_label'] = VALID_P_TYPES.get(r['p_type'], r['p_type'])
    return rows


# ==========================================================================
# v6 - VOUCHER FETCH / UPDATE
#
# Editability follows the host UI exactly:
#   * at_status must be 1.  0 = void, 2 = posted to ledger; the PHP voucher
#     lists only render Edit when at_status == 1 (see debitMemoList()).
#   * no leg may be reconciled - invoice_list()/debitMemoList() hide Edit and
#     Void once `reconciled > 0`.
#   * exactly two detail legs.  Vouchers written by the PHP screens can have
#     many legs and a regrouped reconcile side; rewriting those as two legs
#     would silently destroy data, so we refuse instead.
#
# The update itself mirrors admin_model::updatePayment(): UPDATE the master,
# DELETE both detail tables for that at_id, re-INSERT the legs.  at_id never
# changes, and at_cr_user / at_cr_date are preserved.
# ==========================================================================
def fetch_voucher(conn, at_id: Any) -> Dict[str, Any]:
    master_rows = _fetch_all(
        conn,
        "SELECT at_id, at_date, at_doc_type, at_desc, at_remarks, at_memo, "
        "       at_m_amount, at_party_code, at_bank, at_bank_acc, at_cheque_no, "
        "       at_cheque_date, at_status, at_cr_user, at_cr_date, at_up_user, at_up_date "
        "FROM acc_trans_m WHERE at_id = %s AND system_id = %s",
        (at_id, SYSTEM_ID), "fetch_voucher.master")
    if not master_rows:
        return {"found": False,
                "error": f"No voucher {at_id} for system_id {SYSTEM_ID}."}
    m = master_rows[0]

    doc_type = m['at_doc_type']
    if doc_type not in (DOC_TYPE_CRV, DOC_TYPE_CPV):
        return {"found": False,
                "error": f"Voucher {at_id} is doc type {doc_type}, not a CRV or CPV. "
                         f"Edit it in the host application instead."}
    entry_type = 'CRV' if doc_type == DOC_TYPE_CRV else 'CPV'

    legs = _fetch_all(
        conn,
        "SELECT at_sno, at_acc_code, at_dc_type, at_amount, at_remarks, atd_date, atd_party "
        "FROM acc_trans_d WHERE at_id = %s AND at_system_id = %s ORDER BY at_sno",
        (at_id, SYSTEM_ID), "fetch_voucher.legs")

    rec = _fetch_all(
        conn,
        "SELECT COUNT(*) AS n FROM acc_trans_reconcile "
        "WHERE at_id = %s AND at_system_id = %s AND at_acc_reconcile = 1",
        (at_id, SYSTEM_ID), "fetch_voucher.reconciled")
    reconciled = int(rec[0]['n']) if rec else 0

    party_rows = _fetch_all(
        conn,
        "SELECT p_code, p_type, company_name, person_name FROM acc_party "
        "WHERE p_code = %s AND system_id = %s LIMIT 1",
        (m['at_party_code'], SYSTEM_ID), "fetch_voucher.party") if m['at_party_code'] else []
    party = party_rows[0] if party_rows else None

    # Identify the two legs by DEBIT/CREDIT, not by at_sno and not by nature.
    # _build_legs() fixes the polarity:  CRV = Dr bank / Cr revenue,
    # CPV = Dr expense / Cr bank.  In any two-leg voucher there is exactly one
    # of each, so dc_type separates them cleanly even when a PHP screen wrote
    # the legs in a different order, and even when the contra side happens to
    # share a nature with the category side.
    bank_dc = 'D' if entry_type == 'CRV' else 'C'
    bank_leg = cat_leg = None
    for leg in legs:
        code = str(leg['at_acc_code'])
        row = find_account(conn, code)
        leg['code'] = code
        leg['qualified'] = row['qualified'] if row else code
        leg['nature'] = account_nature(code)
        leg['level'] = account_level(code)
        if leg['at_dc_type'] == bank_dc and bank_leg is None:
            bank_leg = leg
        elif cat_leg is None:
            cat_leg = leg

    blockers = []
    status = str(m['at_status'] or '')
    if status != '1':
        blockers.append(
            "voided (at_status 0)" if status == '0'
            else f"already posted to the ledger (at_status {status})")
    if reconciled:
        blockers.append(f"reconciled ({reconciled} leg(s) marked at_acc_reconcile = 1)")
    if len(legs) != 2:
        blockers.append(f"made of {len(legs)} legs - this editor only handles two-leg vouchers")
    if cat_leg is None or bank_leg is None:
        blockers.append("its legs don't look like a bank + category pair")

    return {
        "found": True,
        "editable": not blockers,
        "blockers": blockers,
        "at_id": str(m['at_id']),
        "voucher_number": f"{entry_type}-{m['at_id']}",
        "entry_type": entry_type,
        "at_status": status,
        "reconciled_legs": reconciled,
        "amount": float(m['at_m_amount'] or 0),
        "transaction_date": m['at_date'].date().isoformat() if hasattr(m['at_date'], 'date')
                            else (str(m['at_date'])[:10] if m['at_date'] else None),
        "description": m['at_remarks'] or m['at_memo'] or m['at_desc'],
        "cheque_no": m['at_cheque_no'],
        "party_code": str(m['at_party_code']) if m['at_party_code'] else None,
        "party_name": (party.get('company_name') or party.get('person_name')) if party else None,
        "party_type": party.get('p_type') if party else None,
        "bank_acc_code": bank_leg['code'] if bank_leg else None,
        "bank_account": bank_leg['qualified'] if bank_leg else None,
        "category_acc_code": cat_leg['code'] if cat_leg else None,
        "category_account": cat_leg['qualified'] if cat_leg else None,
        "legs": [{"sno": l['at_sno'], "code": l['code'], "account": l['qualified'],
                  "dc": l['at_dc_type'], "amount": float(l['at_amount'] or 0),
                  "level": l['level'], "nature": NATURE_LABEL.get(l['nature'], l['nature'])}
                 for l in legs],
        "created_by": m['at_cr_user'],
        "created_at": str(m['at_cr_date']) if m['at_cr_date'] else None,
        "updated_at": str(m['at_up_date']) if m['at_up_date'] else None,
    }


def update_voucher(conn, at_id: Any, upd: VoucherUpdate) -> Dict[str, Any]:
    current = fetch_voucher(conn, at_id)
    if not current.get("found"):
        raise ValueError(current.get("error", f"Voucher {at_id} not found."))
    if not current.get("editable"):
        raise ValueError(
            f"Voucher {current['voucher_number']} can't be edited because it is "
            + "; ".join(current["blockers"])
            + ". Void it and post a fresh one instead.")

    entry_type = current['entry_type']
    if upd.entry_type and upd.entry_type.upper() != entry_type:
        raise ValueError(
            f"{current['voucher_number']} is a {entry_type} and can't be turned into a "
            f"{upd.entry_type.upper()}. The document type is baked into at_id "
            f"(characters 5-6), and the host application reads it back out of "
            f"there to route pages. Void this voucher and create a new one.")

    # ---- amount ----
    amount = current['amount'] if upd.amount is None else abs(float(upd.amount))
    if amount == 0:
        raise ValueError("Amount must be greater than zero.")

    # ---- date ----
    if upd.transaction_date:
        iso = normalize_date_str(upd.transaction_date)
        if not iso:
            raise ValueError(f"Couldn't read \"{upd.transaction_date}\" as a date.")
        trans_date = datetime.strptime(iso, '%Y-%m-%d').date()
    else:
        trans_date = datetime.strptime(current['transaction_date'], '%Y-%m-%d').date()

    fiscal_warning = None
    if not within_open_year(conn, trans_date):
        if STRICT_FISCAL_YEAR:
            raise ValueError(f"{trans_date:%m/%d/%Y} is outside the open fiscal year.")
        fiscal_warning = f"{trans_date:%m/%d/%Y} is outside the open fiscal year."

    # ---- party ----
    party_code = upd.party_code or current['party_code']
    if not party_code:
        raise ValueError("A party is required.")
    prows = _fetch_all(conn,
                       "SELECT p_code, p_type, company_name, person_name FROM acc_party "
                       "WHERE p_code = %s AND system_id = %s LIMIT 1",
                       (party_code, SYSTEM_ID), "update_voucher.party")
    if not prows:
        raise ValueError(f"Party {party_code} does not exist for this company. "
                         f"Create the profile first.")
    party_name = prows[0].get('company_name') or prows[0].get('person_name')

    # ---- accounts (by code, not by name - no fuzzy matching on an edit) ----
    def _need(code, what, natures=None):
        row = find_account(conn, code)
        if not row:
            raise ValueError(f"{what} account {code} is not a postable account "
                             f"for this company.")
        if not transactionable_account(conn, code):
            raise ValueError(f"{row['qualified']} is a group account and can't be posted to.")
        if natures and row['nature'] not in natures:
            raise ValueError(
                f"{row['qualified']} is a {NATURE_LABEL.get(row['nature'], row['nature'])} "
                f"account, which can't be the {what.lower()} leg of a {entry_type}.")
        return row

    bank_row = _need(upd.bank_acc_code or current['bank_acc_code'], "Bank/cash")
    cat_natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
    cat_row = _need(upd.category_acc_code or current['category_acc_code'],
                    "Category", cat_natures)
    if bank_row['code'] == cat_row['code']:
        raise ValueError("Both legs point at the same account, which would cancel out.")

    # ---- cheque / description ----
    cheque_no = current['cheque_no'] if upd.cheque_no is None else (upd.cheque_no.strip() or None)
    description = current['description'] if upd.description is None else upd.description
    at_desc = 'Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher'

    doc_type = DOC_TYPE_CRV if entry_type == 'CRV' else DOC_TYPE_CPV
    legs = _build_legs(entry_type, bank_row['code'], cat_row['code'], amount)
    now = datetime.now()
    cheque_date = trans_date if cheque_no else None

    cur = conn.cursor()
    try:
        # Re-check inside the transaction - status/reconcile could have moved.
        cur.execute(
            "SELECT at_status, (SELECT COUNT(*) FROM acc_trans_reconcile r "
            "  WHERE r.at_id = m.at_id AND r.at_system_id = %s AND r.at_acc_reconcile = 1) "
            "FROM acc_trans_m m WHERE m.at_id = %s AND m.system_id = %s FOR UPDATE",
            (SYSTEM_ID, at_id, SYSTEM_ID))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Voucher {at_id} disappeared.")
        if str(row[0] or '') != '1' or int(row[1] or 0) > 0:
            raise ValueError(f"Voucher {at_id} was locked (posted or reconciled) "
                             f"by someone else while you were editing.")

        cur.execute(
            "UPDATE acc_trans_m SET at_date = %s, at_desc = %s, at_pmode = %s, "
            "  at_bank = %s, at_bank_acc = %s, at_cheque_no = %s, at_cheque_date = %s, "
            "  at_memo = %s, at_m_amount = %s, at_party_code = %s, at_remarks = %s, "
            "  at_up_user = %s, at_up_date = %s "
            "WHERE at_id = %s AND system_id = %s",
            (trans_date, at_desc, PMODE[doc_type],
             (bank_row['desc'] or bank_row['code'])[:AT_BANK_LABEL_MAX_LEN],
             bank_row['code'][:AT_BANK_ACC_MAX_LEN], cheque_no, cheque_date,
             description, amount, party_code, description,
             USER_ID, now, at_id, SYSTEM_ID))

        cur.execute("DELETE FROM acc_trans_d WHERE at_id = %s AND at_system_id = %s",
                    (at_id, SYSTEM_ID))
        cur.execute("DELETE FROM acc_trans_reconcile WHERE at_id = %s AND at_system_id = %s",
                    (at_id, SYSTEM_ID))

        detail_sql = (
            "INSERT INTO acc_trans_d "
            "(at_id,at_sno,at_acc_code,at_dc_type,at_amount,at_qty,at_remarks,"
            " atd_date,atd_party,at_system_id,at_acc_reconcile,doc_path,at_sno_by_user) "
            "VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,0,NULL,0)")
        reconcile_sql = (
            "INSERT INTO acc_trans_reconcile "
            "(at_id,at_sno,at_acc_code,at_dc_type,at_amount,at_remarks,atd_date,"
            " atd_party,at_system_id,at_sno_by_user,at_acc_reconcile,at_rec_id,at_rec_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,NULL,NULL)")
        for leg in legs:
            cur.execute(detail_sql, (at_id, leg['sno'], leg['code'], leg['dc'],
                                     leg['amount'], description, trans_date,
                                     party_code, SYSTEM_ID))
            cur.execute(reconcile_sql, (at_id, leg['sno'], leg['code'], leg['dc'],
                                        leg['amount'], description, trans_date,
                                        party_code, SYSTEM_ID))

        cur.execute(
            "INSERT INTO voucher_cousting "
            "(system_id,voucher_id,ref_id,voucher_type,start_date_time,end_date_time,time_spent) "
            "VALUES (%s,%s,NULL,%s,%s,%s,0) "
            "ON DUPLICATE KEY UPDATE end_date_time = VALUES(end_date_time)",
            (SYSTEM_ID, at_id, doc_type, now, now))

        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()

    direction = (f"Dr {bank_row['qualified']}  /  Cr {cat_row['qualified']}"
                 if entry_type == 'CRV' else
                 f"Dr {cat_row['qualified']}  /  Cr {bank_row['qualified']}")
    print(f"[voucher] {entry_type}-{at_id} UPDATED - {amount:,.2f} - {direction}")

    out = fetch_voucher(conn, at_id)
    out['status'] = 'success'
    out['message'] = (
        f"Updated {entry_type}-{at_id} for ${amount:,.2f}\n\n"
        f"{'Customer' if entry_type == 'CRV' else 'Vendor'}: {party_name}\n"
        f"Bank/Cash: {bank_row['qualified']}  [{bank_row['code']}]\n"
        f"Category: {cat_row['qualified']}  [{cat_row['code']}]\n"
        f"Date: {trans_date.strftime('%m/%d/%Y')}\n"
        f"Journal entry: {direction}"
        + (f"\nCheque #{cheque_no}" if cheque_no else "")
        + (f"\n\nPlease check: {fiscal_warning}" if fiscal_warning else ""))
    out['review_note'] = fiscal_warning
    return out


def void_voucher(conn, at_id: Any) -> Dict[str, Any]:
    """
    The escape hatch when a voucher can't be edited. Sets at_status = 0 and
    stamps void_date, exactly as the host UI's voidVoucher() does. Rows are
    kept so acc_trans_reconcile and voucher_cousting stay consistent; every
    report filters at_status >= 1.
    """
    current = fetch_voucher(conn, at_id)
    if not current.get("found"):
        raise ValueError(current.get("error", f"Voucher {at_id} not found."))
    if current['reconciled_legs']:
        raise ValueError(f"{current['voucher_number']} has reconciled legs and "
                         f"can't be voided. Unreconcile it in the host application first.")
    if current['at_status'] == '0':
        return {**current, "status": "success",
                "message": f"{current['voucher_number']} was already void."}

    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE acc_trans_m SET at_status = 0, void_date = %s, "
            "  at_up_user = %s, at_up_date = %s "
            "WHERE at_id = %s AND system_id = %s",
            (datetime.now(), USER_ID, datetime.now(), at_id, SYSTEM_ID))
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()
    print(f"[voucher] {current['voucher_number']} VOIDED")
    out = fetch_voucher(conn, at_id)
    out['status'] = 'success'
    out['message'] = f"Voided {current['voucher_number']}."
    return out


def list_vouchers(conn, limit: int = 50, q: Optional[str] = None,
                  entry_type: Optional[str] = None) -> List[Dict]:
    sql = ("SELECT m.at_id, m.at_doc_type, m.at_date, m.at_m_amount, m.at_status, "
           "       m.at_remarks, m.at_cheque_no, m.at_party_code, "
           "       COALESCE(p.company_name, p.person_name) AS party_name, "
           "       (SELECT COUNT(*) FROM acc_trans_reconcile r "
           "         WHERE r.at_id = m.at_id AND r.at_system_id = m.system_id "
           "           AND r.at_acc_reconcile = 1) AS reconciled "
           "FROM acc_trans_m m "
           "LEFT JOIN acc_party p ON p.p_code = m.at_party_code AND p.system_id = m.system_id "
           "WHERE m.system_id = %s AND m.at_doc_type IN (%s, %s)")
    params: tuple = (SYSTEM_ID, DOC_TYPE_CRV, DOC_TYPE_CPV)
    if entry_type in ('CRV', 'CPV'):
        sql = sql.replace("m.at_doc_type IN (%s, %s)", "m.at_doc_type = %s")
        params = (SYSTEM_ID, DOC_TYPE_CRV if entry_type == 'CRV' else DOC_TYPE_CPV)
    if q:
        sql += (" AND (m.at_id LIKE %s OR p.company_name LIKE %s "
                "      OR p.person_name LIKE %s OR m.at_remarks LIKE %s)")
        like = f"%{q.strip()}%"
        params += (like, like, like, like)
    sql += " ORDER BY m.at_id DESC LIMIT %s"
    params += (limit,)

    rows = _fetch_all(conn, sql, params, "list_vouchers")
    out = []
    for r in rows:
        et = 'CRV' if r['at_doc_type'] == DOC_TYPE_CRV else 'CPV'
        st = str(r['at_status'] or '')
        out.append({
            "at_id": str(r['at_id']),
            "voucher_number": f"{et}-{r['at_id']}",
            "entry_type": et,
            "transaction_date": (r['at_date'].date().isoformat()
                                 if hasattr(r['at_date'], 'date')
                                 else (str(r['at_date'])[:10] if r['at_date'] else None)),
            "amount": float(r['at_m_amount'] or 0),
            "party_name": r['party_name'],
            "party_code": str(r['at_party_code']) if r['at_party_code'] else None,
            "description": r['at_remarks'],
            "cheque_no": r['at_cheque_no'],
            "at_status": st,
            "status_label": {'0': 'Void', '1': 'Open', '2': 'Posted to ledger'}.get(st, st),
            "reconciled": int(r['reconciled'] or 0),
            "editable": st == '1' and not int(r['reconciled'] or 0),
        })
    return out


bot = AccountingBot()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(request: ChatRequest):
    try:
        return await bot.process_message(request.message, request.session_id,
                                         request.mode)
    except Exception as e:
        return {"status": "error", "message": str(e), "analysis": f"Error: {str(e)}",
                "confidence": "low"}


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    conn = bot._sessions.pop(session_id, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
    return {"status": "ok"}


@app.get("/api/dropdowns/{session_id}")
async def get_dropdowns(session_id: str):
    conn = bot.get_session_db(session_id)
    chart = get_chart(conn)
    return {
        "parties": find_party_candidates(conn, "", limit=1000),
        "accounts": [
            {"code": r['code'], "name": r['desc'], "qualified": r['qualified'],
             "parent": r['parent_desc'], "level": r['level'],
             "nature": NATURE_LABEL.get(r['nature'], r['nature'])}
            for r in chart
        ],
        "income_accounts": [r['qualified'] for r in chart_by_nature(conn, CRV_INCOME_NATURES)],
        "expense_accounts": [r['qualified'] for r in chart_by_nature(conn, CPV_EXPENSE_NATURES)],
    }


@app.get("/api/health")
async def health_check():
    return await bot.health_check()


@app.get("/api/debug/chart")
async def debug_chart(nature: Optional[str] = None, q: Optional[str] = None):
    """Exactly what v_trans_accounts_m2 returns for this tenant."""
    conn = get_connection()
    try:
        rows = get_chart(conn, force=True)
        if nature:
            rows = [r for r in rows if r['nature'] == str(nature)]
        if q:
            nq = normalize_name(q)
            rows = [r for r in rows
                    if nq in normalize_name(r['qualified']) or nq in normalize_name(r['desc'])]
        return {
            "system_id": SYSTEM_ID,
            "count": len(rows),
            "accounts": [
                {"code": r['code'], "level": r['level'],
                 "nature": NATURE_LABEL.get(r['nature'], r['nature']),
                 "name": r['desc'], "qualified": r['qualified']}
                for r in rows
            ],
        }
    finally:
        conn.close()


@app.get("/api/debug/extract")
async def debug_extract(text: str):
    """
    Shows what the parser makes of a message WITHOUT resolving accounts or
    posting anything. Use this to tell 'the LLM died' apart from 'the account
    name didn't resolve'.
    """
    rules = _rule_based_extract(text)
    out = {
        "message": text,
        "looks_transactional": _looks_transactional(text),
        "rule_based": rules,
        "llm_configured": bot.groq_client is not None,
        "llm_model": getattr(bot, "model", None),
    }
    if bot.groq_client:
        try:
            merged = await bot.extract_transaction_info(text, "debug")
            out["merged"] = merged
        except Exception as e:
            out["merged_error"] = f"{type(e).__name__}: {e}"
    return out


@app.get("/api/debug/resolve-account")
async def debug_resolve_account(text: str, side: str = "bank"):
    """side = bank | income | expense"""
    conn = get_connection()
    try:
        if side == "bank":
            res, err = resolve_bank_account(conn, text, allow_default=False)
        else:
            natures = CRV_INCOME_NATURES if side == "income" else CPV_EXPENSE_NATURES
            res, err = resolve_category_account(conn, text, natures)
        if res:
            return {"search_text": text, "side": side, "code": res.code,
                    "name": res.desc, "qualified": res.qualified,
                    "level": res.level,
                    "nature": NATURE_LABEL.get(res.nature, res.nature),
                    "match_type": res.match_type, "score": res.score,
                    "postable": transactionable_account(conn, res.code)}
        return {"search_text": text, "side": side, "error": err or "not found"}
    finally:
        conn.close()


# ==========================================================================
# v6 API - accounts, profiles, voucher update
# ==========================================================================
@app.get("/api/accounts")
async def api_accounts():
    """Everything the Update / Profile forms need to populate their pickers."""
    conn = get_connection()
    try:
        chart = get_chart(conn)

        def shape(rows):
            return [{"code": r['code'], "name": r['desc'], "qualified": r['qualified'],
                     "parent": r['parent_desc'], "level": r['level'],
                     "nature": NATURE_LABEL.get(r['nature'], r['nature'])} for r in rows]

        return {
            "system_id": SYSTEM_ID,
            # The bank/contra leg may be ANY postable account - the host app's
            # own picker (cashBankAccounts) works the same way.
            "bank": shape(chart),
            "income": shape(chart_by_nature(conn, CRV_INCOME_NATURES)),
            "expense": shape(chart_by_nature(conn, CPV_EXPENSE_NATURES)),
            "all": shape(chart),
        }
    finally:
        conn.close()


@app.get("/api/parties")
async def api_list_parties(q: Optional[str] = None, p_type: Optional[str] = None,
                           limit: int = 500):
    conn = get_connection()
    try:
        return {"parties": list_parties(conn, q=q, p_type=p_type, limit=limit),
                "p_types": [{"value": k, "label": v} for k, v in VALID_P_TYPES.items()]}
    finally:
        conn.close()


@app.post("/api/parties")
async def api_create_party(payload: PartyCreate):
    conn = get_connection()
    try:
        result = create_party_full(conn, payload)
        return {"status": "success", **result}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


@app.get("/api/vouchers")
async def api_list_vouchers(limit: int = 50, q: Optional[str] = None,
                            entry_type: Optional[str] = None):
    conn = get_connection()
    try:
        return {"vouchers": list_vouchers(conn, limit=limit, q=q, entry_type=entry_type)}
    finally:
        conn.close()


@app.get("/api/voucher/{at_id}")
async def api_get_voucher(at_id: str):
    conn = get_connection()
    try:
        data = fetch_voucher(conn, at_id)
        return {"status": "success" if data.get("found") else "error", **data}
    finally:
        conn.close()


@app.put("/api/voucher/{at_id}")
async def api_update_voucher(at_id: str, payload: VoucherUpdate):
    conn = get_connection()
    try:
        return update_voucher(conn, at_id, payload)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


@app.post("/api/voucher/{at_id}/void")
async def api_void_voucher(at_id: str):
    conn = get_connection()
    try:
        return void_voucher(conn, at_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


@app.get("/")
async def root():
    return {"bot": bot.name, "status": "online", "database": "MySQL",
            "version": "v6 - chat posting + voucher update + profile creation"}


if __name__ == "__main__":
    import uvicorn
    print(f"\n{'='*62}\n{bot.name} - AI Accounting Bot (v6)\n{'='*62}")
    print(f"System ID: {SYSTEM_ID}  |  Cr User: {USER_ID}")
    print("Accounts resolved from v_trans_accounts_m2 (postable leaves, tenant-scoped).")
    print("Nature taken from the first digit of the account code.")
    print("at_id = max(YYMM||doctype||000001, MAX(at_id)+1) per doc_type + system_id.")
    print("CRV posts Dr Bank / Cr Revenue;  CPV posts Dr Expense / Cr Bank.")
    print("v6: GET/PUT /api/voucher/{id} to edit, GET/POST /api/parties for profiles.\n")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))