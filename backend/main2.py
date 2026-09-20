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

C. CHART OF ACCOUNTS - VIEW AND ADD    chat commands + GET /api/chart
   "show chart" / "expense chart" renders the nature -> main -> sub tree the
   host UI's Chart of Accounts page shows, reconstructed purely from the
   postable set (v_trans_accounts_m2) - no extra tables needed to read it.

   "add expense account Fuel", "add bank account Meezan 1234", "add account
   FICA under Payroll Taxes" create a new main or sub account exactly the
   way the host PHP admin's "Add chart of accounts" button does: a shared
   row in account_main/account_sub (global, keyed across every tenant) plus
   a per-tenant row in account_main_user/account_sub_user, with the code
   minted as parent*10000+sequence, floored into the tenant-added range
   (CHART_USER_SEQ_FLOOR) so it can never collide with a future stock
   account. Adding a sub under a main that already has vouchers posted to it
   would silently turn that main into a heading - refused by default
   (ALLOW_ORPHANING_POSTED_MAIN=1 to match the host app's own behaviour).

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
import traceback
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any, Tuple, NamedTuple

import mysql.connector
from fastapi import FastAPI, UploadFile, File, Form
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
    # When true, a message that would CREATE a voucher stops one step short:
    # everything is parsed and resolved, nothing is written, and the resolved
    # fields come back as a `draft` for the operator to confirm or correct.
    # Every other command (show / update / void / profile / chart) is
    # unaffected and still executes, so the client can send preview=true for
    # every message and only branch on whether a draft came back.
    preview: Optional[bool] = False


class VoucherCommit(BaseModel):
    """
    A draft the operator has reviewed, posted by CODE - never by fuzzy name.
    This is the second half of the preview flow: the guesser ran during
    preview, the human accepted or corrected it, and what arrives here is a
    decision, not a sentence to interpret.
    """
    session_id: Optional[str] = None
    entry_type: str                              # CRV | CPV
    amount: float
    transaction_date: str                        # YYYY-MM-DD (or anything parseable)
    party_code: Optional[str] = None             # existing party...
    party_name: Optional[str] = None             # ...or a name to create one from
    bank_acc_code: str
    category_acc_code: str
    cheque_no: Optional[str] = None
    description: Optional[str] = None
    source_message: Optional[str] = None         # what was typed, for the log


class EditCommit(BaseModel):
    """An edit the operator reviewed. Accounts and party arrive as CODES."""
    session_id: Optional[str] = None
    at_id: str
    # 'void' reverses the voucher instead of changing it; the fields below are
    # then ignored, since there is nothing left to save.
    op: Optional[str] = None
    amount: Optional[float] = None
    transaction_date: Optional[str] = None
    party_code: Optional[str] = None
    bank_acc_code: Optional[str] = None
    category_acc_code: Optional[str] = None
    cheque_no: Optional[str] = None
    description: Optional[str] = None


class EditBatchRequest(BaseModel):
    """
    Open several vouchers for editing at once. `instruction` is the optional
    English change to pre-apply to all of them - resolved, never written, so
    the operator still confirms each one.
    """
    session_id: Optional[str] = None
    at_ids: List[str]
    instruction: Optional[str] = None


class AccountCommit(BaseModel):
    """
    A chart account the operator reviewed. The parent arrives as a CODE.
    `op` + `code` mean an existing account is being renamed or retired;
    without them this creates a new one.
    """
    session_id: Optional[str] = None
    name: Optional[str] = None
    level: Optional[str] = None                  # 'main' (under a nature) | 'sub'
    parent_code: Optional[str] = None
    op: Optional[str] = None                     # rename | deactivate | activate
    code: Optional[str] = None
    source_message: Optional[str] = None


class PartyCommit(BaseModel):
    """
    A profile the operator reviewed. Same fields as PartyCreate, plus p_code -
    present means "this one already exists, change it"; absent means create.
    """
    session_id: Optional[str] = None
    p_code: Optional[str] = None
    p_type: str
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
    p_account: Optional[str] = None
    status: int = 1
    source_message: Optional[str] = None


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

# Let the language model rephrase a clarification in a friendlier voice. The
# facts are always composed here first and verified afterwards (see _voice),
# so this only ever changes wording. ASSISTANT_VOICE=0 turns it off.
ASSISTANT_VOICE = os.getenv("ASSISTANT_VOICE", "1") == "1"

# What the button the person pressed says about what they are probably doing.
# Passed to the model as context, never used as a decision - the sentence wins.
MODE_HINT = {
    'cpv': 'the user pressed "Create CPV", so this is most likely money going OUT',
    'crv': 'the user pressed "Create CRV", so this is most likely money coming IN',
    'update': 'the user pressed "Edit a voucher", so they are probably naming an '
              'existing voucher by id or by date rather than creating one',
    'view': 'the user pressed "View transactions", so they are probably asking to '
            'see records rather than post one',
    'profile': 'the user pressed "Add a customer", so this is probably a new '
               'customer/vendor/employee profile rather than a voucher',
    'editprofile': 'the user pressed "Edit a customer", so they are probably '
                   'changing a detail on an existing profile - a phone, an '
                   'email, an address - rather than creating anything',
    'chart': 'the user pressed "Add to chart of accounts", so this is probably a '
             'new ledger account rather than a voucher',
    'editchart': 'the user pressed "Rename or retire an account", so they are '
                 'probably renaming or deactivating an existing ledger account',
    'chartview': 'the user pressed "View chart of accounts"',
    # This one is here so the mode is never silently unknown, but it is not a
    # typed grammar: the button opens a file picker, and a statement is read by
    # the deterministic reader alone. If a sentence arrives under this mode at
    # all, the person has typed something else while the panel was open, so the
    # hint says to treat it as ordinary.
    'statement': 'the user pressed "Read a statement", which uploads a file '
                 'rather than typing - so anything typed here is an ordinary '
                 'request and the mode says nothing about it',
}
_VOICE_OFF = False          # set once a voice call fails; see _voice()

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
    for i, w in enumerate(name.split()):
        bare = w.strip('.,').upper()
        # "of", "and", "the" stay lowercase inside a name - "Bank of America",
        # not "Bank Of America" - but never as the first word.
        if i and bare.lower() in _LOWERCASE_IN_NAMES:
            out.append(w.lower())
        elif bare in _INITIALISMS:
            out.append(bare + w[len(w.rstrip('.,')):])
        # "3S", "A1", "H2O" - a short mix of letters and digits is a name as
        # typed, never a shouted word, so it survives title-casing intact.
        elif (len(w) <= 5 and any(c.isdigit() for c in w)
              and any(c.isalpha() for c in w)):
            out.append(w if any(c.isupper() for c in w)
                       else re.sub(r'[a-z]', lambda mm: mm.group().upper(), w, count=1))
        elif not shouted and w.isupper() and len(w) <= 4 and not w.isdigit():
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return ' '.join(out)


_BUSINESS_SUFFIXES = {
    'LLC', 'L.L.C', 'LTD', 'INC', 'LLP', 'PLC', 'PC', 'LP', 'CO', 'CORP',
    'SA', 'NV', 'BV', 'GMBH', 'AG', 'PVT', 'PTE', 'PTY', 'DBA', 'USA', 'US',
}

# Only the ones people really do write in capitals. "Corp" and "Co" are words,
# so "Acme Corp" reads better than "Acme CORP".
_INITIALISMS = {'LLC', 'L.L.C', 'LTD', 'INC', 'LLP', 'PLC', 'PC', 'LP',
                'GMBH', 'AG', 'NV', 'BV', 'SA', 'DBA', 'USA', 'US'}
_LOWERCASE_IN_NAMES = {'of', 'and', 'the', 'for', 'de', 'la', 'von', 'van'}


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


def _expand_year(raw: Optional[str]) -> int:
    """'26' -> 2026, '2026' -> 2026, missing -> this year."""
    if not raw:
        return datetime.now().year
    y = int(raw)
    return y + 2000 if y < 100 else y


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
    m = re.search(r'\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b', text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b', text)
    if m:
        try:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass
    # Month-name forms. The separator may be a space, a hyphen or a dot, so
    # "7-june-2026" and "Jun. 7, 26" read the same as "7 June 2026" - people
    # type dates all three ways and a rejected date reads as a broken app.
    # Day-first is tried first on purpose: in "7-Jun-26" the month-first
    # pattern would otherwise read "Jun-26" and lose the 7, posting the 26th.
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?[\s\-./]+(' + _MONTH_ALT + r')\.?'
                  r'(?:,?[\s\-./]+(\d{2,4}))?\b', t)
    if m:
        try:
            d = int(m.group(1))
            mo = _MONTH_NAMES[m.group(2)]
            y = _expand_year(m.group(3))
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(' + _MONTH_ALT + r')\.?[\s\-./]+(\d{1,2})(?:st|nd|rd|th)?'
                  r'(?:,?[\s\-./]+(\d{2,4}))?\b', t)
    if m:
        try:
            mo = _MONTH_NAMES[m.group(1)]
            d = int(m.group(2))
            y = _expand_year(m.group(3))
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
# The bank leg is often introduced by a preposition that is NOT a marker on its
# own - "on bank meezan 1234", "in my account 9523". Bare "on" can't be a marker
# (it introduces dates far more often), so it only counts when a bank word
# follows it. The lookahead keeps that word inside the segment, because half the
# time it is part of the account's real name: "on bank of amercia".
_BANK_WORD = r'(?:the\s+|my\s+|our\s+)?(?:bank|banks|account|acct|a/c|cash)\b'
_MARKER_RE = re.compile(
    r'\b(to|from|for|via|through|thru|into|using|out\s+of|by\s+cheque|by\s+check'
    r'|(?:in|on|at|by|with)(?=\s+' + _BANK_WORD + r'))\b',
    re.IGNORECASE)

# Every way a date may be written, in one place. Used to strip a date out of
# a voucher line before the amount is read, and to recognise a date typed on
# its own (see _parse_voucher_date_command).
_DATE_TOKEN = (
    r'today|yesterday|tomorrow'
    r'|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}'
    r'|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}'
    r'|(?:' + _MONTH_ALT + r')\.?[\s\-./]+\d{1,2}(?:st|nd|rd|th)?(?:,?[\s\-./]+\d{2,4})?'
    r'|\d{1,2}(?:st|nd|rd|th)?[\s\-./]+(?:' + _MONTH_ALT + r')\.?(?:,?[\s\-./]+\d{2,4})?'
)

_DATE_TAIL_RE = re.compile(
    r'\b(?:on|dated|date)?\s*\b(' + _DATE_TOKEN + r')\b', re.IGNORECASE)

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
    for rx in (_AMOUNT_CUR_RE, _AMOUNT_DEC_RE):
        m = rx.findall(work)
        if m:
            try:
                return abs(float(m[-1].replace(',', '')))
            except ValueError:
                continue
    # A bare integer takes the FIRST match, not the last. Without a currency
    # symbol the trailing numbers in a line are almost never the amount -
    # they are the account's identifying digits or an invoice number:
    #     "Paid 450 to Handy Fix from Bank of America 9523"   -> 450, not 9523
    #     "Received 1250 from ABC for invoice 2045 into Chase 4582" -> 1250
    # The decimal and currency forms above still take the last match, because
    # a bank-statement line ends with its amount.
    m = _AMOUNT_INT_RE.findall(work)
    if m:
        try:
            return abs(float(m[0].replace(',', '')))
        except ValueError:
            pass
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
                   'into': 'bank', 'for': 'category',
                   'in': 'bank', 'on': 'bank', 'at': 'bank', 'by': 'bank',
                   'with': 'bank'}
    else:
        role_of = {'from': 'party', 'via': 'bank', 'through': 'bank',
                   'thru': 'bank', 'into': 'bank', 'to': 'bank',
                   'using': 'bank', 'out of': 'bank', 'for': 'category',
                   'in': 'bank', 'on': 'bank', 'at': 'bank', 'by': 'bank',
                   'with': 'bank'}

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


# --------------------------------------------------------------------------
# Reading a bank statement.
#
# A statement is the same job as typing, done 60 times. So it is deliberately
# NOT a new way to write to the ledger: the reader below turns a PDF into the
# same resolved-but-unwritten drafts a typed sentence produces, and they come
# back as the queue a bulk edit already uses. Every row is still read and
# saved one at a time, by code, through /api/commit.
#
#   The typing is bulk. The writing never is. Now the reading is too.
#
# Two layouts are understood, because they are the two the operator actually
# has:
#
#   1. a bank/checking statement, where DIRECTION COMES FROM THE SECTION the
#      row sits in - "DEPOSITS AND ADDITIONS" vs "ELECTRONIC WITHDRAWALS".
#      The rows themselves carry no sign at all, so the heading is the only
#      signal there is, and losing track of it would reverse every voucher
#      under it.
#   2. a credit-card statement, where there are no sections and direction
#      comes from the SIGN - a negative amount is a payment to the card, a
#      positive one is a purchase.
#
# Anything that matches neither is read line by line with the existing
# single-line reader, and a row whose direction is still unknowable is
# skipped and named rather than guessed at.
# --------------------------------------------------------------------------

# Rows carry MM/DD and nothing else, so the year lives in the statement header
# and nowhere else on the page. Getting this wrong posts a whole statement
# into the wrong fiscal year, so it is read explicitly rather than assumed to
# be "now" - a January statement is very often read in February, and a
# December-to-January one straddles two years at once.
_STMT_PERIOD_RES = [
    # "August 01, 2025 through August 29, 2025"
    re.compile(r'(?P<m1>[A-Z][a-z]+)\s+(?P<d1>\d{1,2}),?\s+(?P<y1>\d{4})\s+'
               r'(?:through|to|-|–)\s+'
               r'(?P<m2>[A-Z][a-z]+)\s+(?P<d2>\d{1,2}),?\s+(?P<y2>\d{4})'),
    # "Opening/Closing Date  08/05/25 - 09/04/25"
    re.compile(r'(?P<mm1>\d{1,2})/(?P<dd1>\d{1,2})/(?P<yy1>\d{2,4})\s*(?:-|–|through|to)\s*'
               r'(?P<mm2>\d{1,2})/(?P<dd2>\d{1,2})/(?P<yy2>\d{2,4})'),
]

