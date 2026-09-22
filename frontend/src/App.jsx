// LockInLedger Assistant — v6
// =============================================================================
// One surface: a chat. There are no forms.
//
// Everything the operator can do is typed:
//
//     Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of
//       America 9523 on 06/04/2026
//     show 260902000001
//     update 260902000001 amount 500, category Printing
//     void 260902000001
//     add vendor Handy Fix LLC, email ops@handyfix.com
//
// The `mode` selector is a hint sent alongside the message, not a form switch.
// It only disambiguates an otherwise bare line (a lone voucher id); every
// command works in every mode.
//
// Structured results arrive as `data.card` ({ kind: 'voucher' | 'profile' })
// and are rendered as record cards beneath the reply text.
// =============================================================================

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import {
  Box, Paper, Typography, TextField, IconButton, CircularProgress, Divider,
  ThemeProvider, createTheme, CssBaseline, Snackbar, Alert, Tooltip, Drawer,
  ToggleButton, ToggleButtonGroup, Button, Collapse, Autocomplete,
} from '@mui/material';
import SendIcon from '@mui/icons-material/Send';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import ArrowOutwardIcon from '@mui/icons-material/ArrowOutward';
import SouthWestIcon from '@mui/icons-material/SouthWest';
import ReceiptLongIcon from '@mui/icons-material/ReceiptLong';
import ListAltIcon from '@mui/icons-material/ListAlt';
import ManageAccountsIcon from '@mui/icons-material/ManageAccounts';
import DriveFileRenameIcon from '@mui/icons-material/DriveFileRenameOutline';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import HelpOutlineIcon from '@mui/icons-material/HelpOutlined';
import RefreshIcon from '@mui/icons-material/Refresh';
import CloseIcon from '@mui/icons-material/Close';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import CheckIcon from '@mui/icons-material/Check';
import AddIcon from '@mui/icons-material/Add';
import EditIcon from '@mui/icons-material/Edit';
import PersonAddIcon from '@mui/icons-material/PersonAdd';
import AccountTreeIcon from '@mui/icons-material/AccountTree';
import HistoryIcon from '@mui/icons-material/History';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutlined';
import LightbulbOutlinedIcon from '@mui/icons-material/LightbulbOutlined';
import AttachFileIcon from '@mui/icons-material/AttachFile';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import axios from 'axios';

// -----------------------------------------------------------------------------
// Config
// -----------------------------------------------------------------------------
// Bundler-agnostic on purpose. `process.env` does not exist in a Vite bundle and
// referencing it throws at module load, which blanks the whole app. Set the URL
// from index.html when it differs from the default:
//     <script>window.__API_BASE_URL__ = "https://ledger.internal:8000";</script>
const API_BASE_URL ="http://51.20.161.161"
// const API_BASE_URL =
//   (typeof window !== 'undefined' && window.__API_BASE_URL__) || 'http://localhost:8000';

// -----------------------------------------------------------------------------
// Design tokens — a neutral enterprise palette, one accent, no gradients.
// -----------------------------------------------------------------------------
const C = {
  bg: '#F7F8FA',
  surface: '#FFFFFF',
  raised: '#FBFCFD',
  line: '#E4E7EC',
  lineStrong: '#D0D5DD',
  ink: '#101828',
  inkMid: '#475467',
  inkMute: '#8A94A6',
  accent: '#1B3A6B',
  accentSoft: '#EEF2F8',
  ok: '#067647',
  okSoft: '#ECFDF3',
  warn: '#B54708',
  warnSoft: '#FFFAEB',
  err: '#B42318',
  errSoft: '#FEF3F2',
  violet: '#5B3FA8',
  violetSoft: '#F5F2FE',
  blue: '#1D6FB8',
  blueSoft: '#EFF6FC',
};

const MONO = '"SFMono-Regular", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
const SANS = '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: C.accent },
    background: { default: C.bg, paper: C.surface },
    text: { primary: C.ink, secondary: C.inkMid },
    divider: C.line,
  },
  typography: {
    fontFamily: SANS,
    h6: { fontSize: 15, fontWeight: 600, letterSpacing: '-0.01em' },
    body1: { fontSize: 14, lineHeight: 1.6 },
    body2: { fontSize: 13, lineHeight: 1.55 },
    caption: { fontSize: 11.5, letterSpacing: '0.02em' },
    button: { textTransform: 'none', fontWeight: 500 },
  },
  shape: { borderRadius: 6 },
  components: {
    MuiPaper: { defaultProps: { elevation: 0 } },
    MuiTooltip: {
      styleOverrides: {
        tooltip: { backgroundColor: C.ink, fontSize: 11.5, fontWeight: 400, borderRadius: 4 },
      },
    },
  },
});

// -----------------------------------------------------------------------------
// Formatting
// -----------------------------------------------------------------------------
const money = (v) =>
  Number(v ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// What identifies one draft inside a queue. An edit queue keys on the voucher
// it opened; a statement queue is rows that have no voucher yet, so the
// server stamps each with a draft_id. Without a key, saving one row marks
// every unsaved row as done and the queue closes after the first save.
const draftKey = (d) => String(d?.at_id || d?.draft_id || '');

// A statement arrives as a file, not a sentence — these are the ones worth
// trying. Anything else is refused in the browser rather than uploaded and
// rejected.
const STATEMENT_TYPES = '.pdf,.txt,.csv,.tsv';
const STATEMENT_MAX_MB = 15;

const formatDateForDisplay = (iso) => {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
  return m ? `${m[2]}/${m[3]}/${m[1]}` : String(iso);
};

const clockTime = (d) =>
  new Date(d).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });

// -----------------------------------------------------------------------------
// Primitives
// -----------------------------------------------------------------------------
const Mono = ({ children, sx, ...p }) => (
  <Box component="span" sx={{ fontFamily: MONO, fontSize: 12.5, letterSpacing: '-0.01em', ...sx }} {...p}>
    {children}
  </Box>
);

const Pill = ({ label, tone = 'neutral' }) => {
  const tones = {
    neutral: { bg: '#F2F4F7', fg: C.inkMid, bd: C.line },
    accent: { bg: C.accentSoft, fg: C.accent, bd: '#D6E0EF' },
    ok: { bg: C.okSoft, fg: C.ok, bd: '#ABEFC6' },
    warn: { bg: C.warnSoft, fg: C.warn, bd: '#FEDF89' },
    err: { bg: C.errSoft, fg: C.err, bd: '#FECDCA' },
  };
  const t = tones[tone] || tones.neutral;
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-flex', alignItems: 'center', height: 20, px: 0.75,
        borderRadius: '4px', border: `1px solid ${t.bd}`, background: t.bg, color: t.fg,
        fontSize: 11, fontWeight: 600, letterSpacing: '0.02em', whiteSpace: 'nowrap',
      }}
    >
      {label}
    </Box>
  );
};

// A definition row inside a record card.
const Row = ({ label, children, mono }) =>
  children === null || children === undefined || children === '' ? null : (
    <Box
      sx={{
        display: 'grid', gridTemplateColumns: { xs: '110px 1fr', sm: '140px 1fr' },
        gap: 1.5, py: 0.75, borderBottom: `1px solid ${C.line}`,
        '&:last-of-type': { borderBottom: 'none' },
      }}
    >
      <Typography sx={{ fontSize: 12.5, color: C.inkMute }}>{label}</Typography>
      <Box sx={{ fontSize: 13, color: C.ink, fontFamily: mono ? MONO : 'inherit', minWidth: 0, wordBreak: 'break-word' }}>
        {children}
      </Box>
    </Box>
  );

const CopyButton = ({ value, title = 'Copy' }) => (
  <Tooltip title={title}>
    <IconButton
      size="small"
      onClick={() => navigator.clipboard?.writeText(String(value))}
      sx={{ width: 22, height: 22, color: C.inkMute, '&:hover': { color: C.ink } }}
    >
      <ContentCopyIcon sx={{ fontSize: 13 }} />
    </IconButton>
  </Tooltip>
);

// -----------------------------------------------------------------------------
// Record cards
// -----------------------------------------------------------------------------
const CardShell = ({ title, right, children, tone = 'neutral', flush }) => (
  <Paper
    sx={{
      mt: flush ? 0 : 1.5, border: `1px solid ${C.line}`, borderRadius: '8px',
      background: C.surface, overflow: 'hidden',
    }}
  >
    <Box
      sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1,
        px: 1.75, py: 1.1, background: C.raised, borderBottom: `1px solid ${C.line}`,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
        <Box
          sx={{
            width: 3, height: 14, borderRadius: 2, flexShrink: 0,
            background: tone === 'err' ? C.err : tone === 'warn' ? C.warn : C.accent,
          }}
        />
        {title}
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexShrink: 0 }}>{right}</Box>
    </Box>
    <Box sx={{ px: 1.75, py: 1 }}>{children}</Box>
  </Paper>
);

// A voucher, as a document rather than a paragraph.
//
// One component serves every place a voucher appears — freshly posted, looked
// up with `show`, updated, voided — because they are the same record and
// should not look like three different things. The two shapes it is handed
// (the flat payload of a fresh post, the `card` of a lookup) are normalised
// first; a fresh post has no legs stored yet, so its journal entry is derived
// from the direction, which is the one place polarity is defined client-side.
const ENTRY_TONE = {
  CRV: { bar: '#065F46', soft: C.okSoft, edge: '#A7D8C0',
         doc: 'CRV', flow: 'Cash receipt · money in', party: 'Customer',
         category: 'Income account' },
  CPV: { bar: C.accent, soft: C.accentSoft, edge: '#D6E0EF',
         doc: 'CPV', flow: 'Cash payment · money out', party: 'Vendor',
         category: 'Expense account' },
};

const normalizeVoucher = (v) => {
  const entryType = v.entry_type === 'CRV' ? 'CRV' : 'CPV';
  const amount = Number(v.amount ?? 0);
  const bank = { code: v.bank_acc_code, name: v.bank_account };
  const cat = { code: v.category_acc_code, name: v.category_account };
  // A lookup carries its real legs; a fresh post carries none, so derive them.
  const legs = v.legs?.length
    ? v.legs
    : (bank.code && cat.code
        ? (entryType === 'CRV'
            ? [{ sno: 1, code: bank.code, account: bank.name, dc: 'D', amount },
               { sno: 2, code: cat.code, account: cat.name, dc: 'C', amount: -amount }]
            : [{ sno: 1, code: cat.code, account: cat.name, dc: 'D', amount },
               { sno: 2, code: bank.code, account: bank.name, dc: 'C', amount: -amount }])
        : []);
  const status = v.at_status === undefined || v.at_status === null ? '1' : String(v.at_status);
  return {
    entryType, amount, bank, cat, legs, status,
    atId: v.at_id,
    number: v.voucher_number,
    date: v.transaction_date,
    party: v.party_name,
    partyCode: v.party_code,
    cheque: v.cheque_no,
    description: v.description,
    statusLabel: v.status_label || { '0': 'Void', '1': 'Posted', '2': 'In ledger' }[status] || status,
    editable: v.editable !== false && status === '1',
    blockers: v.blockers || [],
    // `note` on a lookup, `review_note` on a fresh post — same meaning.
    note: v.note || v.review_note || null,
  };
};

// label + value, numbered so the eye can travel down the document
// One line of the document. `hint` is the internal account/party code: it is
// on the row for anyone who needs it (hover) but not printed, because a
// nine-digit ledger code means nothing to the person reading the voucher.
const VoucherRow = ({ n, label, value, hint, mono, last }) => (
  <Box
    title={hint ? `${label}: ${value}  (${hint})` : undefined}
    sx={{
      display: 'grid', gridTemplateColumns: '20px 1fr minmax(0, 1.4fr)',
      columnGap: 1.5, alignItems: 'baseline', px: 1.75, py: 0.85,
      borderBottom: last ? 'none' : `1px solid ${C.line}`,
    }}
  >
    <Box
      sx={{
        width: 17, height: 17, borderRadius: '50%', background: C.raised,
        border: `1px solid ${C.line}`, color: C.inkMute, fontSize: 9.5,
        display: 'grid', placeItems: 'center', alignSelf: 'center',
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      {n}
    </Box>
    <Typography sx={{ fontSize: 12, fontWeight: 600, color: C.inkMid,
                      letterSpacing: '0.01em', lineHeight: 1.5 }}>
      {label}
    </Typography>
    <Typography
      sx={{ fontSize: 13, lineHeight: 1.5, textAlign: 'right',
            color: value ? C.ink : C.inkMute,
            fontFamily: mono ? MONO : 'inherit',
            fontVariantNumeric: mono ? 'tabular-nums' : 'normal',
            overflowWrap: 'anywhere' }}
    >
      {value || '—'}
    </Typography>
  </Box>
);

const VoucherCard = ({ card, onCommand, flush }) => {
  const [legsOpen, setLegsOpen] = useState(false);
  const v = useMemo(() => normalizeVoucher(card), [card]);
  const tone = ENTRY_TONE[v.entryType];
  const voidish = v.status === '0';
  const bar = voidish ? C.inkMid : tone.bar;

  return (
    <Paper
      sx={{
        mt: flush ? 0 : 1.5, border: `1px solid ${C.line}`, borderRadius: '10px',
        overflow: 'hidden', background: C.surface,
      }}
    >
      {/* Header — the whole result in one glance */}
      <Box sx={{ background: bar, color: '#fff', px: 1.75, py: 1.35 }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start',
                   justifyContent: 'space-between', gap: 1.5 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 13.5, fontWeight: 600, letterSpacing: '-0.01em' }}>
              {tone.doc}
            </Typography>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mt: 0.25 }}>
              <Mono sx={{ fontSize: 11.5, opacity: 0.8 }}>{v.number}</Mono>
              <Tooltip title="Copy voucher id">
                <IconButton
                  size="small"
                  onClick={() => navigator.clipboard?.writeText(String(v.atId))}
                  sx={{ width: 18, height: 18, color: '#fff', opacity: 0.6,
                        '&:hover': { opacity: 1, background: 'rgba(255,255,255,0.15)' } }}
                >
                  <ContentCopyIcon sx={{ fontSize: 11 }} />
                </IconButton>
              </Tooltip>
              <Box sx={{ width: 3, height: 3, borderRadius: '50%', background: '#fff',
                         opacity: 0.4 }} />
              <Typography sx={{ fontSize: 11.5, opacity: 0.8 }}>
                {voidish ? 'Voided' : tone.flow}
              </Typography>
            </Box>
          </Box>
          <Box sx={{ textAlign: 'right', flexShrink: 0 }}>
            <Typography
              sx={{ fontFamily: MONO, fontSize: 21, fontWeight: 600,
                    letterSpacing: '-0.02em', lineHeight: 1.15,
                    textDecoration: voidish ? 'line-through' : 'none' }}
            >
              ${money(v.amount)}
            </Typography>
            <Box
              component="span"
              sx={{
                display: 'inline-block', mt: 0.4, px: 0.7, py: '1px',
                borderRadius: '4px', background: 'rgba(255,255,255,0.16)',
                fontSize: 10, fontWeight: 700, letterSpacing: '0.06em',
              }}
            >
              {String(v.statusLabel).toUpperCase()}
            </Box>
          </Box>
        </Box>
      </Box>

      {/* Anything matched automatically */}
      {v.note ? (
        <Box sx={{ px: 1.75, py: 1, background: C.warnSoft,
                   borderBottom: `1px solid #FEDF89` }}>
          <Typography sx={{ fontSize: 12, color: C.warn }}>
            <b>Please double-check:</b> {v.note}
          </Typography>
        </Box>
      ) : null}

      {!v.editable && v.blockers.length ? (
        <Box sx={{ px: 1.75, py: 1, background: C.errSoft,
                   borderBottom: `1px solid #FECDCA` }}>
          <Typography sx={{ fontSize: 12, color: C.err }}>
            <b>Not editable:</b> {v.blockers.join('; ')}.
          </Typography>
        </Box>
      ) : null}

      {/* The document */}
      <VoucherRow n={1} label="Date" value={formatDateForDisplay(v.date)} />
      <VoucherRow n={2} label={tone.party} value={v.party} hint={v.partyCode} />
      <VoucherRow n={3} label="Bank / cash" value={v.bank.name} hint={v.bank.code} />
      <VoucherRow n={4} label={tone.category} value={v.cat.name} hint={v.cat.code} />
      <VoucherRow n={5} label="Amount" value={`$${money(v.amount)}`} mono />
      <VoucherRow n={6} label="Reference" value={v.cheque ? `Check #${v.cheque}` : ''} mono />
      <VoucherRow n={7} label="Remarks" value={v.description} last />

      {/* Journal entry */}
      {v.legs.length ? (
        <Box sx={{ borderTop: `1px solid ${C.line}`, background: C.raised }}>
          <Box
            onClick={() => setLegsOpen((o) => !o)}
            sx={{ display: 'flex', alignItems: 'center', gap: 0.5, px: 1.75, py: 0.9,
                  cursor: 'pointer', '&:hover': { background: C.surface } }}
          >
            <KeyboardArrowDownIcon
              sx={{ fontSize: 16, color: C.inkMute, transition: '.15s',
                    transform: legsOpen ? 'none' : 'rotate(-90deg)' }}
            />
            <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.inkMid,
                              letterSpacing: '0.04em', whiteSpace: 'nowrap' }}>
              JOURNAL ENTRY
            </Typography>
            {!legsOpen ? (
              <Mono
                sx={{ fontSize: 11, color: C.inkMute, ml: 0.5, minWidth: 0,
                      overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap' }}
              >
                Dr {v.legs.find((l) => l.dc === 'D')?.account} / Cr{' '}
                {v.legs.find((l) => l.dc === 'C')?.account}
              </Mono>
            ) : null}
          </Box>
          <Collapse in={legsOpen}>
            <Box sx={{ px: 1.75, pb: 1.25 }}>
              {v.legs.map((l, i) => (
                <Box
                  key={`${l.code}-${l.sno ?? i}`}
                  sx={{
                    display: 'grid', gridTemplateColumns: '26px 1fr auto', gap: 1,
                    alignItems: 'center', py: 0.7,
                    borderBottom: i < v.legs.length - 1 ? `1px solid ${C.line}` : 'none',
                  }}
                >
                  <Mono sx={{ fontWeight: 700, fontSize: 11,
                              color: l.dc === 'D' ? tone.bar : C.inkMid }}>
                    {l.dc === 'D' ? 'DR' : 'CR'}
                  </Mono>
                  <Box sx={{ minWidth: 0 }}>
                    <Typography sx={{ fontSize: 12.5, color: C.ink }} noWrap title={l.account}>
                      {l.account}
                    </Typography>
                    <Mono sx={{ fontSize: 10.5, color: C.inkMute }}>
                      {l.code}{l.nature ? ` · ${l.nature}` : ''}
                    </Mono>
                  </Box>
                  <Mono sx={{ fontSize: 12.5, color: Number(l.amount) < 0 ? C.inkMid : C.ink }}>
                    {Number(l.amount) < 0
                      ? `(${money(Math.abs(l.amount))})`
                      : money(l.amount)}
                  </Mono>
                </Box>
              ))}
            </Box>
          </Collapse>
        </Box>
      ) : null}

      {/* What you can do next */}
      {onCommand && !voidish ? (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, px: 1.75, py: 1.25,
                   borderTop: `1px solid ${C.line}` }}>
          {(v.editable
            ? [['Change amount', `update ${v.atId} amount `],
               ['Change category', `update ${v.atId} category `],
               ['Change date', `update ${v.atId} date `],
               ['Void', `void ${v.atId}`]]
            : [['Open', `show ${v.atId}`], ['Void', `void ${v.atId}`]]
          ).map(([label, cmd]) => (
            <Button
              key={label}
              size="small"
              onClick={() => onCommand(cmd, cmd.endsWith(' '))}
              sx={{
                minHeight: 26, px: 1, fontSize: 12,
                color: label === 'Void' ? C.err : C.inkMid,
                border: `1px solid ${label === 'Void' ? '#FECDCA' : C.line}`,
                borderRadius: '5px',
                '&:hover': { borderColor: label === 'Void' ? C.err : C.lineStrong,
                             background: label === 'Void' ? C.errSoft : C.raised },
              }}
            >
              {label}
            </Button>
          ))}
        </Box>
      ) : null}
    </Paper>
  );
};

const ProfileCard = ({ card, onCommand, flush }) => (
  <CardShell
    flush={flush}
    title={
      <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }} noWrap>
        {card.company_name || card.person_name}
      </Typography>
    }
    right={
      <>
        <Pill label={card.type_label} tone="accent" />
        <Pill label={card.created ? 'Created' : 'Existing'} tone={card.created ? 'ok' : 'neutral'} />
      </>
    }
  >
    <Row label="Contact">{card.person_name && card.person_name !== card.company_name ? card.person_name : null}</Row>
    <Row label="Email">{card.email}</Row>
    <Row label="Phone" mono>{card.phone}</Row>
    <Row label="Address">{card.address}</Row>
    <Row label="Title">{card.job_title}</Row>
    <Row label="Default account">{card.p_account_name || card.p_account}</Row>
    {onCommand ? (
      <Button
        size="small"
        onClick={() => onCommand(`Paid $ to ${card.company_name || card.person_name} for `, true)}
        sx={{
          mt: 1.25, minHeight: 26, px: 1, fontSize: 12, color: C.inkMid,
          border: `1px solid ${C.line}`, borderRadius: '5px',
          '&:hover': { borderColor: C.lineStrong, background: C.raised },
        }}
      >
        Use in a voucher
      </Button>
    ) : null}
  </CardShell>
);

// A chart-of-accounts tree. Postable leaves are clickable — they drop the name
// into the composer, which is the whole point of looking the chart up.
const ChartCard = ({ card, onCommand }) => {
  const [open, setOpen] = useState(() => new Set());
  const toggle = (code) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });

  const Leaf = ({ name, code, indent }) => (
    <Box
      onClick={() => onCommand?.(name)}
      title={`${name}  ${code}`}
      sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 1, pl: indent, pr: 1, py: 0.5, borderRadius: '4px', cursor: 'pointer',
        '&:hover': { background: C.accentSoft },
      }}
    >
      <Typography sx={{ fontSize: 12.5, color: C.ink }} noWrap>{name}</Typography>
      <Mono sx={{ fontSize: 11, color: C.inkMute }}>{code}</Mono>
    </Box>
  );

  return (
    <CardShell
      title={
        <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
          Chart of accounts{card.filtered_to ? ` — ${card.filtered_to}` : ''}
        </Typography>
      }
      right={<Pill label={`${card.postable_total} postable`} tone="accent" />}
    >
      {(card.natures || []).map((n) => (
        <Box key={n.nature} sx={{ mb: 1.25 }}>
          <Typography
            sx={{ fontSize: 11, fontWeight: 600, color: C.inkMute,
                  letterSpacing: '0.06em', mb: 0.4 }}
          >
            {n.label.toUpperCase()} · {n.statement}
          </Typography>
          {n.mains.map((m) =>
            m.postable ? (
              <Leaf key={m.code} name={m.name} code={m.code} indent={1} />
            ) : (
              <Box key={m.code}>
                <Box
                  onClick={() => toggle(m.code)}
                  sx={{
                    display: 'flex', alignItems: 'center', gap: 0.75, pl: 0.5, py: 0.5,
                    borderRadius: '4px', cursor: 'pointer',
                    '&:hover': { background: C.raised },
                  }}
                >
                  <KeyboardArrowDownIcon
                    sx={{ fontSize: 15, color: C.inkMute, transition: '.15s',
                          transform: open.has(m.code) ? 'none' : 'rotate(-90deg)' }}
                  />
                  <Typography sx={{ fontSize: 12.5, fontWeight: 500, color: C.inkMid }}>
                    {m.name}
                  </Typography>
                  <Mono sx={{ fontSize: 11, color: C.inkMute }}>{m.code}</Mono>
                  <Typography sx={{ fontSize: 11, color: C.inkMute }}>
                    · heading, {m.sub_count} below
                  </Typography>
                </Box>
                <Collapse in={open.has(m.code)}>
                  {m.subs.map((s) => (
                    <Leaf key={s.code} name={s.name} code={s.code} indent={3} />
                  ))}
                </Collapse>
              </Box>
            )
          )}
        </Box>
      ))}
      <Typography sx={{ fontSize: 11.5, color: C.inkMute, mt: 0.5 }}>
        Click any account to put its name in the composer. Headings have
        sub-accounts under them and cannot be posted to.
      </Typography>
    </CardShell>
  );
};

const AccountCard = ({ card, onCommand, flush }) => (
  <CardShell
    flush={flush}
    tone="ok"
    title={
      <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }} noWrap>
        {card.name}
      </Typography>
    }
    right={
      <>
        <Pill label={card.level === 'sub' ? 'Sub-account' : 'Account'} tone="accent" />
        <Pill label="Created" tone="ok" />
      </>
    }
  >
    <Row label="Code" mono>{card.code}</Row>
    <Row label="Under">{card.parent_name}</Row>
    {onCommand ? (
      <Button
        size="small"
        onClick={() => onCommand(`Paid $ to  for ${card.name}`, true)}
        sx={{
          mt: 1.25, minHeight: 26, px: 1, fontSize: 12, color: C.inkMid,
          border: `1px solid ${C.line}`, borderRadius: '5px',
          '&:hover': { borderColor: C.lineStrong, background: C.raised },
        }}
      >
        Use in a voucher
      </Button>
    ) : null}
  </CardShell>
);

// What was read out of a statement, before any of it is saved.
//
// The point of this card is the sentence at the bottom of it: sixty rows were
// READ, and nothing was written. So it is a manifest — counts, totals, and
// what still needs a hand — rather than a receipt. The vouchers themselves
// arrive one at a time in the review panel.
const StatementCard = ({ card, flush }) => {
  const [showSkipped, setShowSkipped] = useState(false);
  const skipped = card.skipped || [];
  const newParties = card.new_parties || [];

  const Stat = ({ label, value, sub, hue }) => (
    <Box sx={{ minWidth: 0, flex: '1 1 120px' }}>
      <Typography sx={{ fontSize: 10.5, color: C.inkMute, textTransform: 'uppercase',
                        letterSpacing: '0.06em' }}>
        {label}
      </Typography>
      <Typography sx={{ fontSize: 15, fontWeight: 700, color: hue || C.ink,
                        lineHeight: 1.3 }}>
        {value}
      </Typography>
      {sub ? (
        <Typography sx={{ fontSize: 11, color: C.inkMute }}>{sub}</Typography>
      ) : null}
    </Box>
  );

  return (
    <CardShell
      flush={flush}
      tone={card.needs_account || card.duplicates ? 'warn' : 'neutral'}
      title={
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
          <DescriptionOutlinedIcon sx={{ fontSize: 15, color: C.inkMid, flexShrink: 0 }} />
          <Typography sx={{ fontSize: 12.5, fontWeight: 700, overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {card.filename || 'Statement'}
          </Typography>
        </Box>
      }
      right={
        <Typography sx={{ fontSize: 11, color: C.inkMute }}>
          {card.date_from === card.date_to
            ? formatDateForDisplay(card.date_from)
            : `${formatDateForDisplay(card.date_from)} → ${formatDateForDisplay(card.date_to)}`}
        </Typography>
      }
    >
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.5, py: 0.5 }}>
        <Stat label="Received" value={`$${money(card.money_in)}`}
              sub={`${card.crv_count} CRV`} hue="#065F46" />
        <Stat label="Paid" value={`$${money(card.money_out)}`}
              sub={`${card.cpv_count} CPV`} hue={C.accent} />
        <Stat label="Through" value={card.bank_account || '—'}
              sub={card.account_hint ? `account ${card.account_hint}` : null} />
      </Box>

      {(card.needs_account || card.duplicates || newParties.length) ? (
        <Box sx={{ mt: 1.25, pt: 1, borderTop: `1px solid ${C.line}`,
                   display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          {card.needs_account ? (
            <Typography sx={{ fontSize: 11.5, color: C.inkMid }}>
              <b>{card.needs_account}</b> need an account picking
            </Typography>
          ) : null}
          {card.duplicates ? (
            <Typography sx={{ fontSize: 11.5, color: C.warn }}>
              <b>{card.duplicates}</b> match a voucher already posted that day
            </Typography>
          ) : null}
          {newParties.length ? (
            <Typography sx={{ fontSize: 11.5, color: C.inkMid }}>
              <b>{newParties.length}</b> new {newParties.length === 1 ? 'name' : 'names'}
              {' '}— {newParties.slice(0, 4).join(', ')}
              {newParties.length > 4 ? ` and ${newParties.length - 4} more` : ''}
            </Typography>
          ) : null}
        </Box>
      ) : null}

      {skipped.length ? (
        <Box sx={{ mt: 1 }}>
          <Button
            size="small"
            onClick={() => setShowSkipped((s) => !s)}
            sx={{ fontSize: 11.5, textTransform: 'none', color: C.inkMid, px: 0.5 }}
          >
            {showSkipped ? 'Hide' : 'Show'} {skipped.length} line
            {skipped.length === 1 ? '' : 's'} I couldn’t read
          </Button>
          <Collapse in={showSkipped}>
            <Box sx={{ mt: 0.5, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
              {skipped.map((s, i) => (
                <Box key={i} sx={{ borderLeft: `2px solid ${C.line}`, pl: 1 }}>
                  <Mono sx={{ fontSize: 10.5, color: C.inkMid, overflowWrap: 'anywhere' }}>
                    {s.text}
                  </Mono>
                  <Typography sx={{ fontSize: 10.5, color: C.inkMute }}>{s.why}</Typography>
                </Box>
              ))}
            </Box>
          </Collapse>
        </Box>
      ) : null}

      <Typography sx={{ mt: 1.25, pt: 1, borderTop: `1px solid ${C.line}`,
                        fontSize: 11.5, color: C.inkMid }}>
        Nothing has been written. Each row is saved on its own in the panel.
      </Typography>
    </CardShell>
  );
};

// The statement named an account this ledger doesn't have — asked once, for
// the whole file, rather than sixty times with the same field blank.
const StatementBankPickCard = ({ card, onRetry, busy, flush }) => {
  const [code, setCode] = useState('');
  const choices = card.choices || [];
  return (
    <CardShell
      flush={flush}
      tone="warn"
      title={<Typography sx={{ fontSize: 12.5, fontWeight: 700 }}>
        Which account is this?
      </Typography>}
      right={<Typography sx={{ fontSize: 11, color: C.inkMute }}>
        {card.rows} rows waiting
      </Typography>}
    >
      <Autocomplete
        options={choices}
        getOptionLabel={(o) => o.qualified || ''}
        onChange={(_e, v) => setCode(v?.code || '')}
        size="small"
        renderInput={(params) => (
          <TextField {...params} variant="standard" placeholder="Choose an account"
                     sx={DRAFT_INPUT_SX} />
        )}
      />
      <Button
        fullWidth
        size="small"
        disabled={!code || busy}
        onClick={() => onRetry?.(code)}
        sx={{ mt: 1.25, textTransform: 'none', fontSize: 12.5, fontWeight: 600,
              background: C.accent, color: '#fff', borderRadius: '8px', py: 0.7,
              '&:hover': { background: '#16304F' },
              '&.Mui-disabled': { background: '#E8EBF0', color: C.inkMute } }}
      >
        {busy ? 'Reading…' : 'Read the statement'}
      </Button>
      <Typography sx={{ mt: 0.75, fontSize: 11, color: C.inkMute }}>
        Every row posts through this account, so it is asked once rather than
        guessed sixty times.
      </Typography>
    </CardShell>
  );
};

// A day's vouchers, to pick from. Ticking is how a bulk edit starts: the
// selected ones open in the review panel one at a time, so a change to twenty
// vouchers is still twenty confirmations, not one blind sweep.
const VoucherListCard = ({ card, onBulkEdit, busy }) => {
  const editable = (card.vouchers || []).filter((v) => v.editable);
  const [picked, setPicked] = useState(() => new Set());
  const [instruction, setInstruction] = useState('');

  const toggle = (id) => setPicked((prev) => {
    const next = new Set(prev);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const allOn = picked.size > 0 && picked.size === editable.length;

  return (
    <CardShell
      title={
        <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
          Vouchers on {card.date_label}
        </Typography>
      }
      right={<Pill label={`${card.editable_count} editable`} tone="accent" />}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, pb: 0.75,
                 borderBottom: `1px solid ${C.line}` }}>
        <Button size="small"
          onClick={() => setPicked(allOn ? new Set() : new Set(editable.map((v) => v.at_id)))}
          sx={{ minHeight: 24, px: 0.75, fontSize: 11.5, color: C.inkMid }}
        >
          {allOn ? 'Clear all' : 'Select all editable'}
        </Button>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
          {picked.size} selected
        </Typography>
      </Box>

      {(card.vouchers || []).map((v) => {
        const on = picked.has(v.at_id);
        return (
          <Box
            key={v.at_id}
            onClick={() => v.editable && toggle(v.at_id)}
            sx={{
              display: 'grid', gridTemplateColumns: '18px 1fr auto', gap: 1,
              alignItems: 'center', py: 0.85, borderBottom: `1px solid ${C.line}`,
              cursor: v.editable ? 'pointer' : 'default',
              opacity: v.editable ? 1 : 0.55,
              '&:hover': v.editable ? { background: C.raised } : {},
            }}
          >
            <Box sx={{ width: 14, height: 14, borderRadius: '3px',
                       border: `1.5px solid ${on ? C.accent : C.lineStrong}`,
                       background: on ? C.accent : 'transparent',
                       display: 'grid', placeItems: 'center' }}>
              {on ? <CheckIcon sx={{ fontSize: 11, color: '#fff' }} /> : null}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                <Mono sx={{ fontSize: 11.5, color: C.inkMid }}>{v.voucher_number}</Mono>
                {!v.editable ? <Pill label={v.status_label} tone="warn" /> : null}
              </Box>
              <Typography sx={{ fontSize: 12, color: C.inkMute }} noWrap>
                {v.party_name || '—'}{v.description ? ` · ${v.description}` : ''}
              </Typography>
            </Box>
            <Mono sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
              ${money(v.amount)}
            </Mono>
          </Box>
        );
      })}

      <Box sx={{ mt: 1.25 }}>
        <Typography sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.06em',
                          color: C.inkMute, mb: 0.5 }}>
          CHANGE TO APPLY (OPTIONAL)
        </Typography>
        <TextField
          fullWidth size="small" placeholder="amount 500, category Printing"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          sx={{ '& .MuiOutlinedInput-root': { fontSize: 12.5, background: C.raised,
                  borderRadius: '6px',
                  '& fieldset': { borderColor: C.line } } }}
        />
        <Typography sx={{ fontSize: 11, color: C.inkMute, mt: 0.5 }}>
          Applied to every one you picked, then shown for confirmation one at a time.
        </Typography>
        <Button
          fullWidth disableElevation variant="contained"
          disabled={!picked.size || busy}
          onClick={() => onBulkEdit([...picked], instruction.trim())}
          sx={{ mt: 1, py: 0.75, fontSize: 12.5, fontWeight: 600, borderRadius: '6px',
                background: C.accent, '&:hover': { background: '#16304F' } }}
        >
          {busy ? 'Opening…'
                : `Review ${picked.size || ''} voucher${picked.size === 1 ? '' : 's'}`.replace('  ', ' ')}
        </Button>
      </Box>
    </CardShell>
  );
};

// -----------------------------------------------------------------------------
// Review panel — the confirmation step between typing and posting.
//
// The backend resolved the sentence into codes but wrote nothing. Everything
// here is editable, because the whole point is to catch the cases where the
// parser guessed wrong. Accounts are chosen from the real chart rather than
// typed, so a correction always lands on a code that exists.
// -----------------------------------------------------------------------------
const DRAFT_INPUT_SX = {
  // No `InputProps`/`slotProps` anywhere below: the two spellings belong to
  // different MUI majors, and styling the underline away works on both.
  '& .MuiInput-root:before, & .MuiInput-root:after': { display: 'none' },
  '& .MuiInputBase-root': {
    fontSize: 14, color: C.ink, px: 0.75, py: 0.25, borderRadius: '5px',
    background: 'transparent', transition: 'background .12s',
    '&:hover': { background: C.raised },
    '&.Mui-focused': { background: C.accentSoft },
  },
  '& .MuiInputBase-input': { p: 0 },
};

const DraftField = ({ label, children, hint, tone }) => (
  <Box sx={{ px: 1.75, py: 0.7, borderBottom: `1px solid ${C.line}`, minWidth: 0 }}>
    <Typography
      sx={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '0.07em',
            color: C.inkMute, mb: 0.15 }}
    >
      {label}
    </Typography>
    {children}
    {hint ? (
      <Typography sx={{ fontSize: 10.5, mt: 0.3, lineHeight: 1.4,
                        color: tone === 'warn' ? C.warn : C.inkMute }}>
        {hint}
      </Typography>
    ) : null}
  </Box>
);

// Two short fields on one line. A date and an amount each need about four
// characters of room, and stacking them costs a whole row of vertical space
// in a panel that is mostly scrolling.
const DraftPair = ({ children }) => (
  <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr',
             '& > *:first-of-type': { borderRight: `1px solid ${C.line}` } }}>
    {children}
  </Box>
);