_MONTHS = {m.lower(): i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'], start=1)}

# "Account Number: 000000588379993" / "Account Number: XXXX XXXX XXXX 8704"
_STMT_ACCT_RE = re.compile(
    r'account\s*(?:number|no\.?|#)\s*[:\-]?\s*'
    r'(?P<acct>[X\*x\d][X\*x\d\s\-]{3,30}\d)', re.IGNORECASE)

# A row in either layout: MM/DD (or MM/DD/YY), then text, then the amount at
# the end of the line. The amount may be signed, bracketed, or carry a $.
# The gap after the date is ONE space or more, not two: how wide it comes out
# depends entirely on which extractor read the PDF. pdftotext -layout pads the
# columns out; pdfplumber gives "08/01 Zelle Payment To ... $150.00" with a
# single space, and requiring two silently dropped four rows in five while
# still looking like it had read the file.
#
# The same collapse happens in front of the amount on a long description, so
# the column gap can't be relied on there either. What CAN be relied on is
# that the amount is the last thing on the line and has cents: the pattern is
# anchored to the end and requires ".dd", so a reference number in the middle
# of the description - "Zelle Payment To Yadel 25725052480" - cannot be
# mistaken for it however the spacing comes out.
_STMT_ROW_RE = re.compile(
    r'^\s{0,40}(?P<mm>\d{1,2})/(?P<dd>\d{1,2})(?:/(?P<yy>\d{2,4}))?\s+'
    r'(?P<body>\S.*?)\s+'
    r'(?P<open>\()?\s*(?P<sign>[-+])?\s*\$?\s*'
    r'(?P<amt>\d[\d,]*\.\d{2})\s*(?P<close>\))?\s*(?P<trail>-)?\s*$')

# The cost of that looseness: a DAILY ENDING BALANCE table is three columns of
# date-and-amount pairs, and its last pair would now read as a transaction.
# The heading above it normally stops the reader before it gets there, but a
# statement that words that heading differently would post thirty balances as
# vouchers. A date followed by an amount INSIDE the description is the
# signature of that table and of nothing else.
_STMT_BALANCE_ROW_RE = re.compile(r'\d{1,2}/\d{1,2}\s+\$?[\d,]+\.\d{2}')

# A heading is bare words. The CHECKING SUMMARY block at the top of the page
# repeats every section's name beside its total - "Electronic Withdrawals 36
# -28,654.16" - and reading that as a heading re-opened a section that the
# summary had just closed, so the rows under the NEXT heading inherited the
# wrong direction. A line carrying an amount is a total, never a heading.
_STMT_MONEY_RE = re.compile(r'\d[\d,]*\.\d{2}')

# Section headings on a checking statement, and what each one means. Order
# matters only in that the first match wins on a given line.
_STMT_SECTIONS: List[Tuple[re.Pattern, Optional[str], str]] = [
    (re.compile(r'\bDEPOSITS?\s+AND\s+ADDITIONS?\b', re.I), 'CRV', 'Deposits and additions'),
    (re.compile(r'\bDEPOSITS?\b(?!\s+and\s+withdrawal)', re.I), 'CRV', 'Deposits'),
    (re.compile(r'\bELECTRONIC\s+WITHDRAWALS?\b', re.I), 'CPV', 'Electronic withdrawals'),
    (re.compile(r'\bATM\s*&?\s*DEBIT\s+CARD\s+WITHDRAWALS?\b', re.I), 'CPV', 'Card withdrawals'),
    (re.compile(r'\bOTHER\s+WITHDRAWALS?\b', re.I), 'CPV', 'Other withdrawals'),
    (re.compile(r'\bCHECKS?\s+PAID\b', re.I), 'CPV', 'Checks paid'),
    (re.compile(r'\bWITHDRAWALS?\s+AND\s+DEBITS?\b', re.I), 'CPV', 'Withdrawals'),
    (re.compile(r'\bFEES?(?:\s+AND\s+CHARGES?)?\s*$', re.I), 'CPV', 'Fees'),
    (re.compile(r'\bACCOUNT\s+ACTIVITY\b', re.I), None, 'Card activity'),
    (re.compile(r'\bTRANSACTIONS?\b\s*$', re.I), None, 'Transactions'),
]

# Headings that END the transaction part of the page. A running-balance table
# is full of dates and amounts and would otherwise read as 30 more vouchers.
_STMT_STOP_RE = re.compile(
    r'\b(DAILY\s+ENDING\s+BALANCE|ENDING\s+BALANCE|CHECKING\s+SUMMARY|'
    r'ATM\s*&?\s*DEBIT\s+CARD\s+SUMMARY|INTEREST\s+CHARGES?|'
    r'ACCOUNT\s+SUMMARY|SUMMARY\s+OF\s+ACCOUNT|IN\s+CASE\s+OF\s+ERRORS|'
    r'YEAR-TO-DATE|Totals\s+Year-to-Date)\b', re.I)

# Lines inside a section that are totals, not transactions.
_STMT_TOTAL_RE = re.compile(r'^\s*(total|subtotal|beginning|ending|previous)\b', re.I)

# Whether a line is a HEADING at all, before asking which one it is.
#
# The patterns above were matching prose: a paragraph of card small print
# containing the word "deposits" opened a DEPOSITS section, and every purchase
# under it was then read as money coming IN. An inverted voucher is the worst
# thing this reader can produce, and one sentence of legalese was enough.
#
# A real heading is short and shouted. Both conditions are needed - "FEES" is
# short but so is half the page, and a long ALL-CAPS line is a disclaimer.
def _stmt_is_heading(line: str) -> bool:
    s = _stmt_undouble(line).strip()
    return bool(s) and len(s) <= 60 and not re.search(r'[a-z]', s)


# pdfplumber renders faux-bold text by drawing it twice, so a bold heading
# arrives as "AACCCCOOUUNNTT AACCTTIIVVIITTYY" and matches nothing. The
# collapse is only applied when EVERY letter is doubled, which is what that
# artifact looks like and what an English word does not.
_STMT_DOUBLED_RE = re.compile(r'\b(?:([A-Za-z])\1)+\b')


def _stmt_undouble(line: str) -> str:
    return _STMT_DOUBLED_RE.sub(
        lambda m: m.group(0)[::2], line)


class StatementRow(NamedTuple):
    """One line of a statement, before anything has been looked up."""
    date: str                 # YYYY-MM-DD
    description: str          # as printed
    amount: float             # always positive
    entry_type: Optional[str]  # CRV | CPV | None when unknowable
    section: str
    line_no: int


def _stmt_year_window(text: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(start_month, start_year, end_year) from the statement header."""
    head = text[:6000]
    for rx in _STMT_PERIOD_RES:
        m = rx.search(head)
        if not m:
            continue
        g = m.groupdict()
        if g.get('m1'):
            m1 = _MONTHS.get(g['m1'].lower())
            if not m1:
                continue
            return m1, int(g['y1']), int(g['y2'])
        y1, y2 = int(g['yy1']), int(g['yy2'])
        y1 += 2000 if y1 < 100 else 0
        y2 += 2000 if y2 < 100 else 0
        return int(g['mm1']), y1, y2
    return None, None, None


def _stmt_account_hint(text: str) -> Optional[str]:
    """The last four digits of the account the statement belongs to."""
    m = _STMT_ACCT_RE.search(text[:8000])
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group('acct'))
    return digits[-4:] if len(digits) >= 4 else None


def _stmt_attach_continuations(rows: List['StatementRow'],
                               lines: List[str]) -> List['StatementRow']:
    """Glue a wrapped row's trailing lines back onto its description."""
    starts = {r.line_no for r in rows}
    out: List[StatementRow] = []
    for idx, r in enumerate(rows):
        stop = rows[idx + 1].line_no if idx + 1 < len(rows) else len(lines) + 1
        extra: List[str] = []
        for j in range(r.line_no, min(stop - 1, r.line_no + 5)):
            if (j + 1) in starts:
                break
            nxt = lines[j].strip() if j < len(lines) else ''
            if not nxt:
                continue
            # A continuation is indented text with no date and no heading. A
            # blank line does not end it - the layout extractor double-spaces
            # everything - but another row or a heading does.
            if _STMT_ROW_RE.match(lines[j]) or _STMT_STOP_RE.search(nxt):
                break
            if any(rx.search(nxt) for rx, _e, _l in _STMT_SECTIONS):
                break
            if re.match(r'^(page\s+\d|\*|total\b)', nxt, re.IGNORECASE):
                break
            extra.append(nxt)
        if extra:
            joined = re.sub(r'\s{2,}', ' ', ' '.join([r.description] + extra)).strip()
            r = r._replace(description=joined[:400])
        out.append(r)
    return out


def parse_statement_text(text: str) -> Dict[str, Any]:
    """
    A statement as rows, with nothing looked up and nothing written.

    Pure text in, plain dicts out - no database, no network, no model. That
    is deliberate: this is the part most likely to meet a layout it has never
    seen, and it should be testable from a string.
    """
    lines = (text or '').splitlines()
    start_month, start_year, end_year = _stmt_year_window(text)
    acct = _stmt_account_hint(text)

    rows: List[StatementRow] = []
    unreadable: List[Dict[str, Any]] = []
    section, section_type = '', None
    stopped = False

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            continue

        # A section heading, or the end of the transactional part of the page.
        if not _STMT_ROW_RE.match(line):
            if _STMT_STOP_RE.search(_stmt_undouble(line)):
                section, section_type, stopped = '', None, True
                continue
            if _STMT_MONEY_RE.search(line) or not _stmt_is_heading(line):
                continue
            plain = _stmt_undouble(line)
            for rx, etype, label in _STMT_SECTIONS:
                if rx.search(plain):
                    section, section_type, stopped = label, etype, False
                    break
            continue

        if stopped:
            continue

        m = _STMT_ROW_RE.match(line)
        body = m.group('body').strip()
        if _STMT_TOTAL_RE.match(body) or _STMT_TOTAL_RE.match(line.strip()):
            continue
        if _STMT_BALANCE_ROW_RE.search(body):
            continue

        try:
            amount = float(m.group('amt').replace(',', ''))
        except ValueError:
            continue
        if amount == 0:
            continue

        # A trailing '-' is how some statements mark a credit ("$1.87-").
        negative = (m.group('sign') == '-'
                    or bool(m.group('open') and m.group('close'))
                    or m.group('trail') == '-')

        mm, dd = int(m.group('mm')), int(m.group('dd'))
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        yy = m.group('yy')
        if yy:
            year = int(yy) + (2000 if int(yy) < 100 else 0)
        elif start_year:
            # A period that crosses New Year prints December and January rows
            # with the same MM/DD shape; the month decides which year it is.
            year = start_year if (start_month is None or mm >= start_month) else (end_year or start_year)
        else:
            year = datetime.now().year
        try:
            iso = date(year, mm, dd).isoformat()
        except ValueError:
            unreadable.append({'line': i + 1, 'text': line.strip(),
                               'why': 'the date is not a real one'})
            continue

        # Direction: the section if there is one, otherwise the sign. On a
        # card statement a NEGATIVE amount is money leaving the card account
        # (a payment received against it), and a positive one is a purchase.
        etype = section_type
        if etype is None:
            etype = 'CRV' if negative else 'CPV'
            if not section:
                # No section heading at all and no sign either way: fall back
                # to the wording, and admit it when that says nothing.
                if not negative:
                    if _STMT_CREDIT_RE.search(body):
                        etype = 'CRV'
                    elif _STMT_DEBIT_RE.search(body):
                        etype = 'CPV'
                    else:
                        etype = None
        elif negative and section_type:
            # A negative number inside a named section contradicts the
            # heading - a refund inside a withdrawals block, say. The sign
            # wins, because it is on the row itself.
            etype = 'CRV' if section_type == 'CPV' else 'CPV'

        if etype is None:
            unreadable.append({'line': i + 1, 'text': line.strip(),
                               'why': 'nothing says whether money came in or went out'})
            continue

        rows.append(StatementRow(date=iso, description=re.sub(r'\s{2,}', ' ', body),
                                 amount=abs(amount), entry_type=etype,
                                 section=section or ('Card activity' if not section_type else ''),
                                 line_no=i + 1))

    # An ACH row wraps over three or four lines - the date and the amount are
    # on the first, and the useful half of the name ("Ind Name:...") is on the
    # last. Dropping the continuation loses the part a person would read, so
    # it is glued back onto the row it belongs to.
    if rows:
        rows = _stmt_attach_continuations(rows, lines)

    return {
        'rows': rows,
        'unreadable': unreadable,
        'account_hint': acct,
        'period': ({'start_year': start_year, 'end_year': end_year}
                   if start_year else None),
        'layout': ('checking' if any(r.section and 'Card' not in r.section for r in rows)
                   else ('card' if rows else 'unknown')),
    }


# --- pulling a counterparty out of a statement description -----------------
#
# A description is not a sentence; it is a machine's audit trail with a name
# somewhere inside it. What matters is stripping the confirmation ids, because
# "Zelle Payment From Kevin B Becker 25697289472" and the same payment next
# month carry different ids and would otherwise never match the same profile.

_STMT_NOISE_RE = re.compile(
    r'\b(?:'
    r'\d{9,}'                              # 25697289472 - never part of a name
    r'|trn|tc|eed|sec|ccd|ppd|web|ach|eft|pos'
    r'|trace#?|ind\s+id|orig\s+id|desc\s+date|co\s+entry\s+descr'
    r'|transaction#?|confirmation#?|ref#?|id#?'
    r')\b[:#]?\s*', re.IGNORECASE)

# A Zelle or ACH line ends in a confirmation code - "Cof4Xqdg85O7",
# "Ctinqwoggugc", "Jpm99Bhveu4W". It changes every time, so leaving it on
# means the same payer never matches the same profile twice.
#
# It is stripped as the LAST TOKEN rather than by what it looks like, because
# what it looks like is a surname: "Kellie Maisenbacher Ctinqwoggugc" has two
# twelve-letter words in a row and only one of them is a person. Position is
# the reliable signal; spelling is not. And it never strips the only token it
# has, so "Roula38Thstreet" survives being the whole name.
_STMT_TAIL_CODE_RE = re.compile(r'\s+[A-Za-z0-9]{8,}\s*$')

_STMT_PARTY_RES = [
    # "Orig CO Name:Nys Dtf Wt    Orig ID:..."  -> the originating company
    (re.compile(r'orig\s+co\s+name\s*:\s*(?P<who>.+?)(?=\s+orig\s+id|\s*:|$)',
                re.IGNORECASE), None),
    # "Zelle Payment From Kevin B Becker 256..." / "... To Yadel 257..."
    (re.compile(r'\b(?:zelle|venmo|cash\s*app)\s+payment\s+(?:from|to)\s+(?P<who>.+)$',
                re.IGNORECASE), 'coded'),
    # "08/04 Online ACH Payment 11182566613 To Santosa (_######4994)"
    (re.compile(r'\b(?:online|same-day|recurring)?\s*ach\s+(?:payment|debit|credit|deposit)\s*'
                r'\d*\s*(?:to|from)\s+(?P<who>[^(]+)', re.IGNORECASE), 'coded'),
    # "Recurring Card Purchase 08/12 Spectrum 855-707-7328 MO Card 1284"
    (re.compile(r'\bcard\s+(?:purchase|payment)\s+(?:\d{1,2}/\d{1,2}\s+)?(?P<who>.+?)'
                r'(?=\s+\d{3}[-.]\d{3}|\s+card\s+\d|$)', re.IGNORECASE), None),
    # "Online Transfer To Chk ...1690 Transaction#: 258..."  - an own account
    (re.compile(r'\b(?:online\s+)?transfer\s+(?:to|from)\s+(?P<who>(?:chk|sav|savings|checking)'
                r'\s*\.*\s*\d+)', re.IGNORECASE), 'transfer'),
    # A generic trailing "to X" / "from X"
    (re.compile(r'\b(?:paid\s+to|payment\s+to|to|from)\s+(?P<who>[A-Za-z][^:()]{2,60})$',
                re.IGNORECASE), None),
]

# Descriptions that name no counterparty at all - the bank itself is the other
# side. Guessing a vendor from "Monthly Service Fee" would create a profile
# called "Monthly Service".
_STMT_BANK_ITSELF_RE = re.compile(
    r'\b(monthly\s+service\s+fee|service\s+charge|maintenance\s+fee|overdraft'
    r'|returned\s+item|nsf|interest\s+(?:charge|earned|paid)|annual\s+fee'
    r'|late\s+fee|wire\s+fee|atm\s+fee|standard\s+ach\s+pmnts?\s+initial\s+fee'
    r'|remote\s+online\s+deposit|mobile\s+deposit|counter\s+credit'
    r'|automatic\s+payment)\b', re.IGNORECASE)


def statement_party(description: str) -> Tuple[Optional[str], Optional[str]]:
    """
    (party name, why-there-isn't-one). A statement line that names no
    counterparty is not a broken line - a service fee genuinely has none -
    so the caller shows the row with an empty party for the operator to fill,
    rather than inventing a vendor from the fee's own wording.
    """
    desc = re.sub(r'\s{2,}', ' ', (description or '').strip())
    if not desc:
        return None, 'the line has no description'

    if _STMT_BANK_ITSELF_RE.search(desc):
        return None, 'this is the bank itself, not a customer or vendor'

    who, coded = None, False
    for rx, kind in _STMT_PARTY_RES:
        m = rx.search(desc)
        if m:
            who, coded = m.group('who'), (kind == 'coded')
            break
    if not who:
        who = desc

    who = re.sub(r'\(_?#+\d+\)', ' ', who)          # (_######4994)
    # The tail code goes FIRST. Stripping the long digit strings first would
    # remove the real code and leave the tail rule to eat the last word of the
    # name instead - "464 Putnam Avenue Condominium" losing its Condominium.
    if coded:
        trimmed = _STMT_TAIL_CODE_RE.sub('', who.strip())
        if re.search(r'[A-Za-z]{2}', trimmed):
            who = trimmed
    who = _STMT_NOISE_RE.sub(' ', who)
    who = re.sub(r'\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', ' ', who)
    # A card line ends in the merchant's phone number and state - "Public
    # Storage 77601 800-567-0759 NY". None of that is the vendor's name, and
    # leaving it in means the same vendor never matches itself twice.
    who = re.sub(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b', ' ', who)
    who = re.sub(r'\s+[A-Z]{2}\s*$', ' ', who)
    who = re.sub(r'\s+\d{4,}\s*', ' ', who)
    who = re.sub(r'[.,;:#]+$', '', who.strip())
    who = re.sub(r'\s+', ' ', who).strip(' .,-&*/')
    # A name that is now only digits, or a single letter, is not a name.
    if not who or len(who) < 2 or not re.search(r'[A-Za-z]{2}', who):
        return None, 'no name could be read out of the description'
    if len(who) > 80:
        who = who[:80].rsplit(' ', 1)[0]
    return who, None


# Money moving between two accounts the company already owns. This is the one
# reading that is dangerous to accept quietly: a card autopay or a sweep into
# savings posted as a CRV invents revenue that never happened, and the row
# looks exactly like a deposit while it does it. So it is still offered as a
# draft - the operator may well want it, with the other account as the second
# leg - but it is flagged loudly rather than slipped through.
_STMT_TRANSFER_RE = re.compile(
    r'\b(?:online\s+)?transfer\s+(?:to|from)\b'
    r'|\bautomatic\s+payment\b|autopay|\bpayment\s*-\s*thank\s+you\b'
    r'|\bto\s+(?:chk|sav|savings|checking)\b|\bfrom\s+(?:chk|sav|savings|checking)\b',
    re.IGNORECASE)


def _pretty_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, '%Y-%m-%d').strftime('%m/%d/%Y')
    except Exception:
        return iso or ''


def statement_is_transfer(description: str) -> bool:
    return bool(_STMT_TRANSFER_RE.search(description or ''))


def _looks_transactional(message: str) -> bool:
    """Has a direction word AND a number - used to stop the intent keyword
    shortcuts from hijacking a real posting (e.g. 'report' in a description)."""
    m = message or ''
    return bool((_CPV_VERBS.search(m) or _CRV_VERBS.search(m))
                and _parse_amount(m) is not None)


# --------------------------------------------------------------------------
# Why a line didn't parse.
#
# "I couldn't read that" is a dead end. The person is left guessing which of
# the four things I need was missing - and often only one of them was. So the
# refusal is itself a checklist: what I DID find, what I still need, and the
# nearest thing they probably meant.
# --------------------------------------------------------------------------
_DATEY_TOKEN_RE = re.compile(
    r'^(?:\d{1,4}(?:st|nd|rd|th)?|' + _MONTH_ALT + r'|today|yesterday|tomorrow'
    r'|on|of|for|dated|date)$', re.IGNORECASE)


def _looks_like_a_date_attempt(text: str) -> bool:
    """Only numbers, separators and month words - so a date was meant."""
    tokens = [t for t in re.split(r'[\s/.,\-]+', text.strip()) if t]
    return bool(tokens) and len(tokens) <= 5 and any(
        c.isdigit() for c in text) and all(
        _DATEY_TOKEN_RE.match(t) for t in tokens)


def _diagnose_unparsed(message: str) -> str:
    """A refusal that tells the person exactly what to add."""
    text = (message or '').strip()
    quoted = f'"{text}"' if len(text) <= 60 else 'that'

    # 1. A bare number that looks like it was meant to be a voucher id.
    digits = re.fullmatch(r'(?:crv|cpv)?[\s\-#]*(\d+)', text, re.IGNORECASE)
    if digits:
        n = digits.group(1)
        return (f"{quoted} looks like a voucher id, but ids are 8 to 20 digits "
                f"and this one is {len(n)}.\n\n"
                "  • To open a voucher:  260902000001\n"
                "  • To see a whole day: 7-june-2026\n"
                "  • To post an entry:   Paid $450 to Handy Fix LLC for "
                "Repair and Maintenance from Bank of America 9523")

    # 2. Mostly digits and separators - they meant a date I couldn't read.
    if _looks_like_a_date_attempt(text):
        return (f"I couldn't read {quoted} as a date. These all work:\n\n"
                "  7-june-2026     7 June 2026     7-jun-26\n"
                "  06/07/2026      2026-06-07      today / yesterday\n\n"
                "A date on its own lists that day's vouchers so you can tick "
                "the ones to fix.")

    # 3. A real sentence: say which of the three required pieces are missing.
    has_dir = bool(_CPV_VERBS.search(text) or _CRV_VERBS.search(text))
    amount = _parse_amount(text)
    date_iso = parse_date_text(text)
    # A name is anything left once the numbers and keywords are stripped out.
    residue = re.sub(r'[\d$£€,./\-]+', ' ', text)
    residue = re.sub(r'\b(paid|pay|payment|spent|received|receive|got|from|to|'
                     r'for|via|through|into|on|the|a|an|and|of|dated|date|'
                     r'cheque|check|no|today|yesterday|tomorrow|' + _MONTH_ALT +
                     r')\b', ' ', residue, flags=re.IGNORECASE)
    has_name = len(_clean_segment(residue)) >= 3

    found, missing = [], []
    (found if has_dir else missing).append(
        'a direction — "paid" for money out, "received" for money in'
        if not has_dir else 'the direction')
    (found if amount is not None else missing).append(
        'an amount — like $450 or 450.00' if amount is None
        else f'the amount (${amount:,.2f})')
    (found if has_name else missing).append(
        "a name — who you paid, or who paid you" if not has_name
        else 'a name')
    if date_iso:
        try:
            pretty = datetime.strptime(date_iso, '%Y-%m-%d').strftime('%m/%d/%Y')
        except ValueError:
            pretty = date_iso
        found.append(f'a date ({pretty})')

    # A date and nothing else: they were trying to open a day, not post an
    # entry, so answer the question they were actually asking.
    if date_iso and not has_dir and amount is None and not has_name:
        day = datetime.strptime(date_iso, '%Y-%m-%d')
        spelled = f"{day:%d-%B-%Y}".lower().lstrip('0')
        return (f"I can read a date in {quoted} ({day:%m/%d/%Y}), but nothing "
                f"else — so there is neither an entry to post nor a day to "
                f"open.\n\n"
                f"To list that day's vouchers, type the date on its own:\n"
                f"  {spelled}\n"
                f"  {day:%m/%d/%Y}\n\n"
                f"To post an entry, the line also needs a direction, an amount "
                f"and a name:\n"
                f"  Paid $450 to Handy Fix LLC for Repair and Maintenance "
                f"from Bank of America 9523")

    lines = [f"I couldn't post {quoted} — nothing was written."]
    if found:
        lines.append("\nI did find: " + ", ".join(found) + ".")
    if missing:
        lines.append("\nStill missing:")
        lines += [f"  • {m}" for m in missing]
    else:
        lines.append("\nAll three are there, but I couldn't tell which part was "
                     "which. Keeping the words to / from before the name "
                     "usually fixes it.")
    lines.append("\nA complete line looks like this:")
    lines.append("  Paid $450 to Handy Fix LLC for Repair and Maintenance "
                 "from Bank of America 9523")
    lines.append("  Received $659.25 from John Smith today via "
                 "Bank of America 9523")
    lines.append("\nOnly the amount, the direction and the name are required "
                 "— the date defaults to today, and I'll ask about the "
                 "accounts in the review panel.")
    return "\n".join(lines)


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
    # "change name to X" is how people say it. Safe as a keyword because a
    # value is always consumed before the next keyword is looked for, so the
    # "name" inside "note the name is wrong" is never seen as a field.
    'name': 'party', 'payer': 'party', 'recipient': 'party',
    'bank': 'bank', 'cash': 'bank', 'contra': 'bank', 'source': 'bank',
    'category': 'category', 'classification': 'category', 'account': 'category',
    'expense': 'category', 'income': 'category', 'revenue': 'category', 'head': 'category',
    'cheque': 'cheque_no', 'check': 'cheque_no', 'chq': 'cheque_no',
    'note': 'description', 'notes': 'description', 'description': 'description',
    'desc': 'description', 'memo': 'description', 'remark': 'description',
    'remarks': 'description', 'narration': 'description',
}
# People name the field the way they'd say it out loud: "change bank NAME to
# UBL 1234", "set cheque NUMBER to 4521". The noun after the keyword is part of
# the label, not the value - but only when a connector follows it, or "note
# number of items" would lose its first word. Longest alternative first, so the
# filler form is tried before the plain one.
_UPD_FIELD_RE = re.compile(
    r'\b(' + '|'.join(sorted(_UPD_FIELD_MAP, key=len, reverse=True)) + r')\b'
    r'(?:'
    r'\s+(?:name|no\.?|number|account|acct|field|value|head)\s*(?:to|as|into|=|:)\s*'
    r'|\s*(?:to|as|into|=|:)?\s*'
    r')', re.IGNORECASE)

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


def _scan_update_fields(rest: str) -> Dict[str, str]:
    """
    'amount 500, category Printing, date 06/10/2026' -> {...}

    Scans left to right and consumes each value before looking for the next
    keyword, so a keyword sitting inside a value is never mistaken for a new
    field. Shared by the single-voucher edit and the bulk one - the grammar
    after the target is identical, and having two copies of it was how they
    drifted apart before.
    """
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
    return fields


def _parse_update_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'update 260902000001 amount 500, category Printing, date 06/10/2026'
    Returns {"at_id": ..., "fields": {...}} or None.
    """
    m = _UPDATE_CMD_RE.match(msg or '')
    if not m:
        return None
    at_id = m.group('id')
    rest = (m.group('rest') or '').strip(' ,;:-')
    if not rest:
        return {"at_id": at_id, "fields": {}}
    fields = _scan_update_fields(rest)
    if not fields:
        return {"at_id": at_id, "fields": {}, "unparsed_tail": rest}
    return {"at_id": at_id, "fields": fields}


# --------------------------------------------------------------------------
# Editing a whole day at once
#
# "2026-09-04 change name to 3S for all" named a DATE where the edit grammar
# wanted an id, so it matched nothing and fell through to the voucher parser,
# which reported a missing amount. The tick-list could already do this; there
# was just no way to say it in one line.
#
# Both shapes need an explicit bulk marker - a leading date, or the word
# "vouchers"/"all" - because "Paid 450 to X on 2026-09-04" is a posting, and
# must never be read as an instruction to rewrite that day.
# --------------------------------------------------------------------------
_BULK_TAIL = (r'(?:\s+(?:for|to|across|on)\s+(?:all|every|each|the\s+rest)'
              r'(?:\s+of\s+(?:them|these|those))?'
              r'(?:\s+(?:vouchers?|entries|entry|transactions?))?)\s*$')

_BULK_LEAD_DATE_RE = re.compile(
    r'^\s*(?P<date>' + _DATE_TOKEN + r')\s*[,:;\-]?\s*'
    r'(?:update|edit|change|modify|amend|correct|fix|set)\s+'
    r'(?P<rest>.+?)$', re.IGNORECASE | re.DOTALL)

_BULK_TAIL_DATE_RE = re.compile(
    r'^\s*(?:update|edit|change|modify|amend|correct|fix|set|bulk\s+edit)\s+'
    r'(?P<rest>.+?)\s+'
    r'(?:for|on|in|across)\s+(?:all\s+|every\s+|the\s+)*'
    r'(?:vouchers?|entries|entry|transactions?)\s*'
    r'(?:posted|dated|created|made)?\s*(?:on|for|of|from|dated)?\s*'
    r'(?P<date>' + _DATE_TOKEN + r')\s*$', re.IGNORECASE)

_BULK_TAIL_RE = re.compile(_BULK_TAIL, re.IGNORECASE)


def _parse_bulk_update_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'change name to 3S for vouchers posted on 2026-09-04'
    '2026-09-04 change name to 3S for all'
    -> {"date": "2026-09-04", "fields": {...}} or None.
    """
    msg = (msg or '').strip()
    for rx in (_BULK_TAIL_DATE_RE, _BULK_LEAD_DATE_RE):
        m = rx.match(msg)
        if not m:
            continue
        iso = parse_date_text(m.group('date'))
        if not iso:
            continue
        rest = (m.group('rest') or '').strip(' ,;:-')
        # A trailing "for all" belongs to the sentence, not to the last value:
        # without this, "name to 3S for all" sets the party to "3S for all".
        rest = _BULK_TAIL_RE.sub('', rest).strip(' ,;:-')
        if not rest:
            return None
        fields = _scan_update_fields(rest)
        if not fields:
            return {"date": iso, "fields": {}, "unparsed_tail": rest}
        return {"date": iso, "fields": fields}
    return None


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
    # "name" is what people call it. It was missing entirely, so renaming a
    # profile - the commonest edit there is - had no word for it.
    'company': 'company_name', 'name': 'company_name',
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

# Where the fields start inside a line with no commas at all:
#     update customer Example456 name to Example567 phone number to 000111
# Unanchored, unlike _PROFILE_FIELD_RE, and it REQUIRES a connector after the
# keyword. That requirement is what keeps a company called "State Farm" or
# "Companies House" intact - a field word only counts as a field word when it
# is followed by "to"/"is"/":", which a name never is. The optional noun
# between ("phone NUMBER to") belongs to the label, not the value.
_PROFILE_FIELD_FIND_RE = re.compile(
    r'\b(' + '|'.join(sorted(_PROFILE_FIELD_MAP, key=len, reverse=True)) + r')\b'
    r'(?:\s+(?:number|no\.?|id|line|address))?'
    r'\s*(?:to|is|=|:)\s+', re.IGNORECASE)


def _scan_profile_fields(tail: str) -> Tuple[Dict[str, str], List[str]]:
    """
    'name to Example567 phone number to 000111' -> {company_name, phone}

    Left to right, each value running to the next field keyword - the same
    shape as the voucher field scanner, for the same reason: a value may
    legitimately contain a word that is also a field name.
    """
    fields: Dict[str, str] = {}
    unparsed: List[str] = []
    marks = list(_PROFILE_FIELD_FIND_RE.finditer(tail or ''))
    if not marks:
        return fields, unparsed
    if marks[0].start() > 0:
        lead = tail[:marks[0].start()].strip(' ,;:-')
        if lead:
            unparsed.append(lead)
    for i, m in enumerate(marks):
        key = _PROFILE_FIELD_MAP[re.sub(r'\s+', ' ', m.group(1).lower())]
        end = marks[i + 1].start() if i + 1 < len(marks) else len(tail)
        val = tail[m.end():end].strip().strip(' ,;')
        if val and key not in fields:
            fields[key] = val
    return fields, unparsed


# The same instruction, said the way people say it:
#     "Add ABC Trading LLC as a new customer, phone 555-…"
#     "Create a customer profile for ABC Services"
# Both are rewritten into the canonical "add <kind> <name>[, fields]" and then
# parsed by the one parser below, so there is still only one grammar to know.
_PROFILE_AS_RE = re.compile(
    r'^\s*(?:new|add|create|register|make|setup|set\s+up)\s+'
    r'(?P<name>.+?)\s+as\s+(?:an?\s+|our\s+)?(?:new\s+)?'
    r'(?P<kind>' + '|'.join(_PROFILE_KIND_MAP) + r')\b'
    r'(?P<tail>.*)$', re.IGNORECASE | re.DOTALL)
_PROFILE_FOR_RE = re.compile(
    r'^\s*(?:new|add|create|register|make|setup|set\s+up)\s+(?:an?\s+)?'
    r'(?P<kind>' + '|'.join(_PROFILE_KIND_MAP) + r')\s+'
    r'(?:profile|record|entry)\s+for\s+(?P<name>.+)$',
    re.IGNORECASE | re.DOTALL)


def _canonical_profile_command(msg: str) -> str:
    """Turn the natural phrasings into the canonical one. Unchanged otherwise."""
    m = _PROFILE_AS_RE.match(msg or '')
    if m:
        tail = (m.group('tail') or '').strip()
        if tail and not tail.startswith(','):
            tail = ', ' + tail.lstrip(' ,')
        return f"add {m.group('kind').lower()} {m.group('name').strip()}{tail}"
    m = _PROFILE_FOR_RE.match(msg or '')
    if m:
        return f"add {m.group('kind').lower()} {m.group('name').strip()}"
    return msg


def _parse_profile_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143'
    'Add ABC Trading LLC as a new customer, phone 555-123-4567'
    'Create a customer profile for ABC Services'
    Returns {"p_type", "name", "fields", "unparsed"} or None.

    The name is whatever precedes the first comma, never split on a field
    keyword - company names contain words like "state", "wages" and "account".
    """
    m = _PROFILE_CMD_RE.match(_canonical_profile_command(msg or ''))
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


# "update customer ABC Trading, phone 555-987-6543"
# "change ABC Trading's phone to 555-987-6543"
# "edit vendor 2010100001 email ops@acme.com"
#
# The target comes first and the fields follow, exactly as they do when
# creating one - so the two commands are the same sentence with a different
# verb, and nobody has to learn a second grammar.
_EDIT_PROFILE_RE = re.compile(
    r'^\s*(?:update|edit|change|modify|amend|correct|fix|set)\s+'
    r'(?:the\s+)?'
    r'(?:(?P<kind>' + '|'.join(_PROFILE_KIND_MAP) + r')\s+)?'
    r'(?P<rest>.+)$', re.IGNORECASE | re.DOTALL)

# "ABC Trading's phone" / "ABC Trading phone" - the possessive is how people
# actually write it, and it marks where the name ends.
_POSSESSIVE_RE = re.compile(r"^(?P<name>.+?)[’']s\s+(?P<rest>.+)$",
                            re.IGNORECASE | re.DOTALL)


def _parse_edit_profile_command(msg: str) -> Optional[Dict[str, Any]]:
    m = _EDIT_PROFILE_RE.match(msg or '')
    if not m:
        return None
    kind = (m.group('kind') or '').lower()
    p_type = _PROFILE_KIND_MAP.get(kind) if kind else None
    rest = (m.group('rest') or '').strip()
    if not rest:
        return None

    # A leading voucher id means this is a voucher edit, not a profile one.
    if re.match(r'^\s*(?:crv|cpv)?[\s\-]?\d{8,20}\b', rest):
        return None

    name, tail = None, ''
    pm = _POSSESSIVE_RE.match(rest)
    if pm:
        name, tail = pm.group('name').strip(), pm.group('rest').strip()
    else:
        # "ABC Trading, phone 555-…" - the name is whatever precedes the first
        # comma, same rule the create command uses.
        head, _, after = rest.partition(',')
        # ...but people don't always use the comma. Find where the fields
        # actually begin instead. (The previous attempt at this searched with
        # an anchored pattern, so it could only ever match at position 0 and
        # the branch never ran - an edit without a comma or an apostrophe
        # fell through to the voucher parser, which read the phone number as
        # a dollar amount.)
        fm = _PROFILE_FIELD_FIND_RE.search(head)
        if fm and fm.start() > 0:
            name, tail = head[:fm.start()].strip(), (
                head[fm.start():] + (',' + after if after else '')).strip()
        elif after:
            name, tail = head.strip(), after.strip()
        else:
            return None

    name = name.strip().strip(' ,;:-')
    if not name or not tail:
        return None

    fields: Dict[str, str] = {}
    unparsed: List[str] = []
    for seg in [x.strip() for x in re.split(r'[,;\n]', tail) if x.strip()]:
        # A comma is still a separator; this only splits WITHIN a segment, so
        # "name to X phone to Y" yields two fields while
        # "address to 12 Main St, city Austin" keeps its comma meaning.
        many, lead = _scan_profile_fields(seg)
        if len(many) > 1:
            for k, v in many.items():
                fields.setdefault(k, v)
            unparsed += lead
            continue
        # "phone to 555-…" reads naturally and means the same as "phone 555-…"
        seg = re.sub(r'\bto\s+', '', seg, count=1) if re.match(
            r'^\s*[a-z ]+\s+to\s+', seg, re.IGNORECASE) else seg
        fm = _PROFILE_FIELD_RE.match(seg)
        if not fm:
            unparsed.append(seg)
            continue
        key = _PROFILE_FIELD_MAP[re.sub(r'\s+', ' ', fm.group(1).lower())]
        val = seg[fm.end():].strip().strip(' ,;')
        # "phone NUMBER to 555-…", "tax ID is 12-345" - the noun that trails the
        # field name is part of the label, not the value.
        val = re.sub(r'^(?:number|no\.?|id|address|line|to|is|=|:)\s+', '', val,
                     flags=re.IGNORECASE).strip()
        if val and key not in fields:
            fields[key] = val
    if not fields:
        return None
    return {"p_type": p_type, "name": name, "fields": fields, "unparsed": unparsed}


# ---- chart of accounts ---------------------------------------------------
_CHART_CMD_RE = re.compile(
    r'^\s*(?:show|view|list|display|open)?\s*'
    r'(?:my\s+|the\s+)?'
    r'(?:(?P<nature>asset|assets|liabilit\w*|equity|revenue|income|expense\w*|cogs)\s+)?'
    r'(?:chart(?:\s+of\s+accounts?)?|accounts?\s+list|account\s+tree|accounts?|coa)'
    r'\s*$', re.IGNORECASE)

_NATURE_WORD = {
    'asset': NATURE_ASSET, 'assets': NATURE_ASSET,
    'liability': NATURE_LIABILITY, 'liabilities': NATURE_LIABILITY,
    'equity': NATURE_EQUITY,
    'revenue': NATURE_REVENUE, 'income': NATURE_REVENUE,
    'expense': NATURE_EXPENSE, 'expenses': NATURE_EXPENSE,
    'cogs': NATURE_COGS,
}


def _parse_chart_command(msg: str) -> Optional[Dict[str, Any]]:
    m = _CHART_CMD_RE.match(msg or '')
    if not m:
        return None
    word = (m.group('nature') or '').lower()
    nature = _NATURE_WORD.get(word)
    if word and not nature:                       # "liabilit-ies/y" and friends
        nature = next((v for k, v in _NATURE_WORD.items() if word.startswith(k[:6])), None)
    return {"nature": nature}


_ADD_ACCOUNT_RE = re.compile(
    r'^\s*(?:add|create|new|make)\s+(?:a\s+|an\s+)?'
    r'(?:(?P<kind>asset|liabilit\w*|equity|revenue|income|expense\w*|cogs|bank)\s+)?'
    r'(?:gl\s+|ledger\s+|chart\s+)?account\s+'
    r'(?P<rest>.+)$', re.IGNORECASE | re.DOTALL)
_UNDER_RE = re.compile(r'\s+(?:under|below|beneath|in|inside)\s+(?P<parent>.+)$',
                       re.IGNORECASE | re.DOTALL)


def _parse_add_account_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'add expense account Fuel'                 -> main under EXPENSE
    'add bank account Meezan 1234'             -> sub under the "Banks" heading
    'add account FICA under Payroll Taxes'     -> sub under a named main
    'add account Fuel under EXPENSE'           -> main under a named nature
    """
    m = _ADD_ACCOUNT_RE.match(msg or '')
    if not m:
        return None
    kind = (m.group('kind') or '').lower()
    rest = (m.group('rest') or '').strip()

    parent_text = None
    um = _UNDER_RE.search(rest)
    if um:
        parent_text = um.group('parent').strip().strip(' ,.;')
        rest = rest[:um.start()].strip()
    name = rest.strip().strip(' ,.;"\'')
    if not name:
        return None

    nature = None
    if kind and kind != 'bank':
        nature = _NATURE_WORD.get(kind) or next(
            (v for k, v in _NATURE_WORD.items() if kind.startswith(k[:6])), None)
    return {"name": name, "nature": nature,
            "parent_text": parent_text,
            "bank": kind == 'bank'}


def _chart_parent_options(tree: List[Dict]) -> List[Dict]:
    """
    Everywhere a new account may hang: a nature (which makes it a main) or an
    existing main (which makes it a sub). Sent with an account draft so the
    operator corrects the parent by picking a real one rather than retyping a
    name for the matcher to guess at again.
    """
    opts = []
    for n in tree:
        opts.append({'code': n['nature'], 'label': f"{n['label']} (top level)",
                     'level': 'main', 'group': 'Nature'})
    for n in tree:
        for m in n['mains']:
            opts.append({'code': m['code'], 'label': m['name'], 'level': 'sub',
                         'group': n['label'], 'postable': m['postable'],
                         'sub_count': m.get('sub_count', 0)})
    return opts


# A day's worth of vouchers, for the bulk-edit flow: name a date and pick
# from what comes back rather than remembering ids.
_DATE_LIST_RE = re.compile(
    r'^\s*(?:(?:show|list|view|open|get|display'
    r'|edit|fix|change|update|amend|correct|modify|bulk)\s+)?'
    r'(?:my\s+|the\s+|all\s+)?'
    r'(?:vouchers?|entries|entry|transactions?)?\s*'
    r'(?:on|for|from|dated|date|of)?\s*'
    r'(?P<date>' + _DATE_TOKEN + r')\s*$', re.IGNORECASE)


def _parse_voucher_date_command(msg: str) -> Optional[Dict[str, Any]]:
    """
    'vouchers on 06/04/2026' / 'edit vouchers 2026-06-04' / '06/04/2026'
    A bare date is unambiguous here: a voucher id is all digits, a date is not.
    """
    m = _DATE_LIST_RE.match(msg or '')
    if not m:
        return None
    iso = parse_date_text(m.group('date'))
    return {"date": iso} if iso else None


def find_main_by_name(tree: List[Dict], text: str) -> Tuple[Optional[Dict], List[str]]:
    """Resolve a heading/main by name. Returns (main, near_misses)."""
    target = normalize_name(text)
    mains = [(n, m) for n in tree for m in n['mains']]
    for n, m in mains:
        if normalize_name(m['name']) == target:
            return {**m, 'nature': n['nature'], 'nature_label': n['label']}, []
    hits = [(n, m) for n, m in mains if target and target in normalize_name(m['name'])]
    if len(hits) == 1:
        n, m = hits[0]
        return {**m, 'nature': n['nature'], 'nature_label': n['label']}, []
    return None, [m['name'] for _, m in (hits or mains)][:8]


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


# Natures, in the order the host UI's Chart of Accounts page lists them.
_STATEMENT_OF = {
    NATURE_ASSET: 'Balance Sheet', NATURE_LIABILITY: 'Balance Sheet',
    NATURE_EQUITY: 'Balance Sheet',
    NATURE_REVENUE: 'Profit & Loss', NATURE_EXPENSE: 'Profit & Loss',
    NATURE_COGS: 'Profit & Loss', NATURE_UNEARNED: 'Profit & Loss',
}
_NATURE_ORDER = [NATURE_ASSET, NATURE_LIABILITY, NATURE_EQUITY,
                 NATURE_REVENUE, NATURE_EXPENSE, NATURE_COGS, NATURE_UNEARNED]


def build_chart_tree(conn) -> List[Dict]:
    """
    Reconstructs the nature -> main -> sub tree the host UI's Chart of Accounts
    page shows, using only the postable set.

    That works because the hierarchy is encoded in the code itself: a 9-digit
    sub's first five digits ARE its parent main's code, and subsi_acc_desc is
    its parent's name. So group accounts like "Banks" and "Payroll Taxes" -
    which never appear in v_trans_accounts_m2, precisely because they have
    children - are still recovered exactly, with their real codes.

    The count beside each main is its number of subs, matching the digit the
    host page prints after the name ("Payroll Taxes 3").
    """
    natures: Dict[str, Dict] = {}
    for r in get_chart(conn):
        code, nat = r['code'], r['nature']
        n = natures.setdefault(nat, {
            "nature": nat, "label": NATURE_LABEL.get(nat, nat),
            "statement": _STATEMENT_OF.get(nat, 'Other'), "mains": {}})
        if len(code) == 5:                       # a childless main: postable itself
            m = n["mains"].setdefault(code, {"code": code, "name": r['desc'],
                                             "postable": True, "subs": []})
            m["name"], m["postable"] = r['desc'], True
        else:                                    # sub or individual
            main_code = code[:5]
            m = n["mains"].setdefault(main_code, {"code": main_code,
                                                  "name": r['parent_desc'],
                                                  "postable": False, "subs": []})
            if not m["name"]:
                m["name"] = r['parent_desc']
            m["subs"].append({"code": code, "name": r['desc'],
                              "level": r['level'], "postable": True})

    out = []
    for nat in _NATURE_ORDER:
        if nat not in natures:
            continue
        n = natures[nat]
        mains = sorted(n["mains"].values(), key=lambda m: m["code"])
        for m in mains:
            m["subs"].sort(key=lambda s: s["code"])
            m["sub_count"] = len(m["subs"])
        out.append({**n, "mains": mains,
                    "postable_count": sum(1 for m in mains if m["postable"])
                                      + sum(len(m["subs"]) for m in mains)})
    return out


def render_chart_tree(tree: List[Dict], nature: Optional[str] = None) -> str:
    lines: List[str] = []
    statement = None
    for n in tree:
        if nature and n['nature'] != nature:
            continue
        if n['statement'] != statement:
            statement = n['statement']
            lines.append(statement.upper())
        lines.append(f"  {n['label'].upper()}   [nature {n['nature']}]")
        for m in n['mains']:
            tag = ('' if m['postable'] else
                   f"   {m['sub_count']} sub-account{'' if m['sub_count'] == 1 else 's'}")
            lines.append(f"    {m['name']:<38} {m['code']}{tag}")
            for s in m['subs']:
                lines.append(f"      {s['name']:<36} {s['code']}")
    if not lines:
        return "Nothing in the chart for that filter."
    lines.append("")
    lines.append("Only accounts with no children can be posted to - a main with "
                 "sub-accounts under it is a heading, not a ledger account.")
    return "\n".join(lines)


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


class AccountProblem(str):
    """
    Why a leg didn't resolve, said twice: at length for the conversation, and
    in a few words for the field label in the panel.

    A str subclass so every existing site that formats it into a message keeps
    working unchanged; the panel reads `.short` instead. The long form explains
    and teaches; the short form only has to name the problem, because the field
    it sits under already says which account it is about.
    """
    short: str

    def __new__(cls, long: str, short: str):
        obj = super().__new__(cls, long)
        obj.short = short
        return obj


def _problem_short(note) -> str:
    """The panel form of a note, whatever kind of string it arrived as."""
    return getattr(note, 'short', None) or str(note or '')


def _resolve(conn, search_text: str, candidates: List[Dict],
             what: str) -> Tuple[Optional[Resolution], Optional[str]]:
    if not search_text or not search_text.strip():
        return None, AccountProblem(f"No {what} account name provided.",
                                    "Not named in your message")

    row, score, mtype, ambiguous = _match_account(search_text, candidates)

    if ambiguous:
        # Several accounts fit. Before refusing, let the model look at the
        # shortlist - it reads "the Meezan one" and "bofa 9523" better than a
        # similarity score does. It can only answer with a name we offered.
        picked = _llm_pick_account(search_text, candidates, what)
        if picked:
            return Resolution(picked, 'llm', 0.5), None
        near = [r['qualified'] for _, r in
                sorted(((max(name_similarity(search_text, n) for n in _candidate_names(c)), c)
                        for c in candidates), key=lambda x: x[0], reverse=True)[:4]]
        return None, AccountProblem(
            f"\"{search_text}\" matches more than one account "
            f"({', '.join(near)}), and picking the wrong one puts the "
            f"money in the wrong place — so pick the right one in the panel, "
            f"or say the full name including any number.",
            f"{len(near)} accounts match “{search_text}”")

    if not row:
        picked = _llm_pick_account(search_text, candidates, what)
        if picked and transactionable_account(conn, picked['code']):
            return Resolution(picked, 'llm', 0.5), None
        example = next((c['desc'] for c in candidates if c.get('desc')), 'Fuel')
        prefix = (candidates[0].get('parent_desc') or 'EXPENSE') if candidates else 'EXPENSE'
        return None, AccountProblem(
            f"Nothing in this company's chart matches \"{search_text}\" as a "
            f"{what} account. Pick one in the panel, or use the name as your reports "
            f"show it — either \"{example}\" or the full "
            f"\"{prefix}/{example}\". Type \"show chart\" to see what exists.",
            f"No account matches “{search_text}”")

    if not transactionable_account(conn, row['code']):
        return None, AccountProblem(
            f"\"{row['qualified']}\" is a heading with accounts underneath it, "
            f"not a ledger account, so nothing can be posted to it directly. "
            f"Choose one of the accounts under it.",
            f"“{row['desc']}” is a heading, not a postable account")

    return Resolution(row, mtype, score), None


def _accounts_ending(conn, digits: str) -> List[Dict]:
    """
    Postable accounts whose NAME carries this number - "Chase Bank 9993".

    A statement identifies itself by account number and by nothing else, and
    the number is how people name these accounts in the chart too. This is
    kept separate from the general matcher on purpose: it is a rule about
    statements, where the number is authoritative, not about typing, where a
    bare number is far more likely to be a code or an amount.
    """
    digits = re.sub(r'\D', '', digits or '')
    if len(digits) < 4:
        return []
    out = []
    for row in get_chart(conn):
        tokens = re.findall(r'\d+', f"{row.get('desc') or ''} {row.get('parent_desc') or ''}")
        if any(t == digits or t.endswith(digits) for t in tokens):
            out.append(row)
    return out


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
        return None, (f"The configured default bank account ({DEFAULT_BANK_ACC}) is "
                      f"not a postable account in this company's chart, so I can't "
                      f"use it. Name the account in your message instead.")
    names = _bank_samples(conn)
    listed = (" — yours include " + ", ".join(f'"{n}"' for n in names)
              if names else "")
    return None, ("I don't know which bank or cash account the money moved "
                  "through, and there's no default set for this company, so I "
                  f"won't guess at it{listed}. Pick one on the right, or say it "
                  "in the line with into / from / on bank.")


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
    names = _sample_accounts(conn, natures)
    listed = (" — yours include " + ", ".join(f'"{n}"' for n in names)
              if names else "")
    return None, (f"Your line doesn't say what the money was for, so I can't tell "
                  f"which {kind} account it belongs to{listed}. "
                  f"Pick one on the right, or add \"for <account>\" to the line.")


# --------------------------------------------------------------------------
# Talking like an assistant instead of a validator.
#
# "I couldn't work out which revenue account this belongs to. Name the
# category in your message, or set DEFAULT_REVENUE_ACC." is accurate and
# useless: it doesn't say what I DID understand, it names an environment
# variable, and it leaves the person to compose a whole new sentence.
#
# Everything below builds the other kind of reply: repeat back what I got,
# name the one thing I still need, and hand over their own line with the gap
# filled in - using account names that actually exist in their chart, so the
# suggestion can be sent as-is.
# --------------------------------------------------------------------------
def _money(n) -> str:
    try:
        return f"${float(n):,.2f}"
    except (TypeError, ValueError):
        return str(n)


def _sample_accounts(conn, natures: set, limit: int = 3) -> List[str]:
    """A few real account names of the right nature, shortest first - a short
    name is usually the everyday one ("Printing" over "Printing & Binding -
    Subcontracted")."""
    rows = chart_by_nature(conn, natures)
    names = sorted({r['desc'] for r in rows if r.get('desc')}, key=lambda x: (len(x), x))
    return names[:limit]


def _bank_samples(conn, limit: int = 3) -> List[str]:
    rows = [r for r in get_chart(conn) if r['nature'] == NATURE_ASSET]
    names = sorted({r['desc'] for r in rows if r.get('desc')}, key=lambda x: (len(x), x))
    return names[:limit] or sorted(
        {r['desc'] for r in get_chart(conn)}, key=lambda x: (len(x), x))[:limit]


# Where the bank half of a sentence starts, so a "for ..." can be slipped in
# before it rather than tacked on after it. On a receipt "from" introduces the
# PAYER, not the bank, so it is excluded there - inserting ahead of it would
# put the category between "received" and the person who paid.
def _bank_lead_re(entry_type: Optional[str]) -> re.Pattern:
    leads = ['into', 'via', 'through', 'thru', 'using', r'out\s+of']
    if entry_type != 'CRV':
        leads.append('from')
    return re.compile(
        r'\s+\b(?:' + '|'.join(leads) +
        r'|(?:in|on|at|by|with)(?=\s+' + _BANK_WORD + r'))\b', re.IGNORECASE)


def _line_with_category(msg: str, account: str,
                        entry_type: Optional[str] = None) -> str:
    """Their sentence with 'for <account>' inserted where it belongs."""
    hits = list(_bank_lead_re(entry_type).finditer(msg or ''))
    if hits:
        at = hits[-1].start()
        return f"{msg[:at]} for {account}{msg[at:]}".strip()
    return f"{(msg or '').rstrip(' .')} for {account}"


def _line_with_bank(msg: str, account: str) -> str:
    return f"{(msg or '').rstrip(' .')} into {account}"


def _clarify(*, opening: str, need: str, examples: List[str],
             closing: Optional[str] = None) -> str:
    """One shape for every 'I need one more thing' reply."""
    parts = [opening, "", need]
    if examples:
        parts.append("")
        parts += [f"  {e}" for e in examples]
    if closing:
        parts += ["", closing]
    return "\n".join(parts)


def _understood_so_far(*, amount=None, party=None, entry_type=None,
                       bank=None, date=None) -> str:
    """'I have $659.25 from Medicare going into Banks/Meezan 1234.'"""
    bits = []
    if amount is not None:
        bits.append(_money(amount))
    if party:
        bits.append(f"{'from' if entry_type == 'CRV' else 'to'} {party}")
    if bank:
        bits.append(f"{'into' if entry_type == 'CRV' else 'out of'} {bank}")
    if date:
        try:
            bits.append(f"on {datetime.strptime(date, '%Y-%m-%d'):%m/%d/%Y}")
        except (ValueError, TypeError):
            pass
    if not bits:
        return "I read your line"
    return "Got it — " + " ".join(bits) + "."


def _clarify_reply(*, msg: str, note: str, suggestions: List[str],
                   amount=None, party=None, entry_type=None,
                   bank=None, date=None) -> Dict[str, Any]:
    """A refusal that reads like an answer: what I have, what I need, what to send."""
    text = _clarify(
        opening=_understood_so_far(amount=amount, party=party,
                                   entry_type=entry_type, bank=bank, date=date),
        need=note,
        examples=suggestions,
        closing=("Send one of those as it is, or type \"show chart\" to see every "
                 "account you have." if suggestions else None))
    keep = [s for s in suggestions]
    if amount is not None:
        keep.append(_money(amount))
    if party:
        keep.append(party)
    text = _voice(text, keep)
    return {'status': 'error', 'message': text, 'analysis': text,
            'confidence': 'low', 'suggestions': suggestions}


# ==========================================================================
# The language model as a FALLBACK PARSER
#
# Everything above this line is deterministic, and stays that way. The model
# is consulted only where the regex has already given up, and it is never
# allowed to act:
#
#     it rewrites the message into a command in OUR OWN grammar,
#     the same regex parses that command,
#     the same preview asks the operator to confirm it.
#
# So the model can be as loose as it likes at the front - misspellings,
# missing prepositions, words in any order - without widening what can reach
# the database by a single byte. Three guards make that true:
#
#   * amounts and voucher ids in its answer must appear in the message
#     (_numbers_are_the_users), or the answer is thrown away;
#   * an account it picks must be one of the candidates we handed it;
#   * a destructive command (void) is never run from a model reading - it
#     comes back as a suggestion chip for the person to click.
# ==========================================================================
_LLM_FAILS = 0
_LLM_MAX_FAILS = 2          # a decommissioned model shouldn't be retried all day
_LLM_OFF_UNTIL = 0.0        # when the breaker opened, when to allow one probe
_LLM_COOLDOWN = float(os.getenv("LLM_COOLDOWN_SECONDS", "300"))
LLM_FALLBACK = os.getenv("LLM_FALLBACK", "1") == "1"


def _llm_available() -> bool:
    """
    A circuit breaker, not a kill switch. Two consecutive failures open it, so
    a dead model isn't dialled on every keystroke - but it closes again after
    a cooldown, because the two things that break here recover differently: a
    rate limit or a network blip clears on its own within minutes, while a
    retired model name never does. Without the cooldown, one transient 429
    disabled the fallback until someone restarted the server, and nobody knew
    to. With it, the worst case for a permanently dead model is one wasted
    request every few minutes instead of one per message.
    """
    global _LLM_FAILS
    if not LLM_FALLBACK or getattr(bot, 'groq_client', None) is None:
        return False
    if _LLM_FAILS >= _LLM_MAX_FAILS:
        if time.time() < _LLM_OFF_UNTIL:
            return False
        # Cooldown elapsed: allow exactly one probe. A single failure re-opens
        # the breaker, a success resets it (see _llm_note_ok / _llm_note_fail).
        _LLM_FAILS = _LLM_MAX_FAILS - 1
    return True


def _llm_note_ok() -> None:
    global _LLM_FAILS, _LLM_OFF_UNTIL
    _LLM_FAILS, _LLM_OFF_UNTIL = 0, 0.0


def _llm_note_fail(e: Exception, where: str) -> None:
    """One place to count a failed model call, whichever call site made it."""
    global _LLM_FAILS, _LLM_OFF_UNTIL
    _LLM_FAILS += 1
    if _LLM_FAILS >= _LLM_MAX_FAILS:
        _LLM_OFF_UNTIL = time.time() + _LLM_COOLDOWN
        pretty = (f"{_LLM_COOLDOWN / 60:.0f} min" if _LLM_COOLDOWN >= 60
                  else f"{_LLM_COOLDOWN:g}s")
        when = f"Pausing all model calls for {pretty}, then trying once."
    else:
        when = "Will try once more."
    print(f"NOTE: model call failed in {where} ({type(e).__name__}: {e}). {when}")
    print(_llm_config_hint(e))


def _llm_config_hint(e: Exception) -> str:
    """
    A model failure is nearly always configuration, not code, and the raw
    provider error says so only if you already know what to look for. This
    turns it into the sentence that tells you what to do.
    """
    t, s = f"{type(e).__name__}", str(e).lower()
    if '404' in s or 'does not exist' in s or 'model_not_found' in s:
        return (f"      -> The model name is wrong or retired, not the code. "
                f"GROQ_MODEL is '{getattr(bot, 'model', '?')}'.\n"
                f"         Run  python3 check_groq.py  to see which models "
                f"this key can reach.")
    if '401' in s or '403' in s or 'invalid api key' in s or 'authentication' in s:
        return ("      -> The API key was rejected. Check "
                "ACCOUNTING_GROQ_API_KEY / GROQ_API_KEY, then run "
                "python3 check_groq.py")
    if '429' in s or 'rate limit' in s or 'quota' in s:
        return ("      -> Rate-limited or out of quota. It will recover on "
                "its own; until then the parse is rule-based.")
    if 'timeout' in s or 'timed out' in s or 'connection' in t.lower():
        return ("      -> Couldn't reach the provider. Network, not "
                "configuration.")
    return "      -> Falling back to the rule-based parse; nothing is lost."


def _llm_ask(system: str, user: str, *, max_tokens: int = 300,
             temperature: float = 0.0) -> Optional[str]:
    """One call, all the plumbing in one place. None on any failure."""
    if not _llm_available():
        return None
    try:
        r = bot.groq_client.chat.completions.create(
            model=bot.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            # temperature=temperature, max_tokens=max_tokens, timeout=8,
            temperature=temperature, max_tokens=max_tokens, timeout=20,
        )
        out = (r.choices[0].message.content or '').strip()
        _llm_note_ok()
        return strip_emojis(out)
    except Exception as e:
        _llm_note_fail(e, 'the fallback parser')
        return None


_NUM_RE = re.compile(r'\d[\d,]*(?:\.\d+)?')


def _numbers_are_the_users(src: str, out: str) -> bool:
    """
    Every amount and every voucher id in the rewrite must come from the
    message. A model that turns 659.25 into 659.50, or invents an id, is
    discarded - that is the difference between "flexible" and "posts the
    wrong number". Dates are exempt: filling in the current year is the one
    piece of arithmetic it is allowed to do, and the preview shows the date.
    """
    def norm(t):
        return t.replace(',', '').rstrip('.').lstrip('0') or '0'

    src_nums = {norm(m.group()) for m in _NUM_RE.finditer(src)}
    year = str(datetime.now().year)
    for m in _NUM_RE.finditer(out):
        tok = m.group()
        n = norm(tok)
        if n in src_nums:
            continue
        digits = tok.replace(',', '').replace('.', '')
        if len(digits) <= 2:                       # day, month, small ordinals
            continue
        if digits in (year, year[2:], str(int(year) + 1)):
            continue
        return False
    return True


# The commands the fallback is allowed to produce. Built from one place so a
# new command can't be added to the app and quietly missed by the model.
_CANONICAL_GRAMMAR = """\
Paid <amount> to <name> for <expense account> from <bank account>
Received <amount> from <name> for <income account> into <bank account>
show <voucher id>
update <voucher id> <field> <value>, <field> <value>
void <voucher id>
<date>
add customer <name>, email <e>, phone <p>
add vendor <name>, email <e>, phone <p>
update customer <name>, phone <p>, email <e>
add expense account <name>
add bank account <name>
add account <name> under <parent>
rename account <name> to <new name>
deactivate account <name>
show chart
show my transactions
show my financial summary"""

_FALLBACK_SYSTEM = (
    "You turn a bookkeeper's message into ONE command for an accounting "
    "assistant. You never answer the message and never invent facts.\n\n"
    "Reply with the command on a single line, or exactly UNKNOWN.\n\n"
    "The only commands that exist:\n" + _CANONICAL_GRAMMAR + "\n\n"
    "Rules:\n"
    "- Copy amounts, voucher ids, dates, names and account names from the "
    "message. Never change a number. Never invent an account.\n"
    "- Fix spelling and word order; keep the identifying digits that belong "
    "to an account name (\"Bank of America 9523\").\n"
    "- 'Paid/spent/sent' is money out. 'Received/got/deposit' is money in.\n"
    "- Only a Paid/Received command needs an amount and a direction. If the "
    "message is clearly one of those but says neither, reply UNKNOWN. Every "
    "other command above - profiles, accounts, lookups - has no amount and no "
    "direction, and that is normal. Never answer UNKNOWN just because there "
    "is no money in the message.\n"
    "- If it isn't about accounting at all, reply UNKNOWN.\n\n"
    "Examples:\n"
    "message: paid 450 handy fix llc repair maintanence bofa 9523\n"
    "command: Paid 450 to handy fix llc for repair maintanence from bofa 9523\n"
    "message: recieved 1250 abc trading invoice 2045 chase 4582\n"
    "command: Received 1250 from abc trading for invoice 2045 into chase 4582\n"
    "message: scrap voucher 260902000001\n"
    "command: void 260902000001\n"
    "message: chnage 260902000001 amt to 500\n"
    "command: update 260902000001 amount 500\n"
    "message: new custmer abc trading llc ph 555-123-4567\n"
    "command: add customer abc trading llc, phone 555-123-4567\n"
    # No amount and no direction anywhere in the next four - they are here so
    # a literal-minded model doesn't read the UNKNOWN rule as covering them.
    "message: Add ABC Trading LLC as a new customer, phone 555-123-4567\n"
    "command: add customer ABC Trading LLC, phone 555-123-4567\n"
    "message: set up a vendor profile for Handy Fix LLC\n"
    "command: add vendor Handy Fix LLC\n"
    "message: chnage abc tradings fone to 555-987-6543\n"
    "command: update customer abc trading, phone 555-987-6543\n"
    "message: make a new expence acct calld Fuel\n"
    "command: add expense account Fuel\n"
    "message: whats the weather\n"
    "command: UNKNOWN"
)


def _draft_quality(payload: Dict[str, Any]) -> int:
    """How much of a draft actually resolved. Used to decide whether a rewrite
    was an improvement or just a different guess."""
    d = (payload or {}).get('draft') or {}
    if d.get('kind') == 'edit':
        # Every field on an edit draft is already populated from the stored
        # voucher, so counting filled codes says nothing at all - it is 3 out
        # of 3 whether or not we understood a word of the request. What varies
        # is how many CHANGES were understood.
        return len(d.get('applied') or [])
    return sum(1 for k in ('bank_acc_code', 'category_acc_code', 'party_code')
               if d.get(k))


def _llm_canonical_command(message: str, mode: Optional[str] = None) -> Optional[str]:
    """The message, rewritten as one command in our grammar. None if it can't be."""
    hint = MODE_HINT.get((mode or '').lower())
    user = f"message: {message}"
    if hint:
        user += f"\n(context, a hint only: {hint})"
    out = _llm_ask(_FALLBACK_SYSTEM, user, max_tokens=160)
    if not out:
        return None
    line = out.splitlines()[0].strip()
    line = re.sub(r'^command:\s*', '', line, flags=re.IGNORECASE).strip().strip('`"')
    if not line or line.upper().startswith('UNKNOWN'):
        return None
    if line.strip().lower() == message.strip().lower():
        return None                                  # nothing was actually fixed
    if not _numbers_are_the_users(message, line):
        print(f"NOTE: LLM rewrite invented a number, discarded: {line!r}")
        return None
    return line


_PICK_SYSTEM = (
    "You match what a bookkeeper wrote to ONE account from a list. Reply with "
    "the account's exact name from the list, or exactly NONE. Never invent a "
    "name. Prefer the account whose identifying digits match; if the wording "
    "genuinely fits two of them equally, reply NONE."
)


def _llm_pick_account(search_text: str, candidates: List[Dict], what: str,
                      entry_type: Optional[str] = None) -> Optional[Dict]:
    """
    The disambiguation half. The model chooses among accounts WE hand it, and
    the answer is only accepted if it is one of them - so the worst case is
    the same refusal the operator would have got anyway.
    """
    if not search_text or not candidates or not _llm_available():
        return None
    shortlist = candidates[:40] if len(candidates) <= 40 else sorted(
        candidates,
        key=lambda r: max(name_similarity(search_text, n) for n in _candidate_names(r)),
        reverse=True)[:25]
    listing = "\n".join(f"- {r['qualified']}" for r in shortlist)
    kind = ('bank or cash account' if what.startswith('bank')
            else f"{'income' if entry_type == 'CRV' else 'expense'} account")
    out = _llm_ask(_PICK_SYSTEM,
                   f"The bookkeeper wrote: \"{search_text}\"\n"
                   f"It should be one of these {kind}s:\n{listing}",
                   max_tokens=300)
                #    max_tokens=60)
    if not out:
        return None
    answer = out.splitlines()[0].strip().strip('-`" ')
    if not answer or answer.upper().startswith('NONE'):
        return None
    for r in shortlist:                              # must be one we offered
        if normalize_name(r['qualified']) == normalize_name(answer) or \
                normalize_name(r['desc']) == normalize_name(answer):
            print(f"NOTE: LLM matched {search_text!r} -> {r['qualified']}")
            return r
    print(f"NOTE: LLM answered with an account that wasn't offered: {answer!r}")
    return None


def _voice(text: str, must_keep: Optional[List[str]] = None) -> str:
    """
    Optional last pass: let the language model say the same thing more warmly.

    It is given the finished sentence and asked only to rephrase, and the
    result is thrown away unless every fact in the original survives it - so a
    model having a bad day can make the wording worse, never the content wrong.
    Off when no key is configured, which is most of the time in practice.
    """
    global _VOICE_OFF
    if not ASSISTANT_VOICE or _VOICE_OFF or not text:
        return text
    client = getattr(bot, 'groq_client', None)
    if client is None:
        return text
    keep = [k for k in (must_keep or []) if k]
    try:
        r = client.chat.completions.create(
            model=bot.model,
            messages=[{"role": "system", "content":
                       "You rewrite one short message from an accounting assistant "
                       "so it reads like a helpful colleague. Keep every number, "
                       "name, account and example line EXACTLY as given, keep the "
                       "example lines on their own indented lines, stay under 90 "
                       "words, no emoji, no greeting, no sign-off. Reply with the "
                       "rewritten message only."},
                      {"role": "user", "content": text}],
            temperature=0.2, max_tokens=260, timeout=20,
            # temperature=0.2, max_tokens=260, timeout=6,
        )
        out = strip_emojis((r.choices[0].message.content or '').strip())
    except Exception as e:
        # One failed call is enough: a decommissioned model will not come back
        # mid-session, and every clarification would otherwise pay for a round
        # trip to find that out again.
        _VOICE_OFF = True
        print(f"NOTE: assistant voice unavailable ({type(e).__name__}); using the "
              f"written wording for the rest of this run.")
        return text
    if not out or len(out) > max(400, len(text) * 2):
        return text
    if any(k not in out for k in keep):
        print("NOTE: voice pass dropped a fact; keeping the written wording.")
        return text
    return out


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


def resolve_party(conn, name: str, entry_type: str,
                  create_missing: bool = True) -> Tuple[Optional[str], str, str, float, str]:
    """
    Returns (p_code, display_name, match_type, score, p_type).

    With create_missing=False nothing is written: an unmatched name comes back
    as p_code None with match_type 'new', meaning "this party would have to be
    created". That is what the preview path uses - showing an operator a draft
    must not leave an acc_party row behind if they discard it.
    """
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

    if not create_missing:
        return None, title_case_name(name), 'new', 0.0, p_type

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
# Shared by the three ways a voucher can come into being: a direct post from
# chat, a preview that stops short of writing, and a reviewed draft posted by
# code. Keeping them in one place is the point - a review flag or a message
# line that exists on one path and not another is how an operator learns to
# distrust the confirmation screen.
# --------------------------------------------------------------------------
def _collect_review_items(entry_type: str, party_match_type: str, party_score: float,
                          bank_match_type: str, bank_score: float,
                          category_matched: bool, direction_inferred: bool,
                          base: Optional[List[str]] = None) -> List[str]:
    """Everything on this entry that was guessed rather than stated."""
    items = list(base or [])
    party_label = "customer" if entry_type == 'CRV' else "vendor"
    if party_match_type == 'fuzzy' and party_score < 0.85:
        items.append(party_label)
    if party_match_type == 'new':
        items.append(f'new {party_label} record')
    if bank_match_type not in ('exact', 'code', 'default') and bank_score < 0.90:
        items.append('bank/cash account')
    if not category_matched:
        items.append('category')
    if direction_inferred:
        items.append('DIRECTION (the line gave no in/out signal)')
    return items


def _require_postable(conn, code: Any, what: str, natures: Optional[set] = None,
                      entry_type: Optional[str] = None) -> Dict:
    """A code must name a real, postable account of an acceptable nature."""
    row = find_account(conn, code)
    if not row:
        raise ValueError(
            f"I can't find that {what.lower()} account in your chart of accounts. "
            f"Pick one from the dropdown in the review panel, or type "
            f"\"show chart\" to see what exists — you can add a new one with "
            f"\"add expense account <name>\".")
    if not transactionable_account(conn, code):
        raise ValueError(
            f"\"{row['qualified']}\" is a heading, not a ledger account, so "
            f"nothing can be posted to it directly. Choose one of the accounts "
            f"underneath it — \"show chart\" lists them.")
    if natures and row['nature'] not in natures:
        kind = NATURE_LABEL.get(row['nature'], row['nature'])
        want = ('an income account' if entry_type == 'CRV' else 'an expense account')
        raise ValueError(
            f"\"{row['qualified']}\" is {kind.lower()}, and a "
            f"{entry_type or 'voucher'} needs {want} on that side. "
            f"{'Money coming in is income' if entry_type == 'CRV' else 'Money going out is an expense'} "
            f"— pick a different account in the review panel.")
    return row


def _resolve_update_fields(conn, entry_type: str, fields: Dict[str, str]):
    """
    Turn the named fields of an edit into a VoucherUpdate of CODES.

    Shared by the chat command, the preview and the bulk instruction, so a
    phrase means the same thing however it arrives. Returns
    (upd, applied_field_names, notes, error_text).
    """
    upd = VoucherUpdate(entry_type=entry_type)
    applied: List[str] = []
    notes: List[str] = []

    if 'amount' in fields:
        amt = _parse_amount(fields['amount'])
        if amt is None:
            return upd, applied, notes, f"I couldn't read \"{fields['amount']}\" as an amount."
        upd.amount, _ = amt, applied.append('amount')
    if 'transaction_date' in fields:
        iso = (normalize_date_str(fields['transaction_date'])
               or parse_date_text(fields['transaction_date']))
        if not iso:
            return upd, applied, notes, (f"I couldn't read "
                                         f"\"{fields['transaction_date']}\" as a date.")
        upd.transaction_date, _ = iso, applied.append('transaction_date')
    if 'cheque_no' in fields:
        v = fields['cheque_no'].strip()
        upd.cheque_no = '' if v.lower() in ('none', 'blank', 'clear', 'remove', '-') else v
        applied.append('cheque_no')
    if 'description' in fields:
        upd.description, _ = fields['description'], applied.append('description')

    if 'party' in fields:
        prefer = P_TYPE_CUSTOMER if entry_type == 'CRV' else P_TYPE_VENDOR
        row, err = resolve_party_existing(conn, fields['party'], prefer)
        if not row:
            return upd, applied, notes, err
        upd.party_code = str(row['p_code'])
        applied.append('party')
        notes.append(f"party -> {row['matched_name']}")

    if 'bank' in fields:
        res, err = resolve_bank_account(conn, fields['bank'], allow_default=False)
        if not res:
            return upd, applied, notes, err
        upd.bank_acc_code = res.code
        applied.append('bank')
        notes.append(f"bank -> {res.qualified}")

    if 'category' in fields:
        natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
        res, err = resolve_category_account(conn, fields['category'], natures)
        if not res:
            return upd, applied, notes, (err or f"No category account matching "
                                                f"\"{fields['category']}\".")
        upd.category_acc_code = res.code
        applied.append('category')
        notes.append(f"category -> {res.qualified}")

    return upd, applied, notes, None


def _voucher_success_payload(*, new_id, entry_type: str, amount: float,
                             party_code, party_display, party_type,
                             party_match_type: str, party_score: float,
                             bank_code, bank_qualified, bank_level,
                             cat_code, cat_qualified, cat_level,
                             trans_date, cheque_no, description,
                             review_items: List[str], category_matched: bool = True,
                             extraction_source: Optional[str] = None) -> Dict[str, Any]:
    voucher_number = f"{entry_type}-{new_id}"
    party_label = "Customer" if entry_type == 'CRV' else "Vendor"
    direction = (f"Dr {bank_qualified}  /  Cr {cat_qualified}" if entry_type == 'CRV'
                 else f"Dr {cat_qualified}  /  Cr {bank_qualified}")

    review_line = ""
    if review_items:
        review_line = (f"\n\nPlease double check the {', '.join(review_items)} "
                       f"above - matched automatically.")
    check_line = f"\nCheck #{cheque_no}" if cheque_no else ""
    text = strip_emojis(
        f"Posted {voucher_number} for ${amount:,.2f}\n\n"
        f"{party_label}: {party_display}\n"
        f"Bank/Cash: {bank_qualified}  [{bank_code}]\n"
        f"Category: {cat_qualified}  [{cat_code}]\n"
        f"Date: {trans_date.strftime('%m/%d/%Y')}\n"
        f"Journal entry: {direction}"
        f"{check_line}{review_line}")

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
        'bank_account': bank_qualified,
        'bank_acc_code': bank_code,
        'bank_level': bank_level,
        'category_account': cat_qualified,
        'category_acc_code': cat_code,
        'category_level': cat_level,
        'category_matched': category_matched,
        # --- aliases the React client reads (App.jsx
        # buildVoucherMessageText / VoucherCard). Keep both names so
        # neither side has to be renamed. ---
        'first_leg_account': bank_qualified,
        'account_name': cat_qualified,
        'bank_acc_id': bank_code,
        'income_acc_id': cat_code if entry_type == 'CRV' else None,
        'expense_acc_id': cat_code if entry_type == 'CPV' else None,
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
        'extraction_source': extraction_source,
        'isVoucher': True,
    }