// Chart accounts as Autocomplete options.
const toAccountOptions = (rows) =>
  (rows || []).map((r) => ({ code: String(r.code), label: r.qualified || r.name, name: r.name }));

// A profile draft. Same review step as a voucher: the parser filled these in,
// the operator corrects them, nothing is written until Create.
const PartyDraftFields = ({ draft, set, pickers }) => {
  const o = draft.original || {};
  const wasP = (key) =>
    draft.original && String(draft[key] ?? '') !== String(o[key] ?? '')
      ? `was ${o[key] || '—'}` : undefined;
  const accOptions = useMemo(() => toAccountOptions(pickers.all || pickers.bank),
                             [pickers.all, pickers.bank]);
  const text = (key, label, extra = {}) => (
    <DraftField key={key} label={label} tone={wasP(key) ? 'warn' : undefined}
                hint={wasP(key)}>
      <TextField
        fullWidth variant="standard" placeholder="—"
        value={draft[key] || ''}
        onChange={(e) => set({ [key]: e.target.value })}
        sx={DRAFT_INPUT_SX} {...extra}
      />
    </DraftField>
  );

  return (
    <>
      <DraftField
        label="KIND"
        hint={draft.p_code
          ? 'Fixed — the profile code is seeded from the kind and is stamped on '
            + 'every voucher naming it'
          : 'Decides which control account the code is seeded from'}
      >
        <ToggleButtonGroup
          exclusive size="small" value={draft.p_type} disabled={!!draft.p_code}
          onChange={(e, v) => v && set({
            p_type: v,
            type_label: (draft.p_types || []).find((t) => t.value === v)?.label || v,
          })}
          sx={{ flexWrap: 'wrap',
                '& .MuiToggleButton-root': {
                  px: 1, py: 0.3, fontSize: 11.5, color: C.inkMid, borderColor: C.line,
                  '&.Mui-selected': { background: C.blueSoft, color: C.blue,
                                      borderColor: '#CBE2F4',
                                      '&:hover': { background: C.blueSoft } } } }}
        >
          {(draft.p_types || []).map((t) => (
            <ToggleButton key={t.value} value={t.value}>{t.label}</ToggleButton>
          ))}
        </ToggleButtonGroup>
      </DraftField>

      <DraftField
        label="NAME"
        tone={draft.existing ? 'warn' : undefined}
        hint={draft.existing
          ? `Already exists as ${draft.existing.p_code} — it will be reused, not duplicated.`
          : undefined}
      >
        <TextField
          fullWidth variant="standard" placeholder="Company or person"
          value={draft.company_name || ''}
          onChange={(e) => set({ company_name: e.target.value })}
          sx={DRAFT_INPUT_SX}
        />
      </DraftField>

      {text('person_name', 'CONTACT PERSON')}
      {text('email', 'EMAIL')}
      {text('phone', 'PHONE')}
      {text('address', 'ADDRESS')}
      {text('city', 'CITY')}
      {text('state', 'STATE')}
      {text('zipcode', 'ZIP')}
      {text('job_title', 'JOB TITLE')}

      <DraftField label="DEFAULT ACCOUNT"
                  hint={draft.p_account ? `Code ${draft.p_account}` : 'Optional'}>
        <Autocomplete
          options={accOptions}
          value={accOptions.find((o) => o.code === String(draft.p_account)) || null}
          onChange={(e, v) => set({ p_account: v?.code || '', p_account_label: v?.label || '' })}
          isOptionEqualToValue={(o, v) => o.code === v?.code}
          getOptionLabel={(o) => o?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="—" sx={DRAFT_INPUT_SX} />
          )}
        />
      </DraftField>

      {text('other_desc', 'NOTES')}
    </>
  );
};

// A chart-account draft. The parent is picked from the real chart, so a
// correction always lands somewhere that exists.
const AccountDraftFields = ({ draft, set }) => {
  const options = draft.parent_options || [];
  const current = options.find(
    (o) => o.code === String(draft.parent_code) && o.level === draft.level) || null;

  // Renaming and retiring have one editable field between them, and no parent
  // to choose — the account already has its place in the chart.
  if (draft.op) {
    return (
      <>
        <DraftField label="ACCOUNT"
                    hint={`${draft.level_label || ''} in your chart`}>
          <Typography sx={{ fontSize: 14, color: C.ink, px: 0.75 }}>
            {draft.original?.name || draft.name}
          </Typography>
        </DraftField>
        {draft.op === 'rename' ? (
          <DraftField label="NEW NAME" tone="warn"
                      hint={`was ${draft.original?.name || '—'}`}>
            <TextField
              fullWidth variant="standard" placeholder="New name"
              value={draft.name || ''}
              onChange={(e) => set({ name: e.target.value })}
              sx={DRAFT_INPUT_SX}
            />
          </DraftField>
        ) : (
          <DraftField label="CHANGE" tone="warn"
                      hint="Posted vouchers keep this account either way">
            <Typography sx={{ fontSize: 14, color: C.ink, px: 0.75 }}>
              {draft.op === 'deactivate'
                ? 'Take it out of this company’s chart'
                : 'Put it back in this company’s chart'}
            </Typography>
          </DraftField>
        )}
      </>
    );
  }

  return (
    <>
      <DraftField label="ACCOUNT NAME">
        <TextField
          fullWidth variant="standard" placeholder="Name"
          value={draft.name || ''}
          onChange={(e) => set({ name: e.target.value })}
          sx={DRAFT_INPUT_SX}
        />
      </DraftField>

      <DraftField
        label="GOES UNDER"
        hint={draft.level === 'sub'
          ? `Sub-account of ${draft.parent_name} [${draft.parent_code}]`
          : `Main account directly under ${draft.parent_name}`}
      >
        <Autocomplete
          options={options}
          groupBy={(o) => o.group}
          value={current}
          onChange={(e, v) => set({
            parent_code: v?.code || '',
            parent_name: v?.label || '',
            level: v?.level || draft.level,
            level_label: v?.level === 'main' ? 'Main account' : 'Sub-account',
          })}
          isOptionEqualToValue={(o, v) => o.code === v?.code && o.level === v?.level}
          getOptionLabel={(o) => o?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose a parent"
                       sx={DRAFT_INPUT_SX} />
          )}
        />
      </DraftField>

      <DraftField label="LEVEL"
                  hint="Set by the parent — a nature makes a main, a main makes a sub">
        <Typography sx={{ fontSize: 14, color: C.ink, px: 0.75 }}>
          {draft.level_label || (draft.level === 'sub' ? 'Sub-account' : 'Main account')}
        </Typography>
      </DraftField>
    </>
  );
};

// An existing voucher opened for editing. Identical controls to a new one,
// with a "was …" line under anything the instruction (or the operator) changed
// — an edit is only reviewable if you can see what it is changing from.
const EditDraftFields = ({ draft, set, pickers, parties }) => {
  const isCRV = draft.entry_type === 'CRV';
  const bankOptions = useMemo(() => toAccountOptions(pickers.bank), [pickers.bank]);
  const catOptions = useMemo(
    () => toAccountOptions(isCRV ? pickers.income : pickers.expense),
    [pickers.income, pickers.expense, isCRV]);
  const partyOptions = useMemo(
    () => (parties || []).map((p) => ({
      code: String(p.p_code),
      label: p.company_name || p.person_name || String(p.p_code) })), [parties]);
  const o = draft.original || {};
  const findAcc = (opts, code) => opts.find((x) => x.code === String(code)) || null;

  // "was" only appears when the value actually differs from the stored one.
  const was = (key, shown) =>
    String(draft[key] ?? '') !== String(o[key] ?? '')
      ? `was ${shown || o[key] || '—'}` : undefined;

  return (
    <>
      <DraftPair>
      <DraftField label="DATE" tone={was('transaction_date') ? 'warn' : undefined}
                  hint={was('transaction_date', formatDateForDisplay(o.transaction_date))}>
        <TextField fullWidth variant="standard" type="date"
          value={draft.transaction_date || ''}
          onChange={(e) => set({ transaction_date: e.target.value })}
          sx={DRAFT_INPUT_SX} />
      </DraftField>

      <DraftField label="AMOUNT" tone={was('amount') ? 'warn' : undefined}
                  hint={was('amount', o.amount != null ? `$${money(o.amount)}` : null)}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5 }}>
          <Box sx={{ color: C.inkMute, fontFamily: MONO, fontSize: 14 }}>$</Box>
          <TextField fullWidth variant="standard" inputMode="decimal"
            value={draft.amount ?? ''}
            onChange={(e) => set({ amount: e.target.value })}
            sx={{ ...DRAFT_INPUT_SX,
                  '& .MuiInputBase-input': { p: 0, fontFamily: MONO, fontSize: 15,
                                             fontWeight: 600 } }} />
        </Box>
      </DraftField>
      </DraftPair>

      <DraftField label={isCRV ? 'RECEIVED FROM' : 'PAY TO'}
                  tone={was('party_code') ? 'warn' : undefined}
                  hint={was('party_code', o.party_name)}>
        <Autocomplete
          options={partyOptions}
          value={partyOptions.find((p) => p.code === String(draft.party_code)) || null}
          onChange={(e, v) => set({ party_code: v?.code || '', party_name: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose a profile"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label="BANK / CASH" tone={was('bank_acc_code') ? 'warn' : undefined}
                  hint={was('bank_acc_code', o.bank_account)}>
        <Autocomplete options={bankOptions}
          value={findAcc(bankOptions, draft.bank_acc_code)}
          onChange={(e, v) => set({ bank_acc_code: v?.code || '', bank_account: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose an account"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label={isCRV ? 'INCOME ACCOUNT' : 'EXPENSE ACCOUNT'}
                  tone={was('category_acc_code') ? 'warn' : undefined}
                  hint={was('category_acc_code', o.category_account)}>
        <Autocomplete options={catOptions}
          value={findAcc(catOptions, draft.category_acc_code)}
          onChange={(e, v) => set({ category_acc_code: v?.code || '',
                                    category_account: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose an account"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label="CHECK NO." tone={was('cheque_no') ? 'warn' : undefined}
                  hint={was('cheque_no')}>
        <TextField fullWidth variant="standard" placeholder="—"
          value={draft.cheque_no || ''}
          onChange={(e) => set({ cheque_no: e.target.value })}
          sx={{ ...DRAFT_INPUT_SX, '& .MuiInputBase-input': { p: 0, fontFamily: MONO } }} />
      </DraftField>

      <DraftField label="REMARKS" tone={was('description') ? 'warn' : undefined}
                  hint={was('description')}>
        <TextField fullWidth multiline maxRows={4} variant="standard" placeholder="—"
          value={draft.description || ''}
          onChange={(e) => set({ description: e.target.value })}
          sx={DRAFT_INPUT_SX} />
      </DraftField>
    </>
  );
};

// -----------------------------------------------------------------------------
// The panel has two faces.
//
// Reading and correcting are different jobs. A screen full of dropdowns asks
// you to check the entry and to operate a form at the same time, and the form
// wins - so the panel opens as a plain document you can read in one pass, and
// the controls only appear when you press Edit.
// -----------------------------------------------------------------------------
const PV = ({ label, value, wide, tone, note }) => (
  <Box sx={{ px: 1.75, py: 0.75, minWidth: 0,
             gridColumn: wide ? '1 / -1' : 'auto',
             borderBottom: `1px solid ${C.line}` }}>
    <Typography sx={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '0.07em',
                      color: C.inkMute, lineHeight: 1.6 }}>
      {label}
    </Typography>
    <Typography sx={{ fontSize: 13, lineHeight: 1.45, overflowWrap: 'anywhere',
                      color: !value ? C.inkMute : tone === 'warn' ? C.warn : C.ink }}>
      {value || '—'}
    </Typography>
    {note ? (
      <Typography sx={{ fontSize: 10.5, color: C.warn, lineHeight: 1.4 }}>
        {note}
      </Typography>
    ) : null}
  </Box>
);

const PreviewGrid = ({ children }) => (
  <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr',
             '& > *:nth-of-type(odd)': { borderRight: `1px solid ${C.line}` },
             '& > *[style*="1 / -1"]': { borderRight: 'none' } }}>
    {children}
  </Box>
);

const DraftPreview = ({ draft, kind }) => {
  const isCRV = draft.entry_type === 'CRV';
  // On an edit the draft carries what each field used to be. Showing the old
  // value under the new one is the whole point of reviewing a change.
  const o = draft.original || {};
  const was = (key, shown) =>
    draft.original && String(draft[key] ?? '') !== String(o[key] ?? '')
      ? `was ${shown || o[key] || '—'}` : undefined;

  if (kind === 'party') {
    return (
      <PreviewGrid>
        <PV label="KIND" value={draft.type_label} />
        <PV label="PHONE" value={draft.phone} note={was('phone')} />
        <PV label="NAME" value={draft.company_name || draft.person_name} wide
            note={was('company_name')} />
        {draft.email || was('email') ? (
          <PV label="EMAIL" value={draft.email} wide note={was('email')} />
        ) : null}
        {draft.address || draft.city || was('address') ? (
          <PV label="ADDRESS" wide note={was('address')}
              value={[draft.address, draft.city, draft.state, draft.zipcode]
                .filter(Boolean).join(', ')} />
        ) : null}
        {draft.job_title || was('job_title') ? (
          <PV label="TITLE" value={draft.job_title} wide note={was('job_title')} />
        ) : null}
      </PreviewGrid>
    );
  }
  if (kind === 'account') {
    if (draft.op) {
      return (
        <PreviewGrid>
          <PV label="ACCOUNT" value={o.name} wide />
          {draft.op === 'rename'
            ? <PV label="NEW NAME" value={draft.name} wide tone="warn" />
            : <PV label="CHANGE" wide tone="warn"
                  value={draft.op === 'deactivate'
                    ? 'Take it out of this company’s chart'
                    : 'Put it back in this company’s chart'} />}
          <PV label="POSTED VOUCHERS" wide
              value="Untouched — they keep this account either way" />
        </PreviewGrid>
      );
    }
    return (
      <PreviewGrid>
        <PV label="ACCOUNT NAME" value={draft.name} wide />
        <PV label="GOES UNDER" value={draft.parent_name} />
        <PV label="LEVEL" value={draft.level_label} />
      </PreviewGrid>
    );
  }
  return (
    <PreviewGrid>
      <PV label="DATE" value={formatDateForDisplay(draft.transaction_date)} />
      <PV label="AMOUNT" value={draft.amount === '' || draft.amount == null
        ? '' : `$${money(draft.amount)}`} />
      <PV label={isCRV ? 'RECEIVED FROM' : 'PAY TO'} wide
          tone={draft.party_is_new ? 'warn' : undefined}
          value={draft.party_name
            ? draft.party_name + (draft.party_is_new ? '  (new profile)' : '')
            : ''} />
      <PV label="BANK / CASH" value={draft.bank_account} wide
          tone={draft.bank_acc_code ? undefined : 'warn'} />
      {/* An account that was DEFAULTED rather than matched looks identical to
          one that was read off the line, and on a statement most of them are
          defaults. So the preview says which — otherwise stepping through
          sixty rows means approving sixty guesses that all look certain. */}
      <PV label={isCRV ? 'INCOME ACCOUNT' : 'EXPENSE ACCOUNT'} wide
          value={draft.category_account}
          note={draft.category_acc_code && draft.category_matched === false
            ? (draft.category_note_short || 'Default used') : undefined}
          tone={draft.category_acc_code && draft.category_matched !== false
            ? undefined : 'warn'} />
      <PV label="REFERENCE" value={draft.cheque_no ? `Check #${draft.cheque_no}` : ''} />
      {/* On a statement row the remark IS the statement line, which is already
          shown above in the box that says where it came from. Printing it
          twice in a panel this narrow just pushes the fields off screen. */}
      <PV label="REMARKS"
          value={draft.description === draft.statement_text ? '' : draft.description} />
    </PreviewGrid>
  );
};

const DraftPanel = ({ draft, setDraft, pickers, parties, onPost, onDiscard, posting,
                      error, queue }) => {
  const isCRV = draft.entry_type === 'CRV';
  const bankOptions = useMemo(() => toAccountOptions(pickers.bank), [pickers.bank]);
  const catOptions = useMemo(
    () => toAccountOptions(isCRV ? pickers.income : pickers.expense),
    [pickers.income, pickers.expense, isCRV]
  );
  const partyOptions = useMemo(
    () =>
      (parties || []).map((p) => ({
        code: String(p.p_code),
        label: p.company_name || p.person_name || String(p.p_code),
        type: p.p_type,
      })),
    [parties]
  );

  const set = (patch) => setDraft({ ...draft, ...patch });
  const findAcc = (opts, code) => opts.find((o) => o.code === String(code)) || null;

  // Read first, correct second. A new draft always arrives in preview, even
  // while stepping through a queue, so each voucher is read before it is
  // touched.
  // ...unless it arrives incomplete. A draft with a field I could not work out
  // opens straight into the form, because reading it first would only tell the
  // person what they already have to fix.
  // A void reverses the voucher as it stands — there is nothing to correct,
  // so the panel stays read-only and the button says what it will do.
  const isVoid = draft.kind === 'edit' && draft.op === 'void';
  const incomplete = (draft.kind || 'voucher') === 'account'
    ? (draft.op ? (draft.op === 'rename' && !String(draft.name || '').trim())
                : !draft.parent_code)
    : isVoid ? false
    : (draft.kind === 'party' ? !String(draft.company_name || '').trim()
       : !draft.bank_acc_code || !draft.category_acc_code);
  const [editing, setEditing] = useState(incomplete);
  const identity = `${draft.kind}:${draft.at_id || ''}:${draft.source_message || ''}`;
  useEffect(() => { setEditing(incomplete); }, [identity]);

  // Switching direction re-points the category leg at a different set of
  // natures, so a category that was valid for a payment may not be valid for
  // a receipt. Drop it rather than posting to the wrong side of the ledger.
  const switchType = (next) => {
    if (!next || next === draft.entry_type) return;
    const nextCats = toAccountOptions(next === 'CRV' ? pickers.income : pickers.expense);
    const stillValid = nextCats.some((o) => o.code === String(draft.category_acc_code));
    setDraft({
      ...draft,
      entry_type: next,
      party_label: next === 'CRV' ? 'Customer' : 'Vendor',
      doc_label: next === 'CRV' ? 'CRV' : 'CPV',
      category_acc_code: stillValid ? draft.category_acc_code : '',
      category_account: stillValid ? draft.category_account : '',
    });
  };

  // Three things can be drafted; each has its own required set, its own
  // colour and its own verb, but they share this one review step.
  const kind = draft.kind || 'voucher';
  const amountNum = Number(String(draft.amount ?? '').replace(/[^0-9.\-]/g, ''));
  const problems = [];
  if (kind === 'voucher') {
    if (!(amountNum > 0)) problems.push('an amount greater than zero');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(draft.transaction_date || ''))) problems.push('a date');
    if (!String(draft.party_name || '').trim()) problems.push(isCRV ? 'a customer' : 'a vendor');
    if (!draft.bank_acc_code) problems.push('a bank/cash account');
    if (!draft.category_acc_code) problems.push('a category account');
    if (draft.bank_acc_code && draft.bank_acc_code === draft.category_acc_code) {
      problems.push('two different accounts — both legs point at the same one');
    }
  } else if (kind === 'party') {
    if (!String(draft.company_name || '').trim()) problems.push('a name');
    if (!draft.p_type) problems.push('a kind');
  } else if (kind === 'account') {
    if (draft.op) {
      if (draft.op === 'rename' && !String(draft.name || '').trim()) {
        problems.push('the new name');
      }
    } else {
      if (!String(draft.name || '').trim()) problems.push('an account name');
      if (!draft.parent_code) problems.push('a parent');
    }
  } else if (kind === 'edit' && !isVoid) {
    if (!(amountNum > 0)) problems.push('an amount greater than zero');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(draft.transaction_date || ''))) problems.push('a date');
    if (!draft.party_code) problems.push('a party');
    if (!draft.bank_acc_code) problems.push('a bank/cash account');
    if (!draft.category_acc_code) problems.push('a category account');
    if (draft.bank_acc_code && draft.bank_acc_code === draft.category_acc_code) {
      problems.push('two different accounts — both legs point at the same one');
    }
  }
  const ready = problems.length === 0;

  const SPEC = {
    voucher: { hue: isCRV ? '#065F46' : C.accent, verb: 'Save',
               title: isCRV ? 'CRV' : 'CPV', busy: 'Posting…',
               sub: isCRV ? 'Money in — check and post' : 'Money out — check and post',
               foot: 'Nothing is written until you post.' },
    party:   { hue: C.blue, busy: draft.p_code ? 'Saving…' : 'Creating…',
               verb: draft.p_code ? 'Save changes' : 'Create profile',
               title: draft.p_code ? 'Edit profile' : 'New profile',
               sub: draft.p_code ? 'Check and save' : 'Check and create',
               foot: draft.p_code ? 'Nothing changes until you save.'
                                  : 'Nothing is written until you create it.' },
    account: { hue: C.violet, busy: draft.op ? 'Saving…' : 'Creating…',
               verb: draft.op === 'rename' ? 'Save'
                     : draft.op === 'deactivate' ? 'Take it out of the chart'
                     : draft.op === 'activate' ? 'Put it back'
                     : 'Add to chart',
               title: draft.op ? (draft.doc_label || 'Edit account') : 'New account',
               sub: draft.op ? 'Check and save' : 'Check and create',
               foot: draft.op ? 'Posted vouchers are untouched either way.'
                              : 'Nothing is written until you create it.' },
    edit:    isVoid
      ? { hue: C.err, verb: 'Void this voucher', busy: 'Voiding…',
          title: draft.voucher_number || (isCRV ? 'CRV' : 'CPV'),
          sub: 'Reversing — check it first',
          foot: 'It stays in the ledger, marked void. This cannot be undone here.' }
      : { hue: C.warn, verb: 'Save changes', busy: 'Saving…',
          title: draft.voucher_number || (isCRV ? 'CRV' : 'CPV'),
          sub: 'Check and save', foot: 'Nothing changes until you save.' },
  }[kind];

  // Nothing edited yet is not an error — it just has nothing to save. A void
  // is the exception: reversing it as-is IS the whole change.
  const changed = kind !== 'edit' || isVoid || ['amount', 'transaction_date',
    'party_code', 'bank_acc_code', 'category_acc_code', 'cheque_no', 'description'].some(
      (k) => String(draft[k] ?? '') !== String((draft.original || {})[k] ?? ''));

  const journal = isCRV
    ? `Dr ${draft.bank_account || '—'}  /  Cr ${draft.category_account || '—'}`
    : `Dr ${draft.category_account || '—'}  /  Cr ${draft.bank_account || '—'}`;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0,
               background: C.surface }}>
      {/* Header */}
      <Box sx={{ px: 1.75, py: 1.1, background: SPEC.hue, color: '#fff', flexShrink: 0,
                 display: 'flex', alignItems: 'baseline', gap: 1 }}>
        <Typography sx={{ fontSize: 14, fontWeight: 700, letterSpacing: '0.01em' }}>
          {SPEC.title}
        </Typography>
        <Typography sx={{ fontSize: 11, opacity: 0.8, flex: 1, minWidth: 0 }}>
          {editing ? 'Correcting — nothing saved yet' : SPEC.sub}
        </Typography>
      </Box>

      {/* Stepping through a bulk edit */}
      {queue && queue.total > 1 ? (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                   px: 1, py: 0.6, background: C.raised,
                   borderBottom: `1px solid ${C.line}`, flexShrink: 0 }}>
          <IconButton size="small" disabled={queue.index === 0 || posting}
                      onClick={queue.onPrev} sx={{ color: C.inkMid }}>
            <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: 'rotate(90deg)' }} />
          </IconButton>
          <Box sx={{ textAlign: 'center', minWidth: 0 }}>
            <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.ink }}>
              {queue.index + 1} of {queue.total}
            </Typography>
            <Typography sx={{ fontSize: 10.5, color: C.inkMute }}>
              {queue.savedCount} saved · {queue.total - queue.savedCount} to go
            </Typography>
          </Box>
          <IconButton size="small" disabled={queue.index >= queue.total - 1 || posting}
                      onClick={queue.onNext} sx={{ color: C.inkMid }}>
            <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: 'rotate(-90deg)' }} />
          </IconButton>
        </Box>
      ) : null}

      {/* What I guessed, and what I could not work out at all — two
          different things, so they don't share a sentence. */}
      {draft.review_items?.length ? (() => {
        const needed = draft.review_items.filter((i) => i.includes('choose it here'))
          .map((i) => i.replace(' - choose it here', ''));
        const guessed = draft.review_items.filter((i) => !i.includes('choose it here'));
        // Short labels only. The reason lives in the reply, where there is
        // room to read it; a form rail full of paragraphs pushes the fields
        // off screen and gets skimmed past anyway. A void is the exception —
        // it is one line and it is the whole point of the panel.
        // Two groups, never merged: a thing I could not work out and a thing I
        // guessed are different obligations, and one heading over both makes
        // the guesses look mandatory.
        const Group = ({ title, items, strong }) => (
          <>
            <Typography sx={{ fontSize: 10, color: C.warn, fontWeight: 700,
                              letterSpacing: '.06em', mb: 0.2, mt: title.mt ? 0.6 : 0 }}>
              {title.text}
            </Typography>
            {items.map((t) => (
              <Typography key={t} sx={{ fontSize: 11.5, color: C.warn,
                                        lineHeight: 1.4,
                                        fontWeight: strong ? 600 : 400 }}>
                {isVoid ? t : `• ${t}`}
              </Typography>
            ))}
          </>
        );
        return (
          <Box sx={{ px: 1.75, py: 0.9, background: C.warnSoft, flexShrink: 0,
                     borderBottom: `1px solid #FEDF89`,
                     borderLeft: `3px solid ${C.warn}` }}>
            {needed.length ? (
              <Group title={{ text: 'STILL NEEDED' }} items={needed} strong />
            ) : null}
            {guessed.length ? (
              <Group
                title={{ text: isVoid ? 'BEFORE YOU VOID' : 'WORTH A LOOK',
                         mt: needed.length > 0 }}
                items={guessed.map((g) => (isVoid ? g : reviewCopy(g).short))} />
            ) : null}
          </Box>
        );
      })() : null}

      {/* The line this came off, when it came off a statement. It is the only
          thing that ties the draft back to the page it was read from, and it
          is what you check the fields against — so it sits above them, as
          printed, rather than being paraphrased into a description. */}
      {draft.statement_text ? (
        <Box sx={{ px: 1.5, pb: 1 }}>
          <Typography sx={{ fontSize: 10, color: C.inkMute, textTransform: 'uppercase',
                            letterSpacing: '0.06em', mb: 0.3 }}>
            From the statement
            {draft.statement_section ? ` · ${draft.statement_section}` : ''}
          </Typography>
          <Mono sx={{ fontSize: 10.5, color: C.inkMid, lineHeight: 1.45,
                      display: 'block', background: C.raised, borderRadius: '6px',
                      px: 0.9, py: 0.7, overflowWrap: 'anywhere' }}>
            {draft.statement_text}
          </Mono>
        </Box>
      ) : null}

      {/* Fields — the document, or the form that corrects it */}
      <Box sx={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
        {!editing ? <DraftPreview draft={draft} kind={kind} />
         : kind === 'party' ? <PartyDraftFields draft={draft} set={set} pickers={pickers} />
         : kind === 'account' ? <AccountDraftFields draft={draft} set={set} />
         : kind === 'edit' ? <EditDraftFields draft={draft} set={set} pickers={pickers}
                                              parties={parties} />
         : (
        <>
        <DraftPair>
        <DraftField label="TYPE">
          <ToggleButtonGroup
            exclusive
            size="small"
            value={draft.entry_type}
            onChange={(e, v) => switchType(v)}
            sx={{
              '& .MuiToggleButton-root': {
                px: 1.25, py: 0.3, fontSize: 12, color: C.inkMid, borderColor: C.line,
                '&.Mui-selected': { background: C.accentSoft, color: C.accent,
                                    borderColor: '#D6E0EF',
                                    '&:hover': { background: C.accentSoft } },
              },
            }}
          >
            <ToggleButton value="CRV">CRV</ToggleButton>
            <ToggleButton value="CPV">CPV</ToggleButton>
          </ToggleButtonGroup>
        </DraftField>

        <DraftField label="DATE">
          <TextField
            fullWidth variant="standard" type="date"
            value={draft.transaction_date || ''}
            onChange={(e) => set({ transaction_date: e.target.value })}
            sx={DRAFT_INPUT_SX}
          />
        </DraftField>
        </DraftPair>

        <DraftField
          label={isCRV ? 'RECEIVED FROM' : 'PAY TO'}
          tone={draft.party_is_new ? 'warn' : undefined}
          hint={draft.party_is_new ? 'New — created when you post' : undefined}
        >
          <Autocomplete
            freeSolo
            options={partyOptions}
            value={draft.party_name || ''}
            onChange={(e, v) => {
              if (v && typeof v === 'object') {
                set({ party_code: v.code, party_name: v.label, party_is_new: false });
              } else {
                set({ party_code: null, party_name: v || '', party_is_new: !!v });
              }
            }}
            onInputChange={(e, v, reason) => {
              // Typing a name that is not an existing profile means "create it".
              if (reason === 'input') {
                const hit = partyOptions.find(
                  (o) => o.label.toLowerCase() === (v || '').trim().toLowerCase());
                set(hit
                  ? { party_code: hit.code, party_name: hit.label, party_is_new: false }
                  : { party_code: null, party_name: v, party_is_new: !!v });
              }
            }}
            getOptionLabel={(o) => (typeof o === 'string' ? o : o.label)}
            isOptionEqualToValue={(o, v) =>
              o.label === (typeof v === 'string' ? v : v?.label)}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Name"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftField
          label="BANK / CASH"
          tone={draft.bank_acc_code ? undefined : 'warn'}
          hint={draft.bank_acc_code ? undefined
            : (draft.bank_note_short || 'Not matched — choose it')}
        >
          <Autocomplete
            options={bankOptions}
            value={findAcc(bankOptions, draft.bank_acc_code)}
            onChange={(e, v) => set({ bank_acc_code: v?.code || '', bank_account: v?.label || '' })}
            isOptionEqualToValue={(o, v) => o.code === v?.code}
            getOptionLabel={(o) => o?.label || ''}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Choose an account"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftField
          label={isCRV ? 'INCOME ACCOUNT' : 'EXPENSE ACCOUNT'}
          tone={draft.category_acc_code && draft.category_matched !== false
            ? undefined : 'warn'}
          hint={!draft.category_acc_code
            ? (draft.category_note_short || 'Not matched — choose it')
            : (draft.category_matched === false
                ? (draft.category_note_short || 'Default used') : undefined)}
        >
          <Autocomplete
            options={catOptions}
            value={findAcc(catOptions, draft.category_acc_code)}
            onChange={(e, v) => set({ category_acc_code: v?.code || '',
                                      category_account: v?.label || '',
                                      category_matched: true })}
            isOptionEqualToValue={(o, v) => o.code === v?.code}
            getOptionLabel={(o) => o?.label || ''}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Choose an account"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftPair>
        <DraftField label="AMOUNT">
          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5 }}>
            <Box sx={{ color: C.inkMute, fontFamily: MONO, fontSize: 14 }}>$</Box>
            <TextField
              fullWidth variant="standard" inputMode="decimal"
              value={draft.amount ?? ''}
              onChange={(e) => set({ amount: e.target.value })}
              sx={{ ...DRAFT_INPUT_SX,
                    '& .MuiInputBase-input': { p: 0, fontFamily: MONO, fontSize: 15,
                                               fontWeight: 600 } }}
            />
          </Box>
        </DraftField>

        <DraftField label="CHECK NO.">
          <TextField
            fullWidth variant="standard" placeholder="—"
            value={draft.cheque_no || ''}
            onChange={(e) => set({ cheque_no: e.target.value })}
            sx={{ ...DRAFT_INPUT_SX, '& .MuiInputBase-input': { p: 0, fontFamily: MONO } }}
          />
        </DraftField>
        </DraftPair>

        <DraftField label="REMARKS">
          <TextField
            fullWidth multiline maxRows={4} variant="standard" placeholder="—"
            value={draft.description || ''}
            onChange={(e) => set({ description: e.target.value })}
            sx={DRAFT_INPUT_SX}
          />
        </DraftField>

        <Box sx={{ px: 1.75, py: 0.9 }}>
          <Typography sx={{ fontFamily: MONO, fontSize: 10.5, color: C.inkMute,
                            lineHeight: 1.5 }}>
            {journal}
          </Typography>
        </Box>
        </>
        )}

        {editing && draft.source_message ? (
          <Box sx={{ px: 1.75, py: 1 }}>
            <Typography sx={{ fontSize: 11, color: C.inkMute, fontStyle: 'italic' }}>
              From: “{draft.source_message}”
            </Typography>
          </Box>
        ) : null}
      </Box>

      {/* Footer */}
      <Box sx={{ flexShrink: 0, borderTop: `1px solid ${C.line}`, p: 1.25,
                 background: C.raised }}>
        {error ? (
          <Typography sx={{ fontSize: 12, color: C.err, mb: 1 }}>{error}</Typography>
        ) : null}
        {!ready ? (
          <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 1 }}>
            Needs {problems.join(', ')}.
          </Typography>
        ) : !changed ? (
          <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 1 }}>
            Nothing changed yet — edit a field, or skip to the next.
          </Typography>
        ) : null}
        <Box sx={{ display: 'flex', gap: 0.75 }}>
          <Button
            fullWidth
            disableElevation
            variant="contained"
            disabled={!ready || !changed || posting}
            onClick={() => onPost((kind === 'voucher' || kind === 'edit')
              ? { ...draft, amount: amountNum } : draft)}
            startIcon={posting
              ? <CircularProgress size={13} thickness={5} sx={{ color: 'inherit' }} />
              : null}
            sx={{ py: 0.85, fontSize: 13, fontWeight: 600, borderRadius: '6px',
                  whiteSpace: 'nowrap', background: SPEC.hue,
                  '&:hover': { background: SPEC.hue, filter: 'brightness(1.12)' } }}
          >
            {posting ? SPEC.busy : SPEC.verb}
          </Button>
          {isVoid ? null : (
          <Button
            disabled={posting}
            onClick={() => setEditing((e) => !e)}
            startIcon={<EditIcon sx={{ fontSize: 14 }} />}
            sx={{ flexShrink: 0, px: 0.9, py: 0.85, fontSize: 12.5, borderRadius: '6px',
                  whiteSpace: 'nowrap', minWidth: 0,
                  '& .MuiButton-startIcon': { mr: 0.4 },
                  color: editing ? SPEC.hue : C.inkMid,
                  border: `1px solid ${editing ? SPEC.hue : C.line}`,
                  '&:hover': { borderColor: SPEC.hue, background: C.surface } }}
          >
            {editing ? 'Done' : 'Edit'}
          </Button>
          )}
        </Box>
        <Button
          fullWidth
          disabled={posting}
          onClick={queue && queue.index < queue.total - 1 ? queue.onNext : onDiscard}
          sx={{ mt: 0.6, py: 0.55, fontSize: 12, color: C.inkMid,
                border: `1px solid ${C.line}`, borderRadius: '6px',
                '&:hover': { borderColor: C.lineStrong, background: C.surface } }}
        >
          {queue && queue.index < queue.total - 1 ? 'Skip to next' : 'Discard'}
        </Button>
        <Typography sx={{ mt: 0.6, fontSize: 10, color: C.inkMute, textAlign: 'center' }}>
          {SPEC.foot}
        </Typography>
      </Box>
    </Box>
  );
};