def commit_voucher(conn, d: VoucherCommit) -> Dict[str, Any]:
    """
    Post a draft the operator has reviewed. Accounts are named BY CODE here,
    exactly as on an edit: the fuzzy matcher already had its turn during the
    preview and a human has since signed off, so re-guessing would only be a
    way to post something they never saw.
    """
    entry_type = (d.entry_type or '').strip().upper()
    if entry_type not in ('CRV', 'CPV'):
        raise ValueError("entry_type must be CRV or CPV.")

    try:
        amount = abs(float(d.amount))
    except (TypeError, ValueError):
        raise ValueError("I couldn't read the amount as a number.")
    if amount == 0:
        raise ValueError("The amount is zero, so there is nothing to post. "
                         "Enter the amount in the review panel.")

    iso = normalize_date_str(d.transaction_date)
    if not iso:
        raise ValueError(
            f"I couldn't read \"{d.transaction_date}\" as a date. Try "
            f"06/07/2026, 7-june-2026 or 2026-06-07.")
    trans_date = datetime.strptime(iso, '%Y-%m-%d').date()

    review_items: List[str] = []
    if not within_open_year(conn, trans_date):
        if STRICT_FISCAL_YEAR:
            raise ValueError(
                f"{trans_date:%m/%d/%Y} is outside the financial year this "
                f"company currently has open, so nothing was posted. Either "
                f"change the date to one inside the open year, or ask whoever "
                f"administers LockInLedger to reopen that period.")
        review_items.append('date (outside the open fiscal year)')

    # ---- party: an existing code, or a name to create one from ----
    p_type = P_TYPE_CUSTOMER if entry_type == 'CRV' else P_TYPE_VENDOR
    party_code = (d.party_code or '').strip() or None
    if party_code:
        rows = _fetch_all(
            conn,
            "SELECT p_code, p_type, company_name, person_name FROM acc_party "
            "WHERE p_code = %s AND system_id = %s LIMIT 1",
            (party_code, SYSTEM_ID), "commit_voucher.party")
        if not rows:
            raise ValueError(
                "That customer/vendor record no longer exists in LockInLedger. "
                "Pick another one in the review panel, or create it with "
                "\"add vendor <name>\".")
        party_display = rows[0].get('company_name') or rows[0].get('person_name')
        party_type = rows[0]['p_type']
        party_match_type, party_score = 'code', 1.0
    else:
        name = (d.party_name or '').strip()
        if not name:
            raise ValueError("Name the customer or vendor this entry belongs to.")
        party_code, party_display = create_party(conn, name, p_type)
        party_type, party_match_type, party_score = p_type, 'new', 0.0
        review_items.append(f"new {'customer' if entry_type == 'CRV' else 'vendor'} record")

    # ---- accounts, by code ----
    natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
    bank_row = _require_postable(conn, d.bank_acc_code, "Bank/cash", entry_type=entry_type)
    cat_row = _require_postable(conn, d.category_acc_code, "Category",
                                natures=natures, entry_type=entry_type)
    if bank_row['code'] == cat_row['code']:
        raise ValueError(
            f"Both sides of this entry point at \"{bank_row['qualified']}\", so "
            f"it would cancel itself out and change nothing. The bank/cash line "
            f"and the category line have to be two different accounts.")

    description = (d.description or '').strip() or (
        'Receipt Voucher' if entry_type == 'CRV' else 'Payment Voucher')
    cheque_no = (d.cheque_no or '').strip() or None

    new_id = insert_voucher(conn, entry_type, party_code,
                            Resolution(bank_row, 'code', 1.0),
                            Resolution(cat_row, 'code', 1.0),
                            amount, trans_date, description, cheque_no)

    log_voucher_posting(
        f"{entry_type}-{new_id}", entry_type,
        {'amount': amount, 'party_display': party_display, 'party_type': party_type,
         'party_match_type': party_match_type, 'party_score': party_score,
         'bank_desc': bank_row['qualified'], 'bank_code': bank_row['code'],
         'bank_level': bank_row['level'], 'bank_match_type': 'code', 'bank_score': 1.0,
         'bank_text': None,
         'category_desc': cat_row['qualified'], 'category_code': cat_row['code'],
         'category_level': cat_row['level'], 'category_match_type': 'code',
         'category_score': 1.0,
         'trans_date': trans_date.isoformat(), 'cheque_no': cheque_no},
        d.session_id or 'commit', d.source_message or '(reviewed draft)')

    return _voucher_success_payload(
        new_id=new_id, entry_type=entry_type, amount=amount,
        party_code=party_code, party_display=party_display, party_type=party_type,
        party_match_type=party_match_type, party_score=party_score,
        bank_code=bank_row['code'], bank_qualified=bank_row['qualified'],
        bank_level=bank_row['level'],
        cat_code=cat_row['code'], cat_qualified=cat_row['qualified'],
        cat_level=cat_row['level'],
        trans_date=trans_date, cheque_no=cheque_no, description=description,
        review_items=review_items, extraction_source='reviewed')


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
class AccountingBot:
    def __init__(self):
        self.name = "LedgerAssist"
        api_key = os.getenv("ACCOUNTING_GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
        # The model name doesn't depend on whether a key was found - keeping it
        # unconditional means anything that reads self.model (the voice pass,
        # /api/debug/extract) works the same however the client got attached.
        # self.model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        if api_key:
            self.groq_client = Groq(api_key=api_key)
            print(f"Groq client initialized ({self.model})")
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
            'Everything is typed here, and nothing is written until you confirm\n'
            'it in the panel on the right. Five things I can do:\n\n'
            'RECORD - describe it, then confirm it in the panel on the right\n'
            '  Paid $450 to Handy Fix LLC for Repair and Maintenance '
            'from Bank of America 9523\n'
            '  Received $659.25 from John Smith today on bank Meezan 1234\n'
            '  Paid $300 to XYZ Ltd for Office Supplies yesterday\n'
            '  Paid $700 to XYZ Ltd for EXPENSE/Repair and Maintenance, check 4521\n'
            '  06/04/2026 ACH DEPOSIT - JOHN SMITH 659.25\n'
            '  The bank can be introduced by from / into / via / on / in / at.\n\n'
            'REVIEW / EDIT ONE - name the voucher by its id\n'
            '  260902000001                     (opens it in the preview)\n'
            '  show 260902000001\n'
            '  update 260902000001 amount 500\n'
            '  update 260902000001 category Printing, date 06/10/2026\n'
            '  update 260902000001 party Handy Fix LLC, bank Bank of America 9523\n'
            '  void 260902000001\n'
            '  Editable fields: amount, date, party, bank, category,\n'
            '  check no., description. Only the fields you name change.\n\n'
            'EDIT BY DATE - give a date, tick the ones you want, edit them\n'
            '  7-june-2026                      (bare date lists that day)\n'
            '  06/07/2026\n'
            '  vouchers on 7 June 2026\n'
            '  show vouchers dated 7-jun-26\n'
            '  edit vouchers today\n'
            '  update 7-june-2026\n'
            '  Tick the vouchers, optionally type one change for all of them\n'
            '  (e.g. "category Printing"), then confirm each in the preview.\n\n'
            'PROFILES - customers, vendors, employees\n'
            '  add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143\n'
            '  new customer Acme Corp\n'
            '  add employee Maria Lopez, phone 555-0192\n'
            "  Change ABC Trading's phone number to 555-987-6543\n"
            '  update customer ABC Trading, email accounts@abctrading.com\n'
            '  Kinds: customer, vendor/payee, employee, other. The kind is fixed\n'
            '  once created - the code is seeded from it.\n\n'
            'CHART OF ACCOUNTS - view, add, rename, retire\n'
            '  show chart\n'
            '  expense chart\n'
            '  add expense account Fuel\n'
            '  add bank account Meezan 1234\n'
            '  add account FICA under Payroll Taxes\n'
            '  rename account Fuel to Fuel and Oil\n'
            '  deactivate account Tolls        (hides it; history keeps it)\n'
            '  activate account Tolls\n'
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

    async def extract_transaction_info(self, message: str, session_id: str,
                                       mode: Optional[str] = None) -> Dict:
        # Which command the person pressed before typing. A hint, never a rule:
        # someone can press "Create CPV" and then type a receipt, and the words
        # in the sentence have to win. It only helps the model break a tie.
        hint = MODE_HINT.get((mode or '').lower())
        prompt = f"""
Extract financial transaction information from this message.

User message: "{message}"
{('CONTEXT (a hint only - the words in the message always win): ' + hint)
 if hint else ''}

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

        # Gated on the SAME breaker as the fallback. It used to check only that
        # a client object existed, so a dead model was dialled once per message
        # forever - a full network round-trip to a 404 on the critical path of
        # every single chat request, while the fallback beside it had long since
        # given up. One breaker, both call sites.
        if self.groq_client and _llm_available():
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
                # A model that answered with bad JSON is alive and reachable -
                # that is a bad reply, not a broken connection, and it must not
                # open the breaker. Only a transport or API failure counts.
                if isinstance(e, (json.JSONDecodeError, ValueError)):
                    print(f"Groq extraction error: {llm_error}")
                else:
                    _llm_note_fail(e, 'the extractor')
        elif not self.groq_client:
            llm_error = ("no API key (set ACCOUNTING_GROQ_API_KEY or GROQ_API_KEY)")
        else:
            llm_error = "model calls paused after repeated failures"

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

    async def handle_show_chart(self, conn, nature: Optional[str]) -> Dict:
        tree = build_chart_tree(conn)
        text = render_chart_tree(tree, nature)
        shown = [n for n in tree if not nature or n['nature'] == nature]
        return self._reply(text, action='chart', card={
            "kind": "chart",
            "filtered_to": NATURE_LABEL.get(nature) if nature else None,
            "postable_total": sum(n['postable_count'] for n in shown),
            "natures": shown,
        })

    async def handle_add_account(self, conn, parsed: Dict,
                                 preview: bool = False) -> Dict:
        return self._add_account(conn, parsed, preview)

    def _add_account(self, conn, parsed: Dict, preview: bool = False) -> Dict:
        name = parsed['name']
        tree = build_chart_tree(conn)

        # ---- work out what the new account hangs off ----------------------
        level = parent_code = parent_label = None
        if parsed['bank']:
            main, near = find_main_by_name(tree, 'Banks')
            if not main:
                return self._reply(
                    "I can't find a \"Banks\" heading in this chart. Name the parent "
                    "explicitly:\n    add account " + name + " under <heading>", 'error')
            level, parent_code, parent_label = 'sub', main['code'], main['name']

        elif parsed['parent_text']:
            txt = parsed['parent_text']
            nat = _NATURE_WORD.get(normalize_name(txt))
            if not nat:
                nat = next((n['nature'] for n in tree
                            if normalize_name(n['label']) == normalize_name(txt)
                            or normalize_name(n['label']).rstrip('s') == normalize_name(txt).rstrip('s')),
                           None)
            if nat:
                level, parent_code, parent_label = 'main', nat, NATURE_LABEL.get(nat, nat)
            else:
                main, near = find_main_by_name(tree, txt)
                if not main:
                    return self._reply(
                        f"I couldn't find \"{txt}\" in the chart. Did you mean one of:\n"
                        + "\n".join(f"    {n}" for n in near)
                        + "\n\nOr type \"show chart\" to see the whole thing.", 'error')
                level, parent_code, parent_label = 'sub', main['code'], main['name']

        elif parsed['nature']:
            level = 'main'
            parent_code = parsed['nature']
            parent_label = NATURE_LABEL.get(parent_code, parent_code)
        else:
            return self._reply(
                f"Where should \"{name}\" go? Name the parent:\n"
                f"    add expense account {name}\n"
                f"    add account {name} under EXPENSE\n"
                f"    add account {name} under Payroll Taxes", 'error')

        # ---- the hazard: turning a posted-to account into a heading -------
        if level == 'sub':
            main = next((m for n in tree for m in n['mains']
                         if m['code'] == parent_code), None)
            if main and main['postable']:
                n_txn = _main_has_transactions(conn, parent_code)
                if n_txn and not ALLOW_ORPHANING_POSTED_MAIN:
                    return self._reply(
                        f"\"{parent_label}\" [{parent_code}] has {n_txn} transaction"
                        f"{'' if n_txn == 1 else 's'} posted directly to it.\n\n"
                        f"Adding a sub-account under it would turn it into a heading: "
                        f"those {n_txn} entries would stay pointing at a code that can "
                        f"no longer be posted to, and I would stop resolving the name "
                        f"\"{parent_label}\" for new vouchers.\n\n"
                        f"Either add \"{name}\" as its own account at the same level:\n"
                        f"    add account {name} under "
                        f"{next((n['label'] for n in tree for m in n['mains'] if m['code'] == parent_code), 'EXPENSE')}\n"
                        f"or set ALLOW_ORPHANING_POSTED_MAIN=1 to do it anyway, the way "
                        f"the host application does.", 'error')

        # ---- Stop here when the client asked for a draft ----
        # The parent has been resolved and the hazard checked; nothing is
        # written until the operator confirms.
        if preview:
            return self._account_draft_payload(
                msg=parsed.get('source_message') or '',
                name=name, level=level, parent_code=parent_code,
                parent_label=parent_label, tree=tree)

        try:
            r = add_chart_account(conn, level, name, parent_code)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            # The chat reply only ever showed "KeyError: 0" with no line
            # number, which made this class of bug nearly impossible to
            # localize from the terminal alone. Print the real traceback
            # server-side so the next failure (whatever it is) points at an
            # exact file:line instead of a bare exception name.
            traceback.print_exc()
            return self._reply(_internal_error_message(e, "adding that account"),
                               'error', _final=True)

        return self._account_created_reply(r, level, parent_code, parent_label)

    def _account_created_reply(self, r, level, parent_code, parent_label) -> Dict:
        kind = 'sub-account' if level == 'sub' else 'account'
        lines = [f"Created {kind} {r['code']} - {r['name']}",
                 f"Under: {parent_label} [{parent_code}]",
                 f"It is postable immediately, so you can use "
                 f"\"{r['name']}\" in a voucher now."]
        if level == 'sub':
            lines.append("")
            lines.append(f"\"{parent_label}\" is now a heading and can no longer "
                         f"be posted to directly.")
        return self._reply("\n".join(lines), action='account_created', card={
            "kind": "account", "code": r['code'], "name": r['name'],
            "level": level, "parent_code": parent_code, "parent_name": parent_label,
        })

    def _account_draft_payload(self, *, msg, name, level, parent_code,
                               parent_label, tree) -> Dict[str, Any]:
        """A resolved but UNCREATED chart account, for the operator to confirm."""
        warnings = []
        if level == 'sub':
            main = next((m for n in tree for m in n['mains']
                         if m['code'] == parent_code), None)
            if main and main['postable']:
                warnings.append(f'"{parent_label}" becomes a heading and can no '
                                f'longer be posted to')
        draft = {
            'kind': 'account',
            'doc_label': 'New Chart Account',
            'name': name,
            'level': level,
            'level_label': 'Sub-account' if level == 'sub' else 'Main account',
            'parent_code': parent_code,
            'parent_name': parent_label,
            'parent_options': _chart_parent_options(tree),
            'source_message': msg,
            'review_items': warnings,
        }
        lines = ["Ready to create - nothing has been written yet.", "",
                 f"Name: {name}",
                 f"Level: {draft['level_label']}",
                 f"Under: {parent_label} [{parent_code}]"]
        if warnings:
            lines += ["", "Check before creating: " + '; '.join(warnings) + "."]
        text = strip_emojis("\n".join(lines))
        return {'status': 'draft', 'action': 'draft', 'message': text, 'analysis': text,
                'confidence': 'medium' if warnings else 'high',
                'draft': draft, 'review_items': warnings,
                'review_note': '; '.join(warnings) if warnings else None,
                'card': {**draft, 'kind': 'draft', 'draft_kind': draft['kind']}}

    async def handle_chart_edit(self, conn, parsed: Dict,
                                preview: bool = False, msg: str = '') -> Dict:
        """Rename or retire an account, through the same review step."""
        tree = build_chart_tree(conn)
        target, near = find_main_by_name(tree, parsed['name'])
        code = None
        if target:
            code, label = target['code'], target['name']
        else:
            # It may be a sub-account rather than a heading.
            hits = [(m, sub) for n in tree for m in n['mains'] for sub in m['subs']
                    if normalize_name(sub['name']) == normalize_name(parsed['name'])]
            if len(hits) == 1:
                code, label = hits[0][1]['code'], hits[0][1]['name']
        if not code:
            t = (f"I couldn't find an account called \"{parsed['name']}\"."
                 + (("\n\nDid you mean:\n" + "\n".join(f"  {n}" for n in near[:6]))
                    if near else "")
                 + "\n\nType \"show chart\" to see everything you have.")
            return self._reply(t, 'error')

        op = parsed['op']
        blockers: List[str] = []
        if op == 'rename':
            try:
                others = _tenants_using(conn, code)
            except ValueError as e:
                return self._reply(str(e), 'error')
            if others > 1:
                return self._reply(
                    f"\"{label}\" is part of the standard chart that {others} "
                    f"companies share, and the name is stored once for all of "
                    f"them — renaming it here would rename it for every one.\n\n"
                    f"Add your own account instead:\n"
                    f"  add account {parsed['new_name']} under <heading>",
                    'error', _final=True)

        if preview:
            return self._chart_edit_draft(conn, code=code, label=label, op=op,
                                          new_name=parsed.get('new_name'), msg=msg)
        try:
            if op == 'rename':
                r = rename_chart_account(conn, code, parsed['new_name'])
            else:
                r = set_chart_account_active(conn, code, op == 'activate')
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(_internal_error_message(e, "changing that account"),
                               'error', _final=True)
        return self._reply(r['message'], action='account_updated', card={
            "kind": "account", "code": r['code'], "name": r.get('name'),
            "level": account_level(r['code']), "updated": True,
            "parent_name": None, "parent_code": None,
        })

    def _chart_edit_draft(self, conn, *, code, label, op, new_name, msg) -> Dict[str, Any]:
        warnings = []
        if op == 'deactivate':
            n = _main_has_transactions(conn, code)
            if n:
                warnings.append(f'{n} posted voucher{"" if n == 1 else "s"} already '
                                f'use this account — they keep it, but new entries '
                                f'cannot')
        draft = {
            'kind': 'account',
            'op': op,
            'code': code,
            'doc_label': ('Rename account' if op == 'rename'
                          else 'Retire account' if op == 'deactivate'
                          else 'Restore account'),
            'name': new_name if op == 'rename' else label,
            'level': account_level(code),
            'level_label': ('Sub-account' if account_level(code) == 'sub'
                            else 'Main account'),
            'parent_code': None, 'parent_name': None, 'parent_options': [],
            'original': {'name': label, 'code': code},
            'source_message': msg,
            'review_items': warnings,
        }
        if op == 'rename':
            lines = [f"\"{label}\" would become \"{new_name}\".", "",
                     "Nothing has changed yet."]
        else:
            lines = [f"\"{label}\" would be taken "
                     + ("out of" if op == 'deactivate' else "back into")
                     + " this company's chart.", "",
                     "Posted vouchers are untouched either way."]
        text = strip_emojis("\n".join(lines))
        return {'status': 'draft', 'action': 'draft', 'message': text,
                'analysis': text, 'confidence': 'high', 'draft': draft,
                'review_items': warnings,
                'review_note': '; '.join(warnings) or None,
                'card': {**draft, 'kind': 'draft', 'draft_kind': 'account'}}

    async def handle_show_voucher(self, conn, at_id: str,
                                  preview: bool = False) -> Dict:
        v = fetch_voucher(conn, at_id)
        if not v.get("found"):
            return self._reply(v.get("error", f"No voucher {at_id}."), 'error')

        # Naming a voucher is a request to work on it. Under preview it opens
        # in the review panel, already filled in, instead of printing a card
        # and a grammar lesson about how to change it.
        if preview and v.get('editable'):
            return self._edit_draft_payload(conn, v, msg=f"show {at_id}")
        lines = [f"{v['voucher_number']} — ${v['amount']:,.2f}",
                 f"{'Customer' if v['entry_type'] == 'CRV' else 'Vendor'}: {v['party_name'] or '—'}",
                 f"Bank/Cash: {v['bank_account']} [{v['bank_acc_code']}]",
                 f"Category: {v['category_account']} [{v['category_acc_code']}]",
                 f"Date: {v['transaction_date']}"]
        if v.get('cheque_no'):
            lines.append(f"Check #{v['cheque_no']}")
        if not v['editable']:
            lines.append("")
            lines.append("Locked: " + "; ".join(v['blockers']) + ".")
        else:
            lines.append("")
            lines.append("To change it, name the fields, e.g.")
            lines.append(f"    update {v['at_id']} amount 500, category Printing")
        return self._reply("\n".join(lines), card=self._voucher_card(v), action='show')

    async def handle_update_voucher(self, conn, at_id: str, fields: Dict[str, str],
                                    tail: Optional[str] = None,
                                    preview: bool = False, msg: str = '') -> Dict:
        if not fields:
            # "update 26...0001" with no fields named: open it for editing
            # rather than lecturing about the grammar.
            r = await self.handle_show_voucher(conn, at_id, preview)
            if tail:
                r['message'] = (f"I couldn't see a field name in \"{tail}\" — "
                                f"opened it for editing instead.\n\n") + r['message']
                r['analysis'] = r['message']
                # Same wording miss as a value that wouldn't resolve, and the
                # commoner one: "chnge teh bnk to bofa" has no field name the
                # rules recognise. Opening the voucher untouched is a fine
                # fallback, but the model deserves a look first.
                r['_field_error'] = True
            return r

        current = fetch_voucher(conn, at_id)
        if not current.get("found"):
            return self._reply(current.get("error", f"No voucher {at_id}."), 'error')
        if not current.get("editable"):
            return self._reply(
                f"{current['voucher_number']} can't be edited — it is "
                + "; ".join(current['blockers']) + ".\n\n"
                f"Void it and post a fresh one:\n    void {at_id}",
                'error', card=self._voucher_card(current), _final=True)

        entry_type = current['entry_type']
        upd, applied, resolved_notes, err = _resolve_update_fields(
            conn, entry_type, fields)
        if err:
            # A field value that didn't resolve is a WORDING miss, not a state
            # one - the voucher exists and is editable, we just couldn't read
            # what they wanted it changed to. The caller may retry it through
            # the model; "not found" and "not editable" never should be.
            r = self._reply(err, 'error')
            r['_field_error'] = True
            return r

        # ---- Stop here when the client asked for a draft ----
        # Every change has been resolved to a code; nothing is written.
        if preview:
            return self._edit_draft_payload(
                conn, current, upd=upd, applied=applied,
                notes=resolved_notes, msg=msg)

        try:
            out = update_voucher(conn, at_id, upd)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(_internal_error_message(e, "saving that change"), 'error', _final=True)

        text = out.get('message', f"Updated {at_id}.")
        if resolved_notes:
            text += "\n\nResolved: " + ", ".join(resolved_notes)
        return self._reply(text, card=self._voucher_card(out, out.get('review_note')),
                           action='updated')

    async def handle_void_voucher(self, conn, at_id: str,
                                  preview: bool = False) -> Dict:
        # Voiding is the one thing here that cannot be undone from the chat,
        # so it gets the same review step as everything else: the voucher
        # comes back on screen and the operator confirms what they are
        # reversing. Nothing is written on this path.
        if preview:
            current = fetch_voucher(conn, at_id)
            if not current.get('found'):
                return self._reply(
                    current.get('error', f"I couldn't find voucher {at_id}."),
                    'error')
            if current['at_status'] == '0':
                return self._reply(
                    f"{current['voucher_number']} is already void - "
                    f"nothing to do.", 'error',
                    card=self._voucher_card(current), _final=True)
            if current['reconciled_legs']:
                return self._reply(
                    f"{current['voucher_number']} has been bank-reconciled, so "
                    f"voiding it here would break the reconciliation. "
                    f"Unreconcile it in LockInLedger first, then void it.",
                    'error', _final=True)

            note = (f"This reverses {current['voucher_number']} - "
                    f"${float(current['amount'] or 0):,.2f} "
                    f"{'from' if current['entry_type'] == 'CRV' else 'to'} "
                    f"{current['party_name'] or 'the party'}. The voucher stays "
                    f"in the ledger marked void; it cannot be un-voided here.")
            out = self._edit_draft_payload(conn, current, notes=[note],
                                           msg=f"void {at_id}")
            out['draft']['op'] = 'void'
            out['draft']['doc_label'] = f"Void {current['voucher_number']}"
            out['card']['op'] = 'void'
            out['message'] = out['analysis'] = strip_emojis(
                f"{current['voucher_number']} is ready to void - "
                f"nothing has been written yet.\n\n{note}")
            return out

        try:
            out = void_voucher(conn, at_id)
        except ValueError as e:
            return self._reply(str(e), 'error')
        return self._reply(out.get('message', f"Voided {at_id}."),
                           card=self._voucher_card(out), action='voided')

    async def handle_edit_profile(self, conn, parsed: Dict,
                                  preview: bool = False, msg: str = '') -> Dict:
        """Change an existing profile. Same review step as creating one."""
        row, err = resolve_party_existing(conn, parsed['name'], parsed.get('p_type'))
        if not row:
            names = [p.get('company_name') or p.get('person_name')
                     for p in list_parties(conn, q=parsed['name'][:12], limit=6)]
            t = (f"I couldn't find a profile called \"{parsed['name']}\"."
                 + (("\n\nDid you mean:\n" + "\n".join(f"  {n}" for n in names if n))
                    if names else "")
                 + f"\n\nOr create it:\n  add customer {parsed['name']}")
            return self._reply(t, 'error')

        current = fetch_party(conn, row['p_code'])
        if not current.get('found'):
            return self._reply(current.get('error', 'Profile not found.'), 'error')

        fields = dict(parsed['fields'])
        if fields.get('p_account'):
            res, aerr = resolve_bank_account(conn, fields['p_account'],
                                             allow_default=False)
            if not res:
                return self._reply(f"Default account: {aerr}", 'error')
            fields['p_account'] = res.code

        if preview:
            return self._profile_edit_draft(conn, current, fields,
                                            parsed.get('unparsed'), msg)
        try:
            out = update_party(conn, current['p_code'], fields)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(_internal_error_message(e, "saving that profile"), 'error', _final=True)
        return self._reply(out['message'], action='profile', card={
            "kind": "profile", "created": False, "updated": True,
            "p_code": out['p_code'], "p_type": out['p_type'],
            "type_label": out['type_label'],
            "company_name": out['company_name'], "person_name": out['person_name'],
            "email": out['email'], "phone": out['phone'], "address": out['address'],
            "job_title": out['job_title'], "p_account": out['p_account'],
        })

    def _profile_edit_draft(self, conn, current: Dict, fields: Dict,
                            unparsed: Optional[List[str]], msg: str) -> Dict[str, Any]:
        """An existing profile opened for editing - `original` rides along so
        the panel can show what each field used to be."""
        proposed = {k: current.get(k) for k in _PARTY_EDITABLE}
        proposed.update({k: v for k, v in fields.items() if k in _PARTY_EDITABLE})
        acc_row = (find_account(conn, proposed.get('p_account'))
                   if proposed.get('p_account') else None)
        warnings = [f'unlabelled: {"; ".join(unparsed)}'] if unparsed else []

        draft = {
            'kind': 'party',
            'p_code': current['p_code'],
            'doc_label': f"Edit {current['company_name']}",
            'type_label': current['type_label'],
            'p_types': [{'value': k, 'label': v} for k, v in VALID_P_TYPES.items()],
            'p_account_label': acc_row['qualified'] if acc_row else None,
            'existing': None,
            'applied': sorted(fields),
            'original': {k: current.get(k) for k in _PARTY_EDITABLE},
            'unparsed': unparsed or [],
            'source_message': msg,
            'review_items': warnings,
            **proposed,
        }
        lines = [f"{current['company_name']} is open for editing — nothing has "
                 f"changed yet.", ""]
        for k in sorted(fields):
            lines.append(f"{k.replace('_', ' ')}: {current.get(k) or '—'}"
                         f"  ->  {proposed.get(k)}")
        text = strip_emojis("\n".join(lines))
        return {'status': 'draft', 'action': 'draft', 'message': text,
                'analysis': text, 'confidence': 'high', 'draft': draft,
                'review_items': warnings,
                'review_note': '; '.join(warnings) or None,
                'card': {**draft, 'kind': 'draft', 'draft_kind': 'party'}}

    async def handle_create_profile(self, conn, p_type: str, name: str,
                                    fields: Dict[str, str],
                                    unparsed: Optional[List[str]] = None,
                                    preview: bool = False) -> Dict:
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

        # ---- Stop here when the client asked for a draft ----
        # Only lookups have run: the duplicate check and the default-account
        # resolution are both reads.
        if preview:
            return self._party_draft_payload(
                conn, p_type=p_type, payload=payload, unparsed=unparsed,
                msg=fields.get('_source_message') or '')

        try:
            r = create_party_full(conn, payload)
        except ValueError as e:
            return self._reply(str(e), 'error')
        except Exception as e:
            return self._reply(_internal_error_message(e, "creating that profile"), 'error', _final=True)

        return self._profile_created_reply(conn, r, payload, p_type, unparsed)

    def _profile_created_reply(self, conn, r, payload, p_type, unparsed) -> Dict:
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

    def _party_draft_payload(self, conn, *, p_type, payload, unparsed, msg) -> Dict[str, Any]:
        """A resolved but UNCREATED profile, for the operator to confirm."""
        warnings = list(unparsed and
                        [f'unlabelled: {"; ".join(unparsed)}'] or [])

        # Would this reuse an existing profile rather than create one? Same
        # question create_party_full() asks, asked without writing.
        existing = None
        target = normalize_name(payload.company_name or '')
        for p in find_party_candidates(conn, payload.company_name or '',
                                       p_type=p_type, limit=50):
            for cand in (p.get('company_name'), p.get('person_name')):
                if cand and normalize_name(cand) == target:
                    existing = {'p_code': str(p['p_code']), 'name': cand}
                    break
            if existing:
                break
        if existing:
            warnings.append(f"already exists as {existing['p_code']} - it will be "
                            f"reused, not duplicated")

        acc_row = find_account(conn, payload.p_account) if payload.p_account else None
        draft = {
            'kind': 'party',
            'doc_label': 'New ' + VALID_P_TYPES[p_type],
            'p_type': p_type,
            'type_label': VALID_P_TYPES[p_type],
            'p_types': [{'value': k, 'label': v} for k, v in VALID_P_TYPES.items()],
            'company_name': payload.company_name,
            'person_name': payload.person_name,
            'email': payload.email, 'phone': payload.phone, 'fax': payload.fax,
            'address': payload.address, 'city': payload.city,
            'state': payload.state, 'zipcode': payload.zipcode,
            'job_title': payload.job_title,
            'sale_tax_no': payload.sale_tax_no, 'fedral_id_no': payload.fedral_id_no,
            'business_desc': payload.business_desc, 'other_desc': payload.other_desc,
            'p_account': payload.p_account,
            'p_account_label': acc_row['qualified'] if acc_row else None,
            'existing': existing,
            'unparsed': unparsed or [],
            'source_message': msg,
            'review_items': warnings,
        }
        lines = ["Ready to create - nothing has been written yet.", "",
                 f"{VALID_P_TYPES[p_type]}: {payload.company_name}"]
        for k, lbl in (('email', 'Email'), ('phone', 'Phone'), ('address', 'Address'),
                       ('job_title', 'Title')):
            if getattr(payload, k, None):
                lines.append(f"{lbl}: {getattr(payload, k)}")
        if warnings:
            lines += ["", "Check before creating: " + '; '.join(warnings) + "."]
        text = strip_emojis("\n".join(lines))
        return {'status': 'draft', 'action': 'draft', 'message': text, 'analysis': text,
                'confidence': 'medium' if warnings else 'high',
                'draft': draft, 'review_items': warnings,
                'review_note': '; '.join(warnings) if warnings else None,
                'card': {**draft, 'kind': 'draft', 'draft_kind': draft['kind']}}

    def _edit_draft_payload(self, conn, current: Dict,
                            upd: Optional[VoucherUpdate] = None,
                            applied: Optional[List[str]] = None,
                            notes: Optional[List[str]] = None,
                            msg: str = '') -> Dict[str, Any]:
        """
        An existing voucher opened for editing, with any requested changes
        already resolved but NOT written. `original` travels alongside so the
        panel can show what each field used to be.
        """
        applied = list(applied or [])
        entry_type = current['entry_type']
        proposed = {
            'amount': current['amount'],
            'transaction_date': current['transaction_date'],
            'party_code': current['party_code'],
            'party_name': current['party_name'],
            'bank_acc_code': current['bank_acc_code'],
            'bank_account': current['bank_account'],
            'category_acc_code': current['category_acc_code'],
            'category_account': current['category_account'],
            'cheque_no': current['cheque_no'],
            'description': current['description'],
        }
        if upd is not None:
            if upd.amount is not None:
                proposed['amount'] = upd.amount
            if upd.transaction_date:
                proposed['transaction_date'] = upd.transaction_date
            if upd.cheque_no is not None:
                proposed['cheque_no'] = upd.cheque_no or None
            if upd.description is not None:
                proposed['description'] = upd.description
            # A changed code needs its display name too, or the panel would
            # show the new code beside the old name.
            if upd.party_code:
                proposed['party_code'] = upd.party_code
                rows = _fetch_all(
                    conn,
                    "SELECT company_name, person_name FROM acc_party "
                    "WHERE p_code = %s AND system_id = %s LIMIT 1",
                    (upd.party_code, SYSTEM_ID), "edit_draft.party")
                if rows:
                    proposed['party_name'] = (rows[0].get('company_name')
                                              or rows[0].get('person_name'))
            if upd.bank_acc_code:
                proposed['bank_acc_code'] = upd.bank_acc_code
                row = find_account(conn, upd.bank_acc_code)
                proposed['bank_account'] = row['qualified'] if row else upd.bank_acc_code
            if upd.category_acc_code:
                proposed['category_acc_code'] = upd.category_acc_code
                row = find_account(conn, upd.category_acc_code)
                proposed['category_account'] = row['qualified'] if row else upd.category_acc_code

        draft = {
            'kind': 'edit',
            'at_id': current['at_id'],
            'voucher_number': current['voucher_number'],
            'entry_type': entry_type,
            'doc_label': f"Edit {current['voucher_number']}",
            'party_label': 'Customer' if entry_type == 'CRV' else 'Vendor',
            'status_label': current.get('status_label'),
            'at_status': current.get('at_status'),
            'editable': current.get('editable'),
            'blockers': current.get('blockers') or [],
            'original': {
                'amount': current['amount'],
                'transaction_date': current['transaction_date'],
                'party_code': current['party_code'],
                'party_name': current['party_name'],
                'bank_acc_code': current['bank_acc_code'],
                'bank_account': current['bank_account'],
                'category_acc_code': current['category_acc_code'],
                'category_account': current['category_account'],
                'cheque_no': current['cheque_no'],
                'description': current['description'],
            },
            'applied': applied,
            'source_message': msg,
            'review_items': list(notes or []),
            **proposed,
        }

        lines = [f"{current['voucher_number']} is open for editing - "
                 f"nothing has been changed yet.", "",
                 f"{draft['party_label']}: {proposed['party_name'] or '-'}",
                 f"Bank/Cash: {proposed['bank_account']} [{proposed['bank_acc_code']}]",
                 f"Category: {proposed['category_account']} [{proposed['category_acc_code']}]",
                 f"Amount: ${float(proposed['amount'] or 0):,.2f}",
                 f"Date: {proposed['transaction_date']}"]
        if applied:
            lines += ["", "Requested changes: " + ', '.join(applied)]
        text = strip_emojis("\n".join(lines))
        return {'status': 'draft', 'action': 'draft', 'message': text, 'analysis': text,
                'confidence': 'high', 'draft': draft,
                'review_items': draft['review_items'],
                'review_note': '; '.join(draft['review_items']) or None,
                'card': {**draft, 'kind': 'draft', 'draft_kind': 'edit'}}

    async def handle_bulk_update(self, conn, date_iso: str, fields: Dict[str, str],
                                 tail: Optional[str] = None,
                                 preview: bool = False, msg: str = '') -> Dict:
        """
        One change, every voucher on a day - opened as a QUEUE of drafts.

        "Bulk" here means the typing is bulk, not the writing. Each voucher
        still comes back resolved-but-unwritten and is confirmed on its own,
        because a single misread word would otherwise rewrite a whole day of
        the ledger in one keystroke. This is the same machinery the tick-list
        already used; all that was missing was a way to say it in a sentence.
        """
        pretty = date_iso
        try:
            pretty = datetime.strptime(date_iso, '%Y-%m-%d').strftime('%m/%d/%Y')
        except Exception:
            pass

        if not fields:
            t = (f"I couldn't see a field name in \"{tail or msg}\".\n\n"
                 f"Name what to change, then the day:\n"
                 f"  change party to 3S for all vouchers on {date_iso}\n\n"
                 f"Fields: amount, date, party, bank, category, check, note.")
            return self._reply(t, 'error')

        rows = list_vouchers(conn, limit=200, on_date=date_iso)
        if not rows:
            return self._reply(
                f"There are no vouchers dated {pretty}, so there is nothing to "
                f"change.", 'error', _final=True)

        drafts, skipped = [], []
        for row in rows:
            at_id = str(row.get('at_id'))
            current = fetch_voucher(conn, at_id)
            if not current.get('found'):
                skipped.append((at_id, current.get('error', 'not found')))
                continue
            if not current.get('editable'):
                skipped.append((current['voucher_number'],
                                '; '.join(current['blockers'])))
                continue
            upd, applied, notes, err = _resolve_update_fields(
                conn, current['entry_type'], fields)
            if err:
                # The same value can be valid for one voucher and not another -
                # an income account on a CRV is not one on a CPV - so a failure
                # here skips that voucher rather than sinking the whole day.
                skipped.append((current['voucher_number'], err.splitlines()[0]))
                continue
            d = self._edit_draft_payload(conn, current, upd=upd, applied=applied,
                                         notes=notes, msg=msg)
            drafts.append(d['draft'])

        if not drafts:
            lines = [f"Nothing on {pretty} could take that change."]
            if skipped:
                lines += [""] + [f"  {n} - {why}" for n, why in skipped[:8]]
            return self._reply("\n".join(lines), 'error',
                               _final=not any('match' in w for _, w in skipped))

        what = ', '.join(sorted(fields))
        lines = [f"{len(drafts)} voucher{'' if len(drafts) == 1 else 's'} on "
                 f"{pretty} ready to change ({what}). Nothing has been written."]
        if skipped:
            lines += ["", f"Skipped {len(skipped)}:"]
            lines += [f"  {n} - {why}" for n, why in skipped[:8]]
            if len(skipped) > 8:
                lines.append(f"  ...and {len(skipped) - 8} more")
        lines += ["", "Step through them in the panel - each one saves on its own."]

        return {'status': 'draft', 'action': 'draft_queue',
                'message': strip_emojis("\n".join(lines)),
                'analysis': strip_emojis("\n".join(lines)),
                'confidence': 'high',
                'draft': drafts[0], 'drafts': drafts,
                'skipped': [{'voucher': n, 'reason': w} for n, w in skipped],
                'review_items': drafts[0].get('review_items') or [],
                'card': {**drafts[0], 'kind': 'draft', 'draft_kind': 'edit'}}

    async def handle_voucher_list(self, conn, date_iso: str) -> Dict:
        """A day's vouchers, to pick from for a bulk edit."""
        rows = list_vouchers(conn, limit=200, on_date=date_iso)
        pretty = date_iso
        try:
            pretty = datetime.strptime(date_iso, '%Y-%m-%d').strftime('%m/%d/%Y')
        except Exception:
            pass
        if not rows:
            return self._reply(f"No vouchers dated {pretty}.", 'error')
        editable = [r for r in rows if r['editable']]
        # The card below lists them; repeating it as text is just noise.
        text = (f"{len(rows)} voucher{'' if len(rows) == 1 else 's'} on {pretty}"
                f" - {len(editable)} editable. Tick the ones to change.")
        return self._reply(text, action='voucher_list', card={
            'kind': 'voucher_list', 'date': date_iso, 'date_label': pretty,
            'vouchers': rows, 'editable_count': len(editable),
        })

    # ----------------------------------------------------------------------
    # A statement, as drafts.
    #
    # This deliberately adds NO way to write. Every row is resolved with the
    # same three resolvers a typed sentence uses, packaged by the same
    # _draft_payload, and handed back as the queue a bulk edit already
    # produces - so it is saved one row at a time through /api/commit, by
    # code, after a person has read it. Importing sixty vouchers and posting
    # sixty vouchers stay two different acts.
    # ----------------------------------------------------------------------
    async def handle_statement_import(self, conn, parsed: Dict[str, Any], *,
                                      filename: str = '',
                                      bank_text: Optional[str] = None) -> Dict:
        rows = parsed.get('rows') or []
        if not rows:
            t = ("I couldn't find any transactions in that file. I can read a "
                 "bank or credit-card statement as a PDF, or a plain text or "
                 "CSV export of one - each line needs a date, a description "
                 "and an amount.\n\nIf it's a scanned image rather than a "
                 "digital statement, there's no text in it to read; export it "
                 "from your bank as a PDF or CSV instead.")
            return {'status': 'error', 'message': t, 'analysis': t,
                    'confidence': 'low', '_final': True}

        # ---- The cash leg, once, for the whole statement -------------------
        # Every row moves through the same account - that is what makes it a
        # statement. Leaving it unresolved would mean sixty drafts each
        # missing the identical field, so this one refuses up front instead.
        named = (bank_text or '').strip()
        bank_res = None
        if named:
            bank_res, _bank_err = resolve_bank_account(conn, named, allow_default=False)
        if not bank_res and parsed.get('account_hint'):
            # A statement identifies itself by number and nothing else, and a
            # bare "9993" is not a name the general matcher can use - it looks
            # like an account code, matches none, and falls through to a fuzzy
            # score against words. So the number is matched against the
            # account names directly, and ONLY when exactly one carries it:
            # two accounts ending 9993 is a question for the operator, not a
            # coin toss over which one gets a month of transactions.
            hits = _accounts_ending(conn, parsed['account_hint'])
            if len(hits) == 1:
                bank_res = Resolution(hits[0], 'statement_number', 0.95)
        if not bank_res:
            hint_no = parsed.get('account_hint')
            many = _accounts_ending(conn, hint_no) if hint_no and not named else []
            # When the number narrowed it to a handful, offer those; otherwise
            # offer the usual sample. Either way the operator picks from real
            # accounts rather than retyping a number that already failed.
            choices = ([{'code': a['code'], 'qualified': a['qualified']} for a in many]
                       if many else
                       [{'code': a['code'], 'qualified': a['qualified']}
                        for a in get_chart(conn)[:200]])
            names = ([a['qualified'] for a in many] if many
                     else _bank_samples(conn, 4))
            listed = ("\n\nYours include:\n" + "\n".join(f"  - {n}" for n in names)
                      if names else "")
            if named:
                said = f"I couldn't match \"{named}\" to an account. "
            elif len(many) > 1:
                said = (f"{len(many)} of your accounts carry the number "
                        f"{hint_no} ({', '.join(a['qualified'] for a in many)}), "
                        f"so the statement doesn't say which one it is. ")
            elif hint_no:
                said = (f"The statement says account {hint_no}, but nothing in "
                        f"your chart matches it. ")
            else:
                said = "The statement doesn't say which account it belongs to. "
            t = (f"{said}I need to know which of your accounts this statement "
                 f"belongs to before I can read it - every row posts through "
                 f"it, so guessing would put the whole month in the wrong "
                 f"place.{listed}\n\nUpload it again and name the account, or "
                 f"pick it below. Nothing has been read in.")
            return {'status': 'error', 'message': t, 'analysis': t,
                    'confidence': 'low', 'action': 'statement_needs_bank',
                    'card': {'kind': 'statement_bank_pick',
                             'filename': filename,
                             'choices': choices,
                             'account_hint': parsed.get('account_hint'),
                             'rows': len(rows)},
                    '_final': True}

        # ---- What is already in the ledger on these dates ------------------
        # A statement that gets uploaded twice would otherwise double the
        # month. Nothing is blocked on this - it is a flag, because a customer
        # really can pay the same amount on the same day twice - but it is
        # shown before the save, not discovered after it.
        existing: Dict[str, List[Dict]] = {}
        for d in sorted({r.date for r in rows}):
            try:
                existing[d] = list_vouchers(conn, limit=200, on_date=d)
            except Exception:
                existing[d] = []

        drafts: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        seen_new_party: Dict[str, int] = {}

        for n, r in enumerate(rows, start=1):
            try:
                draft = self._statement_row_draft(
                    conn, r, bank_res=bank_res, existing=existing.get(r.date, []),
                    index=n, filename=filename)
            except Exception as e:
                skipped.append({'line': r.line_no, 'date': r.date,
                                'amount': r.amount,
                                'text': r.description[:120],
                                'why': _internal_error_message(e, "reading that line")})
                continue
            if draft.get('party_is_new') and draft.get('party_name'):
                key = normalize_name(draft['party_name'])
                seen_new_party[key] = seen_new_party.get(key, 0) + 1
            drafts.append(draft)

        for u in (parsed.get('unreadable') or []):
            skipped.append({'line': u.get('line'), 'text': u.get('text', '')[:120],
                            'why': u.get('why', 'could not be read')})

        if not drafts:
            t = ("I read the file but couldn't turn any line into a voucher. "
                 "Nothing has been written.")
            return {'status': 'error', 'message': t, 'analysis': t,
                    'confidence': 'low', '_final': True,
                    'card': {'kind': 'statement_summary', 'skipped': skipped,
                             'filename': filename}}

        return self._statement_reply(drafts, skipped, bank_res=bank_res,
                                     parsed=parsed, filename=filename)

    def _statement_row_draft(self, conn, r: 'StatementRow', *, bank_res,
                             existing: List[Dict], index: int,
                             filename: str) -> Dict[str, Any]:
        """One statement row, resolved into the same draft a sentence makes."""
        natures = CRV_INCOME_NATURES if r.entry_type == 'CRV' else CPV_EXPENSE_NATURES
        party_name, party_why = statement_party(r.description)

        # ---- Party: matched, never created --------------------------------
        party_code = party_display = None
        party_match_type, party_score = 'none', 0.0
        party_type = P_TYPE_CUSTOMER if r.entry_type == 'CRV' else P_TYPE_VENDOR
        if party_name:
            found, _err = resolve_party_existing(conn, party_name, party_type)
            if found:
                party_code = str(found['p_code'])
                party_display = found.get('matched_name') or party_name
                party_type = found.get('p_type') or party_type
                exact = normalize_name(party_display) == normalize_name(party_name)
                party_match_type = 'exact' if exact else 'fuzzy'
                party_score = 1.0 if exact else name_similarity(party_name, party_display)
            else:
                party_display = title_case_name(party_name)
                party_match_type = 'new'
        else:
            party_display = ''

        # ---- Category leg --------------------------------------------------
        # The description is the only hint there is, and most of the time it
        # names a person rather than an account. So a miss here is normal and
        # is left as an empty field with a note, exactly as a typed line
        # would be - not as a refusal, and never as a silent default that
        # quietly files a month of income under one heading.
        cat_res, cat_err = resolve_category_account(conn, r.description, natures)
        if cat_res is None and cat_err is None:
            kind = 'revenue' if r.entry_type == 'CRV' else 'expense'
            raise ValueError(
                f"There are no {kind} accounts in this company's chart yet, so "
                f"there is nothing to post the other side of this row to.")
        category_matched = cat_res is not None
        cat_note = None
        if not cat_res:
            cat_res, _d = _default_category_resolution(conn, r.entry_type, natures)
            if cat_res:
                cat_note = AccountProblem(
                    f"Nothing in the line named an account, so this is the "
                    f"default ({cat_res.qualified}). Change it here if the row "
                    f"belongs somewhere else.",
                    "Defaulted - check it")
            else:
                cat_note = AccountProblem(
                    "The line doesn't name an account and there's no default "
                    "set, so pick the one this belongs to.",
                    "Not matched - choose it")

        review: List[str] = []
        if party_why:
            review.append(f'no party on this line - {party_why}')
        if statement_is_transfer(r.description):
            # The one reading that is quietly wrong rather than obviously
            # wrong: money moved between two accounts this company already
            # owns, posted as income or expense, invents a number that never
            # happened - and the row looks like any other deposit while it
            # does it.
            review.append('looks like a transfer between your own accounts, '
                          'not income or expense - check the other side')

        dup = next((v for v in existing
                    if abs(float(v.get('amount') or 0) - r.amount) < 0.005
                    and v.get('entry_type') == r.entry_type
                    and v.get('at_status') != '0'), None)
        if dup:
            review.append(f"{dup['voucher_number']} is already posted for "
                          f"${r.amount:,.2f} on this date - this may be it again")

        review = _collect_review_items(
            r.entry_type, party_match_type, party_score,
            bank_res.match_type, bank_res.score,
            category_matched, False, base=review)

        # A date, not a datetime: _draft_payload isoformats this straight into
        # the draft, and a datetime would put "2025-08-01T00:00:00" where every
        # other path puts "2025-08-01".
        trans_date = datetime.strptime(r.date, '%Y-%m-%d').date()
        # The source line is kept verbatim: it is what the operator checks the
        # draft against, and the only thing that ties a voucher back to the
        # statement it came from.
        source = f"{r.date}  {r.description}  {r.amount:,.2f}"

        payload = self._draft_payload(
            msg=source, entry_type=r.entry_type, amount=r.amount,
            trans_date=trans_date, party_code=party_code,
            party_display=party_display or '(no name on the line)',
            party_type=party_type, party_match_type=party_match_type,
            party_score=party_score, bank_res=bank_res, cat_res=cat_res,
            category_matched=category_matched, cheque_no=None,
            description=r.description[:200], review_items=review,
            extraction_source='statement', cat_note=cat_note)

        d = payload['draft']
        d['draft_id'] = f"stmt-{index}"
        d['statement_line'] = r.line_no
        d['statement_section'] = r.section
        d['statement_text'] = r.description
        d['statement_file'] = filename
        d['duplicate_of'] = dup['voucher_number'] if dup else None
        # An unnamed party is a field to fill, not a name to post. Clearing it
        # here stops the commit creating a profile called "(no name on the
        # line)" if someone saves the row without looking.
        if not party_name:
            d['party_name'] = None
            d['party_is_new'] = False
        return d

    def _statement_reply(self, drafts: List[Dict], skipped: List[Dict], *,
                         bank_res, parsed: Dict, filename: str) -> Dict:
        crv = [d for d in drafts if d['entry_type'] == 'CRV']
        cpv = [d for d in drafts if d['entry_type'] == 'CPV']
        money_in = sum(d['amount'] for d in crv)
        money_out = sum(d['amount'] for d in cpv)
        needs = [d for d in drafts if not d.get('category_acc_code')
                 or not d.get('party_code') and not d.get('party_name')]
        dups = [d for d in drafts if d.get('duplicate_of')]
        new_parties = sorted({d['party_name'] for d in drafts
                              if d.get('party_is_new') and d.get('party_name')})

        dates = sorted({d['transaction_date'] for d in drafts})
        span = (f"{_pretty_date(dates[0])}"
                + (f" to {_pretty_date(dates[-1])}" if dates[-1] != dates[0] else ''))

        lines = [
            f"{len(drafts)} transactions read from {filename or 'the statement'} "
            f"({span}), through {bank_res.qualified}. "
            f"Nothing has been written.",
            "",
            f"  {len(crv)} received   ${money_in:,.2f}",
            f"  {len(cpv)} paid       ${money_out:,.2f}",
        ]
        if new_parties:
            lines += ["", f"{len(new_parties)} name{'' if len(new_parties) == 1 else 's'} "
                          f"aren't in your ledger yet; each one gets a profile "
                          f"when you save that row."]
        if needs:
            lines += ["", f"{len(needs)} row{'' if len(needs) == 1 else 's'} still "
                          f"need an account picking."]
        if dups:
            lines += ["", f"{len(dups)} row{'' if len(dups) == 1 else 's'} match a "
                          f"voucher already posted on the same day - flagged, not skipped."]
        if skipped:
            lines += ["", f"{len(skipped)} line{'' if len(skipped) == 1 else 's'} "
                          f"couldn't be read; they're listed at the end."]
        lines += ["", "They come one at a time. Read each, correct what needs "
                      "correcting, and save it - or skip it."]
        text = strip_emojis("\n".join(lines))

        return {
            'status': 'draft',
            'action': 'draft_queue',
            'message': text,
            'analysis': text,
            'confidence': 'medium',
            'drafts': drafts,
            'skipped': skipped,
            'draft': drafts[0],
            'card': {
                'kind': 'statement_summary',
                'filename': filename,
                'bank_account': bank_res.qualified,
                'bank_acc_code': bank_res.code,
                'account_hint': parsed.get('account_hint'),
                'layout': parsed.get('layout'),
                'date_from': dates[0], 'date_to': dates[-1],
                'count': len(drafts),
                'crv_count': len(crv), 'cpv_count': len(cpv),
                'money_in': money_in, 'money_out': money_out,
                'needs_account': len(needs),
                'duplicates': len(dups),
                'new_parties': new_parties,
                'skipped': skipped,
            },
        }

    def _draft_payload(self, *, msg, entry_type, amount, trans_date, party_code,
                       party_display, party_type, party_match_type, party_score,
                       bank_res, cat_res, category_matched, cheque_no,
                       description, review_items, extraction_source,
                       bank_note=None, cat_note=None,
                       suggestions=None) -> Dict[str, Any]:
        """
        A resolved but UNWRITTEN voucher, for the operator to confirm.

        Every field carries the code it resolved to, not just the name, so the
        client can post the draft back by code and the thing that gets written
        is the thing that was shown. Either account leg may be None - that is a
        field the operator still has to pick, not a failure.
        """
        party_label = "Customer" if entry_type == 'CRV' else "Vendor"
        bank_label = bank_res.qualified if bank_res else '(choose an account)'
        cat_label = cat_res.qualified if cat_res else '(choose an account)'
        direction = (f"Dr {bank_label}  /  Cr {cat_label}"
                     if entry_type == 'CRV' else
                     f"Dr {cat_label}  /  Cr {bank_label}")

        draft = {
            'kind': 'voucher',
            'entry_type': entry_type,
            # The three-letter code is what the ledger, the reports and the
            # voucher number all use; the long name only ever appeared here.
            'doc_label': 'CRV' if entry_type == 'CRV' else 'CPV',
            'party_label': party_label,
            'amount': amount,
            'transaction_date': trans_date.isoformat(),
            'party_code': party_code,
            'party_name': party_display,
            'party_type': party_type,
            'party_is_new': party_code is None,
            'party_match_type': party_match_type,
            'bank_acc_code': bank_res.code if bank_res else None,
            'bank_account': bank_res.qualified if bank_res else None,
            'bank_match_type': bank_res.match_type if bank_res else None,
            'bank_matched': bank_res is not None,
            # Long for the conversation, short for the field label. The panel
            # is a form, not a place to read a paragraph.
            'bank_note': bank_note,
            'bank_note_short': _problem_short(bank_note) if bank_note else None,
            'category_acc_code': cat_res.code if cat_res else None,
            'category_account': cat_res.qualified if cat_res else None,
            'category_match_type': cat_res.match_type if cat_res else None,
            'category_matched': category_matched,
            'category_note': cat_note,
            'category_note_short': _problem_short(cat_note) if cat_note else None,
            'cheque_no': cheque_no,
            'description': description,
            'journal_preview': direction,
            'source_message': msg,
            'extraction_source': extraction_source,
            'review_items': review_items,
            'suggestions': list(suggestions or []),
        }

        # The panel beside this already shows every field. Repeating them here
        # and putting the EXPLANATION in the panel had it exactly backwards:
        # the form was carrying paragraphs while the conversation carried a
        # table. So the reply says what still stands in the way and why, and
        # the panel is left to be a form.
        cash_word = 'bank/cash account' if entry_type == 'CPV' else 'account the money went into'
        missing = [(cash_word, bank_note)] if bank_note else []
        if cat_note:
            missing.append(('income account' if entry_type == 'CRV'
                            else 'expense account', cat_note))

        if missing:
            what = ' and the '.join(m[0] for m in missing)
            lines = [f"Almost there - I have ${amount:,.2f} "
                     f"{'from' if entry_type == 'CRV' else 'to'} {party_display} "
                     f"on {trans_date.strftime('%m/%d/%Y')}, but not the {what}. "
                     f"Nothing has been written."]
            for label, note in missing:
                # The heading names the field; the sentence under it says why.
                # Repeating the short form here would say the same thing twice.
                lines += ["", f"{label.capitalize()}", f"  {note}"]
            lines += ["", "Pick them in the panel, or send one of the lines below."]
        else:
            lines = [f"Ready to post - ${amount:,.2f} "
                     f"{'from' if entry_type == 'CRV' else 'to'} {party_display}, "
                     f"{direction}. Nothing has been written yet.",
                     "", "Read it through in the panel, then post."]
            if review_items:
                lines += ["", "Two things worth checking first:" if len(review_items) > 1
                          else "One thing worth checking first:"]
                lines += [f"  - {i}" for i in review_items]
        text = strip_emojis("\n".join(lines))

        return {
            'status': 'draft',
            'action': 'draft',
            'message': text,
            'analysis': text,
            'confidence': 'medium' if review_items else 'high',
            'draft': draft,
            'review_items': review_items,
            'review_note': ', '.join(review_items) if review_items else None,
            'extraction_source': extraction_source,
            'suggestions': list(suggestions or []),
            'card': {**draft, 'kind': 'draft', 'draft_kind': draft['kind']},
        }

    # ----------------------------------------------------------------------
    # Asking the model is the DEFAULT, not something each branch opts into.
    #
    # It was the other way round, and it kept going wrong the same way: a new
    # refusal path would ship with no hook, and nothing announced the gap - the
    # reply looked like a considered "no" rather than a "no" from a reader that
    # never got asked. Three separate paths had to be patched by hand before
    # the pattern was obvious.
    #
    # So the deterministic pipeline (_process_once) now knows nothing about the
    # model, and this wrapper decides afterwards, from the RESULT alone,
    # whether the run is worth a second attempt. A path that refuses for a
    # reason wording cannot fix marks itself final; everything else is
    # retryable without having to know this exists.
    # ----------------------------------------------------------------------
    @staticmethod
    def _worth_a_rewrite(out: Dict) -> bool:
        """Is this outcome a 'couldn't read it', or a real answer?"""
        if not isinstance(out, dict) or out.get('_final'):
            return False
        if out.get('status') == 'error':
            return True
        # A voucher draft missing a leg is a failure that doesn't look like
        # one: the message parsed, it just parsed into nonsense.
        d = out.get('draft') if isinstance(out.get('draft'), dict) else None
        if d and d.get('kind') == 'voucher':
            return not (d.get('bank_acc_code') and d.get('category_acc_code'))
        # An edit that opened untouched because no field name was recognised.
        return bool(out.pop('_field_error', False))

    @staticmethod
    def _rewrite_is_better(alt: Dict, first: Dict) -> bool:
        """Keep a rewrite only when it resolved strictly more than we had."""
        if not isinstance(alt, dict) or alt.get('status') == 'error':
            return False
        if first.get('status') == 'error':
            return True
        return _draft_quality(alt) > _draft_quality(first)

    async def process_message(self, message: str, session_id: str,
                              mode: Optional[str] = None,
                              preview: bool = False,
                              _rewritten: bool = False) -> Dict:
        first = await self._process_once(message, session_id, mode, preview,
                                         _rewritten)
        if _rewritten or not self._worth_a_rewrite(first) or not _llm_available():
            first.pop('_final', None)
            return first

        rewritten = _llm_canonical_command(message, mode)
        if not rewritten:
            first.pop('_final', None)
            return first

        # A destructive command is never RUN from a reading of a sentence. If
        # the model thinks that is what was meant, it comes back as a line to
        # send, so the decision stays with the person.
        if _VOID_CMD_RE.match(rewritten) and not _VOID_CMD_RE.match(message or ''):
            t = ("I think you meant to cancel a voucher. I won't do that from "
                 "a guess, so here it is to send if it's right:")
            return {'status': 'error', 'message': t, 'analysis': t,
                    'confidence': 'low', 'suggestions': [rewritten]}

        # An edit may only be restated, never redirected: if the original named
        # a voucher, the rewrite has to name the same one.
        m0 = _UPDATE_CMD_RE.match(message or '') or _SHOW_CMD_RE.match(message or '')
        if m0:
            m1 = _UPDATE_CMD_RE.match(rewritten) or _SHOW_CMD_RE.match(rewritten)
            if not m1 or m1.group('id') != m0.group('id'):
                print(f"NOTE: LLM rewrite changed the voucher, discarded: "
                      f"{rewritten!r}")
                first.pop('_final', None)
                return first

        print(f"NOTE: LLM rewrote {(message or '')[:60]!r} -> {rewritten!r}")
        alt = await self._process_once(rewritten, session_id, mode, preview,
                                       _rewritten=True)
        if not self._rewrite_is_better(alt, first):
            first.pop('_final', None)
            return first

        items = list(alt.get('review_items') or [])
        items.append(f'wording (I read your line as "{rewritten}")')
        alt['review_items'] = items
        alt['rewritten_from'] = message
        alt['rewritten_to'] = rewritten
        if isinstance(alt.get('draft'), dict):
            alt['draft']['review_items'] = items
            alt['draft']['source_message'] = message
        alt.pop('_final', None)
        return alt

    async def _process_once(self, message: str, session_id: str,
                            mode: Optional[str] = None,
                            preview: bool = False,
                            _rewritten: bool = False) -> Dict:
        """One deterministic pass. Knows nothing about the language model."""
        conn = self.get_session_db(session_id)
        msg = (message or "").strip()

        # ---- v6 commands, checked before anything else --------------------
        if msg:
            mv = _VOID_CMD_RE.match(msg)
            if mv:
                return await self.handle_void_voucher(conn, mv.group('id'),
                                                      preview)

            # A profile edit and a voucher edit share the verb; the voucher
            # one is recognised by its id, so it is tried first and this only
            # sees what it didn't take.
            mep = _parse_edit_profile_command(msg)
            if mep and not _UPDATE_CMD_RE.match(msg):
                return await self.handle_edit_profile(conn, mep, preview, msg)

            # A whole day, before the single-voucher form: that one needs an
            # id, and a date is not an id, so this would otherwise fall all
            # the way through to the voucher parser and be reported as a
            # posting with no amount.
            mb = _parse_bulk_update_command(msg)
            if mb:
                return await self.handle_bulk_update(
                    conn, mb['date'], mb['fields'], mb.get('unparsed_tail'),
                    preview, msg)

            mu = _UPDATE_CMD_RE.match(msg)
            if mu:
                parsed = _parse_update_command(msg)
                return await self.handle_update_voucher(
                    conn, parsed['at_id'], parsed['fields'],
                    parsed.get('unparsed_tail'), preview, msg)

            # A bare 10-20 digit number is a voucher id in any mode - nothing
            # else in this grammar looks like one, and requiring the right mode
            # only meant "give me the id" silently did nothing.
            ms = _SHOW_CMD_RE.match(msg) or _BARE_ID_RE.match(msg)
            if ms:
                return await self.handle_show_voucher(conn, ms.group('id'), preview)

            # A date names a day's work: list it and let them pick.
            md = _parse_voucher_date_command(msg)
            if md:
                return await self.handle_voucher_list(conn, md['date'])

            mc = _parse_chart_command(msg)
            if mc:
                return await self.handle_show_chart(conn, mc['nature'])

            mce = _parse_chart_edit_command(msg)
            if mce:
                return await self.handle_chart_edit(conn, mce, preview, msg)

            # Checked before the profile parser: "add ... account ..." is a
            # chart entry, while "add vendor ..." is a party.
            ma = _parse_add_account_command(msg)
            if ma:
                ma['source_message'] = msg
                return await self.handle_add_account(conn, ma, preview)

            mp = _parse_profile_command(msg)
            if mp:
                fields = dict(mp['fields'])
                fields['_source_message'] = msg
                return await self.handle_create_profile(
                    conn, mp['p_type'], mp['name'], fields, mp.get('unparsed'), preview)

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

        extracted = await self.extract_transaction_info(msg, session_id, mode)

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
            hint = _diagnose_unparsed(msg)
            # The raw exception is for the log, not for the person - they can't
            # act on a 404 from a model provider. All they need to know is that
            # the flexible reader is off, so plain phrasing is required.
            if extracted.get('_llm_error'):
                print(f"NOTE: LLM unavailable while parsing "
                      f"{msg[:80]!r}: {extracted['_llm_error']}")
                hint += ("\n\nHeads-up: my language-model reader is offline "
                         "right now, so I'm only reading the plain phrasing "
                         "above. Everything else - editing, profiles, the "
                         "chart of accounts - works normally.")
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

        # `not amount` was catching 0.0 as well as None, so a line that DID
        # name an amount - "Paid $0.00 to ..." - was reported as "missing the
        # amount". It wasn't missing, it was zero, and the dedicated message
        # for that a few lines below could never be reached.
        if not entry_type or amount is None or not party_name:
            missing = []
            if not entry_type:
                missing.append('whether this is money received or paid')
            if amount is None:
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
                    t = (f"{trans_date:%m/%d/%Y} is outside the financial year "
                         f"this company currently has open, so I haven't posted "
                         f"it. Change the date to one inside the open year, or "
                         f"ask whoever administers LockInLedger to reopen that "
                         f"period.")
                    return {'status': 'error', 'message': t, 'analysis': t,
                            'confidence': 'low'}
                review_items.append('date (outside the open fiscal year)')

            # ---- Party ----
            # On a preview this must not create the party - a discarded draft
            # would otherwise leave an acc_party row behind.
            # Look the party up but do NOT create it yet, in either mode. A
            # party created here and an account leg that fails a moment later
            # leaves an orphan acc_party row behind - which is exactly what
            # three failed attempts at the same line used to do. Creation moves
            # to just before the insert, once everything else has held up.
            party_code, party_display, party_match_type, party_score, party_type = \
                resolve_party(conn, party_name, entry_type, create_missing=False)

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
            #
            # An account I can't work out is not a dead end when the operator is
            # about to see the entry anyway: the review panel exists exactly so
            # they can pick it themselves. So under preview an unresolved leg
            # becomes an EMPTY field plus a note, and the panel refuses to post
            # until it is filled. Only a direct post (preview=false) still has
            # to refuse outright, because there is nobody to ask.
            unresolved: List[str] = []
            suggestions: List[str] = []
            bank_note: Optional[str] = None
            bank_text = extracted.get('bank_text')
            bank_res, bank_err = resolve_bank_account(
                conn, bank_text, allow_default=not bool(bank_text and bank_text.strip()))
            if not bank_res:
                t = bank_err or (f"I couldn't work out which account \"{bank_text}\" "
                                 f"means.")
                # Their own sentence, finished - so the fix is one click, not a
                # retype. Real account names, so it posts as sent.
                suggestions += [_line_with_bank(msg, n)
                                for n in _bank_samples(conn, 2)]
                if not preview:
                    return _clarify_reply(
                        msg=msg, note=t, suggestions=suggestions,
                        amount=amount, party=party_display, entry_type=entry_type,
                        date=trans_date.isoformat())
                unresolved.append('bank/cash account')
                bank_note = t

            # ---- Category leg ----
            natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
            category_hint = extracted.get('category_hint')
            cat_res, cat_err = resolve_category_account(conn, category_hint, natures)

            if cat_res is None and cat_err is None:
                # No accounts of that nature exist at all - that one IS a dead
                # end, because there is nothing to pick in the panel either.
                kind = 'revenue' if entry_type == 'CRV' else 'expense'
                t = (f"There are no {kind} accounts in this company's chart yet, so "
                     f"there is nothing to post the {'income' if entry_type == 'CRV' else 'expense'} "
                     f"side of this entry to.\n\nAdd one first — for example:\n"
                     f"  add {kind} account "
                     f"{'Consulting Income' if entry_type == 'CRV' else 'Office Supplies'}")
                return {'status': 'error', 'message': t, 'analysis': t,
                        'confidence': 'low', '_final': True}

            category_matched = cat_res is not None
            cat_note = cat_err if cat_res is None else None
            if cat_res is None and cat_err and not preview:
                # A named category that matches nothing must never be silently
                # rerouted somewhere else on a direct post.
                return _clarify_reply(
                    msg=msg, note=cat_err,
                    suggestions=[_line_with_category(msg, n, entry_type)
                                 for n in _sample_accounts(conn, natures, 2)],
                    amount=amount, party=party_display, entry_type=entry_type,
                    bank=bank_res.qualified if bank_res else None,
                    date=trans_date.isoformat())
            if not cat_res and not cat_err:
                cat_res, _ = resolve_category_account(
                    conn, f"{party_name} {description}", natures)
                category_matched = cat_res is not None
                if not cat_res:
                    cat_res, default_err = _default_category_resolution(
                        conn, entry_type, natures)
                    category_matched = False
                    if not cat_res:
                        t = default_err or "Could not determine the category account."
                        cat_suggestions = [_line_with_category(msg, n, entry_type)
                                           for n in _sample_accounts(conn, natures, 2)]
                        if not preview:
                            return _clarify_reply(
                                msg=msg, note=t, suggestions=cat_suggestions,
                                amount=amount, party=party_display,
                                entry_type=entry_type,
                                bank=bank_res.qualified if bank_res else None,
                                date=trans_date.isoformat())
                        cat_note = default_err
            if not cat_res:
                unresolved.append('category account')

            # ---- Sanity: both legs must not be the same account ----
            if bank_res and cat_res and bank_res.code == cat_res.code:
                t = (f"Both sides of this entry resolved to the same account "
                     f"({bank_res.qualified}), which would be a self-cancelling "
                     f"voucher. Please name the two accounts separately.")
                return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}

            # Everything that was guessed rather than stated. Computed before
            # the post so the preview shows the operator exactly the same
            # warnings the posted voucher would carry.
            review_items = _collect_review_items(
                entry_type, party_match_type, party_score,
                bank_res.match_type if bank_res else 'none',
                bank_res.score if bank_res else 0.0,
                category_matched or cat_res is not None,
                bool(extracted.get('_direction_inferred')), base=review_items)
            # The collector already flags a leg that matched poorly; an
            # unresolved one replaces that entry rather than doubling it.
            for what in unresolved:
                plain = 'bank/cash account' if what.startswith('bank') else 'category'
                review_items = [i for i in review_items if i != plain]
                review_items.append(f'{what} - choose it here')

            # One suggestion per missing piece is a dead end when two are
            # missing: each fix lands back on the other complaint. So the
            # suggested lines fill in EVERY gap at once, and sending one posts.
            if unresolved:
                need_bank = bank_res is None
                need_cat = cat_res is None
                banks = _bank_samples(conn, 2) if need_bank else [None]
                cats = _sample_accounts(conn, natures, 2) if need_cat else [None]
                suggestions = []
                for i in range(max(len(banks), len(cats))):
                    line = msg
                    b = banks[min(i, len(banks) - 1)]
                    c = cats[min(i, len(cats) - 1)]
                    if b:
                        line = _line_with_bank(line, b)
                    if c:
                        line = _line_with_category(line, c, entry_type)
                    if line != msg and line not in suggestions:
                        suggestions.append(line)

            # ---- The rules parsed it, but badly ----------------------------
            # A message with a direction word and a number always "parses" -
            # ---- Stop here when the client asked for a draft ----
            # Nothing has been written at this point: resolve_party ran with
            # create_missing=False, and the two account legs are lookups.
            if preview:
                return self._draft_payload(
                    msg=msg, entry_type=entry_type, amount=amount,
                    trans_date=trans_date, party_code=party_code,
                    party_display=party_display, party_type=party_type,
                    party_match_type=party_match_type, party_score=party_score,
                    bank_res=bank_res, cat_res=cat_res,
                    category_matched=category_matched, cheque_no=cheque_no,
                    description=description, review_items=review_items,
                    extraction_source=extracted.get('_source'),
                    bank_note=bank_note, cat_note=cat_note,
                    suggestions=suggestions)

            # ---- Post ----
            # Everything has resolved; now the party may safely be created.
            if party_code is None:
                p_type = P_TYPE_CUSTOMER if entry_type == 'CRV' else P_TYPE_VENDOR
                party_code, party_display = create_party(conn, party_name, p_type)
                party_type, party_match_type, party_score = p_type, 'new', 0.0

            new_id = insert_voucher(conn, entry_type, party_code, bank_res, cat_res,
                                    amount, trans_date, description, cheque_no)

            voucher_number = f"{entry_type}-{new_id}"

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

            return _voucher_success_payload(
                new_id=new_id, entry_type=entry_type, amount=amount,
                party_code=party_code, party_display=party_display,
                party_type=party_type, party_match_type=party_match_type,
                party_score=party_score,
                bank_code=bank_res.code, bank_qualified=bank_res.qualified,
                bank_level=bank_res.level,
                cat_code=cat_res.code, cat_qualified=cat_res.qualified,
                cat_level=cat_res.level,
                trans_date=trans_date, cheque_no=cheque_no,
                description=description, review_items=review_items,
                category_matched=category_matched,
                extraction_source=extracted.get('_source'))

        except ValueError as e:
            t = str(e)
            return {'status': 'error', 'message': t, 'analysis': t, 'confidence': 'low'}
        except Exception as e:
            t = _internal_error_message(e, "posting that voucher")
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
# v6 - CHART OF ACCOUNTS - ADDING A NEW MAIN OR SUB
#
# Mirrors the host PHP admin's "Add chart of accounts" button. There are two
# tables per level: a SHARED one (account_main / account_sub) that every
# tenant's codes live in - because the code itself is the global primary key
# - and a per-tenant *_user row that adopts it into this company's chart and
# carries the display/report flags. Both must be written, in one transaction,
# or the host UI and this bot disagree about what exists.
#
# Codes are positional arithmetic, every level the same:
#       main = nature * 10000 + seq          4 -> 45016
#       sub  = main   * 10000 + seq      10004 -> 100045001
#       acc  = sub    * 10000 + seq
# Sequences below 5000 are the stock chart shipped to every tenant; everything
# this company added sits at 5000+ (45016, 35002, 100045001, 100025001). New
# accounts continue in that range so they never collide with a future stock
# account.
#
# hasSubAcc on account_main_user is the flag the host UI reads to decide
# whether a main is a heading or a ledger account. Adding a sub MUST set it,
# or the two applications disagree about the shape of the chart.
# ==========================================================================
CHART_USER_SEQ_FLOOR = int(os.getenv("CHART_USER_SEQ_FLOOR", "5000"))
# Adding a sub under a main that has already been posted to takes that main
# OUT of the postable set: it becomes a heading. Existing vouchers keep
# pointing at it, but nothing new can be posted there and the bot will stop
# resolving its name. The host UI allows this silently; here it is refused by
# default. Set ALLOW_ORPHANING_POSTED_MAIN=1 to match the PHP behaviour.
ALLOW_ORPHANING_POSTED_MAIN = os.getenv("ALLOW_ORPHANING_POSTED_MAIN", "0") == "1"