// -----------------------------------------------------------------------------
// Message
// -----------------------------------------------------------------------------
// Cards that repeat their own headline. When one of these is in the reply the
// prose above it is dropped — the card IS the answer.
// A face for each side of the conversation. The assistant's is drawn rather
// than imported so it needs no asset pipeline and inherits the brand colour;
// the person's is their initial, which is all a single-user app can honestly
// know about them.
const BotAvatar = ({ size = 26 }) => (
  <Box
    aria-hidden
    sx={{
      width: size, height: size, borderRadius: '8px', flexShrink: 0,
      display: 'grid', placeItems: 'center',
      background: `linear-gradient(140deg, ${C.accent}, #2E5C96)`,
      boxShadow: '0 1px 2px rgba(16,24,40,.18)',
    }}
  >
    <SmartToyIcon sx={{ fontSize: size * 0.6, color: '#fff' }} />
  </Box>
);

const UserAvatar = ({ initial = 'You', size = 26 }) => (
  <Box
    aria-hidden
    sx={{
      width: size, height: size, borderRadius: '8px', flexShrink: 0,
      display: 'grid', placeItems: 'center', background: C.raised,
      border: `1px solid ${C.lineStrong}`, color: C.inkMid,
      fontSize: size * 0.42, fontWeight: 700, letterSpacing: '0.02em',
    }}
  >
    {String(initial).trim().slice(0, 1).toUpperCase() || 'Y'}
  </Box>
);

// The chart card renders the same tree the message spells out in text, only
// legibly and clickably — printing both was the same content twice, once badly.
const SELF_EXPLANATORY = new Set(['voucher', 'profile', 'account', 'chart']);

// A review flag is a note to a person, not a field name. The backend sends
// short keys so it can stay out of the copy business; the wording lives here.
const REVIEW_COPY = [
  [/outside the open fiscal year/i, 'the date',
   'This date is outside the financial year currently open in LockInLedger.'],
  [/^new (customer|vendor|employee|other) record$/i, 'the new profile',
   'This name is not in the ledger yet — a profile will be created when you post.'],
  [/already exists as/i, 'the existing profile',
   'A profile with this name already exists — it will be reused, not duplicated.'],
  [/becomes a heading/i, 'the parent account',
   'Adding this account turns its parent into a heading, so nothing can be posted '
   + 'directly to the parent any more.'],
  [/^bank\/cash account$/i, 'the bank account',
   'I matched the bank account from your wording rather than an exact name — '
   + 'check it is the right one.'],
  [/^category$/i, 'the category',
   'Nothing in your message named this account, so I filled in a default.'],
  [/^(customer|vendor)$/i, 'the name',
   'I matched this name loosely to an existing profile — check it is the right one.'],
  [/^DIRECTION/i, 'the direction',
   'Your line did not say whether the money came in or went out, so I worked it '
   + 'out from the wording.'],
];

const reviewCopy = (item) => {
  const hit = REVIEW_COPY.find(([re]) => re.test(item));
  return hit ? { short: hit[1], long: hit[2] } : { short: item, long: item };
};


// The one line above a draft. Something I could not work out at all is a
// different message from something I guessed, and saying both in one sentence
// reads as gibberish ("check the bank/cash account - choose it here").
const draftHeadline = (items = []) => {
  const needed = (items || []).filter((i) => i.includes('choose it here'))
    .map((i) => i.replace(' - choose it here', ''));
  const guessed = (items || []).filter((i) => !i.includes('choose it here'));
  const short = guessed.map((g) => reviewCopy(g).short);
  if (needed.length) {
    return `Nothing posted yet — I still need the ${needed.join(' and the ')}. `
      + 'Pick it on the right'
      + (short.length ? `, and check ${short.join(' and ')}.` : '.');
  }
  if (short.length) {
    return `Ready — check ${short.join(' and ')} on the right, then post.`;
  }
  return 'Review the details and save.';
};


// A network failure is not the person's fault and not their vocabulary. Say
// what happened, what it means for their data, and what to try — the raw
// axios detail goes to the console for whoever maintains this.
const friendlyNetworkError = (error) => {
  const detail = error?.response?.data?.detail || error?.message || 'unknown error';
  // eslint-disable-next-line no-console
  console.error('[LedgerAssist] request failed:', detail, error);
  const status = error?.response?.status;
  if (!error?.response) {
    return `I can't reach the ledger service right now, so nothing was written.\n\n`
      + `It usually means the assistant's server isn't running, or this browser `
      + `can't get to ${API_BASE_URL}. Try again once it's back up.`;
  }
  if (status === 404) {
    return 'That request went to an address the ledger service does not have. '
      + 'Nothing was written — the app and the server are probably on different versions.';
  }
  if (status >= 500) {
    return 'The ledger service hit an error handling that, so nothing was written. '
      + 'Try again in a moment; if it keeps happening, whoever maintains the '
      + 'assistant will find the details in the server log.';
  }
  return `That request was refused (${status}), so nothing was written.`;
};


const Message = ({ msg, onCommand, onSuggest, onReopenDraft, onBulkEdit,
                   onStatementAccount, busy }) => {
  if (msg.type === 'user') {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'flex-start',
                 gap: 1, mb: 2 }}>
        <Box
          sx={{
            maxWidth: '80%', px: 1.5, py: 1, borderRadius: '8px 8px 2px 8px',
            background: C.accent, color: '#fff',
          }}
        >
          {msg.attachment ? (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.4 }}>
              <DescriptionOutlinedIcon sx={{ fontSize: 15, opacity: 0.85 }} />
              <Typography sx={{ fontSize: 11, opacity: 0.85 }}>
                {(msg.attachment.size / 1024).toFixed(0)} KB
              </Typography>
            </Box>
          ) : null}
          <Typography sx={{ fontSize: 13.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {msg.content}
          </Typography>
        </Box>
        <UserAvatar />
      </Box>
    );
  }

  // A card with no words around it needs no bubble either — the card has its
  // own frame, and a second one around it just adds a box in a box.
  const bare = !msg.content && !msg.draft && !!msg.card;
  const Shell = bare ? Box : Paper;

  return (
    <Box sx={{ mb: 2.5, maxWidth: 720, display: 'flex', alignItems: 'flex-start',
               gap: 1.25 }}>
      <BotAvatar />
      <Box sx={{ flex: 1, minWidth: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.6 }}>
        <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.ink, letterSpacing: '0.02em' }}>
          LedgerAssist
        </Typography>
        <Typography sx={{ fontSize: 11, color: C.inkMute }}>{clockTime(msg.timestamp)}</Typography>
        {msg.isError ? <Pill label="Not posted" tone="err" /> : null}
      </Box>
      <Shell
        sx={bare ? { background: 'transparent' } : {
          px: 1.75, py: msg.content ? 1.35 : 1, pt: msg.content ? 1.35 : 0.5,
          border: `1px solid ${msg.isError ? '#FECDCA' : C.line}`,
          borderRadius: '8px', background: msg.isError ? C.errSoft : C.surface,
        }}
      >
        {msg.content ? (
          // An explanation is only useful if it is readable. The first line
          // carries the verdict, so it gets the weight and the red; the rest
          // is instructions and reads better in normal ink.
          (() => {
            const [head, ...rest] = String(msg.content).split('\n');
            const body = rest.join('\n').replace(/^\n+/, '');
            return (
              <>
                <Typography
                  sx={{
                    fontSize: 13.5, lineHeight: 1.55,
                    fontWeight: msg.isError && body ? 600 : 400,
                    color: msg.isError ? C.err : C.ink,
                    whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    fontFamily: msg.mono ? MONO : 'inherit',
                  }}
                >
                  {head}
                </Typography>
                {body ? (
                  <Typography
                    sx={{
                      mt: 0.75, fontSize: 13, lineHeight: 1.6,
                      color: msg.isError ? C.inkMid : C.ink,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      fontFamily: msg.mono ? MONO : 'inherit',
                    }}
                  >
                    {body}
                  </Typography>
                ) : null}
              </>
            );
          })()
        ) : null}
        {msg.draft ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1.25 }}>
            <Button
              size="small"
              disableElevation
              variant={msg.draftPosted ? 'text' : 'contained'}
              disabled={msg.draftPosted}
              onClick={() => onReopenDraft?.(msg.draft)}
              sx={{ minHeight: 28, px: 1.25, fontSize: 12, borderRadius: '5px',
                    ...(msg.draftPosted
                      ? { color: C.inkMute }
                      : { background: C.accent, '&:hover': { background: '#16304F' } }) }}
            >
              {msg.draftPosted ? 'Done' : 'Review →'}
            </Button>
          </Box>
        ) : null}
        {msg.suggestions?.length ? (
          <Box sx={{ mt: 1.1, display: 'flex', flexDirection: 'column',
                     alignItems: 'flex-start', gap: 0.6 }}>
            <Typography sx={{ fontSize: 10.5, fontWeight: 700, color: C.inkMute,
                              letterSpacing: '0.06em' }}>
              SEND ONE OF THESE
            </Typography>
            {msg.suggestions.map((sug) => (
              <Box
                key={sug}
                onClick={() => onSuggest?.(sug)}
                sx={{ px: 1.1, py: 0.65, borderRadius: '7px', cursor: 'pointer',
                      border: `1px solid ${C.line}`, background: C.surface,
                      maxWidth: '100%',
                      '&:hover': { borderColor: C.accent, background: C.accentSoft } }}
              >
                <Mono sx={{ fontSize: 12, color: C.accent, lineHeight: 1.45,
                            overflowWrap: 'anywhere' }}>
                  {sug}
                </Mono>
              </Box>
            ))}
          </Box>
        ) : null}
        {msg.quickExamples ? <QuickExamples onPick={(t) => onCommand?.(t, true)} /> : null}
        {msg.intro ? <IntroCard intro={msg.intro} onPick={(t) => onCommand?.(t, true)} /> : null}
        {msg.card?.kind === 'voucher'
          ? <VoucherCard card={msg.card} onCommand={onCommand} flush={bare} /> : null}
        {msg.card?.kind === 'profile'
          ? <ProfileCard card={msg.card} onCommand={onCommand} flush={bare} /> : null}
        {msg.card?.kind === 'chart' ? <ChartCard card={msg.card} onCommand={onCommand} /> : null}
        {msg.card?.kind === 'account'
          ? <AccountCard card={msg.card} onCommand={onCommand} flush={bare} /> : null}
        {msg.card?.kind === 'voucher_list'
          ? <VoucherListCard card={msg.card} onBulkEdit={onBulkEdit} busy={busy} /> : null}
        {msg.card?.kind === 'statement_summary'
          ? <StatementCard card={msg.card} flush={bare} /> : null}
        {msg.card?.kind === 'statement_bank_pick'
          ? <StatementBankPickCard card={msg.card} onRetry={onStatementAccount}
                                   busy={busy} flush={bare} /> : null}
      </Shell>
      </Box>
    </Box>
  );
};

// -----------------------------------------------------------------------------
// Reference panel — the command grammar, not a form.
// -----------------------------------------------------------------------------
const COMMANDS = [
  {
    group: 'Record',
    items: [
      'Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of America 9523 on 06/04/2026',
      'Received $659.25 from John Smith today via Bank of America 9523 for Healthcare Services',
      'Paid $780 to Pixel Studio for EXPENSE/Website Development, check 4521',
      '06/10/2026 Current Assets - ACME OFFICE SUPPLY 129.40 office supplies',
    ],
  },
  {
    group: 'Review and edit',
    hint: 'The id can come first or after the verb — both work.',
    items: [
      'show 260902000001',
      '260902000001',
      'update 260902000001 amount 500',
      '260902000001 amount 500',
      '260902000001 update category Printing, date 06/10/2026',
      'update 260902000001 party Handy Fix LLC, bank Bank of America 9523',
      'void 260902000001',
    ],
  },
  {
    group: 'Edit a whole day',
    hint: 'Give a date instead of an id — tick the vouchers you want, then confirm each one in the preview.',
    items: [
      '7-june-2026',
      '06/07/2026',
      'vouchers on 7 June 2026',
      'show vouchers dated 7-jun-26',
      'edit vouchers today',
      'update 7-june-2026',
    ],
  },
  {
    group: 'Profiles',
    items: [
      'add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143',
      'new customer Acme Corp',
      'add employee Maria Lopez, phone 555-0192, title Nurse',
    ],
  },
  {
    group: 'Chart of accounts',
    items: [
      'add expense account Fuel',
      'add revenue account Consulting Income',
      'add bank account Meezan 1234',
      'add account FICA under Payroll Taxes',
    ],
  },
  {
    group: 'Look up',
    items: ['show my transactions', 'show my financial summary',
            'show chart', 'expense chart', 'revenue chart'],
  },
];

// -----------------------------------------------------------------------------
// App
// -----------------------------------------------------------------------------
// The toolbar. Each button drops the start of a command into the composer and
// puts the cursor after it, so the button teaches the grammar rather than
// hiding it — everything here stays typeable.
const PLACEHOLDERS = {
  cpv: 'Paid $450 to Handy Fix LLC for repair and maintenance from Bank of America 9523',
  crv: 'Received $1,250 from ABC Trading for invoice 2045 into Chase Bank 4582',
  post: 'Paid $450 to Handy Fix LLC for repair and maintenance from Bank of America 9523',
  update: '261203000165 amount 500  —  or a date like 7-june-2026 for a whole day',
  view: 'show my transactions',
  profile: 'Add ABC Trading LLC as a new customer, phone 555-123-4567',
  editprofile: "Change ABC Trading's phone number to 555-987-6543",
  editchart: 'rename account Fuel to Fuel and Oil',
  chart: 'add bank account Meezan 1234',
  chartview: 'show chart',
  // Nothing to type — the attach button beside this box is the command.
  statement: 'Attach a statement with the paperclip, or drop it onto the chat',
};