# Report-placement flags a child inherits from its parent.
_REPORT_FLAGS = ('income_statement', 'balance_sheet', 'trial_balance',
                 'general_ledger', 'reconcile_account', 'activity')


def _next_child_code(cur, parent_code: int, table: str, user_table: str, id_col: str) -> int:
    """
    parent*10000 + next sequence, floored into the tenant-added range.

    MAX() is taken across BOTH the shared table (account_main/account_sub -
    the global primary keys, since two tenants must never mint the same code)
    AND the per-tenant *_user table. In principle the two are always in sync
    because add_chart_account() writes both together in one transaction - but
    real tenant data can predate that invariant (seeded/imported some other
    way), leaving a code present in *_user with no matching row in the shared
    table. If we only checked the shared table in that case we'd re-mint an
    already-used code and collide on the *_user table's primary key - which
    is exactly what happened with 100045001 ("Bank of Amercia 9523"): it
    existed in account_sub_user but not in account_sub, so the old MAX()
    query never saw it. Checking both tables makes this self-healing
    regardless of which side drifted.
    """
    lo, hi = parent_code * 10000, parent_code * 10000 + 9999
    cur.execute(
        f"SELECT IFNULL(MAX(id), 0) AS mx FROM ("
        f"  SELECT `{id_col}` AS id FROM `{table}` WHERE `{id_col}` BETWEEN %s AND %s"
        f"  UNION ALL"
        f"  SELECT `{id_col}` AS id FROM `{user_table}` WHERE `{id_col}` BETWEEN %s AND %s"
        f") t", (lo, hi, lo, hi))
    row = cur.fetchone()
    # Works whether `cur` is a dictionary cursor (real driver, keyed by the
    # AS alias) or a plain tuple cursor (older callers / tests) - never
    # assume positional access on a dict row.
    raw = row['mx'] if isinstance(row, dict) else row[0]
    current = int(raw or 0)
    return max(lo + CHART_USER_SEQ_FLOOR, current + 1)


def _main_has_transactions(conn, main_code: str) -> int:
    """Vouchers posted directly to this main (i.e. while it was postable)."""
    rows = _fetch_all(
        conn,
        "SELECT COUNT(*) AS n FROM acc_trans_d "
        "WHERE at_acc_code = %s AND at_system_id = %s",
        (main_code, SYSTEM_ID), "main_has_transactions")
    return int(rows[0]['n']) if rows else 0


def add_chart_account(conn, level: str, name: str, parent_code: str) -> Dict[str, Any]:
    """
    level 'main'  -> parent_code is a 1-digit nature
    level 'sub'   -> parent_code is a 5-digit main
    """
    name = (name or '').strip()
    if not name:
        raise ValueError("Give the account a name.")
    if len(name) > 250:
        raise ValueError("That name is too long for the chart (250 characters max).")
    parent_code = str(parent_code)

    # Reject a duplicate name within the same parent, in THIS tenant's chart.
    tree = build_chart_tree(conn)
    target = normalize_name(name)
    for n in tree:
        for m in n['mains']:
            if level == 'main' and n['nature'] == parent_code and \
                    normalize_name(m['name']) == target:
                raise ValueError(
                    f"\"{m['name']}\" already exists under {n['label']} as {m['code']}.")
            if level == 'sub' and m['code'] == parent_code:
                for s in m['subs']:
                    if normalize_name(s['name']) == target:
                        raise ValueError(
                            f"\"{s['name']}\" already exists under {m['name']} "
                            f"as {s['code']}.")

    cur = conn.cursor(dictionary=True)
    try:
        if level == 'main':
            nature = parent_code
            cur.execute("SELECT * FROM account_nature WHERE acc_nature_id = %s", (nature,))
            par = cur.fetchone()
            if not par:
                raise ValueError(f"Nature {nature} does not exist.")
            cur.execute("SELECT 1 FROM account_nature_user WHERE acc_nature_id = %s "
                        "AND acc_nature_system_id = %s", (nature, SYSTEM_ID))
            if not cur.fetchone():
                raise ValueError(f"{par['acc_nature_desc']} is not part of this "
                                 f"company's chart.")

            new_id = _next_child_code(cur, int(nature), 'account_main', 'account_main_user', 'acc_main_id')
            flags = {k: par.get(k) for k in _REPORT_FLAGS}

            cur.execute(
                "INSERT INTO account_main (acc_main_id, acc_main_desc, acc_main_label, "
                " acc_nature_id, acc_main_status, sort_order, income_statement, "
                " balance_sheet, trial_balance, general_ledger, reconcile_account, activity) "
                "VALUES (%s,%s,NULL,%s,1,%s,%s,%s,%s,%s,%s,%s)",
                (new_id, name, nature, 0, flags['income_statement'],
                 flags['balance_sheet'], flags['trial_balance'],
                 flags['general_ledger'], flags['reconcile_account'], flags['activity']))
            cur.execute(
                "INSERT INTO account_main_user (acc_main_system_id, acc_main_id, "
                " acc_nature_id, acc_main_status, acc_main_sort, hasSubAcc, "
                " acc_main_activity, acc_main_display_status, show_balance) "
                "VALUES (%s,%s,%s,1,%s,NULL,%s,1,1)",
                (SYSTEM_ID, new_id, nature, 0, flags['activity'] or 0))
            parent_label = par['acc_nature_desc']

        elif level == 'sub':
            cur.execute("SELECT * FROM account_main WHERE acc_main_id = %s", (parent_code,))
            par = cur.fetchone()
            if not par:
                raise ValueError(f"Account {parent_code} does not exist.")
            cur.execute("SELECT 1 FROM account_main_user WHERE acc_main_id = %s "
                        "AND acc_main_system_id = %s", (parent_code, SYSTEM_ID))
            if not cur.fetchone():
                raise ValueError(f"\"{par['acc_main_desc']}\" is not part of this "
                                 f"company's chart.")

            new_id = _next_child_code(cur, int(parent_code), 'account_sub', 'account_sub_user', 'acc_sub_id')
            flags = {k: par.get(k) for k in _REPORT_FLAGS}

            cur.execute(
                "INSERT INTO account_sub (acc_sub_id, acc_sub_desc, acc_sub_label, "
                " acc_main_id, acc_sub_status, sort_order, income_statement, "
                " balance_sheet, trial_balance, general_ledger, reconcile_account, activity) "
                "VALUES (%s,%s,NULL,%s,1,%s,%s,%s,%s,%s,%s,%s)",
                (new_id, name, parent_code, 0, flags['income_statement'],
                 flags['balance_sheet'], flags['trial_balance'],
                 flags['general_ledger'], flags['reconcile_account'], flags['activity']))
            cur.execute(
                "INSERT INTO account_sub_user (acc_sub_system_id, acc_sub_id, acc_main_id, "
                " acc_sub_sort, acc_sub_status, hasAcc, acc_sub_activity, "
                " show_balance, acc_sub_display_status) "
                "VALUES (%s,%s,%s,%s,1,NULL,%s,1,1)",
                (SYSTEM_ID, new_id, parent_code, 0, flags['activity'] or 0))
            # The main is now a heading. Without this the host UI still shows it
            # as a postable account.
            cur.execute(
                "UPDATE account_main_user SET hasSubAcc = 1 "
                "WHERE acc_main_id = %s AND acc_main_system_id = %s",
                (parent_code, SYSTEM_ID))
            parent_label = par['acc_main_desc']
        else:
            raise ValueError("level must be 'main' or 'sub'.")

        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()

    _chart_cache["at"] = 0.0            # next lookup sees the new account
    print(f"[chart] created {level} {new_id} - {name} (under {parent_label})")
    return {"created": True, "level": level, "code": str(new_id), "name": name,
            "parent_code": parent_code, "parent_name": parent_label}