// What each job needs, in that job's own words. Pressing a command in the rail
// puts the matching card in the transcript, so the answer to "what do I type
// now?" is on screen for the thing you just said you wanted to do.
const INTRO = {
  cpv: {
    hue: C.accent, title: 'Create a CPV \u2014 Payment',
    blurb: 'Enter the payee, amount, purpose/expense category, and payment account.',
    examples: [
      ['Paid $450 to Handy Fix LLC for repair and maintenance from Bank of America 9523',
       'Complete transaction details in a single sentence.'],
      ['Paid $1,200 to ABC Supplies for office supplies from Chase 4582', 'Standard bank account transfer example'],
      ['Paid $750 to John Smith for consulting services from cash', 'Cash payment example.'],
      ['Paid $325 to XYZ Electric for electrical repairs from Bank of America 9523, check 4521',
       'Payment with check number included.'],
    ],
    hint: 'The amount, the payee and the bank account are what I need. The expense '
      + 'account and the date are filled in for you to check if you leave them out.',
  },
  crv: {
    hue: '#065F46', title: 'Create Cash Receipt Voucher (CRV) \u2014 Funds Received',
    blurb: ' Enter the payer, amount, reference/invoice number, and deposit account.',
    examples: [
      ['Received $1,250 from ABC Trading for invoice 2045 into Chase Bank 4582',
       'Complete transaction details in a single sentence.'],
      ['Received $500 from John Smith for invoice 1025', 'Omit bank account to select manually during review'],
      ['Received $2,000 from ABC Trading for sales into Bank of America 9523', 'Direct deposit example.'],
      ['Received $750 cash from Handy Fix LLC for invoice 3050 today',
       'Cash receipt using relative date keywords (e.g., today, yesterday)'],
    ],
    hint: 'The amount, the payer and the bank account are what I need. The income '
      + 'account and the date are filled in for you to check if you leave them out.',
  },
  statement: {
    hue: C.violet, title: 'Read a Statement — CRV and CPV from a PDF',
    blurb: 'Attach a bank or credit-card statement. Every line comes back as a '
      + 'draft voucher for you to check — deposits as CRVs, withdrawals as CPVs.',
    examples: [
      ['Choose a PDF or CSV your bank gave you',
       'A downloaded statement, not a scan or a photo — a scan has no text to read.'],
      ['One account per file',
       'It reads the account number off the page; if that matches nothing, it asks '
       + 'once rather than leaving every row blank.'],
      ['Read the rows, save them one at a time',
       'Nothing is written by uploading. Each row is saved on its own, or skipped.'],
      ['Names you have already get matched',
       'The rest are marked new, and a profile is created only when you save that row.'],
    ],
    hint: 'The typing is bulk and so is the reading. The writing never is — '
      + 'sixty rows is still sixty confirmations.',
  },
  update: {
    hue: C.warn, title: 'Edit Voucher',
    blurb: 'Provide a Voucher ID for direct editing, or a date to view daily entries.',
    examples: [
      ['261203000165', 'Retrieve a voucher record for review and corrections'],
      ['Edit voucher 261203000165 and change the amount to $500', 'Modify voucher fields (e.g., amount, date, bank, or category).'],
      ['7-june-2026', "Display all entries for a specific day to select records for modification."],
      ['Change note to Reviewed for all vouchers on 7-june-2026',
       'One change across a whole day \u2014 still confirmed one at a time.'],
    ],
    hint: 'Modifies only named fields (amount, date, vendor, bank, category, '
      + ' check no., remarks). Details open in the right panel for preview. '
      + 'Changes remain pending until you click Save.',
  },
  view: {
    hue: C.inkMid, title: 'View transactions',
    blurb: 'View recent entries, full-day logs, or summary totals.',
    examples: [
      ['show my transactions', 'Display the most recent vouchers'],
      ['Show me recent payments', 'Alternative command for retrieving recent entries'],
      ['7-june-2026', "Display all entries posted on a specific date."],
      ['show my financial summary', 'View breakdown of total income, expenses, and net profit.'],
    ],
    hint: 'Select or enter any Voucher ID from the list to open it directly for editing.',
  },
  profile: {
    hue: C.blue, title: 'Create Profile (Customer, Vendor, or Employee)',
    blurb: 'Entity name is mandatory; contact and role metadata are optional.',
    examples: [
      ['Add ABC Trading LLC as a new customer, phone 555-123-4567, email billing@abctrading.com',
       'Full profile creation with integrated contact parameters.'],
      ['Add John Smith as a customer', 'Basic entity registration'],
      ['add vendor Handy Fix LLC, email ops@handyfix.com', 'Accounts Payable (AP) vendor setup'],
      ['add employee Maria Lopez, phone 555-0192, title Nurse', 'Payroll/Employee record setup with job title'],
    ],
    hint: 'Specify customer, vendor, or employee to assign the correct control account. '
      + ' To modify a profile later, enter "Edit Customer Profile" (or Vendor/Employee Profile) '
  },
  editprofile: {
    hue: C.blue, title: 'Specify the profile name, followed by the fields you wish to update.',
    blurb: 'Name the profile, then the field you want changed.',
    examples: [
      ["Change ABC Trading's phone number to 555-987-6543", 'Direct update using possessive format'],
      ['update customer ABC Trading, email accounts@abctrading.com',
       'Update using entity type and comma-separated fields.'],
      ["Update Handy Fix LLC's address to 12 Main St, city Austin",
       'Update multiple profile fields simultaneouslye'],
      ['update vendor Handy Fix LLC, title Facilities Manager', 'Modify any stored metadata field'],
    ],
    hint: ' The profile retains its unique account code and transaction history\u2014only master data details are updated.'
      + ' Entity classification (Customer, Vendor, or Employee) is permanently linked to the account code sequence; create a new profile if a different entity type is required. '
  },
  editchart: {
    hue: C.violet, title: 'Rename or Deactivate an Account',
    blurb: 'Change what an account is called, or take it out of your chart.',
    examples: [
      ['rename account Fuel to Fuel and Oil', 'Update account display name.'],
      ['deactivate account Tolls', 'Remove account from selection menus'],
      ['activate account Tolls', 'Restore a deactivated account to active status'],
      ['show chart', 'Review current Chart of Accounts hierarchy.'],
    ],
    hint: 'You can rename custom accounts created for your company; system-standard accounts maintain unified naming across organizations.'
      + 'Deactivating an account preserves historical voucher integrity while preventing future entries.'
      + 'Account parent hierarchies are permanently structured by account codes and cannot be relocated.'
  },
  chart: {
    hue: C.violet, title: 'Add to the Chart of Accounts',
    blurb: 'Enter the account title and specify its ledger classification or parent category.',
    examples: [
      ['add expense account Software Subscriptions', 'Create a standard general ledger expense account.g'],
      ['add bank account Chase Operating 4582', 'Register a new cash and bank equivalent sub-account'],
      ['add revenue account Service Revenue', 'Establish an operating income account.'],
      ['add account 401k Matching under Employee Benefits', 'Map a sub-ledger account under an existing parent category'],
    ],
    hint: 'Accounts with sub-accounts act as parent summary headers and cannot receive direct journal postings. LockInLedger will prompt you for confirmation before converting an active transactional account into a parent node. '
      + 'Use "Rename or Retire an Account" to modify existing ledger titles or deactivate unused accounts. '
  },
  chartview: {
    hue: C.violet, title: 'View the chart of accounts',
    blurb: ' Browse your complete general ledger structure organized by account classification',
    examples: [
      ['show chart', 'Display the complete account hierarchy tree'],
      ['expense chart', 'Filter view to expense accounts only.'],
      ['revenue chart', 'Filter view to revenue and income accounts.'],
      ['asset chart', 'Filter view to bank, cash, and asset accounts.'],
    ],
    hint: ' Selecting any account from the hierarchy tree automatically inserts its title into the LockInLedger command bar '
      + 'for quick voucher entry',
  },
};

// The opening screen: one friendly sentence, then the four things people
// actually come here to do, as cards. Each card is a real command — clicking
// one runs it, so the first screen is usable rather than decorative.
const QUICK_EXAMPLES = [
  { key: 'cpv', title: 'Create CPV', mode: 'cpv',
    Icon: ArrowOutwardIcon, hue: C.accent, soft: C.accentSoft,
    example: 'Paid $450 to Handy Fix LLC for repair and maintenance '
      + 'from Bank of America 9523' },
  { key: 'crv', title: 'Create CRV', mode: 'crv',
    Icon: SouthWestIcon, hue: '#065F46', soft: C.okSoft,
    example: 'Received $1,250 from ABC Trading for invoice 2045 into Chase Bank 4582' },
  { key: 'update', title: 'Edit Voucher', mode: 'update',
    Icon: EditIcon, hue: C.warn, soft: C.warnSoft,
    example: 'Edit voucher 260902000001 and change the amount to $500' },
  { key: 'profile', title: 'Add Customer', mode: 'profile',
    Icon: PersonAddIcon, hue: C.blue, soft: C.blueSoft,
    example: 'Add ABC Trading LLC as a new customer, phone 555-123-4567' },
];

const QuickExamples = ({ onPick }) => (
  <Box sx={{ mt: 1.25, border: `1px solid ${C.line}`, borderRadius: '12px',
             overflow: 'hidden', background: C.surface }}>
    <Box sx={{ px: 1.75, pt: 1.5, pb: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.9 }}>
        <AutoAwesomeIcon sx={{ fontSize: 16, color: C.accent }} />
        <Typography sx={{ fontSize: 13.5, fontWeight: 700, color: C.ink }}>
          Quick Examples
        </Typography>
      </Box>
      <Typography sx={{ fontSize: 11.5, color: C.inkMute, mt: 0.15 }}>
        Click one to try it, or type your own request below.
      </Typography>
    </Box>

    <Box sx={{ px: 1.5, pb: 1.5, display: 'grid', gap: 1,
               gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' } }}>
      {QUICK_EXAMPLES.map((q) => (
        <Box
          key={q.key}
          role="button"
          tabIndex={0}
          onClick={() => onPick(q.example)}
          onKeyDown={(e) => { if (e.key === 'Enter') onPick(q.example); }}
          sx={{
            display: 'flex', gap: 1.1, p: 1.25, borderRadius: '10px',
            border: `1px solid ${C.line}`, cursor: 'pointer', minWidth: 0,
            transition: 'border-color .12s, background .12s',
            '&:hover': { borderColor: q.hue, background: q.soft },
            '&:focus-visible': { outline: `2px solid ${q.hue}`, outlineOffset: 2 },
          }}
        >
          <Box sx={{ width: 30, height: 30, borderRadius: '9px', flexShrink: 0,
                     display: 'grid', placeItems: 'center',
                     background: q.soft, color: q.hue }}>
            <q.Icon sx={{ fontSize: 16 }} />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 12.5, fontWeight: 600, color: C.ink,
                              lineHeight: 1.35 }}>
              {q.title}
            </Typography>
            <Typography sx={{ fontSize: 11.5, color: C.inkMute, lineHeight: 1.45,
                              overflowWrap: 'anywhere' }}>
              “{q.example}”
            </Typography>
          </Box>
        </Box>
      ))}
    </Box>

    <Box sx={{ display: 'flex', gap: 1, px: 1.75, py: 1.1,
               borderTop: `1px solid ${C.line}`, background: C.okSoft }}>
      <LightbulbOutlinedIcon sx={{ fontSize: 15, color: '#065F46', flexShrink: 0,
                                   mt: '1px' }} />
      <Typography sx={{ fontSize: 11.5, color: C.inkMid, lineHeight: 1.5 }}>
        <b>Tip</b>&nbsp; Include the amount, vendor name, description, bank account, <br/>
        and any updates(like category, invoice number, or vendor email).

      </Typography>
    </Box>
  </Box>
);

const GREETING = {
  id: 1,
  type: 'bot',
  timestamp: new Date(),
  quickExamples: true,
  content: "Hello! I'm your accounting assistant. I can help you with tasks like "
    + 'recording payments, creating vouchers, finding transactions, and more.\n\n'
    + 'What would you like to do today?',
};

// One card, used for the opening message and for every toolbar button.
const IntroCard = ({ intro, onPick }) => {
  const hue = intro.hue || C.accent;
  return (
    <Box sx={{ mt: 1.25, border: `1px solid ${C.line}`, borderRadius: '10px',
               overflow: 'hidden', background: C.surface }}>
      <Box sx={{ px: 1.75, py: 1, borderBottom: `1px solid ${C.line}`,
                 background: C.raised, borderLeft: `3px solid ${hue}` }}>
        <Typography sx={{ fontSize: 12.5, fontWeight: 700, color: hue }}>
          {intro.title}
        </Typography>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>{intro.blurb}</Typography>
      </Box>
      <Box sx={{ px: 1.25, py: 0.6 }}>
        {intro.examples.map(([cmd, what]) => (
          <Box
            key={cmd}
            onClick={() => onPick?.(cmd)}
            sx={{ px: 1, py: 0.6, borderRadius: '6px', cursor: 'pointer',
                  '&:hover': { background: C.raised } }}
          >
            <Mono sx={{ fontSize: 12, color: hue, display: 'block',
                        overflowWrap: 'anywhere', lineHeight: 1.45 }}>
              {cmd}
            </Mono>
            <Typography sx={{ fontSize: 11, color: C.inkMute }}>{what}</Typography>
          </Box>
        ))}
      </Box>
      <Box sx={{ display: 'flex', gap: 1, px: 1.75, py: 1,
                 borderTop: `1px solid ${C.line}`, background: C.raised }}>
        <LightbulbOutlinedIcon sx={{ fontSize: 15, color: C.warn, flexShrink: 0, mt: '1px' }} />
        <Typography sx={{ fontSize: 11.5, color: C.inkMid, lineHeight: 1.5 }}>
          {intro.hint}
        </Typography>
      </Box>
    </Box>
  );
};

// The rail's resting state: every command the assistant actually has, as a
// button. The field-guide cards that used to live here answered "what fields
// does a CPV need?" — a question the review panel answers better, and only
// once you have something to review.
//
// Nothing is listed that the backend cannot do. Editing a customer or a chart
// account is deliberately absent: both are create-only today.
const QUICK_ACTIONS = [
  { key: 'cpv', label: 'Create CPV', hint: 'Payment',
    Icon: ArrowOutwardIcon, mode: 'cpv', prefill: 'Paid ',
    hue: C.accent, soft: C.accentSoft },
  { key: 'crv', label: 'Create CRV', hint: 'Recieved',
    Icon: SouthWestIcon, mode: 'crv', prefill: 'Received ',
    hue: '#065F46', soft: C.okSoft },
  { key: 'statement', label: 'Read a Statement', hint: 'CRV and CPV from a PDF',
    Icon: UploadFileIcon, mode: 'statement', prefill: '', upload: true,
    hue: C.violet, soft: C.violetSoft },
  { key: 'update', label: 'Edit Voucher', hint: 'By ID or by Date',
    Icon: EditIcon, mode: 'update', prefill: '',
    hue: C.warn, soft: C.warnSoft },
  { key: 'view', label: 'View Transactions', hint: 'See your latest entries',
    Icon: ReceiptLongIcon, mode: 'view', prefill: 'show my transactions',
    hue: C.inkMid, soft: C.raised },
  { key: 'profile', label: 'Add Customer', hint: 'Add a customer, vendor, or employee',
    Icon: PersonAddIcon, mode: 'profile', prefill: 'add customer ',
    hue: C.blue, soft: C.blueSoft },
  { key: 'editprofile', label: 'Edit Customer', hint: 'Update phone, email, or address',
    Icon: ManageAccountsIcon, mode: 'editprofile', prefill: 'update customer ',
    hue: C.blue, soft: C.blueSoft },
  { key: 'chart', label: 'Create Chart of Accounts', hint: 'A new ledger account',
    Icon: AccountTreeIcon, mode: 'chart', prefill: 'add account ',
    hue: C.violet, soft: C.violetSoft },
  { key: 'editchart', label: 'Rename or Deactivate Account', hint: 'Manage your chart of accounts',
    Icon: DriveFileRenameIcon, mode: 'editchart', prefill: 'rename account ',
    hue: C.violet, soft: C.violetSoft },
  { key: 'chartview', label: 'View Chart of Accounts', hint: 'See your available accounts',
    Icon: ListAltIcon, mode: 'chartview', prefill: 'show chart',
    hue: C.violet, soft: C.violetSoft },
];

// Things people ask that are not on a button.
const MORE_HELP = [
  'Show me recent payments',
  'Show my financial summary',
  'Vouchers on 7-june-2026',
  'Void 260902000001',
];

const QuickActionsPanel = ({ onRun, onPick, mode }) => (
  <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0,
             position: 'relative' }}>
    <Box sx={{ flex: 1, overflowY: 'auto', p: 1.5, pb: 5 }}>
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.ink, mb: 1.25 }}>
        Quick Actions
      </Typography>

      {QUICK_ACTIONS.map((a) => {
        const on = mode === a.mode;
        return (
          <Box
            key={a.key}
            role="button"
            tabIndex={0}
            onClick={() => onRun(a)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onRun(a); }}
            sx={{
              display: 'flex', alignItems: 'center', gap: 1.25, mb: 0.75,
              px: 1.25, py: 1, borderRadius: '9px', cursor: 'pointer',
              background: on ? a.soft : C.surface,
              border: `1px solid ${on ? a.hue : C.line}`,
              transition: 'background .12s, border-color .12s, transform .12s',
              '&:hover': { background: a.soft, borderColor: a.hue,
                           transform: 'translateX(1px)' },
              '&:hover .qa-chev': { color: a.hue, transform: 'translateX(2px)' },
              '&:focus-visible': { outline: `2px solid ${a.hue}`, outlineOffset: 2 },
            }}
          >
            <Box sx={{ width: 28, height: 28, borderRadius: '8px', flexShrink: 0,
                       display: 'grid', placeItems: 'center',
                       background: a.soft, color: a.hue }}>
              <a.Icon sx={{ fontSize: 16 }} />
            </Box>
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Typography sx={{ fontSize: 12.5, fontWeight: 600, color: C.ink,
                                lineHeight: 1.35 }}>
                {a.label}
              </Typography>
              <Typography sx={{ fontSize: 11, color: C.inkMute, lineHeight: 1.35 }}>
                {a.hint}
              </Typography>
            </Box>
            <KeyboardArrowDownIcon
              className="qa-chev"
              sx={{ fontSize: 16, color: C.inkMute, flexShrink: 0,
                    transform: 'rotate(-90deg)', transition: '.12s' }}
            />
          </Box>
        );
      })}

      <Box sx={{ mt: 2, p: 1.5, borderRadius: '10px', background: C.raised,
                 border: `1px solid ${C.line}` }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.75 }}>
          <AutoAwesomeIcon sx={{ fontSize: 15, color: C.accent }} />
          {/* <Typography sx={{ fontSize: 12.5, fontWeight: 700, color: C.ink }}>
            Need more help?
          </Typography> */}
        </Box>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 0.75 }}>
          Try asking something like:
        </Typography>
        {MORE_HELP.map((t) => (
          <Box
            key={t}
            onClick={() => onPick(t)}
            sx={{ display: 'flex', gap: 0.75, py: 0.4, cursor: 'pointer',
                  '&:hover .qa-ask': { color: C.accent } }}
          >
            <Box sx={{ width: 4, height: 4, borderRadius: '50%', mt: '7px',
                       flexShrink: 0, background: C.lineStrong }} />
            <Typography className="qa-ask"
                        sx={{ fontSize: 11.5, color: C.inkMid, lineHeight: 1.5 }}>
              “{t}”
            </Typography>
          </Box>
        ))}
      </Box>
    </Box>

    {/* "How it works" used to sit at the bottom of every scroll, in the way of
        the thing people came for. Same words, one hover away. */}
    <HoverHelp />
  </Box>
);