# ==========================================================================
# v6 - CHART OF ACCOUNTS - RENAMING AND RETIRING
#
# Three things people mean by "edit an account", and they are not equally
# safe:
#
#   RENAME      The name lives ONLY in the shared, cross-tenant tables
#               (account_main.acc_main_desc / account_sub.acc_sub_desc) - the
#               per-tenant *_user rows carry flags and sort order, no
#               description. So renaming an account renames it for every
#               company that adopted that code. Allowed only when this tenant
#               is the sole user of the code, which is true for anything it
#               created (sequence >= CHART_USER_SEQ_FLOOR) and generally false
#               for the stock chart.
#
#   DEACTIVATE  Sets this tenant's *_user status/display flags to 0. The
#               account leaves the pickers and stops resolving for new
#               vouchers; posted history is untouched and it can be turned
#               back on. This is what people usually mean by "delete".
#
#   MOVE        Refused. The code IS the hierarchy (parent*10000+seq) and it
#               is stamped on every posted leg, on party defaults and read
#               back by reports. Moving means minting a new code and
#               repointing history: a migration, not an edit.
# ==========================================================================
def _chart_tables(code: str) -> Tuple[str, str, str, str]:
    """(shared table, user table, id column, description column) for a code."""
    if len(str(code)) == 5:
        return 'account_main', 'account_main_user', 'acc_main_id', 'acc_main_desc'
    if len(str(code)) == 9:
        return 'account_sub', 'account_sub_user', 'acc_sub_id', 'acc_sub_desc'
    raise ValueError(
        "Only the accounts this assistant creates - headings and sub-accounts - "
        "can be renamed here. Individual 13-digit accounts and the top-level "
        "natures belong to the host application.")