const HoverHelp = () => {
  const [open, setOpen] = useState(false);
  return (
    <Box
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      sx={{ position: 'absolute', right: 12, bottom: 12, zIndex: 3 }}
    >
      {open ? (
        <Box
          sx={{
            position: 'absolute', right: 0, bottom: 40, width: 258,
            p: 1.5, borderRadius: '8px', background: C.surface,
            border: `1px solid #D6E0EF`, boxShadow: '0 8px 24px rgba(16,24,40,.14)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.6 }}>
            <LightbulbOutlinedIcon sx={{ fontSize: 15, color: C.accent }} />
            <Typography sx={{ fontSize: 11.5, fontWeight: 700, color: C.accent }}>
              How it works
            </Typography>
          </Box>
          <Typography sx={{ fontSize: 11, color: C.inkMid, lineHeight: 1.65 }}>
            Say it in one sentence — order does not matter, and anything you leave
            out is either defaulted or asked for. Press Enter and the entry comes
            back on this side as a draft you can correct. <b>Nothing reaches the
            ledger until you press Post.</b>
          </Typography>
          <Box sx={{ mt: 0.9, pt: 0.9, borderTop: `1px solid #D6E0EF` }}>
            <Typography sx={{ fontSize: 11, color: C.inkMid, lineHeight: 1.65 }}>
              A filled dot is required, a hollow one optional. Account names work
              either way — “Repair and Maintenance” or the full
              “EXPENSE/Repair and Maintenance”.
            </Typography>
          </Box>
        </Box>
      ) : null}
      <Box
        aria-label="How it works"
        sx={{
          width: 30, height: 30, borderRadius: '50%', cursor: 'default',
          display: 'grid', placeItems: 'center', background: C.surface,
          border: `1px solid ${open ? C.accent : C.lineStrong}`,
          color: open ? C.accent : C.inkMute,
          boxShadow: '0 1px 3px rgba(16,24,40,.08)', transition: '.12s',
        }}
      >
        <HelpOutlineIcon sx={{ fontSize: 17 }} />
      </Box>
    </Box>
  );
};

// Session history — the rail that used to always be there, now opened from
// the toolbar.
const HistoryPanel = ({ activity, onPick, onClose }) => (
  <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
    <Box sx={{ px: 2, py: 1.5, borderBottom: `1px solid ${C.line}`, display: 'flex',
               alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
      <Box sx={{ minWidth: 0 }}>
        <Typography sx={{ fontSize: 12, fontWeight: 600, color: C.ink }}>This session</Typography>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
          {activity.length} voucher{activity.length === 1 ? '' : 's'} touched
        </Typography>
      </Box>
      {onClose ? (
        <IconButton size="small" onClick={onClose} sx={{ color: C.inkMid }}>
          <CloseIcon sx={{ fontSize: 17 }} />
        </IconButton>
      ) : null}
    </Box>
    <Box sx={{ flex: 1, overflowY: 'auto', p: 1.25 }}>
      {activity.length === 0 ? (
        <Typography sx={{ fontSize: 12, color: C.inkMute, px: 0.75, py: 1 }}>
          Vouchers you post or edit appear here.
        </Typography>
      ) : (
        activity.map((a) => (
          <Box
            key={a.key}
            onClick={() => onPick(`show ${a.atId || a.voucherNo}`)}
            sx={{
              px: 1.25, py: 1, mb: 0.5, borderRadius: '6px', cursor: 'pointer',
              border: `1px solid ${C.line}`,
              '&:hover': { borderColor: C.lineStrong, background: C.raised },
            }}
          >
            <Box sx={{ display: 'flex', alignItems: 'center',
                       justifyContent: 'space-between', gap: 1 }}>
              <Mono sx={{ fontSize: 11.5, color: C.inkMid }}>{a.voucherNo}</Mono>
              <Pill label={a.entryType} tone="accent" />
            </Box>
            <Typography sx={{ fontFamily: MONO, fontSize: 13, fontWeight: 600,
                              color: C.ink, mt: 0.4 }}>
              ${money(a.amount)}
            </Typography>
            <Typography sx={{ fontSize: 11.5, color: C.inkMute }} noWrap>
              {a.party || '—'} · {a.verb} {clockTime(a.at)}
            </Typography>
          </Box>
        ))
      )}
    </Box>
  </Box>
);

export default function App() {
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [mode, setMode] = useState('post');
  const [connection, setConnection] = useState({ state: 'checking', detail: '' });
  const [accountCount, setAccountCount] = useState(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activity, setActivity] = useState([]);
  const [snack, setSnack] = useState(null);
  // The confirmation step. `draft` is the editable, still-unwritten voucher;
  // it exists only between pressing Enter and pressing Post.
  const [draft, setDraft] = useState(null);
  const [draftMsgId, setDraftMsgId] = useState(null);
  const [posting, setPosting] = useState(false);
  // A bulk edit is a queue of drafts stepped through one at a time; a single
  // edit is just a queue of one.
  const [queue, setQueue] = useState(null);   // { drafts, index, saved:Set }
  const [postError, setPostError] = useState(null);
  // A statement held between the upload and the answer to "which account is
  // this?", so answering re-reads the file the person already chose rather
  // than making them find it again.
  const [pendingStatement, setPendingStatement] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef(null);
  // The real chart and party list, so a correction picks a code that exists.
  const [pickers, setPickers] = useState({ bank: [], income: [], expense: [], all: [] });
  const [parties, setParties] = useState([]);

  const endRef = useRef(null);
  const inputRef = useRef(null);

  // One axios instance for the app's lifetime — rebuilding it each render
  // re-registers interceptors and leaks them.
  const api = useMemo(
    () => axios.create({ baseURL: API_BASE_URL, timeout: 60000, headers: { 'Content-Type': 'application/json' } }),
    []
  );

  const showSnack = useCallback((message, severity = 'info') => setSnack({ message, severity }), []);

  const checkConnection = useCallback(async () => {
    setConnection({ state: 'checking', detail: '' });
    try {
      const r = await api.get('/api/health');
      const ok = r.data?.database === 'connected' || r.data?.status === 'ok' || r.data?.status === 'healthy';
      setConnection({ state: ok ? 'online' : 'degraded', detail: r.data?.database || '' });
      // The chart and the party list back the pickers in the review panel, so
      // a correction is always chosen from what actually exists.
      try {
        const a = await api.get('/api/accounts');
        const d = a.data || {};
        const all = d.all || d.accounts || (Array.isArray(a.data) ? a.data : []);
        setPickers({ bank: d.bank || all, income: d.income || [], expense: d.expense || [], all });
        setAccountCount(all.length || null);
      } catch {
        setAccountCount(null);
      }
      try {
        const p = await api.get('/api/parties');
        setParties(p.data?.parties || []);
      } catch {
        setParties([]);
      }
    } catch (e) {
      setConnection({ state: 'offline', detail: e?.message || 'unreachable' });
    }
  }, [api]);

  useEffect(() => {
    setSessionId('session_' + Date.now() + '_' + Math.random().toString(36).slice(2, 11));
    setMessages([{ ...GREETING, timestamp: new Date() }]);
    checkConnection();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  const loadIntoComposer = useCallback((text, focusOnly) => {
    setInputMessage(text);
    const el = inputRef.current;
    if (el) {
      el.focus();
      if (focusOnly) requestAnimationFrame(() => el.setSelectionRange(text.length, text.length));
    }
  }, []);

  // Shared by a direct post and by a confirmed draft, so a voucher looks the
  // same in the transcript however it got there.
  const recordVoucherResult = useCallback((data, { verb = 'posted' } = {}) => {
    const voucherNo = data.card?.voucher_number || data.voucher_number;
    const atId = data.card?.at_id || data.at_id;
    if (!voucherNo) return;
    setActivity((prev) => [
      {
        key: `${voucherNo}-${Date.now()}`,
        atId,
        voucherNo,
        entryType: data.card?.entry_type || data.entry_type,
        amount: data.card?.amount ?? data.amount,
        party: data.card?.party_name || data.party_name,
        verb,
        at: new Date(),
      },
      ...prev.filter((a) => a.voucherNo !== voucherNo),
    ]);
    showSnack(`${voucherNo} ${verb}`, verb === 'voided' ? 'warning' : 'success');
  }, [showSnack]);

  // A toolbar button sets the mode hint and seeds the composer; the operator
  // finishes the sentence.
  // Pressing a toolbar button is a question ("how do I record a receipt?"), so
  // it gets an answer in the transcript rather than only a changed placeholder.
  // Pressing the same one twice does not repeat the card.
  const runQuickAction = useCallback((a) => {
    setMode(a.mode);
    setInputMessage(a.prefill);
    // The one action that isn't a sentence to finish. It still shows its
    // intro first — a file dialog opening with no explanation is the worst
    // possible introduction to the one feature that reads sixty rows at once.
    if (a.upload) {
      setMessages((prev) => {
        const intro = INTRO[a.mode];
        const last = prev[prev.length - 1];
        if (!intro || last?.intro === intro) return prev;
        return [...prev, { id: Date.now(), type: 'bot', timestamp: new Date(),
                           intro, introKey: a.mode }];
      });
      fileRef.current?.click();
      return;
    }
    const intro = INTRO[a.mode];
    if (intro) {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.intro === intro) return prev;
        return [...prev, { id: Date.now(), type: 'bot', timestamp: new Date(),
                           intro, introKey: a.mode }];
      });
    }
    const el = inputRef.current;
    if (el) {
      el.focus();
      requestAnimationFrame(() =>
        el.setSelectionRange(a.prefill.length, a.prefill.length));
    }
  }, []);

  // Clears the transcript only — vouchers already posted are in the ledger,
  // and the session history keeps its record of them.
  const clearChat = useCallback(() => {
    setMessages([{ ...GREETING, timestamp: new Date() }]);
    setInputMessage('');
    setDraft(null);
    setDraftMsgId(null);
    setPostError(null);
    inputRef.current?.focus();
  }, []);

  // ---- A statement, read in --------------------------------------------
  //
  // Deliberately the same shape as sending a sentence: it posts, it gets a
  // queue of drafts back, and it opens the first one. There is no "import"
  // button anywhere after this point, because there is nothing to import —
  // the rows are saved by the same Save button as everything else, one at a
  // time. The file never touches the ledger; only the drafts do.
  const uploadStatement = useCallback(async (file, bankAccount) => {
    if (!file || isLoading) return;
    if (file.size > STATEMENT_MAX_MB * 1024 * 1024) {
      showSnack(`${file.name} is over ${STATEMENT_MAX_MB} MB`, 'warning');
      return;
    }

    setMessages((prev) => [...prev, {
      id: Date.now(), type: 'user', timestamp: new Date(),
      content: bankAccount ? `${file.name} — read it into ${bankAccount}`
                           : `${file.name}`,
      attachment: { name: file.name, size: file.size },
    }]);
    setIsLoading(true);
    setPendingStatement({ file });

    try {
      const form = new FormData();
      form.append('file', file);
      if (sessionId) form.append('session_id', sessionId);
      if (bankAccount) form.append('bank_account', bankAccount);

      // Reading sixty rows against the chart takes longer than a sentence
      // does, and the default timeout would cut it off mid-file.
      const { data } = await axios.post(`${API_BASE_URL}/api/statement/preview`,
                                        form, { timeout: 180000 });

      if (data.status === 'draft' && data.drafts?.length) {
        setMessages((prev) => [...prev, {
          id: Date.now() + 1, type: 'bot', timestamp: new Date(),
          content: data.message || data.analysis || '',
          card: data.card || null,
        }]);
        setQueue({ drafts: data.drafts, index: 0, saved: new Set() });
        setDraft(data.drafts[0]);
        setDraftMsgId(null);
        setPostError(null);
        setPendingStatement(null);
        return;
      }

      setMessages((prev) => [...prev, {
        id: Date.now() + 1, type: 'bot', timestamp: new Date(),
        content: data.message || data.analysis || 'Nothing was read in.',
        card: data.card || null,
        isError: data.status === 'error',
      }]);
      // The account question is the one failure worth staying open for: the
      // file is still here, so answering it re-reads rather than re-uploads.
      if (data.action !== 'statement_needs_bank') setPendingStatement(null);
      if (data.status === 'error') showSnack('Nothing was read in', 'warning');
    } catch (error) {
      setMessages((prev) => [...prev, {
        id: Date.now() + 1, type: 'bot', isError: true, timestamp: new Date(),
        content: friendlyNetworkError(error),
      }]);
      setPendingStatement(null);
      showSnack('Could not read that file', 'error');
    } finally {
      setIsLoading(false);
    }
  }, [api, sessionId, isLoading, showSnack]);

  const retryStatementWithAccount = useCallback((code) => {
    const acc = (pickers.all || []).find((a) => String(a.code) === String(code));
    if (pendingStatement?.file) {
      uploadStatement(pendingStatement.file, acc?.qualified || String(code));
    }
  }, [pendingStatement, pickers, uploadStatement]);

  const sendMessage = async (overrideText) => {
    const text = (overrideText ?? inputMessage).trim();
    if (!text || isLoading) return;

    setMessages((prev) => [...prev, { id: Date.now(), type: 'user', content: text, timestamp: new Date() }]);
    setInputMessage('');
    setIsLoading(true);

    try {
      // preview:true means a message that would CREATE a voucher comes back as
      // a draft instead of a posting. Everything else (show / update / void /
      // profile / chart) still executes, so this is the only call needed.
      const { data } = await api.post('/api/chat', {
        session_id: sessionId, message: text, mode, preview: true,
      });

      // ---- a whole day, opened as a queue ---------------------------------
      // "change party to 3S for all vouchers on 2026-09-04" comes back as many
      // drafts. Same queue the tick-list builds, so each one is still read and
      // saved on its own — the sentence is bulk, the writing is not.
      if (data.status === 'draft' && data.drafts?.length > 1) {
        setMessages((prev) => [...prev, {
          id: Date.now() + 1, type: 'bot', timestamp: new Date(),
          content: data.message || data.analysis || '',
        }]);
        setQueue({ drafts: data.drafts, index: 0, saved: new Set() });
        setDraft(data.drafts[0]);
        setDraftMsgId(null);
        setPostError(null);
        return;
      }

      // ---- the confirmation step -----------------------------------------
      if (data.status === 'draft' && data.draft) {
        const id = Date.now() + 1;
        const d = { ...data.draft, review_items: data.review_items || [] };
        setMessages((prev) => [
          ...prev,
          {
            id,
            type: 'bot',
            timestamp: new Date(),
            content: draftHeadline(data.review_items),
            suggestions: data.suggestions || [],
            draft: d,
          },
        ]);
        setDraft(d);
        setDraftMsgId(id);
        setPostError(null);
        return;
      }

      const isFreshVoucher = !!data.voucher_number && !data.card;
      // A record card already says everything the sentence above it said, in a
      // form that is easier to check. Two copies of the same result is noise,
      // so the card wins and the prose is dropped.
      const content = isFreshVoucher || SELF_EXPLANATORY.has(data.card?.kind)
        ? '' : data.message || data.analysis || 'Done.';

      setMessages((prev) => [
        ...prev,
        {
          id: Date.now() + 1,
          type: 'bot',
          timestamp: new Date(),
          content,
          // Lines the assistant offers as a one-click fix. They are the
          // person's own sentence with the missing part filled in, so sending
          // one posts — see _clarify_reply on the server.
          suggestions: data.suggestions || [],
          // A fresh post arrives as flat fields; VoucherCard normalises either
          // shape, so it renders the same as a looked-up voucher.
          card: data.card || (isFreshVoucher ? { kind: 'voucher', ...data } : null),
          // A business rejection (ambiguous account, locked voucher, unknown
          // party) is an error even though the HTTP call succeeded.
          isError: data.status === 'error',
        },
      ]);

      // `action` is set by the backend for the commands that change something.
      // A plain `show` must not be logged as activity or announced as a write.
      const wrote = isFreshVoucher ? 'posted' : { updated: 'updated', voided: 'voided' }[data.action];

      if (wrote && data.status !== 'error') {
        recordVoucherResult(data, { verb: wrote });
      } else if (data.action === 'profile' && data.status !== 'error') {
        showSnack(
          `${data.card?.company_name || 'Profile'} ${data.card?.created ? 'created' : 'already existed'}`,
          'success'
        );
      } else if (data.status === 'error') {
        showSnack('Nothing was written — see the reply', 'warning');
      }
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now() + 1,
          type: 'bot',
          isError: true,
          timestamp: new Date(),
          content: friendlyNetworkError(error),
        },
      ]);
      showSnack('Request failed', 'error');
    } finally {
      setIsLoading(false);
    }
  };

  // The reviewed draft goes back by CODE, so what is written is what was on
  // screen — the parser does not get a second turn.
  // Each kind of draft has its own commit endpoint; all three take the
  // reviewed values (codes, not names) and return the same reply shape.
  const COMMIT_ENDPOINT = { voucher: '/api/commit', party: '/api/commit/party',
                            account: '/api/commit/account', edit: '/api/commit/edit' };

  const commitBody = (d, sessionId) => {
    const kind = d.kind || 'voucher';
    if (kind === 'party') {
      const KEYS = ['company_name', 'person_name', 'email', 'phone', 'fax',
                    'address', 'city', 'state', 'zipcode', 'job_title',
                    'sale_tax_no', 'fedral_id_no', 'business_desc',
                    'other_desc', 'p_account'];
      // Kept as typed for the comparison below: '' means "cleared", which is
      // a real change, while null would look like "not mentioned".
      const raw = Object.fromEntries(KEYS.map((k) => [k, d[k] ?? '']));
      if (!d.p_code) {
        return {
          session_id: sessionId, p_type: d.p_type,
          ...Object.fromEntries(KEYS.map((k) => [k, raw[k] || null])),
          source_message: d.source_message || null,
        };
      }
      // An edit sends ONLY what actually changed. Rewriting the whole record
      // would stamp over fields nobody touched — including anything a
      // colleague changed while this draft was open.
      const o = d.original || {};
      const changed = Object.fromEntries(
        KEYS.filter((k) => raw[k] !== String(o[k] ?? '')).map((k) => [k, raw[k]]));
      return {
        session_id: sessionId, p_code: d.p_code, p_type: d.p_type, ...changed,
        source_message: d.source_message || null,
      };
    }
    if (kind === 'account') {
      // `op` means an existing account is being renamed or retired; without it
      // this is a new account and needs a parent.
      return {
        session_id: sessionId, name: d.name || null, level: d.level || null,
        parent_code: d.parent_code || null,
        op: d.op || null, code: d.code || null,
        source_message: d.source_message || null,
      };
    }
    if (kind === 'edit') {
      // A void reverses the voucher as it stands — sending the fields would
      // only invite the server to save them first.
      if (d.op === 'void') {
        return { session_id: sessionId, at_id: d.at_id, op: 'void' };
      }
      return {
        session_id: sessionId, at_id: d.at_id,
        amount: Number(d.amount),
        transaction_date: d.transaction_date,
        party_code: d.party_code || null,
        bank_acc_code: d.bank_acc_code,
        category_acc_code: d.category_acc_code,
        cheque_no: d.cheque_no ?? '',
        description: d.description ?? '',
      };
    }
    return {
      session_id: sessionId,
      entry_type: d.entry_type,
      amount: Number(d.amount),
      transaction_date: d.transaction_date,
      party_code: d.party_code || null,
      party_name: d.party_name || null,
      bank_acc_code: d.bank_acc_code,
      category_acc_code: d.category_acc_code,
      cheque_no: d.cheque_no || null,
      description: d.description || null,
      source_message: d.source_message || null,
    };
  };

  const postDraft = async (d) => {
    if (posting) return;
    const kind = d.kind || 'voucher';
    setPosting(true);
    setPostError(null);
    try {
      const { data } = await api.post(COMMIT_ENDPOINT[kind] || '/api/commit',
                                      commitBody(d, sessionId));

      if (data.status === 'error') {
        setPostError(data.message || 'The ledger refused this entry.');
        showSnack('Nothing was written — see the panel', 'warning');
        return;
      }

      // A voucher rebuilds its card from the flat payload; the other two
      // already come back with the card their chat command would have made.
      setMessages((prev) => [
        ...prev.map((m) => (m.id === draftMsgId ? { ...m, draftPosted: true } : m)),
        {
          id: Date.now() + 1,
          type: 'bot',
          timestamp: new Date(),
          content: kind === 'voucher' || SELF_EXPLANATORY.has(data.card?.kind)
            ? '' : (data.message || data.analysis || 'Done.'),
          card: kind === 'voucher' ? { kind: 'voucher', ...data } : data.card || null,
        },
      ]);

      if (kind === 'voucher') {
        recordVoucherResult(data, { verb: 'posted' });
        // A brand new party is now a real profile — keep the picker current.
        if (!d.party_code && data.party_code) {
          setParties((prev) => [
            ...prev,
            { p_code: data.party_code, p_type: data.party_type,
              company_name: data.party_name, person_name: data.party_name },
          ]);
        }
      } else if (kind === 'party') {
        showSnack(`${data.card?.company_name || 'Profile'} ` +
                  `${data.card?.created ? 'created' : 'already existed'}`, 'success');
        if (data.card?.p_code) {
          setParties((prev) => [
            ...prev.filter((p) => String(p.p_code) !== String(data.card.p_code)),
            { p_code: data.card.p_code, p_type: data.card.p_type,
              company_name: data.card.company_name,
              person_name: data.card.person_name },
          ]);
        }
      } else if (kind === 'edit') {
        recordVoucherResult(data, { verb: d.op === 'void' ? 'voided' : 'updated' });
      } else {
        showSnack(`${data.card?.name || 'Account'} added to the chart`, 'success');
        // The chart grew — refresh the pickers so it can be used at once.
        checkConnection();
      }

      // In a queue, saving moves to the next one rather than closing the
      // panel — that is the whole point of stepping through a bulk edit, and
      // of a statement.
      //
      // Keyed on draftKey rather than at_id: an edit queue is identified by
      // the voucher it opens, but a statement queue is sixty vouchers that do
      // not exist yet and have no id to key on.
      if (queue && queue.drafts.length > 1) {
        const saved = new Set(queue.saved).add(draftKey(d));
        const nextIdx = queue.drafts.findIndex((x, i) => i > queue.index && !saved.has(draftKey(x)));
        const fallback = queue.drafts.findIndex((x) => !saved.has(draftKey(x)));
        const go = nextIdx >= 0 ? nextIdx : fallback;
        if (go >= 0) {
          setQueue({ ...queue, index: go, saved });
          setDraft(queue.drafts[go]);
          setPosting(false);
          return;
        }
        setQueue(null);
        showSnack(`All ${saved.size} vouchers saved`, 'success');
      }

      setDraft(null);
      setDraftMsgId(null);
      setQueue(null);
    } catch (error) {
      setPostError(friendlyNetworkError(error));
      showSnack('Request failed', 'error');
    } finally {
      setPosting(false);
    }
  };

  const discardDraft = () => {
    setDraft(null);
    setDraftMsgId(null);
    setQueue(null);
    setPostError(null);
    inputRef.current?.focus();
  };

  // Ticked vouchers, opened as drafts. The optional instruction is resolved
  // server-side onto every one of them — and still shown for confirmation.
  const bulkEdit = useCallback(async (atIds, instruction) => {
    if (!atIds?.length || posting) return;
    setPosting(true);
    setPostError(null);
    try {
      const { data } = await api.post('/api/edit/batch', {
        session_id: sessionId, at_ids: atIds, instruction: instruction || null,
      });
      if (data.status === 'error' || !data.drafts?.length) {
        showSnack(data.message || 'Nothing there can be edited', 'warning');
        setMessages((prev) => [...prev, {
          id: Date.now(), type: 'bot', timestamp: new Date(), isError: true,
          content: data.message || 'Nothing there can be edited.',
        }]);
        return;
      }
      const skipped = (data.errors || []).length;
      setMessages((prev) => [...prev, {
        id: Date.now(), type: 'bot', timestamp: new Date(),
        content: `${data.drafts.length} voucher${data.drafts.length === 1 ? '' : 's'} `
               + `open for editing${skipped ? ` — ${skipped} skipped `
               + `(${(data.errors || []).map((e) => e.voucher_number || e.at_id).join(', ')})` : ''}.`
               + `\nStep through them on the right; each one saves on its own.`,
      }]);
      setQueue({ drafts: data.drafts, index: 0, saved: new Set() });
      setDraft(data.drafts[0]);
      setDraftMsgId(null);
    } catch (error) {
      showSnack('Could not open those vouchers', 'error');
    } finally {
      setPosting(false);
    }
  }, [api, sessionId, posting, showSnack]);

  // Move within the queue, keeping any edits made to the draft being left.
  const goToDraft = (idx) => {
    if (!queue || idx < 0 || idx >= queue.drafts.length) return;
    const drafts = queue.drafts.slice();
    if (draft) drafts[queue.index] = draft;
    setQueue({ ...queue, drafts, index: idx });
    setDraft(drafts[idx]);
    setPostError(null);
  };

  const reopenDraft = useCallback((d) => {
    setDraft(d);
    setPostError(null);
  }, []);

  const draftPanel = draft ? (
    <DraftPanel
      draft={draft}
      setDraft={setDraft}
      pickers={pickers}
      parties={parties}
      onPost={postDraft}
      onDiscard={discardDraft}
      posting={posting}
      error={postError}
      queue={queue ? {
        index: queue.index,
        total: queue.drafts.length,
        savedCount: queue.saved.size,
        onPrev: () => goToDraft(queue.index - 1),
        onNext: () => goToDraft(queue.index + 1),
      } : null}
    />
  ) : null;


  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ height: '100vh', display: 'flex', flexDirection: 'column', background: C.bg }}>
        {/* Header ------------------------------------------------------- */}
        {/* One bar, not two. The quick-create row moved into the rail as
            Quick Actions, so the top of the screen is identity and history —
            the two things that belong there. */}
        <Box
          component="header"
          sx={{
            height: 58, flexShrink: 0, px: { xs: 1.75, md: 2.5 }, display: 'flex',
            alignItems: 'center', gap: 1.25, background: C.surface,
            borderBottom: `1px solid ${C.line}`,
          }}
        >
          <BotAvatar size={34} />
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 14.5, fontWeight: 700, letterSpacing: '-0.01em',
                              lineHeight: 1.25 }}>
              LedgerAssist
            </Typography>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.6 }}>
              <Box sx={{ width: 6, height: 6, borderRadius: '50%',
                         background: connection.state === 'offline' ? C.err : C.ok }} />
              <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
                {connection.state === 'offline' ? 'Offline' : 'Online'}
              </Typography>
            </Box>
          </Box>

          <Box sx={{ flex: 1 }} />

          {connection.state === 'offline' ? (
            <Button
              size="small"
              onClick={checkConnection}
              startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
              sx={{ fontSize: 12, color: C.err, border: `1px solid #FECDCA`,
                    borderRadius: '7px', minHeight: 30, px: 1.25,
                    '&:hover': { background: C.errSoft } }}
            >
              Reconnect
            </Button>
          ) : null}

          <Button
            size="small"
            onClick={() => setHistoryOpen((o) => !o)}
            startIcon={<HistoryIcon sx={{ fontSize: 16 }} />}
            sx={{
              minHeight: 32, px: 1.25, fontSize: 12.5, borderRadius: '8px',
              whiteSpace: 'nowrap',
              color: historyOpen ? C.accent : C.inkMid,
              background: historyOpen ? C.accentSoft : 'transparent',
              border: `1px solid ${historyOpen ? '#D6E0EF' : 'transparent'}`,
              '& .MuiButton-startIcon': { mr: 0.6 },
              '&:hover': { background: historyOpen ? C.accentSoft : C.raised },
            }}
          >
            Chat History{activity.length ? ` (${activity.length})` : ''}
          </Button>

          <Tooltip title="Clear the conversation — posted vouchers are unaffected">
            <IconButton
              size="small"
              onClick={clearChat}
              sx={{ width: 32, height: 32, borderRadius: '8px', color: C.inkMute,
                    '&:hover': { background: C.raised, color: C.inkMid } }}
            >
              <DeleteOutlineIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </Tooltip>

          <UserAvatar size={32} />
        </Box>

        {/* Body --------------------------------------------------------- */}
        <Box sx={{ flex: 1, minHeight: 0, display: 'grid',
                   gridTemplateColumns: { xs: '1fr',
                     lg: draft ? '1fr 380px' : '1fr 320px' } }}>
          {/* Conversation */}
          <Box
            sx={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0,
                  position: 'relative' }}
            onDragOver={(e) => {
              if (!e.dataTransfer?.types?.includes('Files')) return;
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={(e) => {
              // Only when the pointer leaves the region itself — dragging over
              // a child fires dragleave on the parent and would flicker.
              if (e.currentTarget.contains(e.relatedTarget)) return;
              setDragOver(false);
            }}
            onDrop={(e) => {
              if (!e.dataTransfer?.files?.length) return;
              e.preventDefault();
              setDragOver(false);
              uploadStatement(e.dataTransfer.files[0]);
            }}
          >
            {dragOver ? (
              <Box sx={{
                position: 'absolute', inset: 10, zIndex: 5, borderRadius: '12px',
                border: `2px dashed ${C.accent}`, background: 'rgba(255,255,255,0.92)',
                display: 'flex', flexDirection: 'column', alignItems: 'center',
                justifyContent: 'center', gap: 1, pointerEvents: 'none',
              }}>
                <UploadFileIcon sx={{ fontSize: 30, color: C.accent }} />
                <Typography sx={{ fontSize: 13.5, fontWeight: 600, color: C.accent }}>
                  Drop a statement to read it
                </Typography>
                <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
                  PDF or CSV — nothing is posted until you save each row
                </Typography>
              </Box>
            ) : null}
            <Box sx={{ flex: 1, overflowY: 'auto', px: { xs: 2, md: 4 }, py: 3 }}>
              <Box sx={{ maxWidth: 780, mx: 'auto' }}>
                {messages.map((m) => (
                  <Message key={m.id} msg={m} onCommand={loadIntoComposer}
                           onSuggest={(t) => sendMessage(t)}
                           onReopenDraft={reopenDraft} onBulkEdit={bulkEdit}
                           onStatementAccount={retryStatementWithAccount}
                           busy={posting || isLoading} />
                ))}
                {isLoading ? (
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, mb: 2.5,
                             color: C.inkMute }}>
                    <BotAvatar />
                    <CircularProgress size={12} thickness={5} sx={{ color: C.inkMute }} />
                    <Typography sx={{ fontSize: 12.5 }}>Working…</Typography>
                  </Box>
                ) : null}
                <div ref={endRef} />
              </Box>
            </Box>

            {/* Composer */}
            <Box
              sx={{
                flexShrink: 0, borderTop: `1px solid ${C.line}`, background: C.bg,
                px: { xs: 2, md: 4 }, py: 1.5,
              }}
            >
              {/* One box: what you type, and an example of what to type. The
                  keyboard hints that used to sit under it told a first-time
                  user nothing they wanted to know. */}
              <Box
                sx={{
                  maxWidth: 780, mx: 'auto', border: `1px solid ${C.line}`,
                  borderRadius: '12px', background: C.surface, px: 1.5, py: 1.1,
                  transition: 'border-color .12s',
                  '&:focus-within': { borderColor: C.accent },
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 1 }}>
                  <AutoAwesomeIcon sx={{ fontSize: 17, color: C.accent, flexShrink: 0,
                                         mb: '3px' }} />
                  <input
                    ref={fileRef}
                    type="file"
                    accept={STATEMENT_TYPES}
                    style={{ display: 'none' }}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      // Cleared so choosing the SAME file twice still fires —
                      // a re-upload after picking the account is the common
                      // case, not the rare one.
                      e.target.value = '';
                      if (f) uploadStatement(f);
                    }}
                  />
                  <TextField
                    inputRef={inputRef}
                    fullWidth
                    multiline
                    maxRows={8}
                    variant="standard"
                    value={inputMessage}
                    onChange={(e) => setInputMessage(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        sendMessage();
                      }
                    }}
                    placeholder="Type your request here…"
                    disabled={isLoading}
                    sx={{
                      '& .MuiInput-root:before, & .MuiInput-root:after': { display: 'none' },
                      '& .MuiInputBase-root': { fontSize: 14, color: C.ink },
                      '& .MuiInputBase-input::placeholder': { color: C.inkMute, opacity: 1 },
                    }}
                  />
                  <Tooltip title="Read a bank or card statement (PDF, CSV)">
                    <span>
                      <IconButton
                        onClick={() => fileRef.current?.click()}
                        disabled={isLoading}
                        sx={{
                          width: 34, height: 34, flexShrink: 0, borderRadius: '9px',
                          color: C.inkMid, border: `1px solid ${C.line}`,
                          '&:hover': { borderColor: C.accent, color: C.accent,
                                       background: C.accentSoft },
                        }}
                      >
                        <AttachFileIcon sx={{ fontSize: 17 }} />
                      </IconButton>
                    </span>
                  </Tooltip>
                  <IconButton
                    onClick={() => sendMessage()}
                    disabled={!inputMessage.trim() || isLoading}
                    sx={{
                      width: 38, height: 38, flexShrink: 0, borderRadius: '10px',
                      background: C.accent, color: '#fff',
                      '&:hover': { background: '#16304F' },
                      '&.Mui-disabled': { background: '#E8EBF0', color: C.inkMute },
                    }}
                  >
                    <SendIcon sx={{ fontSize: 18, transform: 'rotate(-20deg)',
                                    ml: '-2px', mt: '1px' }} />
                  </IconButton>
                </Box>
                <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75, mt: 0.6,
                           pl: 3.25, pr: 6 }}>
                  <LightbulbOutlinedIcon sx={{ fontSize: 14, color: C.warn, flexShrink: 0,
                                               mt: '1px' }} />
                  <Typography sx={{ fontSize: 11.5, color: C.inkMute, lineHeight: 1.45,
                                    overflow: 'hidden', textOverflow: 'ellipsis',
                                    whiteSpace: 'nowrap' }}>
                    For example: “{PLACEHOLDERS[mode] || PLACEHOLDERS.post}”
                  </Typography>
                </Box>
              </Box>
            </Box>
          </Box>

          {/* Right rail. A pending draft owns it; History takes it when asked
              for; otherwise it rests on the field guide. */}
          <Box
            sx={{
              display: { xs: 'none', lg: 'flex' }, flexDirection: 'column',
              width: { lg: 288, xl: 312 }, flexShrink: 0,
              borderLeft: `1px solid ${C.line}`, background: C.surface, minHeight: 0,
            }}
          >
            {draft ? draftPanel
              : historyOpen ? (
                <HistoryPanel
                  activity={activity}
                  onPick={loadIntoComposer}
                  onClose={() => setHistoryOpen(false)}
                />
              ) : (
                <QuickActionsPanel
                  onRun={runQuickAction}
                  onPick={(t) => loadIntoComposer(t, true)}
                  mode={mode}
                />
              )}
          </Box>
        </Box>
      </Box>

      {/* Below lg there is no rail, so history becomes a sheet too. */}
      <Drawer
        anchor="right"
        open={historyOpen && !draft}
        onClose={() => setHistoryOpen(false)}
        sx={{ display: { xs: 'block', lg: 'none' } }}
        PaperProps={{ sx: { width: { xs: '100%', sm: 320 }, background: C.surface } }}
      >
        <HistoryPanel
          activity={activity}
          onPick={(t) => { loadIntoComposer(t); setHistoryOpen(false); }}
          onClose={() => setHistoryOpen(false)}
        />
      </Drawer>

      {/* Below lg there is no rail, so the review step becomes a sheet. */}
      <Drawer
        anchor="right"
        open={!!draft}
        onClose={discardDraft}
        sx={{ display: { xs: 'block', lg: 'none' } }}
        PaperProps={{ sx: { width: { xs: '100%', sm: 320 }, background: C.surface } }}
      >
        {draftPanel}
      </Drawer>

      <Snackbar
        open={!!snack}
        autoHideDuration={4000}
        onClose={() => setSnack(null)}
        // Bottom-CENTRE sat on top of the composer and swallowed the next
        // thing typed. Lifted clear of it, and out of the way of the cursor.
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        sx={{ bottom: { xs: 104, sm: 112 }, right: { xs: 12, sm: 24 } }}
      >
        <Alert
          severity={snack?.severity || 'info'}
          variant="outlined"
          onClose={() => setSnack(null)}
          sx={{ fontSize: 12.5, background: C.surface, borderRadius: '6px' }}
        >
          {snack?.message}
        </Alert>
      </Snackbar>
    </ThemeProvider>
  );
}