def _tenants_using(conn, code: str) -> int:
    """How many companies have adopted this code."""
    _shared, user_tbl, id_col, _desc = _chart_tables(code)
    sys_col = 'acc_main_system_id' if id_col == 'acc_main_id' else 'acc_sub_system_id'
    rows = _fetch_all(conn,
                      f"SELECT COUNT(DISTINCT `{sys_col}`) AS n FROM `{user_tbl}` "
                      f"WHERE `{id_col}` = %s", (code,), "tenants_using")
    return int(rows[0]['n']) if rows else 0


def rename_chart_account(conn, code: str, new_name: str) -> Dict[str, Any]:
    code = str(code)
    new_name = (new_name or '').strip()
    if not new_name:
        raise ValueError("Give the account its new name.")
    if len(new_name) > 250:
        raise ValueError("That name is too long for the chart (250 characters max).")

    row = find_account(conn, code)
    tree = build_chart_tree(conn)
    label = row['desc'] if row else next(
        (m['name'] for n in tree for m in n['mains'] if m['code'] == code), code)
    shared, user_tbl, id_col, desc_col = _chart_tables(code)

    others = _tenants_using(conn, code)
    if others > 1:
        raise ValueError(
            f"\"{label}\" is part of the standard chart that {others} companies "
            f"share, and its name is stored once for all of them — renaming it "
            f"here would rename it for every one. Add your own account instead:\n"
            f"  add account {new_name} under <heading>")

    # A rename onto a sibling's name is the same collision the create path refuses.
    for n in tree:
        for m in n['mains']:
            if m['code'] == code:
                siblings = [x['name'] for x in n['mains'] if x['code'] != code]
            elif any(sub['code'] == code for sub in m['subs']):
                siblings = [x['name'] for x in m['subs'] if x['code'] != code]
            else:
                continue
            if any(normalize_name(x) == normalize_name(new_name) for x in siblings):
                raise ValueError(
                    f"There is already an account called \"{new_name}\" alongside "
                    f"this one. Two accounts with the same name in the same place "
                    f"is how a report ends up with the total split across both.")

    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE `{shared}` SET `{desc_col}` = %s WHERE `{id_col}` = %s",
                    (new_name, code))
        if cur.rowcount == 0:
            raise ValueError(
                f"Account {code} isn't in the shared chart, so there is no name "
                f"to change. It may have been created outside this assistant.")
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()
    _chart_cache["at"] = 0.0
    print(f"[chart] renamed {code}: {label!r} -> {new_name!r}")
    return {"code": code, "old_name": label, "name": new_name,
            "message": f"Renamed {code} — \"{label}\" is now \"{new_name}\"."}


def set_chart_account_active(conn, code: str, active: bool) -> Dict[str, Any]:
    """
    Take an account out of this company's chart, or put it back. Nothing is
    deleted: posted vouchers keep pointing at it and every report still adds
    it up. It simply stops being offered and stops resolving by name.
    """
    code = str(code)
    row = find_account(conn, code)
    tree = build_chart_tree(conn)
    label = row['desc'] if row else next(
        (m['name'] for n in tree for m in n['mains'] if m['code'] == code), code)
    _shared, user_tbl, id_col, _desc = _chart_tables(code)
    sys_col = 'acc_main_system_id' if id_col == 'acc_main_id' else 'acc_sub_system_id'
    status_col = 'acc_main_status' if id_col == 'acc_main_id' else 'acc_sub_status'
    display_col = ('acc_main_display_status' if id_col == 'acc_main_id'
                   else 'acc_sub_display_status')

    if not active:
        n_txn = _main_has_transactions(conn, code)
        kids = next((m['sub_count'] for n in tree for m in n['mains']
                     if m['code'] == code), 0)
        if kids:
            raise ValueError(
                f"\"{label}\" is a heading with {kids} account"
                f"{'' if kids == 1 else 's'} under it. Retire those first, or "
                f"retire them instead — hiding the heading would hide them too.")
        if n_txn:
            print(f"[chart] {code} has {n_txn} posted transactions; deactivating "
                  f"hides it from new entries only.")

    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE `{user_tbl}` SET `{status_col}` = %s, `{display_col}` = %s "
            f"WHERE `{id_col}` = %s AND `{sys_col}` = %s",
            (1 if active else 0, 1 if active else 0, code, SYSTEM_ID))
        if cur.rowcount == 0:
            raise ValueError(f"\"{label}\" is not part of this company's chart.")
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()
    _chart_cache["at"] = 0.0
    verb = 'back in' if active else 'out of'
    print(f"[chart] {'activated' if active else 'deactivated'} {code} - {label}")
    return {"code": code, "name": label, "active": active,
            "message": (f"\"{label}\" is {verb} this company's chart. "
                        + ("It can be posted to again."
                           if active else
                           "Posted vouchers keep it; new ones can't use it."))}


_RENAME_ACCOUNT_RE = re.compile(
    r'^\s*(?:rename|relabel)\s+(?:the\s+)?(?:gl\s+|ledger\s+|chart\s+)?'
    r'account\s+(?P<old>.+?)\s+to\s+(?P<new>.+?)\s*$', re.IGNORECASE | re.DOTALL)
_RETIRE_ACCOUNT_RE = re.compile(
    r'^\s*(?P<verb>deactivate|disable|retire|hide|remove|archive|activate|enable|restore)'
    r'\s+(?:the\s+)?(?:gl\s+|ledger\s+|chart\s+)?account\s+(?P<name>.+?)\s*$',
    re.IGNORECASE | re.DOTALL)


def _clean_account_word(raw: str) -> str:
    return (raw or '').strip().strip('."\u2019\'' + " ")


def _parse_chart_edit_command(msg: str) -> Optional[Dict[str, Any]]:
    m = _RENAME_ACCOUNT_RE.match(msg or '')
    if m:
        return {"op": "rename", "name": _clean_account_word(m.group('old')),
                "new_name": _clean_account_word(m.group('new'))}
    m = _RETIRE_ACCOUNT_RE.match(msg or '')
    if m:
        verb = m.group('verb').lower()
        return {"op": "activate" if verb in ('activate', 'enable', 'restore')
                       else "deactivate",
                "name": _clean_account_word(m.group('name'))}
    return None


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


_PARTY_EDITABLE = ('company_name', 'person_name', 'email', 'phone', 'fax',
                   'address', 'city', 'state', 'zipcode', 'sale_tax_no',
                   'fedral_id_no', 'job_title', 'business_desc', 'other_desc',
                   'p_account', 'p_type')

# acc_party spells four of these with a capital letter. MySQL doesn't care,
# but naming them the way the schema does keeps the mapping honest.
_PARTY_COLUMN = {'email': 'Email', 'phone': 'Phone', 'fax': 'Fax',
                 'address': 'Address', 'city': 'user_city',
                 'state': 'user_state', 'zipcode': 'user_zipcode'}


def fetch_party(conn, p_code: Any) -> Dict[str, Any]:
    rows = _fetch_all(
        conn,
        "SELECT p_code, p_type, company_name, person_name, Email, Phone, Fax, "
        "       Address, user_city, user_state, user_zipcode, sale_tax_no, "
        "       fedral_id_no, job_title, business_desc, other_desc, p_account, status "
        "FROM acc_party WHERE p_code = %s AND system_id = %s LIMIT 1",
        (p_code, SYSTEM_ID), "fetch_party")
    if not rows:
        return {"found": False, "error": f"No profile {p_code} for this company."}
    r = rows[0]
    return {
        "found": True, "p_code": str(r['p_code']), "p_type": r['p_type'],
        "type_label": VALID_P_TYPES.get(r['p_type'], r['p_type']),
        "company_name": r['company_name'], "person_name": r['person_name'],
        "email": r['Email'], "phone": r['Phone'], "fax": r['Fax'],
        "address": r['Address'], "city": r['user_city'], "state": r['user_state'],
        "zipcode": r['user_zipcode'], "sale_tax_no": r['sale_tax_no'],
        "fedral_id_no": r['fedral_id_no'], "job_title": r['job_title'],
        "business_desc": r['business_desc'], "other_desc": r['other_desc'],
        "p_account": r['p_account'], "status": r['status'],
    }


def update_party(conn, p_code: Any, changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Change a profile in place. p_code never moves - it is seeded from the
    control account for its type and stamped on every voucher that names this
    party, so an "edit" that reassigns it would be a migration wearing an
    edit's clothes. That is also why p_type is refused here: a customer
    becoming a vendor belongs under a different control account entirely.
    """
    current = fetch_party(conn, p_code)
    if not current.get('found'):
        raise ValueError(current.get('error', f"Profile {p_code} not found."))

    if changes.get('p_type') and changes['p_type'] != current['p_type']:
        raise ValueError(
            f"{current['company_name']} is a {current['type_label']}, and the "
            f"kind can't be changed here — the code {p_code} is seeded from that "
            f"kind's control account and is stamped on every voucher naming this "
            f"profile. Create the other kind separately if you need it.")

    fields = {k: v for k, v in changes.items()
              if k in _PARTY_EDITABLE and k != 'p_type' and v is not None}
    if not fields:
        raise ValueError("Nothing to change — name the field, e.g. "
                         "\"phone 555-987-6543\".")

    for k in ('company_name', 'person_name'):
        if fields.get(k):
            fields[k] = title_case_name(str(fields[k]).strip())

    # Renaming onto a name that already exists as the same kind would create
    # exactly the duplicate the create path refuses to make.
    new_name = fields.get('company_name')
    if new_name and normalize_name(new_name) != normalize_name(current['company_name'] or ''):
        for other in find_party_candidates(conn, new_name,
                                           p_type=current['p_type'], limit=50):
            if str(other['p_code']) == str(p_code):
                continue
            for cand in (other.get('company_name'), other.get('person_name')):
                if cand and normalize_name(cand) == normalize_name(new_name):
                    raise ValueError(
                        f"Another {current['type_label'].lower()} is already called "
                        f"\"{cand}\". Two profiles with the same name are how a "
                        f"ledger ends up with half the history on each.")

    if fields.get('p_account'):
        if not find_account(conn, fields['p_account']):
            raise ValueError("That default account isn't in this company's chart.")

    sets, params = [], []
    for k, v in fields.items():
        sets.append(f"`{_PARTY_COLUMN.get(k, k)}` = %s")
        params.append('' if v == '' else v)
    params += [p_code, SYSTEM_ID]

    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE acc_party SET {', '.join(sets)} "
                    f"WHERE p_code = %s AND system_id = %s", tuple(params))
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        raise
    cur.close()

    out = fetch_party(conn, p_code)
    changed = ', '.join(sorted(fields))
    print(f"[profile] updated {p_code} - {changed}")
    out['status'] = 'success'
    out['created'] = False
    out['updated'] = True
    out['changed_fields'] = sorted(fields)
    out['message'] = (f"Updated {out['type_label']} {out['company_name']} "
                      f"— {changed.replace('_', ' ')}.")
    return out


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
#   * at_status must be 1.  0 = void, 2 = posted to the ledger; the PHP
#     voucher lists only render Edit when at_status == 1 (see debitMemoList()).
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
            raise ValueError(
                f"I couldn't read \"{upd.transaction_date}\" as a date. Try "
                f"06/07/2026, 7-june-2026 or 2026-06-07.")
        trans_date = datetime.strptime(iso, '%Y-%m-%d').date()
    else:
        trans_date = datetime.strptime(current['transaction_date'], '%Y-%m-%d').date()

    fiscal_warning = None
    if not within_open_year(conn, trans_date):
        if STRICT_FISCAL_YEAR:
            raise ValueError(
                f"{trans_date:%m/%d/%Y} is outside the financial year this "
                f"company currently has open, so the change wasn't saved. Pick "
                f"a date inside the open year, or have the period reopened.")
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
        return _require_postable(conn, code, what, natures=natures, entry_type=entry_type)

    bank_row = _need(upd.bank_acc_code or current['bank_acc_code'], "Bank/cash")
    cat_natures = CRV_INCOME_NATURES if entry_type == 'CRV' else CPV_EXPENSE_NATURES
    cat_row = _need(upd.category_acc_code or current['category_acc_code'],
                    "Category", cat_natures)
    if bank_row['code'] == cat_row['code']:
        raise ValueError(
            "The bank/cash account and the category account are the same, so "
            "this entry would cancel itself out. Change one of them in the "
            "review panel.")

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
            raise ValueError(
                f"Voucher {at_id} was deleted in LockInLedger while you had it "
                f"open, so there was nothing left to update.")
        if str(row[0] or '') != '1' or int(row[1] or 0) > 0:
            raise ValueError(
                "Someone else posted or reconciled this voucher while you had "
                "it open, so I stopped rather than overwrite their work. "
                f"Reopen it with \"show {at_id}\" to see where it stands now.")

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
        + (f"\nCheck #{cheque_no}" if cheque_no else "")
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
        raise ValueError(
            f"{current['voucher_number']} has been bank-reconciled, so voiding "
            f"it here would break the reconciliation. Unreconcile it in "
            f"LockInLedger first, then void it.")
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
                  entry_type: Optional[str] = None,
                  on_date: Optional[str] = None) -> List[Dict]:
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
    if on_date:
        sql += " AND DATE(m.at_date) = %s"
        params += (on_date,)
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


# --------------------------------------------------------------------------
# Two kinds of failure, two kinds of message.
#
# A ValueError from this module is a business refusal - it was written for the
# person and already says what to do. Anything else is a fault on our side
# (driver, network, a bug), and "IntegrityError 1062" helps nobody: the person
# gets a plain sentence and something they can actually do, while the real
# exception goes to the log where it belongs.
# --------------------------------------------------------------------------
def _internal_error_message(e: Exception, doing: str) -> str:
    traceback.print_exc()
    name = type(e).__name__
    if 'Integrity' in name or 'Duplicate' in name:
        detail = ("It looks like that record already exists in LockInLedger.")
    elif any(k in name for k in ('Operational', 'Interface', 'Database',
                                 'Connection', 'Timeout', 'Pool')):
        detail = ("I lost the connection to LockInLedger, so nothing was "
                  "written. Try again in a moment.")
    else:
        detail = "Nothing was written."
    return (f"Something went wrong on my side while {doing}. {detail}\n\n"
            f"If it keeps happening, pass this on to whoever maintains the "
            f"assistant: {name}.")


bot = AccountingBot()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(request: ChatRequest):
    try:
        return await bot.process_message(request.message, request.session_id,
                                         request.mode, bool(request.preview))
    except ValueError as e:
        return {"status": "error", "message": str(e), "analysis": str(e),
                "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "reading that message")
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}


@app.post("/api/commit")
async def api_commit_voucher(payload: VoucherCommit):
    """
    Post a draft the operator reviewed (the second half of preview=true).
    Accounts arrive as codes, so what is written is what was on screen.
    """
    own = payload.session_id is None
    conn = get_connection() if own else bot.get_session_db(payload.session_id)
    try:
        return commit_voucher(conn, payload)
    except ValueError as e:
        return {"status": "error", "message": str(e), "analysis": str(e),
                "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "posting that voucher")
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# Getting text out of an uploaded statement.
#
# Three readers, tried in order, because the one that is installed varies by
# machine and a missing library should degrade to a worse layout rather than
# to a failure. Layout mode matters more than it looks: the section heading a
# row sits under is the only thing that says which way the money went, and a
# reader that reflows the page loses it.
# --------------------------------------------------------------------------
STATEMENT_MAX_BYTES = int(os.getenv("STATEMENT_MAX_BYTES", str(15 * 1024 * 1024)))


def _pdf_text(data: bytes) -> Tuple[str, str]:
    """(text, which reader produced it)."""
    errors = []

    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text(layout=True) or '' for p in pdf.pages]
        text = "\n".join(pages)
        if text.strip():
            return text, 'pdfplumber'
        errors.append("pdfplumber found no text")
    except Exception as e:
        errors.append(f"pdfplumber: {e}")

    try:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as fh:
            fh.write(data)
            fh.flush()
            out = subprocess.run(['pdftotext', '-layout', fh.name, '-'],
                                 capture_output=True, timeout=60)
        text = out.stdout.decode('utf-8', 'replace')
        if text.strip():
            return text, 'pdftotext'
        errors.append("pdftotext found no text")
    except Exception as e:
        errors.append(f"pdftotext: {e}")

    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or '') for p in reader.pages)
        if text.strip():
            return text, 'pypdf'
        errors.append("pypdf found no text")
    except Exception as e:
        errors.append(f"pypdf: {e}")

    raise ValueError(
        "I couldn't get any text out of that PDF. "
        + ("It's most likely a scan or a photo rather than a statement "
           "downloaded from the bank - there are no words in the file to "
           "read, only an image of them. Download the PDF or CSV from your "
           "bank's site and upload that instead."
           if any('no text' in e for e in errors) else
           "No PDF reader is installed on the server: run "
           "`pip install pdfplumber` and restart.")
    )


def statement_text_from_upload(filename: str, data: bytes) -> Tuple[str, str]:
    name = (filename or '').lower()
    if name.endswith('.pdf') or data[:5] == b'%PDF-':
        return _pdf_text(data)
    for enc in ('utf-8', 'utf-8-sig', 'latin-1'):
        try:
            return data.decode(enc), 'text'
        except UnicodeDecodeError:
            continue
    raise ValueError("I can read a PDF, or a plain text or CSV export. "
                     "That file is neither.")


@app.post("/api/statement/preview")
async def api_statement_preview(file: UploadFile = File(...),
                                session_id: Optional[str] = Form(None),
                                bank_account: Optional[str] = Form(None)):
    """
    A statement, read into drafts. THIS ENDPOINT NEVER WRITES.

    It is the upload half of preview=true: the rows are resolved here, and
    each one is saved separately through /api/commit afterwards, by code,
    once a person has looked at it. There is deliberately no
    /api/statement/commit - a file should not be able to post sixty vouchers
    because someone clicked once.
    """
    own = session_id is None
    conn = None
    try:
        data = await file.read()
        if not data:
            raise ValueError("That file came through empty.")
        if len(data) > STATEMENT_MAX_BYTES:
            raise ValueError(
                f"That file is {len(data) / 1048576:.1f} MB, over the "
                f"{STATEMENT_MAX_BYTES / 1048576:.0f} MB limit. A statement "
                f"PDF is normally well under a megabyte - if yours is large "
                f"it is probably a scan, which has no text to read.")

        text, reader = statement_text_from_upload(file.filename or '', data)
        parsed = parse_statement_text(text)
        print(f"STATEMENT: {file.filename!r} via {reader} -> "
              f"{len(parsed['rows'])} rows, {len(parsed['unreadable'])} unreadable, "
              f"layout={parsed['layout']}, acct={parsed['account_hint']}")

        conn = get_connection() if own else bot.get_session_db(session_id)
        out = await bot.handle_statement_import(
            conn, parsed, filename=file.filename or 'statement',
            bank_text=bank_account)
        out.pop('_final', None)
        return out
    except ValueError as e:
        t = str(e)
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "reading that statement")
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    finally:
        if own and conn is not None:
            conn.close()


@app.post("/api/edit/batch")
async def api_edit_batch(payload: EditBatchRequest):
    """
    Build an edit draft for each voucher, with an optional instruction already
    resolved onto all of them. Nothing is written - the operator steps through
    the drafts and confirms each.
    """
    own = payload.session_id is None
    conn = get_connection() if own else bot.get_session_db(payload.session_id)
    try:
        # Parse the instruction once. It is the same grammar as an `update`
        # command with the id left off, so "amount 500, category Printing"
        # means the same thing in bulk as it does on one voucher.
        fields: Dict[str, str] = {}
        if (payload.instruction or '').strip():
            parsed = _parse_update_command(f"update 00000000 {payload.instruction}")
            fields = (parsed or {}).get('fields') or {}
            if not fields:
                return {"status": "error",
                        "message": f"I couldn't see a field name in "
                                   f"\"{payload.instruction}\". Name what to change - "
                                   f"amount, date, party, bank, category, check or note.",
                        "drafts": [], "errors": []}

        drafts, errors = [], []
        for at_id in payload.at_ids[:100]:
            current = fetch_voucher(conn, at_id)
            if not current.get('found'):
                errors.append({'at_id': at_id, 'error': current.get('error', 'not found')})
                continue
            if not current.get('editable'):
                errors.append({'at_id': at_id,
                               'voucher_number': current['voucher_number'],
                               'error': '; '.join(current['blockers'])})
                continue
            upd, applied, notes, err = (VoucherUpdate(), [], [], None)
            if fields:
                upd, applied, notes, err = _resolve_update_fields(
                    conn, current['entry_type'], fields)
            if err:
                errors.append({'at_id': at_id,
                               'voucher_number': current['voucher_number'], 'error': err})
                continue
            d = bot._edit_draft_payload(conn, current, upd=upd, applied=applied,
                                        notes=notes,
                                        msg=payload.instruction or f"edit {at_id}")
            drafts.append(d['draft'])

        return {"status": "success" if drafts else "error",
                "drafts": drafts, "errors": errors,
                "message": (f"{len(drafts)} voucher(s) open for editing."
                            if drafts else "Nothing here can be edited.")}
    except Exception as e:
        t = _internal_error_message(e, "opening those vouchers")
        traceback.print_exc()
        return {"status": "error", "message": t, "drafts": [], "errors": []}
    finally:
        if own:
            conn.close()


@app.post("/api/commit/edit")
async def api_commit_edit(payload: EditCommit):
    """Apply an edit the operator reviewed."""
    own = payload.session_id is None
    conn = get_connection() if own else bot.get_session_db(payload.session_id)
    try:
        if (payload.op or '').lower() == 'void':
            out = void_voucher(conn, payload.at_id)
            return bot._reply(out.get('message', f"Voided {payload.at_id}."),
                              card=bot._voucher_card(out), action='voided')
        upd = VoucherUpdate(
            amount=payload.amount, transaction_date=payload.transaction_date,
            party_code=payload.party_code, bank_acc_code=payload.bank_acc_code,
            category_acc_code=payload.category_acc_code,
            cheque_no=payload.cheque_no, description=payload.description)
        out = update_voucher(conn, payload.at_id, upd)
        return bot._reply(out.get('message', f"Updated {payload.at_id}."),
                          card=bot._voucher_card(out, out.get('review_note')),
                          action='updated')
    except ValueError as e:
        return {"status": "error", "message": str(e), "analysis": str(e),
                "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "saving that change")
        traceback.print_exc()
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    finally:
        if own:
            conn.close()


@app.post("/api/commit/account")
async def api_commit_account(payload: AccountCommit):
    """Create a chart account the operator reviewed. Parent arrives as a code."""
    own = payload.session_id is None
    conn = get_connection() if own else bot.get_session_db(payload.session_id)
    try:
        if payload.op:
            if not payload.code:
                raise ValueError("Which account? None was named.")
            if payload.op == 'rename':
                r = rename_chart_account(conn, payload.code, payload.name)
            elif payload.op in ('deactivate', 'activate'):
                r = set_chart_account_active(conn, payload.code,
                                             payload.op == 'activate')
            else:
                raise ValueError(f"Unknown account operation {payload.op!r}.")
            return bot._reply(r['message'], action='account_updated', card={
                "kind": "account", "code": r['code'], "name": r.get('name'),
                "level": account_level(r['code']), "updated": True,
                "parent_name": None, "parent_code": None,
            })

        level = (payload.level or '').strip().lower()
        if level not in ('main', 'sub'):
            raise ValueError("level must be 'main' or 'sub'.")
        parent_code = str(payload.parent_code or '').strip()
        if not parent_code:
            raise ValueError("Choose where the account goes.")

        # Resolve the parent's label from the tree so the confirmation names it
        # the same way the review panel did.
        tree = build_chart_tree(conn)
        if level == 'main':
            parent_label = NATURE_LABEL.get(parent_code, parent_code)
        else:
            main = next((m for n in tree for m in n['mains']
                         if m['code'] == parent_code), None)
            parent_label = main['name'] if main else parent_code

        r = add_chart_account(conn, level, payload.name, parent_code)
        return bot._account_created_reply(r, level, parent_code, parent_label)
    except ValueError as e:
        return {"status": "error", "message": str(e), "analysis": str(e),
                "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "adding that account")
        traceback.print_exc()
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    finally:
        if own:
            conn.close()


@app.post("/api/commit/party")
async def api_commit_party(payload: PartyCommit):
    """Create - or, with a p_code, update - a profile the operator reviewed."""
    own = payload.session_id is None
    conn = get_connection() if own else bot.get_session_db(payload.session_id)
    try:
        data = PartyCreate(**{k: v for k, v in payload.model_dump().items()
                              if k not in ('session_id', 'source_message', 'p_code')})
        if payload.p_code:
            # Only the fields the client actually sent. A bag of Nones would
            # look like "blank everything else" to anyone reading the call.
            sent = payload.model_dump(exclude_unset=True)
            out = update_party(conn, payload.p_code,
                               {k: v for k, v in sent.items()
                                if k not in ('session_id', 'source_message',
                                             'p_code', 'status')})
            return bot._reply(out['message'], action='profile', card={
                "kind": "profile", "created": False, "updated": True,
                "p_code": out['p_code'], "p_type": out['p_type'],
                "type_label": out['type_label'],
                "company_name": out['company_name'],
                "person_name": out['person_name'], "email": out['email'],
                "phone": out['phone'], "address": out['address'],
                "job_title": out['job_title'], "p_account": out['p_account'],
            })
        r = create_party_full(conn, data)
        return bot._profile_created_reply(conn, r, data, data.p_type.strip().upper(), None)
    except ValueError as e:
        return {"status": "error", "message": str(e), "analysis": str(e),
                "confidence": "low"}
    except Exception as e:
        t = _internal_error_message(e, "creating that profile")
        traceback.print_exc()
        return {"status": "error", "message": t, "analysis": t, "confidence": "low"}
    finally:
        if own:
            conn.close()


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


@app.get("/api/chart")
async def api_chart(nature: Optional[str] = None):
    """The nature -> main -> sub tree, as the host UI's Chart of Accounts page
    shows it - the read side of the "show chart" chat command."""
    conn = get_connection()
    try:
        tree = build_chart_tree(conn)
        return {"system_id": SYSTEM_ID,
                "natures": [n for n in tree if not nature or n['nature'] == str(nature)]}
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
            "version": "v6 - chat posting + voucher update + profile creation + chart of accounts"}

import audit_log
audit_log.register(app, bot)
if __name__ == "__main__":
    import uvicorn
    print(f"\n{'='*62}\n{bot.name} - AI Accounting Bot (v6)\n{'='*62}")
    print(f"System ID: {SYSTEM_ID}  |  Cr User: {USER_ID}")
    print("Accounts resolved from v_trans_accounts_m2 (postable leaves, tenant-scoped).")
    print("Nature taken from the first digit of the account code.")
    print("at_id = max(YYMM||doctype||000001, MAX(at_id)+1) per doc_type + system_id.")
    print("CRV posts Dr Bank / Cr Revenue;  CPV posts Dr Expense / Cr Bank.")
    print("v6: GET/PUT /api/voucher/{id} to edit, GET/POST /api/parties for profiles, "
          "GET /api/chart + chat commands for the chart of accounts.\n")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
